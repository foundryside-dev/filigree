"""Resolve scanner callback URLs from the active Filigree dashboard state."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from filigree.core import get_mode
from filigree.ephemeral import read_port_file
from filigree.server import DEFAULT_PORT


@dataclass(frozen=True)
class ScannerApiUrlResolution:
    url: str
    source: str


def resolve_scanner_api_url(filigree_dir: Path, *, explicit_api_url: str | None = None) -> str:
    return resolve_scanner_api_url_with_source(filigree_dir, explicit_api_url=explicit_api_url).url


def resolve_scanner_api_url_with_source(filigree_dir: Path, *, explicit_api_url: str | None = None) -> ScannerApiUrlResolution:
    """Return the dashboard URL scanners should use for result callbacks.

    Explicit caller input wins. Otherwise use the same port sources that start
    and verify the dashboard: server mode reads ``server.json`` and ethereal
    mode reads the active ``.filigree/ephemeral.port`` file. If no active
    ethereal port has been recorded yet, fall back to the legacy default.
    """
    if explicit_api_url is not None:
        return ScannerApiUrlResolution(url=explicit_api_url.strip().rstrip("/"), source="explicit")

    mode = get_mode(filigree_dir)

    if mode == "server":
        from filigree.server import read_server_config

        return ScannerApiUrlResolution(url=f"http://localhost:{read_server_config().port}", source="server_config")

    port = read_port_file(filigree_dir / "ephemeral.port")
    source = "ephemeral_port"
    if port is None:
        port = DEFAULT_PORT
        source = "fallback_default"
    return ScannerApiUrlResolution(url=f"http://localhost:{port}", source=source)
