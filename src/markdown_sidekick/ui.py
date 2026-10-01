"""Tkinter GUI for Markdown Sidekick."""

from __future__ import annotations

import os
import queue
import threading
import time
import traceback
import webbrowser
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import __app_name__, __version__, debuglog, errors
from . import settings as settings_store
from .cleanup import clean_markdown
from .converter import (
    SUPPORTED_EXTENSIONS,
    ConversionEngine,
    ConversionResult,
    default_output_path,
)
from . import export as md_export
from .errorview import NoticeBar, show_error_dialog
from .guide import KOFI_URL, build_mcp_setup_prompt, load_user_guide
from .mdrender import MarkdownRenderer
from .ocr import ocr_available
from .quality import assess_markdown
from .settings import WHISPER_MODELS, Settings

# Optional drag-and-drop support. The app degrades gracefully without it.
try:
    from tkinterdnd2 import DND_FILES, TkinterDnD

    _DND_AVAILABLE = True
except ImportError:  # pragma: no cover
    DND_FILES = None
    TkinterDnD = None
    _DND_AVAILABLE = False


# ---- palette ---------------------------------------------------------------
BG = "#1e1f29"
BG_PANEL = "#262833"
BG_INPUT = "#2f3140"
FG = "#e6e6ec"
FG_MUTED = "#9aa0b4"
ACCENT = "#6c8cff"  # bright accent: progress bar and other non-text uses
# Fills that carry white text need >= 4.5:1 (WCAG AA); the bright accent is
# only 3.07:1, so buttons and selected rows use this darker pair instead.
ACCENT_FILL = "#4a63d8"  # 5.16:1 with white text
ACCENT_HOVER = "#5068e2"  # 4.75:1 with white text
ACCENT_LINK = "#7d9aff"  # links on the input bg (4.86:1; #6c8cff was 4.18)
OK_COLOR = "#5ad19a"
ERR_COLOR = "#ff6b81"
WARN_COLOR = "#f0c05a"
NOTICE_BG = "#3b3423"  # amber-tinted panel for the notice bar (FG on it: 10.9:1)
FONT = ("Segoe UI", 10)
FONT_BOLD = ("Segoe UI", 10, "bold")
FONT_TITLE = ("Segoe UI Semibold", 16)
FONT_MONO = ("Cascadia Code", 10) if True else ("Consolas", 10)

# Rendering a multi-MB document into the Tk Text widget costs ~250ms/MB on
# messy real-world text; cap what the PREVIEW shows so clicking a huge file
# stays instant. Copy/Save always use the full text.
PREVIEW_MAX_CHARS = 400_000


