from __future__ import annotations

import re
import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_VERSION = "0.3.0"


def _declared_version(relative: str) -> str:
    text = (ROOT / relative).read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if match is None:
        raise AssertionError(f"Missing __version__ in {relative}")
    return match.group(1)


class GovernanceTests(unittest.TestCase):
    def test_project_runtime_and_app_versions_are_synchronized(self) -> None:
        with (ROOT / "pyproject.toml").open("rb") as handle:
            project_version = tomllib.load(handle)["project"]["version"]
        self.assertEqual(EXPECTED_VERSION, project_version)
        self.assertEqual(EXPECTED_VERSION, _declared_version("voice_dubbing_runtime/__init__.py"))
        self.assertEqual(EXPECTED_VERSION, _declared_version("voice_dubbing_app/__init__.py"))

    def test_governance_files_and_canonical_pr_target_exist(self) -> None:
        required = (
            ".github/PULL_REQUEST_TEMPLATE.md",
            ".github/ISSUE_TEMPLATE/bug_report.yml",
            ".github/ISSUE_TEMPLATE/feature_request.yml",
            ".github/CODEOWNERS",
            "docs/BRANCH_PROTECTION.md",
        )
        for relative in required:
            with self.subTest(relative=relative):
                self.assertTrue((ROOT / relative).is_file())
        contributing = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
        self.assertIn("pull request vào develop", contributing)
        self.assertIn("owner release review", contributing)
        self.assertEqual("* @akita141188\n", (ROOT / ".github/CODEOWNERS").read_text(encoding="utf-8"))

    def test_protection_plan_uses_exact_ci_check(self) -> None:
        plan = (ROOT / "docs/BRANCH_PROTECTION.md").read_text(encoding="utf-8")
        self.assertIn("## `develop`", plan)
        self.assertIn("## `main`", plan)
        self.assertIn("Require status check `windows-python311`", plan)

    def test_bug_template_requires_sensitive_data_redaction(self) -> None:
        text = (ROOT / ".github/ISSUE_TEMPLATE/bug_report.yml").read_text(encoding="utf-8")
        for phrase in ("path cá nhân", "token", "private voice data"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)


if __name__ == "__main__":
    unittest.main()
