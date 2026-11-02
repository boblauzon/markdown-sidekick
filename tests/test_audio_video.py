"""Paragraph grouping and video-route tests.

The end-to-end video test synthesises an MP4 with PyAV (bundled with
faster-whisper) and runs the real pipeline; it is skipped when the Whisper
model has not been cached locally yet, so the suite never downloads ~150 MB.
"""

from __future__ import annotations

import importlib.util
import io
import math
import wave
import zipfile
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


class TestNothingLeavesTheComputer:
    """markitdown's audio converter uploads to Google's speech API; no route
    may ever reach it. The call is intercepted, so nothing is sent."""

    @pytest.fixture
    def google_calls(self, monkeypatch):
        sr = pytest.importorskip("speech_recognition")
        calls = []

        def intercept(self, audio_data, *args, **kwargs):
            calls.append(len(audio_data.frame_data))
            raise sr.RequestError("intercepted: would have uploaded to Google")

        monkeypatch.setattr(sr.Recognizer, "recognize_google", intercept)
        return calls

    @staticmethod
    def _wav_bytes() -> bytes:
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(b"\x00\x01" * 16000)
        return buf.getvalue()

    def test_audio_turned_off(self, tmp_path, google_calls):
        from markdown_sidekick.converter import ConversionEngine

        wav = tmp_path / "talk.wav"
        wav.write_bytes(self._wav_bytes())
        res = ConversionEngine(enable_ocr=False, enable_audio=False).convert_file(wav)
        assert google_calls == []
        assert not res.ok and res.error_code == "MS-115"

    def test_whisper_failure(self, tmp_path, google_calls, monkeypatch):
        from markdown_sidekick.converter import ConversionEngine

        class Broken:
            model_size = "base"

            def transcribe_to_markdown(self, *args, **kwargs):
                raise RuntimeError("CUDA out of luck")

        wav = tmp_path / "talk.wav"
        wav.write_bytes(self._wav_bytes())
        monkeypatch.setattr(audio, "audio_available", lambda: True)
        engine = ConversionEngine(enable_ocr=False)
        monkeypatch.setattr(engine, "_audio_engine", lambda: Broken())
        res = engine.convert_file(wav)
        assert google_calls == []
        assert not res.ok and res.error_code == "MS-112"

    # Both reached Google before the guard: markitdown sniffs the bare file
    # as audio, and its ZIP converter converts members with the same instance.
    @pytest.mark.parametrize("kind", ["no_extension", "inside_zip"])
    def test_sniffed_and_zipped_audio(self, tmp_path, google_calls, kind):
        from markdown_sidekick.converter import ConversionEngine

        if kind == "no_extension":
            source = tmp_path / "recording"
            source.write_bytes(self._wav_bytes())
        else:
            source = tmp_path / "meeting.zip"
            with zipfile.ZipFile(source, "w") as z:
                z.writestr("meeting.wav", self._wav_bytes())
        ConversionEngine(enable_ocr=False, enable_audio=False).convert_file(source)
        assert google_calls == []

    # The intercept tests above need speech_recognition, which our own
    # requirements no longer install (ADR-001). These two hold either way.
    def test_the_engine_has_no_online_audio_converter(self):
        from markitdown.converters import AudioConverter

        from markdown_sidekick.converter import ConversionEngine

        engine = ConversionEngine(enable_ocr=False, enable_audio=False)
        assert not any(isinstance(r.converter, AudioConverter) for r in engine._md._converters)

    def test_our_requirements_never_install_the_speech_converter(self):
        root = Path(__file__).resolve().parents[1]
        for name in ("requirements.txt", "pyproject.toml"):
            text = (root / name).read_text(encoding="utf-8")
            assert "markitdown[all]" not in text and "audio-transcription" not in text, name
        assert "speechrecognition==" not in (root / "constraints.txt").read_text(encoding="utf-8").lower()


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