def _fmt_mmss(seconds: float) -> str:
    seconds = int(max(0, seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def _root_class():
    return TkinterDnD.Tk if _DND_AVAILABLE else tk.Tk


class MarkdownSidekickApp(_root_class()):  # type: ignore[misc]
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{__app_name__}  ·  v{__version__}")
        self.geometry("1080x680")
        self.minsize(840, 540)
        self.configure(bg=BG)

        # Persisted user settings drive the engine + UI defaults.
        self.settings = Settings.load()
        # Every error is logged with a code (errors.py); uncaught ones too.
        errors.install_crash_hooks()
        errors.add_listener(lambda incident: self._events.put(("incident", incident)))
        if self.settings.debug_mode or debuglog.debug_requested():
            debuglog.enable("gui", snapshots=self.settings.debug_snapshots)
        self._last_dialog: dict[str, float] = {}
        self._dialog: tk.Toplevel | None = None
        self.engine = ConversionEngine(
            enable_ocr=self.settings.enable_ocr,
            ocr_device=self.settings.ocr_device,
            enable_audio=self.settings.enable_audio,
            whisper_model=self.settings.whisper_model,
            mineru_endpoint=self.settings.mineru_endpoint,
            page_anchors=self.settings.page_anchors,
            pdf_layout=self.settings.pdf_layout,
            figure_markers=self.settings.extract_images,
        )
        # Ordered mapping of source path -> result (None until converted).
        self.files: dict[Path, ConversionResult | None] = {}
        self._events: "queue.Queue[tuple]" = queue.Queue()
        self._busy = False
        # Cleaned-output cache + the text currently shown (for copy/save).
        self._clean_cache: dict[Path, str] = {}
        self._clean_stats: dict[Path, object] = {}
        # QualityReport per (path, clean-toggle) — assessing a 500-page book
        # costs ~0.2s, too slow to redo on every list click.
        self._quality_cache: dict[tuple[Path, bool], object] = {}
        self._current_export_text = ""
        # OCR toggle reflects the saved setting (and dep availability).
        self.ocr_var = tk.BooleanVar(value=self.settings.enable_ocr and ocr_available())
        self._help_win: tk.Toplevel | None = None

        self._build_style()
        self._build_layout()
        # Keyboard path — mirrors the buttons. Handlers re-check _busy
        # because key bindings bypass disabled-widget protection. Each accel
        # binds BOTH letter cases: Tk keysyms follow the effective case, so
        # CapsLock turns "o" into "O" and a single-case binding goes dead.
        # Copy requires an explicit Shift so plain Ctrl+C (the Text widget's
        # own selection copy) can never trigger the whole-document copy.
        for seq in ("<Control-o>", "<Control-O>"):
            self.bind(seq, lambda _e: self.add_files())
        for seq in ("<Control-s>", "<Control-S>"):
            self.bind(seq, lambda _e: self.save_markdown())
        for seq in ("<Control-Shift-c>", "<Control-Shift-C>"):
            self.bind(seq, lambda _e: self.copy_preview())
        self.tree.bind("<Delete>", lambda _e: self.remove_selected())
        # A failing Tk callback gets a code, a log entry and a dialog instead
        # of a traceback on a console nobody sees.
        self.report_callback_exception = self._on_tk_error
        self._update_title()
        if debuglog.enabled():
            debuglog.event(
                "ui.start",
                tk=str(self.tk.call("info", "patchlevel")),
                scaling=float(self.tk.call("tk", "scaling")),
                screen=[self.winfo_screenwidth(), self.winfo_screenheight()],
                dnd=_DND_AVAILABLE,
                ocr_available=ocr_available(),
            )
            self.status_var.set(f"Debug mode is on — tracing to {debuglog.session_dir()}")
        if settings_store.LOAD_INCIDENT is not None:
            self.after(300, lambda: self._show_notice(settings_store.LOAD_INCIDENT))
        self._poll_events()

    # -- styling -------------------------------------------------------------
    def _build_style(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")

        style.configure("TFrame", background=BG)
        style.configure("Panel.TFrame", background=BG_PANEL)
        style.configure("TLabel", background=BG, foreground=FG, font=FONT)
        style.configure("Panel.TLabel", background=BG_PANEL, foreground=FG, font=FONT)
        style.configure("Muted.TLabel", background=BG, foreground=FG_MUTED, font=FONT)
        style.configure(
            "PanelMuted.TLabel", background=BG_PANEL, foreground=FG_MUTED, font=FONT
        )
        style.configure("Title.TLabel", background=BG, foreground=FG, font=FONT_TITLE)

        style.configure(
            "Accent.TButton",
            background=ACCENT_FILL,
            foreground="#ffffff",
            font=FONT_BOLD,
            borderwidth=0,
            focuscolor="#ffffff",
            padding=(14, 8),
        )
        style.map(
            "Accent.TButton",
            background=[("active", ACCENT_HOVER), ("disabled", "#3a3c4a")],
            foreground=[("disabled", FG_MUTED)],
        )

        style.configure(
            "TButton",
            background=BG_INPUT,
            foreground=FG,
            font=FONT,
            borderwidth=0,
            focuscolor=ACCENT,
            padding=(10, 6),
        )
        style.map(
            "TButton",
            background=[("active", "#3a3c4a"), ("disabled", "#262833")],
            foreground=[("disabled", FG_MUTED)],
        )

        style.configure(
            "Treeview",
            background=BG_INPUT,
            fieldbackground=BG_INPUT,
            foreground=FG,
            borderwidth=0,
            rowheight=26,
            font=FONT,
        )
        style.map("Treeview", background=[("selected", ACCENT_FILL)], foreground=[("selected", "#fff")])
        style.configure(
            "Treeview.Heading",
            background=BG_PANEL,
            foreground=FG_MUTED,
            font=FONT_BOLD,
            borderwidth=0,
        )
        style.configure(
            "Horizontal.TProgressbar",
            background=ACCENT,
            troughcolor=BG_INPUT,
            borderwidth=0,
            thickness=6,
        )
        style.configure("TNotebook", background=BG_PANEL, borderwidth=0)
        style.configure(
            "TNotebook.Tab",
            background=BG_INPUT,
            foreground=FG_MUTED,
            padding=(16, 7),
            font=FONT,
            borderwidth=0,
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", BG_PANEL)],
            foreground=[("selected", FG)],
        )
        style.configure("TCheckbutton", background=BG, foreground=FG, font=FONT)
        style.map(
            "TCheckbutton",
            background=[("active", BG)],
            foreground=[("active", FG)],
        )
        style.configure(
            "Panel.TCheckbutton", background=BG_PANEL, foreground=FG_MUTED, font=FONT
        )
        style.map(
            "Panel.TCheckbutton",
            background=[("active", BG_PANEL)],
            foreground=[("active", FG)],
            indicatorcolor=[("selected", ACCENT), ("!selected", BG_INPUT)],
        )
        style.configure(
            "TEntry",
            fieldbackground=BG_INPUT,
            foreground=FG,
            insertcolor=FG,
            borderwidth=0,
            padding=4,
        )
        style.configure(
            "TCombobox",
            fieldbackground=BG_INPUT,
            background=BG_INPUT,
            foreground=FG,
            arrowcolor=FG,
            borderwidth=0,
            padding=4,
        )
        style.map("TCombobox", fieldbackground=[("readonly", BG_INPUT)])
        style.configure("Notice.TFrame", background=NOTICE_BG)
        style.configure("Notice.TLabel", background=NOTICE_BG, foreground=FG, font=FONT)
        style.configure(
            "Notice.TButton", background="#4d4430", foreground=FG, font=FONT, padding=(8, 3)
        )
        style.map("Notice.TButton", background=[("active", "#5c5139")])

    # -- layout --------------------------------------------------------------
    def _build_layout(self) -> None:
        # Header
        header = ttk.Frame(self, style="TFrame")
        header.pack(fill="x", padx=20, pady=(16, 8))
        ttk.Label(header, text="Markdown Sidekick", style="Title.TLabel").pack(side="left")
        # Right-side buttons pack FIRST so a long subtitle can never push them
        # out of the window; the subtitle then takes whatever space remains.
        ttk.Button(header, text="⚙  Settings", command=self.open_settings).pack(
            side="right", pady=(2, 0)
        )
        ttk.Button(header, text="☕  Support", command=self.open_kofi).pack(
            side="right", padx=(0, 8), pady=(2, 0)
        )
        ttk.Button(header, text="❓  Help", command=self.open_help).pack(
            side="right", padx=(0, 8), pady=(2, 0)
        )
        ttk.Label(
            header,
            text="Drop files in — get clean Markdown out",
            style="Muted.TLabel",
        ).pack(side="left", padx=(14, 0), pady=(6, 0))

        # Footer is packed at the BOTTOM before the body so it always reserves its
        # space; otherwise the expanding body pushes it off the bottom edge.
        self._build_footer()

        # Body: left file panel | right preview — fills the space that's left.
        body = ttk.Frame(self, style="TFrame")
        body.pack(side="top", fill="both", expand=True, padx=20, pady=(4, 8))
        body.columnconfigure(0, weight=0, minsize=360)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        self._build_file_panel(body)
        self._build_preview_panel(body)

    def _build_file_panel(self, parent: ttk.Frame) -> None:
        panel = ttk.Frame(parent, style="Panel.TFrame", padding=12)
        panel.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        panel.rowconfigure(2, weight=1)
        panel.columnconfigure(0, weight=1)

        ttk.Label(panel, text="Files", style="Panel.TLabel", font=FONT_BOLD).grid(
            row=0, column=0, sticky="w"
        )

        btns = ttk.Frame(panel, style="Panel.TFrame")
        btns.grid(row=1, column=0, sticky="ew", pady=(8, 8))
        self.add_btn = ttk.Button(btns, text="Add files…", command=self.add_files)
        self.add_btn.pack(side="left")
        self.remove_btn = ttk.Button(btns, text="Remove", command=self.remove_selected)
        self.remove_btn.pack(side="left", padx=6)
        self.clear_btn = ttk.Button(btns, text="Clear", command=self.clear_files)
        self.clear_btn.pack(side="left")

        tree = ttk.Treeview(
            panel, columns=("status",), show="tree headings", selectmode="browse"
        )
        tree.heading("#0", text="File")
        tree.heading("status", text="Engine")
        tree.column("#0", width=240, anchor="w", stretch=True)
        tree.column("status", width=108, anchor="center", stretch=False)
        tree.grid(row=2, column=0, sticky="nsew")
        tree.tag_configure("ok", foreground=OK_COLOR)
        tree.tag_configure("err", foreground=ERR_COLOR)
        tree.tag_configure("warn", foreground=WARN_COLOR)
        tree.tag_configure("pending", foreground=FG_MUTED)
        tree.bind("<<TreeviewSelect>>", self._on_select_file)
        self.tree = tree

        scroll = ttk.Scrollbar(panel, orient="vertical", command=tree.yview)
        scroll.grid(row=2, column=1, sticky="ns")
        tree.configure(yscrollcommand=scroll.set)

        hint = (
            "Drag & drop files — they convert automatically"
            if _DND_AVAILABLE
            else "Use “Add files…” — they convert automatically"
        )
        self.drop_hint = ttk.Label(panel, text=hint, style="PanelMuted.TLabel")
        self.drop_hint.grid(row=3, column=0, sticky="w", pady=(8, 2))

        ocr_text = "OCR images & scanned PDFs"
        if not ocr_available():
            ocr_text += "  (install rapidocr-onnxruntime)"
        ocr_chk = ttk.Checkbutton(
            panel,
            text=ocr_text,
            variable=self.ocr_var,
            style="Panel.TCheckbutton",
            command=self._on_ocr_toggle,
        )
        if not ocr_available():
            ocr_chk.configure(state="disabled")
        ocr_chk.grid(row=4, column=0, sticky="w")

        # Right-click: retry/re-convert or remove the row under the cursor.
        self._tree_menu = tk.Menu(
            tree,
            tearoff=0,
            bg=BG_PANEL,
            fg=FG,
            activebackground=ACCENT_FILL,
            activeforeground="#ffffff",
        )
        tree.bind("<Button-3>", self._on_tree_menu)

        if _DND_AVAILABLE:
            tree.drop_target_register(DND_FILES)
            tree.dnd_bind("<<Drop>>", self._on_drop)

    def _build_preview_panel(self, parent: ttk.Frame) -> None:
        panel = ttk.Frame(parent, style="Panel.TFrame", padding=12)
        panel.grid(row=0, column=1, sticky="nsew")
        panel.rowconfigure(1, weight=1)
        panel.columnconfigure(0, weight=1)

        head = ttk.Frame(panel, style="Panel.TFrame")
        head.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(head, text="Markdown preview", style="Panel.TLabel", font=FONT_BOLD).pack(
            side="left"
        )

        self.rendered_var = tk.BooleanVar(value=self.settings.rendered_preview)
        self.clean_var = tk.BooleanVar(value=self.settings.clean_output)
        ttk.Checkbutton(
            head,
            text="Rendered",
            variable=self.rendered_var,
            style="Panel.TCheckbutton",
            command=self._refresh_preview,
        ).pack(side="left", padx=(16, 0))
        ttk.Checkbutton(
            head,
            text="Clean output",
            variable=self.clean_var,
            style="Panel.TCheckbutton",
            command=self._on_clean_toggle,
        ).pack(side="left", padx=(8, 0))

        self.preview_name = ttk.Label(head, text="", style="PanelMuted.TLabel")
        self.preview_name.pack(side="right")

        text = tk.Text(
            panel,
            wrap="word",
            bg=BG_INPUT,
            fg=FG,
            insertbackground=FG,
            relief="flat",
            font=FONT_MONO,
            padx=12,
            pady=10,
            undo=True,
            spacing1=1,
            spacing3=2,
        )
        text.grid(row=1, column=0, sticky="nsew")
        text.configure(state="disabled")
        self.preview = text
        self._renderer = MarkdownRenderer(
            text, fg=FG, muted=FG_MUTED, accent=ACCENT_LINK, code_bg="#15161e"
        )

        pscroll = ttk.Scrollbar(panel, orient="vertical", command=text.yview)
        pscroll.grid(row=1, column=1, sticky="ns")
        text.configure(yscrollcommand=pscroll.set)

        actions = ttk.Frame(panel, style="Panel.TFrame")
        actions.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        ttk.Button(actions, text="Copy", command=self.copy_preview).pack(side="left")
        # Retry appears only while a failed conversion is selected; the fix
        # button carries the error's one-click next step when it has one
        # ("Turn on OCR & retry"), and Copy details its support text.
        self.retry_btn = ttk.Button(actions, text="↻  Retry", command=self.retry_selected)
        self.fix_btn = ttk.Button(actions, style="Accent.TButton")
        self.details_btn = ttk.Button(actions, text="Copy details")
        # Selection-scoped info (cleanup/quality) lives with the preview so
        # selecting a file never evicts batch progress from the status line.
        self.quality_label = ttk.Label(actions, text="", style="PanelMuted.TLabel")
        self.quality_label.pack(side="right")

    # Combobox labels for the export style, mapped to the persisted keys.
    _STYLE_LABELS = (
        ("single", "One Markdown file"),
        ("chapters", "Chapter files (book folder)"),
        ("ai", "AI-sized sections"),
    )

    def _build_footer(self) -> None:
        footer = ttk.Frame(self, style="TFrame")
        footer.pack(side="bottom", fill="x", padx=20, pady=(0, 16))

        self.progress = ttk.Progressbar(
            footer, style="Horizontal.TProgressbar", mode="determinate"
        )
        self.progress.pack(fill="x", pady=(0, 8))
        self.notice = NoticeBar(footer, self._error_handlers)
        self.notice.place_in(fill="x", pady=(0, 8), before=self.progress)

        self.status_var = tk.StringVar(value="Ready — drop files anywhere to convert.")
        ttk.Label(footer, textvariable=self.status_var, style="Muted.TLabel").pack(
            fill="x", pady=(0, 8)
        )

        # The export bar: the output-shape decision lives right next to the
        # Save button, where the decision is actually made.
        row = ttk.Frame(footer, style="TFrame")
        row.pack(fill="x")
        # The button packs FIRST (rightmost) so nothing can push it off-screen.
        self.save_btn = ttk.Button(
            row, text="💾  Save Markdown…", style="Accent.TButton", command=self.save_markdown
        )
        self.save_btn.pack(side="right")

        ttk.Label(row, text="Output:", style="Muted.TLabel").pack(side="left")
        style_labels = [label for _key, label in self._STYLE_LABELS]
        current_label = dict(self._STYLE_LABELS).get(self.settings.export_style, style_labels[0])
        self.style_var = tk.StringVar(value=current_label)
        style_box = ttk.Combobox(
            row,
            textvariable=self.style_var,
            values=style_labels,
            state="readonly",
            width=24,
        )
        style_box.pack(side="left", padx=(8, 16))
        style_box.bind("<<ComboboxSelected>>", self._on_export_style_change)

        ttk.Label(row, text="Optimize for:", style="Muted.TLabel").pack(side="left")
        # Settings.normalize() guarantees ai_target is a valid AI_TARGETS key.
        self.target_var = tk.StringVar(value=self.settings.ai_target)
        self.target_box = ttk.Combobox(
            row,
            textvariable=self.target_var,
            values=list(md_export.AI_TARGETS),
            state="readonly",
            width=15,
        )
        self.target_box.pack(side="left", padx=(8, 0))
        self.target_box.bind("<<ComboboxSelected>>", self._on_export_style_change)
        self._sync_target_state()

    def _export_style_key(self) -> str:
        label_to_key = {label: key for key, label in self._STYLE_LABELS}
        return label_to_key.get(self.style_var.get(), "single")

    def _sync_target_state(self) -> None:
        """The AI-target picker only matters for AI-sized sections."""
        state = "readonly" if self._export_style_key() == "ai" else "disabled"
        self.target_box.configure(state=state)

    def _on_export_style_change(self, _event=None) -> None:
        self._sync_target_state()
        self.settings.export_style = self._export_style_key()
        self.settings.ai_target = self.target_var.get()
        self._save_settings()

    # -- settings ------------------------------------------------------------
    def open_settings(self, show_tab: str | None = None) -> None:
        if self._busy:
            messagebox.showinfo(__app_name__, "Finish the current conversion first.")
            return
        from . import polish  # lazy: stdlib-only, but only the dialog needs it

        dlg = tk.Toplevel(self)
        dlg.title("Settings")
        dlg.configure(bg=BG_PANEL)
        dlg.transient(self)
        dlg.resizable(False, False)
        # master=dlg so these Tcl variables are freed when the dialog is destroyed
        # (otherwise they accumulate on the root across opens).
        ocr_v = tk.BooleanVar(dlg, value=self.settings.enable_ocr)
        ocr_device_v = tk.StringVar(dlg, value=self.settings.ocr_device)
        layout_v = tk.BooleanVar(dlg, value=self.settings.pdf_layout)
        audio_v = tk.BooleanVar(dlg, value=self.settings.enable_audio)
        clean_v = tk.BooleanVar(dlg, value=self.settings.clean_output)
        rendered_v = tk.BooleanVar(dlg, value=self.settings.rendered_preview)
        model_v = tk.StringVar(dlg, value=self.settings.whisper_model)
        endpoint_v = tk.StringVar(dlg, value=self.settings.mineru_endpoint)
        outdir_v = tk.StringVar(dlg, value=self.settings.default_output_dir)
        front_v = tk.BooleanVar(dlg, value=self.settings.export_front_matter)
        anchors_v = tk.BooleanVar(dlg, value=self.settings.page_anchors)
        images_v = tk.BooleanVar(dlg, value=self.settings.extract_images)
        ollama_v = tk.StringVar(dlg, value=self.settings.ollama_endpoint)
        polish_v = tk.StringVar(dlg, value=self.settings.polish_model)
        caption_v = tk.StringVar(dlg, value=self.settings.caption_model)
        summary_v = tk.StringVar(dlg, value=self.settings.summary_model)
        debug_v = tk.BooleanVar(dlg, value=self.settings.debug_mode)
        snapshots_v = tk.BooleanVar(dlg, value=self.settings.debug_snapshots)
        detect_v = tk.StringVar(dlg, value="")

        ttk.Label(
            dlg, text="Markdown Sidekick settings", style="Panel.TLabel", font=FONT_TITLE
        ).pack(anchor="w", padx=16, pady=(16, 8))

        # Tabs keep the dialog under ~450px tall — the old single column was
        # 778px, which clipped Save/Cancel off-screen on 1080p laptops at
        # 150% scaling (measured; roadmap X2).
        nb = ttk.Notebook(dlg)
        nb.pack(fill="both", expand=True, padx=16)

        def tab(title: str) -> ttk.Frame:
            frame = ttk.Frame(nb, style="Panel.TFrame", padding=(16, 12))
            nb.add(frame, text=title)
            return frame

        def section(parent, text: str, row: int, top: int = 12) -> None:
            ttk.Label(parent, text=text, style="Panel.TLabel", font=FONT_BOLD).grid(
                row=row, column=0, columnspan=3, sticky="w", pady=(top, 4)
            )

        def check(parent, text: str, var, row: int) -> None:
            ttk.Checkbutton(
                parent, text=text, variable=var, style="Panel.TCheckbutton"
            ).grid(row=row, column=0, columnspan=3, sticky="w", pady=1)

        # -- Conversion tab ---------------------------------------------------
        conv = tab("Conversion")
        section(conv, "Engines", 0, top=0)
        check(conv, "OCR images & scanned PDFs", ocr_v, 1)
        ttk.Label(conv, text="OCR device", style="Panel.TLabel").grid(
            row=2, column=0, sticky="w", pady=(4, 0)
        )
        ttk.Combobox(
            conv,
            textvariable=ocr_device_v,
            values=["auto", "cpu", "gpu"],
            state="readonly",
            width=12,
        ).grid(row=2, column=1, sticky="w", pady=(4, 0), padx=(12, 0))
        ttk.Label(
            conv,
            text="auto = GPU (DirectML) when present — measured ~6x faster",
            style="PanelMuted.TLabel",
        ).grid(row=2, column=2, sticky="w", padx=(8, 0), pady=(4, 0))
        check(conv, "Column-aware PDF reading (recommended)", layout_v, 3)
        check(conv, "Transcribe audio files", audio_v, 4)
        ttk.Label(conv, text="Whisper model", style="Panel.TLabel").grid(
            row=5, column=0, sticky="w", pady=(8, 0)
        )
        ttk.Combobox(
            conv,
            textvariable=model_v,
            values=list(WHISPER_MODELS),
            state="readonly",
            width=12,
        ).grid(row=5, column=1, sticky="w", pady=(8, 0), padx=(12, 0))
        section(conv, "High-fidelity (optional)", 6)
        ttk.Label(conv, text="MinerU endpoint URL", style="Panel.TLabel").grid(
            row=7, column=0, sticky="w"
        )
        ttk.Entry(conv, textvariable=endpoint_v, width=34).grid(
            row=7, column=1, columnspan=2, sticky="w", padx=(12, 0)
        )
        ttk.Label(
            conv,
            text="e.g. http://127.0.0.1:2364  (blank = off)",
            style="PanelMuted.TLabel",
        ).grid(row=8, column=1, columnspan=2, sticky="w", padx=(12, 0))

        # -- Output tab -------------------------------------------------------
        out_tab = tab("Output")
        section(out_tab, "Saving", 0, top=0)
        ttk.Label(out_tab, text="Default output folder", style="Panel.TLabel").grid(
            row=1, column=0, sticky="w"
        )
        ttk.Entry(out_tab, textvariable=outdir_v, width=28).grid(
            row=1, column=1, sticky="w", padx=(12, 0)
        )

        def browse() -> None:
            d = filedialog.askdirectory(title="Default output folder", parent=dlg)
            if d:
                outdir_v.set(d)

        ttk.Button(out_tab, text="Browse…", command=browse).grid(
            row=1, column=2, sticky="w", padx=6
        )
        section(out_tab, "Preview defaults", 2)
        check(out_tab, "Clean output", clean_v, 3)
        check(out_tab, "Rendered preview", rendered_v, 4)
        # (Chapter/AI splitting lives in the main window's export bar, next to
        # the Save button, where the output decision is actually made.)
        section(out_tab, "AI-friendly export", 5)
        check(out_tab, "Front matter / source header on saved files", front_v, 6)
        check(out_tab, "Page anchors (<!-- page N -->) in PDF conversions", anchors_v, 7)
        check(out_tab, "Extract PDF figures to images/ on save", images_v, 8)

        # -- Local AI tab -----------------------------------------------------
        ai = tab("Local AI")
        section(
            ai,
            f"{polish.known_runtime_names()}… "
            "(optional — everything stays on this machine)",
            0,
            top=0,
        )
        ttk.Label(ai, text="Endpoint", style="Panel.TLabel").grid(
            row=1, column=0, sticky="w"
        )
        ttk.Entry(ai, textvariable=ollama_v, width=28).grid(
            row=1, column=1, sticky="w", padx=(12, 0)
        )
        ttk.Button(ai, text="Detect", command=lambda: run_detect()).grid(
            row=1, column=2, sticky="w", padx=6
        )
        ttk.Label(ai, textvariable=detect_v, style="PanelMuted.TLabel").grid(
            row=2, column=0, columnspan=3, sticky="w", pady=(2, 0)
        )
        ttk.Label(ai, text="Polish model", style="Panel.TLabel").grid(
            row=3, column=0, sticky="w", pady=(8, 0)
        )
        polish_box = ttk.Combobox(ai, textvariable=polish_v, width=22)
        polish_box.grid(row=3, column=1, sticky="w", padx=(12, 0), pady=(8, 0))
        ttk.Label(ai, text="Caption model", style="Panel.TLabel").grid(
            row=4, column=0, sticky="w", pady=(4, 0)
        )
        caption_box = ttk.Combobox(ai, textvariable=caption_v, width=22)
        caption_box.grid(row=4, column=1, sticky="w", padx=(12, 0), pady=(4, 0))
        ttk.Label(ai, text="Summary model", style="Panel.TLabel").grid(
            row=5, column=0, sticky="w", pady=(4, 0)
        )
        summary_box = ttk.Combobox(ai, textvariable=summary_v, width=22)
        summary_box.grid(row=5, column=1, sticky="w", padx=(12, 0), pady=(4, 0))
        ttk.Label(
            ai,
            text="Polish repairs residual artifacts; caption writes alt-text for\n"
            "extracted figures; summary adds a 2–3 sentence description to\n"
            "saved front matter. Blank model = that pass stays off.",
            style="PanelMuted.TLabel",
            justify="left",
        ).grid(row=6, column=0, columnspan=3, sticky="w", pady=(4, 0))
        section(ai, "AI integration (MCP)", 7)
        ttk.Button(
            ai,
            text="📋  Copy AI setup prompt",
            command=lambda: self.copy_mcp_prompt(parent=dlg),
        ).grid(row=8, column=0, sticky="w")
        ttk.Label(
            ai,
            text="Paste it into Claude, Cursor, or any AI assistant\n"
            "to connect Markdown Sidekick as a converter tool.",
            style="PanelMuted.TLabel",
            justify="left",
        ).grid(row=8, column=1, columnspan=2, sticky="w", padx=(8, 0))

        # -- Diagnostics tab --------------------------------------------------
        diag = tab("Diagnostics")
        section(diag, "Error log (always on)", 0, top=0)
        ttk.Label(
            diag,
            text="Every error is recorded automatically — with its code and reference — "
            f"in {debuglog.log_dir()}",
            style="PanelMuted.TLabel",
            justify="left",
            wraplength=500,
        ).grid(row=1, column=0, columnspan=3, sticky="w")
        section(diag, "Debug mode", 2)
        check(diag, "Record a detailed trace of every conversion (timings, decisions)", debug_v, 3)
        check(diag, "Also keep each conversion's raw and cleaned Markdown", snapshots_v, 4)
        ttk.Label(
            diag,
            text="Logs stay on this PC. Traces include file names and paths;\n"
            "the Markdown copies include document text.",
            style="PanelMuted.TLabel",
            justify="left",
        ).grid(row=5, column=0, columnspan=3, sticky="w", pady=(2, 0))
        section(diag, "Report a problem", 6)
        diag_btns = ttk.Frame(diag, style="Panel.TFrame")
        diag_btns.grid(row=7, column=0, columnspan=3, sticky="w")
        ttk.Button(
            diag_btns,
            text="Create diagnostic report…",
            command=lambda: self.create_diagnostic_report(parent=dlg),
        ).pack(side="left")
        ttk.Button(diag_btns, text="Open log folder", command=self.open_log_folder).pack(
            side="left", padx=(8, 0)
        )
        ttk.Label(
            diag,
            text="The report zips the error log, recent debug traces, your settings and\n"
            "a readable summary — attach it to a bug report.",
            style="PanelMuted.TLabel",
            justify="left",
        ).grid(row=8, column=0, columnspan=3, sticky="w", pady=(4, 0))

        def run_detect(auto: bool = False) -> None:
            """Probe Ollama on a worker thread and fill the model pickers.

            Tri-state UX (the Ghostwire pattern): running-with-models,
            running-but-nothing-pulled, and not-running each read differently.
            The auto-probe on open stays quiet when nothing ANSWERS (a found
            runtime is reported even without models); the Detect button
            reports every outcome. Thread-safety follows the app's rule: the
            worker touches only a plain list, and the Tk side polls it via
            dlg.after.
            """
            endpoint = ollama_v.get().strip()
            if not auto:
                detect_v.set("Probing…")
            result_box: list[tuple[str, list[str], str, str]] = []
            threading.Thread(
                target=lambda: result_box.append(polish.detect_local_ai(endpoint)),
                daemon=True,
            ).start()

            def poll() -> None:
                if not dlg.winfo_exists():
                    return
                if not result_box:
                    dlg.after(100, poll)
                    return
                status, models, protocol, found = result_box[0]
                if status == "ok":
                    name = polish.runtime_name(found, protocol)
                    # Fill the endpoint only if the field is STILL blank — the
                    # scan takes a while and must never clobber a URL the user
                    # typed in the meantime.
                    if not endpoint and found and not ollama_v.get().strip():
                        ollama_v.set(found)
                    polish_box.configure(values=models)
                    caption_box.configure(values=models)
                    summary_box.configure(values=models)
                    detect_v.set(f"✓ {name} detected — {len(models)} model(s) available")
                elif status == "empty":
                    name = polish.runtime_name(found, protocol)
                    hint = (
                        "try:  ollama pull llama3.2"
                        if protocol == "ollama"
                        else "load a model in the app, then Detect again"
                    )
                    detect_v.set(f"{name} is running but has no models — {hint}")
                elif not auto:
                    # Say what was actually probed: one typed endpoint, or
                    # the well-known-port scan.
                    if endpoint:
                        detect_v.set("Nothing answered at that endpoint.")
                    else:
                        detect_v.set(
                            f"No local AI found (checked the {polish.known_runtime_names()} ports)."
                        )

            dlg.after(100, poll)

        # Quiet auto-probe: if Ollama is already running, the pickers fill
        # themselves before the user even reaches the tab.
        dlg.after(250, lambda: run_detect(auto=True))

        def save() -> None:
            before = dict(vars(self.settings))
            self.settings.enable_ocr = ocr_v.get()
            self.settings.ocr_device = ocr_device_v.get()
            self.settings.pdf_layout = layout_v.get()
            self.settings.enable_audio = audio_v.get()
            self.settings.whisper_model = model_v.get()
            self.settings.mineru_endpoint = endpoint_v.get()
            self.settings.default_output_dir = outdir_v.get()
            self.settings.clean_output = clean_v.get()
            self.settings.rendered_preview = rendered_v.get()
            self.settings.export_front_matter = front_v.get()
            self.settings.page_anchors = anchors_v.get()
            self.settings.extract_images = images_v.get()
            self.settings.ollama_endpoint = ollama_v.get()
            self.settings.polish_model = polish_v.get()
            self.settings.caption_model = caption_v.get()
            self.settings.summary_model = summary_v.get()
            self.settings.debug_mode = debug_v.get()
            self.settings.debug_snapshots = snapshots_v.get()
            if not self._save_settings(parent=dlg):
                return  # the dialog stays open so nothing typed is lost
            changed = {
                k: [before.get(k), v] for k, v in vars(self.settings).items() if before.get(k) != v
            }
            self._apply_debug()
            debuglog.event("ui.settings_saved", changed=changed)
            self._apply_settings()
            dlg.destroy()
            # Existing results were produced with the old configuration —
            # offer to redo them (this replaces the old "Convert all" button).
            if any(r is not None for r in self.files.values()):
                if messagebox.askyesno(
                    __app_name__,
                    f"Re-convert the {len(self.files)} loaded file(s) with the new settings?",
                ):
                    self.reconvert_all()

        btns = ttk.Frame(dlg, style="Panel.TFrame")
        btns.pack(fill="x", pady=16, padx=16)
        ttk.Button(btns, text="Cancel", command=dlg.destroy).pack(side="right", padx=(8, 0))
        ttk.Button(btns, text="Save", style="Accent.TButton", command=save).pack(side="right")

        dlg.bind("<Escape>", lambda _e: dlg.destroy())
        for tab_id in nb.tabs():
            if nb.tab(tab_id, "text") == show_tab:
                nb.select(tab_id)
        dlg.update_idletasks()
        dlg.grab_set()

    def _on_ocr_toggle(self) -> None:
        """Panel OCR checkbox is a live shortcut for settings.enable_ocr — keep
        the single source of truth in sync so the dialog never silently flips it."""
        self.settings.enable_ocr = self.ocr_var.get()
        self.engine.enable_ocr = self.settings.enable_ocr

    def _apply_settings(self) -> None:
        """Push saved settings into the engine and live UI controls."""
        s = self.settings
        self.engine.enable_ocr = s.enable_ocr
        self.engine.ocr_device = s.ocr_device
        self.engine.enable_audio = s.enable_audio
        self.engine.whisper_model = s.whisper_model
        self.engine.mineru_endpoint = s.mineru_endpoint
        self.engine.page_anchors = s.page_anchors
        self.engine.pdf_layout = s.pdf_layout
        self.engine.figure_markers = s.extract_images
        self.ocr_var.set(s.enable_ocr and ocr_available())
        self.clean_var.set(s.clean_output)
        self.rendered_var.set(s.rendered_preview)
        self._evict_all_caches()
        self._on_select_file()

    # -- help / support --------------------------------------------------------
    def open_kofi(self) -> None:
        webbrowser.open(KOFI_URL)
        self.status_var.set("Thank you for supporting Markdown Sidekick! ☕")

    def copy_mcp_prompt(self, parent: tk.Misc | None = None) -> None:
        """Copy a paste-into-any-AI prompt that sets up this install's MCP server."""
        if not self._to_clipboard(build_mcp_setup_prompt()):
            return
        self.status_var.set("AI setup prompt copied to clipboard.")
        messagebox.showinfo(
            __app_name__,
            "Setup prompt copied!\n\nPaste it into Claude, Cursor, or any AI "
            "assistant and it will connect Markdown Sidekick as a converter tool.",
            parent=parent or self,
        )

    def open_help(self) -> None:
        # Singleton: if the guide is already open, just bring it forward.
        if self._help_win is not None and self._help_win.winfo_exists():
            self._help_win.deiconify()
            self._help_win.lift()
            return

        win = tk.Toplevel(self)
        win.title("Markdown Sidekick — User Guide")
        win.geometry("880x720")
        win.minsize(600, 400)
        win.configure(bg=BG_PANEL)
        win.bind("<Escape>", lambda _e: win.destroy())
        self._help_win = win

        body = ttk.Frame(win, style="Panel.TFrame", padding=12)
        body.pack(fill="both", expand=True)
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)

        text = tk.Text(
            win,
            wrap="word",
            bg=BG_INPUT,
            fg=FG,
            relief="flat",
            font=FONT,
            padx=18,
            pady=14,
            spacing1=1,
            spacing3=2,
        )
        text.grid(in_=body, row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(body, orient="vertical", command=text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        text.configure(yscrollcommand=scroll.set)

        renderer = MarkdownRenderer(
            text, fg=FG, muted=FG_MUTED, accent=ACCENT_LINK, code_bg="#15161e"
        )
        renderer.render(load_user_guide())

        bar = ttk.Frame(body, style="Panel.TFrame")
        bar.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        ttk.Button(
            bar, text="☕  Support on Ko-fi", style="Accent.TButton", command=self.open_kofi
        ).pack(side="left")
        ttk.Button(bar, text="Close", command=win.destroy).pack(side="right")

    # -- file management -----------------------------------------------------
    def add_files(self) -> None:
        patterns = ";".join(f"*{ext}" for ext in SUPPORTED_EXTENSIONS)
        with debuglog.ui_blocking("open dialog"):
            paths = filedialog.askopenfilenames(
                title="Select files to convert",
                filetypes=[("Supported files", patterns), ("All files", "*.*")],
            )
        self._add_paths(paths)

    def _add_paths(self, paths) -> None:
        added = 0
        folders: list[str] = []
        for raw in paths:
            path = Path(raw)
            if path.is_dir():
                folders.append(path.name)
                continue
            if not path.is_file() or path in self.files:
                continue
            self.files[path] = None
            self.tree.insert(
                "", "end", iid=str(path), text=path.name, values=("pending",), tags=("pending",)
            )
            added += 1
        if folders:
            self._show_notice(
                errors.report("MS-104", where="add files", detail=", ".join(folders[:10])),
                extra=f"Skipped {len(folders)} folder(s).",
            )
        if added:
            debuglog.event(
                "ui.action",
                action="add_files",
                count=added,
                exts=sorted({Path(p).suffix.lower() for p in paths}),
            )
            self.status_var.set(f"Added {added} file(s).")
            # Conversion starts by itself — if a batch is already running, the
            # new files wait as "pending" and are picked up when it finishes.
            self._convert_pending()

    def _evict_caches(self, path: Path) -> None:
        """Drop every cache derived from a path's conversion result."""
        self._clean_cache.pop(path, None)
        self._clean_stats.pop(path, None)
        self._quality_cache.pop((path, True), None)
        self._quality_cache.pop((path, False), None)

    def _evict_all_caches(self) -> None:
        """Wholesale version of :meth:`_evict_caches` — one owner for the
        cache set, so a new derived cache can't be missed at a clear site."""
        self._clean_cache.clear()
        self._clean_stats.clear()
        self._quality_cache.clear()

    def _reset_to_pending(self, path: Path) -> None:
        """Return a file to the un-converted state (shared by retry/reconvert)."""
        self.files[path] = None
        self._evict_caches(path)
        iid = str(path)
        if self.tree.exists(iid):
            self.tree.item(iid, values=("pending",), tags=("pending",))

    def remove_selected(self) -> None:
        if self._busy:  # keyboard route; the button is disabled while busy
            return
        for iid in self.tree.selection():
            path = Path(iid)
            self.files.pop(path, None)
            self._evict_caches(path)
            self.tree.delete(iid)
            self.notice.hide(owner=path)
        debuglog.event("ui.action", action="remove")
        self._clear_preview()

    def clear_files(self) -> None:
        if self._busy:
            return
        # Only successful conversions are worth a confirmation — a list of
        # failures (or pending files) clears without ceremony.
        converted = sum(1 for r in self.files.values() if r is not None and r.ok)
        if converted and not messagebox.askyesno(
            __app_name__,
            f"Discard {converted} converted result(s)?\n\n"
            "Your source files aren't touched, but re-adding them means "
            "converting from scratch.",
        ):
            return
        self.files.clear()
        self._evict_all_caches()
        if self.notice.owner is not None:
            self.notice.hide()
        debuglog.event("ui.action", action="clear")
        self.tree.delete(*self.tree.get_children())
        self._clear_preview()
        self.progress["value"] = 0
        self.status_var.set("Ready.")

    def _on_drop(self, event) -> None:
        # tkinterdnd2 returns a brace-wrapped, space-joined string of paths.
        paths = self.tk.splitlist(event.data)
        self._add_paths(paths)

    def retry_selected(self) -> None:
        """Re-run conversion for the selected file (Retry button / context menu)."""
        if self._busy:
            return
        path = self._selected_path()
        if path is None or path not in self.files:
            return
        debuglog.event("ui.action", action="retry", file=path.name)
        self._reset_to_pending(path)
        self._on_select_file()  # swaps the preview to the converting state
        self._convert_pending()

    def _retry_where(self, codes: tuple[str, ...]) -> None:
        """Re-convert the selected file plus every file that failed (or was
        degraded) for one of ``codes`` — the fix just applied helps them all."""
        if self._busy:
            return
        selected = self._selected_path()
        for path, result in self.files.items():
            hit = result is not None and (
                result.error_code in codes or any(w.code in codes for w in result.warnings)
            )
            if hit or path == selected:
                self._reset_to_pending(path)
        self._on_select_file()
        self._convert_pending()

    def retry_failed(self) -> None:
        """Re-convert every failed file (the MS-902 next step)."""
        if self._busy:
            return
        for path, result in self.files.items():
            if result is not None and not result.ok:
                self._reset_to_pending(path)
        self._on_select_file()
        self._convert_pending()

    def _on_tree_menu(self, event) -> None:
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        self.tree.selection_set(iid)
        menu = self._tree_menu
        menu.delete(0, "end")
        result = self.files.get(Path(iid))
        label = "Retry conversion" if result is not None and not result.ok else "Re-convert"
        state = "disabled" if self._busy or result is None else "normal"
        menu.add_command(label=label, command=self.retry_selected, state=state)
        menu.add_command(
            label="Remove",
            command=self.remove_selected,
            state="disabled" if self._busy else "normal",
        )
        menu.tk_popup(event.x_root, event.y_root)

    # -- selection / preview -------------------------------------------------
    def _selected_path(self) -> Path | None:
        selection = self.tree.selection()
        return Path(selection[0]) if selection else None

    def _export_text_for(self, path: Path) -> str | None:
        """The text to show/copy/save for a converted file (cleaned or raw)."""
        result = self.files.get(path)
        if not result or not result.ok:
            return None
        if not self.clean_var.get():
            return result.markdown
        if path not in self._clean_cache:
            cleaned, stats = clean_markdown(result.markdown, engine=result.engine)
            self._clean_cache[path] = cleaned
            self._clean_stats[path] = stats
        return self._clean_cache[path]

    def _on_select_file(self, _event=None) -> None:
        # Selection-scoped widgets reset unconditionally FIRST — an empty
        # selection (Ctrl+click deselect) must not leave a stale Retry
        # button or quality text hovering over a dead preview.
        self.retry_btn.pack_forget()
        self.fix_btn.pack_forget()
        self.details_btn.pack_forget()
        self.quality_label.configure(text="")
        path = self._selected_path()
        if self.notice.owner is not None and self.notice.owner != path:
            self.notice.hide()  # a previous file's warning
        if path is None:
            return
        result = self.files.get(path)
        if result is None:
            self._current_export_text = ""  # nothing real to copy/save
            self._show_plain(
                path.name,
                "Converting… the preview appears when this file finishes.",
                store=False,
            )
        elif not result.ok:
            self._current_export_text = ""
            incident = self._result_incident(result)
            self._show_plain(
                path.name,
                f"⚠  {incident.title}\n\n"
                f"→  What to do: {incident.next_step}\n\n"
                f"{incident.reference()} — the full details are in the error log.\n\n\n"
                f"Technical details\n{result.error}",
                store=False,
            )
            self._show_fix_buttons(incident, retry=True)
        else:
            # Cleanup + quality are normally precomputed on the worker; a miss
            # here (e.g. the Clean toggle flipped after conversion) can cost
            # seconds on a big book, so show a busy cursor while it runs.
            clean_on = bool(self.clean_var.get())
            key = (path, clean_on)
            miss = (clean_on and path not in self._clean_cache) or key not in self._quality_cache
            if miss:
                self.configure(cursor="watch")
                self.update_idletasks()
            started = time.perf_counter()
            try:
                text = self._export_text_for(path) or "(Empty output.)"
                self._current_export_text = text
                self.preview_name.configure(text=f"{path.name}   ·   {result.engine}")
                display = text
                if len(display) > PREVIEW_MAX_CHARS:
                    cut = display.rfind("\n", 0, PREVIEW_MAX_CHARS)
                    display = display[: cut if cut > 0 else PREVIEW_MAX_CHARS]
                    notice = (
                        f"Preview shows the first {PREVIEW_MAX_CHARS // 1000}k "
                        "characters — Copy and Save always use the full document."
                    )
                    if self.rendered_var.get():
                        display = f"*{notice}*\n\n{display}"
                    else:
                        display = f"{notice}\n\n{display}"
                if self.rendered_var.get():
                    try:
                        self._renderer.render(display)
                    except Exception as exc:  # show the raw text rather than nothing
                        self._show_plain(path.name, display, store=False)
                        self._show_notice(
                            errors.report("MS-903", exc=exc, where="preview", file=str(path)),
                            owner=path,
                        )
                else:
                    self._show_plain(path.name, display, store=False)
                # One short line beside the preview — not a stats dump.
                bits = []
                if clean_on and path in self._clean_stats:
                    stats = self._clean_stats[path]
                    if stats.changed:
                        bits.append(stats.brief())
                report = self._quality_cache.get(key)
                if report is None:
                    report = assess_markdown(text)
                    self._quality_cache[key] = report
                bits.append(f"Quality {report.score}/100")
                # A score under 100 with no explanation is a dead end — name
                # the top artifact so the user knows WHY (roadmap X4). Binary
                # noise is the one issue that means "don't trust any of it",
                # so it gets a warning marker rather than a second bit.
                if report.issues:
                    top = report.issues[0]
                    if report.binary_noise:
                        top = "⚠ " + top
                    bits.append(top)
                bits.append(f"~{report.est_tokens:,} tokens")
                if report.headings:
                    bits.append(f"{report.headings} headings")
                self.quality_label.configure(text="   ·   ".join(bits))
                if result.warnings:
                    extra = (
                        f"{len(result.warnings)} problems with this file. "
                        if len(result.warnings) > 1
                        else ""
                    )
                    self._show_notice(result.warnings[0], owner=path, extra=extra)
                debuglog.event(
                    "ui.preview",
                    file=path.name,
                    ms=round((time.perf_counter() - started) * 1000),
                    chars=len(display),
                    rendered=bool(self.rendered_var.get()),
                    miss=miss,
                )
            finally:
                if miss:
                    self.configure(cursor="")

    def _show_plain(self, name: str, content: str, store: bool = True) -> None:
        """Drop the raw text into the preview with no Markdown styling."""
        if store:
            self._current_export_text = content
        self.preview_name.configure(text=name)
        self.preview.configure(state="normal")
        self.preview.delete("1.0", "end")
        self.preview.insert("1.0", content)
        self.preview.configure(state="disabled")
        self.preview.yview_moveto(0.0)

    def _refresh_preview(self) -> None:
        """Re-render the current selection (view-mode toggle changed)."""
        self._on_select_file()

    def _on_clean_toggle(self) -> None:
        """Clean toggle changed — caches are keyed only on cleaned text, so just re-render."""
        self._on_select_file()

    def _clear_preview(self) -> None:
        self.retry_btn.pack_forget()
        self.quality_label.configure(text="")
        self._current_export_text = ""
        self.preview_name.configure(text="")
        self.preview.configure(state="normal")
        self.preview.delete("1.0", "end")
        self.preview.configure(state="disabled")

    def copy_preview(self) -> None:
        from .figures import strip_figure_markers

        # Figure markers only mean something to a save with figure extraction.
        content = strip_figure_markers(self._current_export_text)
        if not content.strip():
            return
        if not self._to_clipboard(content):
            return
        debuglog.event("ui.action", action="copy", chars=len(content))
        self.status_var.set("Markdown copied to clipboard.")

    # -- conversion ----------------------------------------------------------
    def _convert_pending(self) -> None:
        """Convert every file that hasn't been converted yet.

        Called automatically when files are added. If a batch is already
        running, this is a no-op — the batch's completion handler calls back
        here to pick up whatever queued in the meantime.
        """
        if self._busy:
            return
        targets = [p for p, r in self.files.items() if r is None]
        if not targets:
            return
        self._set_busy(True)
        # Read Tk state on the main thread before the worker starts — the OCR
        # toggle for the engine, the Clean toggle for the precompute pass
        # (Tk variables are not safe to read from the worker).
        self.engine.enable_ocr = self.ocr_var.get()
        clean_flag = bool(self.clean_var.get())
        self.progress.configure(maximum=len(targets), value=0)
        self.status_var.set("Converting…")

        thread = threading.Thread(
            target=self._worker_convert,
            args=(targets, clean_flag),
            daemon=True,
            name="convert-worker",
        )
        thread.start()

    def reconvert_all(self) -> None:
        """Reset every loaded file to pending and convert again (used after a
        settings change so results reflect the new configuration)."""
        if self._busy or not self.files:
            return
        for path in self.files:
            self._reset_to_pending(path)
        self._clear_preview()
        self._convert_pending()

    def _worker_convert(self, targets: list[Path], clean_flag: bool) -> None:
        def on_progress(index: int, total: int, result: ConversionResult) -> None:
            # Post the progress event FIRST so the row/progress bar update the
            # moment conversion finishes, then precompute the first-click work
            # (cleanup + quality) and ship it as a separate "pack" event —
            # selecting a big book never freezes the UI thread, and the UI
            # never shows a converted file as still pending.
            self._events.put(("progress", index, total, result))
            if not (result.ok and result.markdown):
                return
            try:
                with debuglog.file_context(result.source):
                    if clean_flag:
                        cleaned, stats = clean_markdown(result.markdown, engine=result.engine)
                        pack = (cleaned, stats, assess_markdown(cleaned))
                    else:
                        pack = (None, None, assess_markdown(result.markdown))
            except Exception as exc:  # precompute is an optimisation, never fatal
                # Logged only: selecting the file redoes this on the UI
                # thread, where a real failure gets its dialog.
                errors.report("MS-901", exc=exc, where="cleanup precompute", file=str(result.source))
                return
            self._events.put(("pack", result.source, clean_flag, pack))

        def on_subprogress(source: Path, current: float, total: float, unit: str) -> None:
            self._events.put(("subprogress", source, current, total, unit))

        try:
            self.engine.convert_many(
                targets, on_progress=on_progress, on_subprogress=on_subprogress
            )
        except BaseException as exc:  # noqa: BLE001 - the batch must always end
            incident = errors.report("MS-902", exc=exc, where="conversion worker")
            self._events.put(("worker_failed", targets, incident))
        finally:
            # Without "done" the app would stay busy (buttons locked) forever.
            self._events.put(("done",))

    def _poll_events(self) -> None:
        # The re-arm lives in a finally: if a handler raises (Tk prints the
        # traceback but the callback dies), the pump must keep running —
        # otherwise pending progress/done events are lost and the app stays
        # locked busy forever.
        debuglog.ui_tick()
        try:
            while True:
                event = self._events.get_nowait()
                self._handle_event(event)
        except queue.Empty:
            pass
        finally:
            self.after(60, self._poll_events)

    def _handle_event(self, event: tuple) -> None:
        kind = event[0]
        if kind == "progress":
            _, index, total, result = event
            # Ignore a late result for a file removed/cleared mid-run.
            if result.source not in self.files:
                self.progress["value"] = index
                return
            self.files[result.source] = result
            # A fresh result invalidates anything derived from the old one.
            self._evict_caches(result.source)
            tag = ("warn" if result.warnings else "ok") if result.ok else "err"
            status = result.engine if result.ok else f"error {result.error_code}".strip()
            if result.ok and result.warnings:
                status += " ⚠"
            iid = str(result.source)
            if self.tree.exists(iid):
                self.tree.item(iid, values=(status,), tags=(tag,))
            self.progress["value"] = index
            self.status_var.set(f"Converting… {index}/{total}  ({result.title})")
            if self.tree.selection() and self.tree.selection()[0] == iid:
                self._on_select_file()
        elif kind == "pack":
            # Precomputed cleanup/quality arriving after its progress event.
            _, source, clean_flag, pack = event
            if source not in self.files:  # removed/cleared mid-run
                return
            cleaned, stats, report = pack
            if cleaned is not None:
                self._clean_cache[source] = cleaned
                self._clean_stats[source] = stats
            self._quality_cache[(source, clean_flag)] = report
            # If the user already selected this file, refresh so the preview
            # and quality line pick up the cleaned text without another click.
            if self.tree.selection() and self.tree.selection()[0] == str(source):
                self._on_select_file()
        elif kind == "subprogress":
            _, source, current, total, unit = event
            name = Path(source).name
            if unit == "sec":
                self.status_var.set(
                    f"Transcribing {name} — {_fmt_mmss(current)}/{_fmt_mmss(total)}…"
                )
            else:
                self.status_var.set(
                    f"OCR {name} — page {int(current)}/{int(total)}…"
                )
        elif kind == "worker_failed":
            # Files the crashed batch never reached become failures, not
            # pending — "done" would otherwise hand them straight back to
            # the same crash.
            _, targets, incident = event
            for path in targets:
                if path in self.files and self.files[path] is None:
                    self.files[path] = ConversionResult(
                        source=path,
                        error="Conversion stopped before this file was reached.",
                        error_code=incident.code,
                        error_ref=incident.ref,
                    )
                    if self.tree.exists(str(path)):
                        self.tree.item(str(path), values=(f"error {incident.code}",), tags=("err",))
            self._show_error(incident)
        elif kind == "incident":
            self._show_error(event[1])
        elif kind == "done":
            self._set_busy(False)
            ok = sum(1 for r in self.files.values() if r and r.ok)
            err = sum(1 for r in self.files.values() if r and not r.ok)
            ocr_used = sum(
                1 for r in self.files.values() if r and r.ok and r.engine.startswith("ocr")
            )
            transcribed = sum(
                1 for r in self.files.values() if r and r.ok and r.engine == "whisper"
            )
            msg = f"Finished — {ok} converted"
            extra = []
            if ocr_used:
                extra.append(f"{ocr_used} via OCR")
            if transcribed:
                extra.append(f"{transcribed} transcribed")
            if extra:
                msg += f" ({', '.join(extra)})"
            if err:
                msg += f", {err} failed"
            self.status_var.set(msg + ".")
            # Auto-select first result so the preview isn't empty.
            if not self.tree.selection():
                children = self.tree.get_children()
                if children:
                    self.tree.selection_set(children[0])
            # Files dropped while this batch ran are still pending — chain.
            self._convert_pending()

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        state = "disabled" if busy else "normal"
        # Removing/clearing is locked while a conversion is in flight so a
        # worker result can't resurrect a removed/cleared file. ADDING stays
        # enabled — new files queue as pending and convert when the batch ends.
        for widget in (
            self.save_btn,
            self.remove_btn,
            self.clear_btn,
            self.retry_btn,
            self.fix_btn,
        ):
            widget.configure(state=state)

    # -- saving --------------------------------------------------------------
    def save_markdown(self) -> None:
        """The one save action. Output shape comes from the export bar:
        single .md file(s), chapter folders, or AI-sized section folders."""
        if self._busy:  # keyboard route; the button is disabled while busy
            return
        converted = [p for p, r in self.files.items() if r and r.ok]
        if not converted:
            messagebox.showinfo(
                __app_name__, "Nothing to save yet — drop in a file and it converts automatically."
            )
            return
        debuglog.event(
            "ui.action",
            action="save",
            files=len(converted),
            style=self._export_style_key(),
            target=self.target_var.get(),
        )
        if len(converted) == 1 and self._export_style_key() == "single":
            self._save_single(converted[0])
        else:
            self._save_batch(converted)

    def _save_single(self, path: Path) -> None:
        text = self._export_text_for(path)
        if text is None:
            messagebox.showwarning(
                __app_name__, "That file hasn't been converted successfully yet."
            )
            return
        with debuglog.ui_blocking("save dialog"):
            dest = filedialog.asksaveasfilename(
                title="Save Markdown",
                defaultextension=".md",
                initialfile=f"{path.stem}.md",
                initialdir=self.settings.default_output_dir or None,
                filetypes=[("Markdown", "*.md"), ("All files", "*.*")],
            )
        if not dest:
            return
        dest_path = Path(dest)
        result = self.files.get(path)
        try:
            with debuglog.file_context(path):
                text = self._with_figures(
                    path, text, dest_path.parent / "images" / dest_path.stem, f"images/{dest_path.stem}"
                )
                summary = ""
                if self.settings.export_front_matter:
                    summary = self._summarize_for_export(text, path.name, result)
                md_export.export_single(
                    text,
                    dest_path,
                    source=path.name,
                    engine=result.engine if result else "",
                    front_matter=self.settings.export_front_matter,
                    summary=summary,
                    title=result.doc_title if result else "",
                    author=result.doc_author if result else "",
                )
        except Exception as exc:
            self._show_error(self._save_incident(exc, dest_path))
            return
        self.status_var.set(f"Saved {dest_path.name}.")

    def _run_blocking(self, fn, status: str):
        """Run ``fn`` on a worker thread and return its result (None if it
        raised or the window closed).

        This side only pumps Tk and polls a plain list (the app's threading
        rule), so the window keeps painting while slow work runs — a local
        model thinking, or hundreds of figures being written. The busy flag
        locks the action buttons meanwhile so a stray click can't re-enter
        save.
        """
        box: list = []
        self._blocking_error = None
        context = debuglog.context(**debuglog.current_context()) if debuglog.enabled() else None

        def target() -> None:
            try:
                if context is not None:
                    with context:  # the caller's file context follows the work
                        box.append((True, fn()))
                else:
                    box.append((True, fn()))
            except Exception as exc:  # never fatal: callers degrade to "no extra"
                box.append((False, exc))

        threading.Thread(target=target, daemon=True).start()
        was_busy = self._busy
        self._set_busy(True)
        self.status_var.set(status)
        self.configure(cursor="watch")
        try:
            while not box:
                self.update()
                time.sleep(0.05)
        except tk.TclError:  # window closed mid-wait
            return None
        finally:
            try:
                self.configure(cursor="")
                self._set_busy(was_busy)
                if not was_busy:
                    # Files added while pumping queued as pending (busy
                    # blocked their conversion); pick them up once idle —
                    # a later wait in the same save is busy again, so this
                    # only fires after the save completes.
                    self.after_idle(self._convert_pending)
            except tk.TclError:
                pass
        ok, value = box[0]
        if not ok:
            self._blocking_error = value  # the caller decides how to report it
            return None
        return value

    def _summarize_for_export(
        self, text: str, name: str, result: ConversionResult | None = None
    ) -> str:
        """Document summary from the configured local AI, or "" when off/failed."""
        endpoint, model = self.settings.ollama_endpoint, self.settings.summary_model
        if not endpoint or not model:
            return ""
        from . import polish

        title = result.doc_title if result else ""
        polish.take_last_failure()  # only this call's failure counts
        summary = self._run_blocking(
            lambda: polish.summarize_markdown(text, endpoint, model, title=title),
            f"Summarizing {name} with {model}…",
        )
        failure = polish.take_last_failure()
        if self._blocking_error is not None:
            failure = errors.report("MS-399", exc=self._blocking_error, where="summary", model=model)
        if not summary and failure is not None:
            # The save goes on without it — but the user learns why.
            self._show_notice(failure, extra=f"No summary for {name}:")
        return summary or ""

    def _with_figures(self, path: Path, text: str, images_dir: Path, rel_dir: str) -> str:
        """Extract the PDF's figures into ``images_dir`` and link them in
        place (Settings → Output); otherwise just drop the figure markers."""
        from . import figures

        if not (self.settings.extract_images and path.suffix.lower() == ".pdf"):
            return figures.strip_figure_markers(text)
        figs = self._run_blocking(
            lambda: figures.extract_pdf_figures(path, images_dir),
            f"Extracting figures from {path.name}…",
        )
        if self._blocking_error is not None:
            self._show_notice(
                errors.report(
                    "MS-401", exc=self._blocking_error, where="figures", file=str(path)
                ),
                extra=f"{path.name}:",
            )
        return figures.insert_figure_links(text, figs or [], rel_dir)

    def _save_batch(self, converted: list[Path]) -> None:
        # Always show the dialog (pre-filled with the default folder) — silent
        # writes leave the user guessing where their files went.
        with debuglog.ui_blocking("folder dialog"):
            out_dir = filedialog.askdirectory(
                title="Choose output folder",
                initialdir=self.settings.default_output_dir or None,
            )
        if not out_dir:
            return
        out = Path(out_dir)
        style = self._export_style_key()
        target = self.target_var.get()
        max_tokens = md_export.AI_TARGETS.get(target, md_export.DEFAULT_MAX_TOKENS)
        notebook = style == "ai" and target in md_export.NOTEBOOK_TARGETS
        saved = 0
        used_names: set[str] = set()
        failed: list[tuple[Path, errors.Incident]] = []
        for path in converted:
            try:
                with debuglog.file_context(path):
                    saved += self._save_one(path, out, style, notebook, max_tokens, used_names)
            except Exception as exc:  # one bad file must not lose the rest
                failed.append((path, self._save_incident(exc, out / path.stem)))
        if failed:
            names = ", ".join(p.name for p, _ in failed[:5]) + (" …" if len(failed) > 5 else "")
            self._show_error(
                failed[0][1],
                extra=f"Saved {saved} file(s); {len(failed)} source(s) couldn't be saved: {names}.",
            )
            return
        self.status_var.set(f"Saved {saved} Markdown file(s) to {out}.")
        if messagebox.askyesno(
            __app_name__, f"Saved {saved} file(s) to:\n{out}\n\nOpen the folder?"
        ):
            self._open_path(out)

    def _save_one(
        self,
        path: Path,
        out: Path,
        style: str,
        notebook: bool,
        max_tokens: int,
        used_names: set[str],
    ) -> int:
        """Write one source's output in a batch save; returns files written."""
        split = style in ("chapters", "ai")
        text = self._export_text_for(path)
        if text is None:
            return 0
        result = self.files.get(path)
        engine = result.engine if result else ""
        title = result.doc_title if result else ""
        author = result.doc_author if result else ""
        if split:
            # Disambiguate same-stem sources so one book folder can never
            # silently overwrite another's chapters/index/manifest.
            folder = path.stem
            if folder in used_names:
                n = 2
                while f"{path.stem}-{n}" in used_names:
                    n += 1
                folder = f"{path.stem}-{n}"
            used_names.add(folder)
            book_dir = out / folder
            text = self._with_figures(path, text, book_dir / "images", "images")
            # A book folder always has somewhere to put the summary (index +
            # manifest), whether or not parts carry front matter.
            summary = self._summarize_for_export(text, path.name, result)
            res = md_export.export_book(
                text,
                book_dir,
                source=path.name,
                engine=engine,
                front_matter=self.settings.export_front_matter,
                max_tokens=max_tokens if style == "ai" else md_export.DEFAULT_MAX_TOKENS,
                ai_sections=style == "ai",
                summary=summary,
                title=title,
                author=author,
                notebook=notebook,
            )
            return len(res.paths)
        dest = default_output_path(path, out)
        # Disambiguate same-stem files from different folders so we never
        # silently overwrite one output with another.
        if dest.name in used_names:
            n = 2
            while (candidate := out / f"{path.stem}-{n}.md").name in used_names:
                n += 1
            dest = candidate
        used_names.add(dest.name)
        text = self._with_figures(path, text, out / "images" / dest.stem, f"images/{dest.stem}")
        summary = ""
        if self.settings.export_front_matter:
            summary = self._summarize_for_export(text, path.name, result)
        md_export.export_single(
            text,
            dest,
            source=path.name,
            engine=engine,
            front_matter=self.settings.export_front_matter,
            summary=summary,
            title=title,
            author=author,
        )
        return 1

    # -- errors ------------------------------------------------------------------
    def _error_handlers(self) -> dict:
        """The one-click next steps (errors.ACTIONS) this window can perform."""
        return {
            "retry": self.retry_selected,
            "retry_failed": self.retry_failed,
            "enable_ocr": self._fix_enable_ocr,
            "enable_audio": self._fix_enable_audio,
            "ocr_cpu": self._fix_ocr_cpu,
            "settings": self.open_settings,
            "settings_conversion": lambda: self.open_settings("Conversion"),
            "settings_output": lambda: self.open_settings("Output"),
            "settings_ai": lambda: self.open_settings("Local AI"),
            "remove": self.remove_selected,
            "add_files": self.add_files,
            "choose_folder": self.save_markdown,
            "save_again": self.save_markdown,
            "copy_again": self.copy_preview,
            "raw_preview": self._fix_raw_preview,
            "open_logs": self.open_log_folder,
        }

    def _show_error(self, incident: errors.Incident, extra: str = "") -> None:
        """The modal explanation for an error that stopped what the user was
        doing. Rate-limited: a repeat within 15 s (or while one is open) only
        updates the status line, so a failing loop can't bury the window."""
        now = time.monotonic()
        dialog_open = self._dialog is not None and self._dialog.winfo_exists()
        if dialog_open or now - self._last_dialog.get(incident.code, -1e9) < 15:
            self.status_var.set(f"⚠ {incident.title} ({incident.code}, ref {incident.ref})")
            return
        self._last_dialog[incident.code] = now
        debuglog.event("ui.error_shown", code=incident.code, ref=incident.ref)
        self._dialog = show_error_dialog(
            self, incident, self._error_handlers(), bg=BG_PANEL, extra=extra
        )

    def _show_notice(
        self, incident: errors.Incident, *, owner: object = None, extra: str = ""
    ) -> None:
        """The non-blocking bar for a problem that didn't stop the user."""
        debuglog.event("ui.notice_shown", code=incident.code, ref=incident.ref)
        self.notice.show(incident, owner=owner, extra=extra)

    def _show_fix_buttons(self, incident: errors.Incident, *, retry: bool) -> None:
        """Preview-row buttons for the selected file's problem."""
        handlers = self._error_handlers()
        action = incident.action
        if action in handlers and action in errors.ACTIONS and action != "retry":
            self.fix_btn.configure(
                text=errors.ACTIONS[action],
                command=handlers[action],
                state="disabled" if self._busy else "normal",
            )
            self.fix_btn.pack(side="left", padx=(8, 0))
        if retry or action == "retry":
            self.retry_btn.pack(side="left", padx=(8, 0))
        self.details_btn.configure(
            text="Copy details",
            command=lambda: self.details_btn.configure(
                text="Copied ✓" if self._to_clipboard(incident.details_text()) else "Clipboard busy"
            ),
        )
        self.details_btn.pack(side="left", padx=(8, 0))

    def _result_incident(self, result: ConversionResult) -> errors.Incident:
        """The incident behind a failed result, rebuilt for display (it was
        logged when the conversion failed)."""
        code = result.error_code or errors.classify_conversion(result.error or "")
        return errors.Incident(
            errors.CATALOG.get(code, errors.CATALOG["MS-199"]),
            result.error_ref or "—",
            result.error or "",
            where=f"convert {result.source.name}",
            context={"file": str(result.source)},
            log_path=debuglog.error_log_path(),
        )

    def _save_incident(self, exc: BaseException, target: Path) -> errors.Incident:
        code = errors.classify_os_error(exc) if isinstance(exc, OSError) else "MS-299"
        return errors.report(code, exc=exc, where="save", target=str(target))

    def _on_tk_error(self, exc_type, exc, tb) -> None:
        """Tk's hook for an exception escaping a callback."""
        try:
            traceback.print_exception(exc_type, exc, tb)  # still visible to a developer
            self._show_error(errors.report("MS-901", exc=exc, where="GUI callback"))
        except Exception:
            pass  # the hook itself must never raise

    def _to_clipboard(self, text: str) -> bool:
        try:
            self.clipboard_clear()
            self.clipboard_append(text)
            return True
        except tk.TclError as exc:
            self._show_notice(errors.report("MS-601", exc=exc, where="clipboard"))
            return False

    def _open_path(self, path: Path) -> None:
        try:
            os.startfile(path)  # noqa: S606 - a folder the user chose or owns
        except OSError as exc:
            self._show_notice(errors.report("MS-602", exc=exc, where="open folder", path=str(path)))

    def open_log_folder(self) -> None:
        folder = debuglog.log_dir()
        folder.mkdir(parents=True, exist_ok=True)
        self._open_path(folder)

    def create_diagnostic_report(self, parent: tk.Misc | None = None) -> None:
        from . import diagnostics

        with debuglog.ui_blocking("report dialog"):
            dest = filedialog.asksaveasfilename(
                parent=parent or self,
                title="Save diagnostic report",
                defaultextension=".zip",
                initialfile=f"MarkdownSidekick-diagnostics-{time.strftime('%Y%m%d-%H%M')}.zip",
                filetypes=[("Zip archive", "*.zip")],
            )
        if not dest:
            return
        report = self._run_blocking(
            lambda: diagnostics.build_report(Path(dest), include_snapshots=self.settings.debug_snapshots),
            "Creating diagnostic report…",
        )
        if report is None:
            self._show_error(
                errors.report("MS-603", exc=self._blocking_error, where="diagnostic report", dest=dest)
            )
            return
        self.status_var.set(f"Diagnostic report saved: {report}")
        if messagebox.askyesno(
            __app_name__,
            f"Diagnostic report saved:\n{report}\n\nOpen its folder?",
            parent=parent or self,
        ):
            self._open_path(Path(report).parent)

    def _save_settings(self, parent: tk.Misc | None = None) -> bool:
        try:
            self.settings.save()
            return True
        except Exception as exc:
            incident = errors.report("MS-502", exc=exc, where="settings save")
            if parent is not None:
                show_error_dialog(parent, incident, {}, bg=BG_PANEL)
            else:
                self._show_notice(incident)
            return False

    def _apply_debug(self) -> None:
        if self.settings.debug_mode:
            if debuglog.enable("gui", snapshots=self.settings.debug_snapshots) is None:
                self.status_var.set("Debug mode couldn't start: the log folder isn't writable.")
            else:
                self.status_var.set(f"Debug mode is on — tracing to {debuglog.session_dir()}")
        elif debuglog.enabled() and not debuglog.debug_requested():
            debuglog.disable()
            self.status_var.set("Debug mode is off. The error log is always kept.")
        self._update_title()

    def _update_title(self) -> None:
        suffix = "  ·  DEBUG MODE" if debuglog.enabled() else ""
        self.title(f"{__app_name__}  ·  v{__version__}{suffix}")

    # -- one-click fixes ---------------------------------------------------------
    def _fix_enable_ocr(self) -> None:
        self.ocr_var.set(ocr_available())
        self.settings.enable_ocr = True
        self.engine.enable_ocr = True
        self._save_settings()
        debuglog.event("ui.fix", action="enable_ocr")
        self._retry_where(("MS-110",))

    def _fix_enable_audio(self) -> None:
        self.settings.enable_audio = True
        self.engine.enable_audio = True
        self._save_settings()
        debuglog.event("ui.fix", action="enable_audio")
        self._retry_where(("MS-115",))

    def _fix_ocr_cpu(self) -> None:
        self.settings.ocr_device = "cpu"
        self.engine.ocr_device = "cpu"
        self._save_settings()
        debuglog.event("ui.fix", action="ocr_cpu")
        self._retry_where(("MS-113", "MS-121", "MS-122"))

    def _fix_raw_preview(self) -> None:
        self.rendered_var.set(False)
        self._refresh_preview()


def run() -> None:
    app = MarkdownSidekickApp()
    app.mainloop()
