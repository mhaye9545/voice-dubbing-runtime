from __future__ import annotations

import argparse
import gc
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any


RUNTIME_ROOT = Path(__file__).resolve().parent
TTS_REVISION = "ff217b3f27b294de194cc59c5119d1e08b06413c"
MODEL_REVISION = "c06f4378883110615941aab481532a9802440b05"
MODEL_SHA256 = "534670e4b752002b7d7224e6ea1f467bd608c8dd3c36efaa45e1f4696e8bd1d2"
ROUND1_REPORT_SHA256 = "287850330252a0da387c0ddc8130bcffd32bd3b2e1e8b715f1e09f7436ecc582"
VENDOR_ROOT = RUNTIME_ROOT / "vendor" / f"TTS-{TTS_REVISION}"
TEST_TEXT = (
    "Xin ch\u00e0o, \u0111\u00e2y l\u00e0 \u0111o\u1ea1n \u00e2m thanh th\u1eed nghi\u1ec7m \u0111\u01b0\u1ee3c t\u1ea1o t\u1eeb "
    "gi\u1ecdng tham chi\u1ebfu trong video."
)
PARAMETERS: dict[str, Any] = {
    "temperature": 0.30,
    "length_penalty": 1.0,
    "repetition_penalty": 10.0,
    "top_k": 30,
    "top_p": 0.85,
    "do_sample": True,
    "enable_text_splitting": True,
    "gpt_cond_len": 12,
    "gpt_cond_chunk_len": 4,
    "max_ref_len": 10,
    "sound_norm_refs": False,
    "seed": 42,
}
OUTPUT_NAMES = (
    "generated_primary_clean",
    "generated_primary_plus_candidate2_clean",
)
MARKER_PREFIX = "@@VOICE_DUB_REFERENCE_QUALITY|"


def emit_marker(payload: dict[str, Any]) -> None:
    print(MARKER_PREFIX + json.dumps(payload, ensure_ascii=True, sort_keys=True), flush=True)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def directory_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return total
    for candidate in path.rglob("*"):
        if candidate.is_file():
            try:
                total += candidate.stat().st_size
            except OSError:
                pass
    return total


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


class PeakRssMonitor:
    def __init__(self) -> None:
        import psutil

        self._psutil = psutil
        self._process = psutil.Process(os.getpid())
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="peak-rss", daemon=True)
        self.peak_bytes = 0

    def _sample(self) -> None:
        processes = [self._process]
        try:
            processes.extend(self._process.children(recursive=True))
        except (self._psutil.NoSuchProcess, self._psutil.AccessDenied):
            pass
        total = 0
        for process in processes:
            try:
                total += process.memory_info().rss
            except (self._psutil.NoSuchProcess, self._psutil.AccessDenied):
                pass
        self.peak_bytes = max(self.peak_bytes, total)

    def _run(self) -> None:
        while not self._stop.wait(0.1):
            self._sample()
        self._sample()

    def __enter__(self) -> "PeakRssMonitor":
        self._sample()
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)


def live_child_process_count() -> int:
    import psutil

    count = 0
    process = psutil.Process(os.getpid())
    try:
        for child in process.children(recursive=True):
            try:
                if child.is_running() and child.status() != psutil.STATUS_ZOMBIE:
                    count += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass
    return count


