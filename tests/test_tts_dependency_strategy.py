from __future__ import annotations

import tomllib
import unittest
from pathlib import Path

from voice_dubbing_runtime.vixtts_backend import TTS_REVISION, VixttsBackend


ROOT = Path(__file__).resolve().parents[1]
VENDOR_ROOT = ROOT / "vendor" / f"TTS-{TTS_REVISION}"


class TtsDependencyStrategyTests(unittest.TestCase):
    def test_vendored_revision_is_the_only_declared_vixtts_source(self) -> None:
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        dependencies = project["project"]["dependencies"]
        self.assertFalse(any(item.partition(" ")[0].lower() == "tts" for item in dependencies))

        requirements = (ROOT / "requirements-cpu.txt").read_text(encoding="utf-8")
        self.assertNotIn("TTS @", requirements)
        self.assertNotIn("thinhlpg/TTS.git", requirements)

        backend = VixttsBackend(ROOT)
        self.assertEqual(VENDOR_ROOT.resolve(), backend.vendor_root)

    def test_vendored_runtime_model_packages_are_present(self) -> None:
        required = (
            "TTS/encoder/models/base_encoder.py",
            "TTS/tts/models/base_tts.py",
            "TTS/tts/models/xtts.py",
            "TTS/vc/models/base_vc.py",
            "TTS/vocoder/models/hifigan_generator.py",
        )
        for relative_path in required:
            with self.subTest(path=relative_path):
                self.assertTrue((VENDOR_ROOT / relative_path).is_file())

    def test_provenance_records_exact_fork_commit_and_license(self) -> None:
        provenance = (ROOT / "vendor" / "TTS_PROVENANCE.md").read_text(encoding="utf-8")
        self.assertIn("https://github.com/thinhlpg/TTS", provenance)
        self.assertIn(TTS_REVISION, provenance)
        self.assertIn("MPL-2.0", provenance)


if __name__ == "__main__":
    unittest.main()
