"""Turn the logs into something a person — or an AI assistant — can act on.

:func:`summarize` reads the error log and the newest debug sessions (see
debuglog.py) and writes a Markdown digest ordered for triage: problems first
(errors by code, silent fallbacks, crashes, UI stalls), then performance
(slowest conversions, per-page timing, cleanup passes, preview renders,
memory), then quality (scores, what each cleanup pass removed, how the PDF
reader decided). :func:`build_report` zips the digest with the raw logs and
settings so a user can hand the whole picture over in one file.

Run from a checkout: ``python -m markdown_sidekick.cli diagnostics``.
"""

from __future__ import annotations

import json
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from . import debuglog

# A cleanup pass that removes more than this share of a document's lines is
# worth a human look — the passes are meant to favour false negatives.
_HEAVY_PASS_SHARE = 0.05
_SLOW_PREVIEW_MS = 250


def load_jsonl(path: Path) -> list[dict]:
    """Every parseable line of a JSON-lines file (a crash can leave a torn
    last line; it is skipped, not fatal)."""
    records = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict):
                    records.append(record)
    except OSError:
        pass
    return records


def recent_sessions(limit: int) -> list[Path]:
    """The newest debug session folders, newest first."""
    root = debuglog.log_dir() / debuglog.SESSIONS_DIR
    try:
        dirs = [p for p in root.iterdir() if (p / "events.jsonl").exists()]
    except OSError:
        return []
    return sorted(dirs, key=lambda p: p.name, reverse=True)[: max(0, limit)]


def load_errors(days: int | None = 30) -> list[dict]:
    records = []
    for path in debuglog.error_log_files():
        records.extend(load_jsonl(path))
    if days is not None:
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        records = [r for r in records if str(r.get("t", "")) >= cutoff]
    return sorted(records, key=lambda r: str(r.get("t", "")))


def _cell(value: object, limit: int = 90) -> str:
    text = " ".join(str(value).split()).replace("|", "\\|")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _tb_tail(tb: str, lines: int = 12) -> str:
    return "\n".join(tb.rstrip().splitlines()[-lines:])


def _table(header: list[str], rows: list[list[object]]) -> list[str]:
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    out += ["| " + " | ".join(_cell(c) for c in row) + " |" for row in rows]
    return out + [""]


# ---------------------------------------------------------------------------
# Error log
# ---------------------------------------------------------------------------
def _errors_section(records: list[dict]) -> list[str]:
    out = ["## Error log", ""]
    if not records:
        return out + ["No errors recorded in the last 30 days.", ""]
    severities = Counter(r.get("severity", "error") for r in records)
    out.append(
        f"{len(records)} incident(s) in the last 30 days — "
        + ", ".join(f"{n} {sev}" for sev, n in severities.most_common())
        + "."
    )
    out.append("")
    by_code: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        by_code[str(r.get("code", "?"))].append(r)
    rows = []
    for code, items in sorted(by_code.items(), key=lambda kv: -len(kv[1])):
        last = items[-1]
        rows.append(
            [code, len(items), str(last.get("t", ""))[:19], last.get("title", ""),
             last.get("ref", ""), last.get("where", "")]
        )
    out += _table(["Code", "Count", "Last seen", "What happened", "Last ref", "Where"], rows)
    out += ["### Most recent incidents", ""]
    for r in records[-10:][::-1]:
        out.append(
            f"- {str(r.get('t', ''))[:19]} **{r.get('code')}** (ref {r.get('ref')}, "
            f"v{r.get('app_version', '?')}) {r.get('where', '')} — {_cell(r.get('detail', ''), 200)}"
        )
        if r.get("tb"):
            out += ["", "  ```", *("  " + l for l in _tb_tail(r["tb"]).splitlines()), "  ```"]
    return out + [""]


