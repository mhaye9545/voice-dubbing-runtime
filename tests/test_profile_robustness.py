from __future__ import annotations

import tempfile
import unittest
import uuid
import inspect
from pathlib import Path

from voice_dubbing_runtime.errors import VoiceRuntimeError
from voice_dubbing_runtime.io_utils import sha256_file
from voice_dubbing_runtime.profiles import VoiceProfileManager

from .helpers import json_write, write_wav


class ProfileRobustnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.root = self.base / "profiles"
        self.manager = VoiceProfileManager(self.root)
        self.reference_a = write_wav(self.base / "A.wav", frequency=220.0)
        self.reference_b = write_wav(self.base / "B.wav", frequency=330.0)
        self.consent = {"confirmed": True, "statement": "fixture rights confirmation"}

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def create(self, name: str, profile_id: str, reference: Path) -> dict:
        return self.manager.create(
            profile_id=profile_id,
            display_name=name,
            profile_type="cloned",
            source_type="audio",
            source_language="vi",
            default_language="vi",
            engine_preference="vixtts_vi",
            reference_files=[reference],
            consent=self.consent,
        )

    def test_create_b_with_selected_a_id_never_changes_a(self) -> None:
        profile_a = self.create("Profile A", "profile_a", self.reference_a)
        a_metadata = sha256_file(self.root / "profile_a" / "profile.json")
        a_reference = sha256_file(self.manager.resolve_references("profile_a")[0])
        # Simulates the historical UI bug passing selected A as the requested ID.
        profile_b = self.create("Profile B", "profile_a", self.reference_b)
        self.assertEqual("profile_a_2", profile_b["profile_id"])
        self.assertEqual(a_metadata, sha256_file(self.root / "profile_a" / "profile.json"))
        self.assertEqual(a_reference, sha256_file(self.manager.resolve_references("profile_a")[0]))
        self.assertNotEqual(
            sha256_file(self.manager.resolve_references("profile_a")[0]),
            sha256_file(self.manager.resolve_references("profile_a_2")[0]),
        )
        self.assertEqual("profile_a", profile_a["profile_id"])

    def test_collision_ids_increment_without_overwrite(self) -> None:
        identifiers = [
            self.create("Đức Bảo", "duc_bao", self.reference_a)["profile_id"],
            self.create("Đức Bảo", "duc_bao", self.reference_b)["profile_id"],
            self.create("Đức Bảo", "duc_bao", self.reference_a)["profile_id"],
        ]
        self.assertEqual(["duc_bao", "duc_bao_2", "duc_bao_3"], identifiers)

    def test_source_language_is_independent_schema_field(self) -> None:
        profile = self.manager.create(
            display_name="Cross language",
            profile_type="cloned",
            source_type="audio",
            source_language="vi",
            default_language="en",
            engine_preference="auto",
            reference_files=[self.reference_a],
            consent=self.consent,
        )
        self.assertEqual("vi", profile["source_language"])
        self.assertEqual("en", profile["default_language"])

    def test_list_keeps_invalid_directory_and_valid_profiles(self) -> None:
        self.create("Zulu", "zulu", self.reference_a)
        invalid = self.root / "broken profile"
        invalid.mkdir(parents=True)
        json_write(invalid / "profile.json", {"schema_version": 999})
        rows = self.manager.list()
        self.assertEqual(2, len(rows))
        broken = next(row for row in rows if row["profile_id"] == "broken profile")
        self.assertFalse(broken["valid"])
        self.assertEqual("PROFILE_ERROR", broken["status"])
        self.assertTrue(broken["error"])
        valid = next(row for row in rows if row["profile_id"] == "zulu")
        self.assertTrue(valid["valid"])

    def test_atomic_create_rolls_back_staging_on_validation_failure(self) -> None:
        def fail(_path: Path) -> None:
            raise RuntimeError("injected staged validation failure")

        manager = VoiceProfileManager(self.root, staged_validator=fail)
        with self.assertRaises(RuntimeError):
            manager.create(
                profile_id="will_rollback",
                display_name="Will rollback",
                profile_type="cloned",
                source_type="audio",
                source_language="en",
                default_language="en",
                engine_preference="auto",
                reference_files=[self.reference_a],
                consent=self.consent,
            )
        self.assertFalse((self.root / "will_rollback").exists())
        self.assertEqual([], list(self.root.glob(".will_rollback.creating-*")))
        self.assertFalse((self.root / ".profile-create.lock").exists())

    def test_existing_update_uses_uuid_staging_and_never_replaces_live_directory(self) -> None:
        profile = self.create("Profile A", "profile_a", self.reference_a)
        live = self.root / "profile_a"
        reference = live / profile["reference_files"][0]["path"]
        original_reference_hash = sha256_file(reference)
        original_identity = live.stat().st_ino
        observed: list[Path] = []

        def inspect_staging(path: Path) -> None:
            observed.append(path)
            self.assertEqual(".staging", path.parent.name)
            uuid.UUID(path.name)
            self.assertNotIn("profile_a", path.name)
            self.assertTrue(live.is_dir())
            self.assertEqual(original_reference_hash, sha256_file(reference))

        self.manager._staged_validator = inspect_staging
        result = self.manager.set_reference_state(
            "profile_a",
            "TECHNICAL_PASS_PENDING_LISTENING",
            evidence={"target_speaker_window": {"start_seconds": 0.0, "end_seconds": 50.0}},
            expected_revision=1,
        )
        self.assertEqual(1, len(observed))
        self.assertTrue(live.is_dir())
        self.assertEqual(original_identity, live.stat().st_ino)
        self.assertEqual(original_reference_hash, sha256_file(reference))
        self.assertTrue(Path(result["history_path"]).is_dir())
        self.assertEqual("uuid_in_place_update", result["staging_policy"])
        self.assertFalse((self.root / ".staging").exists())

    def test_failed_existing_update_preserves_profile_primary_and_consent(self) -> None:
        profile = self.create("Profile A", "profile_a", self.reference_a)
        live = self.root / "profile_a"
        primary = live / profile["reference_files"][0]["path"]
        before = {
            name: sha256_file(live / name)
            for name in ("profile.json", "quality.json", "consent.json", "profile.lock")
        }
        primary_hash = sha256_file(primary)

        def fail(_path: Path) -> None:
            raise RuntimeError("injected existing-profile staged validation failure")

        self.manager._staged_validator = fail
        with self.assertRaisesRegex(RuntimeError, "injected existing-profile"):
            self.manager.set_reference_state(
                "profile_a", "NEEDS_MANUAL_REFERENCE", expected_revision=1
            )
        self.assertTrue(live.is_dir())
        self.assertEqual(primary_hash, sha256_file(primary))
        self.assertEqual(
            before,
            {
                name: sha256_file(live / name)
                for name in ("profile.json", "quality.json", "consent.json", "profile.lock")
            },
        )
        self.assertTrue(self.manager.consent("profile_a")["authorized"])
        self.assertFalse((self.root / ".staging").exists())

    def test_final_source_mix_primary_commits_atomically_once(self) -> None:
        profile = self.create("Old name", "profile_a", self.reference_a)
        live = self.root / "profile_a"
        old_reference = live / profile["reference_files"][0]["path"]
        old_hash = sha256_file(old_reference)
        source_mix = write_wav(self.base / "source_mix.wav", frequency=250.0)
        primary = write_wav(self.base / "light_cleaned.wav", frequency=255.0)
        result = self.manager.install_final_reference_set(
            "profile_a",
            source_mix=source_mix,
            primary=primary,
            voice_only=None,
            expected_revision=1,
            prepare_job_id="job-final",
            validation={"status": "PASS", "ffmpeg_decode": "Pass"},
            selected_reference={
                "candidate_number": 2,
                "start_seconds": 13.0,
                "end_seconds": 27.0,
                "variant": "CLEAN_SOURCE_MIX",
            },
            reference_processing={"separation_effective": False},
            target_speaker_window={"start_seconds": 0.0, "end_seconds": 50.0},
            single_speaker_confirmed=True,
        )
        self.assertEqual(2, result["profile_revision"])
        loaded = self.manager.load("profile_a")
        self.assertEqual("READY", loaded["status"])
        self.assertEqual("Lester Holt EN", loaded["display_name"])
        self.assertEqual("references/ref_primary.wav", loaded["reference_files"][0]["path"])
        self.assertEqual(sha256_file(primary), sha256_file(live / "references/ref_primary.wav"))
        self.assertEqual(sha256_file(source_mix), sha256_file(live / "references/ref_source_mix.wav"))
        self.assertFalse((live / "references/ref_voice_only.wav").exists())
        self.assertTrue(old_reference.is_file())
        self.assertEqual(old_hash, sha256_file(old_reference))

    def test_final_reference_failure_does_not_increment_revision(self) -> None:
        self.create("Profile A", "profile_a", self.reference_a)
        source_mix = write_wav(self.base / "source_mix.wav", frequency=250.0)
        self.manager._staged_validator = lambda _path: (_ for _ in ()).throw(
            RuntimeError("final gate rejected")
        )
        with self.assertRaisesRegex(RuntimeError, "final gate rejected"):
            self.manager.install_final_reference_set(
                "profile_a",
                source_mix=source_mix,
                primary=source_mix,
                voice_only=None,
                expected_revision=1,
                prepare_job_id="job-final",
                validation={"status": "PASS"},
                selected_reference={
                    "candidate_number": 2,
                    "start_seconds": 13.0,
                    "end_seconds": 27.0,
                    "variant": "CLEAN_SOURCE_MIX",
                },
                reference_processing={"separation_effective": False},
                target_speaker_window={"start_seconds": 0.0, "end_seconds": 50.0},
                single_speaker_confirmed=True,
            )
        self.assertEqual(1, self.manager.profile_revision("profile_a"))

    def test_atomic_writer_reopens_temporary_in_r_plus_b_before_fsync(self) -> None:
        source = inspect.getsource(VoiceProfileManager._copy_verified_replace)
        self.assertIn('temporary.open("r+b")', source)
        self.assertIn("os.fsync(durable_handle.fileno())", source)
        self.assertIn("os.replace(temporary, target)", source)


if __name__ == "__main__":
    unittest.main()
