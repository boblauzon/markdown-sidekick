"""Paragraph grouping and video-route tests.

The end-to-end video test synthesises an MP4 with PyAV (bundled with
faster-whisper) and runs the real pipeline; it is skipped when the Whisper
model has not been cached locally yet, so the suite never downloads ~150 MB.
"""

from __future__ import annotations

import importlib.util
import math
from pathlib import Path

import pytest

from markdown_sidekick import audio
from markdown_sidekick.audio import group_paragraphs
from markdown_sidekick.settings import app_data_dir


class TestGroupParagraphs:
    def test_contiguous_segments_merge(self):
        segs = [(0.0, 2.0, "Hello there."), (2.2, 4.0, "This continues."), (4.1, 5.0, "Still going.")]
        paras = group_paragraphs(segs)
        assert len(paras) == 1
        assert paras[0] == (0.0, "Hello there. This continues. Still going.")

    def test_gap_starts_new_paragraph(self):
        segs = [(0.0, 2.0, "First thought."), (7.0, 9.0, "New topic.")]
        paras = group_paragraphs(segs)
        assert len(paras) == 2
        assert paras[1][0] == 7.0

    def test_length_cap_starts_new_paragraph(self):
        segs = [(float(i), float(i) + 0.9, "word " * 40) for i in range(10)]
        paras = group_paragraphs(segs)
        assert len(paras) > 1

    def test_empty_segments_skipped(self):
        assert group_paragraphs([(0.0, 1.0, "  "), (1.0, 2.0, "x")]) == [(1.0, "x")]


class TestVideoRouting:
    def test_video_extensions_in_media(self):
        assert ".mp4" in audio.MEDIA_EXTENSIONS
        assert ".mkv" in audio.MEDIA_EXTENSIONS
        assert ".mp3" in audio.MEDIA_EXTENSIONS


_model_cached = (app_data_dir("models") / "faster-whisper-base").exists()
_av_available = importlib.util.find_spec("av") is not None


@pytest.mark.skipif(
    not (_model_cached and _av_available and audio.audio_available()),
    reason="whisper model not cached locally or av missing",
)
class TestVideoEndToEnd:
    def _make_mp4(self, path: Path, seconds: float = 2.0) -> None:
        """Write an MP4 whose audio track is a quiet sine tone."""
        import numpy as np
        import av

        rate = 16000
        container = av.open(str(path), mode="w")
        stream = container.add_stream("aac", rate=rate)
        stream.layout = "mono"
        total = int(rate * seconds)
        t = np.arange(total, dtype=np.float32)
        samples = (0.1 * np.sin(2 * math.pi * 440.0 * t / rate)).astype(np.float32)
        frame = av.AudioFrame.from_ndarray(samples.reshape(1, -1), format="fltp", layout="mono")
        frame.sample_rate = rate
        for packet in stream.encode(frame):
            container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
        container.close()

    def test_mp4_transcribes_via_whisper_route(self, tmp_path):
        from markdown_sidekick.converter import ConversionEngine

        video = tmp_path / "tone.mp4"
        self._make_mp4(video)
        engine = ConversionEngine(enable_ocr=False, enable_audio=True)
        result = engine.convert_file(video)
        assert result.ok
        assert result.engine == "whisper"
        assert result.markdown.startswith("# Transcript: tone.mp4")
        # A sine tone contains no speech; either the VAD drops everything or
        # whisper hallucinates a fragment — both are fine, the route is the test.
