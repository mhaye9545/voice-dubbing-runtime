"""Run the single user-approved XTTS-v2 English post-save retry."""

from __future__ import annotations

import array
import hashlib
import json
import math
import os
import sys
import tempfile
import time
import wave
from pathlib import Path
from typing import Any

RUNTIME_ROOT = Path(__file__).resolve().parents[1]
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

from voice_dubbing_runtime.capabilities import EngineRegistry
from voice_dubbing_runtime.io_utils import file_record, utc_now, write_json_exclusive
from voice_dubbing_runtime.media import resolve_ffmpeg
from voice_dubbing_runtime.profiles import VoiceProfileManager
from voice_dubbing_runtime.worker import PeakMemoryMonitor, validate_generated_wav
from voice_dubbing_runtime.xtts_backend import (
    MODEL_ID,
    MODEL_REVISION,
    PACKAGE_REVISION,
    XttsV2Backend,
)
from voice_dubbing_runtime.xtts_engine_worker import _save_pcm16_exclusive


APPROVAL_TOKEN = "APPROVED_ONE_EN_POST_SAVE_RETRY"
EXPECTED_MODEL_ID = "coqui/XTTS-v2"
EXPECTED_MODEL_REVISION = "6c2b0d75eae4b7047358e3b6bd9325f857d43f77"
MODEL_MANIFEST_SHA256 = "5E4D9414FF499B6089C0D8DB30EA91E24D42FC19B4D8015657DED214CEF19829"
XTTS_BACKEND_SHA256 = "F24A20D0802EC5426880F395D588206A9A748D23F8E2C1CBE9BC082E5F3F76DB"
ENGLISH_TEXT = (
    "Hello everyone. This is an English voice cloning test created from a reusable voice profile."
)
ENGLISH_TEXT_SHA256 = "4630E90728A9EF3713069183B6BB0CEEED4FFF05FAF5608B6EDB4FA0DD9AC602"
PROFILE_ID = "lua_china_base"
REFERENCE_SHA256 = "46CA9FD06C759ABCB3751D809B21F030EF1AE9682C7DB922A51431977859AE6C"
KO_SHA256 = "5727015AF90D1F14CE4B99F01B1D9B93404759C4DBAF1BB2762EA50D7B0DF5D2"
ZH_CN_SHA256 = "60487C9D80C970D595EFF172B711E00D4D85FD49059432711889691B81BF8C00"
ORIGINAL_FAILURE_SHA256 = "1DB341E6E3A86FE82E604F396E14E58EF1AAB372BF8A49AB5718312ECC3AE0D7"
CONTINUATION_REPORT_SHA256 = "3CFA26EEC6C983ED642E24D4B3B874FF087080F658247A2C62116480881EB55A"
XTTS_INPUT_LOCK_SHA256 = "4C2E39EDE69A1A0FA30880538CB7F26DF7DB2871579606A34B4EB5F29C76B97E"
XTTS_LOCK_SHA256 = "EFC8596CEF0A3EC143E41276DB7E9556460276D94381AF76BCF0C69FD48E8EA8"
WRITER_SOURCE_SHA256 = "CD36C707F59F965CD9536BE76D7FBD76FFFCD860FF552B5608C8BA63A16A6D29"
PROVIDED_ENGLISH_VIDEO = Path(r"C:\Users\akita\Downloads\Video\Lestehrolt_en.mp4")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _remaining_workers() -> list[dict[str, Any]]:
    try:
        import psutil
    except ImportError:
        return [{"status": "Not verified", "reason": "psutil missing"}]
    result: list[dict[str, Any]] = []
    tracked_scripts = (
        "run_xtts_multilingual_smoke.py",
        "continue_xtts_multilingual_smoke.py",
        "retry_xtts_english_post_save.py",
    )
    current_process = psutil.Process(os.getpid())
    excluded_pids = {current_process.pid}
    try:
        excluded_pids.update(process.pid for process in current_process.parents())
    except (psutil.AccessDenied, psutil.NoSuchProcess):
        pass
    for process in psutil.process_iter(["pid", "name", "cmdline"]):
        if process.info.get("pid") in excluded_pids:
            continue
        try:
            arguments = process.info.get("cmdline") or []
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
        is_worker = "voice_dubbing_runtime.xtts_engine_worker" in arguments
        is_harness = any(
            str(argument).lower().endswith(tracked_scripts) for argument in arguments
        )
        if is_worker or is_harness:
            result.append({"pid": process.info["pid"], "name": process.info.get("name")})
    return result


