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

import json
import logging
from collections.abc import Callable
from dataclasses import KW_ONLY, dataclass
from typing import Any, Protocol, TypedDict
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from filigree.types.core import EntityId, FileId, RegistryBackend, make_content_hash, make_entity_id, make_file_id

logger = logging.getLogger(__name__)

# Name of the env var that carries the Clarion Bearer token by default.
# Not a token value itself; the actual token lives in the operator's
# environment under this name. Suppressing the hardcoded-secret lint
# because this string is an env-var name, not a credential.
DEFAULT_CLARION_TOKEN_ENV = "CLARION_LOOM_TOKEN"  # noqa: S105

DEFAULT_TEST_REGISTRY_BACKENDS: tuple[RegistryBackend, ...] = ("local", "clarion")
REGISTRY_BACKEND_FEATURES: tuple[RegistryBackend, ...] = ("local", "clarion")

# Clarion's `_capabilities` response declares an `api_version: u8`. Filigree
# rejects startup under `clarion` mode if Clarion advertises a version this
# build was not written against — a mismatch means the wire contract changed
# in a way no in-process fallback can mask. Bumped when ADR-014 makes a
# breaking change to the resolver protocol (see ADR-014 §4 and the
# Briefing-block masking section).
EXPECTED_CLARION_API_VERSION = 1


class ResolvedFile(TypedDict):
    """File identity resolved by the configured registry backend.

    ``content_hash=""`` is reserved for the local registry sentinel. Displaced
    registries must supply a non-empty hash token.
    """

    file_id: FileId | EntityId
    content_hash: str
    canonical_path: str
    language: str
    registry_backend: RegistryBackend


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


def clarion_capabilities_url(base_url: str) -> str:
    """Build the Clarion capability-probe URL."""
    return f"{base_url.rstrip('/')}/api/v1/_capabilities"


def _build_clarion_request(
    url: str,
    *,
    auth_token: str | None,
    data: bytes | None = None,
    method: str | None = None,
) -> Request:
    """Build a Clarion HTTP Request, attaching the Bearer token if present.

    Per the Clarion 1.0 cross-product contract: send ``Authorization: Bearer <token>``
    whenever a token is configured and resolved; otherwise send no auth header
    (Clarion accepts unauthenticated on loopback bind, rejects on non-loopback).

    Pass ``data`` to issue a POST (Content-Type defaults to application/json
    since every Clarion POST in this codebase is JSON).
    """
    headers: dict[str, str] = {}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"
    if data is not None:
        headers.setdefault("Content-Type", "application/json")
    return Request(url, data=data, headers=headers, method=method)  # noqa: S310 — scheme validated by normalize_clarion_base_url


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
    request = _build_clarion_request(url, auth_token=auth_token)
    try:
        with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
            raw = response.read().decode("utf-8")
    except HTTPError as exc:
        reason = exc.reason or exc.msg
        if exc.code == 401:
            msg = f"Clarion capability probe rejected at {url}: HTTP 401 {reason} (check token_env)"
            raise RegistryUnavailableError(msg, url=url, path="", cause_kind="auth") from exc
        msg = f"Clarion capability probe failed at {url}: HTTP {exc.code} {reason}"
        raise RegistryUnavailableError(msg, url=url, path="", cause_kind="http_error") from exc
    except (URLError, TimeoutError, OSError) as exc:
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

    return ClarionCapabilities(
        registry_backend=payload["registry_backend"],
        file_registry=payload["file_registry"],
        api_version=payload["api_version"],
        instance_id=payload["instance_id"],
    )


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


