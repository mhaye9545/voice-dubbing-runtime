from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from voice_dubbing_runtime.cli import main

from .helpers import write_pcm_wav


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.profiles = self.base / "profiles"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def invoke(self, arguments: list[str]) -> tuple[int, dict]:
        stream = io.StringIO()
        with redirect_stdout(stream):
            code = main(["--profiles-root", str(self.profiles), *arguments])
        return code, json.loads(stream.getvalue())

    def test_profiles_create_list_get_update_delete_json(self) -> None:
        reference = write_pcm_wav(self.base / "giọng mẫu.wav")
        create_request = self.base / "create.json"
        create_request.write_text(
            json.dumps(
                {
                    "profile_id": "preset_01",
                    "display_name": "Nữ nhẹ nhàng 01",
                    "profile_type": "preset",
                    "source_type": "audio",
                    "default_language": "en",
                    "engine_preference": "auto",
                    "reference_files": [str(reference)],
                    "consent": {"confirmed": True},
                    "is_base_voice_preset": True,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        code, created = self.invoke(["profiles", "create", "--request", str(create_request), "--json"])
        self.assertEqual(0, code)
        self.assertEqual("preset_01", created["profile_id"])
        code, listed = self.invoke(["profiles", "list", "--json"])
        self.assertEqual(["preset_01"], [item["profile_id"] for item in listed["profiles"]])
        code, loaded = self.invoke(["profiles", "get", "--profile-id", "preset_01", "--json"])
        self.assertEqual("Nữ nhẹ nhàng 01", loaded["display_name"])
        update_request = self.base / "update.json"
        update_request.write_text(
            json.dumps({"profile_id": "preset_01", "display_name": "Tên đã đổi"}, ensure_ascii=False),
            encoding="utf-8",
        )
        code, updated = self.invoke(["profiles", "update", "--request", str(update_request), "--json"])
        self.assertEqual("Tên đã đổi", updated["display_name"])
        code, deleted = self.invoke(["profiles", "delete", "--profile-id", "preset_01", "--json"])
        self.assertEqual("deleted", deleted["status"])


if __name__ == "__main__":
    unittest.main()
