"""Thin adapter for the isolated XTTS-v2 Python environment.

This module imports no ML package. Heavy imports and the persistent model live
only in ``.venv-xtts`` and communicate through an explicit JSON-lines protocol.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Sequence

try:
    import psutil
except ImportError:  # pragma: no cover - memory guard degrades to unknown
    psutil = None

from .errors import (
    CANCELLED,
    ENGINE_UNAVAILABLE,
    INVALID_REFERENCE,
    SYNTHESIS_FAILED,
    XTTS_OUT_OF_MEMORY,
    XTTS_REFERENCE_INVALID,
    XTTS_WORKER_CRASHED,
    XTTS_WORKER_LAUNCH_FAILED,
    XTTS_WORKER_TIMEOUT,
    VoiceRuntimeError,
)
from .io_utils import read_json, sha256_file, utc_now


ENGINE_MARKER = "@@XTTS_ENGINE|"
MODEL_ID = "coqui/XTTS-v2"
MODEL_REVISION = "6c2b0d75eae4b7047358e3b6bd9325f857d43f77"
PACKAGE_REVISION = "coqui-tts==0.27.5"
PERSISTENT_MIN_AVAILABLE_RAM_BYTES = int(5.5 * 1024**3)
WORKER_TIMEOUT_SECONDS = 3600.0


class XttsV2Backend:
    engine_id = "xtts_v2_multilingual"

    def __init__(self, runtime_root: Path) -> None:
        self.runtime_root = runtime_root.resolve()
        self.python = (self.runtime_root / ".venv-xtts" / "Scripts" / "python.exe").resolve()
        self.model_dir = (self.runtime_root / "models" / "xtts_v2").resolve()
        self._process: subprocess.Popen[str] | None = None
        self._events: queue.Queue[tuple[str, str]] = queue.Queue()
        self._reader_threads: list[threading.Thread] = []
        self._stdout_lines: list[str] = []
        self._stderr_lines: list[str] = []
        self._lock = threading.RLock()
        self._unhealthy = False

    @property
    def worker_pid(self) -> int | None:
        process = self._process
        return process.pid if process is not None and process.poll() is None else None

    @property
    def persistent_alive(self) -> bool:
        return self.worker_pid is not None and not self._unhealthy

    @staticmethod
    def _cancelled(cancel_token: Any) -> bool:
        checker = getattr(cancel_token, "is_cancelled", None)
        return bool(checker()) if checker else False

    @staticmethod
    def _kill_tree(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        else:
            process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()

    def _verify_launcher(self) -> tuple[str, ...]:
        expected_python = self.runtime_root / ".venv-xtts" / "Scripts" / "python.exe"
        if self.python != expected_python.resolve():
            raise VoiceRuntimeError(
                XTTS_WORKER_LAUNCH_FAILED,
                "XTTS-v2 Python executable does not match the isolated runtime.",
                {"expected": str(expected_python), "actual": str(self.python)},
            )
        required = (
            self.python,
            self.model_dir / "config.json",
            self.model_dir / "model.pth",
            self.model_dir / "vocab.json",
            self.model_dir / "LICENSE.txt",
            self.model_dir / "model_manifest.json",
        )
        missing = [str(path) for path in required if not path.is_file() or path.stat().st_size == 0]
        if missing:
            raise VoiceRuntimeError(
                ENGINE_UNAVAILABLE,
                "XTTS-v2 runtime/model files are not provisioned.",
                {"engine": self.engine_id, "missing": missing},
            )
        manifest = read_json(self.model_dir / "model_manifest.json")
        if manifest.get("model_id") != MODEL_ID or manifest.get("revision") != MODEL_REVISION:
            raise VoiceRuntimeError(
                ENGINE_UNAVAILABLE,
                "XTTS-v2 manifest does not match the pinned official revision.",
                {"expected_model_id": MODEL_ID, "expected_revision": MODEL_REVISION},
            )
        config = read_json(self.model_dir / "config.json")
        languages = config.get("languages")
        if not isinstance(languages, list) or not languages:
            raise VoiceRuntimeError(ENGINE_UNAVAILABLE, "XTTS-v2 config declares no languages.")
        return tuple(
            str(value).strip().lower().replace("_", "-")
            for value in languages
            if isinstance(value, str) and value.strip()
        )

    @staticmethod
    def _environment() -> dict[str, str]:
        environment = dict(os.environ)
        environment.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
        environment.pop("TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD", None)
        return environment

    def _command(self, persistent: bool) -> list[str]:
        command = [str(self.python), "-u", "-m", "voice_dubbing_runtime.xtts_engine_worker"]
        if persistent:
            command.append("--persistent")
        return command

    @staticmethod
    def _reader(channel: str, stream: Any, events: queue.Queue[tuple[str, str]], sink: list[str]) -> None:
        try:
            for raw in stream:
                line = raw.rstrip("\r\n")
                sink.append(line)
                events.put((channel, line))
        finally:
            events.put((channel, "@@EOF"))

    def _start_persistent(self) -> subprocess.Popen[str]:
        if self._process is not None and self._process.poll() is None:
            return self._process
        if psutil is not None:
            available = int(psutil.virtual_memory().available)
            if available < PERSISTENT_MIN_AVAILABLE_RAM_BYTES:
                raise VoiceRuntimeError(
                    XTTS_OUT_OF_MEMORY,
                    "Not enough available RAM to keep XTTS-v2 loaded safely.",
                    {
                        "available_ram_gib": available / (1024**3),
                        "required_ram_gib": PERSISTENT_MIN_AVAILABLE_RAM_BYTES / (1024**3),
                    },
                )
        command = self._command(True)
        try:
            process = subprocess.Popen(
                command,
                cwd=str(self.runtime_root),
                env=self._environment(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            raise VoiceRuntimeError(
                XTTS_WORKER_LAUNCH_FAILED,
                "Could not launch the XTTS-v2 worker.",
                {"command": command, "exception_type": type(exc).__name__, "traceback": traceback.format_exc()},
            ) from exc
        assert process.stdout is not None and process.stderr is not None
        self._events = queue.Queue()
        self._stdout_lines = []
        self._stderr_lines = []
        self._reader_threads = [
            threading.Thread(target=self._reader, args=("stdout", process.stdout, self._events, self._stdout_lines), daemon=True),
            threading.Thread(target=self._reader, args=("stderr", process.stderr, self._events, self._stderr_lines), daemon=True),
        ]
        for thread in self._reader_threads:
            thread.start()
        self._process = process
        self._unhealthy = False
        return process

    @staticmethod
    def _parse_marker(line: str) -> dict[str, Any] | None:
        if not line.startswith(ENGINE_MARKER):
            return None
        try:
            value = json.loads(line[len(ENGINE_MARKER) :])
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None

    def _request_payload(
        self,
        *,
        action: str,
        job: dict[str, Any],
        profile: dict[str, Any],
        references: Sequence[Path],
        output_path: Path,
    ) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "job_id": str(job.get("job_id", "")),
            "action": action,
            "text": str(job.get("text", "")),
            "language": str(job.get("language", "")).strip().lower().replace("_", "-"),
            "device": "cpu",
            "speed": float(job.get("speed", 1.0)),
            "seed": int(job.get("seed", 42)),
            "references": [str(path.resolve()) for path in references],
            "reference_records": [
                {"path": str(path.resolve()), "sha256": sha256_file(path), "size_bytes": path.stat().st_size}
                for path in references
            ],
            "output_path": str(output_path.resolve()),
            "profile_id": str(profile.get("profile_id", "")),
            "profile_path": str(profile.get("_profile_path", "")),
            "profile_revision": int(profile.get("_profile_revision", 0)),
            "keep_model_loaded": bool(job.get("keep_model_loaded", job.get("keep_model_warm", False))),
        }

    def _archive_failure(
        self,
        *,
        request: dict[str, Any],
        command: list[str],
        pid: int | None,
        exit_code: int | None,
        stdout: str,
        stderr: str,
        marker: dict[str, Any] | None,
        elapsed: float,
    ) -> Path | None:
        job_id = str(request.get("job_id") or "unknown")
        target = self.runtime_root / "runs" / f"xtts_worker_failure_{job_id}"
        try:
            target.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            return target
        except OSError:
            return None
        error = marker.get("error") if isinstance(marker, dict) and isinstance(marker.get("error"), dict) else {}
        details = error.get("details") if isinstance(error.get("details"), dict) else {}
        related_environment = {
            key: value
            for key, value in self._environment().items()
            if key.startswith(("PYTHON", "TORCH", "FRAMEEXTRACT", "UV_"))
            or key in {"LOCALAPPDATA", "VIRTUAL_ENV"}
        }
        invocation = {
            "schema_version": 1,
            "captured_at": utc_now(),
            "command": command,
            "command_line": subprocess.list2cmdline(command),
            "python_executable": str(self.python),
            "virtual_environment": str(self.python.parents[2]),
            "current_working_directory": str(self.runtime_root),
            "environment_variables": related_environment,
            "model_path": str(self.model_dir),
            "model_revision": MODEL_REVISION,
            "profile_id": request.get("profile_id"),
            "profile_path": request.get("profile_path"),
            "reference_paths": request.get("references"),
            "reference_records": request.get("reference_records"),
            "output_path": request.get("output_path"),
            "pid": pid,
            "exit_code": exit_code,
            "elapsed_seconds": elapsed,
            "last_stage": details.get("stage"),
            "exception_type": details.get("exception_type"),
            "temporary_files_left": [
                str(path)
                for path in Path(str(request.get("output_path"))).parent.glob(
                    f".{Path(str(request.get('output_path'))).stem}.*.wav"
                )
            ],
        }
        environment_report = details.get("environment") if isinstance(details.get("environment"), dict) else {}
        (target / "worker_stdout.log").write_text(stdout, encoding="utf-8", newline="\n")
        (target / "worker_stderr.log").write_text(stderr, encoding="utf-8", newline="\n")
        (target / "worker_invocation.json").write_text(json.dumps(invocation, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (target / "environment_report.json").write_text(json.dumps(environment_report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        report = "\n".join(
            [
                "# XTTS worker failure",
                "",
                f"- Job ID: `{job_id}`",
                f"- Failure stage: `{details.get('stage', 'unknown')}`",
                f"- Error code: `{error.get('code', XTTS_WORKER_CRASHED)}`",
                f"- Exception: `{details.get('exception_type', 'unknown')}: {error.get('message', '')}`",
                f"- Exit code: `{exit_code}`",
                f"- PID: `{pid}`",
                f"- Elapsed: `{elapsed:.6f}` seconds",
                f"- Python: `{self.python}`",
                f"- Output: `{request.get('output_path')}`",
                "",
                "## Full traceback",
                "",
                "```text",
                str(details.get("traceback") or "Traceback was not emitted before process exit."),
                "```",
                "",
            ]
        )
        (target / "failure_report.md").write_text(report, encoding="utf-8", newline="\n")
        return target

    def _raise_failure(
        self,
        *,
        request: dict[str, Any],
        command: list[str],
        process: subprocess.Popen[str],
        marker: dict[str, Any] | None,
        stdout: str,
        stderr: str,
        elapsed: float,
        fallback_code: str = XTTS_WORKER_CRASHED,
    ) -> None:
        error = marker.get("error") if isinstance(marker, dict) and isinstance(marker.get("error"), dict) else {}
        details = dict(error.get("details")) if isinstance(error.get("details"), dict) else {}
        diagnostic = self._archive_failure(
            request=request,
            command=command,
            pid=process.pid,
            exit_code=process.poll(),
            stdout=stdout,
            stderr=stderr,
            marker=marker,
            elapsed=elapsed,
        )
        if diagnostic is not None:
            details["diagnostic_directory"] = str(diagnostic)
        details.update(
            {
                "exit_code": process.poll(),
                "worker_pid": process.pid,
                "worker_stdout": stdout,
                "worker_stderr": stderr,
            }
        )
        raise VoiceRuntimeError(
            str(error.get("code") or fallback_code),
            str(error.get("message") or "XTTS-v2 worker exited without a valid final response."),
            details,
        )

    def _run_one_shot(
        self,
        request: dict[str, Any],
        progress: Callable[[str, float], None],
        cancel_token: Any,
    ) -> dict[str, Any]:
        command = self._command(False)
        try:
            process = subprocess.Popen(
                command,
                cwd=str(self.runtime_root),
                env=self._environment(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            raise VoiceRuntimeError(
                XTTS_WORKER_LAUNCH_FAILED,
                "Could not launch the XTTS-v2 worker.",
                {"command": command, "exception_type": type(exc).__name__, "traceback": traceback.format_exc()},
            ) from exc
        started = time.perf_counter()
        try:
            assert process.stdin is not None
            process.stdin.write(json.dumps(request, ensure_ascii=False))
            process.stdin.close()
            process.stdin = None
            while process.poll() is None:
                if self._cancelled(cancel_token):
                    self._kill_tree(process)
                    raise VoiceRuntimeError(CANCELLED, "Voice synthesis was cancelled.")
                if time.perf_counter() - started > WORKER_TIMEOUT_SECONDS:
                    self._kill_tree(process)
                    raise VoiceRuntimeError(XTTS_WORKER_TIMEOUT, "XTTS-v2 worker timed out.")
                time.sleep(0.1)
            stdout, stderr = process.communicate()
        finally:
            if process.poll() is None:
                self._kill_tree(process)
        final: dict[str, Any] | None = None
        for line in stdout.splitlines():
            marker = self._parse_marker(line)
            if marker is None:
                continue
            if marker.get("type") == "stage":
                progress(str(marker.get("name", "load_model")), float(marker.get("progress", 0.0)))
            elif marker.get("type") == "result" and marker.get("job_id") == request["job_id"]:
                final = marker
        elapsed = time.perf_counter() - started
        if process.returncode != 0 or final is None or final.get("status") != "success":
            self._raise_failure(
                request=request,
                command=command,
                process=process,
                marker=final,
                stdout=stdout,
                stderr=stderr,
                elapsed=elapsed,
            )
        metrics = final.get("metrics")
        if not isinstance(metrics, dict):
            self._raise_failure(
                request=request,
                command=command,
                process=process,
                marker=final,
                stdout=stdout,
                stderr=stderr,
                elapsed=elapsed,
            )
        return dict(metrics)

    def _run_persistent(
        self,
        request: dict[str, Any],
        progress: Callable[[str, float], None],
        cancel_token: Any,
    ) -> dict[str, Any]:
        process = self._start_persistent()
        command = self._command(True)
        stdout_start = len(self._stdout_lines)
        stderr_start = len(self._stderr_lines)
        started = time.perf_counter()
        try:
            if process.stdin is None:
                raise BrokenPipeError("XTTS-v2 persistent stdin is closed")
            process.stdin.write(json.dumps(request, ensure_ascii=False, separators=(",", ":")) + "\n")
            process.stdin.flush()
        except (OSError, BrokenPipeError) as exc:
            self._unhealthy = True
            self._kill_tree(process)
            raise VoiceRuntimeError(
                XTTS_WORKER_CRASHED,
                "Could not send a job to the XTTS-v2 worker.",
                {"exception_type": type(exc).__name__, "traceback": traceback.format_exc()},
            ) from exc
        final: dict[str, Any] | None = None
        while final is None:
            if self._cancelled(cancel_token):
                self._unhealthy = True
                self._kill_tree(process)
                self._process = None
                raise VoiceRuntimeError(CANCELLED, "Voice synthesis was cancelled.")
            if time.perf_counter() - started > WORKER_TIMEOUT_SECONDS:
                self._unhealthy = True
                self._kill_tree(process)
                self._process = None
                raise VoiceRuntimeError(XTTS_WORKER_TIMEOUT, "XTTS-v2 worker timed out.")
            try:
                channel, line = self._events.get(timeout=0.1)
            except queue.Empty:
                if process.poll() is not None:
                    break
                continue
            if channel != "stdout":
                continue
            marker = self._parse_marker(line)
            if marker is None:
                continue
            if marker.get("type") == "stage" and marker.get("job_id") == request["job_id"]:
                progress(str(marker.get("name", "load_model")), float(marker.get("progress", 0.0)))
            elif marker.get("type") == "result" and marker.get("job_id") == request["job_id"]:
                final = marker
        stdout = "\n".join(self._stdout_lines[stdout_start:])
        stderr = "\n".join(self._stderr_lines[stderr_start:])
        elapsed = time.perf_counter() - started
        if final is None or final.get("status") != "success":
            if process.poll() is not None:
                self._unhealthy = True
                self._process = None
            self._raise_failure(
                request=request,
                command=command,
                process=process,
                marker=final,
                stdout=stdout,
                stderr=stderr,
                elapsed=elapsed,
            )
        metrics = final.get("metrics")
        if not isinstance(metrics, dict):
            self._raise_failure(
                request=request,
                command=command,
                process=process,
                marker=final,
                stdout=stdout,
                stderr=stderr,
                elapsed=elapsed,
            )
        return dict(metrics)

    def _validate_call(
        self,
        *,
        job: dict[str, Any],
        references: Sequence[Path],
        output_path: Path,
    ) -> tuple[str, str, float]:
        languages = self._verify_launcher()
        language = str(job.get("language", "")).strip().lower().replace("_", "-")
        if language not in languages:
            raise VoiceRuntimeError(
                SYNTHESIS_FAILED,
                f"XTTS-v2 does not declare language '{language}'.",
                {"supported_languages": list(languages)},
            )
        if not references or any(not item.is_file() for item in references):
            raise VoiceRuntimeError(XTTS_REFERENCE_INVALID, "XTTS-v2 requires an existing reference WAV.")
        text = str(job.get("text", "")).strip()
        speed = float(job.get("speed", 1.0))
        if not 0.5 <= speed <= 2.0:
            raise VoiceRuntimeError(SYNTHESIS_FAILED, "speed must be between 0.5 and 2.0.")
        if output_path.exists():
            raise FileExistsError(f"Refusing to overwrite XTTS-v2 output: {output_path}")
        if self._cancelled(job.get("cancel_token")):
            raise VoiceRuntimeError(CANCELLED, "Voice synthesis was cancelled.")
        return language, text, speed

    def probe(
        self,
        *,
        job: dict[str, Any],
        profile: dict[str, Any],
        references: Sequence[Path],
        output_path: Path,
        progress: Callable[[str, float], None],
        cancel_token: Any,
        persistent: bool = False,
    ) -> dict[str, Any]:
        self._validate_call(job=job, references=references, output_path=output_path)
        request = self._request_payload(
            action="probe", job=job, profile=profile, references=references, output_path=output_path
        )
        with self._lock:
            return (
                self._run_persistent(request, progress, cancel_token)
                if persistent
                else self._run_one_shot(request, progress, cancel_token)
            )

    def synthesize(
        self,
        *,
        job: dict[str, Any],
        profile: dict[str, Any],
        references: Sequence[Path],
        output_path: Path,
        progress: Callable[[str, float], None],
        cancel_token: Any,
    ) -> dict[str, Any]:
        _language, text, _speed = self._validate_call(
            job=job, references=references, output_path=output_path
        )
        if not text:
            raise VoiceRuntimeError(SYNTHESIS_FAILED, "Synthesis text must not be empty.")
        if self._cancelled(cancel_token):
            raise VoiceRuntimeError(CANCELLED, "Voice synthesis was cancelled.")
        request = self._request_payload(
            action="synthesize", job=job, profile=profile, references=references, output_path=output_path
        )
        persistent = bool(job.get("keep_model_loaded", job.get("keep_model_warm", False)))
        with self._lock:
            return (
                self._run_persistent(request, progress, cancel_token)
                if persistent
                else self._run_one_shot(request, progress, cancel_token)
            )

    def close(self) -> None:
        with self._lock:
            process = self._process
            self._process = None
            if process is None or process.poll() is not None:
                return
            try:
                if process.stdin is not None:
                    process.stdin.write(json.dumps({"schema_version": 1, "action": "shutdown"}) + "\n")
                    process.stdin.flush()
                    process.stdin.close()
                process.wait(timeout=15)
            except (OSError, subprocess.TimeoutExpired):
                self._kill_tree(process)
            finally:
                if process.poll() is None:
                    self._kill_tree(process)
