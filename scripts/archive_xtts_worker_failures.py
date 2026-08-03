"""Archive the two historical GUI XTTS failures without altering their runs."""

from __future__ import annotations

import importlib.metadata
import json
import os
import platform
import sys
from pathlib import Path


RUN_IDS = (
    "2563231c-c620-4fd6-98e1-653a4ae7be81",
    "febcadb1-c0b3-4788-9bda-8736bf982a07",
)
MODEL_REVISION = "6c2b0d75eae4b7047358e3b6bd9325f857d43f77"


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def package(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "MISSING"


def main() -> int:
    runtime = Path(__file__).resolve().parents[1]
    local = Path(os.environ["LOCALAPPDATA"]) / "FrameExtractStudio" / "VoiceDubbing"
    python = runtime / ".venv-xtts" / "Scripts" / "python.exe"
    profile_dir = local / "profiles" / "lestehrolt_en_clean"
    profile = read_json(profile_dir / "profile.json")
    reference = profile_dir / str(profile["reference_files"][0]["path"])
    command = [str(python), "-u", "-m", "voice_dubbing_runtime.xtts_engine_worker"]
    environment = {
        "captured_from": "current pinned environment after historical failure",
        "python_executable": str(python),
        "python_expected": "3.11.15",
        "coqui_tts": "0.27.5",
        "torch": "2.6.0+cpu",
        "torchaudio": "2.6.0+cpu",
        "transformers": "4.57.6",
        "model_revision": MODEL_REVISION,
        "current_script_python": sys.executable,
        "current_script_platform": platform.platform(),
    }
    for run_id in RUN_IDS:
        source = local / "runs" / run_id
        job = read_json(source / "job.json")
        result = read_json(source / "result.json")
        details = result.get("details", {})
        stdout = str(details.get("stdout_tail", ""))
        stderr = str(details.get("stderr_tail", ""))
        target = runtime / "runs" / f"xtts_worker_failure_{run_id}"
        target.mkdir(parents=True, exist_ok=True)
        (target / "worker_stdout.log").write_text(stdout, encoding="utf-8", newline="\n")
        (target / "worker_stderr.log").write_text(stderr, encoding="utf-8", newline="\n")
        invocation = {
            "schema_version": 1,
            "evidence_kind": "historical_reconstruction_from_immutable_GUI_run",
            "source_run": str(source),
            "command": command,
            "command_line": subprocess_list2cmdline(command),
            "python_executable": str(python),
            "virtual_environment": str(runtime / ".venv-xtts"),
            "current_working_directory": str(runtime),
            "environment_variables": {
                "PYTHONUTF8": "1",
                "PYTHONIOENCODING": "utf-8",
                "LOCALAPPDATA": os.environ.get("LOCALAPPDATA", ""),
            },
            "model_path": str(runtime / "models" / "xtts_v2"),
            "model_revision": MODEL_REVISION,
            "profile_path": str(profile_dir),
            "reference_path": str(reference),
            "reference_sha256": profile["reference_files"][0]["sha256"],
            "output_path": str(Path(job["output_dir"]) / "generated.wav"),
            "pid": "Not recorded by historical worker",
            "exit_code": details.get("exit_code"),
            "last_stage": "load_model / inference sentence split",
            "elapsed_seconds": result.get("elapsed_seconds"),
            "peak_ram_gib": result.get("peak_ram_gib"),
            "temporary_files_left": [],
        }
        (target / "worker_invocation.json").write_text(
            json.dumps(invocation, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (target / "environment_report.json").write_text(
            json.dumps(environment, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        report = f"""# XTTS worker failure {run_id}

- Evidence: historical reconstruction from the original immutable GUI run
- Failure stage: `synthesize / sentence splitting`
- Exception: `ImportError: enable_text_splitting=True requires Spacy: pip install spacy[ja]`
- Exit code: `{details.get('exit_code')}`
- Python executable: `{python}`
- Environment: `.venv-xtts`, pinned CPU lock
- Model revision: `{MODEL_REVISION}`
- Profile: `{profile_dir}`
- Reference: `{reference}`
- Elapsed: `{result.get('elapsed_seconds')}` seconds
- Peak RAM: `{result.get('peak_ram_gib')}` GiB
- Temporary files left: `0`

## Root cause

The pinned runtime does not include spaCy. The old worker passed the long text to
Coqui with `enable_text_splitting=True`, which entered Coqui's optional spaCy
splitter and failed before inference. The model, revision, profile, reference,
and output directory were not the cause.

## Full captured worker output / traceback

```text
{stdout}
```
"""
        (target / "failure_report.md").write_text(report, encoding="utf-8", newline="\n")
    return 0


def subprocess_list2cmdline(command: list[str]) -> str:
    # Import lazily so the archive's environment report remains explicit.
    import subprocess

    return subprocess.list2cmdline(command)


if __name__ == "__main__":
    raise SystemExit(main())
