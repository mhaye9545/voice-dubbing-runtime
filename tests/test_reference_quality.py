from __future__ import annotations

import math
import struct
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from voice_dubbing_runtime.errors import (
    BACKGROUND_AUDIO_DETECTED,
    INVALID_REFERENCE,
    SOURCE_SEPARATION_NO_EFFECT,
    VoiceRuntimeError,
)
from voice_dubbing_runtime.reference_quality import validate_voice_only_reference


def write_pattern(path: Path, *, seconds: float = 10.0, background: float = 0.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    rate = 24000
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        frames = bytearray()
        for index in range(int(seconds * rate)):
            cycle = (index / rate) % 1.0
            speech = 0.20 * math.sin(2 * math.pi * 220 * index / rate) if cycle < 0.65 else 0.0
            bed = background * math.sin(2 * math.pi * 90 * index / rate)
            value = max(-0.99, min(0.99, speech + bed))
            frames.extend(struct.pack("<h", int(32767 * value)))
        writer.writeframes(frames)
    return path


class VoiceOnlyQualityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_clean_voice_with_quiet_pauses_passes(self) -> None:
        mix = write_pattern(self.base / "mix.wav", background=0.04)
        voice = write_pattern(self.base / "voice.wav", background=0.0)
        with patch("voice_dubbing_runtime.reference_quality.strict_ffmpeg_decode"):
            result = validate_voice_only_reference(voice, mix, self.base / "ffmpeg.exe")
        self.assertEqual("PASS", result["status"])
        self.assertEqual(1.0, result["voice_only"]["finite_sample_ratio"])
        self.assertGreater(result["voice_only"]["speech_ratio"], 0.35)
        self.assertGreater(result["mix_voice_comparison"]["difference_rms"], 0.0)
        self.assertEqual(
            "TECHNICAL_PROXY_PASS_PENDING_LISTENING",
            result["background_gate"]["status"],
        )

    def test_continuous_background_bed_is_rejected(self) -> None:
        mix = write_pattern(self.base / "mix.wav", background=0.08)
        voice = write_pattern(self.base / "voice.wav", background=0.05)
        with patch("voice_dubbing_runtime.reference_quality.strict_ffmpeg_decode"):
            result = validate_voice_only_reference(voice, mix, self.base / "ffmpeg.exe")
        self.assertEqual(BACKGROUND_AUDIO_DETECTED, result["status"])
        self.assertIn("noise_floor_too_high", result["background_gate"]["failures"])

    def test_reported_background_rejects_noop_separator(self) -> None:
        mix = write_pattern(self.base / "reported_mix.wav", background=0.01)
        voice = write_pattern(self.base / "unchanged_voice.wav", background=0.01)
        with patch("voice_dubbing_runtime.reference_quality.strict_ffmpeg_decode"):
            result = validate_voice_only_reference(
                voice,
                mix,
                self.base / "ffmpeg.exe",
                background_was_reported=True,
            )
        self.assertEqual(SOURCE_SEPARATION_NO_EFFECT, result["status"])
        self.assertIn(
            "no_material_separation_delta",
            result["background_gate"]["failures"],
        )

    def test_gain_scaled_copy_is_rejected_as_no_effect(self) -> None:
        mix = write_pattern(self.base / "scaled_mix.wav", background=0.02)
        scaled = self.base / "scaled_voice.wav"
        with wave.open(str(mix), "rb") as reader:
            parameters = reader.getparams()
            values = struct.unpack(
                f"<{reader.getnframes()}h", reader.readframes(reader.getnframes())
            )
        with wave.open(str(scaled), "wb") as writer:
            writer.setparams(parameters)
            writer.writeframes(
                struct.pack(f"<{len(values)}h", *(round(value * 0.707) for value in values))
            )
        with patch("voice_dubbing_runtime.reference_quality.strict_ffmpeg_decode"):
            result = validate_voice_only_reference(scaled, mix, self.base / "ffmpeg.exe")
        self.assertEqual(SOURCE_SEPARATION_NO_EFFECT, result["status"])
        effectiveness = result["separation_effectiveness"]
        self.assertTrue(effectiveness["scaled_copy"])
        self.assertGreater(effectiveness["correlation"], 0.995)
        self.assertLess(effectiveness["gain_fitted_residual_ratio"], 0.15)
        self.assertAlmostEqual(0.707, effectiveness["best_fit_gain"], places=3)

    def test_duration_outside_eight_to_fifteen_seconds_fails(self) -> None:
        mix = write_pattern(self.base / "mix.wav", seconds=7.9)
        voice = write_pattern(self.base / "voice.wav", seconds=7.9)
        with patch("voice_dubbing_runtime.reference_quality.strict_ffmpeg_decode"):
            with self.assertRaises(VoiceRuntimeError) as context:
                validate_voice_only_reference(voice, mix, self.base / "ffmpeg.exe")
        self.assertEqual(INVALID_REFERENCE, context.exception.code)

    def test_voice_only_duration_must_match_selected_mix(self) -> None:
        mix = write_pattern(self.base / "mix_10s.wav", seconds=10.0, background=0.03)
        voice = write_pattern(self.base / "voice_8s.wav", seconds=8.0, background=0.0)
        with patch("voice_dubbing_runtime.reference_quality.strict_ffmpeg_decode"):
            with self.assertRaises(VoiceRuntimeError) as context:
                validate_voice_only_reference(voice, mix, self.base / "ffmpeg.exe")
        self.assertEqual(INVALID_REFERENCE, context.exception.code)
        self.assertIn("duration", context.exception.message.lower())


if __name__ == "__main__":
    unittest.main()
