from __future__ import annotations

import io
import json
import queue
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from voice_dubbing_runtime.errors import VoiceRuntimeError, XTTS_WORKER_CRASHED
from voice_dubbing_runtime.xtts_backend import ENGINE_MARKER, XttsV2Backend


class _NeverCancelled:
    @staticmethod
    def is_cancelled() -> bool:
        return False


class _FakeInput:
    def __init__(self) -> None:
        self.writes: list[str] = []
        self.closed = False

    def write(self, value: str) -> int:
        self.writes.append(value)
        return len(value)

    def flush(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _FakeProcess:
    def __init__(
        self,
        *,
        stdout: str = "",
        stderr: str = "",
        alive: bool = True,
        returncode: int = 0,
        hang_on_wait: bool = False,
    ) -> None:
        self.pid = 4321
        self.stdin_stream = _FakeInput()
        self.stdin: _FakeInput | None = self.stdin_stream
        self.stdout = io.StringIO(stdout)
        self.stderr = io.StringIO(stderr)
        self.returncode: int | None = None if alive else returncode
        self.hang_on_wait = hang_on_wait
        self.killed = False

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None and self.hang_on_wait:
            raise subprocess.TimeoutExpired(["fake-xtts-worker"], timeout)
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def communicate(self) -> tuple[str, str]:
        raise AssertionError("one-shot transport must consume output incrementally")


def _marker(payload: dict[str, object]) -> str:
    return ENGINE_MARKER + json.dumps(payload, separators=(",", ":")) + "\n"


class XttsOneShotLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.backend = XttsV2Backend(self.root)
        self.request = {"schema_version": 1, "job_id": "job-1", "action": "synthesize"}

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _kill(process: _FakeProcess) -> None:
        process.killed = True
        process.returncode = -9

    def test_success_is_returned_before_hung_child_finalization_is_forced(self) -> None:
        process = _FakeProcess(
            stdout=_marker(
                {
                    "schema_version": 1,
                    "type": "result",
                    "status": "success",
                    "job_id": "job-1",
                    "metrics": {"worker_pid": 4321, "model_load_count": 1},
                }
            ),
            hang_on_wait=True,
        )
        started = time.perf_counter()
        with patch("voice_dubbing_runtime.xtts_backend.subprocess.Popen", return_value=process), patch.object(
            XttsV2Backend, "_kill_tree", side_effect=self._kill
        ), patch("voice_dubbing_runtime.xtts_backend.WORKER_TIMEOUT_SECONDS", 0.05), patch(
            "voice_dubbing_runtime.xtts_backend.WORKER_FINALIZATION_TIMEOUT_SECONDS", 0.01
        ):
            metrics = self.backend._run_one_shot(
                self.request, lambda _name, _value: None, _NeverCancelled()
            )
        self.assertLess(time.perf_counter() - started, 1.0)
        self.assertEqual(4321, metrics["worker_pid"])
        self.assertTrue(process.killed)
        self.assertTrue(process.stdin_stream.closed)

    def test_persistent_success_keeps_worker_available(self) -> None:
        process = _FakeProcess(alive=True)
        self.backend._stdout_lines = []
        self.backend._stderr_lines = []
        self.backend._events = queue.Queue()
        self.backend._events.put(
            (
                "stdout",
                _marker(
                    {
                        "schema_version": 1,
                        "type": "result",
                        "status": "success",
                        "job_id": "job-1",
                        "metrics": {"worker_pid": 4321, "model_load_count": 1},
                    }
                ).strip(),
            )
        )
        with patch.object(self.backend, "_start_persistent", return_value=process):
            metrics = self.backend._run_persistent(
                self.request, lambda _name, _value: None, _NeverCancelled()
            )
        self.assertEqual(4321, metrics["worker_pid"])
        self.assertIsNone(process.poll())
        self.assertFalse(process.killed)
        self.assertIsNotNone(process.stdin)
        self.assertTrue(process.stdin_stream.writes[0].endswith("\n"))

    def test_unexpected_child_exit_returns_structured_error(self) -> None:
        process = _FakeProcess(stderr="worker crashed\n", alive=False, returncode=7)
        with patch("voice_dubbing_runtime.xtts_backend.subprocess.Popen", return_value=process), patch.object(
            self.backend, "_archive_failure", return_value=None
        ):
            with self.assertRaises(VoiceRuntimeError) as raised:
                self.backend._run_one_shot(
                    self.request, lambda _name, _value: None, _NeverCancelled()
                )
        self.assertEqual(XTTS_WORKER_CRASHED, raised.exception.code)
        self.assertEqual(7, raised.exception.details["exit_code"])

    def test_malformed_result_is_bounded_and_kills_live_child(self) -> None:
        process = _FakeProcess(stdout=ENGINE_MARKER + "not-json\n", hang_on_wait=True)
        started = time.perf_counter()
        with patch("voice_dubbing_runtime.xtts_backend.subprocess.Popen", return_value=process), patch.object(
            XttsV2Backend, "_kill_tree", side_effect=self._kill
        ), patch.object(self.backend, "_archive_failure", return_value=None), patch(
            "voice_dubbing_runtime.xtts_backend.WORKER_TIMEOUT_SECONDS", 0.05
        ):
            with self.assertRaises(VoiceRuntimeError) as raised:
                self.backend._run_one_shot(
                    self.request, lambda _name, _value: None, _NeverCancelled()
                )
        self.assertLess(time.perf_counter() - started, 1.0)
        self.assertEqual(XTTS_WORKER_CRASHED, raised.exception.code)
        self.assertTrue(process.killed)

    def test_close_uses_bounded_shutdown_and_kill_fallback(self) -> None:
        process = _FakeProcess(hang_on_wait=True)
        self.backend._process = process
        with patch.object(XttsV2Backend, "_kill_tree", side_effect=self._kill), patch(
            "voice_dubbing_runtime.xtts_backend.WORKER_FINALIZATION_TIMEOUT_SECONDS", 0.01
        ):
            self.backend.close()
        self.assertIn('"action": "shutdown"', process.stdin_stream.writes[0])
        self.assertTrue(process.stdin_stream.closed)
        self.assertTrue(process.killed)
        self.assertIsNone(self.backend._process)


if __name__ == "__main__":
    unittest.main()
