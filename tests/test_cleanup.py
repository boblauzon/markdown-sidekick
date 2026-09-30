"""Tests for the cleanup passes, built from artifact patterns observed in a
real 25-book library of PDF→Markdown conversions (Packt technical books)."""

from __future__ import annotations

import textwrap

from markdown_sidekick.cleanup import (
    CleanupStats,
    clean_markdown,
    fence_code_blocks,
    fix_shadow_text,
    join_wrapped_lines,
    normalize_bullets,
    normalize_characters,
    promote_chapter_headings,
    repair_fences,
    strip_page_noise,
    strip_plain_toc,
    strip_prepress,
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

    def test_pdfminer_cid_placeholders_removed(self):
        stats = CleanupStats()
        out = normalize_characters("Gra(cid:16)hic (cid:3)\n", stats)
        assert "(cid:" not in out
        assert stats.chars_normalized == 2


# ---------------------------------------------------------------------------
# doubled "shadow" text (Quarto design-book corpus)
# ---------------------------------------------------------------------------
class TestShadowText:
    def test_doubled_title_lines_repaired(self):
        stats = CleanupStats()
        text = "DDrraawwiinngg CCoommiiccss LLaabb\nCChhaarraacctteerrss,, PPaanneellss,,\n"
        out = fix_shadow_text(text, stats)
        assert out == "Drawing Comics Lab\nCharacters, Panels,\n"
        assert stats.shadow_words_fixed == 5

    def test_lone_doubled_word_and_quadruple_layers(self):
        assert fix_shadow_text("MMaakkiinngg\n", CleanupStats()) == "Making\n"
        out = fix_shadow_text("JJJJoooobbbb::::00003333 TTTTiiiittttlllleeee\n", CleanupStats())
        assert out == "Job:03 Title\n"

    def test_doubled_run_inside_a_normal_line(self):
        out = fix_shadow_text("see page 4 JJoobb::0033--770000006655 TTiittllee::RRPP here\n", CleanupStats())
        assert out == "see page 4 Job:03-700065 Title:RP here\n"

    def test_real_words_and_numbers_untouched(self):
        text = "The bookkeeper saw 1100 committees in 2200 BC. Aaron sees Mississippi.\n"
        assert fix_shadow_text(text, CleanupStats()) == text

    def test_genuinely_doubled_tokens_untouched(self):
        # Placeholders and sequences are doubled on purpose (review finding:
        # "XXXX XXXX" once became "XX XX", "AATT CCGG" became "AT CG").
        text = "Bookkeeping codes XXXX XXXX XXXX 1234 and primers AATT CCGG AACCGGTT.\n"
        assert fix_shadow_text(text, CleanupStats()) == text

    def test_hex_colours_untouched(self):
        # Review finding: "FFAABBCC" became "FABC" and a swatch label
        # "FFEEDD" alone on its line became "FED".
        text = "Set the ARGB value to FFAABBCC here\nFFEEDD\nSwatches AABBCC DDEEFF\n"
        assert fix_shadow_text(text, CleanupStats()) == text

    def test_hex_lettered_word_repaired_inside_a_shadow_run(self):
        assert fix_shadow_text("BBEEDD RROOOOMM IIDDEEAASS\n", CleanupStats()) == "BED ROOM IDEAS\n"

    def test_short_doubled_words_repaired_in_company(self):
        assert fix_shadow_text("tthhee GG rriidd\n", CleanupStats()) == "the G rid\n"

    def test_fenced_code_untouched(self):
        text = "```\nxx = 'aabbccdd' 'eeffgghh'\n```\n"
        assert fix_shadow_text(text, CleanupStats()) == text


# ---------------------------------------------------------------------------
# prepress slugs (InDesign file stamps, job tickets, DTP tags)
# ---------------------------------------------------------------------------
class TestPrepress:
    _SLUGS = (
        "700065 - MakingBreakingGrid2ndED_001-077.indd 1 3/23/17 5:23 PM\n"
        "Real paragraph text survives.\n"
        "Job No: 05-30592 Title: RP-Graphic Design Reference & Specification\n"
        "#175 DTP: 216 Page: 4 (RAY)(Text)\n"
        "Drawing In Black & White_001-144_11520 C2.indd 19 20/8/16 14:37\n"
    )

    def test_slug_lines_removed(self):
        stats = CleanupStats()
        out = strip_prepress(self._SLUGS, stats)
        assert out == "Real paragraph text survives.\n"
        assert stats.prepress_removed >= 5

    def test_real_text_on_a_slug_line_is_kept(self):
        text = (
            "MAKING AND BREAKING THE GRID 700065 - Grid2ndED_001-077.indd 44 3/23/17 5:23 PM\n"
            "018-035_28824.indd 22 7/31/12 2:01 PM\n"
        )
        out = strip_prepress(text, CleanupStats())
        assert out == "MAKING AND BREAKING THE GRID\n"

    def test_table_rows_are_cleaned_not_mangled(self):
        text = (
            "001-007_30592.indd 1 5/13/13 3:37 PM\n"
            "| Measure | P 186C(RAY)(Text) | #175 DTP: 216 Page: 8 |\n"
            "| mm | points | picas |\n"
        )
        out = strip_prepress(text, CleanupStats())
        assert out.split("\n") == ["| Measure |  |  |", "| mm | points | picas |", ""]

    def test_code_and_prose_mentions_survive_in_a_stamped_document(self):
        # Review findings: "(text)" arguments were stripped from unfenced
        # code, and prose mentioning a .indd file lost its first half.
        text = (
            "700065 - Grid_001-077.indd 1 3/23/17 5:23 PM\n"
            "def show(text):\n"
            "    print(text)\n"
            "    return len(text)\n"
            "Designers who use the master_page approach save grid.indd 2 times before printing it.\n"
        )
        out = strip_prepress(text, CleanupStats())
        assert out == (
            "def show(text):\n"
            "    print(text)\n"
            "    return len(text)\n"
            "Designers who use the master_page approach save grid.indd 2 times before printing it.\n"
        )

    def test_undated_stamp_alone_on_its_line_is_removed(self):
        text = "Body.\n9780760383186 - Logos that Last_front_endpaper.indd 9\nUPOD p001-032_.indd   21\nMore.\n"
        assert strip_prepress(text, CleanupStats()) == "Body.\nMore.\n"

    def test_documents_without_strong_markers_untouched(self):
        # A single weak marker is not proof of a print proof.
        text = "The (Text) variable holds the Job: 12 Title: field.\n"
        assert strip_prepress(text, CleanupStats()) == text

    def test_doubled_ticket_removed_end_to_end(self):
        text = (
            "001-017_28824.indd 2 7/31/12 10:48 AM\n"
            "((FFooggrraa 2299))WWFF JJoobb::0077--2288882244 TTiittllee::RRPP--DDrraawwiinngg\n"
            "Body.\n"
        )
        out = _clean(text)
        assert out == "Body.\n"


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

    def test_repl_output_inside_fences_kept(self):
        # Corpus finding (Clean Code in Python): a REPL's bare "5432" / "42"
        # output lines were deleted from fenced listings as page numbers.
        block = '```\n>>> os.getenv("DPORT", 5432)\n5432\n```\n\nProse.\n\n'
        text = "".join(block.replace("5432", str(n)) for n in (5432, 42, 7, 99, 12, 8))
        stats = CleanupStats()
        assert strip_page_noise(text, stats) == text
        assert stats.removed_noise_lines == 0

    def test_layout_engine_output_keeps_repeated_lines(self):
        # Corpus finding: pdflayout already removes furniture from the page
        # margins, so on its output this pass only deleted content — step
        # labels, citations ("Elsevier, 2007"), a chart's years.
        text = "".join(f"Step {n % 3 + 1}\n\nDraw panel border {n}.\n\n" for n in range(8))
        assert _clean(text, engine="pdflayout").count("Step 1") == text.count("Step 1")
        assert _clean(text, engine="markitdown").count("Step 1") < text.count("Step 1")

    def test_bare_page_numbers_outside_fences_still_removed(self):
        text = "".join(f"Prose on page {n}.\n{n}\n" for n in range(10, 16)) + "```\n3\n```\n"
        out = strip_page_noise(text, CleanupStats())
        assert "\n11\n" not in out
        assert out.endswith("```\n3\n```\n")


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

    def test_fenced_console_tables_untouched(self):
        # Box-drawn console tables match the leader pattern; inside a fence
        # they are output, and dropping a fence marker would unbalance the
        # rest of the document.
        table = "```\n┌──────┬──────┐\n│ id   │ name │\n├──────┼──────┤\n└──────┴──────┘\n```\n"
        text = "Run the query:\n\n" + table + "\nThen again:\n\n" + table * 3
        stats = CleanupStats()
        assert strip_plain_toc(text, stats) == text
        assert stats.toc_lines_removed == 0

    def test_fenced_pipe_output_is_not_a_toc_table(self):
        rows = "\n".join(f"| {n} |  | {n + 1} |" for n in range(1, 9))
        text = f"```\n{rows}\n```\n"
        assert _clean(text, fence_code=False, join_wrapped=False) == text


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


class TestJoinScaling:
    def test_pathological_wrap_join_stays_fast(self):
        """10k hard-wrapped unpunctuated lines (bad-OCR shape) must clean in
        seconds, not minutes — the join pass was quadratic in both regex
        scanning and string copies before the _JOIN_MAX_LEN cap."""
        import time

        # Lines must stay unique even with digits stripped: repeated shapes
        # look like running headers/footers to the noise pass and would be
        # removed before ever reaching the join pass.
        import random

        rng = random.Random(7)
        vocab = (
            "pipeline contract freshness idempotence partition schema replay "
            "backfill retry publish validate transform ingest observability "
            "ledger quorum shard beacon anchor drift"
        ).split()
        text = "\n".join(
            " ".join(rng.sample(vocab, 11)) + " continues onward and"
            for _ in range(10_000)
        )
        t = time.perf_counter()
        out, stats = clean_markdown(text)
        elapsed = time.perf_counter() - t
        assert elapsed < 10.0, f"cleanup took {elapsed:.1f}s on pathological input"
        assert stats.lines_joined > 5_000  # the pass still actually joins
        # The cap breaks the accumulation into paragraphs; no content is lost.
        assert len(out) > len(text) * 0.9  # nothing was stripped as noise

    def test_join_cap_bounds_line_length(self):
        # Same uniqueness requirement as above, at a smaller scale.
        text = "\n".join(
            f"filler{i} " + " ".join(f"w{i}x{j} pad" for j in range(8))
            for i in range(2_000)
        )
        out, _ = clean_markdown(text)
        longest = max(len(ln) for ln in out.split("\n"))
        # _JOIN_MAX_LEN (4000) + one more joined line of slack
        assert longest < 4200
