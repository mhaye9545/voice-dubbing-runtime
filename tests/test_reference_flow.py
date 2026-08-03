from __future__ import annotations

import io
import json
import shutil
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from voice_dubbing_runtime.errors import (
    BACKGROUND_AUDIO_DETECTED_PENDING_LISTENING,
    PROFILE_ID_CONFLICT,
    REFERENCE_APPROVAL_REQUIRED,
    VoiceRuntimeError,
)
from voice_dubbing_runtime.io_utils import file_record, sha256_file
from voice_dubbing_runtime.profiles import VoiceProfileManager
from voice_dubbing_runtime.worker import MarkerEmitter, VoiceWorker

from .helpers import write_pcm_wav


PASS_VALIDATION = {
    "schema_version": 1,
    "validation_revision": "voice_only_gate_v1",
    "status": "PASS",
}


class FakeSeparator:
    def separate_vocals(self, *, input_path, output_path, work_dir, progress, cancel_token):
        cancel_token.raise_if_cancelled()
        progress("source_separation_load_model", 0.30)
        shutil.copy2(input_path, output_path)
        progress("source_separation_complete", 0.70)
        return {"package": "demucs==4.1.0", "model": "htdemucs"}


class ReferenceFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.profiles = VoiceProfileManager(self.base / "profiles")
        self.original = write_pcm_wav(self.base / "nguồn cũ.wav", seconds=10.0, frequency=180)
        self.profile = self.profiles.create(
            profile_id="english_anchor",
            display_name="English Anchor",
            profile_type="cloned",
            source_type="audio",
            default_language="en",
            engine_preference="xtts_v2_multilingual",
            reference_files=[self.original],
            consent={"confirmed": True},
            quality={"selection_start_seconds": 194.35, "selection_end_seconds": 204.35},
        )
        self.stream = io.StringIO()
        self.worker = VoiceWorker(
            profile_manager=self.profiles,
            emitter=MarkerEmitter(self.stream),
            separator_factory=lambda _root: FakeSeparator(),
            root=self.base,
            runs_root=self.base,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _media_patches(self):
        def normalize(_source, _kind, output, _ffmpeg, _token):
            shutil.copy2(self.original, output)
            return {
                "source": str(self.original),
                "duration_seconds": 10.0,
                "sample_rate": 24000,
            }

        def candidate(_source, _kind, output, start, end, _ffmpeg, _token):
            shutil.copy2(self.original, output)
            return {"start_seconds": start, "end_seconds": end, "file": file_record(output)}

        def voice(source, output, _ffmpeg, _token):
            shutil.copy2(source, output)
            return {"voice_only": file_record(output), "duration_seconds": 10.0}

        return (
            patch("voice_dubbing_runtime.worker.resolve_ffmpeg", return_value=self.base / "ffmpeg.exe"),
            patch("voice_dubbing_runtime.worker.normalize_source", side_effect=normalize),
            patch("voice_dubbing_runtime.worker.prepare_separation_candidate", side_effect=candidate),
            patch("voice_dubbing_runtime.worker.normalize_voice_only", side_effect=voice),
            patch("voice_dubbing_runtime.worker.validate_voice_only_reference", return_value=PASS_VALIDATION),
        )

    def test_prepare_marker_and_commit_install_exact_three_file_contract(self) -> None:
        original_hash = sha256_file(
            self.profiles.root / "english_anchor" / self.profile["reference_files"][0]["path"]
        )
        prepare_id = str(uuid.uuid4())
        prepare = {
            "schema_version": 1,
            "job_id": prepare_id,
            "action": "prepare_profile_reference",
            "profile_id": "english_anchor",
            "update_existing": True,
            "reported_background_audio": True,
            "input_path": str(self.original),
            "source_type": "audio",
            "selection": {"mode": "manual", "start_seconds": 194.35, "end_seconds": 204.35},
            "output_dir": str(self.base / prepare_id),
        }
        patches = self._media_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            code, prepared = self.worker.execute(prepare)
        self.assertEqual(0, code, prepared)
        self.assertEqual(prepare_id, prepared["preparation_id"])
        self.assertTrue(prepared["ready_for_commit"])
        self.assertEqual(original_hash, prepared["reference_artifacts"]["source_mix"]["sha256"])
        self.assertTrue(prepared["reference_artifacts"]["source_mix"]["has_background"])
        self.assertEqual("PASS", prepared["reference_artifacts"]["voice_only"]["validation_status"])
        self.assertFalse(prepared["reference_artifacts"]["primary"]["committed"])
        result_markers = []
        for line in self.stream.getvalue().splitlines():
            if line.startswith("@@VOICE_DUB|"):
                payload = json.loads(line.split("|", 1)[1])
                if payload.get("type") == "result":
                    result_markers.append(payload)
        self.assertEqual(prepare_id, result_markers[-1]["preparation_id"])
        self.assertIn("reference_artifacts", result_markers[-1])
        with self.assertRaises(VoiceRuntimeError) as blocked:
            self.profiles.resolve_references("english_anchor")
        self.assertEqual(REFERENCE_APPROVAL_REQUIRED, blocked.exception.code)

        missing_speaker_id = str(uuid.uuid4())
        missing_speaker = {
            "schema_version": 1,
            "job_id": missing_speaker_id,
            "action": "commit_profile_reference",
            "profile_id": "english_anchor",
            "preparation_id": prepare_id,
            "user_listening_confirmed": True,
            "use_voice_only": True,
            "reference_artifacts": prepared["reference_artifacts"],
            "output_dir": str(self.base / missing_speaker_id),
        }
        missing_code, missing_result = self.worker.execute(missing_speaker)
        self.assertEqual(2, missing_code)
        self.assertEqual(REFERENCE_APPROVAL_REQUIRED, missing_result["error_code"])

        commit_id = str(uuid.uuid4())
        commit = {
            "schema_version": 1,
            "job_id": commit_id,
            "action": "commit_profile_reference",
            "profile_id": "english_anchor",
            "preparation_id": prepare_id,
            "user_listening_confirmed": True,
            "single_speaker_confirmed": True,
            "use_voice_only": True,
            "reference_artifacts": prepared["reference_artifacts"],
            "output_dir": str(self.base / commit_id),
        }
        with patch("voice_dubbing_runtime.worker.resolve_ffmpeg", return_value=self.base / "ffmpeg.exe"), patch(
            "voice_dubbing_runtime.worker.validate_voice_only_reference", return_value=PASS_VALIDATION
        ):
            code, committed = self.worker.execute(commit)
        self.assertEqual(0, code)
        self.assertEqual("READY", committed["profile_status"])
        directory = self.profiles.root / "english_anchor"
        for name in ("ref_source_mix.wav", "ref_voice_only.wav", "ref_primary.wav"):
            self.assertTrue((directory / "references" / name).is_file())
        loaded = self.profiles.load("english_anchor")
        self.assertEqual("references/ref_primary.wav", loaded["reference_files"][0]["path"])
        self.assertEqual(
            sha256_file(directory / "references" / "ref_voice_only.wav"),
            sha256_file(directory / "references" / "ref_primary.wav"),
        )
        quality = json.loads((directory / "quality.json").read_text(encoding="utf-8"))
        provenance = quality["voice_only_reference_provenance"]
        self.assertEqual(prepare_id, provenance["preparation_id"])
        self.assertEqual("manual", provenance["selection"]["mode"])
        self.assertEqual(194.35, provenance["selection"]["start_seconds"])
        self.assertTrue(provenance["reported_background_audio"])
        self.assertTrue(provenance["original_profile_source_preserved"])
        self.assertTrue(Path(committed["history_path"]).is_dir())

    def test_review_candidate_preserves_both_previews_and_cannot_commit(self) -> None:
        original_profile_hash = sha256_file(
            self.profiles.root
            / "english_anchor"
            / self.profile["reference_files"][0]["path"]
        )
        job_id = str(uuid.uuid4())
        job = {
            "schema_version": 1,
            "job_id": job_id,
            "action": "prepare_profile_reference",
            "profile_id": "english_anchor",
            "update_existing": True,
            "candidate_review_only": True,
            "reported_background_audio": True,
            "input_path": str(self.original),
            "source_type": "audio",
            "selection": {"mode": "manual", "start_seconds": 0.0, "end_seconds": 10.0},
            "target_speaker_window": {"start_seconds": 0.0, "end_seconds": 10.0},
            "output_dir": str(self.base / job_id),
        }
        background_validation = {
            **PASS_VALIDATION,
            "status": "BACKGROUND_AUDIO_DETECTED",
            "background_gate": {"failures": ["noise_floor_too_high"]},
        }
        patches = self._media_patches()
        with patches[0], patches[1], patches[2], patches[3], patch(
            "voice_dubbing_runtime.worker.validate_voice_only_reference",
            return_value=background_validation,
        ):
            code, prepared = self.worker.execute(job)
        self.assertEqual(0, code, prepared)
        self.assertEqual("TECHNICAL_PASS_PENDING_LISTENING", prepared["profile_status"])
        self.assertEqual(
            BACKGROUND_AUDIO_DETECTED_PENDING_LISTENING,
            prepared["candidate_status"],
        )
        self.assertFalse(prepared["ready_for_commit"])
        self.assertNotIn("primary", prepared["reference_artifacts"])
        self.assertFalse((self.base / job_id / "ref_primary.wav").exists())
        self.assertTrue((self.base / job_id / "ref_source_mix.wav").is_file())
        self.assertTrue((self.base / job_id / "ref_voice_only.wav").is_file())
        self.assertEqual(
            {"start_seconds": 0.0, "end_seconds": 10.0},
            prepared["target_speaker_window"],
        )
        active = self.profiles.load("english_anchor")
        self.assertEqual(
            original_profile_hash,
            sha256_file(
                self.profiles.root
                / "english_anchor"
                / active["reference_files"][0]["path"]
            ),
        )

    def test_existing_create_collision_fails_before_any_heavy_media_work(self) -> None:
        job_id = str(uuid.uuid4())
        job = {
            "schema_version": 1,
            "job_id": job_id,
            "action": "prepare_profile_reference",
            "profile_id": "english_anchor",
            "display_name": "English Anchor",
            "profile_type": "cloned",
            "input_path": str(self.original),
            "source_type": "audio",
            "default_language": "en",
            "selection": {"mode": "manual", "start_seconds": 0.0, "end_seconds": 10.0},
            "consent": {"confirmed": True},
            "output_dir": str(self.base / job_id),
        }
        with patch("voice_dubbing_runtime.worker.normalize_source") as normalize:
            code, failed = self.worker.execute(job)
        self.assertEqual(2, code)
        self.assertEqual(PROFILE_ID_CONFLICT, failed["error_code"])
        self.assertEqual(
            "Profile ID english_anchor đã tồn tại. Hãy đổi tên hoặc chọn Cập nhật reference.",
            failed["message"],
        )
        normalize.assert_not_called()


if __name__ == "__main__":
    unittest.main()
