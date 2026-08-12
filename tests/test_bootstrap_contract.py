from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class BootstrapContractTests(unittest.TestCase):
    def test_bootstraps_accept_explicit_python_and_never_delete_environments(self) -> None:
        for name in ("bootstrap_dev.ps1", "bootstrap_cpu.ps1"):
            source = (ROOT / "scripts" / name).read_text(encoding="utf-8")
            with self.subTest(script=name):
                self.assertIn("PythonExecutable", source)
                self.assertIn("Python 3.11", source)
                self.assertNotIn("Remove-Item -Recurse", source)
                self.assertNotIn("py -3.11", source)

    def test_cpu_bootstrap_uses_hash_lock_and_authoritative_vendor(self) -> None:
        source = (ROOT / "scripts" / "bootstrap_cpu.ps1").read_text(encoding="utf-8")
        self.assertIn("requirements-cpu.lock.txt", source)
        self.assertIn("--require-hashes", source)
        self.assertIn("VENDOR_TTS_IMPORT_PASS", source)
        self.assertNotIn("pip install TTS", source)

    def test_dev_bootstrap_is_self_contained_for_gui_and_tests(self) -> None:
        source = (ROOT / "scripts" / "bootstrap_dev.ps1").read_text(encoding="utf-8")
        runner = (ROOT / "scripts" / "run_tests.ps1").read_text(encoding="utf-8")
        self.assertIn("requirements-dev.lock.txt", source)
        self.assertIn("DEV_IMPORT_PASS", source)
        self.assertIn(".venv-dev", runner)
        self.assertNotIn("PYTHONPATH", runner)


if __name__ == "__main__":
    unittest.main()
