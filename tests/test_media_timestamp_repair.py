from __future__ import annotations

import subprocess
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from voice_dubbing_runtime.media import normalize_source


class MediaTimestampRepairTests(unittest.TestCase):
    def test_normalize_source_rebuilds_monotonic_audio_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "downloaded.mp4"
            source.write_bytes(b"fixture")
            output = root / "analysis.wav"
            captured: list[str] = []

            def fake_run(command, _cancel_token):
                captured.extend(command)
                temporary = Path(command[-1])
                with wave.open(str(temporary), "wb") as writer:
                    writer.setnchannels(1)
                    writer.setsampwidth(2)
                    writer.setframerate(24000)
                    writer.writeframes(b"\x00\x00" * 24000)
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch("voice_dubbing_runtime.media._run_ffmpeg", side_effect=fake_run):
                metadata = normalize_source(
                    source,
                    "video",
                    output,
                    root / "ffmpeg.exe",
                    cancel_token=None,
                )

            self.assertEqual(1.0, metadata["duration_seconds"])
            self.assertIn("-fflags", captured)
            self.assertEqual("+genpts", captured[captured.index("-fflags") + 1])
            self.assertIn("-af", captured)
            self.assertEqual(
                "asetpts=N/SR/TB,aresample=24000:async=1:first_pts=0",
                captured[captured.index("-af") + 1],
            )


if __name__ == "__main__":
    unittest.main()
