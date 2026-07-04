"""Optional high-fidelity conversion via a local MinerU server.

MinerU (https://github.com/opendatalab/mineru) gives layout-aware Markdown for
complex / scientific / multi-column PDFs. It is heavy (PyTorch, multi-GB models),
so rather than bundling it we talk to a MinerU server the user runs and points us
at via Settings. Everything here fails soft — any error returns ``None`` and the
caller falls back to the local OCR / markitdown pipeline.
"""

from __future__ import annotations

from pathlib import Path

# Response keys different MinerU versions use for the Markdown payload.
_MD_KEYS = ("md_content", "markdown", "md", "content")
_DEFAULT_TIMEOUT = 300


def mineru_configured(endpoint: str | None) -> bool:
    return bool(endpoint and endpoint.strip())


def convert_via_mineru(
    path: str | Path, endpoint: str, timeout: int = _DEFAULT_TIMEOUT
) -> str | None:
    """POST ``path`` to the MinerU server; return Markdown, or None on any failure."""
    try:
        import requests  # bundled via markitdown's deps
    except ImportError:
        return None
    url = endpoint.rstrip("/") + "/file_parse"
    try:
        with open(path, "rb") as fh:
            response = requests.post(
                url,
                files={"file": fh},
                data={"return_md": "true"},
                timeout=timeout,
            )
    except Exception:
        return None
    if response.status_code != 200:
        return None
    # Prefer a JSON payload; fall back to a raw Markdown body.
    try:
        data = response.json()
    except ValueError:
        text = (response.text or "").strip()
        return text or None
    if isinstance(data, dict):
        for key in _MD_KEYS:
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value
    return None
