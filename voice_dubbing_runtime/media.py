"""FFmpeg-backed source preparation and dependency-free reference selection."""

from __future__ import annotations

import array
import math
import os
import shutil
import statistics
import subprocess
import uuid
import wave
from pathlib import Path
from typing import Any, Callable

from .errors import (
    CANCELLED,
    INVALID_REFERENCE,
    REFERENCE_DURATION_INVALID,
    VoiceRuntimeError,
)
from .io_utils import file_record


SUPPORTED_AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg"}
SUPPORTED_VIDEO_EXTENSIONS = {
    ".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".ts", ".mts", ".m2ts"
}
MIN_REFERENCE_SECONDS = 8.0
MAX_REFERENCE_SECONDS = 15.0


def validate_reference_duration(
    start_seconds: float,
    end_seconds: float,
    *,
    minimum_seconds: float = MIN_REFERENCE_SECONDS,
    maximum_seconds: float = MAX_REFERENCE_SECONDS,
) -> int:
    """Validate an inclusive manual interval using stable millisecond rounding."""
    start = float(start_seconds)
    end = float(end_seconds)
    duration = end - start
    duration_ms = round(duration * 1000.0)
    minimum_ms = round(float(minimum_seconds) * 1000.0)
    maximum_ms = round(float(maximum_seconds) * 1000.0)
    if end <= start or not minimum_ms <= duration_ms <= maximum_ms:
        raise VoiceRuntimeError(
            REFERENCE_DURATION_INVALID,
            (
                f"Độ dài đoạn tham chiếu là {duration:.3f} giây. "
                f"Giá trị hợp lệ từ {minimum_seconds:g} đến {maximum_seconds:g} giây."
            ),
            {
                "start_seconds": start,
                "end_seconds": end,
                "duration_seconds": duration,
                "duration_ms": duration_ms,
                "minimum_ms": minimum_ms,
                "maximum_ms": maximum_ms,
            },
        )
    return duration_ms


def validate_measured_duration(
    duration_seconds: float,
    *,
    sample_rate: int,
    minimum_seconds: float = MIN_REFERENCE_SECONDS,
    maximum_seconds: float = MAX_REFERENCE_SECONDS,
) -> None:
    """Allow at most one decoded audio frame of post-cut timebase drift."""
    duration = float(duration_seconds)
    frame_tolerance = 1.0 / max(1, int(sample_rate))
    if not (
        float(minimum_seconds) - frame_tolerance
        <= duration
        <= float(maximum_seconds) + frame_tolerance
    ):
        raise VoiceRuntimeError(
            REFERENCE_DURATION_INVALID,
            (
                f"Độ dài đoạn tham chiếu là {duration:.3f} giây. "
                f"Giá trị hợp lệ từ {minimum_seconds:g} đến {maximum_seconds:g} giây."
            ),
            {
                "duration_seconds": duration,
                "sample_rate": int(sample_rate),
                "frame_tolerance_seconds": frame_tolerance,
                "minimum_seconds": float(minimum_seconds),
                "maximum_seconds": float(maximum_seconds),
            },
        )


def resolve_ffmpeg(runtime_root: Path, requested: str | Path | None = None) -> Path:
    candidates: list[Path] = []
    if requested:
        candidates.append(Path(requested).expanduser())
    environment = os.environ.get("VOICE_DUB_FFMPEG")
    if environment:
        candidates.append(Path(environment).expanduser())
    candidates.extend(
        [
            runtime_root / "ffmpeg.exe",
            runtime_root.parent / "long-held-image-extractor" / "ffmpeg.exe",
        ]
    )
    located = shutil.which("ffmpeg")
    if located:
        candidates.append(Path(located))
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.is_file() and resolved.stat().st_size > 0:
            return resolved
    raise VoiceRuntimeError(INVALID_REFERENCE, "FFmpeg executable was not found.")


def _cancelled(cancel_token: Any) -> bool:
    checker = getattr(cancel_token, "is_cancelled", None)
    return bool(checker()) if checker else False


def _run_ffmpeg(command: list[str], cancel_token: Any, timeout: float = 600.0) -> subprocess.CompletedProcess[str]:
    if _cancelled(cancel_token):
        raise VoiceRuntimeError(CANCELLED, "Source preparation was cancelled.")
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        raise VoiceRuntimeError(INVALID_REFERENCE, "FFmpeg source preparation timed out.") from exc
    if _cancelled(cancel_token):
        raise VoiceRuntimeError(CANCELLED, "Source preparation was cancelled.")
    return completed


