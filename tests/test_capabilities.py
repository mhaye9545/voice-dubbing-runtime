from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from voice_dubbing_runtime.capabilities import EngineRegistry
from voice_dubbing_runtime.errors import ENGINE_UNAVAILABLE, UNSUPPORTED_LANGUAGE, VoiceRuntimeError


class EngineRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.root = self.base / "runtime"
        self.root.mkdir()
        for relative in ("vi/model.pth", "vi/config.json", "multi/model.pth", "multi/config.json"):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}", encoding="utf-8")
        (self.root / "multi" / "config.json").write_text(
            json.dumps({"languages": ["en", "fr", "zh-cn"]}), encoding="utf-8"
        )
        self.config = self.base / "engines.json"
        self.config.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "engines": [
                        {
                            "id": "vixtts_vi",
                            "priority": 10,
                            "languages": ["vi"],
                            "language_source": "engine_contract",
                            "devices": ["cpu"],
                            "required_paths": ["vi/model.pth", "vi/config.json"],
                            "backend": "tests.fake:Backend",
                        },
                        {
                            "id": "xtts_v2_multilingual",
                            "priority": 20,
                            "languages": [],
                            "language_source": "model_config",
                            "model_config_path": "multi/config.json",
                            "devices": ["cpu"],
                            "required_paths": ["multi/model.pth", "multi/config.json"],
                            "backend": "tests.fake:Backend",
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.registry = EngineRegistry(self.root, self.config)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_capabilities_languages_come_from_engine_and_model_configs(self) -> None:
        payload = self.registry.as_dict()
        engines = {item["id"]: item for item in payload["engines"]}
        self.assertEqual(["vi"], engines["vixtts_vi"]["languages"])
        self.assertEqual(["en", "fr", "zh-cn"], engines["xtts_v2_multilingual"]["languages"])
        self.assertTrue(engines["vixtts_vi"]["available"])

    def test_auto_prefers_vixtts_for_vietnamese(self) -> None:
        self.assertEqual("vixtts_vi", self.registry.select("auto", "vi").id)

    def test_auto_selects_multilingual_for_declared_language(self) -> None:
        self.assertEqual("xtts_v2_multilingual", self.registry.select("auto", "fr").id)

    def test_explicit_engine_never_falls_through(self) -> None:
        with self.assertRaises(VoiceRuntimeError) as context:
            self.registry.select("vixtts_vi", "en")
        self.assertEqual(UNSUPPORTED_LANGUAGE, context.exception.code)

    def test_unsupported_language_is_stable_error(self) -> None:
        with self.assertRaises(VoiceRuntimeError) as context:
            self.registry.select("auto", "th")
        self.assertEqual(UNSUPPORTED_LANGUAGE, context.exception.code)

    def test_missing_model_marks_engine_unavailable(self) -> None:
        (self.root / "vi" / "model.pth").unlink()
        capability = self.registry.get("vixtts_vi")
        self.assertFalse(capability.available)
        with self.assertRaises(VoiceRuntimeError) as context:
            self.registry.select("vixtts_vi", "vi")
        self.assertEqual(ENGINE_UNAVAILABLE, context.exception.code)


if __name__ == "__main__":
    unittest.main()
