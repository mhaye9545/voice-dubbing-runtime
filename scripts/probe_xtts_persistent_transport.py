"""Verify incremental persistent JSONL transport without loading the model."""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from pathlib import Path


MARKER = "@@XTTS_ENGINE|"


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    python = root / ".venv-xtts" / "Scripts" / "python.exe"
    command = [
        str(python),
        "-u",
        "-m",
        "voice_dubbing_runtime.xtts_engine_worker",
        "--persistent",
    ]
    environment = dict(os.environ)
    environment.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
    process = subprocess.Popen(
        command,
        cwd=str(root),
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    assert process.stdin is not None and process.stdout is not None
    job_id = str(uuid.uuid4())
    process.stdin.write(
        json.dumps({"schema_version": 1, "job_id": job_id, "action": "ping"}) + "\n"
    )
    process.stdin.flush()
    started = time.perf_counter()
    line = process.stdout.readline()
    elapsed = time.perf_counter() - started
    if elapsed > 5.0 or not line.startswith(MARKER):
        process.kill()
        raise RuntimeError(f"Persistent marker was not incremental: {elapsed=}, {line=}")
    payload = json.loads(line[len(MARKER) :])
    if payload.get("job_id") != job_id or payload.get("status") != "success":
        process.kill()
        raise RuntimeError(f"Unexpected ping result: {payload}")
    process.stdin.write(json.dumps({"schema_version": 1, "action": "shutdown"}) + "\n")
    process.stdin.flush()
    process.stdin.close()
    lifecycle = process.stdout.readline()
    code = process.wait(timeout=10)
    report = {
        "status": "PASS",
        "elapsed_seconds": elapsed,
        "worker_pid": payload["metrics"]["worker_pid"],
        "model_load_count": payload["metrics"]["model_load_count"],
        "exit_code": code,
        "lifecycle": lifecycle.strip(),
    }
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
