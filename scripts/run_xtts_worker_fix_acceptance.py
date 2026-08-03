"""Run the approved one-shot and persistent XTTS worker acceptance sequence."""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import psutil

RUNTIME_ROOT = Path(__file__).resolve().parents[1]
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

from voice_dubbing_runtime.media import resolve_ffmpeg
from voice_dubbing_runtime.profiles import VoiceProfileManager
from voice_dubbing_runtime.worker import _ffmpeg_decode, validate_generated_wav
from voice_dubbing_runtime.xtts_backend import MODEL_REVISION, XttsV2Backend


PROFILE_ID = "lestehrolt_en_clean"
ONE_SHOT_TEXT = (
    "Good evening. This is a short XTTS worker verification using the Lester Holt English voice profile."
)
PERSISTENT_1_TEXT = (
    "This is the first persistent worker test. The model should load once and remain available in memory."
)
PERSISTENT_2_TEXT = (
    "This is the second persistent worker test. The XTTS model should be reused without loading the checkpoint again."
)


class NeverCancelled:
    @staticmethod
    def is_cancelled() -> bool:
        return False


def xtts_worker_processes() -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for process in psutil.process_iter(["pid", "exe", "cmdline"]):
        try:
            command = " ".join(process.info.get("cmdline") or [])
            if "voice_dubbing_runtime.xtts_engine_worker" in command:
                found.append(
                    {"pid": process.pid, "exe": process.info.get("exe"), "cmdline": command}
                )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return found


def job(text: str, *, keep: bool) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "job_id": str(uuid.uuid4()),
        "action": "synthesize",
        "profile_id": PROFILE_ID,
        "text": text,
        "language": "en",
        "engine": "xtts_v2_multilingual",
        "device": "cpu",
        "speed": 1.0,
        "seed": 42,
        "keep_model_loaded": keep,
    }