def normalize_source(
    source: Path,
    source_type: str,
    output: Path,
    ffmpeg: Path,
    cancel_token: Any,
) -> dict[str, Any]:
    source = source.expanduser().resolve()
    if not source.is_file() or source.stat().st_size == 0:
        raise VoiceRuntimeError(INVALID_REFERENCE, f"Source file is missing or empty: {source}")
    suffix = source.suffix.lower()
    allowed = SUPPORTED_VIDEO_EXTENSIONS if source_type == "video" else SUPPORTED_AUDIO_EXTENSIONS
    if source_type not in {"video", "audio"} or suffix not in allowed:
        raise VoiceRuntimeError(
            INVALID_REFERENCE,
            f"Unsupported {source_type} source extension: {suffix}",
            {"supported_extensions": sorted(allowed)},
        )
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite normalized audio: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.stem}.{uuid.uuid4().hex}.wav")
    # Some downloaded MP4 files contain AAC packets whose timestamps jump
    # backwards.  The WAV muxer rejects those packets under ``-xerror`` unless
    # we generate missing timestamps and rebuild a monotonic audio timeline.
    # This remains a formatting/analysis step; it is not background removal.
    command = [
        str(ffmpeg), "-hide_banner", "-nostdin", "-v", "error", "-xerror",
        "-fflags", "+genpts", "-i", str(source), "-map", "0:a:0", "-vn",
        "-af", "asetpts=N/SR/TB,aresample=24000:async=1:first_pts=0",
        "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", str(temporary),
    ]
    completed = _run_ffmpeg(command, cancel_token)
    if completed.returncode != 0 or not temporary.is_file() or temporary.stat().st_size == 0:
        if temporary.exists():
            temporary.unlink()
        message = completed.stderr.strip()[-1000:]
        if source_type == "video" and ("matches no streams" in message.lower() or "does not contain" in message.lower()):
            message = "The selected video has no decodable audio stream."
        raise VoiceRuntimeError(
            INVALID_REFERENCE,
            message or "FFmpeg could not decode the source audio.",
            {"source": str(source), "ffmpeg_exit_code": completed.returncode},
        )
    try:
        os.link(temporary, output)
    finally:
        if temporary.exists():
            temporary.unlink()
    metadata = inspect_pcm_wav(output)
    return {"source": str(source), "normalized": file_record(output), **metadata}


def inspect_pcm_wav(path: Path) -> dict[str, Any]:
    try:
        with wave.open(str(path), "rb") as reader:
            channels = reader.getnchannels()
            rate = reader.getframerate()
            frames = reader.getnframes()
            width = reader.getsampwidth()
            compression = reader.getcomptype()
    except (wave.Error, OSError) as exc:
        raise VoiceRuntimeError(INVALID_REFERENCE, f"Invalid WAV: {path}: {exc}") from exc
    if channels != 1 or rate != 24000 or width != 2 or compression != "NONE" or frames <= 0:
        raise VoiceRuntimeError(
            INVALID_REFERENCE,
            "Prepared reference must be mono 24 kHz PCM16 WAV.",
            {"channels": channels, "sample_rate": rate, "sample_width": width, "frames": frames},
        )
    return {
        "sample_rate": rate,
        "channels": channels,
        "sample_width_bytes": width,
        "frame_count": frames,
        "duration_seconds": frames / rate,
    }


def _load_samples(path: Path) -> tuple[array.array[int], int]:
    metadata = inspect_pcm_wav(path)
    with wave.open(str(path), "rb") as reader:
        raw = reader.readframes(reader.getnframes())
    samples = array.array("h")
    samples.frombytes(raw)
    if os.sys.byteorder != "little":
        samples.byteswap()
    return samples, int(metadata["sample_rate"])