def _is_briefing_blocked_body(exc: HTTPError) -> bool:
    """Return True if a 403 response body declares ``code: "BRIEFING_BLOCKED"``.

    HTTPError exposes the response body as a read-once stream (``exc.read()``);
    we tolerate empty / malformed bodies and treat them as "not specifically
    briefing-blocked" so generic 403s fall through to the regular
    RegistryResolutionError path.
    """
    try:
        raw = exc.read()
    except Exception:
        return False
    if not raw:
        return False
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
        return ResolvedFile(
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

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_url", normalize_clarion_base_url(self.base_url))
        if isinstance(self.timeout_seconds, bool) or not isinstance(self.timeout_seconds, int | float) or self.timeout_seconds <= 0:
            msg = f"clarion.timeout_seconds must be a positive number, got {self.timeout_seconds!r}"
            raise ValueError(msg)
        object.__setattr__(self, "timeout_seconds", float(self.timeout_seconds))
        if self.auth_token is not None and not isinstance(self.auth_token, str):
            msg = f"clarion.auth_token must be a string or None, got {type(self.auth_token).__name__}"
            raise ValueError(msg)

    def resolve_file(
        self,
        path: str,
        *,
        language: str = "",
        actor: str = "",
    ) -> ResolvedFile:
        url = clarion_file_read_url(self.base_url, path, language=language)
        request = _build_clarion_request(url, auth_token=self.auth_token)
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:  # noqa: S310
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            reason = exc.reason or exc.msg
            if exc.code == 401:
                msg = f"Clarion registry rejected auth at {url}: HTTP 401 {reason} (check token_env)"
                raise RegistryUnavailableError(msg, url=url, path=path, cause_kind="auth") from exc
            if exc.code == 403 and _is_briefing_blocked_body(exc):
                msg = f"Clarion registry refuses briefing-blocked file at {url}: HTTP 403 {reason}"
                raise RegistryBriefingBlockedError(msg, status_code=exc.code, url=url) from exc
            if exc.code == 404:
                msg = f"Clarion registry could not resolve file at {url}: HTTP 404 {reason}"
                raise RegistryFileNotFoundError(msg, status_code=exc.code, url=url) from exc
            if 400 <= exc.code < 500:
                msg = f"Clarion registry rejected file resolution at {url}: HTTP {exc.code} {reason}"
                raise RegistryResolutionError(msg, status_code=exc.code, url=url) from exc
            msg = f"Clarion registry unavailable at {url}: HTTP {exc.code} {reason}"
            raise RegistryUnavailableError(msg, url=url, path=path, cause_kind="http_error") from exc
        except (URLError, TimeoutError, OSError) as exc:
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

        return ResolvedFile(
            file_id=make_entity_id(payload["entity_id"]),
            content_hash=content_hash,
            canonical_path=payload["canonical_path"],
            language=payload["language"],
            registry_backend="clarion",
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
        body = json.dumps({"queries": [{"path": q["path"], "language": q.get("language", "")} for q in chunk]}).encode("utf-8")
        request = _build_clarion_request(url, auth_token=self.auth_token, data=body, method="POST")
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:  # noqa: S310
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            reason = exc.reason or exc.msg
            if exc.code == 401:
                msg = f"Clarion batch resolve rejected auth at {url}: HTTP 401 {reason} (check token_env)"
                raise RegistryUnavailableError(msg, url=url, path="", cause_kind="auth") from exc
            if exc.code == 403 and _is_briefing_blocked_body(exc):
                msg = f"Clarion batch resolve refuses briefing-blocked file(s) at {url}: HTTP 403 {reason}"
                raise RegistryBriefingBlockedError(msg, status_code=exc.code, url=url) from exc
            if 400 <= exc.code < 500:
                msg = f"Clarion batch resolve rejected request at {url}: HTTP {exc.code} {reason}"
                raise RegistryResolutionError(msg, status_code=exc.code, url=url) from exc
            msg = f"Clarion batch resolve failed at {url}: HTTP {exc.code} {reason}"
            raise RegistryUnavailableError(msg, url=url, path="", cause_kind="http_error") from exc
        except (URLError, TimeoutError, OSError) as exc:
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

        return self._parse_batch_response(payload, url=url)

    def _parse_batch_response(self, payload: dict[str, Any], *, url: str) -> BatchResolution:
        def require_list(field: str) -> list[Any]:
            if field not in payload:
                return []
            value = payload[field]
            if not isinstance(value, list):
                msg = f"Clarion batch resolve at {url}: '{field}' must be a list"
                raise RegistryUnavailableError(msg, url=url, path="", cause_kind="invalid_response")
            return value

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
            resolved[item["requested_path"]] = ResolvedFile(
                file_id=make_entity_id(item["entity_id"]),
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
            not_found.append(item)
        briefing_blocked: list[str] = []
        for item in require_list("briefing_blocked"):
            if not isinstance(item, str):
                msg = f"Clarion batch resolve at {url}: 'briefing_blocked' item must be a string"
                raise RegistryUnavailableError(msg, url=url, path="", cause_kind="invalid_response")
            briefing_blocked.append(item)
        errors: list[BatchResolutionError] = []
        for item in require_list("errors"):
            if not isinstance(item, dict):
                msg = f"Clarion batch resolve at {url}: 'errors' item must be an object"
                raise RegistryUnavailableError(msg, url=url, path="", cause_kind="invalid_response")
            requested = item.get("requested_path")
            code = item.get("code")
            message = item.get("message")
            if not isinstance(requested, str) or not isinstance(code, str) or not isinstance(message, str):
                msg = f"Clarion batch resolve at {url}: errors entry missing string field(s): requested_path, code, message"
                raise RegistryUnavailableError(msg, url=url, path="", cause_kind="invalid_response")
            errors.append(BatchResolutionError(requested_path=requested, code=code, message=message))

        return BatchResolution(
            resolved=resolved,
            not_found=not_found,
            briefing_blocked=briefing_blocked,
            errors=errors,
            messages={},
        )

    def is_displaced(self) -> bool:
        return True
