"""Asynchronous Qt client for the existing CLI and JSONL worker contracts."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, Signal


MARKER_PREFIX = "@@VOICE_DUB|"


class RuntimeClient(QObject):
    """Own CLI processes and one lazily-started generic runtime worker."""

    runtime_status = Signal(dict)
    capabilities_ready = Signal(dict)
    profiles_ready = Signal(list)
    profile_command_result = Signal(str, dict)
    job_started = Signal(dict)
    stage_changed = Signal(str, float)
    job_result = Signal(dict)
    job_error = Signal(dict)
    busy_changed = Signal(bool)
    log_line = Signal(str)
    cancelled = Signal()

    def __init__(
        self,
        repo_root: str | Path | None = None,
        python_executable: str | Path | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.repo_root = Path(repo_root or Path(__file__).resolve().parents[1]).resolve()
        self.python_executable = Path(
            python_executable
            or self.repo_root / ".venv-cpu" / "Scripts" / "python.exe"
        ).resolve()
        self._commands: dict[QProcess, dict[str, Any]] = {}
        self._worker: QProcess | None = None
        self._worker_stdout = ""
        self._worker_stderr = ""
        self._pending_job: dict[str, Any] | None = None
        self._pending_error: dict[str, Any] | None = None
        self._cancel_requested = False
        self._shutting_down = False

    @property
    def is_busy(self) -> bool:
        return self._pending_job is not None

    @property
    def worker_process_id(self) -> int | None:
        if self._worker is None or self._worker.state() == QProcess.NotRunning:
            return None
        value = int(self._worker.processId())
        return value or None

    def _environment(self) -> QProcessEnvironment:
        environment = QProcessEnvironment.systemEnvironment()
        # The GUI may run from its own Qt environment. Do not leak that
        # environment's import path into the pinned Python 3.11 runtime.
        environment.remove("PYTHONPATH")
        environment.remove("PYTHONHOME")
        environment.insert("PYTHONUTF8", "1")
        environment.insert("PYTHONIOENCODING", "utf-8")
        return environment

    def check_runtime(self) -> bool:
        available = self.python_executable.is_file()
        payload = {
            "available": available,
            "python_executable": str(self.python_executable),
            "repo_root": str(self.repo_root),
            "reason": None if available else "Không tìm thấy .venv-cpu\\Scripts\\python.exe.",
        }
        self.runtime_status.emit(payload)
        return available

    def refresh_all(self) -> None:
        if not self.check_runtime():
            self.capabilities_ready.emit(
                {"schema_version": 1, "runtime": {"available": False}, "engines": []}
            )
            self.profiles_ready.emit([])
            return
        self.refresh_capabilities()
        self.refresh_profiles()

    def refresh_capabilities(self) -> None:
        self._start_command("capabilities", ["capabilities", "--json"])

    def refresh_profiles(self) -> None:
        self._start_command("profiles_list", ["profiles", "list", "--json"])

    def get_profile(self, profile_id: str) -> None:
        self._start_command(
            "profile_get", ["profiles", "get", "--profile-id", profile_id, "--json"]
        )

    def delete_profile(self, profile_id: str) -> None:
        self._start_command(
            "profile_delete",
            ["profiles", "delete", "--profile-id", profile_id, "--json"],
            context={"profile_id": profile_id},
        )

    def _start_command(
        self, operation: str, arguments: list[str], *, context: dict[str, Any] | None = None
    ) -> None:
        if not self.check_runtime():
            self.job_error.emit(
                {
                    "code": "RUNTIME_NOT_FOUND",
                    "message": f"Runtime Python không tồn tại: {self.python_executable}",
                    "details": {},
                    "operation": operation,
                }
            )
            return
        process = QProcess(self)
        process.setProgram(str(self.python_executable))
        process.setArguments(["-u", "-m", "voice_dubbing_runtime", *arguments])
        process.setWorkingDirectory(str(self.repo_root))
        process.setProcessEnvironment(self._environment())
        process.setProcessChannelMode(QProcess.SeparateChannels)
        metadata = {
            "operation": operation,
            "stdout": "",
            "stderr": "",
            "context": context or {},
        }
        self._commands[process] = metadata
        process.readyReadStandardOutput.connect(
            lambda p=process: self._read_command_channel(p, False)
        )
        process.readyReadStandardError.connect(
            lambda p=process: self._read_command_channel(p, True)
        )
        process.finished.connect(
            lambda exit_code, _status, p=process: self._command_finished(p, exit_code)
        )
        process.errorOccurred.connect(
            lambda _error, p=process: self._command_process_error(p)
        )
        self.log_line.emit(f"CLI {operation}: {subprocess.list2cmdline(process.arguments())}")
        process.start()

    def _read_command_channel(self, process: QProcess, stderr: bool) -> None:
        metadata = self._commands.get(process)
        if metadata is None:
            return
        raw = process.readAllStandardError() if stderr else process.readAllStandardOutput()
        text = bytes(raw).decode("utf-8", errors="replace")
        key = "stderr" if stderr else "stdout"
        metadata[key] += text

    def _command_process_error(self, process: QProcess) -> None:
        metadata = self._commands.get(process)
        if metadata is None or process.state() != QProcess.NotRunning:
            return
        self._emit_command_error(metadata, f"Không thể chạy runtime: {process.errorString()}")

    def _command_finished(self, process: QProcess, exit_code: int) -> None:
        self._read_command_channel(process, False)
        self._read_command_channel(process, True)
        metadata = self._commands.pop(process, None)
        process.deleteLater()
        if metadata is None:
            return
        stdout = str(metadata["stdout"]).strip()
        stderr = str(metadata["stderr"]).strip()
        if stderr:
            self.log_line.emit(stderr[-4000:])
        try:
            payload = json.loads(stdout)
            if not isinstance(payload, dict):
                raise ValueError("CLI response is not an object")
        except (json.JSONDecodeError, ValueError) as exc:
            self._emit_command_error(
                metadata,
                f"Runtime trả JSON không hợp lệ ({exit_code}): {exc}",
                {"stdout_tail": stdout[-4000:], "stderr_tail": stderr[-4000:]},
            )
            return
        if exit_code != 0 or payload.get("status") == "failed":
            self._emit_command_error(
                metadata,
                str(payload.get("message") or f"CLI thoát với mã {exit_code}."),
                payload,
            )
            return
        operation = str(metadata["operation"])
        if operation == "capabilities":
            self.capabilities_ready.emit(payload)
        elif operation == "profiles_list":
            profiles = payload.get("profiles")
            self.profiles_ready.emit(profiles if isinstance(profiles, list) else [])
        else:
            enriched = {**payload, **metadata["context"]}
            self.profile_command_result.emit(operation, enriched)

    def _emit_command_error(
        self,
        metadata: dict[str, Any],
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        payload = details or {}
        self.job_error.emit(
            {
                "code": str(payload.get("code") or payload.get("error_code") or "RUNTIME_COMMAND_FAILED"),
                "message": message,
                "details": payload.get("details") if isinstance(payload.get("details"), dict) else payload,
                "operation": metadata.get("operation"),
                **metadata.get("context", {}),
            }
        )

    @staticmethod
    def parse_marker(line: str) -> dict[str, Any] | None:
        if not line.startswith(MARKER_PREFIX):
            return None
        try:
            value = json.loads(line[len(MARKER_PREFIX) :])
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None

    def submit_job(self, job: dict[str, Any]) -> bool:
        if self.is_busy:
            self.job_error.emit(
                {
                    "code": "DUPLICATE_SUBMIT_BLOCKED",
                    "message": "Đang có một tác vụ chạy. Hãy chờ hoặc bấm Hủy.",
                    "details": {},
                }
            )
            return False
        if not self.check_runtime():
            self.job_error.emit(
                {
                    "code": "RUNTIME_NOT_FOUND",
                    "message": f"Runtime Python không tồn tại: {self.python_executable}",
                    "details": {},
                }
            )
            return False
        if not isinstance(job, dict) or not job.get("job_id"):
            self.job_error.emit(
                {"code": "INVALID_REQUEST", "message": "Job thiếu job_id.", "details": {}}
            )
            return False
        self._pending_job = dict(job)
        self._pending_error = None
        self._cancel_requested = False
        self.busy_changed.emit(True)
        self.job_started.emit(dict(job))
        worker = self._ensure_worker()
        if worker.state() == QProcess.Running:
            self._write_pending_job()
        return True

    def _ensure_worker(self) -> QProcess:
        if self._worker is not None and self._worker.state() != QProcess.NotRunning:
            return self._worker
        worker = QProcess(self)
        worker.setProgram(str(self.python_executable))
        worker.setArguments(
            ["-u", "-m", "voice_dubbing_runtime", "worker", "--jobs-jsonl", "-"]
        )
        worker.setWorkingDirectory(str(self.repo_root))
        worker.setProcessEnvironment(self._environment())
        worker.setProcessChannelMode(QProcess.SeparateChannels)
        worker.readyReadStandardOutput.connect(self._read_worker_stdout)
        worker.readyReadStandardError.connect(self._read_worker_stderr)
        worker.started.connect(self._write_pending_job)
        worker.finished.connect(self._worker_finished)
        worker.errorOccurred.connect(self._worker_error)
        self._worker = worker
        self._worker_stdout = ""
        self._worker_stderr = ""
        self.log_line.emit("Khởi động generic JSONL worker.")
        worker.start()
        return worker

    def _write_pending_job(self) -> None:
        if self._worker is None or self._worker.state() != QProcess.Running:
            return
        if self._pending_job is None or self._pending_job.get("_sent_to_worker"):
            return
        wire_job = {key: value for key, value in self._pending_job.items() if not key.startswith("_")}
        encoded = (json.dumps(wire_job, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
        self._worker.write(encoded)
        self._pending_job["_sent_to_worker"] = True
        self.log_line.emit(
            f"Gửi {wire_job.get('action')} job {wire_job.get('job_id')} tới worker."
        )

    def _read_worker_stdout(self) -> None:
        if self._worker is None:
            return
        text = bytes(self._worker.readAllStandardOutput()).decode("utf-8", errors="replace")
        self._worker_stdout += text
        while "\n" in self._worker_stdout:
            line, self._worker_stdout = self._worker_stdout.split("\n", 1)
            self._handle_worker_line(line.rstrip("\r"))

    def _read_worker_stderr(self) -> None:
        if self._worker is None:
            return
        text = bytes(self._worker.readAllStandardError()).decode("utf-8", errors="replace")
        self._worker_stderr = (self._worker_stderr + text)[-12000:]
        for line in text.splitlines():
            if line.strip():
                self.log_line.emit(line[-2000:])

    def _handle_worker_line(self, line: str) -> None:
        marker = self.parse_marker(line)
        if marker is None:
            if line.strip():
                self.log_line.emit(line[-2000:])
            return
        marker_type = marker.get("type")
        if marker_type == "stage":
            name = str(marker.get("name", "unknown"))
            try:
                progress = float(marker.get("progress", 0.0))
            except (TypeError, ValueError):
                progress = 0.0
            self.stage_changed.emit(name, max(0.0, min(1.0, progress)))
            self.log_line.emit(f"{name}: {progress:.0%}")
            return
        if marker_type == "error":
            self._pending_error = {
                "code": str(marker.get("code") or "RUNTIME_JOB_FAILED"),
                "message": str(marker.get("message") or "Runtime job thất bại."),
                "details": marker.get("details") if isinstance(marker.get("details"), dict) else {},
            }
            self.log_line.emit(
                f"{self._pending_error['code']}: {self._pending_error['message']}"
            )
            return
        if marker_type != "result":
            self.log_line.emit(json.dumps(marker, ensure_ascii=False))
            return
        pending = self._pending_job
        if pending is None:
            self.log_line.emit("Bỏ qua terminal marker không thuộc job đang chờ.")
            return
        job_id = str(pending.get("job_id"))
        marker_job_id = marker.get("job_id")
        if marker_job_id not in (None, "", job_id):
            self.log_line.emit(f"Bỏ qua result marker cho job khác: {marker_job_id}")
            return
        if marker.get("status") == "success":
            payload = self._hydrate_result(marker)
            payload.setdefault("job_id", job_id)
            payload["_submitted_action"] = pending.get("action")
            self._finish_active_job()
            self.job_result.emit(payload)
            return
        error = self._pending_error or {
            "code": str(marker.get("error_code") or "RUNTIME_JOB_FAILED"),
            "message": "Runtime job thất bại.",
            "details": {},
        }
        error["job_id"] = job_id
        error["action"] = pending.get("action")
        self._finish_active_job()
        self.job_error.emit(error)

    def _hydrate_result(self, marker: dict[str, Any]) -> dict[str, Any]:
        result_path = marker.get("result_path")
        if isinstance(result_path, str) and result_path:
            try:
                with Path(result_path).open("r", encoding="utf-8-sig") as handle:
                    payload = json.load(handle)
                if isinstance(payload, dict):
                    payload["result_path"] = result_path
                    return payload
            except (OSError, json.JSONDecodeError) as exc:
                self.log_line.emit(f"Không đọc được result.json: {exc}")
        return dict(marker)

    def _finish_active_job(self) -> None:
        self._pending_job = None
        self._pending_error = None
        self._cancel_requested = False
        self.busy_changed.emit(False)

    def cancel_active_job(self) -> None:
        if self._pending_job is None:
            return
        self._cancel_requested = True
        self.log_line.emit("Đang hủy tác vụ và dừng process tree...")
        worker = self._worker
        if worker is None or worker.state() == QProcess.NotRunning:
            self._finish_active_job()
            self.cancelled.emit()
            return
        if os.name == "nt":
            # QProcess.terminate() uses TerminateProcess on Windows. If the
            # parent exits first, an XTTS/Demucs child can become orphaned and
            # can no longer be reached by taskkill /T through that parent PID.
            # Kill the tree while the parent identity is still live.
            self._kill_process_tree(int(worker.processId()))
            worker.kill()
            return
        worker.terminate()
        QTimer.singleShot(1500, self._force_cancel_worker)

    def _force_cancel_worker(self) -> None:
        worker = self._worker
        if worker is not None and worker.state() != QProcess.NotRunning:
            self._kill_process_tree(int(worker.processId()))
            worker.kill()

    @staticmethod
    def _kill_process_tree(pid: int) -> None:
        if pid <= 0:
            return
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=15,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
        except (OSError, subprocess.TimeoutExpired):
            pass

    def _worker_error(self, _error: QProcess.ProcessError) -> None:
        if self._shutting_down or self._cancel_requested:
            return
        if self._pending_job is not None and self._worker is not None:
            payload = {
                "code": "WORKER_CRASHED",
                "message": f"Worker không thể tiếp tục: {self._worker.errorString()}",
                "details": {"stderr_tail": self._worker_stderr[-4000:]},
                "action": self._pending_job.get("action"),
                "job_id": self._pending_job.get("job_id"),
            }
            self._finish_active_job()
            self.job_error.emit(payload)

    def _worker_finished(self, exit_code: int, _status: QProcess.ExitStatus) -> None:
        worker = self._worker
        if worker is not None:
            self._read_worker_stdout()
            self._read_worker_stderr()
        self._worker = None
        if self._cancel_requested:
            self._finish_active_job()
            self.cancelled.emit()
            return
        if self._shutting_down:
            return
        if self._pending_job is not None:
            payload = {
                "code": "WORKER_CRASHED",
                "message": f"Worker thoát ngoài dự kiến với mã {exit_code}.",
                "details": {"stderr_tail": self._worker_stderr[-4000:]},
                "action": self._pending_job.get("action"),
                "job_id": self._pending_job.get("job_id"),
            }
            self._finish_active_job()
            self.job_error.emit(payload)

    def shutdown(self, timeout_ms: int = 5000) -> None:
        self._shutting_down = True
        for process in list(self._commands):
            if process.state() != QProcess.NotRunning:
                process.kill()
        worker = self._worker
        if worker is None or worker.state() == QProcess.NotRunning:
            return
        pid = int(worker.processId())
        worker.closeWriteChannel()
        if not worker.waitForFinished(max(0, int(timeout_ms))):
            self._kill_process_tree(pid)
            worker.kill()
            worker.waitForFinished(2000)


__all__ = ["MARKER_PREFIX", "RuntimeClient"]
