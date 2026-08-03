"""Run the three required XTTS-v2 CPU smokes exactly once each."""

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
        "en",
        "Hello everyone. This is an English voice cloning test created from a reusable voice profile.",
        "generated_en.wav",
    ),
    (
        "ko",
        "안녕하세요, 여러분. 이것은 저장된 음성 프로필을 사용한 한국어 음성 복제 테스트입니다.",
        "generated_ko.wav",
    ),
    (
        "zh-cn",
        "大家好，这是一个使用已保存声音配置文件生成的中文语音克隆测试。",
        "generated_zh_cn.wav",
    ),
)


def _remaining_workers() -> list[dict[str, Any]]:
    try:
        import psutil
    except ImportError:
        return [{"status": "Not verified", "reason": "psutil missing"}]
    result: list[dict[str, Any]] = []
    for process in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            command = " ".join(process.info.get("cmdline") or [])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if "voice_dubbing_runtime.xtts_engine_worker" in command:
            result.append({"pid": process.info["pid"], "name": process.info.get("name")})
    return result


def _write_markdown(path: Path, report: dict[str, Any]) -> None:
    lines = [
        "# XTTS-v2 multilingual CPU smoke",
        "",
        f"- Status: `{report['status']}`",
        f"- Model: `{MODEL_ID}`",
        f"- Revision: `{MODEL_REVISION}`",
        f"- Package: `{PACKAGE_REVISION}`",
        "- License: `Coqui Public Model License 1.0.0`",
        "- Scope: `Research / personal PoC; no commercial-use claim`",
        "",
    ]
    for language, item in report.get("outputs", {}).items():
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
    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")


def main() -> int:
    runtime_root = RUNTIME_ROOT
    run_dir = runtime_root / "runs" / "xtts_v2_multilingual_smoke"
    health_path = runtime_root / "models" / "xtts_v2" / "engine_health.json"
    if run_dir.exists() or health_path.exists():
        raise FileExistsError(
            "Refusing to rerun or overwrite the one-call-per-language XTTS-v2 smoke."
        )
    run_dir.mkdir(parents=True, exist_ok=False)
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
        "outputs": {},
    }
    try:
        for index, (language, text, filename) in enumerate(CASES):
            output = run_dir / filename
            stages: list[dict[str, Any]] = []
            started = time.perf_counter()
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
                raise RuntimeError(f"XTTS-v2 worker process remained after {language}: {remaining}")
            report["outputs"][language] = {
                "synthesis_call_index": index + 1,
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
        report["status"] = "PASS"
        report["completed_at"] = utc_now()
        report["remaining_processes_final"] = _remaining_workers()
        if report["remaining_processes_final"]:
            raise RuntimeError("XTTS-v2 process cleanup failed after all cases")
        report_json = run_dir / "xtts_multilingual_smoke_report.json"
        report_md = run_dir / "xtts_multilingual_smoke_report.md"
        write_json_exclusive(report_json, report)
        _write_markdown(report_md, report)
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
            "languages_validated": [item[0] for item in CASES],
            "report_json": str(report_json),
            "report_markdown": str(report_md),
            "completed_at": report["completed_at"],
            "license_id": "Coqui Public Model License 1.0.0",
            "license_url": "https://coqui.ai/cpml.txt",
            "license_scope": "research_personal_poc_noncommercial",
            "commercial_use_claimed": False,
        }
        write_json_exclusive(health_path, health)
        print(json.dumps(health, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        report["status"] = "FAILED"
        report["failed_at"] = utc_now()
        report["error"] = repr(exc)
        failure = run_dir / "xtts_multilingual_smoke_failure.json"
        if not failure.exists():
            write_json_exclusive(failure, report)
        raise


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUTF8", "1")
    raise SystemExit(main())