def strict_ffmpeg_decode(path: Path, ffmpeg: Path) -> None:
    completed = subprocess.run(
        [
            str(ffmpeg),
            "-hide_banner",
            "-nostdin",
            "-v",
            "error",
            "-xerror",
            "-err_detect",
            "explode",
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-f",
            "null",
            "NUL" if os.name == "nt" else "/dev/null",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    if completed.returncode != 0:
        raise ValueError(f"FFmpeg decode failed ({completed.returncode}): {completed.stderr.strip()}")


def measure_loudness(path: Path, ffmpeg: Path, target_i: float = -23.0) -> dict[str, float]:
    completed = subprocess.run(
        [
            str(ffmpeg),
            "-hide_banner",
            "-nostdin",
            "-i",
            str(path),
            "-af",
            f"loudnorm=I={target_i:.2f}:TP=-2.00:LRA=7.00:print_format=json",
            "-f",
            "null",
            "NUL" if os.name == "nt" else "/dev/null",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"Loudness measurement failed for {path}: {completed.stderr.strip()}")
    matches = re.findall(r'\{\s*"input_i"[\s\S]*?\}', completed.stderr)
    if not matches:
        raise RuntimeError(f"No loudnorm measurement JSON for {path}")
    payload = json.loads(matches[-1])
    result = {
        "integrated_lufs": float(payload["input_i"]),
        "true_peak_dbtp": float(payload["input_tp"]),
        "loudness_range_lu": float(payload["input_lra"]),
        "threshold_lufs": float(payload["input_thresh"]),
    }
    if not all(math.isfinite(value) for value in result.values()):
        raise ValueError(f"Non-finite loudness result for {path}: {result}")
    return result


def analyze_signal(path: Path, ffmpeg: Path, *, require_reference_duration: bool = False) -> dict[str, Any]:
    import torch
    import torchaudio

    if not path.is_file() or path.stat().st_size <= 0:
        raise FileNotFoundError(f"Missing or empty WAV: {path}")
    waveform, sample_rate = torchaudio.load(str(path))
    metadata = torchaudio.info(str(path))
    if waveform.numel() == 0 or sample_rate <= 0:
        raise ValueError(f"No decodable samples: {path}")
    if not torch.isfinite(waveform).all():
        raise ValueError(f"NaN or Inf samples: {path}")
    duration = waveform.shape[-1] / sample_rate
    if duration <= 0:
        raise ValueError(f"Non-positive duration: {path}")
    if require_reference_duration and not 8.0 <= duration <= 15.0:
        raise ValueError(f"Reference must be 8-15 seconds, got {duration:.6f}: {path}")

    absolute = waveform.abs()
    peak = float(absolute.max().item())
    rms = float(torch.sqrt(torch.mean(waveform * waveform)).item())
    clipping_ratio = float((absolute >= 0.999).float().mean().item())
    silence_ratio = float((absolute < 0.001).float().mean().item())
    if peak <= 0.0001 or rms <= 0.00001 or silence_ratio >= 0.999:
        raise ValueError(f"Effectively silent WAV: {path}")
    if clipping_ratio > 0.0:
        raise ValueError(f"Clipping detected: {path}")
    strict_ffmpeg_decode(path, ffmpeg)

    mono = waveform.mean(dim=0)
    pitch_frame_time = 0.01
    pitch_win_length = 30
    pitch_activity_threshold_dbfs = -45.0
    pitch_activity_min_fraction = 0.50
    pitch = torchaudio.functional.detect_pitch_frequency(
        mono.unsqueeze(0),
        sample_rate,
        frame_time=pitch_frame_time,
        win_length=pitch_win_length,
        freq_low=140,
        freq_high=400,
    )[0]

    # detect_pitch_frequency returns an in-range value for every retained frame,
    # including silence/noise. Mirror its 10 ms framing and 30-frame median
    # window, then keep pitch only where at least half of that window is active.
    pitch_frame_size = math.ceil(sample_rate * pitch_frame_time)
    pitch_padding = (-mono.numel()) % pitch_frame_size
    pitch_audio = torch.nn.functional.pad(mono, (0, pitch_padding)) if pitch_padding else mono
    pitch_frame_rms = torch.sqrt(
        torch.mean(pitch_audio.unfold(0, pitch_frame_size, pitch_frame_size).square(), dim=1)
    )
    pitch_frame_active = pitch_frame_rms > 10 ** (pitch_activity_threshold_dbfs / 20)
    pitch_left_pad = (pitch_win_length - 1) // 2
    pitch_activity_padded = torch.cat(
        (pitch_frame_active[:1].repeat(pitch_left_pad), pitch_frame_active)
    )
    pitch_activity_fraction = (
        pitch_activity_padded.unfold(0, pitch_win_length, 1).float().mean(dim=1)
    )
    aligned_pitch_count = min(pitch.numel(), pitch_activity_fraction.numel())
    pitch = pitch[:aligned_pitch_count]
    pitch_activity_fraction = pitch_activity_fraction[:aligned_pitch_count]
    pitch_activity_mask = pitch_activity_fraction >= pitch_activity_min_fraction
    valid_pitch_mask = (
        torch.isfinite(pitch)
        & (pitch >= 140)
        & (pitch <= 400)
        & pitch_activity_mask
    )
    valid_pitch = pitch[valid_pitch_mask]
    if valid_pitch.numel() < 10:
        raise ValueError(f"Too few valid pitch frames: {path}")
    f0_p10 = float(torch.quantile(valid_pitch, 0.10).item())
    f0_p90 = float(torch.quantile(valid_pitch, 0.90).item())

    n_fft = 2048
    hop_length = 512
    window = torch.hann_window(n_fft, dtype=mono.dtype, device=mono.device)
    spectrum = torch.stft(
        mono,
        n_fft=n_fft,
        hop_length=hop_length,
        win_length=n_fft,
        window=window,
        center=True,
        return_complex=True,
    ).abs()
    frequencies = torch.linspace(0, sample_rate / 2, spectrum.shape[0], device=mono.device)
    centroids = (spectrum * frequencies[:, None]).sum(dim=0) / spectrum.sum(dim=0).clamp_min(1e-12)
    padded = torch.nn.functional.pad(
        mono.unsqueeze(0).unsqueeze(0),
        (n_fft // 2, n_fft // 2),
        mode="reflect",
    ).reshape(-1)
    frames = padded.unfold(0, n_fft, hop_length)
    frame_rms = torch.sqrt(torch.mean(frames * frames, dim=1)).clamp_min(1e-12)
    frame_count = min(centroids.numel(), frame_rms.numel())
    centroids = centroids[:frame_count]
    frame_rms = frame_rms[:frame_count]
    active = frame_rms > 10 ** (-45 / 20)
    active_centroids = centroids[active] if torch.any(active) else centroids
    active_db = 20.0 * torch.log10(frame_rms[active] if torch.any(active) else frame_rms)

    full_spectrum = torch.fft.rfft(mono)
    full_power = full_spectrum.abs().square()
    full_frequencies = torch.fft.rfftfreq(mono.numel(), d=1.0 / sample_rate)

    def band_ratio(low: float, high: float) -> float:
        mask = (full_frequencies >= low) & (full_frequencies < high)
        return float((full_power[mask].sum() / full_power.sum().clamp_min(1e-12)).item())

    return {
        "path": str(path),
        "size_bytes": path.stat().st_size,
        "sha256": sha256_file(path),
        "codec_expected": "pcm_s16le",
        "torchaudio_encoding": metadata.encoding,
        "bits_per_sample": metadata.bits_per_sample,
        "sample_rate": sample_rate,
        "channels": waveform.shape[0],
        "sample_count_per_channel": waveform.shape[-1],
        "duration_seconds": duration,
        "peak": peak,
        "peak_dbfs": 20.0 * math.log10(max(peak, 1e-12)),
        "rms": rms,
        "rms_dbfs": 20.0 * math.log10(max(rms, 1e-12)),
        "dc_dbfs": 20.0 * math.log10(max(abs(float(mono.mean().item())), 1e-12)),
        "silence_ratio_below_minus_60_dbfs": silence_ratio,
        "clipping_ratio": clipping_ratio,
        "f0_median_hz": float(torch.median(valid_pitch).item()),
        "f0_p10_hz": f0_p10,
        "f0_p90_hz": f0_p90,
        "f0_p10_p90_span_semitones": 12.0 * math.log2(f0_p90 / f0_p10),
        "f0_metric_scope": "active-frame pitch proxy",
        "pitch_total_aligned_frames": aligned_pitch_count,
        "pitch_active_valid_frames": valid_pitch.numel(),
        "pitch_activity_frame_time_seconds": pitch_frame_time,
        "pitch_activity_threshold_dbfs": pitch_activity_threshold_dbfs,
        "pitch_activity_min_window_fraction": pitch_activity_min_fraction,
        "spectral_centroid_active_median_hz": float(torch.median(active_centroids).item()),
        "active_rms_range_p05_p95_db": float(
            (torch.quantile(active_db, 0.95) - torch.quantile(active_db, 0.05)).item()
        ),
        "rumble_20_80_energy_ratio": band_ratio(20, 80),
        "sub_50_energy_ratio": band_ratio(0, 50),
        "ffmpeg_decode": "Pass",
        "finite_samples": "Pass",
        "non_silent": "Pass",
    }


def render_static_gain(raw_path: Path, output_path: Path, ffmpeg: Path, gain_db: float) -> float:
    if output_path.exists():
        raise FileExistsError(f"Refusing to overwrite final output: {output_path}")
    started = time.perf_counter()
    completed = subprocess.run(
        [
            str(ffmpeg),
            "-hide_banner",
            "-nostdin",
            "-v",
            "error",
            "-xerror",
            "-i",
            str(raw_path),
            "-map",
            "0:a:0",
            "-af",
            f"volume={gain_db:.6f}dB:precision=double",
            "-c:a",
            "pcm_s16le",
            "-ar",
            "24000",
            "-ac",
            "1",
            str(output_path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        check=False,
    )
    elapsed = time.perf_counter() - started
    if completed.returncode != 0:
        raise RuntimeError(f"Static gain render failed for {raw_path}: {completed.stderr.strip()}")
    return elapsed


def synthesize_once(
    model: Any,
    config: Any,
    torch: Any,
    torchaudio: Any,
    references: list[Path],
    output: Path,
    tracker: dict[str, Any],
) -> tuple[float, int]:
    if tracker["synthesis_calls_attempted"] >= 2:
        raise RuntimeError("Hard guard blocked a third synthesis call")
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite raw output: {output}")
    tracker["synthesis_calls_attempted"] += 1
    tracker["stage"] = f"synthesis_call_{tracker['synthesis_calls_attempted']}"
    config_owned = {"gpt_cond_len", "gpt_cond_chunk_len", "max_ref_len", "sound_norm_refs"}
    call_parameters = {
        key: value for key, value in PARAMETERS.items() if key != "seed" and key not in config_owned
    }
    torch.manual_seed(int(PARAMETERS["seed"]))
    started = time.perf_counter()
    with PeakRssMonitor() as memory:
        with torch.inference_mode():
            synthesized = model.synthesize(
                TEST_TEXT,
                config,
                speaker_wav=[str(path) for path in references],
                language="vi",
                **call_parameters,
            )
        waveform = torch.as_tensor(synthesized["wav"], dtype=torch.float32).unsqueeze(0)
        torchaudio.save(str(output), waveform.cpu(), 24000, encoding="PCM_S", bits_per_sample=16)
    elapsed = time.perf_counter() - started
    tracker["synthesis_calls_completed"] += 1
    del waveform, synthesized
    gc.collect()
    return elapsed, memory.peak_bytes


def write_markdown(path: Path, report: dict[str, Any]) -> None:
    lines = [
        "# viXTTS Reference Quality Round",
        "",
        f"Status: `{report['status']}`",
        "",
        f"Test text: {report['test_text']}",
        "",
        "| File | References | Duration | F0 median/span | Centroid | Peak/RMS | LUFS | Inference | Peak RAM | Decode |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for name in OUTPUT_NAMES:
        item = report["outputs"][name]
        signal = item["final_signal"]
        sources = " + ".join(source["id"] for source in item["reference_sources"])
        lines.append(
            "| {file} | {sources} | {duration:.6f}s | {f0:.2f} Hz / {span:.2f} st | "
            "{centroid:.2f} Hz | {peak:.2f}/{rms:.2f} dBFS | {lufs:.2f} | {elapsed:.3f}s | "
            "{ram:.3f} GiB | {decode} |".format(
                file=item["file"],
                sources=sources,
                duration=signal["duration_seconds"],
                f0=signal["f0_median_hz"],
                span=signal["f0_p10_p90_span_semitones"],
                centroid=signal["spectral_centroid_active_median_hz"],
                peak=signal["peak_dbfs"],
                rms=signal["rms_dbfs"],
                lufs=item["post_normalization_loudness"]["integrated_lufs"],
                elapsed=item["inference_elapsed_seconds"],
                ram=item["peak_rss_gib"],
                decode=signal["ffmpeg_decode"],
            )
        )
    lines.extend(
        [
            "",
            "Both outputs use baseline inference parameters and static gain to -23 LUFS. No limiter, "
            "compression, pitch, or speed processing was used.",
            "",
            "Transcripts are approximate PhoWhisper tool output and are not human verified. Speaker identity, "
            "overlap, music, and reverb retain the limitations recorded in the manifest.",
            "",
            "No GUI integration was performed. User listening approval is not inferred.",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def run(args: argparse.Namespace, tracker: dict[str, Any]) -> int:
    if os.environ.get("VOICE_DUB_AUTHORIZED_USE") != "1":
        raise PermissionError("VOICE_DUB_AUTHORIZED_USE=1 is required")
    tracker["stage"] = "preflight"

    output_dir = args.output_dir.resolve()
    manifest_path = args.manifest.resolve()
    transcript_audit_path = args.transcript_audit.resolve()
    primary_original = args.primary_original.resolve()
    candidate1_original = args.candidate1_original.resolve()
    candidate2_original = args.candidate2_original.resolve()
    primary_clean = args.primary_clean.resolve()
    candidate2_clean = args.candidate2_clean.resolve()
    baseline = args.baseline.resolve()
    round1_report_path = args.round1_report.resolve()
    ffmpeg = args.ffmpeg.resolve()
    model_dir = args.model_dir.resolve()

    expected_output_dir = (RUNTIME_ROOT / "runs" / "reference_quality_round").resolve()
    if output_dir != expected_output_dir:
        raise ValueError(
            "Output containment guard requires exactly "
            f"{expected_output_dir}, got {output_dir}"
        )

    required = [
        manifest_path,
        transcript_audit_path,
        primary_original,
        candidate1_original,
        candidate2_original,
        primary_clean,
        candidate2_clean,
        baseline,
        round1_report_path,
        ffmpeg,
        model_dir / "model.pth",
        model_dir / "config.json",
    ]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"Required files are missing: {missing}")
    if not (VENDOR_ROOT / "TTS" / "tts" / "models" / "xtts.py").is_file():
        raise FileNotFoundError(f"Pinned TTS source is missing: {VENDOR_ROOT}")
    if sha256_file(model_dir / "model.pth").lower() != MODEL_SHA256:
        raise ValueError("Pinned viXTTS model hash mismatch")

    output_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = output_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    report_json_path = output_dir / "reference_quality_report.json"
    report_md_path = output_dir / "reference_quality_report.md"
    failure_path = output_dir / "reference_quality_failure_report.json"
    final_paths = {name: output_dir / f"{name}.wav" for name in OUTPUT_NAMES}
    raw_paths = {name: raw_dir / f"{name}.raw.wav" for name in OUTPUT_NAMES}
    forbidden = [report_json_path, report_md_path, failure_path, *final_paths.values(), *raw_paths.values()]
    existing = [str(path) for path in forbidden if path.exists()]
    existing.extend(str(path) for path in output_dir.glob("generated_*.wav"))
    existing.extend(str(path) for path in raw_dir.iterdir())
    existing = sorted(set(existing))
    if existing:
        raise FileExistsError(f"One-round guard refuses existing artifacts: {existing}")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    transcript_audit = json.loads(transcript_audit_path.read_text(encoding="utf-8"))
    if manifest.get("status") != "REFERENCE_QUALITY_PREFLIGHT_READY_WITH_LIMITATIONS":
        raise ValueError("Reference quality manifest is not ready")
    if manifest.get("listening_decision_before_round") != "USER_LISTENING_REJECTED_TUNING_ROUND_1":
        raise ValueError("Listening rejection gate is missing")
    if manifest.get("test_text") != TEST_TEXT:
        raise ValueError("Manifest test text mismatch")
    if manifest["scope_guards"]["maximum_new_outputs"] != 2:
        raise ValueError("Manifest output limit is not two")
    if manifest["scope_guards"].get("maximum_synthesis_calls") != 2:
        raise ValueError("Manifest synthesis-call limit is not two")
    forbidden_scope_values = {
        "engine_change": False,
        "gui_changes": False,
        "portable_build": False,
        "agent_sdk_installation": False,
        "source_separation": False,
        "vad": False,
        "random_parameter_tuning": False,
        "round_1_artifact_mutation": False,
    }
    for key, expected_value in forbidden_scope_values.items():
        if manifest["scope_guards"].get(key) is not expected_value:
            raise ValueError(f"Manifest scope guard mismatch for {key}")
    if manifest.get("inference_parameters") != PARAMETERS:
        raise ValueError("Manifest inference parameters do not exactly match baseline parameters")
    expected_experiments = [
        {
            "output": "generated_primary_clean.wav",
            "speaker_wav_ids": ["primary"],
        },
        {
            "output": "generated_primary_plus_candidate2_clean.wav",
            "speaker_wav_ids": ["primary", "candidate_2"],
            "order_note": (
                "All 8.546792s of primary plus approximately the first 3.453208s of candidate 2 "
                "enter the 12s GPT conditioning window; full speaker embeddings are averaged."
            ),
        },
    ]
    if manifest.get("experiments") != expected_experiments:
        raise ValueError("Manifest experiment definitions changed")
    if sha256_file(transcript_audit_path).upper() != manifest["content_audit"]["transcript_tool"][
        "report_sha256"
    ]:
        raise ValueError("Transcript audit hash mismatch")
    if transcript_audit.get("status") != "REFERENCE_TRANSCRIPT_TOOL_AUDIT_COMPLETE":
        raise ValueError("Transcript audit did not complete")

    manifest_refs = {item["id"]: item for item in manifest["references"]}
    path_contract = {
        "primary_original": (primary_original, manifest_refs["primary"]["original_sha256"]),
        "primary_clean": (primary_clean, manifest_refs["primary"]["clean_sha256"]),
        "candidate_1_original": (candidate1_original, manifest_refs["candidate_1"]["original_sha256"]),
        "candidate_2_original": (candidate2_original, manifest_refs["candidate_2"]["original_sha256"]),
        "candidate_2_clean": (candidate2_clean, manifest_refs["candidate_2"]["clean_sha256"]),
    }
    for identifier, (path, expected_hash) in path_contract.items():
        actual_hash = sha256_file(path)
        if actual_hash.lower() != expected_hash.lower():
            raise ValueError(f"Reference hash mismatch for {identifier}: {actual_hash}")

    canonical = Path(manifest["canonical_source"]["path"])
    if not canonical.is_file() or sha256_file(canonical).upper() != manifest["canonical_source"]["sha256"]:
        raise ValueError("Canonical source is missing or its hash changed")

    tracker["stage"] = "reference_and_baseline_validation"
    reference_signal = {
        identifier: analyze_signal(path, ffmpeg, require_reference_duration=True)
        for identifier, (path, _expected_hash) in path_contract.items()
    }
    reference_loudness = {
        identifier: measure_loudness(path, ffmpeg) for identifier, (path, _hash) in path_contract.items()
    }
    for original_id, clean_id in (
        ("primary_original", "primary_clean"),
        ("candidate_2_original", "candidate_2_clean"),
    ):
        original = reference_signal[original_id]
        clean = reference_signal[clean_id]
        if clean["torchaudio_encoding"] != "PCM_S" or clean["bits_per_sample"] != 16:
            raise ValueError(f"Cleaned reference is not PCM s16: {clean_id}")
        if clean["sample_rate"] != 24000 or clean["channels"] != 1:
            raise ValueError(f"Cleaned reference format changed: {clean_id}")
        if clean["sample_count_per_channel"] != original["sample_count_per_channel"]:
            raise ValueError(f"Cleaning changed sample count: {clean_id}")
        if abs(clean["duration_seconds"] - original["duration_seconds"]) > (1.0 / 24000):
            raise ValueError(f"Cleaning changed duration: {clean_id}")
    round1_report = json.loads(round1_report_path.read_text(encoding="utf-8"))
    if sha256_file(round1_report_path).lower() != ROUND1_REPORT_SHA256:
        raise ValueError("Immutable Round 1 report hash mismatch")
    if manifest["immutable_round_1"]["report_sha256"].lower() != ROUND1_REPORT_SHA256:
        raise ValueError("Manifest does not pin the expected Round 1 report")
    if round1_report.get("status") != "TUNING_ROUND_1_PENDING_LISTENING":
        raise ValueError("Round 1 report status mismatch")
    if round1_report.get("test_text") != TEST_TEXT:
        raise ValueError("Round 1 test text mismatch")
    if round1_report.get("model", {}).get("revision") != MODEL_REVISION:
        raise ValueError("Round 1 model revision mismatch")
    if round1_report.get("model", {}).get("model_pth_sha256", "").lower() != MODEL_SHA256:
        raise ValueError("Round 1 model hash mismatch")
    baseline_parameters = round1_report["outputs"]["generated_baseline"]["inference_parameters"]
    for key, expected_value in PARAMETERS.items():
        if baseline_parameters.get(key) != expected_value:
            raise ValueError(f"Round 1 baseline parameter mismatch for {key}")
    if baseline_parameters.get("speaker_wav_count") != 1:
        raise ValueError("Round 1 baseline speaker_wav_count mismatch")
    expected_baseline_hash = round1_report["outputs"]["generated_baseline"]["technical_validation"][
        "sha256"
    ]
    if expected_baseline_hash.lower() != manifest["immutable_round_1"]["baseline_sha256"].lower():
        raise ValueError("Manifest baseline hash does not match the immutable Round 1 report")
    if sha256_file(baseline).lower() != expected_baseline_hash.lower():
        raise ValueError("Immutable Round 1 baseline hash mismatch")
    baseline_signal = analyze_signal(baseline, ffmpeg)
    baseline_loudness = measure_loudness(baseline, ffmpeg)
    if abs(baseline_loudness["integrated_lufs"] - (-23.0)) > 0.20:
        raise ValueError(f"Baseline is no longer matched to -23 LUFS: {baseline_loudness}")

    tracker["stage"] = "model_load"
    sys.path.insert(0, str(VENDOR_ROOT))
    import torch
    import torchaudio
    import transformers
    from TTS.tts.configs.xtts_config import XttsConfig
    from TTS.tts.models.xtts import Xtts

    if transformers.__version__ != "4.49.0":
        raise ValueError(f"transformers compatibility pin changed: {transformers.__version__}")
    total_started = time.perf_counter()
    model_started = time.perf_counter()
    with PeakRssMonitor() as model_memory:
        config = XttsConfig()
        config.load_json(str(model_dir / "config.json"))
        actual_conditioning = {
            "gpt_cond_len": config.gpt_cond_len,
            "gpt_cond_chunk_len": config.gpt_cond_chunk_len,
            "max_ref_len": config.max_ref_len,
            "sound_norm_refs": config.sound_norm_refs,
        }
        expected_conditioning = {
            key: PARAMETERS[key]
            for key in ("gpt_cond_len", "gpt_cond_chunk_len", "max_ref_len", "sound_norm_refs")
        }
        if actual_conditioning != expected_conditioning:
            raise ValueError(
                f"Pinned conditioning config mismatch: {actual_conditioning} != {expected_conditioning}"
            )
        model = Xtts.init_from_config(config)
        model.load_checkpoint(config, checkpoint_dir=str(model_dir), eval=True, use_deepspeed=False)
        model.to("cpu")
        model.eval()
    model_elapsed = time.perf_counter() - model_started
    emit_marker({"status": "MODEL_LOAD_PASS", "elapsed_seconds": model_elapsed})

    synthesis_specs = {
        "generated_primary_clean": [primary_clean],
        "generated_primary_plus_candidate2_clean": [primary_clean, candidate2_clean],
    }
    performance: dict[str, dict[str, Any]] = {}
    for name in OUTPUT_NAMES:
        elapsed, peak_bytes = synthesize_once(
            model,
            config,
            torch,
            torchaudio,
            synthesis_specs[name],
            raw_paths[name],
            tracker,
        )
        performance[name] = {
            "inference_elapsed_seconds": elapsed,
            "peak_rss_bytes": peak_bytes,
            "peak_rss_gib": peak_bytes / (1024**3),
        }
        emit_marker({"status": "SYNTHESIS_PASS", "name": name, "elapsed_seconds": elapsed})
    if tracker["synthesis_calls_attempted"] != 2 or tracker["synthesis_calls_completed"] != 2:
        raise RuntimeError(f"Exactly two synthesis calls were required: {tracker}")
    del model
    gc.collect()

    tracker["stage"] = "normalization_and_validation"
    target_lufs = -23.0
    raw_signal = {name: analyze_signal(path, ffmpeg) for name, path in raw_paths.items()}
    raw_loudness = {name: measure_loudness(path, ffmpeg, target_lufs) for name, path in raw_paths.items()}
    reference_mapping = {
        "generated_primary_clean": [manifest_refs["primary"]],
        "generated_primary_plus_candidate2_clean": [
            manifest_refs["primary"],
            manifest_refs["candidate_2"],
        ],
    }
    outputs: dict[str, dict[str, Any]] = {}
    for name in OUTPUT_NAMES:
        gain_db = target_lufs - raw_loudness[name]["integrated_lufs"]
        predicted_true_peak = raw_loudness[name]["true_peak_dbtp"] + gain_db
        if predicted_true_peak > -0.10:
            raise ValueError(
                f"Static -23 LUFS gain would risk clipping for {name}: "
                f"predicted {predicted_true_peak:.3f} dBTP"
            )
        normalization_elapsed = render_static_gain(
            raw_paths[name], final_paths[name], ffmpeg, gain_db
        )
        final_signal = analyze_signal(final_paths[name], ffmpeg)
        final_loudness = measure_loudness(final_paths[name], ffmpeg, target_lufs)
        if abs(final_loudness["integrated_lufs"] - target_lufs) > 0.20:
            raise ValueError(f"Post-normalization LUFS outside tolerance for {name}: {final_loudness}")
        if final_loudness["true_peak_dbtp"] > 0.00:
            raise ValueError(f"Post-normalization true peak exceeds 0 dBTP for {name}")
        if final_signal["sample_count_per_channel"] != raw_signal[name]["sample_count_per_channel"]:
            raise ValueError(f"Static gain changed sample count for {name}")
        if final_signal["sample_rate"] != 24000 or final_signal["channels"] != 1:
            raise ValueError(f"Unexpected final output format for {name}")

        outputs[name] = {
            "file": final_paths[name].name,
            "reference_sources": reference_mapping[name],
            "inference_parameters": {
                **PARAMETERS,
                "speaker_wav_count": len(reference_mapping[name]),
            },
            "inference_elapsed_seconds": performance[name]["inference_elapsed_seconds"],
            "normalization_elapsed_seconds": normalization_elapsed,
            "artifact_elapsed_seconds": performance[name]["inference_elapsed_seconds"]
            + normalization_elapsed,
            "peak_rss_bytes": performance[name]["peak_rss_bytes"],
            "peak_rss_gib": performance[name]["peak_rss_gib"],
            "raw_intermediate": {
                "sha256": raw_signal[name]["sha256"],
                "size_bytes": raw_signal[name]["size_bytes"],
                "signal": raw_signal[name],
                "loudness": raw_loudness[name],
                "retention": "REMOVED_AFTER_FINAL_VALIDATION",
            },
            "static_gain_db": gain_db,
            "predicted_true_peak_after_gain_dbtp": predicted_true_peak,
            "post_normalization_loudness": final_loudness,
            "final_signal": final_signal,
            "remaining_child_processes_at_report_time": live_child_process_count(),
        }
        emit_marker({"status": "NORMALIZATION_AND_DECODE_PASS", "name": name})

    for raw_path in raw_paths.values():
        raw_path.unlink()
    raw_dir.rmdir()
    generated_outputs = sorted(output_dir.glob("generated_*.wav"))
    if [path.stem for path in generated_outputs] != list(OUTPUT_NAMES):
        raise RuntimeError(f"Final output count/name guard failed: {generated_outputs}")

    reference_comparisons = {
        "primary_original_to_clean": {
            "original": reference_signal["primary_original"],
            "clean": reference_signal["primary_clean"],
            "original_loudness": reference_loudness["primary_original"],
            "clean_loudness": reference_loudness["primary_clean"],
            "cleaning_filter": manifest_refs["primary"]["cleaning_filter"],
        },
        "candidate2_original_to_clean": {
            "original": reference_signal["candidate_2_original"],
            "clean": reference_signal["candidate_2_clean"],
            "original_loudness": reference_loudness["candidate_2_original"],
            "clean_loudness": reference_loudness["candidate_2_clean"],
            "cleaning_filter": manifest_refs["candidate_2"]["cleaning_filter"],
        },
        "candidate1_audit_only": {
            "original": reference_signal["candidate_1_original"],
            "original_loudness": reference_loudness["candidate_1_original"],
            "used_for_synthesis": False,
        },
    }
    report = {
        "schema_version": 1,
        "status": "REFERENCE_QUALITY_ROUND_PENDING_LISTENING",
        "listening_decision_before_round": "USER_LISTENING_REJECTED_TUNING_ROUND_1",
        "manual_acceptance": "PENDING_USER_LISTENING",
        "test_text": TEST_TEXT,
        "language": "vi",
        "engine": "vixtts",
        "device": "cpu",
        "manifest": manifest,
        "transcript_audit": transcript_audit,
        "reference_pre_post": reference_comparisons,
        "baseline_benchmark": {
            "path": str(baseline),
            "immutable_sha256": expected_baseline_hash,
            "signal": baseline_signal,
            "loudness": baseline_loudness,
            "rerendered": False,
        },
        "analysis_method": {
            "pitch": "Active-frame pitch proxy: torchaudio.functional.detect_pitch_frequency; frame_time=0.01s; win_length=30; range=140-400Hz; keep frames only when at least 50% of the aligned 30-frame RMS window exceeds -45 dBFS; report median and P10-P90 semitone span",
            "spectral_centroid": "magnitude STFT n_fft=2048 hop=512 Hann; median over frames with time-domain RMS > -45 dBFS",
            "comparability_note": "Fixed method for this report. Round 1 evaluation did not preserve executable analyzer provenance, so exact numerical equivalence is not claimed.",
        },
        "model": {
            "repo_id": "capleaf/viXTTS",
            "revision": MODEL_REVISION,
            "model_pth_sha256": MODEL_SHA256,
            "tts_revision": TTS_REVISION,
            "transformers": transformers.__version__,
            "conditioning_parameters_loaded_from_config": actual_conditioning,
            "load_elapsed_seconds": model_elapsed,
            "load_peak_rss_bytes": model_memory.peak_bytes,
            "load_peak_rss_gib": model_memory.peak_bytes / (1024**3),
            "directory_size_bytes": directory_size(model_dir),
        },
        "normalization": {
            "target_integrated_lufs": target_lufs,
            "measurement": "FFmpeg loudnorm EBU R128 analysis only",
            "render": "single static volume gain per generated file",
            "true_peak_preflight_clipping_margin_dbtp": -0.10,
            "true_peak_final_ceiling_dbtp": 0.00,
            "loudness_tolerance_lu": 0.20,
            "baseline_rerendered": False,
            "limiter": False,
            "dynamic_compression": False,
            "pitch_processing": False,
            "speed_processing": False,
        },
        "outputs": outputs,
        "output_file_count": len(generated_outputs),
        "synthesis_calls_attempted": tracker["synthesis_calls_attempted"],
        "synthesis_calls_completed": tracker["synthesis_calls_completed"],
        "one_round_guard": "exactly_two_new_synthesis_calls_no_retry",
        "raw_intermediates_retained": False,
        "total_round_process_elapsed_seconds": time.perf_counter() - total_started,
        "remaining_child_processes_at_report_time": live_child_process_count(),
        "external_process_audit": "PENDING_WRAPPER_POST_EXIT_AUDIT",
        "gui_changes": "NONE",
        "portable_build": "NOT_RUN",
        "agent_sdk": "NOT_INSTALLED",
        "vad": "NOT_RUN",
        "source_separation": "NOT_RUN",
        "openai_api_calls": 0,
        "other_model_api_calls": 0,
        "audio_uploaded": False,
        "decision_rule": "User must listen. If neither new output exceeds the immutable baseline, propose a separate Phase 2; do not random-tune further.",
    }
    atomic_json(report_json_path, report)
    write_markdown(report_md_path, report)
    emit_marker({"status": report["status"], "report": str(report_json_path)})
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one controlled viXTTS Reference Quality Round")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--transcript-audit", type=Path, required=True)
    parser.add_argument("--primary-original", type=Path, required=True)
    parser.add_argument("--candidate1-original", type=Path, required=True)
    parser.add_argument("--candidate2-original", type=Path, required=True)
    parser.add_argument("--primary-clean", type=Path, required=True)
    parser.add_argument("--candidate2-clean", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--round1-report", type=Path, required=True)
    parser.add_argument("--ffmpeg", type=Path, required=True)
    parser.add_argument("--model-dir", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    started = time.perf_counter()
    tracker: dict[str, Any] = {
        "stage": "argument_parsing",
        "synthesis_calls_attempted": 0,
        "synthesis_calls_completed": 0,
    }
    try:
        return run(args, tracker)
    except Exception as exc:  # noqa: BLE001 - stable CLI failure boundary.
        traceback.print_exc(file=sys.stderr)
        output_dir = args.output_dir.resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        failure_path = output_dir / "reference_quality_failure_report.json"
        if not failure_path.exists():
            atomic_json(
                failure_path,
                {
                    "schema_version": 1,
                    "status": "REFERENCE_QUALITY_ROUND_FAILED",
                    "stage": tracker["stage"],
                    "synthesis_calls_attempted": tracker["synthesis_calls_attempted"],
                    "synthesis_calls_completed": tracker["synthesis_calls_completed"],
                    "error_code": type(exc).__name__.upper(),
                    "message": str(exc),
                    "elapsed_seconds": time.perf_counter() - started,
                    "no_retry_performed": True,
                },
            )
        emit_marker(
            {
                "status": "REFERENCE_QUALITY_ROUND_FAILED",
                "stage": tracker["stage"],
                "error_code": type(exc).__name__.upper(),
                "message": str(exc),
            }
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