# ---------------------------------------------------------------------------
# Debug sessions
# ---------------------------------------------------------------------------
def _session_section(folder: Path) -> list[str]:
    events = load_jsonl(folder / "events.jsonl")
    out = [f"## Debug session {folder.name}", ""]
    if not events:
        return out + ["(empty)", ""]
    start = next((e for e in events if e.get("ev") == "session.start"), {})
    end = next((e for e in events if e.get("ev") == "session.end"), None)
    span = events[-1].get("rel", 0)
    out.append(
        f"- Source: {start.get('source', '?')} · app {start.get('app_version', '?')} · "
        f"{start.get('platform', '?')} · Python {start.get('python', '?')} · "
        f"{'frozen build' if start.get('frozen') else 'from source'}"
    )
    out.append(
        f"- Machine: {start.get('cpu_count', '?')} CPUs · {start.get('ram_total_mb', '?')} MB RAM · "
        f"{start.get('processor', '')}"
    )
    out.append(
        f"- Duration: {span:.0f} s"
        + ("" if end else " (no session.end — the app crashed or was killed)")
        + (f" · peak memory {end.get('peak_mb')} MB" if end and end.get("peak_mb") else "")
    )
    packages = start.get("packages") or {}
    if packages:
        out.append("- Packages: " + ", ".join(f"{k} {v}" for k, v in sorted(packages.items())))
    fault = folder / "faulthandler.log"
    if fault.exists() and fault.stat().st_size:
        out.append(
            f"- **faulthandler.log has {fault.stat().st_size:,} bytes** — a native crash or a "
            "UI hang of 20 s+ dumped thread stacks there (a file dialog left open also does)."
        )
    out.append("")
    out += _problems(events)
    out += _performance(events)
    out += _quality(events)
    return out


def _problems(events: list[dict]) -> list[str]:
    out = ["### Problems", ""]
    errors = [e for e in events if e.get("ev") == "error"]
    if errors:
        out.append(f"**{len(errors)} error incident(s)** (details in the error log section):")
        for e in errors[-15:]:
            out.append(f"- {e.get('code')} (ref {e.get('ref')}) {e.get('where', '')}: {_cell(e.get('detail', ''), 160)}")
        out.append("")
    # markitdown is the last route: its failure is the file's error, listed above.
    routes = [
        e
        for e in events
        if e.get("ev") in ("route", "pdf.analysis")
        and e.get("ok") is False
        and e.get("route") != "markitdown"
    ]
    if routes:
        out.append("**Routes that failed and fell through** (the output came from a lesser route):")
        for e in routes:
            name = e.get("route") or e.get("ev")
            out.append(f"- {e.get('file', '?')}: `{name}` — {_cell(e.get('error', ''), 160)}")
        out.append("")
    thin = [e for e in events if e.get("ev") == "route" and e.get("outcome") == "too_little_text"]
    if thin:
        out.append("**PDF layout reader found too little text** (scanned PDF with OCR off?):")
        out += [f"- {e.get('file', '?')}: {e.get('body_chars')} chars" for e in thin]
        out.append("")
    stalls = [e for e in events if e.get("ev") == "ui.stall"]
    if stalls:
        worst = max(stalls, key=lambda e: e.get("gap_ms", 0))
        out.append(
            f"**UI stalls: {len(stalls)}** (Tk loop blocked ≥ 1 s) — worst {worst.get('gap_ms')} ms "
            f"after `{worst.get('last_event')}`:"
        )
        for e in sorted(stalls, key=lambda e: -e.get("gap_ms", 0))[:8]:
            out.append(f"- {e.get('gap_ms')} ms at +{e.get('rel')} s after `{e.get('last_event')}`")
        out.append("")
    logs = [e for e in events if e.get("ev") == "log"]
    if logs:
        grouped = Counter((e.get("logger"), _cell(e.get("msg", ""), 140)) for e in logs)
        out.append("**Library warnings:**")
        out += [f"- {n}× {logger}: {msg}" for (logger, msg), n in grouped.most_common(10)]
        out.append("")
    if len(out) == 2:
        out += ["None recorded.", ""]
    return out


