"""Load the pinned XTTS-v2 model on CPU without making a synthesis call."""

from __future__ import annotations

import gc
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

import psutil

RUNTIME_ROOT = Path(__file__).resolve().parents[1]
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

from voice_dubbing_runtime.io_utils import file_record, utc_now, write_json_exclusive
from voice_dubbing_runtime.xtts_engine_worker import (
    LICENSE_ID,
    MODEL_ID,
    MODEL_REVISION,
    _verify_model_and_license,
)


class PeakMemorySampler:
    """Sample RSS for this process and any children while the gate is active."""

    def __init__(self) -> None:
        self._process = psutil.Process(os.getpid())
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._sample_loop, daemon=True)
        self.peak_bytes = 0

    def _sample(self) -> None:
        processes = [self._process]
        try:
            processes.extend(self._process.children(recursive=True))
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            pass
        total = 0
        for process in processes:
            try:
                total += process.memory_info().rss
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                continue
        self.peak_bytes = max(self.peak_bytes, total)

    def _sample_loop(self) -> None:
        while not self._stop.wait(0.05):
            self._sample()

    def __enter__(self) -> "PeakMemorySampler":
        self._sample()
        self._thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)
        self._sample()


def _remaining_workers() -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for process in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            command = " ".join(process.info.get("cmdline") or [])
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
        if "voice_dubbing_runtime.xtts_engine_worker" in command:
            result.append({"pid": process.info["pid"], "name": process.info.get("name")})
    return result


def main() -> int:
    runtime_root = RUNTIME_ROOT
    run_dir = runtime_root / "runs" / "xtts_v2_model_load_probe"
    if run_dir.exists():
        raise FileExistsError("Refusing to rerun or overwrite the XTTS-v2 model-load gate.")
    run_dir.mkdir(parents=True, exist_ok=False)
    report_path = run_dir / "model_load_report.json"
    failure_path = run_dir / "model_load_failure.json"
    started_at = utc_now()
    gate_started = time.perf_counter()
    report: dict[str, Any] = {
        "schema_version": 1,
        "status": "RUNNING",
        "started_at": started_at,
        "gate": "XTTS_V2_CPU_MODEL_LOAD_ONLY",
        "model_id": MODEL_ID,
        "model_revision": MODEL_REVISION,
        "license_id": LICENSE_ID,
        "synthesis_call_count": 0,
        "device": "cpu",
    }
    model = None
    config = None
    try:
        with PeakMemorySampler() as memory:
            verification_started = time.perf_counter()
            model_dir, manifest = _verify_model_and_license(runtime_root)
            verification_elapsed = time.perf_counter() - verification_started

            # Torch 2.6 defaults to weights_only=True. This opt-out is scoped to
            # the already hash-verified, exact-revision official checkpoint.
            os.environ["TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD"] = "1"
            import torch
            import TTS
            from TTS.tts.configs.xtts_config import XttsConfig
            from TTS.tts.models.xtts import Xtts

            load_started = time.perf_counter()
            config = XttsConfig()
            config.load_json(str(model_dir / "config.json"))
            model = Xtts.init_from_config(config)
            model.load_checkpoint(
                config,
                checkpoint_dir=str(model_dir),
                eval=True,
                use_deepspeed=False,
            )
            model.to("cpu")
            model_load_elapsed = time.perf_counter() - load_started
            languages = [str(value).lower().replace("_", "-") for value in config.languages]
            required_languages = ["en", "ko", "zh-cn"]
            missing_languages = [value for value in required_languages if value not in languages]
            if missing_languages:
                raise RuntimeError(f"XTTS_CONFIG_LANGUAGES_MISSING:{missing_languages}")

        del model, config
        model = None
        config = None
        gc.collect()
        remaining = _remaining_workers()
        if remaining:
            raise RuntimeError(f"XTTS_WORKER_PROCESS_REMAINED:{remaining}")
        report.update(
            {
                "status": "PASS",
                "completed_at": utc_now(),
                "model_load": "Pass",
                "manifest_verification": "Pass",
                "manifest_verification_elapsed_seconds": verification_elapsed,
                "model_load_elapsed_seconds": model_load_elapsed,
                "elapsed_seconds": time.perf_counter() - gate_started,
                "peak_ram_gib": memory.peak_bytes / (1024**3),
                "languages": languages,
                "required_smoke_languages": required_languages,
                "model_manifest": file_record(model_dir / "model_manifest.json"),
                "manifest_total_size_bytes": manifest.get("total_size_bytes"),
                "package_revision": f"coqui-tts=={getattr(TTS, '__version__', 'unknown')}",
                "torch_version": torch.__version__,
                "remaining_process_count": 0,
            }
        )
        write_json_exclusive(report_path, report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        report.update(
            {
                "status": "FAILED",
                "failed_at": utc_now(),
                "error": repr(exc),
                "elapsed_seconds": time.perf_counter() - gate_started,
                "remaining_processes": _remaining_workers(),
            }
        )
        if not failure_path.exists():
            write_json_exclusive(failure_path, report)
        raise
    finally:
        if model is not None:
            del model
        if config is not None:
            del config
        gc.collect()


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUTF8", "1")
    raise SystemExit(main())
