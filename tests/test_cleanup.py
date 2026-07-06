"""Tests for the cleanup passes, built from artifact patterns observed in a
real 25-book library of PDF→Markdown conversions (Packt technical books)."""

from __future__ import annotations

import textwrap

from markdown_sidekick.cleanup import (
    CleanupStats,
    clean_markdown,
    fence_code_blocks,
    join_wrapped_lines,
    normalize_bullets,
    normalize_characters,
    promote_chapter_headings,
    repair_fences,
    strip_page_noise,
    strip_plain_toc,
    strip_repeated_blocks,
    _guess_language,
    _harvest_section_titles,
)


def _clean(text: str, **kw) -> str:
    return clean_markdown(text, **kw)[0]


# ---------------------------------------------------------------------------
# character normalization
# ---------------------------------------------------------------------------
class TestNormalizeCharacters:
    def test_ligatures_decomposed(self):
        stats = CleanupStats()
        out = normalize_characters("deﬁnition oﬀers ﬂexibility diﬃcult", stats)
        assert out == "definition offers flexibility difficult"
        assert stats.chars_normalized == 4

    def test_soft_hyphen_and_nbsp(self):
        stats = CleanupStats()
        out = normalize_characters("co­operate a b", stats)
        assert out == "cooperate a b"

    def test_replacement_runs_scrubbed_end_to_end(self):
        out = _clean("word �� word\n")
        assert "�" not in out
        # A lone replacement char marks one lost character — kept.
        out = _clean("wo�d\n")
        assert "wo�d" in out


# ---------------------------------------------------------------------------
# page noise (bare numbers + roman numerals)
# ---------------------------------------------------------------------------
class TestPageNoise:
    def test_bare_roman_numeral_pages_removed(self):
        lines = ["Table of Contents"]
        for numeral in ("viii", "ix", "xvii", "xxiv"):
            lines += ["prose paragraph here.", numeral]
        stats = CleanupStats()
        out = strip_page_noise("\n".join(lines), stats)
        for numeral in ("viii", "ix", "xvii", "xxiv"):
            assert f"\n{numeral}" not in out
        assert stats.removed_noise_lines == 4

    def test_roman_lookalike_words_kept(self):
        text = "\n".join(["mild", "did", "civil", "mix", "prose."])
        stats = CleanupStats()
        out = strip_page_noise(text, stats)
        # Only strict roman numerals with enough repeats are candidates;
        # ordinary words must survive.
        for word in ("mild", "did", "civil"):
            assert word in out

    def test_single_roman_line_kept(self):
        text = "start\nxvii\nend"
        stats = CleanupStats()
        assert "xvii" in strip_page_noise(text, stats)


# ---------------------------------------------------------------------------
# plain-text TOC stripping
# ---------------------------------------------------------------------------
_PLAIN_TOC = textwrap.dedent(
    """\
    Table of Contents

    Preface

     xvii

    Chapter 1: Clean Architecture Essentials    3

    Technical requirements  ���������������������������� 4

    Why Clean Architecture  ���������������������������� 4

    The complexity challenge • 5

    The agility imperative • 6

    What is Clean Architecture?  ���������������������������� 8

    The onion architecture concept • 9

    Summary  ���������������������������� 22

    Further reading  ���������������������������� 23

    Chapter 2: SOLID Foundations

    Understanding single responsibility • 26

    SRP and testing • 30

    Real prose stays: this paragraph is a long full sentence that carries meaning and ends with a period.
    """
)


