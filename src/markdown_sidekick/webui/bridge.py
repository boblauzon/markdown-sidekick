"""The Python half of the web UI: the file list, the conversion worker, and
the calls the page makes (``window.pywebview.api.<name>``).

pywebview runs every API call on its own thread, so all list state sits
behind one lock and the page is only ever sent whole snapshots (``_push``),
never a half-updated list. Everything that isn't an API method is private:
pywebview exposes every public attribute of this object to the page.
"""

from __future__ import annotations

import ctypes
import json
import re
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path

from .. import __app_name__, __version__, debuglog, errors, export
from ..audio import MEDIA_EXTENSIONS
from ..export import AI_TARGETS
from ..guide import KOFI_URL, load_user_guide
from ..ocr import OCR_IMAGE_EXTENSIONS
from ..settings import Settings

# Same cap as the Tk preview: the page renders at most this much of a
# document; Copy and Save always use all of it.
PREVIEW_MAX_CHARS = 400_000
# Progress pushes faster than this are coalesced (OCR reports every page,
# Whisper every segment).
_PUSH_INTERVAL_S = 0.12
# The only links the preview keeps clickable. Any other scheme (search-ms:,
# ms-settings:, …) could start a program: a middle-click asks WebView2 for a
# new window, and pywebview hands that URL to Windows whatever it is.
_WEB_LINK = re.compile(r"(?i)^https?://")

_STYLES = (
    ("single", "One file", "One .md per source file."),
    ("chapters", "Chapter files", "Each book becomes a folder of per-chapter files, index.md and manifest.json."),
    ("ai", "AI-sized sections", "Parts guaranteed to fit an AI's context window. Short chapters are packed together."),
)
_FORMATS = (
    ("Documents", "PDF, DOCX, PPTX, XLSX, XLS, EPUB"),
    ("Web / data", "HTML, CSV, JSON, XML, TXT, MD"),
    ("Images", "PNG, JPG, GIF, BMP, TIFF, WEBP"),
    ("Audio / video", "MP3, WAV, M4A, FLAC, OGG, MP4, MKV, MOV"),
    ("Archives", "ZIP (contents converted recursively)"),
)


def _fmt_size(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f} MB"
    return f"{max(1, round(n / 1000))} KB"