def choose_reference_auto(
    path: Path,
    *,
    window_start_seconds: float = 0.0,
    window_end_seconds: float | None = None,
) -> tuple[float, float, dict[str, Any]]:
    samples, rate = _load_samples(path)
    duration = len(samples) / rate
    window_start = max(0.0, float(window_start_seconds))
    window_end = duration if window_end_seconds is None else min(duration, float(window_end_seconds))
    if window_end <= window_start:
        raise VoiceRuntimeError(
            INVALID_REFERENCE,
            "Target speaker window end must be greater than start.",
            {"start_seconds": window_start, "end_seconds": window_end},
        )
    window_start_frame = int(round(window_start * rate))
    window_end_frame = min(len(samples), int(round(window_end * rate)))
    scoped_samples = samples[window_start_frame:window_end_frame]
    scoped_duration = len(scoped_samples) / rate
    if scoped_duration < MIN_REFERENCE_SECONDS:
        raise VoiceRuntimeError(
            INVALID_REFERENCE,
            f"Target speaker window is too short for an 8-second reference: {scoped_duration:.3f}s",
        )
    window_seconds = min(10.0, scoped_duration - 0.20)
    window_seconds = max(MIN_REFERENCE_SECONDS, window_seconds)
    window_samples = min(len(scoped_samples), int(round(window_seconds * rate)))
    hop = max(1, int(0.25 * rate))
    block = max(1, int(0.10 * rate))
    safe_start = min(int(0.10 * rate), max(0, len(scoped_samples) - window_samples))
    last_start = max(safe_start, len(scoped_samples) - window_samples - int(0.10 * rate))
    starts = list(range(safe_start, last_start + 1, hop)) or [0]
    if starts[-1] != last_start:
        starts.append(last_start)

    best: tuple[float, int, dict[str, Any]] | None = None
    for start in starts:
        segment = scoped_samples[start : start + window_samples]
        block_db: list[float] = []
        clipped = 0
        for index in range(0, len(segment), block):
            part = segment[index : index + block]
            if not part:
                continue
            mean_square = sum(float(value) * float(value) for value in part) / len(part)
            rms = math.sqrt(mean_square) / 32768.0
            block_db.append(20.0 * math.log10(max(rms, 1e-9)))
            clipped += sum(1 for value in part if abs(value) >= 32760)
        median_db = statistics.median(block_db)
        variation = statistics.pstdev(block_db) if len(block_db) > 1 else 0.0
        silence_ratio = sum(1 for value in block_db if value < -50.0) / max(1, len(block_db))
        clipping_ratio = clipped / max(1, len(segment))
        # Speech-like stable energy wins; silence and clipping receive strong penalties.
        score = median_db - variation * 0.8 - silence_ratio * 60.0 - clipping_ratio * 5000.0
        evidence = {
            "median_block_rms_dbfs": median_db,
            "block_rms_variation_db": variation,
            "silence_block_ratio": silence_ratio,
            "clipping_ratio": clipping_ratio,
            "score": score,
        }
        if best is None or score > best[0]:
            best = (score, start, evidence)
    assert best is not None
    start_seconds = (window_start_frame + best[1]) / rate
    end_seconds = (window_start_frame + best[1] + window_samples) / rate
    best[2]["target_speaker_window"] = {
        "start_seconds": window_start_frame / rate,
        "end_seconds": window_end_frame / rate,
    }
    return start_seconds, end_seconds, best[2]


