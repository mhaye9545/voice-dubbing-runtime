from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from voice_dubbing_runtime.errors import (
    CONSENT_REQUIRED,
    PROFILE_NOT_FOUND,
    VoiceRuntimeError,
)
from voice_dubbing_runtime.io_utils import read_json, sha256_file
from voice_dubbing_runtime.profiles import VoiceProfileManager
from voice_dubbing_runtime.repair import KnownProfileRepair

from .helpers import build_broken_split_fixture, wave_validator


def data_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): sha256_file(path)
        for path in root.rglob("*")
        if path.is_file()
    }


class KnownProfileRepairTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.profiles = self.base / "profiles"
        _primary, self.primary_hash, self.duc_hash = build_broken_split_fixture(self.profiles)
        self.original_lua_hashes = data_hashes(self.profiles / "lua_china_base")
        self.manager = VoiceProfileManager(self.profiles)
        self.service = KnownProfileRepair(
            self.manager,
            backup_root=self.base / "backup",
            report_root=self.base / "reports",
            audio_validator=wave_validator,
            expected_lua_reference_sha256=self.primary_hash,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_repair_splits_profiles_preserves_history_and_requires_duc_consent(self) -> None:
        source = self.profiles / "lua_china_base" / "references" / "reference_001.wav"
        source_bytes = source.read_bytes()
        result = self.service.execute(application_closed_confirmed=True)
        self.assertEqual("PASS_CONSENT_RECONFIRM_REQUIRED", result["status"])
        lua = self.manager.load("lua_china_base")
        duc = self.manager.load("duc_bao")
        self.assertEqual("Lụa ở China", lua["display_name"])
        self.assertEqual("TECHNICAL_PASS_PENDING_LISTENING", lua["status"])
        self.assertEqual("PENDING", lua["manual_listening"])
        self.assertEqual(["references/ref_primary.wav"], [r["path"] for r in lua["reference_files"]])
        self.assertEqual("Đức Bảo", duc["display_name"])
        self.assertEqual(["references/ref_primary.wav"], [r["path"] for r in duc["reference_files"]])
        self.assertEqual(self.primary_hash, sha256_file(self.manager.resolve_references("lua_china_base")[0]))
        self.assertEqual(self.duc_hash, sha256_file(self.manager.resolve_references("duc_bao")[0]))
        self.assertEqual(source_bytes, source.read_bytes())
        self.assertEqual(
            source_bytes,
            (self.profiles / "duc_bao" / "references" / "ref_primary.wav").read_bytes(),
        )
        self.assertTrue((self.profiles / "lua_china_base" / "profile.phase1.json").is_file())
        self.assertEqual(1, self.manager.profile_revision("lua_china_base"))
        self.assertEqual(1, self.manager.profile_revision("duc_bao"))
        with self.assertRaises(VoiceRuntimeError) as context:
            self.manager.consent("duc_bao")
        self.assertEqual(CONSENT_REQUIRED, context.exception.code)
        self.assertEqual("Pass", result["backup_manifest"]["hash_validation"])
        preserved = Path(result["preserved_original_path"])
        self.assertRegex(preserved.name, r"^\.lua_china_base\.pre-repair-[0-9a-f]{8}$")
        self.assertEqual(self.original_lua_hashes, data_hashes(preserved))
        (self.profiles / ".history").mkdir()
        (self.profiles / ".trash").mkdir()
        listed = {row["profile_id"]: row for row in self.manager.list()}
        self.assertEqual({"lua_china_base", "duc_bao"}, set(listed))
        self.assertEqual("READY", listed["lua_china_base"]["profile_status"])
        self.assertEqual(
            "TECHNICAL_PASS_PENDING_LISTENING",
            listed["lua_china_base"]["status"],
        )
        self.assertTrue(Path(result["report_json"]).is_file())
        self.assertTrue(Path(result["report_markdown"]).is_file())

    def test_repair_discovery_requires_the_exact_legacy_source_id(self) -> None:
        generic = self.profiles / "generic_profile"
        os.rename(self.profiles / "lua_china_base", generic)
        before = data_hashes(generic)
        with self.assertRaises(VoiceRuntimeError) as context:
            self.service.execute(application_closed_confirmed=True)
        self.assertEqual(PROFILE_NOT_FOUND, context.exception.code)
        self.assertEqual(before, data_hashes(generic))
        self.assertFalse((self.profiles / "duc_bao").exists())

    def test_repair_is_idempotent_and_does_not_duplicate_profiles(self) -> None:
        self.service.execute(application_closed_confirmed=True)
        before = data_hashes(self.profiles)
        repeated = self.service.execute(application_closed_confirmed=True)
        after = data_hashes(self.profiles)
        self.assertEqual("ALREADY_REPAIRED", repeated["status"])
        self.assertEqual(before, after)
        visible = [row for row in self.manager.list() if not row["profile_id"].startswith(".")]
        self.assertEqual(["duc_bao", "lua_china_base"], sorted(row["profile_id"] for row in visible))

    def test_explicit_duc_consent_can_be_confirmed_once_after_repair(self) -> None:
        self.service.execute(application_closed_confirmed=True)
        consent = {
            "confirmed": True,
            "profile_id": "duc_bao",
            "statement": "I have rights to Đức Bảo voice.",
            "source": "repair_confirmation",
        }
        confirmed = self.service.execute(
            duc_bao_consent=consent, application_closed_confirmed=True
        )
        self.assertEqual("CONSENT_CONFIRMED", confirmed["status"])
        self.assertTrue(self.manager.consent("duc_bao")["authorized"])
        before = data_hashes(self.profiles)
        again = self.service.execute(
            duc_bao_consent=consent, application_closed_confirmed=True
        )
        self.assertEqual("ALREADY_REPAIRED", again["status"])
        self.assertEqual(before, data_hashes(self.profiles))

    def test_lua_consent_is_rejected_for_duc(self) -> None:
        with self.assertRaises(VoiceRuntimeError) as context:
            self.service.execute(
                duc_bao_consent={
                    "confirmed": True,
                    "profile_id": "lua_china_base",
                    "statement": "wrong identity",
                },
                application_closed_confirmed=True,
            )
        self.assertEqual(CONSENT_REQUIRED, context.exception.code)
        self.assertFalse((self.profiles / "duc_bao").exists())

    def test_generic_consent_confirmation_is_identity_bound_and_idempotent(self) -> None:
        self.service.execute(application_closed_confirmed=True)
        with self.assertRaises(VoiceRuntimeError) as context:
            self.manager.confirm_consent(
                "duc_bao",
                {
                    "confirmed": True,
                    "profile_id": "lua_china_base",
                    "statement": "wrong profile",
                },
            )
        self.assertEqual(CONSENT_REQUIRED, context.exception.code)
        request = {
            "confirmed": True,
            "profile_id": "duc_bao",
            "statement": "Explicit Đức Bảo rights confirmation",
            "source": "test_checkbox",
        }
        first = self.manager.confirm_consent("duc_bao", request)
        self.assertEqual("CONSENT_CONFIRMED", first["status"])
        self.assertEqual("READY", first["profile"]["status"])
        self.assertEqual(2, self.manager.profile_revision("duc_bao"))
        before = data_hashes(self.profiles / "duc_bao")
        second = self.manager.confirm_consent("duc_bao", request)
        self.assertEqual("ALREADY_CONFIRMED", second["status"])
        self.assertFalse(second["changed"])
        self.assertEqual(2, self.manager.profile_revision("duc_bao"))
        self.assertEqual(before, data_hashes(self.profiles / "duc_bao"))

    def test_failed_audio_validation_rolls_original_profile_back(self) -> None:
        profile_hash = sha256_file(self.profiles / "lua_china_base" / "profile.json")

        def fail_on_duc(path: Path) -> dict:
            if path.name == "reference_001.wav":
                raise RuntimeError("injected audio validation failure")
            return wave_validator(path)

        service = KnownProfileRepair(
            self.manager,
            backup_root=self.base / "failed-backup",
            report_root=self.base / "failed-report",
            audio_validator=fail_on_duc,
            expected_lua_reference_sha256=self.primary_hash,
        )
        with self.assertRaises(RuntimeError):
            service.execute(application_closed_confirmed=True)
        self.assertEqual(profile_hash, sha256_file(self.profiles / "lua_china_base" / "profile.json"))
        self.assertFalse((self.profiles / "duc_bao").exists())
        self.assertEqual([], list(self.profiles.glob(".profile-repair-txn-*")))

    def test_commit_failure_restores_original_profile_after_pre_repair_snapshot(self) -> None:
        before = data_hashes(self.profiles)
        real_rename = os.rename

        def fail_duc_commit(source: str | Path, destination: str | Path) -> None:
            source_path = Path(source)
            destination_path = Path(destination)
            if (
                destination_path == self.profiles / "duc_bao"
                and source_path.name == "duc_bao"
                and ".profile-repair-txn-" in source_path.as_posix()
            ):
                raise OSError("injected failure after Lụa commit")
            real_rename(source, destination)

        with mock.patch("voice_dubbing_runtime.repair.os.rename", side_effect=fail_duc_commit):
            with self.assertRaisesRegex(OSError, "injected failure"):
                self.service.execute(application_closed_confirmed=True)

        self.assertEqual(before, data_hashes(self.profiles))
        self.assertFalse((self.profiles / "duc_bao").exists())
        self.assertEqual([], list(self.profiles.glob(".lua_china_base.pre-repair-*")))
        self.assertEqual([], list(self.profiles.glob(".profile-repair-txn-*")))

    def test_migrate_legacy_is_idempotent(self) -> None:
        first = self.manager.migrate_legacy(
            "lua_china_base",
            display_name="Lụa ở China",
            profile_type="cloned",
            source_type="video",
            source_language="vi",
            default_language="vi",
            engine_preference="vixtts_vi",
            canonical_reference="references/ref_primary.wav",
        )
        first_hash = sha256_file(self.profiles / "lua_china_base" / "profile.json")
        second = self.manager.migrate_legacy(
            "lua_china_base",
            display_name="Lụa ở China",
            profile_type="cloned",
            source_type="video",
            source_language="vi",
            default_language="vi",
            engine_preference="vixtts_vi",
            canonical_reference="references/ref_primary.wav",
        )
        self.assertEqual(first, second)
        self.assertEqual(first_hash, sha256_file(self.profiles / "lua_china_base" / "profile.json"))


if __name__ == "__main__":
    unittest.main()
