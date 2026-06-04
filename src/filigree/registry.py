"""File identity registry backends.

Path normalization (CONTRACT-4)
-------------------------------
All paths sent to Clarion — single-file ``GET /api/v1/files`` and batched
``POST /api/v1/files/batch`` — are *lexical*, *forward-slash*, and
*project-relative*. Backslashes and ``.``/``..`` segments are normalized at
the boundary (``filigree.db_files._normalize_scan_path``) before reaching
this module, and disk presence is NOT required: Clarion looks up entries by
its ``source_file_path`` column, which is a catalog key, not a filesystem
probe. A path that resolves cleanly inside the project root but has no
file on disk still has an entry in Clarion's catalog and resolves
successfully.

Auth (CONTRACT-2)
-----------------
``ClarionRegistry.auth_token`` is read at construction from the env var
named by ``ClarionConfig.token_env`` (default ``CLARION_LOOM_TOKEN``).
When set, every outbound request carries ``Authorization: Bearer <token>``.
When unset, no Authorization header is sent — Clarion accepts unauthenticated
calls on loopback bind and rejects them on non-loopback per the 1.0
cross-product contract.

Briefing-blocked (CONTRACT-3)
-----------------------------
Clarion 1.0 returns HTTP 403 with body ``{"code": "BRIEFING_BLOCKED", ...}``
for files it intentionally withholds (secret-bearing, owner-locked).
``ClarionRegistry`` maps that response to :class:`RegistryBriefingBlockedError`,
which extends :class:`RegistryResolutionError` (NOT :class:`RegistryUnavailableError`)
so the ``_ClarionLocalFallbackRegistry`` wrapper does not engage — silently
re-attaching the file under a local file_id would defeat the briefing block.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import time
from collections.abc import Callable
from dataclasses import KW_ONLY, dataclass
from dataclasses import field as dataclass_field
from typing import Any, Literal, Protocol, TypeAlias, TypedDict
from urllib.parse import urlencode, urlparse

import httpx

from filigree.types.core import ContentHash, EntityId, FileId, RegistryBackend, make_content_hash, make_entity_id, make_file_id

logger = logging.getLogger(__name__)

# Name of the env var that carries the Clarion Bearer token by default.
# Not a token value itself; the actual token lives in the operator's
# environment under this name. Suppressing the hardcoded-secret lint
# because this string is an env-var name, not a credential.
DEFAULT_CLARION_TOKEN_ENV = "CLARION_LOOM_TOKEN"  # noqa: S105

DEFAULT_TEST_REGISTRY_BACKENDS: tuple[RegistryBackend, ...] = ("local", "clarion")
REGISTRY_BACKEND_FEATURES: tuple[RegistryBackend, ...] = ("local", "clarion")
CLARION_RESOLVE_FILE_MAX_ATTEMPTS = 3
CLARION_RESOLVE_FILE_RETRY_BACKOFF_SECONDS = 0.05

# Clarion's `_capabilities` response declares an `api_version: u8`. Filigree
# rejects startup under `clarion` mode if Clarion advertises a version this
# build was not written against — a mismatch means the wire contract changed
# in a way no in-process fallback can mask. Bumped when ADR-014 makes a
# breaking change to the resolver protocol (see ADR-014 §4 and the
# Briefing-block masking section).
EXPECTED_CLARION_API_VERSION = 1


class LocalResolvedFile(TypedDict):
    """File identity resolved by the local (Filigree-native) registry.

    The local backend mints a ``FileId`` and cannot compute a drift hash, so
    ``content_hash`` is the empty-string sentinel (pinned to ``Literal[""]`` so
    a local record carrying a hash will not type-check).
    """

    file_id: FileId
    content_hash: Literal[""]
    canonical_path: str
    language: str
    registry_backend: Literal["local"]


class ClarionResolvedFile(TypedDict):
    """File identity resolved by the Clarion (federated) registry.

    Clarion returns an opaque ``EntityId`` and a non-empty drift hash; the hash
    is branded ``ContentHash`` (minted via ``make_content_hash``, which rejects
    blank tokens), so a clarion record cannot carry the empty sentinel.
    """

    file_id: EntityId
    content_hash: ContentHash
    canonical_path: str
    language: str
    registry_backend: Literal["clarion"]


# Discriminated on ``registry_backend``. The former flat shape let mismatched
# backend/identity combinations type-check (a ``local`` file with a drift hash,
# a ``clarion`` file with the empty sentinel). The union pins ``file_id`` and
# ``content_hash`` to the backend so those illegal combinations are
# unconstructible at the mint sites. All five keys are shared across both
# members, so consumers reading common fields (db_files.py) narrow without
# branching.
ResolvedFile: TypeAlias = LocalResolvedFile | ClarionResolvedFile


class BatchQuery(TypedDict):
    """Single item in a batched file-resolution request."""

    path: str
    language: str


class BatchResolutionError(TypedDict):
    """One failed item in a batch response (other than not_found / briefing_blocked)."""

    requested_path: str
    code: str
    message: str


class BatchResolution(TypedDict):
    """Structured outcome of ``resolve_files_batch``.

    The four channels mirror Clarion 1.0's ``POST /api/v1/files/batch`` body:
    ``resolved`` is keyed by the requested path (Filigree's lookup key);
    ``not_found`` and ``briefing_blocked`` are bare path lists; ``errors``
    captures per-item failures Clarion couldn't slot into the other
    channels. Callers decide per-item policy (raise vs. continue) without
    try/except gymnastics over a flat list of futures.

    ``messages`` is an optional per-path sidecar carrying the original
    registry exception's ``str()`` for items in ``not_found`` /
    ``briefing_blocked``. Wire-protocol batch responses do not populate it
    (those channels are bare path lists on the wire); the loop-fallback
    adapter populates it from the single-item exception messages so call
    sites can preserve the original context when promoting batch channels
    back into per-item exceptions.
    """

    resolved: dict[str, ResolvedFile]
    not_found: list[str]
    briefing_blocked: list[str]
    errors: list[BatchResolutionError]
    messages: dict[str, str]


class SeiResolution(TypedDict):
    """Structured outcome of a batched locator→SEI resolve (ADR-038 §7).

    The three channels map Clarion's ``POST /api/v1/identity/resolve:batch``
    body onto the producer-backfill decision the playbook prescribes:

    - ``resolved`` — keyed by the submitted locator, value is the alive SEI
      (extracted from Clarion's ``{sei, current_locator, content_hash, alive}``
      record). The backfill rewrites the stored id to this SEI.
    - ``orphaned`` — locators Clarion reports as ``alive:false`` (its
      ``not_found`` channel). The locator no longer resolves; the backfill keeps
      it verbatim and flags it ORPHAN for human review (never silently dropped).
    - ``already_migrated`` — locators Clarion *rejected* through its ``invalid``
      channel (the REQ-F-02 reserved-prefix rejection). The name is a slight
      misnomer: an id that is *already* an SEI never reaches this channel, because
      the backfill skips SEI-prefixed values client-side (the ``SEI_PREFIX`` filter,
      counted as ``associations_already_sei``) *before* any network call — that
      client-side skip, not this channel, is what makes a partial backfill
      resumable. In practice this channel therefore carries malformed locators
      Clarion refused; its sole consumer (``sei_backfill._orphan_reason``)
      classifies membership here as a ``reason="invalid"`` orphan.

    Filigree never parses the SEI beyond the sanctioned ``clarion:eid:`` prefix
    check; the string is stored opaquely.
    """

    resolved: dict[str, str]
    orphaned: list[str]
    already_migrated: list[str]


# Clarion 1.0 caps batch requests at 256 queries (returns 400 with
# code=BATCH_TOO_LARGE on overflow). Filigree chunks at this size before
# sending so it never trips the cap; the constant is exposed so callers
# can size their inputs deliberately.
CLARION_BATCH_MAX_QUERIES = 256


def resolve_files_batch_via_loop(
    registry: object,
    queries: list[BatchQuery],
    *,
    actor: str = "",
) -> BatchResolution:
    """Default ``resolve_files_batch`` implementation that loops ``resolve_file``.

    Used by call sites to gracefully support registry fakes that only
    implement ``resolve_file`` (test fakes predating CONTRACT-1). Production
    backends (LocalRegistry, ClarionRegistry, _ClarionLocalFallbackRegistry)
    expose their own ``resolve_files_batch`` and never reach this fallback.

    Maps per-item exceptions to the structured channels so the call site
    sees the same shape from both code paths.
    """
    resolved: dict[str, ResolvedFile] = {}
    not_found: list[str] = []
    briefing_blocked: list[str] = []
    errors: list[BatchResolutionError] = []
    messages: dict[str, str] = {}
    for query in queries:
        path = query["path"]
        if path in resolved or path in not_found or path in briefing_blocked:
            continue
        try:
            resolved[path] = registry.resolve_file(path, language=query.get("language", ""), actor=actor)  # type: ignore[attr-defined]
        except RegistryBriefingBlockedError as exc:
            briefing_blocked.append(path)
            messages[path] = str(exc)
        except RegistryFileNotFoundError as exc:
            not_found.append(path)
            messages[path] = str(exc)
        except RegistryResolutionError as exc:
            errors.append(BatchResolutionError(requested_path=path, code="RESOLUTION_ERROR", message=str(exc)))
            messages[path] = str(exc)
    return BatchResolution(
        resolved=resolved,
        not_found=not_found,
        briefing_blocked=briefing_blocked,
        errors=errors,
        messages=messages,
    )


class RegistryProtocol(Protocol):
    """Protocol consumed by file auto-create paths."""

    def resolve_file(
        self,
        path: str,
        *,
        language: str = "",
        actor: str = "",
    ) -> ResolvedFile: ...

    def resolve_files_batch(
        self,
        queries: list[BatchQuery],
        *,
        actor: str = "",
    ) -> BatchResolution: ...

    def is_displaced(self) -> bool: ...


class RegistryUnavailableError(RuntimeError):
    """Raised when the configured registry backend cannot resolve a file."""

    def __init__(self, message: str, *, url: str = "", path: str = "", cause_kind: str = "unknown") -> None:
        super().__init__(message)
        self.url = url
        self.path = path
        self.cause_kind = cause_kind


class RegistryResolutionError(ValueError):
    """Raised when a reachable registry rejects a file resolution request."""

    def __init__(self, message: str, *, status_code: int, url: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.url = url


class RegistryFileNotFoundError(RegistryResolutionError):
    """Raised when a reachable registry does not know the requested file."""


class RegistryBriefingBlockedError(RegistryResolutionError):
    """Raised when a reachable registry refuses to expose a briefing-blocked file.

    Distinct from :class:`RegistryFileNotFoundError` because the file *does*
    exist on Clarion's side; it is intentionally withheld (secret-bearing,
    owner-locked, briefing policy). Critically distinct from
    :class:`RegistryUnavailableError` so the ``_ClarionLocalFallbackRegistry``
    wrapper does NOT swallow it — silently falling back to a local file_id
    would re-attach the secret-bearing file under Filigree-native identity,
    defeating Clarion's briefing block.

    Cross-product contract: Clarion 1.0 returns HTTP 403 with body
    ``{"code": "BRIEFING_BLOCKED", ...}`` for these paths.
    """


class RegistryVersionMismatchError(RuntimeError):
    """Raised when Clarion advertises an api_version this Filigree was not written against.

    Distinct from ``RegistryUnavailableError`` because no fallback can fix it:
    the resolver wire contract has changed. Operators must upgrade Filigree
    (or downgrade Clarion) to a compatible pair.
    """

    def __init__(self, message: str, *, url: str, expected: int, advertised: object) -> None:
        super().__init__(message)
        self.url = url
        self.expected = expected
        self.advertised = advertised


class ClarionCapabilities(TypedDict):
    """Clarion ``GET /api/v1/_capabilities`` response shape.

    Field names mirror Clarion's wire surface verbatim. ``registry_backend``
    is Clarion's boolean "I am willing to serve registry-backend traffic" flag
    and is NOT the same field as Filigree's
    ``config_flags.registry_backend: 'local'|'clarion'`` (project-mode string).
    The collision is in name only, not in meaning; see ADR-014's
    "Briefing-block masking" section and the cross-project C-6 review item.
    """

    registry_backend: bool
    file_registry: bool
    api_version: int
    instance_id: str
    # Stable Entity Identity (Clarion Wave 1 / ADR-038). ``sei_supported``
    # mirrors Clarion's nested ``sei.supported`` flag; ``sei_version`` mirrors
    # ``sei.version``. Both default to the pre-SEI shape (False / 0) when Clarion
    # omits the ``sei`` object, so a consumer probing an older Clarion degrades
    # gracefully (keeps working on locators) rather than crashing. The
    # locator→SEI backfill (``filigree sei-backfill``) gates entirely on
    # ``sei_supported``.
    sei_supported: bool
    sei_version: int


def clarion_capabilities_url(base_url: str) -> str:
    """Build the Clarion capability-probe URL."""
    return f"{base_url.rstrip('/')}/api/v1/_capabilities"


def clarion_identity_resolve_batch_url(base_url: str) -> str:
    """Build the Clarion batched locator→SEI resolve URL (ADR-038, REQ-F-02)."""
    return f"{base_url.rstrip('/')}/api/v1/identity/resolve:batch"


def _is_loopback_origin(url: str) -> bool:
    host = urlparse(url).hostname
    if host is None:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _validate_clarion_token_origin(url: str, *, auth_token: str | None) -> None:
    if auth_token and not _is_loopback_origin(url):
        msg = (
            "clarion.auth_token may only be sent to loopback Clarion origins by default; "
            f"refusing token-bearing request to {urlparse(url).netloc!r}"
        )
        raise ValueError(msg)


def _clarion_headers(*, auth_token: str | None, has_body: bool = False) -> dict[str, str]:
    headers: dict[str, str] = {}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"
    if has_body:
        headers["Content-Type"] = "application/json"
    return headers


def clarion_files_batch_url(base_url: str) -> str:
    """Build the Clarion batch-resolve URL."""
    return f"{base_url.rstrip('/')}/api/v1/files/batch"


def probe_clarion_capabilities(base_url: str, *, timeout_seconds: float, auth_token: str | None = None) -> ClarionCapabilities:
    """Issue ``GET /api/v1/_capabilities`` against Clarion and validate the shape.

    On HTTP-level failure (network, timeout, non-200) raises
    ``RegistryUnavailableError`` so callers can treat probe-time and
    resolve-time outages with the same fallback policy.
    On schema-level failure (missing field, wrong type) raises
    ``RegistryUnavailableError`` with ``cause_kind='invalid_response'``.
    Version-mismatch checks are layered on by ``validate_clarion_capabilities``.
    """
    url = clarion_capabilities_url(base_url)
    _validate_clarion_token_origin(url, auth_token=auth_token)
    try:
        with httpx.Client(trust_env=False, follow_redirects=True) as client:
            response = client.get(url, headers=_clarion_headers(auth_token=auth_token), timeout=timeout_seconds)
        raw = response.text
        if response.status_code >= 400:
            reason = response.reason_phrase
            if response.status_code == 401:
                msg = f"Clarion capability probe rejected at {url}: HTTP 401 {reason} (check token_env)"
                raise RegistryUnavailableError(msg, url=url, path="", cause_kind="auth")
            msg = f"Clarion capability probe failed at {url}: HTTP {response.status_code} {reason}"
            raise RegistryUnavailableError(msg, url=url, path="", cause_kind="http_error")
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        msg = f"Clarion capability probe unreachable at {url}: {exc}"
        raise RegistryUnavailableError(msg, url=url, path="", cause_kind="network") from exc

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        msg = f"Clarion capability probe returned invalid JSON from {url}: {exc}"
        raise RegistryUnavailableError(msg, url=url, path="", cause_kind="invalid_response") from exc
    if not isinstance(payload, dict):
        msg = f"Clarion capability probe returned non-object response from {url}: {type(payload).__name__}"
        raise RegistryUnavailableError(msg, url=url, path="", cause_kind="invalid_response")

    bool_fields = ("registry_backend", "file_registry")
    for field in bool_fields:
        if not isinstance(payload.get(field), bool):
            msg = f"Clarion capability probe from {url} missing boolean field {field!r}"
            raise RegistryUnavailableError(msg, url=url, path="", cause_kind="invalid_response")
    if not isinstance(payload.get("api_version"), int) or isinstance(payload["api_version"], bool):
        msg = f"Clarion capability probe from {url} missing integer field 'api_version'"
        raise RegistryUnavailableError(msg, url=url, path="", cause_kind="invalid_response")
    if not isinstance(payload.get("instance_id"), str) or not payload["instance_id"]:
        msg = f"Clarion capability probe from {url} missing non-empty string 'instance_id'"
        raise RegistryUnavailableError(msg, url=url, path="", cause_kind="invalid_response")

    sei_supported, sei_version = _parse_sei_capability(payload, url=url)

    return ClarionCapabilities(
        registry_backend=payload["registry_backend"],
        file_registry=payload["file_registry"],
        api_version=payload["api_version"],
        instance_id=payload["instance_id"],
        sei_supported=sei_supported,
        sei_version=sei_version,
    )


def _parse_sei_capability(payload: dict[str, Any], *, url: str) -> tuple[bool, int]:
    """Read Clarion's nested ``sei`` capability, tolerating a pre-SEI Clarion.

    A pre-SEI Clarion omits the ``sei`` object entirely; that is not an error —
    it means "SEI unsupported, degrade to locators" (returns ``(False, 0)``).
    When the object IS present it must be well-formed (``supported: bool`` plus
    an integer ``version``); a malformed advertisement is a wire-contract break
    and raises ``invalid_response`` like every other shape check above.
    """
    sei = payload.get("sei")
    if sei is None:
        return (False, 0)
    if not isinstance(sei, dict):
        msg = f"Clarion capability probe from {url}: 'sei' must be an object, got {type(sei).__name__}"
        raise RegistryUnavailableError(msg, url=url, path="", cause_kind="invalid_response")
    supported = sei.get("supported", False)
    if not isinstance(supported, bool):
        msg = f"Clarion capability probe from {url}: 'sei.supported' must be a boolean"
        raise RegistryUnavailableError(msg, url=url, path="", cause_kind="invalid_response")
    version = sei.get("version", 0)
    if not isinstance(version, int) or isinstance(version, bool):
        msg = f"Clarion capability probe from {url}: 'sei.version' must be an integer"
        raise RegistryUnavailableError(msg, url=url, path="", cause_kind="invalid_response")
    return (supported, version)


def validate_clarion_capabilities(capabilities: ClarionCapabilities, *, base_url: str) -> None:
    """Reject Clarion advertisements that contradict ADR-014's contract.

    Raises ``RegistryVersionMismatchError`` on api_version mismatch (no fallback
    can fix a wire-protocol break). Raises ``RegistryUnavailableError`` when
    Clarion reports it is unwilling to serve registry-backend traffic — this is
    a transient configuration issue, so fallback semantics apply.
    """
    url = clarion_capabilities_url(base_url)
    advertised = capabilities["api_version"]
    if advertised != EXPECTED_CLARION_API_VERSION:
        msg = (
            f"Clarion capability probe at {url} advertised api_version={advertised!r}; "
            f"this Filigree was built for api_version={EXPECTED_CLARION_API_VERSION}. "
            "Upgrade Filigree or downgrade Clarion to a matching pair."
        )
        raise RegistryVersionMismatchError(
            msg,
            url=url,
            expected=EXPECTED_CLARION_API_VERSION,
            advertised=advertised,
        )
    if not capabilities["registry_backend"] or not capabilities["file_registry"]:
        msg = (
            f"Clarion at {url} declined registry-backend role: "
            f"registry_backend={capabilities['registry_backend']}, "
            f"file_registry={capabilities['file_registry']}. "
            "Reconfigure Clarion or switch this project to registry_backend='local'."
        )
        raise RegistryUnavailableError(msg, url=url, path="", cause_kind="role_declined")


def _is_briefing_blocked_payload(raw: str | bytes | bytearray) -> bool:
    try:
        text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
        payload = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and payload.get("code") == "BRIEFING_BLOCKED"


def clarion_file_read_url(base_url: str, path: str, *, language: str = "") -> str:
    """Build the Clarion read-API URL for an operator-facing hint."""
    query = urlencode({"path": path, "language": language})
    return f"{base_url.rstrip('/')}/api/v1/files?{query}"


def normalize_clarion_base_url(base_url: str) -> str:
    """Validate and canonicalize a Clarion registry base URL."""
    if not isinstance(base_url, str) or not base_url.strip():
        msg = f"clarion.base_url must be a non-empty http(s) URL with a host, got {base_url!r}"
        raise ValueError(msg)
    normalized = base_url.strip().rstrip("/")
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.hostname is None:
        msg = f"clarion.base_url must be a non-empty http(s) URL with a host, got {base_url!r}"
        raise ValueError(msg)
    return normalized


class LocalRegistry:
    """Filigree-native registry backend."""

    def __init__(self, file_id_factory: Callable[[], str]) -> None:
        self._file_id_factory = file_id_factory

    def resolve_file(
        self,
        path: str,
        *,
        language: str = "",
        actor: str = "",
    ) -> ResolvedFile:
        return LocalResolvedFile(
            file_id=make_file_id(self._file_id_factory()),
            content_hash="",
            canonical_path=path,
            language=language,
            registry_backend="local",
        )

    def resolve_files_batch(
        self,
        queries: list[BatchQuery],
        *,
        actor: str = "",
    ) -> BatchResolution:
        """LocalRegistry never fails per-item — every query mints a fresh local id."""
        resolved: dict[str, ResolvedFile] = {}
        for query in queries:
            path = query["path"]
            if path in resolved:
                continue
            resolved[path] = self.resolve_file(path, language=query.get("language", ""), actor=actor)
        return BatchResolution(resolved=resolved, not_found=[], briefing_blocked=[], errors=[], messages={})

    def is_displaced(self) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class ClarionRegistry:
    """HTTP-backed registry that resolves file identity through Clarion.

    ``auth_token`` is read once at construction (typically from the env var
    named by ``ClarionConfig.token_env``) and threaded into every outbound
    request as ``Authorization: Bearer <token>``. ``None`` or empty string
    means "send no auth header" (loopback-only Clarion deployments accept
    unauthenticated traffic).
    """

    base_url: str
    _: KW_ONLY
    timeout_seconds: float = 5
    auth_token: str | None = None
    _http_client: httpx.Client = dataclass_field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_url", normalize_clarion_base_url(self.base_url))
        if isinstance(self.timeout_seconds, bool) or not isinstance(self.timeout_seconds, int | float) or self.timeout_seconds <= 0:
            msg = f"clarion.timeout_seconds must be a positive number, got {self.timeout_seconds!r}"
            raise ValueError(msg)
        object.__setattr__(self, "timeout_seconds", float(self.timeout_seconds))
        if self.auth_token is not None and not isinstance(self.auth_token, str):
            msg = f"clarion.auth_token must be a string or None, got {type(self.auth_token).__name__}"
            raise ValueError(msg)
        object.__setattr__(self, "_http_client", httpx.Client(trust_env=False, follow_redirects=True))

    def resolve_file(
        self,
        path: str,
        *,
        language: str = "",
        actor: str = "",
    ) -> ResolvedFile:
        url = clarion_file_read_url(self.base_url, path, language=language)
        deadline = time.monotonic() + self.timeout_seconds
        attempt = 1
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                msg = f"Clarion registry unavailable at {url}: retry budget exhausted"
                raise RegistryUnavailableError(msg, url=url, path=path, cause_kind="timeout")
            try:
                response = self._http_client.get(
                    url,
                    headers=_clarion_headers(auth_token=self.auth_token),
                    timeout=remaining,
                )
                raw = response.text
                if response.status_code >= 400:
                    if response.status_code >= 500 and self._should_retry_read(attempt, deadline):
                        self._log_retry(url=url, attempt=attempt, cause_kind="http_error")
                        self._sleep_before_retry(deadline)
                        attempt += 1
                        continue
                    self._raise_file_http_error(response, url=url, path=path)
                break
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if self._should_retry_read(attempt, deadline):
                    self._log_retry(url=url, attempt=attempt, cause_kind="network")
                    self._sleep_before_retry(deadline)
                    attempt += 1
                    continue
                msg = f"Clarion registry unavailable at {url}: {exc}"
                raise RegistryUnavailableError(msg, url=url, path=path, cause_kind="network") from exc

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            msg = f"Clarion registry returned invalid JSON from {url}: {exc}"
            raise RegistryUnavailableError(msg, url=url, path=path, cause_kind="invalid_response") from exc
        if not isinstance(payload, dict):
            msg = f"Clarion registry returned non-object response from {url}: {type(payload).__name__}"
            raise RegistryUnavailableError(msg, url=url, path=path, cause_kind="invalid_response")

        required = ("entity_id", "content_hash", "canonical_path", "language")
        missing = [field for field in required if not isinstance(payload.get(field), str)]
        if missing:
            msg = f"Clarion registry response from {url} missing string field(s): {', '.join(missing)}"
            raise RegistryUnavailableError(msg, url=url, path=path, cause_kind="invalid_response")
        try:
            content_hash = make_content_hash(payload["content_hash"])
        except ValueError as exc:
            msg = f"Clarion registry response from {url} has invalid content_hash: {exc}"
            raise RegistryUnavailableError(msg, url=url, path=path, cause_kind="invalid_response") from exc
        try:
            file_id = make_entity_id(payload["entity_id"])
        except ValueError as exc:
            msg = f"Clarion registry response from {url} has invalid entity_id: {exc}"
            raise RegistryUnavailableError(msg, url=url, path=path, cause_kind="invalid_response") from exc

        return ClarionResolvedFile(
            file_id=file_id,
            content_hash=content_hash,
            canonical_path=payload["canonical_path"],
            language=payload["language"],
            registry_backend="clarion",
        )

    def _raise_file_http_error(self, response: httpx.Response, *, url: str, path: str) -> None:
        reason = response.reason_phrase
        if response.status_code == 401:
            msg = f"Clarion registry rejected auth at {url}: HTTP 401 {reason} (check token_env)"
            raise RegistryUnavailableError(msg, url=url, path=path, cause_kind="auth")
        if response.status_code == 403 and _is_briefing_blocked_payload(response.text):
            msg = f"Clarion registry refuses briefing-blocked file at {url}: HTTP 403 {reason}"
            raise RegistryBriefingBlockedError(msg, status_code=response.status_code, url=url)
        if response.status_code == 404:
            msg = f"Clarion registry could not resolve file at {url}: HTTP 404 {reason}"
            raise RegistryFileNotFoundError(msg, status_code=response.status_code, url=url)
        if 400 <= response.status_code < 500:
            msg = f"Clarion registry rejected file resolution at {url}: HTTP {response.status_code} {reason}"
            raise RegistryResolutionError(msg, status_code=response.status_code, url=url)
        msg = f"Clarion registry unavailable at {url}: HTTP {response.status_code} {reason}"
        raise RegistryUnavailableError(msg, url=url, path=path, cause_kind="http_error")

    def _should_retry_read(self, attempt: int, deadline: float) -> bool:
        return attempt < CLARION_RESOLVE_FILE_MAX_ATTEMPTS and deadline - time.monotonic() > CLARION_RESOLVE_FILE_RETRY_BACKOFF_SECONDS

    def _sleep_before_retry(self, deadline: float) -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(CLARION_RESOLVE_FILE_RETRY_BACKOFF_SECONDS, remaining))

    def _log_retry(self, *, url: str, attempt: int, cause_kind: str) -> None:
        logger.warning(
            "Retrying Clarion registry request after transient failure",
            extra={
                "url": url,
                "attempt": attempt,
                "next_attempt": attempt + 1,
                "max_attempts": CLARION_RESOLVE_FILE_MAX_ATTEMPTS,
                "cause_kind": cause_kind,
            },
        )

    def resolve_files_batch(
        self,
        queries: list[BatchQuery],
        *,
        actor: str = "",
    ) -> BatchResolution:
        """CONTRACT-1 batch resolution: POST /api/v1/files/batch.

        Chunks ``queries`` into runs of ``CLARION_BATCH_MAX_QUERIES`` (256)
        and merges the per-chunk results into a single ``BatchResolution``.
        Whole-batch failures (network, timeout, HTTP 5xx, malformed body,
        HTTP 401 auth) raise ``RegistryUnavailableError`` — fallback policy
        applies. Per-item failures (not_found, briefing_blocked, structured
        errors) populate the corresponding channel and the call still
        returns; callers decide whether to raise per item.
        """
        aggregate = BatchResolution(resolved={}, not_found=[], briefing_blocked=[], errors=[], messages={})
        if not queries:
            return aggregate
        for start in range(0, len(queries), CLARION_BATCH_MAX_QUERIES):
            chunk = queries[start : start + CLARION_BATCH_MAX_QUERIES]
            chunk_result = self._resolve_files_batch_chunk(chunk)
            aggregate["resolved"].update(chunk_result["resolved"])
            aggregate["not_found"].extend(chunk_result["not_found"])
            aggregate["briefing_blocked"].extend(chunk_result["briefing_blocked"])
            aggregate["errors"].extend(chunk_result["errors"])
            aggregate["messages"].update(chunk_result.get("messages", {}))
        return aggregate

    def _resolve_files_batch_chunk(self, chunk: list[BatchQuery]) -> BatchResolution:
        url = clarion_files_batch_url(self.base_url)
        body = {"queries": [{"path": q["path"], "language": q.get("language", "")} for q in chunk]}
        # Batch resolve is an idempotent read, so it retries transient 5xx and
        # network failures on the same deadline/backoff budget as the
        # single-file ``resolve_file`` path (CONTRACT-1 retry parity). Auth and
        # 4xx outcomes are deterministic and raise immediately without retry.
        deadline = time.monotonic() + self.timeout_seconds
        attempt = 1
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                msg = f"Clarion batch resolve unreachable at {url}: retry budget exhausted"
                raise RegistryUnavailableError(msg, url=url, path="", cause_kind="timeout")
            try:
                response = self._http_client.post(
                    url,
                    json=body,
                    headers=_clarion_headers(auth_token=self.auth_token, has_body=True),
                    timeout=remaining,
                )
                raw = response.text
                if response.status_code >= 400:
                    if response.status_code >= 500 and self._should_retry_read(attempt, deadline):
                        self._log_retry(url=url, attempt=attempt, cause_kind="http_error")
                        self._sleep_before_retry(deadline)
                        attempt += 1
                        continue
                    reason = response.reason_phrase
                    if response.status_code == 401:
                        msg = f"Clarion batch resolve rejected auth at {url}: HTTP 401 {reason} (check token_env)"
                        raise RegistryUnavailableError(msg, url=url, path="", cause_kind="auth")
                    if response.status_code == 403 and _is_briefing_blocked_payload(response.text):
                        msg = f"Clarion batch resolve refuses briefing-blocked file(s) at {url}: HTTP 403 {reason}"
                        raise RegistryBriefingBlockedError(msg, status_code=response.status_code, url=url)
                    if 400 <= response.status_code < 500:
                        msg = f"Clarion batch resolve rejected request at {url}: HTTP {response.status_code} {reason}"
                        raise RegistryResolutionError(msg, status_code=response.status_code, url=url)
                    msg = f"Clarion batch resolve failed at {url}: HTTP {response.status_code} {reason}"
                    raise RegistryUnavailableError(msg, url=url, path="", cause_kind="http_error")
                break
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if self._should_retry_read(attempt, deadline):
                    self._log_retry(url=url, attempt=attempt, cause_kind="network")
                    self._sleep_before_retry(deadline)
                    attempt += 1
                    continue
                msg = f"Clarion batch resolve unreachable at {url}: {exc}"
                raise RegistryUnavailableError(msg, url=url, path="", cause_kind="network") from exc

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            msg = f"Clarion batch resolve returned invalid JSON from {url}: {exc}"
            raise RegistryUnavailableError(msg, url=url, path="", cause_kind="invalid_response") from exc
        if not isinstance(payload, dict):
            msg = f"Clarion batch resolve returned non-object response from {url}: {type(payload).__name__}"
            raise RegistryUnavailableError(msg, url=url, path="", cause_kind="invalid_response")

        requested_paths = [q["path"] for q in chunk]
        return self._parse_batch_response(payload, url=url, requested_paths=requested_paths)

    def _parse_batch_response(self, payload: dict[str, Any], *, url: str, requested_paths: list[str]) -> BatchResolution:
        def require_list(field: str) -> list[Any]:
            if field not in payload:
                return []
            value = payload[field]
            if not isinstance(value, list):
                msg = f"Clarion batch resolve at {url}: '{field}' must be a list"
                raise RegistryUnavailableError(msg, url=url, path="", cause_kind="invalid_response")
            return value

        def record_outcome(path: str, channel: str) -> None:
            existing = outcomes.get(path)
            if existing is not None:
                msg = f"Clarion batch resolve at {url}: requested path {path!r} appears in multiple result channels: {existing}, {channel}"
                raise RegistryUnavailableError(msg, url=url, path="", cause_kind="invalid_response")
            outcomes[path] = channel

        requested_path_set = set(requested_paths)
        outcomes: dict[str, str] = {}
        resolved: dict[str, ResolvedFile] = {}
        for item in require_list("resolved"):
            if not isinstance(item, dict):
                msg = f"Clarion batch resolve at {url}: 'resolved' item must be an object"
                raise RegistryUnavailableError(msg, url=url, path="", cause_kind="invalid_response")
            required = ("requested_path", "entity_id", "content_hash", "canonical_path", "language")
            missing = [f for f in required if not isinstance(item.get(f), str)]
            if missing:
                msg = f"Clarion batch resolve at {url}: resolved entry missing string field(s): {', '.join(missing)}"
                raise RegistryUnavailableError(msg, url=url, path="", cause_kind="invalid_response")
            try:
                content_hash = make_content_hash(item["content_hash"])
            except ValueError as exc:
                msg = f"Clarion batch resolve at {url} has invalid content_hash for {item['requested_path']!r}: {exc}"
                raise RegistryUnavailableError(msg, url=url, path="", cause_kind="invalid_response") from exc
            try:
                file_id = make_entity_id(item["entity_id"])
            except ValueError as exc:
                msg = f"Clarion batch resolve at {url} has invalid entity_id for {item['requested_path']!r}: {exc}"
                raise RegistryUnavailableError(msg, url=url, path="", cause_kind="invalid_response") from exc
            record_outcome(item["requested_path"], "resolved")
            resolved[item["requested_path"]] = ClarionResolvedFile(
                file_id=file_id,
                content_hash=content_hash,
                canonical_path=item["canonical_path"],
                language=item["language"],
                registry_backend="clarion",
            )

        not_found: list[str] = []
        for item in require_list("not_found"):
            if not isinstance(item, str):
                msg = f"Clarion batch resolve at {url}: 'not_found' item must be a string"
                raise RegistryUnavailableError(msg, url=url, path="", cause_kind="invalid_response")
            record_outcome(item, "not_found")
            not_found.append(item)
        briefing_blocked: list[str] = []
        for item in require_list("briefing_blocked"):
            if not isinstance(item, str):
                msg = f"Clarion batch resolve at {url}: 'briefing_blocked' item must be a string"
                raise RegistryUnavailableError(msg, url=url, path="", cause_kind="invalid_response")
            record_outcome(item, "briefing_blocked")
            briefing_blocked.append(item)
        errors: list[BatchResolutionError] = []
        for item in require_list("errors"):
            if not isinstance(item, dict):
                msg = f"Clarion batch resolve at {url}: 'errors' item must be an object"
                raise RegistryUnavailableError(msg, url=url, path="", cause_kind="invalid_response")
            requested_path = item.get("requested_path")
            code = item.get("code")
            message = item.get("message")
            if not isinstance(requested_path, str) or not isinstance(code, str) or not isinstance(message, str):
                msg = f"Clarion batch resolve at {url}: errors entry missing string field(s): requested_path, code, message"
                raise RegistryUnavailableError(msg, url=url, path="", cause_kind="invalid_response")
            record_outcome(requested_path, "errors")
            errors.append(BatchResolutionError(requested_path=requested_path, code=code, message=message))

        unexpected = sorted(set(outcomes) - requested_path_set)
        if unexpected:
            msg = f"Clarion batch resolve at {url}: response included unexpected path(s): {', '.join(repr(path) for path in unexpected)}"
            raise RegistryUnavailableError(msg, url=url, path="", cause_kind="invalid_response")
        missing = sorted(requested_path_set - set(outcomes))
        if missing:
            msg = f"Clarion batch resolve at {url}: missing outcome for requested path(s): {', '.join(repr(path) for path in missing)}"
            raise RegistryUnavailableError(msg, url=url, path="", cause_kind="invalid_response")

        return BatchResolution(
            resolved=resolved,
            not_found=not_found,
            briefing_blocked=briefing_blocked,
            errors=errors,
            messages={},
        )

    def resolve_locators_batch(self, locators: list[str]) -> SeiResolution:
        """Resolve a batch of locators to SEIs via ``POST /api/v1/identity/resolve:batch``.

        Chunks ``locators`` into runs of ``CLARION_BATCH_MAX_QUERIES`` (256 — the
        per-batch cap Clarion pins on the identity surface, same as files) and
        merges the per-chunk channels. Whole-batch failures (network, timeout,
        HTTP 5xx, malformed body, 401 auth) raise ``RegistryUnavailableError``;
        per-locator outcomes (resolved / orphaned / already-migrated) populate
        the returned :class:`SeiResolution`. This is the resolve client the
        operator-invoked ``filigree sei-backfill`` command drives — it lives on
        the network-allowed registry layer, never in the DB association layer.
        """
        aggregate = SeiResolution(resolved={}, orphaned=[], already_migrated=[])
        if not locators:
            return aggregate
        for start in range(0, len(locators), CLARION_BATCH_MAX_QUERIES):
            chunk = locators[start : start + CLARION_BATCH_MAX_QUERIES]
            chunk_result = self._resolve_locators_batch_chunk(chunk)
            aggregate["resolved"].update(chunk_result["resolved"])
            aggregate["orphaned"].extend(chunk_result["orphaned"])
            aggregate["already_migrated"].extend(chunk_result["already_migrated"])
        return aggregate

    def _resolve_locators_batch_chunk(self, chunk: list[str]) -> SeiResolution:
        url = clarion_identity_resolve_batch_url(self.base_url)
        body = {"locators": chunk}
        # Identity resolve is an idempotent read, so it retries transient 5xx /
        # network failures on the same deadline/backoff budget as the file
        # resolve paths. Auth and other 4xx outcomes are deterministic and raise
        # immediately without retry.
        deadline = time.monotonic() + self.timeout_seconds
        attempt = 1
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                msg = f"Clarion identity resolve unreachable at {url}: retry budget exhausted"
                raise RegistryUnavailableError(msg, url=url, path="", cause_kind="timeout")
            try:
                response = self._http_client.post(
                    url,
                    json=body,
                    headers=_clarion_headers(auth_token=self.auth_token, has_body=True),
                    timeout=remaining,
                )
                raw = response.text
                if response.status_code >= 400:
                    if response.status_code >= 500 and self._should_retry_read(attempt, deadline):
                        self._log_retry(url=url, attempt=attempt, cause_kind="http_error")
                        self._sleep_before_retry(deadline)
                        attempt += 1
                        continue
                    reason = response.reason_phrase
                    if response.status_code == 401:
                        msg = f"Clarion identity resolve rejected auth at {url}: HTTP 401 {reason} (check token_env)"
                        raise RegistryUnavailableError(msg, url=url, path="", cause_kind="auth")
                    if 400 <= response.status_code < 500:
                        msg = f"Clarion identity resolve rejected request at {url}: HTTP {response.status_code} {reason}"
                        raise RegistryResolutionError(msg, status_code=response.status_code, url=url)
                    msg = f"Clarion identity resolve failed at {url}: HTTP {response.status_code} {reason}"
                    raise RegistryUnavailableError(msg, url=url, path="", cause_kind="http_error")
                break
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if self._should_retry_read(attempt, deadline):
                    self._log_retry(url=url, attempt=attempt, cause_kind="network")
                    self._sleep_before_retry(deadline)
                    attempt += 1
                    continue
                msg = f"Clarion identity resolve unreachable at {url}: {exc}"
                raise RegistryUnavailableError(msg, url=url, path="", cause_kind="network") from exc

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            msg = f"Clarion identity resolve returned invalid JSON from {url}: {exc}"
            raise RegistryUnavailableError(msg, url=url, path="", cause_kind="invalid_response") from exc
        if not isinstance(payload, dict):
            msg = f"Clarion identity resolve returned non-object response from {url}: {type(payload).__name__}"
            raise RegistryUnavailableError(msg, url=url, path="", cause_kind="invalid_response")
        return self._parse_sei_resolution(payload, url=url, requested_locators=chunk)

    @staticmethod
    def _parse_sei_resolution(payload: dict[str, Any], *, url: str, requested_locators: list[str]) -> SeiResolution:
        def require_list(field: str) -> list[Any]:
            value = payload.get(field, [])
            if not isinstance(value, list):
                msg = f"Clarion identity resolve at {url}: {field!r} must be a list"
                raise RegistryUnavailableError(msg, url=url, path="", cause_kind="invalid_response")
            return value

        def record_outcome(locator: str, channel: str) -> None:
            existing = outcomes.get(locator)
            if existing is not None:
                msg = f"Clarion identity resolve at {url}: locator {locator!r} appears in multiple result channels: {existing}, {channel}"
                raise RegistryUnavailableError(msg, url=url, path="", cause_kind="invalid_response")
            outcomes[locator] = channel

        requested_locator_set = set(requested_locators)
        outcomes: dict[str, str] = {}

        resolved: dict[str, str] = {}
        raw_resolved = payload.get("resolved", {})
        if not isinstance(raw_resolved, dict):
            msg = f"Clarion identity resolve at {url}: 'resolved' must be an object"
            raise RegistryUnavailableError(msg, url=url, path="", cause_kind="invalid_response")
        for locator, record in raw_resolved.items():
            if not isinstance(record, dict) or not isinstance(record.get("sei"), str) or not record["sei"]:
                msg = f"Clarion identity resolve at {url}: resolved entry for {locator!r} missing non-empty string 'sei'"
                raise RegistryUnavailableError(msg, url=url, path="", cause_kind="invalid_response")
            record_outcome(locator, "resolved")
            resolved[locator] = record["sei"]

        orphaned: list[str] = []
        for item in require_list("not_found"):
            if not isinstance(item, str):
                msg = f"Clarion identity resolve at {url}: 'not_found' item must be a string"
                raise RegistryUnavailableError(msg, url=url, path="", cause_kind="invalid_response")
            record_outcome(item, "not_found")
            orphaned.append(item)

        already_migrated: list[str] = []
        for item in require_list("invalid"):
            if not isinstance(item, str):
                msg = f"Clarion identity resolve at {url}: 'invalid' item must be a string"
                raise RegistryUnavailableError(msg, url=url, path="", cause_kind="invalid_response")
            record_outcome(item, "invalid")
            already_migrated.append(item)

        # Completeness, mirroring the file-path sibling (_parse_batch_response):
        # every submitted locator must appear in exactly one channel. A locator
        # Clarion silently drops from all channels would otherwise read as None
        # downstream and destructively orphan a live binding — so an omission is
        # rejected, not inferred. An orphan is only one Clarion *affirmatively*
        # reported in 'not_found'.
        unexpected = sorted(set(outcomes) - requested_locator_set)
        if unexpected:
            joined = ", ".join(repr(loc) for loc in unexpected)
            msg = f"Clarion identity resolve at {url}: response included unexpected locator(s): {joined}"
            raise RegistryUnavailableError(msg, url=url, path="", cause_kind="invalid_response")
        missing = sorted(requested_locator_set - set(outcomes))
        if missing:
            msg = f"Clarion identity resolve at {url}: missing outcome for requested locator(s): {', '.join(repr(loc) for loc in missing)}"
            raise RegistryUnavailableError(msg, url=url, path="", cause_kind="invalid_response")

        return SeiResolution(resolved=resolved, orphaned=orphaned, already_migrated=already_migrated)

    def is_displaced(self) -> bool:
        return True

    def close(self) -> None:
        self._http_client.close()
