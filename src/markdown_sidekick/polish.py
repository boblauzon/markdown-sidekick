"""Optional local-AI finishing passes (Ollama, LM Studio, Jan, LocalAI, …).

Heuristics fix ~95% of conversion artifacts; the remainder (garbled words,
mis-joined sentences, odd table cells) needs judgement. When the user points
the app at a local AI server (Settings → Local AI — either the Ollama-native
dialect or any OpenAI-compatible /v1 server), two opt-in extras become
available:

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
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
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
    """Cheap reachability probe — true for either local-AI dialect."""
    if not endpoint:
        return False
    return _probe_protocol(endpoint)[0] is not None


# Well-known local AI runtimes, probed in order when no endpoint is
# configured. THE single source for the roster — endpoints, friendly names,
# and the port→name lookup all derive from this table. Localhost-only by
# design; detection never leaves the machine unless the user configured a
# remote endpoint. Spelled 127.0.0.1 rather than "localhost" deliberately:
# on Windows, localhost resolves to ::1 first and a v4-only daemon (Ollama's
# default bind) costs ~2s of IPv6-refusal retries on EVERY request —
# measured 2045ms vs 4ms per call against a live Ollama.
LOCAL_RUNTIMES: tuple[tuple[str, str], ...] = (
    ("http://127.0.0.1:11434", "Ollama"),
    ("http://127.0.0.1:1234", "LM Studio"),
    ("http://127.0.0.1:1337", "Jan"),
    ("http://127.0.0.1:8080", "LocalAI"),
)
KNOWN_LOCAL_ENDPOINTS: tuple[str, ...] = tuple(ep for ep, _name in LOCAL_RUNTIMES)
DEFAULT_OLLAMA_ENDPOINT = KNOWN_LOCAL_ENDPOINTS[0]
_PORT_NAMES = {
    urllib.parse.urlsplit(ep).port: name for ep, name in LOCAL_RUNTIMES
}
# Probe order matters: Ollama serves BOTH dialects, so the native probe is
# what distinguishes it from a pure-OpenAI-compatible server.
_PROBES: tuple[tuple[str, str, str, str], ...] = (
    ("/api/tags", "models", "name", "ollama"),
    ("/v1/models", "data", "id", "openai"),
)


def known_runtime_names() -> str:
    """The scanned-runtime roster as prose, for user-facing messages."""
    return ", ".join(name for _ep, name in LOCAL_RUNTIMES)


def _get_json(url: str, timeout: float) -> dict | None:
    """GET returning a parsed JSON dict, or None on any failure/non-dict."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _probe_protocol(endpoint: str, timeout: float = 3) -> tuple[str | None, list[str]]:
    """(protocol, models) for one endpoint, or (None, []) if nothing answers.

    The payload SHAPE is required, not just parseable JSON: a catch-all dev
    server that answers 200 + a JSON object on every path must read as
    unreachable, not as an empty Ollama.
    """
    endpoint = endpoint.rstrip("/")
    for path, list_key, id_field, protocol in _PROBES:
        data = _get_json(f"{endpoint}{path}", timeout)
        if data is None:
            continue
        entries = data.get(list_key)
        if not isinstance(entries, list):
            continue
        models = [
            mid
            for entry in entries
            if isinstance(entry, dict)
            and (mid := str(entry.get(id_field, "")).strip())
        ]
        return protocol, models
    return None, []


def _resolve_protocol(endpoint: str) -> str:
    """Probe once, per call chain — NEVER cached: a transient daemon hiccup
    must not pin the wrong dialect for the rest of the process (a stale
    "ollama" verdict makes every request 404 silently on a /v1 server)."""
    proto, _models = _probe_protocol(endpoint)
    return proto or "ollama"


def runtime_name(endpoint: str, protocol: str) -> str:
    """Friendly runtime label for status text, best-effort from the port."""
    if protocol == "ollama":
        return "Ollama"
    try:
        port = urllib.parse.urlsplit(endpoint).port
    except ValueError:
        port = None
    return _PORT_NAMES.get(port, "OpenAI-compatible local AI")


# Live local daemons answer in milliseconds; a short per-probe timeout keeps
# the nothing-installed blank scan quick, and probing ports concurrently
# makes its worst case one slow port, not the sum (serial 3s probes measured
# ~33s worst case on Windows).
_SCAN_TIMEOUT_S = 1.0


def detect_local_ai(
    endpoint: str = "", timeout: float = 3
) -> tuple[str, list[str], str, str]:
    """Probe for a local AI runtime; tri-state result across all dialects.

    Returns ``(status, models, protocol, endpoint)``:

    - ``("ok", [model, ...], protocol, endpoint)`` — running, models loaded
    - ``("empty", [], protocol, endpoint)`` — running but no models yet (the
      middle state naive probes skip — it prevents "connected but nothing
      happens" confusion)
    - ``("unreachable", [], "", "")`` — nothing answered

    A blank ``endpoint`` scans the well-known local ports concurrently (see
    ``LOCAL_RUNTIMES``) and reports the first-listed runtime found, preferring
    one with models over one without.
    """
    if endpoint.strip():
        candidates = [endpoint.rstrip("/")]
        results = [_probe_protocol(candidates[0], timeout=timeout)]
    else:
        candidates = list(KNOWN_LOCAL_ENDPOINTS)
        with ThreadPoolExecutor(max_workers=len(candidates)) as pool:
            results = list(
                pool.map(
                    lambda c: _probe_protocol(c, timeout=_SCAN_TIMEOUT_S), candidates
                )
            )
    empty_hit: tuple[str, list[str], str, str] | None = None
    for cand, (proto, models) in zip(candidates, results):
        if proto is None:
            continue
        if models:
            return "ok", models, proto, cand
        if empty_hit is None:
            empty_hit = ("empty", [], proto, cand)
    return empty_hit or ("unreachable", [], "", "")


def _generate(
    endpoint: str,
    model: str,
    prompt: str,
    images: list[str] | None = None,
    protocol: str | None = None,
) -> str | None:
    """Generate via whichever protocol the endpoint speaks.

    Callers doing many generations resolve the protocol once and pass it in;
    left None, it is resolved fresh (never cached across calls)."""
    endpoint = endpoint.rstrip("/")
    if (protocol or _resolve_protocol(endpoint)) == "openai":
        return _generate_openai(endpoint, model, prompt, images)
    payload: dict = {"model": model, "prompt": prompt, "stream": False}
    if images:
        payload["images"] = images
    data = _post_json(f"{endpoint}/api/generate", payload)
    if not data:
        return None
    text = data.get("response")
    return text if isinstance(text, str) and text.strip() else None


def _generate_openai(
    endpoint: str, model: str, prompt: str, images: list[str] | None = None
) -> str | None:
    """OpenAI-compatible /v1/chat/completions (LM Studio, Jan, LocalAI, …)."""
    if images:
        content: object = [{"type": "text", "text": prompt}] + [
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}
            for b64 in images
        ]
    else:
        content = prompt
    data = _post_json(
        f"{endpoint}/v1/chat/completions",
        {"model": model, "messages": [{"role": "user", "content": content}], "stream": False},
    )
    if not data:
        return None
    try:
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return None
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
    # One protocol resolution per run: chunks reuse it, and a transient
    # probe failure costs at most this run, never the whole process.
    protocol = _resolve_protocol(endpoint)
    chunks = _split_chunks(text)
    out: list[str] = []
    changed = 0
    total = len(chunks)
    for n, chunk in enumerate(chunks, start=1):
        repaired = _generate(endpoint, model, _POLISH_PROMPT + chunk, protocol=protocol)
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
