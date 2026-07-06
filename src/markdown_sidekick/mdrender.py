"""A small Markdown renderer for a Tkinter ``Text`` widget.

This is deliberately not a full CommonMark implementation — it renders the
subset that matters for previewing converted documents: headings, bold/italic,
inline code, fenced code blocks, bullet/numbered lists, block quotes, tables,
links and horizontal rules. Rendering is purely visual (tags), so the raw
Markdown remains the source of truth for copy/save.
"""

from __future__ import annotations

import re
import tkinter as tk
import webbrowser

# Inline spans, matched left-to-right. Underscore emphasis is intentionally
# omitted — snake_case identifiers are everywhere in technical text and would
# misrender. Emphasis delimiters must sit at a non-word boundary so intra-word
# asterisks ("a*b", "2*3") are left literal; bold is matched before italic.
_INLINE = re.compile(
    r"(?<!\w)\*\*(?=\S).+?(?<=\S)\*\*(?!\w)"          # bold
    r"|`[^`]+`"                                        # inline code
    r"|\[[^\]]+\]\([^)]+\)"                            # link
    r"|(?<![\w*])\*(?=\S)[^*\n]+?(?<=\S)\*(?![\w*])"   # italic
)
_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET = re.compile(r"^(\s*)[-*•]\s+(.*)$")
_NUMBERED = re.compile(r"^(\s*)(\d+[.)])\s+(.*)$")
# Thematic break. Only '-' and '_' runs — '*' is left to the emphasis parser so
# a stray '***' isn't turned into a rule.
_HR = re.compile(r"^\s*([-_])(\s*\1){2,}\s*$")
_BLOCKQUOTE = re.compile(r"^\s*>\s?(.*)$")
# A fenced-code marker: 3+ backticks at line start.
_FENCE = re.compile(r"^(\s*)(`{3,})(.*)$")


