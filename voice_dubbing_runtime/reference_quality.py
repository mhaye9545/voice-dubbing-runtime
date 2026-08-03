"""Dependency-free technical gates for voice-only cloning references.

The acoustic measurements in this module are conservative proxies.  They can
reject continuous residual energy, but they never claim to perform speaker
diarization.  Human listening and an explicit single-speaker assertion remain
separate commit gates.
"""

from __future__ import annotations

import array
import math
import os
import subprocess
import wave
from pathlib import Path
from typing import Any, Sequence

from .errors import (
    BACKGROUND_AUDIO_DETECTED,
    INVALID_REFERENCE,
    NEEDS_MANUAL_REFERENCE,
    SOURCE_SEPARATION_NO_EFFECT,
    VoiceRuntimeError,
)
from .io_utils import file_record
from .media import MAX_REFERENCE_SECONDS, MIN_REFERENCE_SECONDS, inspect_pcm_wav


REFERENCE_GATE_REVISION = "voice_only_gate_v1"
MAX_CLIPPING_RATIO = 0.001
MIN_SPEECH_RATIO = 0.35
MAX_SPEECH_RATIO = 0.98
MAX_NOISE_FLOOR_DBFS = -38.0
MIN_SPEECH_NOISE_CONTRAST_DB = 14.0
MIN_QUIET_BLOCK_RATIO = 0.05
MAX_DURATION_DELTA_SECONDS = 0.10
MIN_EFFECTIVE_SEPARATION_CORRELATION = 0.995
MAX_GAIN_FITTED_RESIDUAL_RATIO = 0.15


