"""Tkinter GUI for Markdown Sidekick."""

from __future__ import annotations

import queue
import threading
import webbrowser
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import __app_name__, __version__
from .cleanup import clean_markdown
from .converter import (
    SUPPORTED_EXTENSIONS,
    ConversionEngine,
    ConversionResult,
    default_output_path,
)
from . import export as md_export
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
ACCENT = "#6c8cff"
ACCENT_ACTIVE = "#5577f0"
OK_COLOR = "#5ad19a"
ERR_COLOR = "#ff6b81"
FONT = ("Segoe UI", 10)
FONT_BOLD = ("Segoe UI", 10, "bold")
FONT_TITLE = ("Segoe UI Semibold", 16)
FONT_MONO = ("Cascadia Code", 10) if True else ("Consolas", 10)


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
        self.engine = ConversionEngine(
            enable_ocr=self.settings.enable_ocr,
            enable_audio=self.settings.enable_audio,
            whisper_model=self.settings.whisper_model,
            mineru_endpoint=self.settings.mineru_endpoint,
            page_anchors=self.settings.page_anchors,
        )
        # Ordered mapping of source path -> result (None until converted).
        self.files: dict[Path, ConversionResult | None] = {}
        self._events: "queue.Queue[tuple]" = queue.Queue()
        self._busy = False
        # Cleaned-output cache + the text currently shown (for copy/save).
        self._clean_cache: dict[Path, str] = {}
        self._clean_stats: dict[Path, object] = {}
        self._current_export_text = ""
        # OCR toggle reflects the saved setting (and dep availability).
        self.ocr_var = tk.BooleanVar(value=self.settings.enable_ocr and ocr_available())
        self._help_win: tk.Toplevel | None = None

        self._build_style()
        self._build_layout()
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
            background=ACCENT,
            foreground="#ffffff",
            font=FONT_BOLD,
            borderwidth=0,
            focuscolor=ACCENT,
            padding=(14, 8),
        )
        style.map(
            "Accent.TButton",
            background=[("active", ACCENT_ACTIVE), ("disabled", "#3a3c4a")],
            foreground=[("disabled", FG_MUTED)],
        )

        style.configure(
            "TButton",
            background=BG_INPUT,
            foreground=FG,
            font=FONT,
            borderwidth=0,
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
        style.map("Treeview", background=[("selected", ACCENT)], foreground=[("selected", "#fff")])
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

    # -- layout --------------------------------------------------------------
    def _build_layout(self) -> None:
        # Header
        header = ttk.Frame(self, style="TFrame")
        header.pack(fill="x", padx=20, pady=(16, 8))
        ttk.Label(header, text="Markdown Sidekick", style="Title.TLabel").pack(side="left")
        ttk.Label(
            header,
            text="Convert documents, images & audio to Markdown — powered by Microsoft markitdown",
            style="Muted.TLabel",
        ).pack(side="left", padx=(14, 0), pady=(6, 0))
        ttk.Button(header, text="⚙  Settings", command=self.open_settings).pack(
            side="right", pady=(2, 0)
        )
        ttk.Button(header, text="☕  Support", command=self.open_kofi).pack(
            side="right", padx=(0, 8), pady=(2, 0)
        )
        ttk.Button(header, text="❓  User Guide", command=self.open_help).pack(
            side="right", padx=(0, 8), pady=(2, 0)
        )

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
        tree.column("#0", width=196, anchor="w")
        tree.column("status", width=104, anchor="center", stretch=False)
        tree.grid(row=2, column=0, sticky="nsew")
        tree.tag_configure("ok", foreground=OK_COLOR)
        tree.tag_configure("err", foreground=ERR_COLOR)
        tree.tag_configure("pending", foreground=FG_MUTED)
        tree.bind("<<TreeviewSelect>>", self._on_select_file)
        self.tree = tree

        scroll = ttk.Scrollbar(panel, orient="vertical", command=tree.yview)
        scroll.grid(row=2, column=1, sticky="ns")
        tree.configure(yscrollcommand=scroll.set)

        hint = "Drag & drop files here" if _DND_AVAILABLE else "Use “Add files…” to begin"
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
            text, fg=FG, muted=FG_MUTED, accent=ACCENT, code_bg="#15161e"
        )

        pscroll = ttk.Scrollbar(panel, orient="vertical", command=text.yview)
        pscroll.grid(row=1, column=1, sticky="ns")
        text.configure(yscrollcommand=pscroll.set)

        actions = ttk.Frame(panel, style="Panel.TFrame")
        actions.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        ttk.Button(actions, text="Copy", command=self.copy_preview).pack(side="left")
        ttk.Button(actions, text="Save as…", command=self.save_selected).pack(
            side="left", padx=6
        )

    def _build_footer(self) -> None:
        footer = ttk.Frame(self, style="TFrame")
        footer.pack(side="bottom", fill="x", padx=20, pady=(0, 16))

        self.progress = ttk.Progressbar(
            footer, style="Horizontal.TProgressbar", mode="determinate"
        )
        self.progress.pack(fill="x", pady=(0, 10))

        row = ttk.Frame(footer, style="TFrame")
        row.pack(fill="x")

        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(row, textvariable=self.status_var, style="Muted.TLabel").pack(side="left")

        self.convert_btn = ttk.Button(
            row, text="Convert all", style="Accent.TButton", command=self.convert_all
        )
        self.convert_btn.pack(side="right")
        self.save_all_btn = ttk.Button(
            row, text="Save all…", command=self.save_all
        )
        self.save_all_btn.pack(side="right", padx=(0, 8))

    # -- settings ------------------------------------------------------------
    def open_settings(self) -> None:
        if self._busy:
            messagebox.showinfo(__app_name__, "Finish the current conversion first.")
            return
        dlg = tk.Toplevel(self)
        dlg.title("Settings")
        dlg.configure(bg=BG_PANEL)
        dlg.transient(self)
        dlg.resizable(False, False)
        pad = {"padx": 16}

        # master=dlg so these Tcl variables are freed when the dialog is destroyed
        # (otherwise they accumulate on the root across opens).
        ocr_v = tk.BooleanVar(dlg, value=self.settings.enable_ocr)
        audio_v = tk.BooleanVar(dlg, value=self.settings.enable_audio)
        clean_v = tk.BooleanVar(dlg, value=self.settings.clean_output)
        rendered_v = tk.BooleanVar(dlg, value=self.settings.rendered_preview)
        model_v = tk.StringVar(dlg, value=self.settings.whisper_model)
        endpoint_v = tk.StringVar(dlg, value=self.settings.mineru_endpoint)
        outdir_v = tk.StringVar(dlg, value=self.settings.default_output_dir)
        front_v = tk.BooleanVar(dlg, value=self.settings.export_front_matter)
        split_v = tk.BooleanVar(dlg, value=self.settings.split_chapters)
        anchors_v = tk.BooleanVar(dlg, value=self.settings.page_anchors)
        images_v = tk.BooleanVar(dlg, value=self.settings.extract_images)
        ollama_v = tk.StringVar(dlg, value=self.settings.ollama_endpoint)
        polish_v = tk.StringVar(dlg, value=self.settings.polish_model)
        caption_v = tk.StringVar(dlg, value=self.settings.caption_model)

        def section(text: str, row: int) -> None:
            ttk.Label(dlg, text=text, style="Panel.TLabel", font=FONT_BOLD).grid(
                row=row, column=0, columnspan=3, sticky="w", pady=(14, 4), **pad
            )

        def check(text: str, var, row: int) -> None:
            ttk.Checkbutton(dlg, text=text, variable=var, style="Panel.TCheckbutton").grid(
                row=row, column=0, columnspan=3, sticky="w", **pad
            )

        ttk.Label(dlg, text="Markdown Sidekick settings", style="Panel.TLabel",
                  font=FONT_TITLE).grid(row=0, column=0, columnspan=3, sticky="w",
                                        pady=(16, 0), **pad)

        section("Conversion engines", 1)
        check("OCR images & scanned PDFs", ocr_v, 2)
        check("Transcribe audio files", audio_v, 3)

        ttk.Label(dlg, text="Whisper model", style="Panel.TLabel").grid(
            row=4, column=0, sticky="w", pady=(8, 0), **pad
        )
        ttk.Combobox(dlg, textvariable=model_v, values=list(WHISPER_MODELS),
                     state="readonly", width=12).grid(row=4, column=1, sticky="w", pady=(8, 0))

        section("High-fidelity (optional)", 5)
        ttk.Label(dlg, text="MinerU endpoint URL", style="Panel.TLabel").grid(
            row=6, column=0, sticky="w", **pad
        )
        ttk.Entry(dlg, textvariable=endpoint_v, width=38).grid(
            row=6, column=1, columnspan=2, sticky="w"
        )
        ttk.Label(dlg, text="e.g. http://127.0.0.1:2364  (blank = off)",
                  style="PanelMuted.TLabel").grid(row=7, column=1, columnspan=2, sticky="w")

        section("Output", 8)
        ttk.Label(dlg, text="Default output folder", style="Panel.TLabel").grid(
            row=9, column=0, sticky="w", **pad
        )
        ttk.Entry(dlg, textvariable=outdir_v, width=30).grid(row=9, column=1, sticky="w")

        def browse() -> None:
            d = filedialog.askdirectory(title="Default output folder", parent=dlg)
            if d:
                outdir_v.set(d)

        ttk.Button(dlg, text="Browse…", command=browse).grid(row=9, column=2, sticky="w", padx=6)

        section("Preview defaults", 10)
        check("Clean output", clean_v, 11)
        check("Rendered preview", rendered_v, 12)

        section("AI-friendly export", 13)
        check("YAML front matter on saved files", front_v, 14)
        check("Save all: split books into chapter folders", split_v, 15)
        check("Page anchors (<!-- page N -->) in PDF conversions", anchors_v, 16)
        check("Extract PDF figures to assets/ on save", images_v, 17)

        section("Local LLM extras (optional, via Ollama)", 18)
        ttk.Label(dlg, text="Ollama endpoint", style="Panel.TLabel").grid(
            row=19, column=0, sticky="w", **pad
        )
        ttk.Entry(dlg, textvariable=ollama_v, width=38).grid(
            row=19, column=1, columnspan=2, sticky="w"
        )
        ttk.Label(dlg, text="Polish model / caption model", style="Panel.TLabel").grid(
            row=20, column=0, sticky="w", **pad
        )
        ttk.Entry(dlg, textvariable=polish_v, width=14).grid(row=20, column=1, sticky="w")
        ttk.Entry(dlg, textvariable=caption_v, width=14).grid(
            row=20, column=2, sticky="w", padx=6
        )
        ttk.Label(
            dlg,
            text="e.g. http://localhost:11434 with llama3.2 / llava  (blank = off)",
            style="PanelMuted.TLabel",
        ).grid(row=21, column=1, columnspan=2, sticky="w")

        section("AI integration (MCP)", 22)
        ttk.Button(
            dlg,
            text="📋  Copy AI setup prompt",
            command=lambda: self.copy_mcp_prompt(parent=dlg),
        ).grid(row=23, column=0, sticky="w", padx=16)
        ttk.Label(
            dlg,
            text="Paste it into Claude, Cursor, or any AI assistant\nto connect Markdown Sidekick as a converter tool.",
            style="PanelMuted.TLabel",
            justify="left",
        ).grid(row=23, column=1, columnspan=2, sticky="w", padx=(8, 16))

        def save() -> None:
            self.settings.enable_ocr = ocr_v.get()
            self.settings.enable_audio = audio_v.get()
            self.settings.whisper_model = model_v.get()
            self.settings.mineru_endpoint = endpoint_v.get()
            self.settings.default_output_dir = outdir_v.get()
            self.settings.clean_output = clean_v.get()
            self.settings.rendered_preview = rendered_v.get()
            self.settings.export_front_matter = front_v.get()
            self.settings.split_chapters = split_v.get()
            self.settings.page_anchors = anchors_v.get()
            self.settings.extract_images = images_v.get()
            self.settings.ollama_endpoint = ollama_v.get()
            self.settings.polish_model = polish_v.get()
            self.settings.caption_model = caption_v.get()
            self.settings.save()
            self._apply_settings()
            dlg.destroy()

        btns = ttk.Frame(dlg, style="Panel.TFrame")
        btns.grid(row=24, column=0, columnspan=3, sticky="e", pady=16, padx=16)
        ttk.Button(btns, text="Cancel", command=dlg.destroy).pack(side="right", padx=(8, 0))
        ttk.Button(btns, text="Save", style="Accent.TButton", command=save).pack(side="right")

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
        self.engine.enable_audio = s.enable_audio
        self.engine.whisper_model = s.whisper_model
        self.engine.mineru_endpoint = s.mineru_endpoint
        self.engine.page_anchors = s.page_anchors
        self.ocr_var.set(s.enable_ocr and ocr_available())
        self.clean_var.set(s.clean_output)
        self.rendered_var.set(s.rendered_preview)
        self._clean_cache.clear()
        self._clean_stats.clear()
        self._on_select_file()

    # -- help / support --------------------------------------------------------
    def open_kofi(self) -> None:
        webbrowser.open(KOFI_URL)
        self.status_var.set("Thank you for supporting Markdown Sidekick! ☕")

    def copy_mcp_prompt(self, parent: tk.Misc | None = None) -> None:
        """Copy a paste-into-any-AI prompt that sets up this install's MCP server."""
        self.clipboard_clear()
        self.clipboard_append(build_mcp_setup_prompt())
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
            text, fg=FG, muted=FG_MUTED, accent=ACCENT, code_bg="#15161e"
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
        paths = filedialog.askopenfilenames(
            title="Select files to convert",
            filetypes=[("Supported files", patterns), ("All files", "*.*")],
        )
        self._add_paths(paths)

    def _add_paths(self, paths) -> None:
        added = 0
        for raw in paths:
            path = Path(raw)
            if not path.is_file() or path in self.files:
                continue
            self.files[path] = None
            self.tree.insert(
                "", "end", iid=str(path), text=path.name, values=("pending",), tags=("pending",)
            )
            added += 1
        if added:
            self.status_var.set(f"Added {added} file(s). {len(self.files)} ready to convert.")

    def remove_selected(self) -> None:
        for iid in self.tree.selection():
            path = Path(iid)
            self.files.pop(path, None)
            self._clean_cache.pop(path, None)
            self._clean_stats.pop(path, None)
            self.tree.delete(iid)
        self._clear_preview()

    def clear_files(self) -> None:
        self.files.clear()
        self._clean_cache.clear()
        self._clean_stats.clear()
        self.tree.delete(*self.tree.get_children())
        self._clear_preview()
        self.progress["value"] = 0
        self.status_var.set("Ready.")

    def _on_drop(self, event) -> None:
        # tkinterdnd2 returns a brace-wrapped, space-joined string of paths.
        paths = self.tk.splitlist(event.data)
        self._add_paths(paths)

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
            cleaned, stats = clean_markdown(result.markdown)
            self._clean_cache[path] = cleaned
            self._clean_stats[path] = stats
        return self._clean_cache[path]

    def _on_select_file(self, _event=None) -> None:
        path = self._selected_path()
        if path is None:
            return
        result = self.files.get(path)
        if result is None:
            self._current_export_text = ""  # nothing real to copy/save
            self._show_plain(path.name, "Not converted yet — click “Convert all”.", store=False)
        elif not result.ok:
            self._current_export_text = ""
            self._show_plain(path.name, f"⚠ Conversion failed:\n\n{result.error}", store=False)
        else:
            text = self._export_text_for(path) or "(Empty output.)"
            self._current_export_text = text
            self.preview_name.configure(text=f"{path.name}   ·   {result.engine}")
            if self.rendered_var.get():
                self._renderer.render(text)
            else:
                self._show_plain(path.name, text, store=False)
            status_bits = []
            if self.clean_var.get() and path in self._clean_stats:
                stats = self._clean_stats[path]
                if getattr(stats, "changed", False):
                    status_bits.append(stats.summary())
            status_bits.append(assess_markdown(text).summary())
            self.status_var.set("   ·   ".join(status_bits))

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
        self._current_export_text = ""
        self.preview_name.configure(text="")
        self.preview.configure(state="normal")
        self.preview.delete("1.0", "end")
        self.preview.configure(state="disabled")

    def copy_preview(self) -> None:
        content = self._current_export_text
        if not content.strip():
            return
        self.clipboard_clear()
        self.clipboard_append(content)
        self.status_var.set("Markdown copied to clipboard.")

    # -- conversion ----------------------------------------------------------
    def convert_all(self) -> None:
        if self._busy:
            return
        if not self.files:
            messagebox.showinfo(__app_name__, "Add some files first.")
            return
        self._set_busy(True)
        self._clean_cache.clear()
        self._clean_stats.clear()
        # Apply the OCR toggle on the main thread before the worker starts.
        self.engine.enable_ocr = self.ocr_var.get()
        targets = list(self.files.keys())
        self.progress.configure(maximum=len(targets), value=0)
        self.status_var.set("Converting…")

        thread = threading.Thread(target=self._worker_convert, args=(targets,), daemon=True)
        thread.start()

    def _worker_convert(self, targets: list[Path]) -> None:
        def on_progress(index: int, total: int, result: ConversionResult) -> None:
            self._events.put(("progress", index, total, result))

        def on_subprogress(source: Path, current: float, total: float, unit: str) -> None:
            self._events.put(("subprogress", source, current, total, unit))

        self.engine.convert_many(
            targets, on_progress=on_progress, on_subprogress=on_subprogress
        )
        self._events.put(("done",))

    def _poll_events(self) -> None:
        try:
            while True:
                event = self._events.get_nowait()
                self._handle_event(event)
        except queue.Empty:
            pass
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
            tag = "ok" if result.ok else "err"
            status = result.engine if result.ok else "error"
            iid = str(result.source)
            if self.tree.exists(iid):
                self.tree.item(iid, values=(status,), tags=(tag,))
            self.progress["value"] = index
            self.status_var.set(f"Converting… {index}/{total}  ({result.title})")
            if self.tree.selection() and self.tree.selection()[0] == iid:
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

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        state = "disabled" if busy else "normal"
        # Lock file-list mutation while a conversion is in flight, so a worker
        # result can't resurrect a removed/cleared file.
        for widget in (
            self.convert_btn,
            self.save_all_btn,
            self.add_btn,
            self.remove_btn,
            self.clear_btn,
        ):
            widget.configure(state=state)

    # -- saving --------------------------------------------------------------
    def save_selected(self) -> None:
        path = self._selected_path()
        if path is None:
            messagebox.showinfo(__app_name__, "Select a file in the list first.")
            return
        text = self._export_text_for(path)
        if text is None:
            messagebox.showwarning(
                __app_name__, "That file hasn't been converted successfully yet."
            )
            return
        dest = filedialog.asksaveasfilename(
            title="Save Markdown",
            defaultextension=".md",
            initialfile=f"{path.stem}.md",
            filetypes=[("Markdown", "*.md"), ("All files", "*.*")],
        )
        if not dest:
            return
        result = self.files.get(path)
        md_export.export_single(
            text,
            Path(dest),
            source=path.name,
            engine=result.engine if result else "",
            front_matter=self.settings.export_front_matter,
        )
        self.status_var.set(f"Saved {Path(dest).name}.")

    def save_all(self) -> None:
        converted = [p for p, r in self.files.items() if r and r.ok]
        if not converted:
            messagebox.showinfo(__app_name__, "Nothing converted yet.")
            return
        # Use the configured default folder if it still exists; otherwise ask.
        default_dir = self.settings.default_output_dir
        if default_dir and Path(default_dir).is_dir():
            out_dir = default_dir
        else:
            out_dir = filedialog.askdirectory(title="Choose output folder")
        if not out_dir:
            return
        out = Path(out_dir)
        saved = 0
        used_names: set[str] = set()
        for path in converted:
            text = self._export_text_for(path)
            if text is None:
                continue
            result = self.files.get(path)
            engine = result.engine if result else ""
            # Optional AI-friendly extras (all controlled from Settings).
            if self.settings.extract_images and path.suffix.lower() == ".pdf":
                from . import figures

                book_dir = out / path.stem if self.settings.split_chapters else out
                figs = figures.extract_pdf_figures(path, book_dir / "assets")
                if figs:
                    text = figures.insert_figure_links(text, figs)
            if self.settings.split_chapters:
                res = md_export.export_book(
                    text,
                    out / path.stem,
                    source=path.name,
                    engine=engine,
                    front_matter=self.settings.export_front_matter,
                )
                saved += len(res.paths)
                continue
            dest = default_output_path(path, out)
            # Disambiguate same-stem files from different folders so we never
            # silently overwrite one output with another.
            if dest.name in used_names:
                n = 2
                while (candidate := out / f"{path.stem}-{n}.md").name in used_names:
                    n += 1
                dest = candidate
            used_names.add(dest.name)
            md_export.export_single(
                text,
                dest,
                source=path.name,
                engine=engine,
                front_matter=self.settings.export_front_matter,
            )
            saved += 1
        mode = "cleaned" if self.clean_var.get() else "raw"
        self.status_var.set(f"Saved {saved} {mode} Markdown file(s) to {out.name}.")
        messagebox.showinfo(__app_name__, f"Saved {saved} file(s) to:\n{out}")


def run() -> None:
    app = MarkdownSidekickApp()
    app.mainloop()