class MarkdownRenderer:
    """Configures display tags on a Text widget and renders Markdown into it."""

    def __init__(
        self,
        widget: tk.Text,
        *,
        fg: str,
        muted: str,
        accent: str,
        code_bg: str,
        base_family: str = "Segoe UI",
        mono_family: str = "Cascadia Code",
        base_size: int = 10,
    ) -> None:
        self.w = widget
        # url-per-link-tag map; rebuilt on every render().
        self._links: dict[str, str] = {}
        self._configure_tags(fg, muted, accent, code_bg, base_family, mono_family, base_size)

    def _configure_tags(self, fg, muted, accent, code_bg, base, mono, size) -> None:
        w = self.w
        w.tag_configure("h1", font=(base, size + 9, "bold"), foreground=fg, spacing3=8, spacing1=10)
        w.tag_configure("h2", font=(base, size + 5, "bold"), foreground=fg, spacing3=6, spacing1=8)
        w.tag_configure("h3", font=(base, size + 2, "bold"), foreground=fg, spacing3=4, spacing1=6)
        w.tag_configure("h4", font=(base, size + 1, "bold"), foreground=muted, spacing3=3, spacing1=4)
        w.tag_configure("bold", font=(base, size, "bold"))
        w.tag_configure("italic", font=(base, size, "italic"))
        w.tag_configure("code_inline", font=(mono, size), background=code_bg, foreground=accent)
        w.tag_configure(
            "code_block",
            font=(mono, size),
            background=code_bg,
            lmargin1=18,
            lmargin2=18,
            spacing1=1,
            spacing3=1,
        )
        w.tag_configure("bullet", lmargin1=20, lmargin2=36, spacing3=2)
        w.tag_configure("table", font=(mono, size - 1), foreground=muted)
        w.tag_configure("quote", lmargin1=18, lmargin2=18, foreground=muted, font=(base, size, "italic"))
        w.tag_configure("link", foreground=accent, underline=True)
        w.tag_configure("hr", foreground=muted, justify="center", spacing1=4, spacing3=6)
        w.tag_configure("muted", foreground=muted)

    # -- links ----------------------------------------------------------------
    def _open_link(self, tag: str) -> None:
        url = self._links.get(tag, "")
        if url.startswith(("http://", "https://")):  # never shell out to odd schemes
            webbrowser.open(url)

    def _add_link(self, text: str, url: str, base_tags: tuple[str, ...]) -> None:
        w = self.w
        tag = f"href{len(self._links)}"
        self._links[tag] = url
        w.insert("end", text, base_tags + ("link", tag))
        w.tag_bind(tag, "<Button-1>", lambda _e, t=tag: self._open_link(t))
        w.tag_bind(tag, "<Enter>", lambda _e: w.configure(cursor="hand2"))
        w.tag_bind(tag, "<Leave>", lambda _e: w.configure(cursor=""))

    # -- rendering -----------------------------------------------------------
    def render(self, markdown: str) -> None:
        w = self.w
        w.configure(state="normal")
        w.delete("1.0", "end")
        self._links.clear()
        in_code = False
        fence_len = 0
        for line in markdown.split("\n"):
            m = _FENCE.match(line)
            if not in_code:
                # Open only on a fence line with no extra backticks after the run
                # (an info string is fine, e.g. ```python).
                if m and "`" not in m.group(3):
                    in_code = True
                    fence_len = len(m.group(2))
                    continue
                self._render_line(line)
            else:
                # Close only on a bare fence of at least the opening length; a line
                # like ```python *inside* a block is content, not a close.
                if m and len(m.group(2)) >= fence_len and m.group(3).strip() == "":
                    in_code = False
                    fence_len = 0
                    continue
                w.insert("end", (line or " ") + "\n", ("code_block",))
        w.configure(state="disabled")
        w.yview_moveto(0.0)

    def _render_line(self, line: str) -> None:
        w = self.w
        if line.strip() == "":
            w.insert("end", "\n")
            return
        if _HR.match(line):
            w.insert("end", "─" * 48 + "\n", ("hr",))
            return
        m = _HEADING.match(line)
        if m:
            level = min(len(m.group(1)), 4)
            self._render_inline(m.group(2), base_tags=(f"h{level}",))
            w.insert("end", "\n", (f"h{level}",))
            return
        m = _BLOCKQUOTE.match(line)
        if m:
            self._render_inline(m.group(1), base_tags=("quote",))
            w.insert("end", "\n", ("quote",))
            return
        if line.strip().startswith("|"):
            w.insert("end", line + "\n", ("table",))
            return
        m = _BULLET.match(line)
        if m:
            w.insert("end", m.group(1) + "•  ", ("bullet",))
            self._render_inline(m.group(2), base_tags=("bullet",))
            w.insert("end", "\n", ("bullet",))
            return
        m = _NUMBERED.match(line)
        if m:
            w.insert("end", f"{m.group(1)}{m.group(2)}  ", ("bullet",))
            self._render_inline(m.group(3), base_tags=("bullet",))
            w.insert("end", "\n", ("bullet",))
            return
        self._render_inline(line)
        w.insert("end", "\n")

    def _render_inline(self, text: str, base_tags: tuple[str, ...] = ()) -> None:
        w = self.w
        pos = 0
        for m in _INLINE.finditer(text):
            if m.start() > pos:
                w.insert("end", text[pos : m.start()], base_tags)
            tok = m.group(0)
            if tok.startswith("**"):
                w.insert("end", tok[2:-2], base_tags + ("bold",))
            elif tok.startswith("`"):
                w.insert("end", tok[1:-1], base_tags + ("code_inline",))
            elif tok.startswith("["):
                link = _LINK.match(tok)
                if link:
                    self._add_link(link.group(1), link.group(2), base_tags)
                else:
                    w.insert("end", tok, base_tags + ("link",))
            else:  # *italic*
                w.insert("end", tok[1:-1], base_tags + ("italic",))
            pos = m.end()
        if pos < len(text):
            w.insert("end", text[pos:], base_tags)