class TestPlainToc:
    def test_toc_block_removed_prose_kept(self):
        stats = CleanupStats()
        out = strip_plain_toc(_PLAIN_TOC, stats)
        assert "Technical requirements" not in out
        assert "The agility imperative" not in out
        assert "Understanding single responsibility" not in out
        assert "Real prose stays" in out
        assert stats.toc_lines_removed > 10

    def test_interstitial_skeleton_removed(self):
        stats = CleanupStats()
        out = strip_plain_toc(_PLAIN_TOC, stats)
        # Chapter/Part skeleton lines inside the TOC cluster go too.
        assert "Chapter 1: Clean Architecture Essentials" not in out

    def test_dot_leader_form(self):
        lines = ["Table of Contents"]
        for i in range(9):
            lines.append(f"Section number {i} .......... {i + 3}")
        lines.append("Prose sentence that is definitely not a table of contents entry, and long.")
        stats = CleanupStats()
        out = strip_plain_toc("\n".join(lines), stats)
        assert "Section number 4" not in out
        assert "Prose sentence" in out

    def test_body_bullets_never_stripped(self):
        # "• text" list items (bullet FIRST) are content, not TOC entries.
        body = "\n".join(["•  Point one about design", "•  Point two about tests"] * 6)
        stats = CleanupStats()
        assert strip_plain_toc(body, stats) == body

    def test_small_documents_untouched(self):
        text = "A heading\n\nSome text • 5\n\nMore text ..... 9\n"
        stats = CleanupStats()
        assert strip_plain_toc(text, stats) == text  # below the signal gate


# ---------------------------------------------------------------------------
# chapter heading promotion + running headers
# ---------------------------------------------------------------------------
_BOOK = textwrap.dedent(
    """\
    Chapter 1: Clean Architecture Essentials: Transforming Python Development    3
    Chapter 2: SOLID Foundations: Building Robust Python Applications
    Chapter 3: Type-Enhanced Python: Strengthening Clean Architecture

    Some preface prose that is long enough to be a real sentence, ending properly.

    SOLID Foundations: Building
    Robust Python Applications

    In the previous chapter, we explored Clean Architecture in detail.

    SOLID Foundations: Building Robust Python Applications

    More body prose follows the running header on the next printed page.

    SOLID Foundations: Building Robust Python Applications

    Even more prose.
    """
)


class TestChapterHeadings:
    def test_harvest(self):
        titles = _harvest_section_titles(_BOOK)
        assert (
            titles["SOLID Foundations: Building Robust Python Applications"]
            == "Chapter 2: SOLID Foundations: Building Robust Python Applications"
        )
        assert len(titles) == 3

    def test_wrapped_opening_promoted_and_running_headers_removed(self):
        titles = _harvest_section_titles(_BOOK)
        stats = CleanupStats()
        out = promote_chapter_headings(_BOOK, titles, stats)
        assert "# Chapter 2: SOLID Foundations: Building Robust Python Applications" in out
        # Both single-line repeats (running headers) are gone; only the "# " and
        # "Chapter 2:" prefixed lines still carry the title text.
        bare = "SOLID Foundations: Building Robust Python Applications"
        assert not any(ln.strip() == bare for ln in out.split("\n"))
        assert "SOLID Foundations: Building\nRobust Python Applications" not in out
        assert stats.headings_promoted == 1
        assert stats.removed_noise_lines == 2

    def test_no_promotion_when_chapter_headings_exist(self):
        doc = (
            "# SOLID Foundations: Building Robust Python Applications\n"
            "# Clean Architecture Essentials: Transforming Python Development\n"
            + _BOOK
        )
        titles = _harvest_section_titles(doc)
        stats = CleanupStats()
        out = promote_chapter_headings(doc, titles, stats)
        assert stats.headings_promoted == 0
        # Bare titles removed entirely as running headers.
        assert "SOLID Foundations: Building\nRobust Python Applications" not in out

    def test_too_few_chapters_is_inert(self):
        doc = "Chapter 1: Only One Chapter Here\n\nOnly One Chapter Here\n"
        titles = _harvest_section_titles(doc)
        stats = CleanupStats()
        assert promote_chapter_headings(doc, titles, stats) == doc


