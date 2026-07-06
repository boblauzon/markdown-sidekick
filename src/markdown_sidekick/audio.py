"""Local, dependency-free audio transcription.

Uses **faster-whisper**, whose ``av`` (PyAV) dependency ships pre-compiled FFmpeg
shared libraries *inside the wheel* — so MP3/M4A/WAV/FLAC decode out of the box
with no system ``ffmpeg`` on PATH. Inference runs through CTranslate2 (int8 on
CPU, float16 on CUDA when present).

faster-whisper is imported lazily (it pulls in CTranslate2); availability is
probed cheaply with find_spec so app startup stays fast. The Whisper model is
downloaded once on first use to a local cache under %LOCALAPPDATA%.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Callable

from .settings import app_data_dir

_FASTER_WHISPER_AVAILABLE = importlib.util.find_spec("faster_whisper") is not None

# Audio container formats PyAV can decode.
AUDIO_EXTENSIONS: tuple[str, ...] = (
    ".mp3",
    ".wav",
    ".m4a",
    ".flac",
    ".ogg",
    ".opus",
    ".aac",
    ".wma",
)

# Video containers: PyAV decodes their audio track the same way, so
# faster-whisper transcribes them directly — no extraction step needed.
VIDEO_EXTENSIONS: tuple[str, ...] = (
    ".mp4",
    ".m4v",
    ".mkv",
    ".mov",
    ".webm",
    ".avi",
    ".mpg",
    ".mpeg",
)

# Everything the transcription route accepts.
MEDIA_EXTENSIONS: tuple[str, ...] = AUDIO_EXTENSIONS + VIDEO_EXTENSIONS

_DEFAULT_MODEL_SIZE = "base"

# Transcript paragraphs break on a speech gap or when they grow too long —
# a wall of one-line segments reads poorly to humans and AI models alike.
_PARAGRAPH_GAP_S = 2.0
_PARAGRAPH_MAX_CHARS = 700


def group_paragraphs(
    segments: list[tuple[float, float, str]],
) -> list[tuple[float, str]]:
    """Group (start, end, text) segments into (start, paragraph_text) blocks."""
    paragraphs: list[tuple[float, list[str]]] = []
    last_end: float | None = None
    for start, end, text in segments:
        text = text.strip()
        if not text:
            continue
        new_para = (
            not paragraphs
            or (last_end is not None and start - last_end > _PARAGRAPH_GAP_S)
            or sum(len(t) + 1 for t in paragraphs[-1][1]) > _PARAGRAPH_MAX_CHARS
        )
        if new_para:
            paragraphs.append((start, [text]))
        else:
            paragraphs[-1][1].append(text)
        last_end = end
    return [(start, " ".join(texts)) for start, texts in paragraphs]


def _short_stamp(seconds: float) -> str:
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def audio_available() -> bool:
    """True if local audio transcription can run."""
    return _FASTER_WHISPER_AVAILABLE


def _model_cache_dir() -> str:
    path = app_data_dir("models")
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


# Files a complete faster-whisper model snapshot must contain.
_MODEL_FILES = ("model.bin", "config.json", "tokenizer.json", "vocabulary.txt")


class AudioTranscriber:
    """Lazy wrapper around faster-whisper with CPU/GPU auto-detection."""

    def __init__(self, model_size: str = _DEFAULT_MODEL_SIZE) -> None:
        self.model_size = model_size
        self._model = None
        self._device = None
        self._compute_type = None

    def _detect_hardware(self) -> tuple[str, str]:
        """('cuda','float16') when a CUDA GPU is present, else ('cpu','int8')."""
        try:
            import ctranslate2

            if ctranslate2.get_cuda_device_count() > 0:
                return "cuda", "float16"
        except Exception:
            pass
        return "cpu", "int8"

    def _ensure_model_dir(self) -> str:
        """Download the model as real file copies (not the HF symlink cache).

        On Windows the HuggingFace cache uses symlinks that can dangle (no
        symlink privilege / hf-xet), which CTranslate2 cannot open. Downloading
        into a ``local_dir`` materialises real files and sidesteps that.
        """
        from huggingface_hub import snapshot_download

        local_dir = Path(_model_cache_dir()) / f"faster-whisper-{self.model_size}"
        # Re-download if any expected file is missing (e.g. an interrupted prior
        # download left model.bin but not the tokenizer/config).
        if not all((local_dir / name).exists() for name in _MODEL_FILES):
            snapshot_download(
                repo_id=f"Systran/faster-whisper-{self.model_size}",
                local_dir=str(local_dir),
            )
        return str(local_dir)

    def _load_model(self):
        if self._model is None:
            from faster_whisper import WhisperModel

            self._device, self._compute_type = self._detect_hardware()
            self._model = WhisperModel(
                self._ensure_model_dir(),
                device=self._device,
                compute_type=self._compute_type,
            )
        return self._model

    def transcribe_to_markdown(
        self,
        path: str | Path,
        on_progress: Callable[[float, float], None] | None = None,
    ) -> str:
        """Transcribe an audio file to a timestamped Markdown transcript.

        ``on_progress(seconds_done, seconds_total)`` is called per segment.
        """
        source = Path(path)
        model = self._load_model()
        segments, info = model.transcribe(
            str(source),
            beam_size=5,
            vad_filter=True,  # skip silence
            vad_parameters={"min_silence_duration_ms": 500},
        )
        duration = float(getattr(info, "duration", 0.0) or 0.0)
        # faster-whisper can return None for these on undetectable/empty audio.
        language = info.language or "unknown"
        prob = info.language_probability
        lang_line = (
            f"**Detected language:** {language} ({prob:.0%} confidence)"
            if isinstance(prob, (int, float))
            else f"**Detected language:** {language}"
        )
        header = [
            f"# Transcript: {source.name}",
            "",
            lang_line,
            f"**Engine:** faster-whisper ({self.model_size}, {self._device}/{self._compute_type})",
            "",
            "---",
            "",
        ]
        collected: list[tuple[float, float, str]] = []
        for seg in segments:
            collected.append((float(seg.start), float(seg.end), seg.text))
            if on_progress is not None:
                # VAD can make info.duration < the last segment end; keep the
                # reported total >= current so progress never exceeds 100%.
                on_progress(float(seg.end), max(duration, float(seg.end)))
        body: list[str] = []
        for start, paragraph in group_paragraphs(collected):
            body.append(f"**[{_short_stamp(start)}]** {paragraph}")
            body.append("")
        if not body:
            body.append("_(No speech detected.)_")
        return "\n".join(header + body).rstrip() + "\n"