def _performance(events: list[dict]) -> list[str]:
    out = ["### Performance", ""]
    ends = [e for e in events if e.get("ev") == "convert.end"]
    starts = {e.get("conv"): e for e in events if e.get("ev") == "convert.start"}
    timing = {e.get("conv"): e for e in events if e.get("ev") == "pages.timing"}
    if ends:
        rows = []
        for e in sorted(ends, key=lambda e: -e.get("ms", 0))[:15]:
            st = starts.get(e.get("conv"), {})
            pt = timing.get(e.get("conv"), {})
            pages = pt.get("pages", "")
            per_page = pt.get("mean_ms", "")
            rows.append([
                e.get("file", "?"), e.get("engine", ""), "ok" if e.get("ok") else e.get("code", "error"),
                f"{e.get('ms', 0):,}", f"{(st.get('bytes') or 0) // 1024:,}", pages, per_page,
                f"{e.get('chars', 0):,}", e.get("rss_mb", ""),
            ])
        out.append(f"**Conversions: {len(ends)}** (slowest first)")
        out.append("")
        out += _table(
            ["File", "Engine", "Result", "ms", "KB", "Pages", "ms/page", "Chars", "RSS MB"], rows
        )
    for e in [e for e in events if e.get("ev") == "pages.timing"]:
        if e.get("p95_ms", 0) > 500:
            out.append(
                f"- {e.get('file', '?')} `{e.get('route')}`: {e.get('pages')} pages, mean "
                f"{e.get('mean_ms')} ms, p95 {e.get('p95_ms')} ms, slowest {e.get('slowest')}"
            )
    for e in [e for e in events if e.get("ev") == "ocr.engine_init"]:
        out.append(
            f"- OCR engine: setting `{e.get('device_setting')}` → `{e.get('device')}` "
            f"(providers {e.get('providers')}) in {e.get('ms')} ms"
        )
        if e.get("device") == "cpu" and "DmlExecutionProvider" in (e.get("providers") or []):
            out.append("  - ⚠ DirectML is available but OCR ran on the CPU (~6x slower).")
    for e in [e for e in events if e.get("ev") == "whisper.done"]:
        out.append(
            f"- Whisper `{e.get('model')}` on {e.get('device')}/{e.get('compute_type')}: "
            f"{e.get('audio_s')} s of audio in {e.get('ms')} ms (realtime factor {e.get('realtime_factor')})"
        )
    passes: dict[str, float] = defaultdict(float)
    for e in [e for e in events if e.get("ev") == "cleanup.done"]:
        for p in e.get("passes") or []:
            passes[p.get("pass", "?")] += p.get("ms", 0)
    if passes:
        top = sorted(passes.items(), key=lambda kv: -kv[1])[:6]
        out.append("- Cleanup time by pass (all documents): " + ", ".join(f"{k} {v:,.0f} ms" for k, v in top))
    for e in [e for e in events if e.get("ev") == "pdflayout.done"]:
        phases = e.get("phases_ms") or {}
        out.append(
            f"- pdflayout {e.get('file', '?')}: "
            + ", ".join(f"{k} {v:,.0f} ms" for k, v in phases.items())
        )
    slow_previews = [e for e in events if e.get("ev") == "ui.preview" and e.get("ms", 0) >= _SLOW_PREVIEW_MS]
    if slow_previews:
        worst = max(slow_previews, key=lambda e: e.get("ms", 0))
        out.append(
            f"- Slow preview renders (≥ {_SLOW_PREVIEW_MS} ms on the UI thread): {len(slow_previews)}, "
            f"worst {worst.get('ms')} ms for {worst.get('file')} ({worst.get('chars'):,} chars, "
            f"cache miss: {worst.get('miss')})"
        )
    for e in [e for e in events if e.get("ev") in ("export.book", "export.single")]:
        out.append(f"- {e.get('ev')}: {e.get('mode', 'single')} → {len(e.get('parts') or [1])} file(s)")
    for e in [e for e in events if e.get("ev") == "llm.http"]:
        out.append(f"- Local AI {e.get('model')}: {e.get('ms')} ms, {e.get('bytes')} bytes")
    if len(out) == 2:
        out += ["Nothing measured.", ""]
    return out + [""]


