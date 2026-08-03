"""Create and score the locked Lester Holt Candidate 02/03 variants.

This script never runs source separation.  It preserves the already-debugged
Demucs output as evidence and excludes it when the scaled-copy gate failed.
"""

from __future__ import annotations

import array
import argparse
import json
import math
import os
import shutil
import subprocess
import time
import uuid
import wave
from pathlib import Path
from typing import Any

from voice_dubbing_runtime.io_utils import file_record, sha256_file, utc_now, write_json_exclusive
from voice_dubbing_runtime.worker import PeakMemoryMonitor


CANDIDATES = {
    2: {"start_seconds": 13.074667, "end_seconds": 27.737854, "speech_ratio": 0.8503401360544217},
    3: {"start_seconds": 35.7125, "end_seconds": 49.3715, "speech_ratio": 0.8102189781021898},
}


def _copy_exclusive(source: Path, target: Path) -> None:
    if target.exists():
        raise FileExistsError(f"Refusing to overwrite variant: {target}")
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        with source.open("rb") as input_handle, temporary.open("xb") as output_handle:
            shutil.copyfileobj(input_handle, output_handle, 1024 * 1024)
        with temporary.open("r+b") as durable_handle:
            durable_handle.flush()
            os.fsync(durable_handle.fileno())
        if sha256_file(source) != sha256_file(temporary):
            raise RuntimeError(f"Variant copy hash mismatch: {target}")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _strict_decode(path: Path, ffmpeg: Path) -> str:
    completed = subprocess.run(
        [
            str(ffmpeg), "-hide_banner", "-nostdin", "-v", "error", "-xerror",
            "-err_detect", "explode", "-i", str(path), "-map", "0:a:0", "-f",
            "null", "NUL",
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
    if completed.returncode != 0:
        raise RuntimeError(f"Strict decode failed for {path}: {completed.stderr[-1000:]}")
    return "Pass"


def _read_samples(path: Path) -> tuple[array.array[int], dict[str, Any]]:
    with wave.open(str(path), "rb") as reader:
        if reader.getsampwidth() != 2:
            raise RuntimeError(f"Expected PCM16 WAV: {path}")
        channels = reader.getnchannels()
        rate = reader.getframerate()
        frames = reader.getnframes()
        raw = reader.readframes(frames)
    samples = array.array("h")
    samples.frombytes(raw)
    if os.sys.byteorder != "little":
        samples.byteswap()
    if channels != 1:
        raise RuntimeError(f"Expected mono variant: {path}")
    if not samples:
        raise RuntimeError(f"Empty PCM variant: {path}")
    peak = max(abs(value) for value in samples) / 32768.0
    rms = math.sqrt(sum(float(value) ** 2 for value in samples) / len(samples)) / 32768.0
    clipping = sum(abs(value) >= 32760 for value in samples) / len(samples)
    nonzero = sum(value != 0 for value in samples) / len(samples)
    block_size = max(1, round(rate * 0.10))
    block_rms: list[float] = []
    for offset in range(0, len(samples), block_size):
        block = samples[offset : offset + block_size]
        if len(block) < block_size // 2:
            continue
        block_rms.append(
            math.sqrt(sum(float(value) ** 2 for value in block) / len(block)) / 32768.0
        )
    speech_threshold = max(10 ** (-45.0 / 20.0), rms * 0.22)
    speech_ratio = sum(value >= speech_threshold for value in block_rms) / max(1, len(block_rms))
    return samples, {
        "codec": "pcm_s16le",
        "sample_rate": rate,
        "channels": channels,
        "duration_seconds": frames / rate,
        "finite_sample_ratio": 1.0,
        "nonzero_sample_ratio": nonzero,
        "peak": peak,
        "rms": rms,
        "peak_dbfs": 20.0 * math.log10(max(peak, 1e-12)),
        "rms_dbfs": 20.0 * math.log10(max(rms, 1e-12)),
        "clipping_ratio": clipping,
        "speech_ratio_proxy": speech_ratio,
    }


def _compare(left: array.array[int], right: array.array[int]) -> dict[str, float]:
    count = min(len(left), len(right))
    dot = sum(float(left[i]) * float(right[i]) for i in range(count))
    left_square = sum(float(left[i]) ** 2 for i in range(count))
    right_square = sum(float(right[i]) ** 2 for i in range(count))
    correlation = dot / math.sqrt(max(1e-18, left_square * right_square))
    gain = dot / max(1e-18, left_square)
    residual_square = max(0.0, right_square - dot * dot / max(1e-18, left_square))
    residual_rms = math.sqrt(residual_square / max(1, count))
    right_rms = math.sqrt(right_square / max(1, count))
    return {
        "correlation": correlation,
        "best_fit_gain": gain,
        "best_fit_gain_db": 20.0 * math.log10(max(abs(gain), 1e-12)),
        "gain_fitted_residual_ratio": residual_rms / max(right_rms, 1e-12),
    }


def _create_light_cleaned(source: Path, target: Path, ffmpeg: Path) -> None:
    if target.exists():
        raise FileExistsError(f"Refusing to overwrite variant: {target}")
    completed = subprocess.run(
        [
            str(ffmpeg), "-hide_banner", "-nostdin", "-v", "error", "-xerror",
            "-i", str(source), "-map", "0:a:0", "-af", "highpass=f=65:p=1",
            "-ar", "24000", "-ac", "1", "-c:a", "pcm_s16le", str(target),
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
    if completed.returncode != 0:
        target.unlink(missing_ok=True)
        raise RuntimeError(f"Light cleanup failed: {completed.stderr[-1000:]}")
    with target.open("r+b") as handle:
        handle.flush()
        os.fsync(handle.fileno())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--ffmpeg", type=Path, required=True)
    parser.add_argument("--canonical-source", type=Path, required=True)
    args = parser.parse_args()
    runtime = args.runtime_root.expanduser().resolve()
    ffmpeg = args.ffmpeg.expanduser().resolve()
    output_root = runtime / "runs" / "lestehrolt_en_final"
    batch_root = runtime / "runs" / "manual_reference_target_0_50" / "f69be27d-7fd9-485e-873b-503a50436714"
    report_path = output_root / "final_reference_variant_report.json"
    if report_path.exists():
        raise FileExistsError(f"Refusing to overwrite final report: {report_path}")

    started = time.perf_counter()
    variants: list[dict[str, Any]] = []
    with PeakMemoryMonitor() as memory:
        for candidate, interval in CANDIDATES.items():
            source = batch_root / f"ref_source_mix_candidate_{candidate:02d}.wav"
            debug_voice = output_root / f"debug_candidate_{candidate:02d}" / "final_voice_only.wav"
            paths = {
                "CLEAN_SOURCE_MIX": output_root / f"candidate_{candidate:02d}_source_mix.wav",
                "LIGHT_CLEANED_MIX": output_root / f"candidate_{candidate:02d}_light_cleaned.wav",
                "TRUE_VOCALS": output_root / f"candidate_{candidate:02d}_true_vocals.wav",
            }
            _copy_exclusive(source, paths["CLEAN_SOURCE_MIX"])
            _create_light_cleaned(source, paths["LIGHT_CLEANED_MIX"], ffmpeg)
            _copy_exclusive(debug_voice, paths["TRUE_VOCALS"])
            source_samples, _ = _read_samples(paths["CLEAN_SOURCE_MIX"])
            for variant, path in paths.items():
                variant_started = time.perf_counter()
                samples, metrics = _read_samples(path)
                decode = _strict_decode(path, ffmpeg)
                comparison = _compare(source_samples, samples)
                true_vocals = variant == "TRUE_VOCALS"
                eligible = not true_vocals
                variants.append(
                    {
                        "candidate_number": candidate,
                        **interval,
                        "variant": variant,
                        "eligible_for_primary": eligible,
                        "exclusion_code": "SOURCE_SEPARATION_NO_EFFECT" if true_vocals else None,
                        "path": str(path),
                        **file_record(path),
                        **metrics,
                        "ffmpeg_strict_decode": decode,
                        "comparison_to_source_mix": comparison,
                        "background_assessment": (
                            "SOURCE_SEPARATION_NO_EFFECT_SCALED_COPY"
                            if true_vocals
                            else "MILD_65_HZ_RUMBLE_ATTENUATION_TECHNICAL_ONLY"
                            if variant == "LIGHT_CLEANED_MIX"
                            else "LIGHT_BACKGROUND_ACCEPTED_FROM_USER_LISTENING_HISTORY"
                        ),
                        "single_speaker": "TARGET_SPEAKER_WINDOW_AND_USER_LISTENING_HISTORY",
                        "elapsed_seconds": time.perf_counter() - variant_started,
                    }
                )

    selected = next(
        item
        for item in variants
        if item["candidate_number"] == 2 and item["variant"] == "CLEAN_SOURCE_MIX"
    )
    report = {
        "schema_version": 1,
        "status": "Pass",
        "created_at": utc_now(),
        "source": str(args.canonical_source.expanduser().resolve()),
        "target_speaker_window": {"start_seconds": 0.0, "end_seconds": 50.0},
        "candidate_01_excluded": "LOW_SPEECH_RATIO_0.1172",
        "variants": variants,
        "selected_reference": {
            **selected,
            "selection_reason": (
                "Candidate 02 has the higher speech ratio and longer duration; the user found "
                "source mixes more natural, true vocals failed the scaled-copy gate, and the "
                "unheard high-pass variant has no demonstrated identity benefit."
            ),
        },
        "separation_effective": False,
        "scaled_copy_gate": "SOURCE_SEPARATION_NO_EFFECT",
        "fallback_used": "CLEAN_SOURCE_MIX",
        "total_elapsed_seconds": time.perf_counter() - started,
        "peak_ram_bytes": memory.peak_bytes,
        "peak_ram_gib": memory.peak_bytes / (1024 ** 3),
        "processes_remaining": [],
    }
    write_json_exclusive(report_path, report)
    print(json.dumps({"status": "Pass", "report": str(report_path), "selected": selected["path"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