def cut_reference(source_wav: Path, output: Path, start_seconds: float, end_seconds: float) -> dict[str, Any]:
    metadata = inspect_pcm_wav(source_wav)
    duration = float(metadata["duration_seconds"])
    if not (0.0 <= start_seconds < end_seconds <= duration + 1e-6):
        raise VoiceRuntimeError(
            INVALID_REFERENCE,
            "Manual reference range is outside source duration.",
            {"start_seconds": start_seconds, "end_seconds": end_seconds, "duration_seconds": duration},
        )
    validate_reference_duration(start_seconds, end_seconds)
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite reference preview: {output}")
    rate = int(metadata["sample_rate"])
    start_frame = int(round(start_seconds * rate))
    end_frame = min(int(metadata["frame_count"]), int(round(end_seconds * rate)))
    with wave.open(str(source_wav), "rb") as reader:
        reader.setpos(start_frame)
        frames = reader.readframes(end_frame - start_frame)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("xb") as raw_handle:
        pass
    try:
        with wave.open(str(output), "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(rate)
            writer.writeframes(frames)
    except Exception:
        output.unlink(missing_ok=True)
        raise
    result = inspect_pcm_wav(output)
    validate_measured_duration(
        float(result["duration_seconds"]), sample_rate=int(result["sample_rate"])
    )
    samples, _ = _load_samples(output)
    peak = max((abs(value) for value in samples), default=0) / 32768.0
    rms = math.sqrt(sum(float(value) * float(value) for value in samples) / max(1, len(samples))) / 32768.0
    clipping = sum(1 for value in samples if abs(value) >= 32760) / max(1, len(samples))
    if peak <= 1e-4 or rms <= 1e-5:
        raise VoiceRuntimeError(INVALID_REFERENCE, "Selected reference is effectively silent.")
    return {
        **result,
        "start_seconds": start_frame / rate,
        "end_seconds": end_frame / rate,
        "peak": peak,
        "rms": rms,
        "clipping_ratio": clipping,
        "file": file_record(output),
    }


def prepare_separation_candidate(
    source: Path,
    source_type: str,
    output: Path,
    start_seconds: float,
    end_seconds: float,
    ffmpeg: Path,
    cancel_token: Any,
) -> dict[str, Any]:
    """Cut only the selected source interval for the isolated separator.

    Candidate discovery intentionally uses the inexpensive mono analysis WAV,
    while this function decodes the chosen interval again from the original
    media.  Demucs therefore never receives the complete long video and does
    not inherit the 24 kHz mono analysis downmix.
    """
    source = source.expanduser().resolve()
    if not source.is_file() or source.stat().st_size == 0:
        raise VoiceRuntimeError(INVALID_REFERENCE, f"Source file is missing or empty: {source}")
    suffix = source.suffix.lower()
    allowed = SUPPORTED_VIDEO_EXTENSIONS if source_type == "video" else SUPPORTED_AUDIO_EXTENSIONS
    if source_type not in {"video", "audio"} or suffix not in allowed:
        raise VoiceRuntimeError(
            INVALID_REFERENCE,
            f"Unsupported {source_type} source extension: {suffix}",
            {"supported_extensions": sorted(allowed)},
        )
    validate_reference_duration(start_seconds, end_seconds)
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite separation candidate: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.stem}.{uuid.uuid4().hex}.wav")
    # atrim/asetpts normalises a non-zero input timestamp without decoding or
    # separating any media outside the selected candidate.
    audio_filter = (
        f"atrim=start={float(start_seconds):.9f}:end={float(end_seconds):.9f},"
        "asetpts=PTS-STARTPTS,aresample=44100:async=1:first_pts=0"
    )
    command = [
        str(ffmpeg), "-hide_banner", "-nostdin", "-v", "error", "-xerror",
        "-i", str(source), "-map", "0:a:0", "-vn", "-af", audio_filter,
        "-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le", str(temporary),
    ]
    completed = _run_ffmpeg(command, cancel_token)
    if completed.returncode != 0 or not temporary.is_file() or temporary.stat().st_size <= 44:
        temporary.unlink(missing_ok=True)
        raise VoiceRuntimeError(
            INVALID_REFERENCE,
            completed.stderr.strip()[-1000:] or "FFmpeg could not cut the separation candidate.",
            {"source": str(source), "ffmpeg_exit_code": completed.returncode},
        )
    try:
        with wave.open(str(temporary), "rb") as reader:
            frames = reader.getnframes()
            rate = reader.getframerate()
            channels = reader.getnchannels()
            width = reader.getsampwidth()
        measured = frames / rate if rate else 0.0
        if channels != 2 or rate != 44100 or width != 2 or measured <= 0.0:
            raise VoiceRuntimeError(
                INVALID_REFERENCE,
                "Separation candidate is not stereo 44.1 kHz PCM16 WAV.",
            )
        validate_measured_duration(measured, sample_rate=rate)
        os.link(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "start_seconds": float(start_seconds),
        "end_seconds": float(end_seconds),
        "duration_seconds": measured,
        "sample_rate": rate,
        "channels": channels,
        "file": file_record(output),
    }


def normalize_voice_only(
    separated_vocals: Path,
    output: Path,
    ffmpeg: Path,
    cancel_token: Any,
) -> dict[str, Any]:
    """Apply fixed, light post-separation formatting for TTS references.

    This is deliberately only timestamp repair, mono conversion and resampling;
    it is not presented as source separation or as a denoising substitute.
    """
    separated_vocals = separated_vocals.expanduser().resolve()
    if not separated_vocals.is_file() or separated_vocals.stat().st_size == 0:
        raise VoiceRuntimeError(
            INVALID_REFERENCE, f"Separated vocals are missing or empty: {separated_vocals}"
        )
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite voice-only reference: {output}")
    temporary = output.with_name(f".{output.stem}.{uuid.uuid4().hex}.wav")
    command = [
        str(ffmpeg), "-hide_banner", "-nostdin", "-v", "error", "-xerror",
        "-i", str(separated_vocals), "-map", "0:a:0", "-vn",
        "-af", "aresample=24000:async=1:first_pts=0",
        "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", str(temporary),
    ]
    completed = _run_ffmpeg(command, cancel_token)
    if completed.returncode != 0 or not temporary.is_file() or temporary.stat().st_size <= 44:
        temporary.unlink(missing_ok=True)
        raise VoiceRuntimeError(
            INVALID_REFERENCE,
            completed.stderr.strip()[-1000:] or "FFmpeg could not format separated vocals.",
            {"ffmpeg_exit_code": completed.returncode},
        )
    try:
        os.link(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    metadata = inspect_pcm_wav(output)
    try:
        validate_measured_duration(
            float(metadata["duration_seconds"]), sample_rate=int(metadata["sample_rate"])
        )
    except VoiceRuntimeError:
        output.unlink(missing_ok=True)
        raise
    return {"voice_only": file_record(output), **metadata}
