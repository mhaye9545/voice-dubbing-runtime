"""Consume only the two unattempted XTTS smoke calls after the EN save failure."""

from __future__ import annotations

import json
import os
import sys
import time
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


CASES = (
    (
        "ko",
        "안녕하세요, 여러분. 이것은 저장된 음성 프로필을 사용한 한국어 음성 복제 테스트입니다.",
        "generated_ko.wav",
        2,
    ),
    (
        "zh-cn",
        "大家好，这是一个使用已保存声音配置文件生成的中文语音克隆测试。",
        "generated_zh_cn.wav",
        3,
    ),
)
ORIGINAL_FAILURE_SHA256 = "1DB341E6E3A86FE82E604F396E14E58EF1AAB372BF8A49AB5718312ECC3AE0D7"
WRITER_SOURCE_SHA256_BEFORE = "5AC68117AF06BF4CEC551CD65412B954E8DDF89CDF48AA9D9BF5A989A43BDCDB"


def _remaining_workers() -> list[dict[str, Any]]:
    try:
        import psutil
    except ImportError:
        return [{"status": "Not verified", "reason": "psutil missing"}]
    result: list[dict[str, Any]] = []
    for process in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            command = " ".join(process.info.get("cmdline") or [])
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
        if "voice_dubbing_runtime.xtts_engine_worker" in command:
            result.append({"pid": process.info["pid"], "name": process.info.get("name")})
    return result


def _write_markdown(path: Path, report: dict[str, Any]) -> None:
    lines = [
        "# XTTS-v2 multilingual smoke continuation",
        "",
        f"- Overall status: `{report['status']}`",
        f"- Model: `{MODEL_ID}`",
        f"- Revision: `{MODEL_REVISION}`",
        f"- Package: `{PACKAGE_REVISION}`",
        "- Retry policy: `No retry; one dispatch per language`",
        "- EN: model synthesis returned audio, but WAV commit failed after synthesis.",
        "- EN output: `Missing`; decode/duration/silence/clipping: `Not run`.",
        "- Engine health created: `False`",
        "",
    ]
    for language in ("ko", "zh-cn"):
        item = report["outputs"].get(language)
        if item is None:
            failure = report["failures"].get(language, {})
            lines.extend(
                [
                    f"## {language}",
                    "",
                    "- Synthesis: `Failed`",
                    f"- Error: `{failure.get('error', 'Not recorded')}`",
                    f"- Elapsed: `{failure.get('elapsed_seconds', 'Not recorded')}`",
                    f"- Peak RAM: `{failure.get('peak_ram_gib', 'Not recorded')}`",
                    f"- Remaining processes: `{failure.get('remaining_processes', 'Not recorded')}`",
                    "",
                ]
            )
            continue
        validation = item["validation"]
        lines.extend(
            [
                f"## {language}",
                "",
                f"- Output: `{item['output']['path']}`",
                f"- Model load: `Pass` ({item['backend_metrics']['model_load_elapsed_seconds']:.3f} s)",
                f"- Synthesis: `Pass` ({item['backend_metrics']['synthesis_elapsed_seconds']:.3f} s)",
                f"- Duration: `{validation['duration_seconds']:.3f} s`",
                f"- Peak / RMS: `{validation['peak']:.6f}` / `{validation['rms']:.6f}`",
                f"- Clipping ratio: `{validation['clipping_ratio']:.8f}`",
                f"- FFmpeg strict decode: `{validation['ffmpeg_decode']}`",
                f"- Elapsed: `{item['elapsed_seconds']:.3f} s`",
                f"- Peak RAM: `{item['peak_ram_gib']:.3f} GiB`",
                f"- Remaining worker processes: `{item['remaining_process_count']}`",
                "",
            ]
        )
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines))