def strict_ffmpeg_decode(path: Path, ffmpeg: Path) -> None:
    """Decode one audio stream with FFmpeg's strict error handling."""
    sink = "NUL" if os.name == "nt" else "/dev/null"
    try:
        completed = subprocess.run(
            [
                str(ffmpeg), "-hide_banner", "-nostdin", "-v", "error", "-xerror",
                "-err_detect", "explode", "-i", str(path), "-map", "0:a:0",
                "-f", "null", sink,
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        raise VoiceRuntimeError(INVALID_REFERENCE, "FFmpeg reference decode timed out.") from exc
    if completed.returncode != 0:
        raise VoiceRuntimeError(
            INVALID_REFERENCE,
            f"FFmpeg reference decode failed: {completed.stderr.strip()[-1000:]}",
            {"ffmpeg_exit_code": completed.returncode},
        )


def _percentile(values: Sequence[float], quantile: float) -> float:
    if not values:
        return -180.0
    ordered = sorted(values)
    position = max(0.0, min(1.0, quantile)) * (len(ordered) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _samples_and_metrics(path: Path) -> tuple[array.array[int], dict[str, Any]]:
    metadata = inspect_pcm_wav(path)
    duration = float(metadata["duration_seconds"])
    if not MIN_REFERENCE_SECONDS <= duration <= MAX_REFERENCE_SECONDS:
        raise VoiceRuntimeError(
            INVALID_REFERENCE,
            f"Voice reference duration must be 8–15 seconds, got {duration:.3f}s.",
        )
    with wave.open(str(path), "rb") as reader:
        raw = reader.readframes(reader.getnframes())
    samples = array.array("h")
    samples.frombytes(raw)
    if os.sys.byteorder != "little":
        samples.byteswap()
    if not samples:
        raise VoiceRuntimeError(INVALID_REFERENCE, "Voice reference contains no PCM samples.")

    peak = max(abs(value) for value in samples) / 32768.0
    mean_square = sum(float(value) * float(value) for value in samples) / len(samples)
    rms = math.sqrt(mean_square) / 32768.0
    clipping_ratio = sum(1 for value in samples if abs(value) >= 32760) / len(samples)
    if peak <= 1e-4 or rms <= 1e-5:
        raise VoiceRuntimeError(INVALID_REFERENCE, "Voice reference is effectively silent.")
    if clipping_ratio > MAX_CLIPPING_RATIO:
        raise VoiceRuntimeError(
            INVALID_REFERENCE,
            f"Voice reference clipping ratio is too high: {clipping_ratio:.6f}.",
            {"clipping_ratio": clipping_ratio, "maximum": MAX_CLIPPING_RATIO},
        )

    rate = int(metadata["sample_rate"])
    block_size = max(1, int(round(rate * 0.10)))
    block_dbfs: list[float] = []
    for offset in range(0, len(samples), block_size):
        block = samples[offset : offset + block_size]
        if len(block) < block_size // 2:
            continue
        block_ms = sum(float(value) * float(value) for value in block) / len(block)
        block_rms = math.sqrt(block_ms) / 32768.0
        block_dbfs.append(20.0 * math.log10(max(block_rms, 1e-9)))
    if not block_dbfs:
        raise VoiceRuntimeError(INVALID_REFERENCE, "Voice reference has no analyzable audio blocks.")

    noise_floor = _percentile(block_dbfs, 0.10)
    speech_level = _percentile(block_dbfs, 0.80)
    contrast = speech_level - noise_floor
    speech_threshold = max(-50.0, noise_floor + 10.0, speech_level - 18.0)
    speech_ratio = sum(value >= speech_threshold for value in block_dbfs) / len(block_dbfs)
    quiet_ratio = sum(value < speech_threshold for value in block_dbfs) / len(block_dbfs)
    return samples, {
        **metadata,
        "finite_sample_count": len(samples),
        "sample_count": len(samples),
        "finite_sample_ratio": 1.0,
        "peak": peak,
        "peak_dbfs": 20.0 * math.log10(max(peak, 1e-12)),
        "rms": rms,
        "rms_dbfs": 20.0 * math.log10(max(rms, 1e-12)),
        "clipping_ratio": clipping_ratio,
        "block_count": len(block_dbfs),
        "noise_floor_dbfs": noise_floor,
        "speech_level_dbfs": speech_level,
        "speech_noise_contrast_db": contrast,
        "speech_ratio": speech_ratio,
        "quiet_block_ratio": quiet_ratio,
    }


def validate_voice_only_reference(
    voice_only_path: Path,
    source_mix_path: Path,
    ffmpeg: Path,
    *,
    background_was_reported: bool = False,
) -> dict[str, Any]:
    """Return technical evidence without asserting speaker identity or human audibility.

    The energy comparison is deliberately only a deterministic proxy.  It can
    catch a no-op separator when background was explicitly reported, while the
    mandatory listening gate remains the authority for residual audible music.
    """
    voice_only_path = voice_only_path.expanduser().resolve()
    source_mix_path = source_mix_path.expanduser().resolve()
    if not voice_only_path.is_file() or not source_mix_path.is_file():
        raise VoiceRuntimeError(INVALID_REFERENCE, "Reference mix or voice-only WAV is missing.")
    strict_ffmpeg_decode(voice_only_path, ffmpeg)
    strict_ffmpeg_decode(source_mix_path, ffmpeg)
    voice_samples, voice = _samples_and_metrics(voice_only_path)
    mix_samples, mix = _samples_and_metrics(source_mix_path)

    duration_delta = abs(
        float(voice["duration_seconds"]) - float(mix["duration_seconds"])
    )
    if duration_delta > MAX_DURATION_DELTA_SECONDS:
        raise VoiceRuntimeError(
            INVALID_REFERENCE,
            "Voice-only duration does not match the selected source interval.",
            {
                "voice_only_duration_seconds": voice["duration_seconds"],
                "source_mix_duration_seconds": mix["duration_seconds"],
                "duration_delta_seconds": duration_delta,
                "maximum_delta_seconds": MAX_DURATION_DELTA_SECONDS,
            },
        )

    aligned_count = min(len(voice_samples), len(mix_samples))
    difference_square = 0.0
    dot = 0.0
    mix_square = 0.0
    voice_square = 0.0
    for index in range(aligned_count):
        mixed = float(mix_samples[index]) / 32768.0
        separated = float(voice_samples[index]) / 32768.0
        delta = mixed - separated
        difference_square += delta * delta
        dot += mixed * separated
        mix_square += mixed * mixed
        voice_square += separated * separated
    difference_rms = math.sqrt(difference_square / max(1, aligned_count))
    correlation = dot / math.sqrt(max(1e-18, mix_square * voice_square))
    best_fit_gain = dot / max(1e-18, mix_square)
    fitted_residual_square = max(
        0.0, voice_square - (dot * dot / max(1e-18, mix_square))
    )
    gain_fitted_residual_rms = math.sqrt(
        fitted_residual_square / max(1, aligned_count)
    )
    aligned_voice_rms = math.sqrt(voice_square / max(1, aligned_count))
    gain_fitted_residual_ratio = gain_fitted_residual_rms / max(
        aligned_voice_rms, 1e-12
    )
    scaled_copy = (
        abs(correlation) > MIN_EFFECTIVE_SEPARATION_CORRELATION
        and gain_fitted_residual_ratio < MAX_GAIN_FITTED_RESIDUAL_RATIO
    )
    energy_reduction_db = float(mix["rms_dbfs"]) - float(voice["rms_dbfs"])
    noise_floor_reduction_db = float(mix["noise_floor_dbfs"]) - float(
        voice["noise_floor_dbfs"]
    )

    background_failures: list[str] = []
    if voice["noise_floor_dbfs"] > MAX_NOISE_FLOOR_DBFS:
        background_failures.append("noise_floor_too_high")
    if voice["speech_noise_contrast_db"] < MIN_SPEECH_NOISE_CONTRAST_DB:
        background_failures.append("speech_noise_contrast_too_low")
    if voice["speech_ratio"] < MIN_SPEECH_RATIO:
        background_failures.append("speech_ratio_too_low")
    if (
        background_was_reported
        and energy_reduction_db < 0.50
        and difference_rms < 0.005
    ):
        background_failures.append("no_material_separation_delta")

    status = "PASS"
    if scaled_copy:
        status = SOURCE_SEPARATION_NO_EFFECT
    elif background_failures:
        status = BACKGROUND_AUDIO_DETECTED
    elif (
        voice["quiet_block_ratio"] < MIN_QUIET_BLOCK_RATIO
        or voice["speech_ratio"] > MAX_SPEECH_RATIO
    ):
        # With no usable pause/noise-floor evidence, the runtime cannot safely
        # distinguish uninterrupted speech from a continuous background bed.
        status = NEEDS_MANUAL_REFERENCE

    return {
        "schema_version": 1,
        "validation_revision": REFERENCE_GATE_REVISION,
        "status": status,
        "ffmpeg_decode": "Pass",
        "voice_only": {**file_record(voice_only_path), **voice},
        "source_mix": {**file_record(source_mix_path), **mix},
        "mix_voice_comparison": {
            "background_was_reported": bool(background_was_reported),
            "aligned_sample_count": aligned_count,
            "duration_delta_seconds": duration_delta,
            "difference_rms": difference_rms,
            "waveform_correlation": correlation,
            "best_fit_gain": best_fit_gain,
            "best_fit_gain_db": 20.0
            * math.log10(max(abs(best_fit_gain), 1e-12)),
            "gain_fitted_residual_rms": gain_fitted_residual_rms,
            "gain_fitted_residual_ratio": gain_fitted_residual_ratio,
            "overall_energy_reduction_db": energy_reduction_db,
            "noise_floor_reduction_db": noise_floor_reduction_db,
            "interpretation": "technical_proxy_pending_human_listening",
        },
        "separation_effectiveness": {
            "status": (
                SOURCE_SEPARATION_NO_EFFECT if scaled_copy else "EFFECTIVE_CHANGE"
            ),
            "scaled_copy": scaled_copy,
            "correlation": correlation,
            "best_fit_gain": best_fit_gain,
            "best_fit_gain_db": 20.0
            * math.log10(max(abs(best_fit_gain), 1e-12)),
            "gain_fitted_residual_rms": gain_fitted_residual_rms,
            "gain_fitted_residual_ratio": gain_fitted_residual_ratio,
            "thresholds": {
                "minimum_absolute_correlation": MIN_EFFECTIVE_SEPARATION_CORRELATION,
                "maximum_gain_fitted_residual_ratio": MAX_GAIN_FITTED_RESIDUAL_RATIO,
            },
        },
        "background_gate": {
            "status": (
                "TECHNICAL_PROXY_PASS_PENDING_LISTENING"
                if status == "PASS"
                else status
            ),
            "failures": background_failures,
            "thresholds": {
                "maximum_noise_floor_dbfs": MAX_NOISE_FLOOR_DBFS,
                "minimum_speech_noise_contrast_db": MIN_SPEECH_NOISE_CONTRAST_DB,
                "minimum_speech_ratio": MIN_SPEECH_RATIO,
                "maximum_speech_ratio": MAX_SPEECH_RATIO,
                "minimum_quiet_block_ratio": MIN_QUIET_BLOCK_RATIO,
            },
        },
        "single_speaker": {
            "status": "UNVERIFIED",
            "reason": "Source separation does not perform speaker diarization.",
        },
    }