def _quality(events: list[dict]) -> list[str]:
    out = ["### Quality", ""]
    quality = [e for e in events if e.get("ev") == "quality" and e.get("file")]
    latest: dict[str, dict] = {}
    for e in quality:
        latest[e["file"]] = e
    if latest:
        rows = [
            [f, e.get("score"), f"{e.get('est_tokens', 0):,}", e.get("headings"),
             "; ".join(e.get("issues") or []) or "—"]
            for f, e in sorted(latest.items(), key=lambda kv: kv[1].get("score", 100))
        ]
        out += _table(["File", "Score", "Tokens", "Headings", "Issues"], rows)
    for e in [e for e in events if e.get("ev") == "cleanup.done"]:
        lines = max(1, e.get("lines_in") or (e.get("chars_in") or 0) // 60)
        for p in e.get("passes") or []:
            gone = p.get("lines_gone", 0)
            if gone and gone / lines >= _HEAVY_PASS_SHARE:
                out.append(
                    f"- ⚠ {e.get('file', '?')}: `{p.get('pass')}` removed/rewrote {gone:,} lines "
                    f"(~{gone / lines:.0%} of the document) — check it deleted no real content. Samples:"
                )
                out += [f"  - `{_cell(s, 150)}`" for s in (p.get("samples") or [])[:6]]
    for e in [e for e in events if e.get("ev") == "pdflayout.done"]:
        stats = e.get("stats") or {}
        out.append(
            f"- pdflayout {e.get('file', '?')}: title “{e.get('title', '')}” from "
            f"{e.get('title_source') or 'nowhere'}; {stats.get('pages')} pages, {stats.get('tables')} tables, "
            f"{stats.get('code_blocks')} code blocks, {stats.get('headings')} headings, "
            f"{e.get('furniture_removed')} running headers/folios removed, "
            f"{stats.get('ligatures_repaired')} ligatures repaired, "
            f"{stats.get('duplicate_chars')} shadow glyphs dropped, mono share {e.get('mono_share')}"
        )
    snaps = [e for e in events if e.get("ev") == "snapshot"]
    if snaps:
        out.append(f"- {len(snaps)} Markdown snapshot(s) in snapshots/ (raw vs cleaned per file).")
    if len(out) == 2:
        out += ["Nothing assessed.", ""]
    return out + [""]


def summarize(sessions: int = 1, days: int | None = 30) -> str:
    """The Markdown digest of the error log plus the newest debug sessions."""
    from . import __version__

    out = [
        "# Markdown Sidekick diagnostics",
        "",
        f"Generated {datetime.now():%Y-%m-%d %H:%M} by v{__version__} · logs in `{debuglog.log_dir()}`",
        "",
    ]
    out += _errors_section(load_errors(days))
    folders = recent_sessions(sessions)
    if not folders:
        out += [
            "## Debug sessions",
            "",
            "None recorded. Turn on Settings → Diagnostics → *Debug mode* (or run with "
            "`--debug`) and reproduce the problem to capture a full trace.",
            "",
        ]
    for folder in folders:
        out += _session_section(folder)
    return "\n".join(out).rstrip() + "\n"


def build_report(dest: Path, sessions: int = 5, include_snapshots: bool = True) -> Path:
    """Zip the digest, the error log, the newest debug sessions and the
    settings into ``dest`` for sharing. Returns ``dest``."""
    from .settings import Settings

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    root = debuglog.log_dir()
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("summary.md", summarize(sessions))
        for path in debuglog.error_log_files():
            zf.write(path, f"logs/{path.name}")
        for folder in recent_sessions(sessions):
            for path in folder.rglob("*"):
                if not path.is_file():
                    continue
                if not include_snapshots and path.parent.name == "snapshots":
                    continue
                zf.write(path, f"logs/{path.relative_to(root).as_posix()}")
        settings = Settings.config_path()
        if settings.exists():
            zf.write(settings, "settings.json")
        zf.writestr("environment.json", json.dumps(debuglog.environment(), indent=2, default=str))
    return dest