# ---------------------------------------------------------------------------
# repeated boilerplate blocks
# ---------------------------------------------------------------------------
class TestRepeatedBlocks:
    def test_qr_block_removed(self):
        block = (
            "Get this book's PDF version and more\n"
            "Scan the QR code (or go to packtpub.com/unlock). Search for this book by name, confirm the\n"
            "edition, and then follow the steps on the page.\n"
        )
        chapters = []
        for i in range(4):
            chapters.append(
                f"Chapter prose number {i} is a unique sentence about a unique topic entirely.\n\n"
                + block
            )
        stats = CleanupStats()
        out = strip_repeated_blocks("\n".join(chapters), stats)
        assert "Scan the QR code" not in out
        assert "Chapter prose number 2" in out

    def test_repeated_sentence_with_unique_neighbours_kept(self):
        parts = []
        for i in range(5):
            parts.append(f"Unique paragraph {i} that differs every single time it appears here.")
            parts.append("The output is as follows and shown below:")
        text = "\n\n".join(parts)
        stats = CleanupStats()
        out = strip_repeated_blocks(text, stats)
        assert out.count("The output is as follows") == 5

    def test_code_inside_fences_untouched(self):
        fenced = "```python\nx = 1\ny = 2\nx = 1\ny = 2\nx = 1\ny = 2\nx = 1\ny = 2\n```"
        stats = CleanupStats()
        assert strip_repeated_blocks(fenced, stats) == fenced


# ---------------------------------------------------------------------------
# fencing + language guessing
# ---------------------------------------------------------------------------
class TestFencing:
    def test_python_run_fenced_with_label(self):
        text = "Intro prose:\n\ndef handler(event):\n    return event\n\nAfter prose."
        stats = CleanupStats()
        out = fence_code_blocks(text, stats)
        assert "```python" in out
        assert stats.code_blocks_fenced == 1

    def test_go_code_labelled_go(self):
        assert _guess_language("currLocation := NewPoint(3, 4)\nresult := track(loc)") == "go"

    def test_cpp_detected(self):
        code = '#include <iostream>\nstd::cout << "x";\n'
        assert _guess_language(code) == "cpp"

    def test_csharp_detected(self):
        code = "using Microsoft.AspNetCore.Mvc;\nnamespace Demo;\npublic class HomeController\n"
        assert _guess_language(code) == "csharp"

    def test_ambiguous_c_like_gets_plain_fence(self):
        assert _guess_language("a[i] = b;\nfoo(bar);\nbaz();") == ""

    def test_indented_continuation_keeps_block_together(self):
        text = (
            "def create(self, content):\n"
            "    post = make(content)\n"
            "    self.posts.append(post)\n"
            "        return post\n"
            "After prose sentence that ends the listing and is clearly text."
        )
        stats = CleanupStats()
        out = fence_code_blocks(text, stats)
        assert stats.code_blocks_fenced == 1
        block = out.split("```")[1]
        assert "return post" in block


class TestRepairFences:
    def test_fragmented_fences_merged(self):
        text = textwrap.dedent(
            """\
            ```python
            def create(self, content):
                post = build(content)
            ```
                return post

            ```python
            def update(self):
                pass
            ```
            """
        )
        stats = CleanupStats()
        out = repair_fences(text, stats)
        assert stats.fences_merged == 1
        assert out.count("```") == 2  # one open + one close
        assert "return post" in out.split("```")[1]

    def test_wrong_python_label_regussed(self):
        text = "```python\nfunc TrackPlayer() {\n\tcurrLocation := NewPoint(3, 4)\n}\n```"
        stats = CleanupStats()
        out = repair_fences(text, stats)
        assert "```go" in out

    def test_double_spaced_listing_tightened(self):
        text = "```python\nx = 1\n\ny = 2\n\nz = 3\n\nw = 4\n```"
        stats = CleanupStats()
        out = repair_fences(text, stats)
        assert "x = 1\ny = 2\nz = 3\nw = 4" in out

    def test_real_blank_structure_kept(self):
        # Only ~1 blank per 3 lines: intentional spacing, keep it.
        text = "```python\nx = 1\ny = 2\nz = 3\n\nw = 4\nv = 5\nu = 6\n```"
        stats = CleanupStats()
        out = repair_fences(text, stats)
        assert "z = 3\n\nw = 4" in out


