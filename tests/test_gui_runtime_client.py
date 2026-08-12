from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

from PySide6.QtCore import QProcess

from voice_dubbing_app.runtime_client import MARKER_PREFIX, RuntimeClient

from .gui_helpers import application


class RuntimeClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = application()

    def test_parse_stage_success_and_error_markers(self) -> None:
        for payload in (
            {"type": "stage", "name": "load_model", "progress": 0.3},
            {"type": "result", "status": "success", "job_id": "x"},
            {"type": "error", "code": "ENGINE_UNAVAILABLE", "message": "missing"},
        ):
            line = MARKER_PREFIX + json.dumps(payload)
            self.assertEqual(payload, RuntimeClient.parse_marker(line))

    def test_ignore_non_marker_and_malformed_marker(self) -> None:
        self.assertIsNone(RuntimeClient.parse_marker("model noise"))
        self.assertIsNone(RuntimeClient.parse_marker(MARKER_PREFIX + "{"))

    def test_duplicate_submit_is_blocked_client_side(self) -> None:
        client = RuntimeClient(python_executable=sys.executable)
        fake_worker = Mock()
        fake_worker.state.return_value = QProcess.Running
        errors: list[dict] = []
        client.job_error.connect(errors.append)
        job = {
            "schema_version": 1,
            "job_id": str(uuid.uuid4()),
            "action": "synthesize",
        }
        with patch.object(client, "_ensure_worker", return_value=fake_worker):
            self.assertTrue(client.submit_job(job))
            self.assertFalse(client.submit_job({**job, "job_id": str(uuid.uuid4())}))
        self.assertEqual("DUPLICATE_SUBMIT_BLOCKED", errors[-1]["code"])

    def test_cancel_without_started_process_resets_client(self) -> None:
        client = RuntimeClient(python_executable=sys.executable)
        client._pending_job = {"job_id": str(uuid.uuid4()), "action": "synthesize"}
        seen: list[bool] = []
        client.cancelled.connect(lambda: seen.append(True))
        client.cancel_active_job()
        self.assertFalse(client.is_busy)
        self.assertEqual([True], seen)

    def test_windows_cancel_targets_the_live_process_tree(self) -> None:
        client = RuntimeClient(python_executable=sys.executable)
        worker = Mock()
        worker.state.return_value = QProcess.Running
        worker.processId.return_value = 4321
        client._worker = worker
        client._pending_job = {"job_id": str(uuid.uuid4()), "action": "synthesize"}
        with patch.object(client, "_kill_process_tree") as kill_tree:
            client.cancel_active_job()
        kill_tree.assert_called_once_with(4321)
        worker.kill.assert_called_once()

    def test_worker_crash_emits_stable_error_and_resets_busy(self) -> None:
        client = RuntimeClient(python_executable=sys.executable)
        client._pending_job = {"job_id": str(uuid.uuid4()), "action": "synthesize"}
        errors: list[dict] = []
        client.job_error.connect(errors.append)
        client._worker_finished(7, QProcess.CrashExit)
        self.assertEqual("WORKER_CRASHED", errors[-1]["code"])
        self.assertFalse(client.is_busy)

    def test_runtime_missing_is_reported_without_crash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "không tồn tại" / "python.exe"
            client = RuntimeClient(python_executable=missing)
            statuses: list[dict] = []
            client.runtime_status.connect(statuses.append)
            self.assertFalse(client.check_runtime())
            self.assertFalse(statuses[-1]["available"])
            self.assertIn("python.exe", statuses[-1]["python_executable"])

    def test_unicode_runtime_path_is_supported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "Giọng Việt" / "python.exe"
            executable.parent.mkdir()
            executable.write_bytes(b"test")
            client = RuntimeClient(python_executable=executable)
            self.assertTrue(client.check_runtime())

    def test_hydrates_full_result_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "kết quả.json"
            expected = {"status": "success", "selection": {"start_seconds": 1.0}}
            path.write_text(json.dumps(expected), encoding="utf-8")
            client = RuntimeClient(python_executable=sys.executable)
            actual = client._hydrate_result({"result_path": str(path), "status": "success"})
            self.assertEqual(expected["selection"], actual["selection"])
            self.assertEqual(str(path), actual["result_path"])

    def test_package_root_imports_no_ml_stack(self) -> None:
        command = [
            sys.executable,
            "-c",
            (
                "import sys, voice_dubbing_app; "
                "blocked=('torch','torchaudio','transformers','TTS','demucs'); "
                "print([name for name in blocked if name in sys.modules])"
            ),
        ]
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=30,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("[]", completed.stdout.strip())


if __name__ == "__main__":
    unittest.main()
