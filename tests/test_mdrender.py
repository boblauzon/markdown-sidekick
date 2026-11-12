"""The classic window's preview renderer, drawn into a real Text widget.

It is a deliberate CommonMark *subset*: these tests pin what it shows, and
what it leaves alone on purpose (snake_case, intra-word asterisks, odd link
schemes).
"""

from __future__ import annotations

import tkinter as tk

import pytest

from markdown_sidekick import mdrender
from markdown_sidekick.mdrender import MarkdownRenderer


@pytest.fixture
def text(tk_root):
    widget = tk.Text(tk_root)
    yield widget
    widget.destroy()


@pytest.fixture
def render(text):
    renderer = MarkdownRenderer(text, fg="#111111", muted="#666666", accent="#3344cc", code_bg="#eeeeee")

    def go(markdown: str) -> MarkdownRenderer:
        renderer.render(markdown)
        return renderer

    return go


def shown(widget: tk.Text) -> str:
    return widget.get("1.0", "end-1c")


def tagged(widget: tk.Text, tag: str) -> list[str]:
    """Each run of text carrying ``tag``, in document order."""
    ranges = widget.tag_ranges(tag)
    return [widget.get(a, b) for a, b in zip(ranges[::2], ranges[1::2])]


class TestBlocks:
    def test_headings_lose_their_hashes(self, text, render):
        render("# Title\n## Section\n### Sub\n#### Deep")
        assert shown(text) == "Title\nSection\nSub\nDeep\n"
        assert [tagged(text, f"h{n}")[0].strip() for n in (1, 2, 3, 4)] == ["Title", "Section", "Sub", "Deep"]

    def test_levels_five_and_six_draw_as_the_smallest_heading(self, text, render):
        render("###### Tiny")
        assert tagged(text, "h4") == ["Tiny\n"]

    def test_a_hash_without_a_space_is_not_a_heading(self, text, render):
        render("#hashtag")
        assert shown(text) == "#hashtag\n" and not tagged(text, "h1")

    def test_bullets_and_numbers(self, text, render):
        render("- one\n* two\n• three\n  - nested\n1. first\n2) second")
        assert shown(text) == "•  one\n•  two\n•  three\n  •  nested\n1.  first\n2)  second\n"

    def test_a_block_quote(self, text, render):
        render("> quoted **words**")
        assert shown(text) == "quoted words\n"
        assert "".join(tagged(text, "quote")) == "quoted words\n" and tagged(text, "bold") == ["words"]

    def test_a_table_is_shown_as_typed(self, text, render):
        render("| a | **b** |\n|---|---|")
        assert shown(text) == "| a | **b** |\n|---|---|\n"
        assert "".join(tagged(text, "table")) == shown(text)

    @pytest.mark.parametrize("line", ["---", "___", "- - -", "  -----  "])
    def test_a_rule(self, text, render, line):
        render(line)
        assert tagged(text, "hr") == ["─" * 48 + "\n"]

    def test_stars_alone_are_not_a_rule(self, text, render):
        render("***")
        assert shown(text) == "***\n" and not tagged(text, "hr")

    def test_page_anchors_and_figure_markers_are_placeholders(self, text, render):
        render("<!-- page 3 -->\ntext\n<!-- figure 3.1 -->")
        assert shown(text) == "⟨page 3⟩\ntext\n⟨figure 3.1⟩\n"
        assert tagged(text, "muted") == ["⟨page 3⟩\n", "⟨figure 3.1⟩\n"]

    def test_a_comment_inside_a_line_is_left_alone(self, text, render):
        render("before <!-- note --> after")
        assert shown(text) == "before <!-- note --> after\n"

    def test_blank_lines_are_kept(self, text, render):
        render("a\n\nb")
        assert shown(text) == "a\n\nb\n"


class TestInline:
    def test_bold_italic_and_code(self, text, render):
        render("a **bold** b *slanted* c `x = 1` d")
        assert shown(text) == "a bold b slanted c x = 1 d\n"
        assert tagged(text, "bold") == ["bold"]
        assert tagged(text, "italic") == ["slanted"]
        assert tagged(text, "code_inline") == ["x = 1"]

    @pytest.mark.parametrize(
        "line",
        ["my_snake_case_name", "__init__ and _private_", "2*3*4", "a*b and c*d", "5 * 3 * 2", "**", "* *"],
    )
    def test_literal_text_is_not_emphasis(self, text, render, line):
        render("x " + line)  # "x " keeps a leading "* " from reading as a bullet
        assert shown(text) == "x " + line + "\n"
        assert not tagged(text, "bold") and not tagged(text, "italic")

    def test_markers_inside_inline_code_stay(self, text, render):
        render("`**not bold**`")
        assert shown(text) == "**not bold**\n" and not tagged(text, "bold")

    def test_inline_styles_inside_a_heading_and_a_bullet(self, text, render):
        render("# The `convert` tool\n- a **key** point")
        assert shown(text) == "The convert tool\n•  a key point\n"
        assert tagged(text, "code_inline") == ["convert"] and tagged(text, "bold") == ["key"]


