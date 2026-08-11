from __future__ import annotations

import io
import json
import shutil
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from voice_dubbing_runtime.profiles import VoiceProfileManager
from voice_dubbing_runtime.worker import (
    CancellationToken,
    MarkerEmitter,
    VoiceWorker,
    validate_generated_wav,
)

from .helpers import write_pcm_wav


class FakeRegistry:
    def __init__(self, *, preferred_available: bool = True) -> None:
        self.requests: list[tuple[str, str, str]] = []
        self.preferred_available = preferred_available

    def select(self, engine: str, language: str, device: str = "cpu") -> SimpleNamespace:
        self.requests.append((engine, language, device))
        if engine == "missing_preferred":
            from voice_dubbing_runtime.errors import ENGINE_UNAVAILABLE, VoiceRuntimeError

            raise VoiceRuntimeError(ENGINE_UNAVAILABLE, "preferred unavailable")
        return SimpleNamespace(id="vixtts_vi")

    def instantiate_backend(self, _capability: object) -> object:
        return FakeBackend()

    def engines(self) -> tuple[SimpleNamespace, ...]:
        return (
            SimpleNamespace(
                id="vixtts_vi",
                available=True,
                languages=("vi",),
                devices=("cpu",),
            ),
        )


class FakeBackend:
    closed = 0

    def synthesize(self, *, job, profile, references, output_path, progress, cancel_token):
        cancel_token.raise_if_cancelled()
        progress("load_model", 0.30)
        progress("synthesize", 0.70)
        write_pcm_wav(output_path, seconds=1.0, amplitude=0.15)
        return {"fake": True, "reference_count": len(references)}

    def close(self) -> None:
        type(self).closed += 1


class WorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.profiles = VoiceProfileManager(self.base / "profiles")
        reference = write_pcm_wav(self.base / "ref.wav")
        self.reference = reference
        self.profiles.create(
            profile_id="sample_voice",
            display_name="Vietnamese Test Voice",
            profile_type="cloned",
            source_type="audio",
            default_language="vi",
            engine_preference="vixtts_vi",
            reference_files=[reference],
            consent={"confirmed": True},
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def make_job(self, run_name: str = "run") -> dict:
        job_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"voice-worker-test:{run_name}"))
        return {
            "schema_version": 1,
            "job_id": job_id,
            "action": "synthesize",
            "profile_id": "sample_voice",
            "text": "Xin chào thế giới",
            "language": "vi",
            "engine": "auto",
            "device": "cpu",
            "speed": 1.0,
            "seed": 42,
            "keep_model_warm": False,
            "output_dir": str(self.base / job_id),
            "ffmpeg_path": str(self.base / "not-used-in-patched-decode.exe"),
        }

    def make_worker(self, registry: FakeRegistry | None = None, token: CancellationToken | None = None):
        stream = io.StringIO()
        registry = registry or FakeRegistry()
        worker = VoiceWorker(
            profile_manager=self.profiles,
            registry=registry,
            emitter=MarkerEmitter(stream),
            cancel_token=token,
            backend_factory=lambda _capability: FakeBackend(),
            root=self.base,
            runs_root=self.base,
        )
        return worker, stream, registry

    def test_synthesize_worker_markers_and_output_bundle(self) -> None:
        worker, stream, _registry = self.make_worker()
        with patch("voice_dubbing_runtime.worker.resolve_ffmpeg", return_value=self.base / "fake.exe"), patch(
            "voice_dubbing_runtime.worker._ffmpeg_decode", return_value=None
        ):
            job = self.make_job()
            code, result = worker.execute(job)
        self.assertEqual(0, code)
        output = Path(result["output_audio"])
        self.assertTrue(output.is_file())
        self.assertEqual("Pass", result["output_validation"]["ffmpeg_decode"])
        self.assertIn("@@VOICE_DUB|", stream.getvalue())
        self.assertIn('"name":"load_profile"', stream.getvalue())
        self.assertIn('"peak_ram_gib":', stream.getvalue())
        for relative in ("generated.wav", "job.json", "result.json", "run.log"):
            self.assertTrue((Path(job["output_dir"]) / relative).is_file(), relative)

    def test_marker_wire_handles_vietnamese_on_cp1252_console(self) -> None:
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="cp1252", newline="\n")
        emitter = MarkerEmitter(stream)
        log_path = self.base / "unicode-run.log"
        emitter.set_log_path(log_path)
        message = (
            "Đoạn tham chiếu vẫn còn nhạc hoặc âm thanh nền. "
            "Hãy chọn đoạn khác hoặc chạy lại tách giọng."
        )

        emitter.emit({"type": "error", "message": message})
        stream.flush()
        wire = raw.getvalue().decode("ascii").strip()
        payload = json.loads(wire.split("@@VOICE_DUB|", 1)[1])

        self.assertEqual(message, payload["message"])
        self.assertIn("\\u0110", wire)
        self.assertIn(message, log_path.read_text(encoding="utf-8"))

    def test_legacy_create_cloned_profile_requires_prepare_commit_flow(self) -> None:
        source = write_pcm_wav(self.base / "nguồn Unicode" / "Giọng nói mẫu.wav", seconds=14.0)
        job_id = str(uuid.uuid4())
        job = {
            "schema_version": 1,
            "job_id": job_id,
            "action": "create_profile",
            "input_path": str(source),
            "source_type": "audio",
            "profile_id": "unicode_audio_profile",
            "display_name": "Hồ sơ giọng Unicode",
            "profile_type": "cloned",
            "default_language": "vi",
            "engine_preference": "auto",
            "selection": {"mode": "auto"},
            "consent": {"confirmed": True},
            "output_dir": str(self.base / job_id),
        }
        worker, stream, _registry = self.make_worker()
        code, result = worker.execute(job)
        self.assertEqual(2, code)
        self.assertEqual("REFERENCE_APPROVAL_REQUIRED", result["error_code"])
        self.assertFalse((self.profiles.root / "unicode_audio_profile").exists())
        self.assertNotIn('"name":"normalize_audio"', stream.getvalue())

    def test_create_profile_rejects_consent_before_media_processing(self) -> None:
        job_id = str(uuid.uuid4())
        job = {
            "schema_version": 1,
            "job_id": job_id,
            "action": "create_profile",
            "input_path": str(self.reference),
            "source_type": "audio",
            "display_name": "Denied",
            "profile_type": "cloned",
            "default_language": "vi",
            "engine_preference": "auto",
            "selection": {"mode": "auto"},
            "consent": {"confirmed": "false"},
            "output_dir": str(self.base / job_id),
        }
        worker, stream, _registry = self.make_worker()
        code, result = worker.execute(job)
        self.assertEqual(2, code)
        self.assertEqual("CONSENT_REQUIRED", result["error_code"])
        self.assertNotIn('"name":"normalize_audio"', stream.getvalue())
        self.assertFalse((Path(job["output_dir"]) / "source_normalized.wav").exists())

    def test_profile_preference_is_attempted_before_auto(self) -> None:
        self.profiles.update("sample_voice", engine_preference="missing_preferred")
        registry = FakeRegistry()
        worker, _stream, registry = self.make_worker(registry)
        with patch("voice_dubbing_runtime.worker.resolve_ffmpeg", return_value=self.base / "fake.exe"), patch(
            "voice_dubbing_runtime.worker._ffmpeg_decode", return_value=None
        ):
            code, _result = worker.execute(self.make_job("fallback"))
        self.assertEqual(0, code)
        self.assertEqual("missing_preferred", registry.requests[0][0])
        self.assertEqual("auto", registry.requests[1][0])

    def test_auto_language_resolves_from_runtime_capability(self) -> None:
        self.profiles.update(
            "sample_voice",
            default_language="auto",
            engine_preference="auto",
        )
        job = self.make_job("auto-language")
        job["language"] = "auto"
        registry = FakeRegistry()
        worker, _stream, registry = self.make_worker(registry)
        with patch("voice_dubbing_runtime.worker.resolve_ffmpeg", return_value=self.base / "fake.exe"), patch(
            "voice_dubbing_runtime.worker._ffmpeg_decode", return_value=None
        ):
            code, result = worker.execute(job)
        self.assertEqual(0, code)
        self.assertEqual("vi", result["language"])
        self.assertEqual(("auto", "vi", "cpu"), registry.requests[0])

    def test_cancel_before_first_stage_writes_cancelled_result(self) -> None:
        token = CancellationToken()
        token.cancel()
        worker, stream, _registry = self.make_worker(token=token)
        job = self.make_job("cancelled")
        code, result = worker.execute(job)
        self.assertEqual(130, code)
        self.assertEqual("CANCELLED", result["error_code"])
        self.assertIn('"code":"CANCELLED"', stream.getvalue())
        self.assertTrue((Path(job["output_dir"]) / "result.json").is_file())

    def test_existing_result_refuses_overwrite(self) -> None:
        job = self.make_job("collision")
        run = Path(job["output_dir"])
        run.mkdir()
        sentinel = run / "result.json"
        sentinel.write_text("sentinel", encoding="utf-8")
        worker, _stream, _registry = self.make_worker()
        code, result = worker.execute(job)
        self.assertEqual(2, code)
        self.assertEqual("sentinel", sentinel.read_text(encoding="utf-8"))
        self.assertEqual("INVALID_REQUEST", result["error_code"])

    def test_output_directory_outside_configured_runs_root_is_rejected(self) -> None:
        job = self.make_job("escape")
        job["output_dir"] = str(self.base.parent / str(job["job_id"]))
        worker, _stream, _registry = self.make_worker()
        code, result = worker.execute(job)
        self.assertEqual(2, code)
        self.assertEqual("INVALID_REQUEST", result["error_code"])
        self.assertFalse(Path(job["output_dir"]).exists())

    def test_output_validator_rejects_clipping(self) -> None:
        clipped = write_pcm_wav(self.base / "clipped.wav", seconds=1.0, amplitude=1.0)
        with patch("voice_dubbing_runtime.worker._ffmpeg_decode", return_value=None):
            with self.assertRaises(Exception) as context:
                validate_generated_wav(clipped, self.base / "fake.exe")
        self.assertIn("clipping", str(context.exception).lower())


if __name__ == "__main__":
    unittest.main()