def main() -> int:
    runtime_root = RUNTIME_ROOT
    run_dir = runtime_root / "runs" / "xtts_v2_multilingual_smoke"
    failure_path = run_dir / "xtts_multilingual_smoke_failure.json"
    report_path = run_dir / "xtts_multilingual_smoke_continuation_report.json"
    report_md_path = run_dir / "xtts_multilingual_smoke_continuation_report.md"
    health_path = runtime_root / "models" / "xtts_v2" / "engine_health.json"
    english_output = run_dir / "generated_en.wav"
    if not failure_path.is_file() or file_record(failure_path)["sha256"] != ORIGINAL_FAILURE_SHA256:
        raise RuntimeError("ORIGINAL_EN_FAILURE_ARTIFACT_CHANGED")
    if english_output.exists():
        raise RuntimeError("UNEXPECTED_EN_OUTPUT_EXISTS")
    if health_path.exists():
        raise RuntimeError("ENGINE_HEALTH_MUST_NOT_EXIST_AFTER_FAILED_EN_GATE")
    if report_path.exists() or report_md_path.exists():
        raise FileExistsError("Refusing to rerun the no-retry continuation.")

    worker_source = runtime_root / "voice_dubbing_runtime" / "xtts_engine_worker.py"
    manager = VoiceProfileManager()
    profile = manager.load("lua_china_base")
    manager.consent("lua_china_base")
    references = manager.resolve_references("lua_china_base")
    ffmpeg = resolve_ffmpeg(runtime_root)
    backend = XttsV2Backend(runtime_root)
    report: dict[str, Any] = {
        "schema_version": 1,
        "status": "RUNNING",
        "started_at": utc_now(),
        "engine": "xtts_v2_multilingual",
        "model_id": MODEL_ID,
        "model_revision": MODEL_REVISION,
        "package_revision": PACKAGE_REVISION,
        "profile_id": "lua_china_base",
        "reference_files": [file_record(path) for path in references],
        "synthesis_call_policy": "exactly_one_per_language_no_retry",
        "original_failure": file_record(failure_path),
        "root_cause": "Windows os.fsync rejected the read-only temporary WAV descriptor after EN model.synthesize returned.",
        "writer_fix": {
            "scope": "temporary WAV persistence only; rb changed to r+b before os.fsync",
            "source_sha256_before": WRITER_SOURCE_SHA256_BEFORE,
            "source_after": file_record(worker_source),
            "model_changed": False,
            "inference_parameters_changed": False,
        },
        "calls": {
            "en": {
                "synthesis_call_index": 1,
                "synthesis_call_count_for_language": 1,
                "model_synthesize_returned": True,
                "output_commit": "Failed",
                "output_exists": False,
                "ffmpeg_decode": "Not run",
                "duration": "Not recorded",
                "silence": "Not run",
                "clipping": "Not run",
                "elapsed": "Not recorded",
                "peak_ram": "Not recorded",
                "retry_performed": False,
            }
        },
        "outputs": {},
        "failures": {},
    }

    for language, text, filename, call_index in CASES:
        output = run_dir / filename
        dispatch_marker = run_dir / f"call_{call_index:02d}_{language.replace('-', '_')}_dispatched.json"
        result_marker = run_dir / f"call_{call_index:02d}_{language.replace('-', '_')}_result.json"
        if output.exists() or dispatch_marker.exists() or result_marker.exists():
            raise RuntimeError(f"NO_RETRY_GUARD_TRIGGERED:{language}")
        dispatch = {
            "schema_version": 1,
            "language": language,
            "synthesis_call_index": call_index,
            "synthesis_call_count_for_language": 1,
            "dispatched_at": utc_now(),
            "retry_allowed": False,
            "output_path": str(output),
        }
        write_json_exclusive(dispatch_marker, dispatch)
        started = time.perf_counter()
        memory: PeakMemoryMonitor | None = None
        try:
            stages: list[dict[str, Any]] = []
            with PeakMemoryMonitor() as memory:
                metrics = backend.synthesize(
                    job={
                        "text": text,
                        "language": language,
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
            validation = validate_generated_wav(output, ffmpeg)
            remaining = _remaining_workers()
            if remaining:
                raise RuntimeError(f"XTTS_WORKER_PROCESS_REMAINED:{remaining}")
            item = {
                "synthesis_call_index": call_index,
                "synthesis_call_count_for_language": 1,
                "text": text,
                "output": file_record(output),
                "validation": validation,
                "backend_metrics": metrics,
                "elapsed_seconds": time.perf_counter() - started,
                "peak_ram_gib": memory.peak_bytes / (1024**3),
                "remaining_process_count": 0,
                "stages": stages,
            }
            report["outputs"][language] = item
            report["calls"][language] = {
                "synthesis_call_index": call_index,
                "synthesis_call_count_for_language": 1,
                "status": "PASS",
                "retry_performed": False,
            }
            write_json_exclusive(
                result_marker,
                {"schema_version": 1, "status": "PASS", "language": language, **item},
            )
        except Exception as exc:
            remaining = _remaining_workers()
            failure = {
                "synthesis_call_index": call_index,
                "synthesis_call_count_for_language": 1,
                "error": repr(exc),
                "elapsed_seconds": time.perf_counter() - started,
                "peak_ram_gib": None if memory is None else memory.peak_bytes / (1024**3),
                "remaining_processes": remaining,
                "output_exists": output.exists(),
                "retry_performed": False,
            }
            report["failures"][language] = failure
            report["calls"][language] = {
                "synthesis_call_index": call_index,
                "synthesis_call_count_for_language": 1,
                "status": "FAILED",
                "retry_performed": False,
            }
            write_json_exclusive(
                result_marker,
                {"schema_version": 1, "status": "FAILED", "language": language, **failure},
            )
            if remaining:
                break

    report["completed_at"] = utc_now()
    report["remaining_processes_final"] = _remaining_workers()
    report["engine_health_created"] = health_path.exists()
    capability = EngineRegistry(runtime_root).get("xtts_v2_multilingual")
    report["capability"] = capability.as_dict()
    if report["remaining_processes_final"]:
        report["status"] = "FAILED_PROCESS_CLEANUP"
    elif report["failures"]:
        report["status"] = "FAILED_MULTIPLE"
    else:
        report["status"] = "FAILED_EN_ARTIFACT_COMMIT"
    if report["engine_health_created"] or capability.available:
        raise RuntimeError("FAILED_SMOKE_MUST_NOT_ENABLE_XTTS_CAPABILITY")
    write_json_exclusive(report_path, report)
    _write_markdown(report_md_path, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUTF8", "1")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    raise SystemExit(main())
