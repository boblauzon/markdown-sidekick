"""Conversion-quality assessment for produced Markdown.

Scans a document for the residual artifact signatures the cleanup pipeline
targets and reports a compact, human- and machine-readable summary. Used by
the GUI's preview info line, the CLI (``--quality``), and the MCP server so an AI client
can tell whether a file is ready to consume or should be re-converted via a
higher-fidelity path (e.g. MinerU).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from . import debuglog
from .cleanup import is_shadow_word

# ~4 characters per token is a good cross-model estimate for English prose;
# it avoids shipping a tokenizer dependency for what is only a gauge.
_CHARS_PER_TOKEN = 4

# --- binary-noise detection -------------------------------------------------
# markitdown "successfully" converts unrecognized binary garbage (a corrupt
# .docx, an .mp3 with broken magic bytes) by decoding the raw bytes as text,
# so a conversion can report ok while the output is unreadable salad. Two
# signals, both computed over a bounded sample:
#   noise_ratio — control chars (minus \t\n\r), U+FFFD, private-use/unassigned
#   word_ratio  — fraction of whitespace tokens made only of word-like chars
# Guards keep this conservative: short outputs are never flagged, and each
# rule needs either an extreme single signal or two weaker ones together.
_NOISE_SAMPLE_CHARS = 65_536
_NOISE_MIN_CHARS = 400
_NOISE_MIN_TOKENS = 40


def _is_noise_char(ch: str) -> bool:
    o = ord(ch)
    if o < 32:
        return o not in (9, 10, 13)  # \t \n \r are fine
    if o < 127:
        return False
    if o <= 159 or o == 0xFFFD:  # C1 controls / replacement char
        return True
    return unicodedata.category(ch) in ("Co", "Cn")


# Third signal, for bytes decoded with the wrong multi-byte codec (markitdown
# 0.1.8 turns binary junk into Hangul + CJK + Armenian + Arabic salad, all of
# which are "word-like"): real text keeps to one script per word. On 483 real
# converted books the share of mixed-script words peaked at 0.1% (at most 3
# script groups per book); codec salad mixes ~90% of words over ~9 groups.
_MIXED_SCRIPT_RATIO = 0.20
_MIXED_SCRIPT_GROUPS = 3


def _script_group(ch: str) -> str | None:
    if not ch.isalpha():
        return None
    o = ord(ch)
    if o < 0x250 or 0x1E00 <= o < 0x1F00:
        return "latin"
    if 0x370 <= o < 0x400 or 0x1F00 <= o < 0x2000:
        return "greek"
    if 0x400 <= o < 0x530:
        return "cyrillic"
    if 0x530 <= o < 0x590:
        return "armenian"
    if 0x590 <= o < 0x600:
        return "hebrew"
    if 0x600 <= o < 0x780 or 0x8A0 <= o < 0x900 or 0xFB50 <= o < 0xFF00:
        return "arabic"
    if 0x900 <= o < 0xE00:
        return "indic"
    if 0xE00 <= o < 0x1000:
        return "southeast-asian"
    if 0x1100 <= o < 0x1200 or 0x3130 <= o < 0x3190 or 0xAC00 <= o < 0xD7B0:
        return "hangul"
    # Kana and kanji are one group: Japanese mixes them inside every word.
    if 0x2E80 <= o < 0xA000 or 0xF900 <= o < 0xFB00 or 0xFF00 <= o < 0xFFF0 or o >= 0x20000:
        return "cjk"
    return "other"


def _script_mix(tokens: list[str]) -> tuple[float, int]:
    """(share of words mixing two or more scripts, distinct script groups)."""
    words = [t for t in tokens if sum(c.isalpha() for c in t) >= 2]
    seen: set[str] = set()
    mixed = 0
    for word in words:
        groups = {g for g in map(_script_group, word) if g}
        seen |= groups
        mixed += len(groups) >= 2
    return (mixed / len(words) if words else 0.0), len(seen)


def _is_wordlike_char(ch: str) -> bool:
    o = ord(ch)
    if o < 127:
        return o > 32  # any printable ASCII — code tokens stay word-like
    if o <= 159 or o == 0xFFFD:
        return False
    cat = unicodedata.category(ch)
    # Letters, marks, numbers, punctuation of any script, plus currency signs.
    return cat[0] in "LMNP" or cat == "Sc"

_TOC_RESIDUE_RE = re.compile(r"^\S.{0,120}?\s+•\s+\d{1,4}\s*$|�{2,}|\.{6,}\s*\d{1,4}\s*$", re.M)
_LONE_BULLET_RE = re.compile(r"^\s*•\s*$", re.M)
_BULLET_CHAR_RE = re.compile(r"^\s*•\s+\S", re.M)
_HEADING_RE = re.compile(r"^#{1,6}\s+\S", re.M)
_FENCE_RE = re.compile(r"^\s*```(\w*)\s*$", re.M)
_LONG_LINE_HARD_WRAP_RE = re.compile(r"^.{60,90}[a-z,]\n[a-z]", re.M)
# Printer's slug text that leaked from outside a PDF's trim box.
_PREPRESS_RE = re.compile(
    r"\.indd\b|\bJob(?:\s*No\.?)?\s*:\s*[\d-]+\s+Title\s*:|#\d+\s+DTP\s*:|\bDtp\s*:\s*\w+\s+Page\s*:",
    re.IGNORECASE,
)
# Shadow / fake-bold type extracted twice over: "DDrraawwiinngg". The regex
# finds candidates; cleanup's word test drops placeholders ("IIIIIIII") and
# hex colours ("FFEEDD"), which are doubled on purpose.
_DOUBLED_WORD_RE = re.compile(r"\b(?:([A-Za-z])\1){3,}\b")


@dataclass
class QualityReport:
    """What the document looks like from an AI-consumption standpoint."""

    chars: int = 0
    est_tokens: int = 0
    headings: int = 0
    fenced_blocks: int = 0
    unlabeled_fences: int = 0
    fence_parity_ok: bool = True
    toc_residue: int = 0
    lone_bullets: int = 0
    raw_bullet_lines: int = 0
    hard_wrap_hints: int = 0
    prepress_residue: int = 0
    doubled_words: int = 0
    noise_ratio: float = 0.0
    word_ratio: float = 1.0
    script_mix: float = 0.0  # share of words mixing scripts (wrong-codec salad)
    binary_noise: bool = False
    issues: list[str] = field(default_factory=list)
    score: int = 100  # 0-100; 100 = no detected artifacts, good structure

    def summary(self) -> str:
        parts = [
            f"~{self.est_tokens:,} tokens",
            f"{self.headings} heading(s)",
            f"{self.fenced_blocks} code block(s)",
        ]
        if self.issues:
            parts.append("issues: " + "; ".join(self.issues))
        # Plain hyphen: Windows consoles with legacy code pages mangle em-dashes.
        return f"Quality {self.score}/100 - " + ", ".join(parts)

    def as_dict(self) -> dict:
        return {
            "score": self.score,
            "est_tokens": self.est_tokens,
            "chars": self.chars,
            "headings": self.headings,
            "fenced_blocks": self.fenced_blocks,
            "unlabeled_fences": self.unlabeled_fences,
            "fence_parity_ok": self.fence_parity_ok,
            "toc_residue": self.toc_residue,
            "lone_bullets": self.lone_bullets,
            "raw_bullet_lines": self.raw_bullet_lines,
            "hard_wrap_hints": self.hard_wrap_hints,
            "prepress_residue": self.prepress_residue,
            "doubled_words": self.doubled_words,
            "noise_ratio": round(self.noise_ratio, 3),
            "word_ratio": round(self.word_ratio, 3),
            "script_mix": round(self.script_mix, 3),
            "binary_noise": self.binary_noise,
            "issues": list(self.issues),
        }


def assess_markdown(text: str) -> QualityReport:
    """Score converted Markdown; deductions mirror the cleanup pass targets."""
    report = _assess(text)
    debuglog.event("quality", **report.as_dict())
    return report


def _assess(text: str) -> QualityReport:
    r = QualityReport()
    if not text:
        r.score = 0
        r.issues.append("empty output")
        return r
    r.chars = len(text)
    r.est_tokens = max(1, r.chars // _CHARS_PER_TOKEN)
    r.headings = len(_HEADING_RE.findall(text))

    fences = _FENCE_RE.findall(text)
    r.fence_parity_ok = len(fences) % 2 == 0
    # Openers carry the label; closers are always bare. With balanced fences,
    # every second marker is a closer, so unlabeled openers = bare - closers.
    bare = sum(1 for label in fences if not label)
    r.fenced_blocks = len(fences) // 2
    r.unlabeled_fences = max(0, bare - r.fenced_blocks)

    r.toc_residue = len(_TOC_RESIDUE_RE.findall(text))
    r.lone_bullets = len(_LONE_BULLET_RE.findall(text))
    r.raw_bullet_lines = len(_BULLET_CHAR_RE.findall(text))
    r.hard_wrap_hints = len(_LONG_LINE_HARD_WRAP_RE.findall(text))
    r.prepress_residue = len(_PREPRESS_RE.findall(text))
    r.doubled_words = sum(
        1 for m in _DOUBLED_WORD_RE.finditer(text) if is_shadow_word(m.group(0))
    )

    sample = text[:_NOISE_SAMPLE_CHARS]
    r.noise_ratio = sum(1 for ch in sample if _is_noise_char(ch)) / len(sample)
    tokens = sample.split()
    groups = 0
    if tokens:
        wordlike = sum(1 for t in tokens if all(_is_wordlike_char(c) for c in t))
        r.word_ratio = wordlike / len(tokens)
        r.script_mix, groups = _script_mix(tokens)
    if len(sample) >= _NOISE_MIN_CHARS and len(tokens) >= _NOISE_MIN_TOKENS:
        r.binary_noise = (
            r.noise_ratio >= 0.10
            or (r.noise_ratio >= 0.02 and r.word_ratio <= 0.15)
            or r.word_ratio <= 0.05
            or (r.script_mix >= _MIXED_SCRIPT_RATIO and groups >= _MIXED_SCRIPT_GROUPS)
        )

    score = 100
    if r.binary_noise:
        score -= 80
        r.issues.append("output looks like binary noise (source may be corrupt)")
    big_doc = r.chars > 20_000  # heading expectations only apply to documents
    if big_doc and r.headings == 0:
        score -= 25
        r.issues.append("no headings")
    if not r.fence_parity_ok:
        score -= 15
        r.issues.append("unbalanced code fences")
    for count, per, cap, label in (
        (r.prepress_residue, 10, 20, "prepress slug residue"),
        (r.doubled_words, 20, 10, "doubled (shadow) text"),
        (r.toc_residue, 10, 20, "TOC residue"),
        (r.lone_bullets, 20, 15, "sheared bullets"),
        (r.raw_bullet_lines, 50, 10, "non-Markdown bullets"),
        (r.hard_wrap_hints, 100, 10, "hard-wrapped prose"),
    ):
        if count:
            score -= min(cap, 1 + count // per)
            r.issues.append(f"{count} {label}")
    r.score = max(0, score)
    return r