def _writer_probe(runtime_root: Path) -> dict[str, Any]:
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="xtts_writer_probe_", dir=runtime_root / "runs") as raw:
        output = Path(raw) / "writer_probe.wav"
        _save_pcm16_exclusive(output, [0.0, 0.25, -0.25, 0.1], 24000)
        record = file_record(output)
        with wave.open(str(output), "rb") as reader:
            audio = {
                "channels": reader.getnchannels(),
                "sample_width_bytes": reader.getsampwidth(),
                "sample_rate": reader.getframerate(),
                "frames": reader.getnframes(),
            }
        if audio != {
            "channels": 1,
            "sample_width_bytes": 2,
            "sample_rate": 24000,
            "frames": 4,
        }:
            raise RuntimeError(f"WRITER_PROBE_FORMAT_MISMATCH:{audio}")
        temporary_files = list(output.parent.glob(".writer_probe.*.wav"))
        if temporary_files:
            raise RuntimeError(f"WRITER_PROBE_TEMP_REMAINED:{temporary_files}")
        return {
            "status": "PASS",
            "bad_file_descriptor": False,
            "writer_mode": "r+b",
            "output": record,
            "audio": audio,
            "elapsed_seconds": time.perf_counter() - started,
        }


def _pcm_evidence(path: Path) -> dict[str, Any]:
    with wave.open(str(path), "rb") as reader:
        channels = reader.getnchannels()
        frames = reader.getnframes()
        raw = reader.readframes(frames)
    samples = array.array("h")
    samples.frombytes(raw)
    if sys.byteorder != "little":
        samples.byteswap()
    sample_count = len(samples)
    finite_sample_count = sum(math.isfinite(float(value)) for value in samples)
    nonzero_sample_count = sum(value != 0 for value in samples)
    if sample_count <= 0 or finite_sample_count != sample_count:
        raise RuntimeError("GENERATED_EN_NONFINITE_OR_EMPTY_SAMPLES")
    if nonzero_sample_count <= 0:
        raise RuntimeError("GENERATED_EN_ALL_ZERO_SAMPLES")
    return {
        "sample_encoding": "pcm_s16le",
        "channels": channels,
        "frames": frames,
        "sample_count": sample_count,
        "finite_sample_count": finite_sample_count,
        "nonzero_sample_count": nonzero_sample_count,
        "finite_samples": "Pass",
        "not_all_silence": "Pass",
    }


def _write_markdown(path: Path, report: dict[str, Any]) -> None:
    validation = report["validation"]
    metrics = report["backend_metrics"]
    lines = [
        "# XTTS-v2 approved English post-save retry",
        "",
        f"- Status: `{report['status']}`",
        f"- Approval: `{APPROVAL_TOKEN}`",
        f"- Model: `{MODEL_ID}`",
        f"- Revision: `{MODEL_REVISION}`",
        f"- Profile: `{PROFILE_ID}`",
        f"- Reference SHA-256: `{REFERENCE_SHA256}`",
        f"- Text SHA-256: `{ENGLISH_TEXT_SHA256}`",
        "- Language / seed / speed: `en` / `42` / `1.0`",
        "- This retry synthesis calls: `1`",
        "- Korean/Chinese synthesis calls: `0`",
        f"- Output: `{report['output']['path']}`",
        f"- Size: `{report['output']['size_bytes']}` bytes",
        f"- Duration: `{validation['duration_seconds']:.3f} s`",
        f"- Peak / RMS: `{validation['peak']:.6f}` / `{validation['rms']:.6f}`",
        f"- Clipping ratio: `{validation['clipping_ratio']:.8f}`",
        f"- Finite samples: `{validation['finite_samples']}`",
        f"- Not all silence: `{validation['not_all_silence']}`",
        f"- FFmpeg strict decode: `{validation['ffmpeg_decode']}`",
        f"- Model load: `{metrics['model_load_elapsed_seconds']:.3f} s`",
        f"- Synthesis: `{metrics['synthesis_elapsed_seconds']:.3f} s`",
        f"- Elapsed: `{report['elapsed_seconds']:.3f} s`",
        f"- Peak RAM: `{report['peak_ram_gib']:.3f} GiB`",
        f"- Remaining processes: `{report['remaining_process_count']}`",
        "- KO/ZH outputs changed: `False`",
        "",
    ]
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines))


