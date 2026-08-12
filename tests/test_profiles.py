from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from voice_dubbing_runtime.errors import (
    CONSENT_REQUIRED,
    INVALID_REFERENCE,
    VoiceRuntimeError,
)
from voice_dubbing_runtime.profiles import VoiceProfileManager

from .helpers import write_pcm_wav


class VoiceProfileManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.manager = VoiceProfileManager(self.base / "profiles")
        self.reference = write_pcm_wav(self.base / "nguồn giọng" / "giọng mẫu.wav")
        self.consent = {"confirmed": True, "statement": "Tôi có quyền sử dụng giọng nói này."}

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def create_cloned(self, profile_id: str = "sample_voice") -> dict:
        return self.manager.create(
            profile_id=profile_id,
            display_name="Sample Voice",
            profile_type="cloned",
            source_type="audio",
            default_language="vi",
            engine_preference="vixtts_vi",
            reference_files=[self.reference],
            consent=self.consent,
        )

    def test_create_cloned_profile_writes_required_layout(self) -> None:
        profile = self.create_cloned()
        directory = self.manager.root / profile["profile_id"]
        self.assertEqual("vi", profile["default_language"])
        self.assertFalse(profile["is_base_voice_preset"])
        for relative in ("profile.json", "quality.json", "consent.json", "profile.lock"):
            self.assertTrue((directory / relative).is_file(), relative)
        self.assertTrue((directory / profile["reference_files"][0]["path"]).is_file())
        self.assertEqual(profile, self.manager.load("sample_voice"))

    def test_unicode_source_path_round_trips_and_copy_hash_is_verified(self) -> None:
        self.create_cloned()
        resolved = self.manager.resolve_references("sample_voice")
        self.assertEqual(1, len(resolved))
        self.assertGreater(resolved[0].stat().st_size, 0)

    def test_create_preset_and_base_voice_flag(self) -> None:
        profile = self.manager.create(
            profile_id="female_soft_01",
            display_name="Nữ nhẹ nhàng 01",
            profile_type="preset",
            source_type="audio",
            default_language="en",
            engine_preference="xtts_v2_multilingual",
            reference_files=[self.reference],
            consent=self.consent,
            is_base_voice_preset=True,
        )
        self.assertTrue(profile["is_base_voice_preset"])
        self.assertEqual("preset", profile["profile_type"])

    def test_create_without_consent_is_rejected(self) -> None:
        with self.assertRaises(VoiceRuntimeError) as context:
            self.manager.create(
                profile_id="denied",
                display_name="Denied",
                profile_type="cloned",
                source_type="audio",
                default_language="vi",
                engine_preference="auto",
                reference_files=[self.reference],
                consent=None,
            )
        self.assertEqual(CONSENT_REQUIRED, context.exception.code)

    def test_string_false_is_not_consent(self) -> None:
        with self.assertRaises(VoiceRuntimeError) as context:
            self.manager.create(
                profile_id="string_false",
                display_name="Denied",
                profile_type="cloned",
                source_type="audio",
                default_language="vi",
                engine_preference="auto",
                reference_files=[self.reference],
                consent={"confirmed": "false"},
            )
        self.assertEqual(CONSENT_REQUIRED, context.exception.code)

    def test_consent_record_must_match_profile_id(self) -> None:
        self.create_cloned()
        path = self.manager.root / "sample_voice" / "consent.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["profile_id"] = "another_profile"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(VoiceRuntimeError) as context:
            self.manager.consent("sample_voice")
        self.assertEqual(CONSENT_REQUIRED, context.exception.code)

    def test_duplicate_profile_never_overwrites(self) -> None:
        original = self.create_cloned()
        duplicate = self.create_cloned()
        self.assertEqual("sample_voice_2", duplicate["profile_id"])
        self.assertEqual(original, self.manager.load("sample_voice"))
        self.assertEqual(duplicate, self.manager.load("sample_voice_2"))

    def test_update_reference_preserves_prior_file_and_reuses_consent(self) -> None:
        original = self.create_cloned()
        old_path = self.manager.root / "sample_voice" / original["reference_files"][0]["path"]
        replacement = write_pcm_wav(self.base / "新的参考.wav", frequency=260.0)
        updated = self.manager.update(
            "sample_voice",
            display_name="Tên mới",
            reference_files=[replacement],
        )
        new_path = self.manager.root / "sample_voice" / updated["reference_files"][0]["path"]
        self.assertEqual("Tên mới", updated["display_name"])
        self.assertTrue(old_path.is_file())
        self.assertTrue(new_path.is_file())
        self.assertNotEqual(old_path, new_path)
        self.assertTrue(self.manager.consent("sample_voice")["authorized"])

    def test_migrate_legacy_profile_preserves_phase1_evidence(self) -> None:
        profile_dir = self.manager.root / "lua_china_base"
        references = profile_dir / "references"
        references.mkdir(parents=True)
        reference = write_pcm_wav(references / "ref_primary.wav", seconds=8.0)
        legacy_profile = {
            "schema_version": 1,
            "profile_id": "lua_china_base",
            "created_at": "2026-08-02T00:00:00+07:00",
            "status": "TECHNICAL_PASS_PENDING_LISTENING",
            "engine": "vixtts",
            "language": "vi",
            "reference_files": ["references/ref_primary.wav"],
        }
        legacy_consent = {
            "schema_version": 1,
            "voice_profile_id": "lua_china_base",
            "authorized_use_confirmed": True,
            "statement": "Tôi xác nhận có quyền sử dụng giọng nói này.",
            "confirmed_at": "2026-08-02T00:00:00+07:00",
        }
        (profile_dir / "profile.json").write_text(json.dumps(legacy_profile), encoding="utf-8")
        (profile_dir / "consent.json").write_text(json.dumps(legacy_consent), encoding="utf-8")
        (profile_dir / "quality_report.json").write_text(
            json.dumps({"schema_version": 1, "status": "TECHNICAL_PASS_PENDING_LISTENING"}),
            encoding="utf-8",
        )

        migrated = self.manager.migrate_legacy(
            "lua_china_base",
            display_name="Lụa ở China",
            profile_type="cloned",
            source_type="audio",
            default_language="vi",
            engine_preference="vixtts_vi",
        )

        self.assertEqual(migrated["display_name"], "Lụa ở China")
        self.assertTrue(migrated["enabled"])
        self.assertEqual(migrated["reference_files"][0]["path"], "references/ref_primary.wav")
        self.assertTrue(self.manager.consent("lua_china_base")["authorized"])
        self.assertEqual(self.manager.resolve_references("lua_china_base"), [reference.resolve()])
        for filename in (
            "profile.phase1.json", "consent.phase1.json", "profile.lock", "quality.json"
        ):
            self.assertTrue((profile_dir / filename).is_file(), filename)
        self.assertEqual(
            json.loads((profile_dir / "profile.phase1.json").read_text(encoding="utf-8")),
            legacy_profile,
        )
        self.assertEqual(
            json.loads((profile_dir / "consent.phase1.json").read_text(encoding="utf-8")),
            legacy_consent,
        )
        self.assertEqual(
            self.manager.migrate_legacy(
                "lua_china_base",
                display_name="Lụa ở China",
                profile_type="cloned",
                source_type="audio",
                default_language="vi",
                engine_preference="vixtts_vi",
            )["profile_id"],
            "lua_china_base",
        )

    def test_tampered_reference_is_rejected(self) -> None:
        profile = self.create_cloned()
        path = self.manager.root / "sample_voice" / profile["reference_files"][0]["path"]
        with path.open("ab") as handle:
            handle.write(b"tamper")
        with self.assertRaises(VoiceRuntimeError) as context:
            self.manager.resolve_references("sample_voice")
        self.assertEqual(INVALID_REFERENCE, context.exception.code)

    def test_list_and_recoverable_delete(self) -> None:
        self.create_cloned()
        self.assertEqual(["sample_voice"], [item["profile_id"] for item in self.manager.list()])
        deleted = self.manager.delete("sample_voice")
        self.assertEqual("deleted", deleted["status"])
        self.assertTrue(Path(deleted["recoverable_path"]).is_dir())
        self.assertEqual([], self.manager.list())

    def test_legacy_lester_profile_id_round_trips_without_display_name_contract(self) -> None:
        created = self.manager.create(
            profile_id="lestehrolt_en_clean",
            display_name="Synthetic English Profile",
            profile_type="cloned",
            source_type="audio",
            source_language="en",
            default_language="en",
            engine_preference="xtts_v2_multilingual",
            reference_files=[self.reference],
            consent=self.consent,
        )
        self.assertEqual("lestehrolt_en_clean", created["profile_id"])
        self.assertEqual(
            "Synthetic English Profile",
            self.manager.load("lestehrolt_en_clean")["display_name"],
        )
        self.assertEqual(1, self.manager.profile_revision("lestehrolt_en_clean"))
        listed = {row["profile_id"]: row for row in self.manager.list()}
        self.assertEqual({"lestehrolt_en_clean"}, set(listed))
        self.assertEqual("READY", listed["lestehrolt_en_clean"]["profile_status"])


if __name__ == "__main__":
    unittest.main()
