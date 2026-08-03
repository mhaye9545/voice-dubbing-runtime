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
VENDOR_ROOT = RUNTIME_ROOT / "vendor" / f"TTS-{TTS_REVISION}"
TEST_TEXT = (
    "Xin ch\u00e0o, \u0111\u00e2y l\u00e0 \u0111o\u1ea1n \u00e2m thanh th\u1eed nghi\u1ec7m \u0111\u01b0\u1ee3c t\u1ea1o t\u1eeb "
    "gi\u1ecdng tham chi\u1ebfu trong video."
)
BASELINE_PARAMETERS: dict[str, Any] = {
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
TUNED_PARAMETERS: dict[str, Any] = {
    **BASELINE_PARAMETERS,
    "temperature": 0.40,
    "top_k": 40,
    "top_p": 0.88,
}
OUTPUT_NAMES = (
    "generated_baseline",
    "generated_candidate_1",
    "generated_candidate_2",
    "generated_tuned",
)
MARKER_PREFIX = "@@VOICE_DUB_TUNING|"


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


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
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

    process = psutil.Process(os.getpid())
    count = 0
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


def validate_wav(path: Path, ffmpeg: Path, *, reference: bool = False) -> dict[str, Any]:
    import torch
    import torchaudio

    if not path.is_file() or path.stat().st_size <= 0:
        raise FileNotFoundError(f"Missing or empty WAV: {path}")
    waveform, sample_rate = torchaudio.load(str(path))
    if waveform.numel() == 0 or sample_rate <= 0:
        raise ValueError(f"No decodable samples: {path}")
    if not torch.isfinite(waveform).all():
        raise ValueError(f"NaN or Inf samples: {path}")
    duration = waveform.shape[-1] / sample_rate
    if duration <= 0:
        raise ValueError(f"Non-positive duration: {path}")
    if reference and not 8.0 <= duration <= 15.0:
        raise ValueError(f"Reference must be 8-15 seconds, got {duration:.6f}: {path}")
    absolute = waveform.abs()
    peak = float(absolute.max().item())
    rms = float(torch.sqrt(torch.mean(waveform * waveform)).item())
    silence_ratio = float((absolute < 0.001).float().mean().item())
    clipping_ratio = float((absolute >= 0.999).float().mean().item())
    if not reference and (peak <= 0.0001 or rms <= 0.00001 or silence_ratio >= 0.999):
        raise ValueError(f"Effectively silent generated WAV: {path}")
    decode = subprocess.run(
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
    if decode.returncode != 0:
        raise ValueError(f"FFmpeg decode failed ({decode.returncode}): {decode.stderr.strip()}")
    return {
        "path": str(path),
        "size_bytes": path.stat().st_size,
        "sha256": sha256_file(path),
        "sample_rate": sample_rate,
        "channels": waveform.shape[0],
        "sample_count_per_channel": waveform.shape[-1],
        "duration_seconds": duration,
        "peak": peak,
        "peak_dbfs": 20.0 * math.log10(max(peak, 1e-12)),
        "rms": rms,
        "rms_dbfs": 20.0 * math.log10(max(rms, 1e-12)),
        "silence_ratio_below_minus_60_dbfs": silence_ratio,
        "clipping_ratio": clipping_ratio,
        "finite_samples": "Pass",
        "non_silent": "Pass" if not reference else "Not required",
        "ffmpeg_decode": "Pass",
    }


def measure_loudness(path: Path, ffmpeg: Path, target_i: float) -> dict[str, float]:
    command = [
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
    ]
    completed = subprocess.run(
        command,
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
        raise ValueError(f"Non-finite loudness measurement for {path}: {result}")
    return result


def render_static_gain(raw_path: Path, output_path: Path, ffmpeg: Path, gain_db: float) -> float:
    if output_path.exists():
        raise FileExistsError(f"Refusing to overwrite output: {output_path}")
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
        raise RuntimeError(f"Static-gain render failed for {raw_path}: {completed.stderr.strip()}")
    return elapsed


def synthesize_variant(
    model: Any,
    config: Any,
    torch: Any,
    torchaudio: Any,
    references: list[Path],
    parameters: dict[str, Any],
    output: Path,
) -> tuple[float, int]:
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite raw output: {output}")
    config_owned_keys = {"gpt_cond_len", "gpt_cond_chunk_len", "max_ref_len", "sound_norm_refs"}
    call_parameters = {
        key: value
        for key, value in parameters.items()
        if key != "seed" and key not in config_owned_keys
    }
    torch.manual_seed(int(parameters["seed"]))
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
    del waveform, synthesized
    gc.collect()
    return elapsed, memory.peak_bytes


def write_markdown(report_path: Path, report: dict[str, Any]) -> None:
    lines = [
        "# viXTTS controlled tuning round 1",
        "",
        f"Status: `{report['status']}`",
        "",
        f"Test text: {report['test_text']}",
        "",
        (
            "Normalization: EBU R128 measurement followed by static gain to "
            f"{report['normalization']['common_target_integrated_lufs']:.2f} LUFS; "
            "no limiter, dynamic compression, speed, or pitch processing."
        ),
        "",
        "| File | Reference | Parameters | Duration | Peak / RMS | LUFS | Inference | Peak RAM | Decode |",
        "|---|---|---|---:|---:|---:|---:|---:|---|",
    ]
    for name in OUTPUT_NAMES:
        item = report["outputs"][name]
        sources = "; ".join(
            f"{source['source_start_seconds']:.6f}-{source['source_end_seconds']:.6f}s"
            for source in item["reference_sources"]
        )
        params = item["inference_parameters"]
        technical = item["technical_validation"]
        lines.append(
            "| {file} | {sources} | T={temperature:.2f}, k={top_k}, p={top_p:.2f}, refs={refs} | "
            "{duration:.6f}s | {peak:.2f} / {rms:.2f} dBFS | {lufs:.2f} | {elapsed:.3f}s | "
            "{ram:.3f} GiB | {decode} |".format(
                file=item["file"],
                sources=sources,
                temperature=params["temperature"],
                top_k=params["top_k"],
                top_p=params["top_p"],
                refs=params["speaker_wav_count"],
                duration=technical["duration_seconds"],
                peak=technical["peak_dbfs"],
                rms=technical["rms_dbfs"],
                lufs=item["post_normalization_loudness"]["integrated_lufs"],
                elapsed=item["inference_elapsed_seconds"],
                ram=item["peak_rss_gib"],
                decode=technical["ffmpeg_decode"],
            )
        )
    lines.extend(
        [
            "",
            f"Model load: {report['model']['load_elapsed_seconds']:.3f}s.",
            "",
            "Reference cleanliness, speaker count, overlap, and transcript were selected by acoustic heuristics only "
            "and remain pending human listening because ASR/VAD/separation were explicitly out of scope.",
            "",
            "No GUI integration was performed. User listening approval is not inferred.",
            "",
        ]
    )
    report_path.write_text("\n".join(lines), encoding="utf-8")


def run(args: argparse.Namespace) -> int:
    if os.environ.get("VOICE_DUB_AUTHORIZED_USE") != "1":
        raise PermissionError("VOICE_DUB_AUTHORIZED_USE=1 is required")

    ffmpeg = args.ffmpeg.resolve()
    model_dir = args.model_dir.resolve()
    output_dir = args.output_dir.resolve()
    metadata_path = args.reference_metadata.resolve()
    baseline_report_path = args.baseline_report.resolve()
    baseline_raw_source = args.baseline_raw.resolve()
    reference_paths = {
        "baseline": args.baseline_reference.resolve(),
        "candidate_1": args.candidate_1.resolve(),
        "candidate_2": args.candidate_2.resolve(),
        "candidate_3": args.candidate_3.resolve(),
    }
    required = [ffmpeg, metadata_path, baseline_report_path, baseline_raw_source, *reference_paths.values()]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"Required files are missing: {missing}")
    if not (VENDOR_ROOT / "TTS" / "tts" / "models" / "xtts.py").is_file():
        raise FileNotFoundError(f"Pinned TTS source is missing: {VENDOR_ROOT}")
    model_file = model_dir / "model.pth"
    if sha256_file(model_file).lower() != MODEL_SHA256:
        raise ValueError("Pinned model.pth hash mismatch")

    output_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = output_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    report_json_path = output_dir / "tuning_report.json"
    report_md_path = output_dir / "tuning_report.md"
    failure_path = output_dir / "tuning_failure_report.json"
    forbidden_existing = [report_json_path, report_md_path, failure_path]
    forbidden_existing.extend(output_dir / f"{name}.wav" for name in OUTPUT_NAMES)
    forbidden_existing.extend(raw_dir / f"{name}.raw.wav" for name in OUTPUT_NAMES)
    existing = [str(path) for path in forbidden_existing if path.exists()]
    if existing:
        raise FileExistsError(f"One-round guard refuses existing tuning artifacts: {existing}")

    reference_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata_by_id = {item["id"]: item for item in reference_metadata["references"]}
    if set(reference_paths) - set(metadata_by_id):
        raise ValueError("Reference metadata does not cover every reference file")
    reference_validation = {
        key: validate_wav(path, ffmpeg, reference=True) for key, path in reference_paths.items()
    }
    for key, path in reference_paths.items():
        expected = metadata_by_id[key].get("sha256")
        actual = reference_validation[key]["sha256"]
        if expected and expected.lower() != actual.lower():
            raise ValueError(f"Reference hash mismatch for {key}: expected {expected}, got {actual}")

    baseline_report = json.loads(baseline_report_path.read_text(encoding="utf-8"))
    baseline_contract = {
        "status": baseline_report.get("status") == "ENGINE_PROBE_PASS",
        "text": baseline_report.get("text") == TEST_TEXT,
        "model_revision": baseline_report.get("model_revision") == MODEL_REVISION,
        "model_hash": baseline_report.get("model", {}).get("model_pth_sha256", "").lower()
        == MODEL_SHA256,
        "reference_hash": reference_validation["baseline"]["sha256"].lower()
        == baseline_report.get("reference", {}).get("sha256", "").lower(),
    }
    failed_baseline_contract = [key for key, passed in baseline_contract.items() if not passed]
    if failed_baseline_contract:
        raise ValueError(f"Baseline probe provenance mismatch: {failed_baseline_contract}")
    baseline_source_validation = validate_wav(baseline_raw_source, ffmpeg)
    expected_baseline_hash = baseline_report["output"]["sha256"]
    if baseline_source_validation["sha256"].lower() != expected_baseline_hash.lower():
        raise ValueError(
            "Baseline raw source hash does not match the successful probe report: "
            f"expected {expected_baseline_hash}, got {baseline_source_validation['sha256']}"
        )
    desired_target = -23.0
    baseline_pre_loudness = measure_loudness(baseline_raw_source, ffmpeg, desired_target)
    baseline_raw = raw_dir / "generated_baseline.raw.wav"
    shutil.copy2(baseline_raw_source, baseline_raw)
    baseline_copy_validation = validate_wav(baseline_raw, ffmpeg)
    if baseline_copy_validation["sha256"] != baseline_source_validation["sha256"]:
        raise ValueError("Copied baseline raw WAV does not match its source")
    raw_paths = {
        "generated_baseline": baseline_raw,
        "generated_candidate_1": raw_dir / "generated_candidate_1.raw.wav",
        "generated_candidate_2": raw_dir / "generated_candidate_2.raw.wav",
        "generated_tuned": raw_dir / "generated_tuned.raw.wav",
    }

    sys.path.insert(0, str(VENDOR_ROOT))
    import torch
    import torchaudio
    from TTS.tts.configs.xtts_config import XttsConfig
    from TTS.tts.models.xtts import Xtts

    total_started = time.perf_counter()
    model_started = time.perf_counter()
    with PeakRssMonitor() as model_memory:
        config = XttsConfig()
        config.load_json(str(model_dir / "config.json"))
        actual_conditioning_config = {
            "gpt_cond_len": config.gpt_cond_len,
            "gpt_cond_chunk_len": config.gpt_cond_chunk_len,
            "max_ref_len": config.max_ref_len,
            "sound_norm_refs": config.sound_norm_refs,
        }
        expected_conditioning_config = {
            key: BASELINE_PARAMETERS[key]
            for key in ("gpt_cond_len", "gpt_cond_chunk_len", "max_ref_len", "sound_norm_refs")
        }
        if actual_conditioning_config != expected_conditioning_config:
            raise ValueError(
                "Pinned model conditioning config mismatch: "
                f"expected {expected_conditioning_config}, got {actual_conditioning_config}"
            )
        model = Xtts.init_from_config(config)
        model.load_checkpoint(config, checkpoint_dir=str(model_dir), eval=True, use_deepspeed=False)
        model.to("cpu")
    model_elapsed = time.perf_counter() - model_started
    emit_marker({"status": "MODEL_LOAD_PASS", "elapsed_seconds": model_elapsed})

    synthesis_specs = {
        "generated_candidate_1": ([reference_paths["candidate_1"]], BASELINE_PARAMETERS),
        "generated_candidate_2": (
            [reference_paths["candidate_1"], reference_paths["candidate_2"]],
            BASELINE_PARAMETERS,
        ),
        "generated_tuned": ([reference_paths["candidate_1"]], TUNED_PARAMETERS),
    }
    performance: dict[str, dict[str, Any]] = {
        "generated_baseline": {
            "inference_elapsed_seconds": baseline_report["stage_elapsed_seconds"]["synthesis_and_save"],
            "original_total_elapsed_seconds": baseline_report["elapsed_seconds"],
            "peak_rss_bytes": baseline_report["peak_rss_bytes"],
            "peak_rss_gib": baseline_report["peak_rss_gib"],
            "provenance": "reused_existing_baseline_without_resynthesis",
        }
    }
    for name, (references, parameters) in synthesis_specs.items():
        elapsed, peak_bytes = synthesize_variant(
            model,
            config,
            torch,
            torchaudio,
            references,
            parameters,
            raw_paths[name],
        )
        performance[name] = {
            "inference_elapsed_seconds": elapsed,
            "peak_rss_bytes": peak_bytes,
            "peak_rss_gib": peak_bytes / (1024**3),
            "provenance": "synthesized_in_controlled_tuning_round_1",
        }
        emit_marker({"status": "SYNTHESIS_PASS", "name": name, "elapsed_seconds": elapsed})

    del model
    gc.collect()

    raw_validation = {
        "generated_baseline": baseline_copy_validation,
        **{
            name: validate_wav(path, ffmpeg)
            for name, path in raw_paths.items()
            if name != "generated_baseline"
        },
    }
    pre_loudness = {
        "generated_baseline": baseline_pre_loudness,
        **{
            name: measure_loudness(path, ffmpeg, desired_target)
            for name, path in raw_paths.items()
            if name != "generated_baseline"
        },
    }
    safe_targets = [
        value["integrated_lufs"] + (-2.0 - value["true_peak_dbtp"]) - 0.10
        for value in pre_loudness.values()
    ]
    common_target = min(desired_target, *safe_targets)
    common_target = math.floor(common_target * 100.0) / 100.0

    reference_mapping = {
        "generated_baseline": ["baseline"],
        "generated_candidate_1": ["candidate_1"],
        "generated_candidate_2": ["candidate_1", "candidate_2"],
        "generated_tuned": ["candidate_1"],
    }
    parameter_mapping = {
        "generated_baseline": BASELINE_PARAMETERS,
        "generated_candidate_1": BASELINE_PARAMETERS,
        "generated_candidate_2": BASELINE_PARAMETERS,
        "generated_tuned": TUNED_PARAMETERS,
    }
    outputs: dict[str, dict[str, Any]] = {}
    for name in OUTPUT_NAMES:
        output_path = output_dir / f"{name}.wav"
        gain_db = common_target - pre_loudness[name]["integrated_lufs"]
        normalization_elapsed = render_static_gain(raw_paths[name], output_path, ffmpeg, gain_db)
        technical = validate_wav(output_path, ffmpeg)
        post = measure_loudness(output_path, ffmpeg, common_target)
        if abs(post["integrated_lufs"] - common_target) > 0.20:
            raise ValueError(f"Post-normalization LUFS outside tolerance for {name}: {post}")
        if post["true_peak_dbtp"] > -2.00:
            raise ValueError(f"Post-normalization true peak exceeds guard for {name}: {post}")
        if technical["sample_count_per_channel"] != raw_validation[name]["sample_count_per_channel"]:
            raise ValueError(f"Static gain changed sample count for {name}")
        if technical["sample_rate"] != 24000 or technical["channels"] != 1:
            raise ValueError(f"Unexpected output format for {name}: {technical}")
        if technical["clipping_ratio"] > 0.0:
            raise ValueError(f"Clipping detected after normalization for {name}: {technical}")

        params = dict(parameter_mapping[name])
        params["speaker_wav_count"] = len(reference_mapping[name])
        item_performance = performance[name]
        outputs[name] = {
            "file": output_path.name,
            "raw_file": str(raw_paths[name].relative_to(output_dir)),
            "reference_sources": [metadata_by_id[key] for key in reference_mapping[name]],
            "inference_parameters": params,
            "inference_elapsed_seconds": item_performance["inference_elapsed_seconds"],
            "normalization_elapsed_seconds": normalization_elapsed,
            "artifact_elapsed_seconds": item_performance["inference_elapsed_seconds"]
            + normalization_elapsed,
            "peak_rss_bytes": item_performance["peak_rss_bytes"],
            "peak_rss_gib": item_performance["peak_rss_gib"],
            "performance_provenance": item_performance["provenance"],
            "pre_normalization_loudness": pre_loudness[name],
            "static_gain_db": gain_db,
            "post_normalization_loudness": post,
            "technical_validation": technical,
            "remaining_child_processes_at_report_time": live_child_process_count(),
        }
        emit_marker({"status": "NORMALIZATION_AND_DECODE_PASS", "name": name})

    report = {
        "schema_version": 1,
        "status": "TUNING_ROUND_1_PENDING_LISTENING",
        "listening_decision_before_round": "USER_LISTENING_REJECTED_FOR_TUNING",
        "manual_acceptance": "PENDING_USER_LISTENING",
        "test_text": TEST_TEXT,
        "language": "vi",
        "device": "cpu",
        "engine": "vixtts",
        "one_round_guard": "exactly_three_new_synthesis_calls; baseline_reused",
        "model": {
            "repo_id": "capleaf/viXTTS",
            "revision": MODEL_REVISION,
            "model_pth_sha256": MODEL_SHA256,
            "tts_revision": TTS_REVISION,
            "directory_size_bytes": directory_size(model_dir),
            "load_elapsed_seconds": model_elapsed,
            "load_peak_rss_bytes": model_memory.peak_bytes,
            "load_peak_rss_gib": model_memory.peak_bytes / (1024**3),
            "conditioning_parameters_loaded_from_config": actual_conditioning_config,
        },
        "reference_selection": reference_metadata,
        "reference_validation": reference_validation,
        "normalization": {
            "measurement": "FFmpeg loudnorm EBU R128 analysis only",
            "render": "single static volume gain per file",
            "desired_target_integrated_lufs": desired_target,
            "common_target_integrated_lufs": common_target,
            "true_peak_guard_dbtp": -2.0,
            "loudness_tolerance_lu": 0.20,
            "limiter": False,
            "dynamic_compression": False,
            "pitch_processing": False,
            "speed_processing": False,
        },
        "outputs": outputs,
        "total_tuning_process_elapsed_seconds": time.perf_counter() - total_started,
        "remaining_child_processes_at_report_time": live_child_process_count(),
        "asr": "NOT_INSTALLED_OR_RUN_BY_EXPLICIT_SCOPE",
        "vad": "NOT_INSTALLED_OR_RUN_BY_EXPLICIT_SCOPE",
        "source_separation": "NOT_INSTALLED_OR_RUN_BY_EXPLICIT_SCOPE",
        "gui_changes": "NONE",
        "agent_api_calls": 0,
        "audio_uploaded": False,
    }
    atomic_write_json(report_json_path, report)
    write_markdown(report_md_path, report)
    emit_marker({"status": report["status"], "report": str(report_json_path)})
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run exactly one controlled viXTTS CPU tuning round")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--reference-metadata", type=Path, required=True)
    parser.add_argument("--baseline-reference", type=Path, required=True)
    parser.add_argument("--candidate-1", type=Path, required=True)
    parser.add_argument("--candidate-2", type=Path, required=True)
    parser.add_argument("--candidate-3", type=Path, required=True)
    parser.add_argument("--baseline-raw", type=Path, required=True)
    parser.add_argument("--baseline-report", type=Path, required=True)
    parser.add_argument("--ffmpeg", type=Path, required=True)
    parser.add_argument("--model-dir", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    started = time.perf_counter()
    try:
        return run(args)
    except Exception as exc:  # noqa: BLE001 - stable CLI failure boundary.
        traceback.print_exc(file=sys.stderr)
        output_dir = args.output_dir.resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        failure_path = output_dir / "tuning_failure_report.json"
        if not failure_path.exists():
            atomic_write_json(
                failure_path,
                {
                    "schema_version": 1,
                    "status": "TUNING_ROUND_1_FAILED",
                    "error_code": type(exc).__name__.upper(),
                    "message": str(exc),
                    "elapsed_seconds": time.perf_counter() - started,
                    "no_retry_performed": True,
                },
            )
        emit_marker(
            {
                "status": "TUNING_ROUND_1_FAILED",
                "error_code": type(exc).__name__.upper(),
                "message": str(exc),
            }
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