def main() -> int:
    runtime_root = RUNTIME_ROOT
    run_dir = runtime_root / "runs" / "xtts_v2_multilingual_smoke"
    report_path = run_dir / "xtts_english_post_save_retry_report.json"
    report_md_path = run_dir / "xtts_english_post_save_retry_report.md"
    failure_path = run_dir / "xtts_english_post_save_retry_failure.json"
    approval_path = run_dir / "en_post_save_retry_approval.json"
    dispatch_marker = run_dir / "call_01_en_post_save_retry_dispatched.json"
    result_marker = run_dir / "call_01_en_post_save_retry_result.json"
    capability_path = run_dir / "xtts_english_post_save_retry_capability.json"
    health_path = runtime_root / "models" / "xtts_v2" / "engine_health.json"
    output = run_dir / "generated_en.wav"
    ko_output = run_dir / "generated_ko.wav"
    zh_output = run_dir / "generated_zh_cn.wav"
    original_failure = run_dir / "xtts_multilingual_smoke_failure.json"
    continuation_report = run_dir / "xtts_multilingual_smoke_continuation_report.json"
    worker_source = runtime_root / "voice_dubbing_runtime" / "xtts_engine_worker.py"
    backend_source = runtime_root / "voice_dubbing_runtime" / "xtts_backend.py"
    manifest_path = runtime_root / "models" / "xtts_v2" / "model_manifest.json"

    no_retry_targets = (
        report_path,
        report_md_path,
        failure_path,
        approval_path,
        dispatch_marker,
        result_marker,
        capability_path,
        health_path,
    )
    existing = [str(path) for path in no_retry_targets if path.exists()]
    if existing:
        raise FileExistsError(f"NO_RETRY_GUARD_TRIGGERED:{existing}")
    if output.exists():
        raise FileExistsError("Refusing to overwrite an existing generated_en.wav")

    removed_temporary_artifacts: list[str] = []
    for pattern in (".generated_en.*.wav", "generated_en.*.tmp"):
        for candidate in run_dir.glob(pattern):
            resolved = candidate.resolve()
            resolved.relative_to(run_dir.resolve())
            if resolved.is_file():
                resolved.unlink()
                removed_temporary_artifacts.append(str(resolved))

    if _sha256(original_failure) != ORIGINAL_FAILURE_SHA256:
        raise RuntimeError("ORIGINAL_EN_FAILURE_ARTIFACT_CHANGED")
    if _sha256(continuation_report) != CONTINUATION_REPORT_SHA256:
        raise RuntimeError("KO_ZH_CONTINUATION_REPORT_CHANGED")
    if _sha256(ko_output) != KO_SHA256 or _sha256(zh_output) != ZH_CN_SHA256:
        raise RuntimeError("KO_OR_ZH_OUTPUT_CHANGED_BEFORE_EN_RETRY")
    if _sha256(worker_source) != WRITER_SOURCE_SHA256:
        raise RuntimeError("WRITER_SOURCE_CHANGED")
    if _sha256(backend_source) != XTTS_BACKEND_SHA256:
        raise RuntimeError("XTTS_BACKEND_CHANGED")
    worker_text = worker_source.read_text(encoding="utf-8")
    if 'temporary.open("r+b")' not in worker_text or "os.fsync(handle.fileno())" not in worker_text:
        raise RuntimeError("WINDOWS_WRITER_FIX_MISSING")
    if _sha256(runtime_root / "requirements-xtts.in.txt") != XTTS_INPUT_LOCK_SHA256:
        raise RuntimeError("XTTS_INPUT_DEPENDENCIES_CHANGED")
    if _sha256(runtime_root / "requirements-xtts.lock.txt") != XTTS_LOCK_SHA256:
        raise RuntimeError("XTTS_DEPENDENCY_LOCK_CHANGED")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if MODEL_ID != EXPECTED_MODEL_ID or MODEL_REVISION != EXPECTED_MODEL_REVISION:
        raise RuntimeError("BACKEND_MODEL_CONSTANT_CHANGED")
    if _sha256(manifest_path) != MODEL_MANIFEST_SHA256:
        raise RuntimeError("MODEL_MANIFEST_CHANGED")
    if (
        manifest.get("model_id") != EXPECTED_MODEL_ID
        or manifest.get("revision") != EXPECTED_MODEL_REVISION
    ):
        raise RuntimeError("MODEL_REVISION_CHANGED")
    if hashlib.sha256(ENGLISH_TEXT.encode("utf-8")).hexdigest().upper() != ENGLISH_TEXT_SHA256:
        raise RuntimeError("ENGLISH_TEXT_CHANGED")
    if not PROVIDED_ENGLISH_VIDEO.is_file():
        raise RuntimeError("PROVIDED_ENGLISH_VIDEO_MISSING")
    remaining_before = _remaining_workers()
    if remaining_before:
        raise RuntimeError(f"XTTS_PROCESS_PRESENT_BEFORE_RETRY:{remaining_before}")

    manager = VoiceProfileManager()
    profile = manager.load(PROFILE_ID)
    manager.consent(PROFILE_ID)
    references = manager.resolve_references(PROFILE_ID)
    if len(references) != 1 or _sha256(references[0]) != REFERENCE_SHA256:
        raise RuntimeError("REFERENCE_CHANGED")

    writer_probe = _writer_probe(runtime_root)
    ffmpeg = resolve_ffmpeg(runtime_root)
    backend = XttsV2Backend(runtime_root)
    memory = PeakMemoryMonitor()
    approval = {
        "schema_version": 1,
        "approval": APPROVAL_TOKEN,
        "approved": True,
        "scope": "exactly_one_english_post_save_retry",
        "recorded_at": utc_now(),
        "constraints": {
            "language": "en",
            "cpu_only": True,
            "korean_or_chinese_rerun": False,
            "model_revision": MODEL_REVISION,
            "profile_id": PROFILE_ID,
            "reference_sha256": REFERENCE_SHA256,
            "text_sha256": ENGLISH_TEXT_SHA256,
            "seed": 42,
            "speed": 1.0,
            "inference_parameters_changed": False,
            "dependencies_changed": False,
            "post_processing_changed": False,
        },
    }
    write_json_exclusive(approval_path, approval)
    dispatch = {
        "schema_version": 1,
        "approval": APPROVAL_TOKEN,
        "language": "en",
        "retry_call_count": 1,
        "lifetime_english_synthesis_call_index": 2,
        "dispatched_at": utc_now(),
        "retry_allowed_after_this_call": False,
        "output_path": str(output),
    }
    write_json_exclusive(dispatch_marker, dispatch)

    started = time.perf_counter()
    health_created_by_this_run = False
    try:
        stages: list[dict[str, Any]] = []
        with PeakMemoryMonitor() as memory:
            metrics = backend.synthesize(
                job={
                    "text": ENGLISH_TEXT,
                    "language": "en",
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
                cancel_token=None,
            )
        validation = dict(validate_generated_wav(output, ffmpeg))
        validation.update(_pcm_evidence(output))
        remaining = _remaining_workers()
        if remaining:
            raise RuntimeError(f"XTTS_PROCESS_REMAINED:{remaining}")
        if _sha256(ko_output) != KO_SHA256 or _sha256(zh_output) != ZH_CN_SHA256:
            raise RuntimeError("KO_OR_ZH_OUTPUT_CHANGED_AFTER_EN_RETRY")
        elapsed = time.perf_counter() - started
        report = {
            "schema_version": 1,
            "status": "PASS",
            "approval": APPROVAL_TOKEN,
            "completed_at": utc_now(),
            "engine": "xtts_v2_multilingual",
            "model_id": MODEL_ID,
            "model_revision": MODEL_REVISION,
            "package_revision": PACKAGE_REVISION,
            "device": "cpu",
            "profile_id": PROFILE_ID,
            "reference_files": [file_record(path) for path in references],
            "text": ENGLISH_TEXT,
            "text_sha256": ENGLISH_TEXT_SHA256,
            "language": "en",
            "seed": 42,
            "speed": 1.0,
            "retry_synthesis_call_count": 1,
            "korean_synthesis_call_count": 0,
            "chinese_synthesis_call_count": 0,
            "removed_temporary_artifacts": removed_temporary_artifacts,
            "writer_probe": writer_probe,
            "writer_source": file_record(worker_source),
            "backend_source": file_record(backend_source),
            "dependency_locks": {
                "requirements-xtts.in.txt": file_record(runtime_root / "requirements-xtts.in.txt"),
                "requirements-xtts.lock.txt": file_record(runtime_root / "requirements-xtts.lock.txt"),
            },
            "provided_english_video": {
                "path": str(PROVIDED_ENGLISH_VIDEO),
                "size_bytes": PROVIDED_ENGLISH_VIDEO.stat().st_size,
                "used_as_reference": False,
                "reason": "The approved retry requires the previous profile/reference unchanged.",
            },
            "output": file_record(output),
            "validation": validation,
            "backend_metrics": metrics,
            "elapsed_seconds": elapsed,
            "peak_ram_gib": memory.peak_bytes / (1024**3),
            "remaining_process_count": 0,
            "stages": stages,
            "preserved_outputs": {
                "ko": file_record(ko_output),
                "zh-cn": file_record(zh_output),
            },
            "original_failure": file_record(original_failure),
            "continuation_report": file_record(continuation_report),
        }
        write_json_exclusive(report_path, report)
        _write_markdown(report_md_path, report)
        write_json_exclusive(
            result_marker,
            {
                "schema_version": 1,
                "status": "PASS",
                "language": "en",
                "retry_synthesis_call_count": 1,
                "output": report["output"],
                "validation": validation,
                "elapsed_seconds": elapsed,
                "peak_ram_gib": report["peak_ram_gib"],
                "remaining_process_count": 0,
                "completed_at": report["completed_at"],
            },
        )

        health = {
            "schema_version": 1,
            "status": "PASS",
            "engine": "xtts_v2_multilingual",
            "model_id": MODEL_ID,
            "model_revision": MODEL_REVISION,
            "package_revision": PACKAGE_REVISION,
            "model_load": "Pass",
            "synthesis_smoke": "Pass",
            "ffmpeg_decode": "Pass",
            "finite_samples": "Pass",
            "not_all_silence": "Pass",
            "languages_validated": ["en", "ko", "zh-cn"],
            "outputs": {
                "en": file_record(output),
                "ko": file_record(ko_output),
                "zh-cn": file_record(zh_output),
            },
            "report_json": str(report_path),
            "report_markdown": str(report_md_path),
            "original_failure_report": str(original_failure),
            "continuation_report": str(continuation_report),
            "completed_at": report["completed_at"],
            "license_id": "Coqui Public Model License 1.0.0",
            "license_url": "https://coqui.ai/cpml.txt",
            "license_scope": "research_personal_poc_noncommercial",
            "commercial_use_claimed": False,
            "approval": APPROVAL_TOKEN,
            "call_policy": "EN initial call failed post-synthesis commit; exactly one approved EN retry; KO/ZH not rerun.",
        }
        write_json_exclusive(health_path, health)
        health_created_by_this_run = True
        capability = EngineRegistry(runtime_root).get("xtts_v2_multilingual")
        if not capability.available or not {"en", "ko", "zh-cn"}.issubset(capability.languages):
            raise RuntimeError(f"CAPABILITY_NOT_AVAILABLE_AFTER_HEALTH:{capability.as_dict()}")
        write_json_exclusive(
            capability_path,
            {
                "schema_version": 1,
                "status": "PASS",
                "engine_health": file_record(health_path),
                "capability": capability.as_dict(),
                "verified_at": utc_now(),
            },
        )
        print(
            json.dumps(
                {
                    "report": report,
                    "engine_health": health,
                    "capability": capability.as_dict(),
                },
                ensure_ascii=True,
                indent=2,
            )
        )
        return 0
    except Exception as exc:
        if health_created_by_this_run and health_path.exists():
            health_path.unlink()
        remaining = _remaining_workers()
        failure = {
            "schema_version": 1,
            "status": "FAILED",
            "approval": APPROVAL_TOKEN,
            "failed_at": utc_now(),
            "error": repr(exc),
            "retry_synthesis_call_count": 1,
            "retry_allowed_after_this_call": False,
            "output_exists": output.exists(),
            "output": file_record(output) if output.exists() else None,
            "elapsed_seconds": time.perf_counter() - started,
            "peak_ram_gib": None if memory is None else memory.peak_bytes / (1024**3),
            "remaining_processes": remaining,
            "ko": file_record(ko_output),
            "zh-cn": file_record(zh_output),
            "engine_health_exists": health_path.exists(),
        }
        if not failure_path.exists():
            write_json_exclusive(failure_path, failure)
        if not result_marker.exists():
            write_json_exclusive(result_marker, failure)
        print(json.dumps(failure, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUTF8", "1")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    raise SystemExit(main())
