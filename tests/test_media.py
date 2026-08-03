from __future__ import annotations

import tempfile
import unittest
from array import array
from pathlib import Path
from unittest.mock import patch

from voice_dubbing_runtime.errors import (
    INVALID_REFERENCE,
    REFERENCE_DURATION_INVALID,
    VoiceRuntimeError,
)
from voice_dubbing_runtime.media import (
    choose_reference_auto,
    cut_reference,
    inspect_pcm_wav,
    validate_measured_duration,
    validate_reference_duration,
)

from .helpers import write_pcm_wav


class ReferenceSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_auto_selects_six_to_twelve_second_window(self) -> None:
        source = self.base / "Unicode 音声.wav"
        rate = 100
        samples = array("h", [4000 if index % 20 < 10 else -4000 for index in range(20 * rate)])
        with patch(
            "voice_dubbing_runtime.media._load_samples",
            return_value=(samples, rate),
        ):
            start, end, evidence = choose_reference_auto(source)
        self.assertGreaterEqual(end - start, 6.0)
        self.assertLessEqual(end - start, 12.0)
        self.assertIn("clipping_ratio", evidence)

    def test_manual_cut_is_pcm_mono_24k_and_non_overwrite(self) -> None:
        source = write_pcm_wav(self.base / "source.wav", seconds=12.0)
        output = self.base / "preview.wav"
        report = cut_reference(source, output, 1.0, 9.0)
        self.assertAlmostEqual(8.0, report["duration_seconds"], places=3)
        self.assertEqual(24000, inspect_pcm_wav(output)["sample_rate"])
        with self.assertRaises(FileExistsError):
            cut_reference(source, output, 1.0, 9.0)

    def test_too_short_reference_is_rejected(self) -> None:
        source = write_pcm_wav(self.base / "short.wav", seconds=5.0)
        with self.assertRaises(VoiceRuntimeError) as context:
            choose_reference_auto(source)
        self.assertEqual(INVALID_REFERENCE, context.exception.code)

    def test_manual_duration_boundaries_use_inclusive_millisecond_rounding(self) -> None:
        self.assertEqual(8000, validate_reference_duration(0.0, 8.0))
        self.assertEqual(15000, validate_reference_duration(0.0, 15.0))
        for duration in (7.999, 15.001):
            with self.subTest(duration=duration), self.assertRaises(VoiceRuntimeError) as context:
                validate_reference_duration(0.0, duration)
            self.assertEqual(REFERENCE_DURATION_INVALID, context.exception.code)
            self.assertIn(f"{duration:.3f} giây", context.exception.message)

    def test_measured_duration_allows_at_most_one_audio_frame_of_timebase_drift(self) -> None:
        validate_measured_duration(15.0 + (1.0 / 24000), sample_rate=24000)
        with self.assertRaises(VoiceRuntimeError) as context:
            validate_measured_duration(15.0 + (2.0 / 24000), sample_rate=24000)
        self.assertEqual(REFERENCE_DURATION_INVALID, context.exception.code)

    def test_auto_selector_never_crosses_target_speaker_window(self) -> None:
        source = self.base / "speaker_a_then_speaker_b.wav"
        rate = 100
        samples = array("h")
        # Speaker A occupies 0–50 s; the louder, technically more attractive
        # speaker B starts after 50 s.  The target boundary, not energy, must win.
        for second in range(70):
            amplitude = 3500 if second < 50 else 15000
            samples.extend(
                [amplitude if index % 20 < 10 else -amplitude for index in range(rate)]
            )
        with patch(
            "voice_dubbing_runtime.media._load_samples",
            return_value=(samples, rate),
        ):
            start, end, evidence = choose_reference_auto(
                source, window_start_seconds=0.0, window_end_seconds=50.0
            )
        self.assertGreaterEqual(start, 0.0)
        self.assertLessEqual(end, 50.0)
        self.assertEqual(
            {"start_seconds": 0.0, "end_seconds": 50.0},
            evidence["target_speaker_window"],
        )
        for candidate_start, candidate_end in ((0.0, 15.0), (15.0, 30.0), (30.0, 45.0)):
            self.assertGreaterEqual(candidate_start, 0.0)
            self.assertLessEqual(candidate_end, 50.0)


if __name__ == "__main__":
    unittest.main()
