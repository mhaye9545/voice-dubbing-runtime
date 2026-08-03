"""Audit Demucs raw stems for the locked Lester Holt candidates.

Run only in the pinned ``.venv-source-separation`` environment.  The script
writes lossless debug evidence; it does not touch voice profiles or TTS.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

import numpy as np
import psutil
import sphn

from demucs.api import Separator, save_audio
from voice_dubbing_runtime.source_separation import MODEL_NAME, verify_model_manifest
from voice_dubbing_runtime.source_separation_worker import _verify_runtime_packages


LOCKED = {
    2: (13.074667, 27.737854),
    3: (35.712500, 49.371500),
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _write_json_exclusive(path: Path, value: dict[str, Any]) -> None:
    encoded = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    with path.open("xb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())


def _copy_durable(source: Path, target: Path) -> None:
    with source.open("rb") as incoming, target.open("xb") as outgoing:
        shutil.copyfileobj(incoming, outgoing, 1024 * 1024)
        outgoing.flush()
        os.fsync(outgoing.fileno())
    if _sha256(source) != _sha256(target):
        raise RuntimeError(f"Durable copy hash mismatch: {target}")


def _load_mono(
    path: Path, sample_rate: int | None = None
) -> tuple[np.ndarray, int, int]:
    audio, rate = sphn.read(str(path), sample_rate=sample_rate)
    array = np.asarray(audio, dtype=np.float64)
    if array.ndim == 1:
        array = array[None, :]
    channels = int(array.shape[0])
    return array.mean(axis=0), int(rate), channels


def _audio_metrics(path: Path) -> dict[str, Any]:
    samples, rate, channels = _load_mono(path)
    if samples.size == 0 or not np.isfinite(samples).all():
        raise RuntimeError(f"Invalid/non-finite debug WAV: {path}")
    peak = float(np.max(np.abs(samples)))
    rms = float(np.sqrt(np.mean(np.square(samples))))
    return {
        "path": str(path),
        "size_bytes": path.stat().st_size,
        "sha256": _sha256(path),
        "duration_seconds": float(samples.size / rate),
        "sample_rate": rate,
        "channels_in_file": channels,
        "peak": peak,
        "rms": rms,
        "clipping_ratio": float(np.mean(np.abs(samples) >= 0.999)),
        "finite_sample_ratio": 1.0,
    }


def _comparison(input_path: Path, candidate_path: Path) -> dict[str, Any]:
    mixed, rate_a, _ = _load_mono(input_path, sample_rate=24000)
    output, rate_b, _ = _load_mono(candidate_path, sample_rate=24000)
    if rate_a != rate_b:
        raise RuntimeError("Debug comparison sample-rate mismatch")
    count = min(mixed.size, output.size)
    mixed = mixed[:count]
    output = output[:count]
    dot = float(np.dot(mixed, output))
    mix_square = float(np.dot(mixed, mixed))
    out_square = float(np.dot(output, output))
    gain = dot / max(mix_square, 1e-18)
    residual = output - gain * mixed
    residual_rms = float(np.sqrt(np.mean(np.square(residual))))
    output_rms = float(np.sqrt(np.mean(np.square(output))))
    correlation = dot / math.sqrt(max(mix_square * out_square, 1e-18))
    residual_ratio = residual_rms / max(output_rms, 1e-12)
    scaled_copy = abs(correlation) > 0.995 and residual_ratio < 0.15
    return {
        "aligned_sample_count": count,
        "correlation": correlation,
        "best_fit_gain": gain,
        "best_fit_gain_db": 20.0 * math.log10(max(abs(gain), 1e-12)),
        "gain_fitted_residual_rms": residual_rms,
        "gain_fitted_residual_ratio": residual_ratio,
        "scaled_copy_thresholds": {
            "minimum_absolute_correlation": 0.995,
            "maximum_gain_fitted_residual_ratio": 0.15,
        },
        "scaled_copy": scaled_copy,
        "gate": "SOURCE_SEPARATION_NO_EFFECT" if scaled_copy else "PASS",
    }


def _children_remaining() -> list[dict[str, Any]]:
    root = psutil.Process(os.getpid())
    return [
        {"pid": child.pid, "name": child.name()}
        for child in root.children(recursive=True)
        if child.is_running()
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--candidate-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--ffmpeg", type=Path, required=True)
    args = parser.parse_args()
    runtime_root = args.runtime_root.resolve()
    candidate_root = args.candidate_root.resolve()
    output_root = args.output_root.resolve()
    ffmpeg = args.ffmpeg.resolve()
    if not ffmpeg.is_file():
        raise FileNotFoundError(ffmpeg)
    output_root.mkdir(parents=True, exist_ok=True)
    staging_root = output_root / ".staging"
    staging_root.mkdir(parents=True, exist_ok=True)

    manifest = verify_model_manifest(
        runtime_root / "models" / "source_separation" / "htdemucs"
    )
    packages = _verify_runtime_packages()
    load_started = time.perf_counter()
    separator = Separator(
        model=MODEL_NAME,
        repo=runtime_root / "models" / "source_separation" / "htdemucs",
        device="cpu",
        shifts=0,
        split=True,
        overlap=0.25,
        progress=False,
        jobs=0,
    )
    model_load_elapsed = time.perf_counter() - load_started
    if "vocals" not in separator._model.sources:
        raise RuntimeError("SOURCE_SEPARATION_VOCALS_STEM_MISSING")

    reports: list[dict[str, Any]] = []
    for number, (start, end) in LOCKED.items():
        final_dir = output_root / f"debug_candidate_{number:02d}"
        if final_dir.exists():
            raise FileExistsError(final_dir)
        staging = staging_root / uuid.uuid4().hex
        staging.mkdir(parents=False, exist_ok=False)
        input_source = candidate_root / f"ref_source_mix_candidate_{number:02d}.wav"
        if not input_source.is_file():
            raise FileNotFoundError(input_source)
        input_mix = staging / "input_mix.wav"
        vocals_raw = staging / "vocals_raw.wav"
        no_vocals_raw = staging / "no_vocals_raw.wav"
        vocals_normalized = staging / "vocals_normalized.wav"
        final_voice_only = staging / "final_voice_only.wav"

        completed = subprocess.run(
            [
                str(ffmpeg),
                "-hide_banner",
                "-nostdin",
                "-v",
                "error",
                "-xerror",
                "-i",
                str(input_source),
                "-map",
                "0:a:0",
                "-vn",
                "-ac",
                "2",
                "-ar",
                str(separator.samplerate),
                "-c:a",
                "pcm_s16le",
                str(input_mix),
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode != 0 or not input_mix.is_file():
            raise RuntimeError(
                f"FFmpeg debug input preparation failed: {completed.stderr[-1000:]}"
            )

        separation_started = time.perf_counter()
        original, stems = separator.separate_audio_file(input_mix)
        separation_elapsed = time.perf_counter() - separation_started
        if not isinstance(stems, dict) or "vocals" not in stems:
            raise RuntimeError("SOURCE_SEPARATION_VOCALS_STEM_MISSING")
        non_vocal_names = [name for name in separator._model.sources if name != "vocals"]
        if not non_vocal_names or any(name not in stems for name in non_vocal_names):
            raise RuntimeError("SOURCE_SEPARATION_NON_VOCALS_STEM_MISSING")
        no_vocals = sum(stems[name] for name in non_vocal_names)
        save_audio(
            stems["vocals"],
            vocals_raw,
            samplerate=separator.samplerate,
            clip="none",
            bits_per_sample=32,
            as_float=True,
        )
        save_audio(
            no_vocals,
            no_vocals_raw,
            samplerate=separator.samplerate,
            clip="none",
            bits_per_sample=32,
            as_float=True,
        )
        save_audio(
            stems["vocals"],
            vocals_normalized,
            samplerate=separator.samplerate,
            clip="rescale",
            bits_per_sample=16,
            as_float=False,
        )
        completed = subprocess.run(
            [
                str(ffmpeg),
                "-hide_banner",
                "-nostdin",
                "-v",
                "error",
                "-xerror",
                "-i",
                str(vocals_normalized),
                "-map",
                "0:a:0",
                "-vn",
                "-ac",
                "1",
                "-ar",
                "24000",
                "-c:a",
                "pcm_s16le",
                str(final_voice_only),
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode != 0 or not final_voice_only.is_file():
            raise RuntimeError(
                f"FFmpeg final voice-only formatting failed: {completed.stderr[-1000:]}"
            )
        for path in (input_mix, vocals_raw, no_vocals_raw, vocals_normalized, final_voice_only):
            with path.open("r+b") as handle:
                handle.flush()
                os.fsync(handle.fileno())

        reconstruction = original.detach().cpu().numpy()
        reconstructed_stems = sum(stems.values()).detach().cpu().numpy()
        count = min(reconstruction.shape[-1], reconstructed_stems.shape[-1])
        reconstruction_error = float(
            np.sqrt(
                np.mean(
                    np.square(
                        reconstruction[..., :count] - reconstructed_stems[..., :count]
                    )
                )
            )
        )
        os.rename(staging, final_dir)
        final_input_mix = final_dir / "input_mix.wav"
        final_vocals_raw = final_dir / "vocals_raw.wav"
        final_no_vocals_raw = final_dir / "no_vocals_raw.wav"
        final_vocals_normalized = final_dir / "vocals_normalized.wav"
        final_voice = final_dir / "final_voice_only.wav"
        files = {
            path.name: _audio_metrics(path)
            for path in (
                final_input_mix,
                final_vocals_raw,
                final_no_vocals_raw,
                final_vocals_normalized,
                final_voice,
            )
        }
        comparisons = {
            "input_vs_vocals_raw": _comparison(final_input_mix, final_vocals_raw),
            "input_vs_no_vocals_raw": _comparison(final_input_mix, final_no_vocals_raw),
            # The candidate source and final voice-only are both canonical
            # mono 24 kHz streams. Comparing those avoids unrelated group delay
            # from two different 44.1 -> 24 kHz resamplers.
            "input_vs_final_voice_only": _comparison(input_source, final_voice),
        }
        report = {
            "schema_version": 1,
            "candidate": number,
            "start_seconds": start,
            "end_seconds": end,
            "duration_seconds": end - start,
            "input_source": str(input_source),
            "model_id": manifest["model_id"],
            "model_name": MODEL_NAME,
            "model_sources": list(separator._model.sources),
            "selected_stem": "vocals",
            "non_vocal_stems": non_vocal_names,
            "correct_vocals_stem": True,
            "input_reused_as_vocals": False,
            "no_vocals_used_as_vocals": False,
            "package_versions": packages,
            "model_load_elapsed_seconds": model_load_elapsed,
            "separation_elapsed_seconds": separation_elapsed,
            "files": files,
            "comparisons": comparisons,
            "stem_sum_reconstruction_rms": reconstruction_error,
            "scaled_copy_gate": comparisons["input_vs_final_voice_only"]["gate"],
        }
        _write_json_exclusive(final_dir / "debug_metrics.json", report)
        reports.append(report)
        del stems, no_vocals, original

    final_report = {
        "schema_version": 1,
        "status": "Pass",
        "model_load_elapsed_seconds": model_load_elapsed,
        "candidates": reports,
        "processes_remaining": _children_remaining(),
    }
    _write_json_exclusive(output_root / "source_separation_debug_report.json", final_report)
    try:
        staging_root.rmdir()
    except OSError:
        pass
    print(json.dumps(final_report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