def _fmt_mmss(seconds: float) -> str:
    seconds = int(max(0, seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def _kind(path: Path) -> str:
    ext = path.suffix.lower()
    if ext == ".pdf":
        return "pdf"
    if ext in OCR_IMAGE_EXTENSIONS or ext == ".gif":
        return "image"
    if ext in MEDIA_EXTENSIONS:
        return "audio"
    return "doc"


@dataclass
class _Item:
    id: str
    path: Path
    size: int
    result: object = None  # ConversionResult once converted
    progress: float = 0.0
    sub: str = ""  # "page 3/12" or "1:20/41:30" while converting
    # Derived from the result: cleaned text and its stats, and a quality
    # report per Clean-toggle state. Assessing a big book costs ~0.2 s.
    cleaned: str | None = None
    clean_stats: object = None
    quality: dict = field(default_factory=dict)
    # The worker is precomputing the above; a preview waits for it rather
    # than cleaning the same book a second time.
    preparing: bool = False

    def evict(self) -> None:
        self.cleaned = None
        self.clean_stats = None
        self.quality.clear()
        self.preparing = False


def _set_clipboard(text: str) -> bool:
    """CF_UNICODETEXT on the Windows clipboard (the page can't hold a 5 MB
    book just to copy it)."""
    k32, u32 = ctypes.windll.kernel32, ctypes.windll.user32
    k32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    k32.GlobalAlloc.restype = ctypes.c_void_p
    k32.GlobalLock.argtypes = [ctypes.c_void_p]
    k32.GlobalLock.restype = ctypes.c_void_p
    k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    k32.GlobalFree.argtypes = [ctypes.c_void_p]
    u32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    u32.SetClipboardData.restype = ctypes.c_void_p
    data = text.encode("utf-16-le") + b"\0\0"
    for _ in range(10):  # another program may hold the clipboard briefly
        if u32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        return False
    try:
        u32.EmptyClipboard()
        handle = k32.GlobalAlloc(0x0002, len(data))  # GMEM_MOVEABLE
        ptr = k32.GlobalLock(handle) if handle else None
        if not ptr:  # out of memory: writing through NULL would kill the app
            if handle:
                k32.GlobalFree(handle)
            return False
        ctypes.memmove(ptr, data, len(data))
        k32.GlobalUnlock(handle)
        if not u32.SetClipboardData(13, handle):  # CF_UNICODETEXT
            k32.GlobalFree(handle)
            return False
        return True
    finally:
        u32.CloseClipboard()


class Api:
    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or Settings.load()
        self._lock = threading.RLock()
        self._items: dict[str, _Item] = {}  # insertion order = list order
        self._next_id = 1
        self._busy = False
        self._current: str | None = None  # id of the file converting now
        self._toast = ""
        self._toast_until = 0.0
        self._window = None
        self._engine_obj = None
        self._engine_lock = threading.Lock()
        self._md = None
        self._last_push = 0.0
        self._push_timer: threading.Timer | None = None
        self._seq = 0  # snapshot number: the page drops one older than it has
        self._saving = False  # a save is under way (from its dialog on)
        self._save_note = ""  # what the save is doing now, for the status line
        self.probe_path: str | None = None  # set by run() in probe mode

    # -- wiring (called from run(), not the page) ----------------------------
    def _attach(self, window) -> None:
        self._window = window
        errors.add_listener(lambda incident: self._flash(f"⚠ {incident.title} ({incident.code}, ref {incident.ref})"))
        # markitdown takes seconds to import; do it while the window paints.
        threading.Thread(target=self._engine, daemon=True, name="engine-warmup").start()

    def _engine(self):
        with self._engine_lock:
            if self._engine_obj is None:
                from ..converter import ConversionEngine

                s = self._settings
                self._engine_obj = ConversionEngine(
                    enable_ocr=s.enable_ocr,
                    ocr_device=s.ocr_device,
                    enable_audio=s.enable_audio,
                    whisper_model=s.whisper_model,
                    mineru_endpoint=s.mineru_endpoint,
                    page_anchors=s.page_anchors,
                    pdf_layout=s.pdf_layout,
                    figure_markers=s.extract_images,
                )
            return self._engine_obj

    def _renderer(self):
        if self._md is None:
            from markdown_it import MarkdownIt

            # html=False: a converted document's raw HTML shows as text, so a
            # file can't inject script or markup into the window.
            md = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])

            def link_open(renderer, tokens, idx, options, env):
                if not _WEB_LINK.match(tokens[idx].attrGet("href") or ""):
                    tokens[idx].attrs.pop("href", None)  # shown as text, goes nowhere
                return renderer.renderToken(tokens, idx, options, env)

            md.add_render_rule("link_open", link_open)
            self._md = md
        return self._md

    def _on_drop(self, event: dict) -> None:
        """DOM drop handler (pywebview fills in each file's full path)."""
        files = (event.get("dataTransfer") or {}).get("files") or []
        paths = [f.get("pywebviewFullPath") for f in files if f.get("pywebviewFullPath")]
        if self.probe_path:
            self._probe_note("drop", {"names": [f.get("name") for f in files], "paths": paths})
        self._add(paths)

    # -- state snapshots -----------------------------------------------------
    def _status(self, item: _Item) -> str:
        r = item.result
        if r is None:
            return "converting" if item.id == self._current else "queued"
        if not r.ok:
            return "error"
        return "warn" if r.warnings else "done"

    def _row(self, item: _Item) -> dict:
        status = self._status(item)
        r = item.result
        ext = item.path.suffix.lstrip(".").upper() or "File"
        meta = f"{ext} · {_fmt_size(item.size)}"
        if status == "converting":
            meta = item.sub or "Converting…"
        return {
            "id": item.id,
            "name": item.path.name,
            "kind": _kind(item.path),
            "meta": meta,
            "status": status,
            "engine": r.engine if r is not None and r.ok else "",
            "errCode": (r.error_code or "MS-199") if r is not None and not r.ok else "",
            "pct": round(item.progress * 100),
        }

    def _snapshot(self) -> dict:
        with self._lock:
            items = list(self._items.values())
            rows = [self._row(i) for i in items]
            done = [i for i in items if i.result is not None and i.result.ok]
            failed = [i for i in items if i.result is not None and not i.result.ok]
            pending = [i for i in items if i.result is None]
            # The file converting now, if it is still in the list (Remove or
            # Clear all doesn't stop the file already under way).
            cur = self._items.get(self._current) if self._current else None
            active = bool(pending) or cur is not None
            if self._toast and time.monotonic() < self._toast_until:
                status = self._toast
            elif self._save_note:
                status = self._save_note
            elif active:
                status = f"Converting… {len(items) - len(pending) + (1 if cur else 0)}/{len(items)}"
                if cur is not None:
                    status += f"  —  {cur.path.name}" + (f" · {cur.sub}" if cur.sub else "")
            elif items:
                ocr = sum(1 for i in done if i.result.engine.startswith("ocr"))
                heard = sum(1 for i in done if i.result.engine == "whisper")
                extra = [x for x in (ocr and f"{ocr} via OCR", heard and f"{heard} transcribed") if x]
                status = f"Finished — {len(done)} converted" + (f" ({', '.join(extra)})" if extra else "")
                status += f", {len(failed)} failed." if failed else "."
            else:
                status = "Ready — drop files anywhere to convert."
            cur_progress = cur.progress if cur is not None else 0.0
            overall = (len(items) - len(pending) + cur_progress) / len(items) if items else 0.0
            self._seq += 1
            return {
                "seq": self._seq,
                "files": rows,
                "busy": active,
                "status": status,
                "overall": round(overall * 100, 1),
                "canSave": bool(done) and not self._saving,
                # A fix button can turn OCR on, so the switch follows Python.
                "ocr": self._settings.enable_ocr,
            }

    def _push(self, force: bool = False) -> None:
        """Send the page a fresh snapshot, coalescing bursts."""
        if self._window is None:
            return
        now = time.monotonic()
        with self._lock:
            if not force and now - self._last_push < _PUSH_INTERVAL_S:
                if self._push_timer is None:
                    self._push_timer = threading.Timer(_PUSH_INTERVAL_S, self._push, kwargs={"force": True})
                    self._push_timer.daemon = True
                    self._push_timer.start()
                return
            if self._push_timer is not None:
                self._push_timer.cancel()
                self._push_timer = None
            self._last_push = now
        try:
            self._window.run_js(f"window.app && app.receive({json.dumps(self._snapshot())})")
        except Exception:  # window closing
            pass

    def _flash(self, message: str, seconds: float = 2.8) -> None:
        with self._lock:
            self._toast = message
            self._toast_until = time.monotonic() + seconds
        self._push(force=True)
        clear = threading.Timer(seconds + 0.05, self._push, kwargs={"force": True})
        clear.daemon = True  # never keeps a closed window's process alive
        clear.start()

    # -- adding and converting -----------------------------------------------
    def _add(self, raw_paths) -> None:
        added, folders = 0, []
        with self._lock:
            known = {i.path for i in self._items.values()}
            for raw in raw_paths:
                path = Path(raw)
                if path.is_dir():
                    folders.append(path.name)
                    continue
                if not path.is_file() or path in known:
                    continue
                item = _Item(f"f{self._next_id}", path, path.stat().st_size)
                self._next_id += 1
                self._items[item.id] = item
                known.add(path)
                added += 1
        # One message: a second flash would replace the folder note at once.
        notes = [f"Added {added} file(s)."] if added else []
        if folders:
            incident = errors.report("MS-104", where="add files", detail=", ".join(folders[:10]))
            notes.append(f"Skipped {len(folders)} folder(s) — {incident.next_step} ({incident.code})")
        if notes:
            self._flash(" ".join(notes), 6 if folders else 2.8)
        if added:
            debuglog.event("ui.action", action="add_files", count=added, front_end="webui")
            self._convert_pending()

    def _convert_pending(self) -> None:
        with self._lock:
            if self._busy:
                return  # the running batch picks these up when it finishes
            targets = [i for i in self._items.values() if i.result is None]
            if not targets:
                return
            self._busy = True
            self._current = targets[0].id
            clean_flag = self._settings.clean_output
        threading.Thread(
            target=self._worker, args=(targets, clean_flag), daemon=True, name="convert-worker"
        ).start()
        self._push(force=True)

    def _worker(self, targets: list[_Item], clean_flag: bool) -> None:
        from ..cleanup import clean_markdown
        from ..quality import assess_markdown

        by_path = {t.path: t for t in targets}

        def on_progress(index: int, total: int, result) -> None:
            item = targets[index - 1]  # convert_many reports targets in order, 1-based
            with self._lock:
                live = self._items.get(item.id) is item
                if live:
                    item.result, item.progress, item.sub = result, 1.0, ""
                    item.evict()
                    item.preparing = bool(result.ok and result.markdown)
                nxt = targets[index].id if index < len(targets) else None
                self._current = nxt
            self._push(force=True)
            if not (live and result.ok and result.markdown):
                return
            # Precompute what the first click needs, so selecting a big book
            # never waits on cleanup.
            try:
                with debuglog.file_context(result.source):
                    if clean_flag:
                        cleaned, stats = clean_markdown(result.markdown, engine=result.engine)
                        report = assess_markdown(cleaned)
                    else:
                        cleaned, stats, report = None, None, assess_markdown(result.markdown)
            except Exception as exc:  # an optimisation, never fatal
                # Logged only: the preview redoes this and reports a real failure.
                errors.report("MS-901", exc=exc, where="cleanup precompute", file=str(result.source))
                cleaned = report = None
            with self._lock:
                if item.result is result:
                    if cleaned is not None:
                        item.cleaned, item.clean_stats = cleaned, stats
                    if report is not None:
                        item.quality[clean_flag] = report
                    item.preparing = False
            self._run_js(f"app.previewReady({json.dumps(item.id)})")

        def on_subprogress(source: Path, current: float, total: float, unit: str) -> None:
            item = by_path.get(Path(source))
            if item is None or not total:
                return
            with self._lock:
                item.progress = max(0.0, min(1.0, current / total))
                item.sub = (
                    f"Transcribing — {_fmt_mmss(current)}/{_fmt_mmss(total)}"
                    if unit == "sec"
                    else f"OCR — page {int(current)}/{int(total)}"
                )
            self._push()

        try:
            engine = self._engine()
            with self._lock:
                engine.enable_ocr = self._settings.enable_ocr
            engine.convert_many([t.path for t in targets], on_progress=on_progress, on_subprogress=on_subprogress)
        except BaseException as exc:  # noqa: BLE001 - the batch must always end
            incident = errors.report("MS-902", exc=exc, where="conversion worker")
            from ..converter import ConversionResult

            with self._lock:
                for t in targets:
                    if t.result is None:
                        t.result = ConversionResult(
                            source=t.path,
                            error="Conversion stopped before this file was reached.",
                            error_code=incident.code,
                            error_ref=incident.ref,
                        )
            self._flash(f"⚠ {incident.title} ({incident.code})", 6)
        finally:
            with self._lock:
                self._busy = False
                self._current = None
            self._push(force=True)
            self._convert_pending()  # files dropped meanwhile

    def _run_js(self, script: str) -> None:
        if self._window is not None:
            try:
                self._window.run_js(f"window.app && {script}")
            except Exception:
                pass

    # -- preview -------------------------------------------------------------
    def _text_and_report(self, item: _Item, result, clean: bool):
        """(text, quality report, cleanup stats) for ``result``, the item's
        result as the caller saw it under the lock — a Retry may replace
        ``item.result`` at any moment, so the item's caches are used only
        while they still belong to it."""
        from ..cleanup import clean_markdown
        from ..quality import assess_markdown

        with self._lock:
            current = item.result is result
            cleaned, stats = (item.cleaned, item.clean_stats) if current else (None, None)
            report = item.quality.get(clean) if current else None
        if not clean:
            text, stats = result.markdown, None
        elif cleaned is None:
            text, stats = clean_markdown(result.markdown, engine=result.engine)
            with self._lock:
                if item.result is result:
                    item.cleaned, item.clean_stats = text, stats
        else:
            text = cleaned
        if report is None:
            report = assess_markdown(text)
            with self._lock:
                if item.result is result:
                    item.quality[clean] = report
        return text, report, stats

    def _incident_for(self, result) -> errors.Incident:
        code = result.error_code or errors.classify_conversion(result.error or "")
        return errors.Incident(
            errors.CATALOG.get(code, errors.CATALOG["MS-199"]),
            result.error_ref or "—",
            result.error or "",
            where=f"convert {result.source.name}",
            context={"file": str(result.source)},
            log_path=debuglog.error_log_path(),
        )

    @staticmethod
    def _incident_dict(incident: errors.Incident) -> dict:
        return {
            "code": incident.code,
            "title": incident.title,
            "next": incident.next_step,
            "ref": incident.ref,
            "detail": incident.detail,
            "action": incident.action if incident.action in errors.ACTIONS else "",
            "actionLabel": errors.ACTIONS.get(incident.action, "").replace("↻  ", ""),
        }

    # -- the page's API ------------------------------------------------------
    def ready(self) -> dict:
        """First call from the page: everything static plus a snapshot."""
        s = self._settings
        if self.probe_path:
            self._probe_note("ready", {"t": time.time()})
        return {
            "app": __app_name__,
            "version": __version__,
            "clean": s.clean_output,
            "rendered": s.rendered_preview,
            "exportStyle": s.export_style,
            "aiTarget": s.ai_target,
            "styles": [{"id": k, "label": label, "desc": desc} for k, label, desc in _STYLES],
            "targets": list(AI_TARGETS),
            "formats": [{"cat": c, "list": f} for c, f in _FORMATS],
            "probe": bool(self.probe_path),
            **self._snapshot(),
        }

    def pick_files(self) -> None:
        import webview

        from ..converter import SUPPORTED_EXTENSIONS

        patterns = ";".join(f"*{ext}" for ext in SUPPORTED_EXTENSIONS + MEDIA_EXTENSIONS)
        with debuglog.ui_blocking("open dialog"):
            paths = self._window.create_file_dialog(
                webview.FileDialog.OPEN,
                allow_multiple=True,
                file_types=(f"Supported files ({patterns})", "All files (*.*)"),
            )
        if paths:
            self._add(paths)

    def preview(self, item_id: str, view: str = "rendered") -> dict:
        with self._lock:
            item = self._items.get(item_id)
            if item is None:
                return {"state": "none"}
            result = item.result
            status = self._status(item)
            if result is None:
                return {"state": status, "name": item.path.name, "sub": item.sub, "pct": round(item.progress * 100)}
            if item.preparing:
                return {"state": "preparing", "name": item.path.name}  # previewReady follows
        if not result.ok:
            return {"state": "error", "name": item.path.name, **self._incident_dict(self._incident_for(result)),
                    "details": self._incident_for(result).details_text()}
        clean = self._settings.clean_output
        started = time.perf_counter()
        try:
            text, report, stats = self._text_and_report(item, result, clean)
        except Exception as exc:
            incident = errors.report("MS-903", exc=exc, where="preview", file=str(item.path))
            return {"state": "error", "name": item.path.name, **self._incident_dict(incident),
                    "details": incident.details_text()}
        text = text or "(Empty output.)"
        display, truncated = text, False
        if len(display) > PREVIEW_MAX_CHARS:
            cut = display.rfind("\n", 0, PREVIEW_MAX_CHARS)
            display, truncated = display[: cut if cut > 0 else PREVIEW_MAX_CHARS], True
        payload = {
            "state": "done",
            "name": item.path.name,
            "engine": result.engine,
            "truncated": truncated,
            "limitK": PREVIEW_MAX_CHARS // 1000,
            "warning": None,
            "stats": [
                {"label": "Quality", "value": f"{report.score}/100",
                 "note": report.issues[0] if report.issues else "No issues found",
                 "alert": bool(report.binary_noise)},
                {"label": "Tokens", "value": f"~{report.est_tokens:,}", "note": "estimated"},
                {"label": "Headings", "value": f"{report.headings:,}", "note": "in the document"},
                {"label": "Cleanup", "value": f"{stats.total_fixes:,}" if clean and stats else "Off",
                 "note": "fixes applied" if clean else "raw conversion"},
            ],
        }
        if result.warnings:
            w = self._incident_dict(result.warnings[0])
            w["more"] = len(result.warnings) - 1
            payload["warning"] = w
        if view == "raw":
            payload["raw"] = display
        else:
            try:
                payload["html"] = self._renderer().render(display)
            except Exception as exc:  # show the raw text rather than nothing
                errors.report("MS-903", exc=exc, where="preview render", file=str(item.path))
                payload["raw"] = display
        debuglog.event("ui.preview", file=item.path.name, ms=round((time.perf_counter() - started) * 1000),
                       chars=len(display), view=view, front_end="webui")
        return payload

    def set_option(self, key: str, value) -> dict:
        """Toggles and pickers the page owns; each is persisted."""
        s = self._settings
        if key == "clean":
            s.clean_output = bool(value)
        elif key == "rendered":
            s.rendered_preview = bool(value)
        elif key == "ocr":
            s.enable_ocr = bool(value)
            if self._engine_obj is not None:
                self._engine_obj.enable_ocr = s.enable_ocr
        elif key == "exportStyle" and value in {k for k, _, _ in _STYLES}:
            s.export_style = value
        elif key == "aiTarget" and value in AI_TARGETS:
            s.ai_target = value
        else:
            return {"ok": False}
        try:
            s.save()
        except Exception as exc:
            self._flash(f"⚠ {errors.report('MS-502', exc=exc, where='settings save').title}", 5)
        if key == "ocr":
            self._push(force=True)  # the switch redraws from the snapshot
        return {"ok": True}

    def remove(self, item_id: str) -> None:
        with self._lock:
            self._items.pop(item_id, None)
        debuglog.event("ui.action", action="remove", front_end="webui")
        self._push(force=True)

    def clear(self) -> bool:
        with self._lock:
            converted = sum(1 for i in self._items.values() if i.result is not None and i.result.ok)
        if converted and not self._window.create_confirmation_dialog(
            __app_name__,
            f"Discard {converted} converted result(s)?\n\nYour source files aren't touched, "
            "but re-adding them means converting from scratch.",
        ):
            return False
        with self._lock:
            self._items.clear()
        debuglog.event("ui.action", action="clear", front_end="webui")
        self._push(force=True)
        return True

    def retry(self, item_id: str) -> None:
        self._retry_where((), also=item_id)

    def _retry_where(self, codes: tuple[str, ...], also: str | None = None) -> None:
        with self._lock:
            for item in self._items.values():
                r = item.result
                hit = r is not None and (
                    (codes and r.error_code in codes) or any(w.code in codes for w in r.warnings)
                )
                if hit or (item.id == also and r is not None):
                    item.result, item.progress, item.sub = None, 0.0, ""
                    item.evict()
        self._push(force=True)
        self._convert_pending()

    def fix(self, action: str, item_id: str = "") -> None:
        """A one-click next step from an error page or warning bar."""
        s = self._settings
        debuglog.event("ui.fix", action=action, front_end="webui")
        if action == "retry":
            self.retry(item_id)
        elif action == "retry_failed":
            with self._lock:
                failed = [i.id for i in self._items.values() if i.result is not None and not i.result.ok]
            for fid in failed:
                self.retry(fid)
        elif action in ("enable_ocr", "enable_audio", "ocr_cpu"):
            if action == "enable_ocr":
                s.enable_ocr, codes = True, ("MS-110",)
            elif action == "enable_audio":
                s.enable_audio, codes = True, ("MS-115",)
            else:
                s.ocr_device, codes = "cpu", ("MS-113", "MS-121", "MS-122")
            engine = self._engine()
            engine.enable_ocr, engine.enable_audio, engine.ocr_device = s.enable_ocr, s.enable_audio, s.ocr_device
            try:
                s.save()
            except Exception as exc:
                errors.report("MS-502", exc=exc, where="settings save")
            self._retry_where(codes, also=item_id)
        elif action == "remove":
            self.remove(item_id)
        elif action == "open_logs":
            import os

            folder = debuglog.log_dir()
            folder.mkdir(parents=True, exist_ok=True)
            os.startfile(folder)  # noqa: S606 - the app's own log folder
        else:
            self._flash("That step isn't in the new window yet — use the classic window for it.", 4)

    def copy(self, item_id: str) -> bool:
        from ..figures import strip_figure_markers

        with self._lock:
            item = self._items.get(item_id)
            result = item.result if item is not None else None
        if result is None or not result.ok:
            return False
        text, _, _ = self._text_and_report(item, result, self._settings.clean_output)
        if not _set_clipboard(strip_figure_markers(text)):
            self._flash(f"⚠ {errors.report('MS-601', where='clipboard').title}", 5)
            return False
        debuglog.event("ui.action", action="copy", chars=len(text), front_end="webui")
        self._flash("Markdown copied to clipboard.")
        return True

    def copy_text(self, text: str) -> bool:
        """Copy a short string the page already has (error details)."""
        return _set_clipboard(text)

    def save(self) -> None:
        """Save Markdown…, by the classic window's rules: one converted file
        in the One file style gets a Save dialog, anything else a folder.
        Every file goes through export.save, like every other front-end."""
        import webview

        with self._lock:
            if self._saving:
                return
            done = [(i, i.result) for i in self._items.values() if i.result is not None and i.result.ok]
            if done:
                self._saving = True
        if not done:
            self._flash("Nothing to save yet — drop in a file and it converts automatically.", 4)
            return
        s = self._settings
        style, target = s.export_style, s.ai_target
        single = len(done) == 1 and style == "single"
        debuglog.event("ui.action", action="save", files=len(done), style=style, target=target, front_end="webui")
        try:
            self._push(force=True)  # the Save button greys out
            with debuglog.ui_blocking("save dialog"):
                if single:
                    picked = self._window.create_file_dialog(
                        webview.FileDialog.SAVE,
                        directory=s.default_output_dir,
                        save_filename=f"{done[0][0].path.stem}.md",
                        file_types=("Markdown (*.md)", "All files (*.*)"),
                    )
                else:
                    picked = self._window.create_file_dialog(webview.FileDialog.FOLDER, directory=s.default_output_dir)
            if picked:
                self._save_to(Path(picked[0]), done, style, target, single)
        finally:
            with self._lock:
                self._saving, self._save_note = False, ""
            self._push(force=True)

    def _save_to(self, picked: Path, done: list, style: str, target: str, single: bool) -> None:
        saved, used, notes = 0, set(), []
        failed: list[tuple[str, errors.Incident]] = []
        for n, (item, result) in enumerate(done, start=1):
            if single:
                dest = picked
            else:  # same-named sources from different folders never collide
                name = export.claim_name(item.path.stem, used)
                dest = picked / name if style != "single" else picked / f"{name}.md"
            with self._lock:
                self._save_note = f"Saving {n}/{len(done)}  —  {item.path.name}"
            self._push(force=True)
            try:
                with debuglog.file_context(item.path):
                    saved += self._save_one(item, result, dest, style, target, notes)
            except Exception as exc:  # one bad file must not lose the rest
                code = errors.classify_os_error(exc) if isinstance(exc, OSError) else "MS-299"
                failed.append((item.path.name, errors.report(code, exc=exc, where="save", target=str(dest))))
        if failed:
            first = failed[0][1]
            names = ", ".join(name for name, _ in failed[:5]) + (" …" if len(failed) > 5 else "")
            self._flash(f"⚠ Saved {saved} file(s); {len(failed)} couldn't be saved ({names}): "
                        f"{first.title} {first.next_step} ({first.code}, ref {first.ref})", 12)
            return
        tail = "".join(f"  ⚠ {note}" for note in notes)
        if single:
            self._flash(f"Saved {picked.name}.{tail}", 8 if notes else 3)
            return
        self._flash(f"Saved {saved} file(s) to {picked}.{tail}", 8 if notes else 4)
        if self._window.create_confirmation_dialog(__app_name__, f"Saved {saved} file(s) to:\n{picked}\n\nOpen the folder?"):
            import os

            os.startfile(picked)  # noqa: S606 - the folder the user just chose

    def _save_one(self, item: _Item, result, dest: Path, style: str, target: str, notes: list[str]) -> int:
        """Write one source's output; returns the files written. Figures and
        the summary are this front-end's part (ADR-002); a failure in either
        is a note, never a lost save."""
        from .. import figures

        s = self._settings
        text, _, _ = self._text_and_report(item, result, s.clean_output)
        if s.extract_images and item.path.suffix.lower() == ".pdf":
            images_dir, rel_dir = export.figure_dirs(dest, style)
            try:
                figs = figures.extract_pdf_figures(item.path, images_dir)
            except Exception as exc:  # the Markdown is still worth saving
                figs = []
                incident = errors.report("MS-401", exc=exc, where="figures", file=str(item.path))
                notes.append(f"{item.path.name}: {incident.title} ({incident.code})")
            text = figures.insert_figure_links(text, figs, rel_dir)
        else:
            text = figures.strip_figure_markers(text)
        summary = ""
        # A book folder always has room for a summary (index + manifest).
        if (style != "single" or s.export_front_matter) and s.ollama_endpoint and s.summary_model:
            from .. import polish

            with self._lock:
                self._save_note += f" · summarizing with {s.summary_model}…"
            self._push(force=True)
            polish.take_last_failure()  # only this call's failure counts
            try:
                summary = polish.summarize_markdown(text, s.ollama_endpoint, s.summary_model,
                                                    title=result.doc_title) or ""
                failure = polish.take_last_failure()
            except Exception as exc:
                failure = errors.report("MS-399", exc=exc, where="summary", model=s.summary_model)
            if not summary and failure is not None:
                notes.append(f"No summary for {item.path.name}: {failure.title} ({failure.code})")
        res = export.save(text, dest, source=item.path.name, style=style, ai_target=target,
                          details=result.details, front_matter=s.export_front_matter, summary=summary)
        return len(res.paths)

    def help(self) -> list[dict]:
        """The user guide, one entry per ``##`` section, rendered."""
        guide = load_user_guide()
        parts = re.split(r"(?m)^## +", guide)
        sections = [{"title": "Overview", "html": self._renderer().render(re.sub(r"(?m)^# .*\n", "", parts[0]))}]
        for part in parts[1:]:
            title, _, body = part.partition("\n")
            sections.append({"title": title.strip(), "html": self._renderer().render(body)})
        return [s for s in sections if s["html"].strip()]

    def open_url(self, url: str) -> None:
        """Links open in the browser — the window itself never navigates."""
        if re.match(r"(?i)https?://", url or ""):
            webbrowser.open(url)

    def kofi(self) -> None:
        self.open_url(KOFI_URL)
        self._flash("Thank you for supporting Markdown Sidekick!")

    def not_yet(self, what: str) -> None:
        self._flash(f"{what} isn't in the new window yet — the classic window still has it.", 4)

    # -- probe mode (spike measurements) -------------------------------------
    def _probe_note(self, key: str, value) -> None:
        try:
            p = Path(self.probe_path)
            data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
            data[key] = value
            p.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _probe_add(self, paths: list[str]) -> None:
        self._add(paths)

    def probe_result(self, key: str, value) -> None:
        if self.probe_path:
            self._probe_note(key, value)

    def probe_done(self) -> None:
        if self.probe_path and self._window is not None:
            self._window.destroy()
