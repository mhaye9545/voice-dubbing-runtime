from __future__ import annotations

import ast
import hashlib
import json
import os
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from voice_dubbing_runtime.capabilities import EngineRegistry
from voice_dubbing_runtime.xtts_engine_worker import (
    LICENSE_ID,
    LICENSE_URL,
    MODEL_ID,
    MODEL_REVISION,
    _save_pcm16_exclusive,
    _verify_model_and_license,
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


class XttsCapabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        for relative in ("bin/python.exe", "model/model.pth", "model/vocab.json"):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x")
        (self.root / "model" / "config.json").write_text(
            json.dumps({"languages": ["en", "ko", "zh-cn", "fr"]}), encoding="utf-8"
        )
        (self.root / "model" / "health.json").write_text(
            json.dumps(
                {
                    "status": "PASS",
                    "model_load": "Pass",
                    "synthesis_smoke": "Pass",
                    "ffmpeg_decode": "Pass",
                    "model_revision": MODEL_REVISION,
                    "languages_validated": ["en", "ko", "zh-cn"],
                }
            ),
            encoding="utf-8",
        )
        self.config = self.root / "engines.json"
        self.config.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "engines": [
                        {
                            "id": "xtts_v2_multilingual",
                            "language_source": "model_config",
                            "model_config_path": "model/config.json",
                            "devices": ["cpu"],
                            "profile_types": ["cloned", "preset"],
                            "required_paths": [
                                "bin/python.exe",
                                "model/model.pth",
                                "model/vocab.json",
                                "model/health.json",
                            ],
                            "health_report_path": "model/health.json",
                            "health_required_values": {
                                "model_load": "Pass",
                                "synthesis_smoke": "Pass",
                                "ffmpeg_decode": "Pass",
                                "model_revision": MODEL_REVISION,
                            },
                            "health_required_languages": ["en", "ko", "zh-cn"],
                            "backend": "tests.fake:Backend",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_available_languages_come_from_model_not_three_language_smoke_list(self) -> None:
        item = EngineRegistry(self.root, self.config).as_dict()["engines"][0]
        self.assertTrue(item["available"])
        self.assertEqual(["en", "ko", "zh-cn", "fr"], item["languages"])
        self.assertEqual(["cloned", "preset"], item["profile_types"])

    def test_engine_is_unavailable_until_real_health_report_passes(self) -> None:
        health = self.root / "model" / "health.json"
        payload = json.loads(health.read_text(encoding="utf-8"))
        payload["status"] = "FAILED"
        health.write_text(json.dumps(payload), encoding="utf-8")
        item = EngineRegistry(self.root, self.config).as_dict()["engines"][0]
        self.assertFalse(item["available"])
        self.assertIn("ENGINE_HEALTH_NOT_PASSED", item["unavailable_reason"])


class XttsIntegrityGateTests(unittest.TestCase):
    def test_worker_verifies_model_hashes_and_separate_cpml_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "runtime"
            model = root / "models" / "xtts_v2"
            model.mkdir(parents=True)
            for name, content in (
                ("LICENSE.txt", b"license"),
                ("config.json", b"{}"),
                ("model.pth", b"model"),
                ("vocab.json", b"{}"),
            ):
                (model / name).write_bytes(content)
            records = [
                {
                    "path": path.name,
                    "size_bytes": path.stat().st_size,
                    "sha256": _sha(path),
                }
                for path in sorted(model.iterdir())
            ]
            (model / "model_manifest.json").write_text(
                json.dumps(
                    {
                        "model_id": MODEL_ID,
                        "revision": MODEL_REVISION,
                        "files": records,
                    }
                ),
                encoding="utf-8",
            )
            local = Path(raw) / "local"
            acceptance = (
                local
                / "FrameExtractStudio"
                / "VoiceDubbing"
                / "licenses"
                / "coqui_xtts_v2_cpml.json"
            )
            acceptance.parent.mkdir(parents=True)
            acceptance.write_text(
                json.dumps(
                    {
                        "accepted": True,
                        "model_id": MODEL_ID,
                        "revision": MODEL_REVISION,
                        "license_id": LICENSE_ID,
                        "license_url": LICENSE_URL,
                        "license_sha256": _sha(model / "LICENSE.txt"),
                        "scope": "research_personal_poc_noncommercial",
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"LOCALAPPDATA": str(local)}):
                verified, manifest = _verify_model_and_license(root)
                self.assertEqual(model, verified)
                self.assertEqual(MODEL_REVISION, manifest["revision"])
                payload = json.loads(acceptance.read_text(encoding="utf-8"))
                payload["revision"] = "wrong"
                acceptance.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "MODEL_LICENSE_NOT_ACCEPTED"):
                    _verify_model_and_license(root)

    def test_parent_backend_source_has_no_ml_import(self) -> None:
        import voice_dubbing_runtime.xtts_backend as module

        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertTrue({"torch", "torchaudio", "transformers", "TTS"}.isdisjoint(imported))


class XttsOutputPersistenceTests(unittest.TestCase):
    def test_pcm16_output_is_durable_on_windows_and_never_overwritten(self) -> None:
        runtime_volume = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(dir=runtime_volume) as raw:
            output = Path(raw) / "generated.wav"
            _save_pcm16_exclusive(output, [0.0, 0.25, -0.25, 0.1], 24000)
            self.assertTrue(output.is_file())
            self.assertGreater(output.stat().st_size, 44)
            original_hash = _sha(output)
            with wave.open(str(output), "rb") as reader:
                self.assertEqual(1, reader.getnchannels())
                self.assertEqual(2, reader.getsampwidth())
                self.assertEqual(24000, reader.getframerate())
                self.assertEqual(4, reader.getnframes())
            with self.assertRaises(FileExistsError):
                _save_pcm16_exclusive(output, [0.0], 24000)
            self.assertEqual(original_hash, _sha(output))
            self.assertEqual([], list(output.parent.glob(".generated.*.wav")))


if __name__ == "__main__":
    unittest.main()
