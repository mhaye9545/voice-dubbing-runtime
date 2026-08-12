from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PUBLIC_DOCS = (
    "LICENSE",
    "README.md",
    "README.vi.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "CODE_OF_CONDUCT.md",
    "THIRD_PARTY_NOTICES.md",
    "LICENSE_STATUS.md",
    "docs/MODEL_LICENSES.md",
    "docs/BRANCH_PROTECTION.md",
)
LINK = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


class PublicDocsTests(unittest.TestCase):
    def test_required_public_docs_exist(self) -> None:
        for relative in PUBLIC_DOCS:
            with self.subTest(relative=relative):
                self.assertTrue((ROOT / relative).is_file())

    def test_relative_markdown_links_resolve(self) -> None:
        for relative in PUBLIC_DOCS:
            document = ROOT / relative
            for destination in LINK.findall(document.read_text(encoding="utf-8")):
                if destination.startswith(("http://", "https://", "mailto:", "#")):
                    continue
                path_text = destination.split("#", 1)[0]
                with self.subTest(document=relative, destination=destination):
                    self.assertTrue((document.parent / path_text).resolve().exists())

    def test_license_status_preserves_code_model_data_boundary(self) -> None:
        text = (ROOT / "LICENSE_STATUS.md").read_text(encoding="utf-8")
        self.assertIn("Apache License 2.0", text)
        self.assertIn("CODE LICENSE != MODEL LICENSE != WEIGHTS/DATA/VOICE RIGHTS", text)
        self.assertIn("viXTTS/XTTS-v2", text)
        self.assertIn("voice/reference/user data", text)

    def test_root_license_is_unmodified_apache_2_text(self) -> None:
        text = (ROOT / "LICENSE").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("\n                                 Apache License\n"))
        self.assertIn("Version 2.0, January 2004", text)
        self.assertIn("3. Grant of Patent License.", text)
        self.assertIn("END OF TERMS AND CONDITIONS", text)


if __name__ == "__main__":
    unittest.main()
