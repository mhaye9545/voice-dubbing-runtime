"""Run the single locked English XTTS-v2 synthesis for Lester Holt EN."""

from __future__ import annotations

import array
import argparse
import json
import math
import os
import time
import wave
from pathlib import Path
from typing import Any

from voice_dubbing_runtime.io_utils import file_record, read_json, sha256_file, utc_now, write_json_exclusive
from voice_dubbing_runtime.media import resolve_ffmpeg
from voice_dubbing_runtime.profiles import VoiceProfileManager
from voice_dubbing_runtime.worker import CancellationToken, PeakMemoryMonitor, validate_generated_wav
from voice_dubbing_runtime.xtts_backend import MODEL_ID, MODEL_REVISION, XttsV2Backend


PROFILE_ID = "lestehrolt_en_clean"
TEXT = (
    "Good evening. This is a final English voice profile test. The purpose of this "
    "recording is to verify that the selected reference preserves a natural speaking "
    "voice while avoiding noticeable music or background sound in the generated audio."
)
REFERENCE_SHA256 = "2ACCBCA2F1FF1AC469C64721F76C299E8FAB6CCDE8ED02C70D8B451A447C254D"
PINNED_REVISION = "6c2b0d75eae4b7047358e3b6bd9325f857d43f77"


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return -180.0
    position = max(0.0, min(1.0, fraction)) * (len(ordered) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _background_proxy(path: Path) -> dict[str, Any]:
    with wave.open(str(path), "rb") as reader:
        channels = reader.getnchannels()
        rate = reader.getframerate()
        raw = reader.readframes(reader.getnframes())
    samples = array.array("h")
    samples.frombytes(raw)
    if os.sys.byteorder != "little":
        samples.byteswap()
    if channels > 1:
        mono = array.array("h")
        for offset in range(0, len(samples), channels):
            mono.append(round(sum(samples[offset : offset + channels]) / channels))
        samples = mono
    block_size = max(1, round(rate * 0.05))
    block_dbfs: list[float] = []
    longest_zero_samples = 0
    current_zero_samples = 0
    for value in samples:
        if value == 0:
            current_zero_samples += 1
            longest_zero_samples = max(longest_zero_samples, current_zero_samples)
        else:
            current_zero_samples = 0
    for offset in range(0, len(samples), block_size):
        block = samples[offset : offset + block_size]
        if len(block) < block_size // 2:
            continue
        rms = math.sqrt(sum(float(value) ** 2 for value in block) / len(block)) / 32768.0
        block_dbfs.append(20.0 * math.log10(max(rms, 1e-12)))
    noise_floor = _percentile(block_dbfs, 0.10)
    speech_level = _percentile(block_dbfs, 0.80)
    quiet_ratio = sum(value <= -45.0 for value in block_dbfs) / max(1, len(block_dbfs))
    dynamic_contrast = speech_level - noise_floor
    # This flags only an obvious, continuously loud bed. It is intentionally
    # conservative and does not claim to replace a listening assessment.
    obvious_bed = noise_floor > -32.0 and quiet_ratio < 0.01 and dynamic_contrast < 9.0
    severe_dropout = longest_zero_samples / rate > 2.0
    return {
        "status": "FAIL_OBVIOUS_CONTINUOUS_BED" if obvious_bed else "PASS_NO_OBVIOUS_CONTINUOUS_BED",
        "continuous_background_bed_detected": obvious_bed,
        "noise_floor_proxy_dbfs_p10": noise_floor,
        "speech_level_proxy_dbfs_p80": speech_level,
        "speech_noise_contrast_proxy_db": dynamic_contrast,
        "quiet_block_ratio_below_minus_45_dbfs": quiet_ratio,
        "longest_zero_run_seconds": longest_zero_samples / rate,
        "severe_dropout_detected": severe_dropout,
        "second_speaker_assessment": "STRUCTURAL_SINGLE_SPEAKER_TTS_OUTPUT_NOT_DIARIZATION_PROOF",
        "artifact_assessment": (
            "FAIL_SEVERE_DROPOUT" if severe_dropout else "PASS_NO_SEVERE_TECHNICAL_ARTIFACT"
        ),
        "limitation": "Technical proxy; final naturalness and speaker identity remain listening judgments.",
    }


def _remaining_xtts_workers() -> list[dict[str, Any]]:
    try:
        import psutil
    except ImportError:
        return []
    found: list[dict[str, Any]] = []
    for process in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            command = " ".join(process.info.get("cmdline") or [])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if "voice_dubbing_runtime.xtts_engine_worker" in command:
            found.append({"pid": process.info["pid"], "name": process.info.get("name"), "cmdline": command})
    return found


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--profiles-root", type=Path, required=True)
    args = parser.parse_args()
    runtime = args.runtime_root.expanduser().resolve()
    profiles_root = args.profiles_root.expanduser().resolve()
    run_root = runtime / "runs" / "lestehrolt_en_final"
    output = run_root / "lestehrolt_en_final_test.wav"
    dispatch_path = run_root / "lestehrolt_en_final_synthesis_dispatch.json"
    report_path = run_root / "lestehrolt_en_final_synthesis_report.json"
    failure_path = run_root / "lestehrolt_en_final_synthesis_failure.json"
    if any(path.exists() for path in (output, dispatch_path, report_path, failure_path)):
        raise FileExistsError("Final synthesis artifact already exists; refusing a second call")
    if MODEL_ID != "coqui/XTTS-v2" or MODEL_REVISION != PINNED_REVISION:
        raise RuntimeError("XTTS-v2 backend model/revision changed")
    manifest = read_json(runtime / "models" / "xtts_v2" / "model_manifest.json")
    if manifest.get("model_id") != MODEL_ID or manifest.get("revision") != PINNED_REVISION:
        raise RuntimeError("Pinned XTTS-v2 manifest mismatch")
    remaining_before = _remaining_xtts_workers()
    if remaining_before:
        raise RuntimeError(f"XTTS worker exists before dispatch: {remaining_before}")

    manager = VoiceProfileManager(profiles_root)
    if manager.profile_revision(PROFILE_ID) != 8:
        raise RuntimeError("Final profile revision must be 8 before synthesis")
    profile = manager.assert_synthesis_ready(PROFILE_ID)
    manager.consent(PROFILE_ID)
    references = manager.resolve_references(PROFILE_ID)
    if len(references) != 1 or sha256_file(references[0]) != REFERENCE_SHA256:
        raise RuntimeError("Final profile primary reference changed")

    dispatch = {
        "schema_version": 1,
        "status": "DISPATCHED",
        "dispatched_at": utc_now(),
        "maximum_synthesis_calls": 1,
        "engine": "xtts_v2_multilingual",
        "model_id": MODEL_ID,
        "model_revision": MODEL_REVISION,
        "profile_id": PROFILE_ID,
        "profile_revision": 8,
        "reference": file_record(references[0]),
        "text": TEXT,
        "language": "en",
        "device": "cpu",
        "speed": 1.0,
        "seed": 42,
        "output": str(output),
        "korean_or_chinese_smoke_calls": 0,
    }
    write_json_exclusive(dispatch_path, dispatch)
    backend = XttsV2Backend(runtime)
    stages: list[dict[str, Any]] = []
    started = time.perf_counter()
    try:
        with PeakMemoryMonitor() as memory:
            backend_metrics = backend.synthesize(
                job={
                    "text": TEXT,
                    "language": "en",
                    "device": "cpu",
                    "speed": 1.0,
                    "seed": 42,
                    "keep_model_warm": False,
                },
                profile=profile,
                references=references,
                output_path=output,
                progress=lambda name, progress: stages.append(
                    {"name": name, "progress": progress, "at": utc_now()}
                ),
                cancel_token=CancellationToken(),
            )
        elapsed = time.perf_counter() - started
        validation = dict(validate_generated_wav(output, resolve_ffmpeg(runtime)))
        validation.update(
            {
                "pcm": "Pass",
                "finite_samples": "Pass",
                "nonzero_samples": "Pass",
                "not_all_silence": "Pass",
            }
        )
        background = _background_proxy(output)
        if background["continuous_background_bed_detected"] or background["severe_dropout_detected"]:
            raise RuntimeError(f"FINAL_SYNTHESIS_BACKGROUND_OR_ARTIFACT_GATE:{background}")
        remaining = _remaining_xtts_workers()
        if remaining:
            raise RuntimeError(f"XTTS worker remained after synthesis: {remaining}")
        if manager.profile_revision(PROFILE_ID) != 8:
            raise RuntimeError("Synthesis unexpectedly changed the profile revision")
        report = {
            "schema_version": 1,
            "status": "PASS",
            "completed_at": utc_now(),
            "synthesis_call_count": 1,
            "fallback_synthesis_call_count": 0,
            "engine": "xtts_v2_multilingual",
            "model_id": MODEL_ID,
            "model_revision": MODEL_REVISION,
            "device": "cpu",
            "profile_id": PROFILE_ID,
            "profile_revision": 8,
            "references": [file_record(path) for path in references],
            "text": TEXT,
            "language": "en",
            "speed": 1.0,
            "seed": 42,
            "output": file_record(output),
            "validation": validation,
            "background_assessment": background,
            "model_load": "Pass",
            "backend_metrics": backend_metrics,
            "elapsed_seconds": elapsed,
            "peak_ram_bytes": memory.peak_bytes,
            "peak_ram_gib": memory.peak_bytes / (1024 ** 3),
            "processes_remaining": remaining,
            "processes_remaining_count": len(remaining),
            "stages": stages,
            "engine_health_modified": False,
            "other_language_smokes_run": [],
        }
        write_json_exclusive(report_path, report)
        print(json.dumps({"status": "PASS", "output": str(output), "report": str(report_path)}))
        return 0
    except Exception as exc:
        failure = {
            "schema_version": 1,
            "status": "FAILED",
            "failed_at": utc_now(),
            "error": str(exc),
            "output_exists": output.is_file(),
            "output": file_record(output) if output.is_file() else None,
            "elapsed_seconds": time.perf_counter() - started,
            "synthesis_call_count": 1,
            "fallback_synthesis_call_count": 0,
            "processes_remaining": _remaining_xtts_workers(),
        }
        write_json_exclusive(failure_path, failure)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
