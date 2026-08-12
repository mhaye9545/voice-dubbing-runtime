from __future__ import annotations

import unittest
from pathlib import Path

from voice_dubbing_runtime.doctor import run_doctor


ROOT = Path(__file__).resolve().parents[1]


class DoctorTests(unittest.TestCase):
    def test_doctor_is_read_only_and_reports_stable_status_vocabulary(self) -> None:
        before = sorted(path.relative_to(ROOT) for path in ROOT.rglob("migration.json"))
        payload = run_doctor(ROOT)
        after = sorted(path.relative_to(ROOT) for path in ROOT.rglob("migration.json"))
        self.assertEqual(before, after)
        self.assertEqual(1, payload["schema_version"])
        self.assertIn(payload["status"], {"PASS", "WARN", "FAIL"})
        self.assertTrue(payload["checks"])
        self.assertTrue(
            all(item["status"] in {"PASS", "WARN", "FAIL", "SKIP"} for item in payload["checks"])
        )

    def test_required_vendor_source_resolves_without_installed_tts(self) -> None:
        payload = run_doctor(ROOT)
        checks = {item["name"]: item for item in payload["checks"] if item["group"] == "vixtts"}
        self.assertEqual("PASS", checks["vendor_files"]["status"])
        self.assertEqual("PASS", checks["duplicate_distribution"]["status"])
        self.assertEqual("PASS", checks["source_resolution"]["status"])
        self.assertIn("vendor", checks["source_resolution"]["details"]["tts_file"])


if __name__ == "__main__":
    unittest.main()