# ---------------------------------------------------------------------------
# bullets
# ---------------------------------------------------------------------------
class TestBullets:
    def test_bullet_char_converted(self):
        stats = CleanupStats()
        out = normalize_bullets("•  First point\n•  Second point", stats)
        assert out == "- First point\n- Second point"
        assert stats.bullets_normalized == 2

    def test_sheared_lone_bullets_repaired(self):
        text = textwrap.dedent(
            """\
            responsibilities:

            •

            •

            •

            Post-creation and management

            Timeline generation

            Profile updates

            This structure combines core user data with application behaviors in one place.
            """
        )
        stats = CleanupStats()
        out = normalize_bullets(text, stats)
        assert "- Post-creation and management" in out
        assert "- Timeline generation" in out
        assert "- Profile updates" in out
        assert "•" not in out
        assert "This structure combines" in out

    def test_lone_bullets_next_to_prose_left_alone(self):
        text = "•\n\n•\n\nOnly a sentence follows here.\n\nA long prose sentence that is definitely a full paragraph, ending with a period."
        stats = CleanupStats()
        out = normalize_bullets(text, stats)
        assert out.count("•") == 2  # sentences are not the bullets' missing items

    def test_bullets_inside_fences_untouched(self):
        text = "```\n• not a list, code output\n```"
        stats = CleanupStats()
        assert normalize_bullets(text, stats) == text


# ---------------------------------------------------------------------------
# wrapped-line joining
# ---------------------------------------------------------------------------
class TestJoinWrapped:
    def test_paragraph_block_joined(self):
        text = (
            "Sam Keen is a software engineering leader with over 25 years of experience testing\n"
            "systems in production environments and building scalable platforms for large teams.\n"
        )
        stats = CleanupStats()
        out = join_wrapped_lines(text, stats)
        assert "experience testing systems in production" in out
        assert stats.lines_joined == 1

    def test_hyphenated_word_rejoined(self):
        text = (
            "She has driven strategic, enterprise-wide BPM initiatives and contributed through Ag-\n"
            "ile-Scrum and iterative methodologies over many years.\n"
        )
        stats = CleanupStats()
        out = join_wrapped_lines(text, stats)
        assert "Agile-Scrum" in out

    def test_compound_hyphen_kept(self):
        text = (
            "Your encouragement guides me every day of this journey. And to my loving parents-in-\n"
            "law, thank you for everything you have done for our family.\n"
        )
        stats = CleanupStats()
        out = join_wrapped_lines(text, stats)
        assert "parents-in-law" in out

    def test_short_heading_line_not_joined(self):
        text = "About the author\nSam Keen is a software engineering leader with over 25 years of experience.\n"
        stats = CleanupStats()
        out = join_wrapped_lines(text, stats)
        assert "About the author\nSam Keen" in out

    def test_lists_and_code_not_joined(self):
        text = (
            "- a list item that is quite long and would exceed the sixty five character floor easily\n"
            "- second item\n"
        )
        stats = CleanupStats()
        assert join_wrapped_lines(text, stats) == text

    def test_fenced_code_not_joined(self):
        text = (
            "```python\n"
            "some_variable = a_function_call(argument_one, argument_two, argument_three, four)\n"
            "another = call()\n"
            "```\n"
        )
        stats = CleanupStats()
        assert join_wrapped_lines(text, stats) == text


# ---------------------------------------------------------------------------
# end to end
# ---------------------------------------------------------------------------
class TestEndToEnd:
    def test_kitchen_sink(self):
        text = _PLAIN_TOC + "\n" + _BOOK + "\ndeﬁnition of ﬂow\n"
        out, stats = clean_markdown(text)
        assert "definition of flow" in out
        assert "�" not in out
        assert "# Chapter 2: SOLID Foundations" in out
        assert stats.changed
        assert out.endswith("\n")

    def test_empty_input(self):
        out, stats = clean_markdown("")
        assert out == ""
        assert not stats.changed

    def test_toggles_off_is_identity_modulo_trailing_newline(self):
        text = "some • text ..... 4\nxvii\n"
        out, stats = clean_markdown(
            text,
            normalize_chars=False,
            strip_noise=False,
            strip_toc=False,
            promote_headings=False,
            strip_boilerplate=False,
            fence_code=False,
            bullets=False,
            join_wrapped=False,
            collapse_blanks=False,
        )
        assert out == text
        assert not stats.changed