class TestCodeBlocks:
    def test_nothing_inside_a_fence_is_rendered(self, text, render):
        render("```python\n# not a heading\n**kept**\n- item\n<!-- page 2 -->\n```\nafter")
        assert shown(text) == "# not a heading\n**kept**\n- item\n<!-- page 2 -->\nafter\n"
        assert "".join(tagged(text, "code_block")) == "# not a heading\n**kept**\n- item\n<!-- page 2 -->\n"
        assert not tagged(text, "h1") and not tagged(text, "bold") and not tagged(text, "muted")

    def test_a_blank_line_in_code_keeps_its_background(self, text, render):
        render("```\na\n\nb\n```")
        assert "".join(tagged(text, "code_block")) == "a\n \nb\n"

    def test_a_labelled_fence_inside_a_block_is_content(self, text, render):
        render("````\n```python\nprint(1)\n```\n````\nafter")
        assert "".join(tagged(text, "code_block")) == "```python\nprint(1)\n```\n"
        assert shown(text).endswith("after\n")

    def test_a_fence_left_open_runs_to_the_end(self, text, render):
        render("```\ncode\n# still code")
        assert "".join(tagged(text, "code_block")) == "code\n# still code\n" and not tagged(text, "h1")

    def test_inline_triple_backticks_do_not_open_a_block(self, text, render):
        render("```not a fence``` here\n# Heading")
        assert tagged(text, "h1") == ["Heading\n"] and not tagged(text, "code_block")


class TestLinks:
    def test_a_link_shows_its_text_and_opens_its_address(self, text, render, monkeypatch):
        opened = []
        monkeypatch.setattr(mdrender.webbrowser, "open", opened.append)
        renderer = render("See [the guide](https://example.com/guide) and [more](http://example.org).")
        assert shown(text) == "See the guide and more.\n"
        assert tagged(text, "link") == ["the guide", "more"]
        renderer._open_link("href0")
        renderer._open_link("href1")
        assert opened == ["https://example.com/guide", "http://example.org"]

    @pytest.mark.parametrize(
        "address", ["search-ms:query=x", "ms-settings:privacy", "file:///C:/Windows/notepad.exe", "javascript:alert(1)"]
    )
    def test_only_web_addresses_open(self, text, render, monkeypatch, address):
        """A converted document must not get Windows to run something."""
        opened = []
        monkeypatch.setattr(mdrender.webbrowser, "open", opened.append)
        renderer = render(f"[click]({address})")
        assert shown(text) == "click\n"
        renderer._open_link("href0")
        assert opened == []

    def test_a_click_reaches_the_link_under_it(self, text, render, monkeypatch):
        opened = []
        monkeypatch.setattr(mdrender.webbrowser, "open", opened.append)
        render("[one](https://example.com/1) [two](https://example.com/2)")
        assert text.tag_bind("href1", "<Button-1>", None)  # each link has its own binding
        assert set(text.tag_names("1.5")) >= {"link", "href1"}

    def test_an_address_with_brackets_is_kept_whole(self, text, render, monkeypatch):
        opened = []
        monkeypatch.setattr(mdrender.webbrowser, "open", opened.append)
        renderer = render("[Mercury](https://en.wikipedia.org/wiki/Mercury_(planet)) (the planet) and [b](https://b.example)")
        assert shown(text) == "Mercury (the planet) and b\n"
        renderer._open_link("href0")
        assert opened == ["https://en.wikipedia.org/wiki/Mercury_(planet)"]

    def test_a_new_document_forgets_the_old_links(self, text, render, monkeypatch):
        opened = []
        monkeypatch.setattr(mdrender.webbrowser, "open", opened.append)
        render("[old](https://example.com/old)")
        renderer = render("no links here")
        renderer._open_link("href0")
        assert opened == []

    def test_a_link_inside_bold_is_not_parsed(self, text, render):
        """Known limit (USERGUIDE keeps its links outside bold for this reason)."""
        render("**[guide](https://example.com)**")
        assert shown(text) == "[guide](https://example.com)\n" and not tagged(text, "link")


def test_the_preview_is_read_only_and_starts_at_the_top(text, render):
    render("\n".join(f"line {n}" for n in range(500)))
    assert str(text.cget("state")) == "disabled"
    assert text.yview()[0] == 0.0
    render("second")  # rendering again replaces, never appends
    assert shown(text) == "second\n"
