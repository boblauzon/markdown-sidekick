"""Optional local-LLM finishing passes (Ollama-compatible endpoint).

Heuristics fix ~95% of conversion artifacts; the remainder (garbled words,
mis-joined sentences, odd table cells) needs judgement. When the user points
the app at a local Ollama server (Settings → ollama_endpoint), two opt-in
extras become available:

- :func:`polish_markdown` — chunk the document and ask a text model to repair
  ONLY mechanical artifacts, with a size guardrail so a model that rewrites or
  truncates a chunk is ignored and the original kept.
- :func:`caption_image` — ask a vision model for one-sentence alt text for an
  extracted figure, so AI readers see the diagram's content, not a bare link.

Everything degrades silently: any network/model failure returns the input (or
None for captions). Nothing here ever runs unless an endpoint is configured.
"""

from __future__ import annotations

import base64
import json
import re
import urllib.request
from pathlib import Path
from typing import Callable

_TIMEOUT_S = 180
_CHUNK_TARGET_CHARS = 8_000
# Guardrail: a repaired chunk must stay close in size to the original —
# a big delta means the model summarised, expanded, or truncated.
_MIN_RATIO = 0.7
_MAX_RATIO = 1.3

_POLISH_PROMPT = (
    "You are a mechanical text-repair filter for Markdown converted from PDF. "
    "Fix ONLY conversion artifacts: garbled or split words, stray page-number "
    "fragments, broken list items, misaligned table cells. Do NOT rewrite, "
    "summarise, reorder, translate, or add/remove content. Preserve all "
    "Markdown syntax, code blocks, and HTML comments exactly. Return ONLY the "
    "repaired Markdown with no commentary.\n\n"
)

_CAPTION_PROMPT = (
    "Describe this figure from a technical document in one factual sentence "
    "for use as image alt text. No preamble."
)


def _post_json(url: str, payload: dict, timeout: int = _TIMEOUT_S) -> dict | None:
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def llm_available(endpoint: str) -> bool:
    """Cheap reachability probe (GET /api/tags)."""
    if not endpoint:
        return False
    try:
        with urllib.request.urlopen(f"{endpoint}/api/tags", timeout=3) as resp:
            return resp.status == 200
    except Exception:
        return False


def _generate(endpoint: str, model: str, prompt: str, images: list[str] | None = None) -> str | None:
    payload: dict = {"model": model, "prompt": prompt, "stream": False}
    if images:
        payload["images"] = images
    data = _post_json(f"{endpoint}/api/generate", payload)
    if not data:
        return None
    text = data.get("response")
    return text if isinstance(text, str) and text.strip() else None


def _split_chunks(text: str, target: int = _CHUNK_TARGET_CHARS) -> list[str]:
    """Split at blank lines near the target size; never inside a code fence."""
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    in_fence = False
    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        current.append(line)
        size += len(line) + 1
        if size >= target and not in_fence and not line.strip():
            chunks.append("\n".join(current))
            current, size = [], 0
    if current:
        chunks.append("\n".join(current))
    return chunks


def polish_markdown(
    text: str,
    endpoint: str,
    model: str,
    on_progress: Callable[[int, int], None] | None = None,
) -> tuple[str, int]:
    """Repair residual artifacts chunk by chunk; returns (text, chunks_changed).

    A chunk is only replaced when the model's output stays within a size
    envelope of the original — otherwise the original is kept verbatim.
    """
    if not endpoint or not model or not text.strip():
        return text, 0
    chunks = _split_chunks(text)
    out: list[str] = []
    changed = 0
    total = len(chunks)
    for n, chunk in enumerate(chunks, start=1):
        repaired = _generate(endpoint, model, _POLISH_PROMPT + chunk)
        if repaired is not None and _MIN_RATIO <= len(repaired) / max(1, len(chunk)) <= _MAX_RATIO:
            # Strip a markdown wrapper some models add around the whole reply.
            m = re.fullmatch(r"```(?:markdown)?\n(.*)\n```\s*", repaired, re.S)
            if m:
                repaired = m.group(1)
            if repaired != chunk:
                changed += 1
            out.append(repaired)
        else:
            out.append(chunk)
        if on_progress is not None:
            on_progress(n, total)
    return "\n".join(out) if len(out) > 1 else (out[0] if out else text), changed


def caption_image(image_path: str | Path, endpoint: str, model: str) -> str | None:
    """One-sentence alt text for a figure, or None on any failure."""
    if not endpoint or not model:
        return None
    try:
        data = Path(image_path).read_bytes()
    except OSError:
        return None
    b64 = base64.b64encode(data).decode("ascii")
    caption = _generate(endpoint, model, _CAPTION_PROMPT, images=[b64])
    if caption is None:
        return None
    caption = " ".join(caption.split())
    # Alt text must stay a short single line.
    return caption[:300] if caption else None