def main() -> int:
    runtime = Path(__file__).resolve().parents[1]
    run = runtime / "runs" / "xtts_worker_fix"
    run.mkdir(parents=True, exist_ok=True)
    outputs = {
        "one_shot": run / "one_shot_test.wav",
        "persistent_1": run / "persistent_test_01.wav",
        "persistent_2": run / "persistent_test_02.wav",
    }
    collisions = [str(path) for path in outputs.values() if path.exists()]
    if collisions:
        raise FileExistsError(f"Refusing to overwrite acceptance output(s): {collisions}")

    manager = VoiceProfileManager()
    profile = manager.assert_synthesis_ready(PROFILE_ID)
    manager.consent(PROFILE_ID)
    references = manager.resolve_references(PROFILE_ID)
    profile = {
        **profile,
        "_profile_path": str((manager.root / PROFILE_ID).resolve()),
        "_profile_revision": manager.profile_revision(PROFILE_ID),
    }
    ffmpeg = resolve_ffmpeg(runtime)
    for reference in references:
        _ffmpeg_decode(reference, ffmpeg)

    progress_log: list[dict[str, Any]] = []

    def progress(stage: str, value: float) -> None:
        progress_log.append(
            {"stage": stage, "progress": value, "at": time.time()}
        )

    report: dict[str, Any] = {
        "schema_version": 1,
        "model_revision": MODEL_REVISION,
        "profile_id": PROFILE_ID,
        "profile_revision": profile["_profile_revision"],
        "references": [str(path) for path in references],
        "processes_before": xtts_worker_processes(),
    }

    def checkpoint(stage: str, **extra: Any) -> None:
        payload = {
            "schema_version": 1,
            "stage": stage,
            "updated_at_epoch": time.time(),
            **extra,
        }
        (run / "acceptance_state.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    if report["processes_before"]:
        raise RuntimeError(f"Stale XTTS worker exists before acceptance: {report['processes_before']}")

    one_backend = XttsV2Backend(runtime)
    one_job = job(ONE_SHOT_TEXT, keep=False)
    checkpoint("one_shot_started", job=one_job, output=str(outputs["one_shot"]))
    one_started = time.perf_counter()
    try:
        one_metrics = one_backend.synthesize(
            job=one_job,
            profile=profile,
            references=references,
            output_path=outputs["one_shot"],
            progress=progress,
            cancel_token=NeverCancelled(),
        )
    finally:
        one_backend.close()
    one_elapsed = time.perf_counter() - one_started
    time.sleep(0.5)
    one_validation = validate_generated_wav(outputs["one_shot"], ffmpeg)
    one_left = xtts_worker_processes()
    if one_left:
        raise RuntimeError(f"One-shot worker did not exit: {one_left}")
    report["one_shot"] = {
        "job_id": one_job["job_id"],
        "synthesis_call_count": 1,
        "output": str(outputs["one_shot"]),
        "elapsed_seconds": one_elapsed,
        "metrics": one_metrics,
        "validation": one_validation,
        "processes_left": one_left,
    }
    checkpoint("one_shot_passed", one_shot=report["one_shot"])

    persistent = XttsV2Backend(runtime)
    persistent_records: list[dict[str, Any]] = []
    try:
        for key, text in (
            ("persistent_1", PERSISTENT_1_TEXT),
            ("persistent_2", PERSISTENT_2_TEXT),
        ):
            current_job = job(text, keep=True)
            checkpoint(
                f"{key}_started",
                job=current_job,
                output=str(outputs[key]),
                completed_persistent_jobs=persistent_records,
            )
            started = time.perf_counter()
            metrics = persistent.synthesize(
                job=current_job,
                profile=profile,
                references=references,
                output_path=outputs[key],
                progress=progress,
                cancel_token=NeverCancelled(),
            )
            elapsed = time.perf_counter() - started
            validation = validate_generated_wav(outputs[key], ffmpeg)
            active = xtts_worker_processes()
            persistent_records.append(
                {
                    "job_id": current_job["job_id"],
                    "synthesis_call_count": 1,
                    "output": str(outputs[key]),
                    "elapsed_seconds": elapsed,
                    "metrics": metrics,
                    "validation": validation,
                    "processes_while_active": active,
                }
            )
            checkpoint(
                f"{key}_passed",
                completed_persistent_jobs=persistent_records,
            )
            if len(active) != 1 or int(active[0]["pid"]) != int(metrics["worker_pid"]):
                raise RuntimeError(f"Persistent worker inventory mismatch: {active}, {metrics}")
        first, second = persistent_records
        if first["metrics"]["worker_pid"] != second["metrics"]["worker_pid"]:
            raise RuntimeError("Persistent jobs used different worker PIDs")
        if first["metrics"]["model_load_count"] != 1 or second["metrics"]["model_load_count"] != 1:
            raise RuntimeError("Persistent worker loaded the model more than once")
        if second["metrics"]["model_load_elapsed_seconds"] != 0.0:
            raise RuntimeError("Second persistent job reloaded the checkpoint")
        if second["metrics"]["conditioning_cache_hit"] is not True:
            raise RuntimeError("Second persistent job did not reuse matching conditioning")
    finally:
        persistent.close()
    time.sleep(0.5)
    processes_left = xtts_worker_processes()
    if processes_left:
        raise RuntimeError(f"Persistent worker did not shut down: {processes_left}")
    report["persistent"] = {
        "jobs": persistent_records,
        "worker_pid": persistent_records[0]["metrics"]["worker_pid"],
        "model_load_count": persistent_records[-1]["metrics"]["model_load_count"],
        "model_reused": True,
        "shutdown": "Pass",
        "processes_left": processes_left,
    }
    report["progress"] = progress_log
    report["status"] = "PASS"
    checkpoint("completed", report=report)
    (run / "acceptance_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    lines = [
        "# XTTS worker fix acceptance",
        "",
        "- Status: **PASS**",
        f"- Model revision: `{MODEL_REVISION}`",
        f"- Profile: `{PROFILE_ID}` revision `{profile['_profile_revision']}`",
        f"- One-shot output: `{outputs['one_shot']}`",
        f"- Persistent PID: `{report['persistent']['worker_pid']}`",
        "- Model load count after two persistent jobs: `1`",
        "- Process left after shutdown: `0`",
        "",
    ]
    (run / "acceptance_report.md").write_text("\n".join(lines), encoding="utf-8", newline="\n")
    print(json.dumps(report, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
