from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


class CiContractTests(unittest.TestCase):
    def test_windows_python311_check_is_stable_and_least_privilege(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("permissions:\n  contents: read", text)
        self.assertIn("  windows-python311:\n    name: windows-python311", text)
        self.assertIn("runs-on: windows-latest", text)
        self.assertIn('python-version: "3.11"', text)

    def test_ci_runs_local_acceptance_without_models_or_secrets(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        for required in (
            "bootstrap_dev.ps1",
            "compileall",
            "test_tts_dependency_strategy",
            "test_known_profile_repair tests.test_profiles tests.test_cli",
            "voice_dubbing_runtime doctor --json",
            "run_tests.ps1",
            "import voice_dubbing_runtime, voice_dubbing_app",
            "pip check",
            "git diff --exit-code",
        ):
            with self.subTest(required=required):
                self.assertIn(required, text)
        lowered = text.lower()
        for forbidden in ("secrets.", "provision_xtts", "model download", "cuda", "synthesize"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, lowered)


if __name__ == "__main__":
    unittest.main()
