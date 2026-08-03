from __future__ import annotations

import argparse
import gc
import hashlib
import json
import math
import os
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
DEFAULT_MODEL_DIR = RUNTIME_ROOT / "models" / f"capleaf_viXTTS_{MODEL_REVISION[:12]}"
DEFAULT_HF_HOME = RUNTIME_ROOT / ".cache" / "huggingface"
MARKER_PREFIX = "@@VOICE_DUB|"
TEST_TEXT = (
    "Xin chào, đây là đoạn âm thanh thử nghiệm được tạo từ giọng tham chiếu "
    "trong video."
)


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def emit_marker(payload: dict[str, Any]) -> None:
    print(MARKER_PREFIX + json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)


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


def validate_reference(path: Path) -> dict[str, Any]:
    import torch
    import torchaudio

    waveform, sample_rate = torchaudio.load(str(path))
    if waveform.numel() == 0 or sample_rate <= 0:
        raise ValueError("Reference WAV has no decodable samples")
    duration = waveform.shape[-1] / sample_rate
    if not 6.0 <= duration <= 12.0:
        raise ValueError(f"Reference duration must be 6–12 seconds, got {duration:.6f}")
    if not torch.isfinite(waveform).all():
        raise ValueError("Reference WAV contains NaN or Inf")
    absolute = waveform.abs()
    return {
        "path": str(path),
        "sha256": sha256_file(path),
        "sample_rate": sample_rate,
        "channels": waveform.shape[0],
        "duration_seconds": duration,
        "peak": float(absolute.max().item()),
        "rms": float(torch.sqrt(torch.mean(waveform * waveform)).item()),
        "silence_ratio_below_minus_60_dbfs": float((absolute < 0.001).float().mean().item()),
        "clipping_ratio": float((absolute >= 0.999).float().mean().item()),
    }


def validate_output(path: Path, ffmpeg: Path) -> dict[str, Any]:
    import torch
    import torchaudio

    waveform, sample_rate = torchaudio.load(str(path))
    if waveform.numel() == 0 or sample_rate <= 0:
        raise ValueError("Generated WAV has no decodable samples")
    duration = waveform.shape[-1] / sample_rate
    if duration <= 0:
        raise ValueError("Generated WAV duration is not positive")
    if not torch.isfinite(waveform).all():
        raise ValueError("Generated WAV contains NaN or Inf")
    absolute = waveform.abs()
    peak = float(absolute.max().item())
    rms = float(torch.sqrt(torch.mean(waveform * waveform)).item())
    silence_ratio = float((absolute < 0.001).float().mean().item())
    clipping_ratio = float((absolute >= 0.999).float().mean().item())
    if peak <= 0.0001 or rms <= 0.00001 or silence_ratio >= 0.999:
        raise ValueError("Generated WAV is effectively silent")
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
        "duration_seconds": duration,
        "peak": peak,
        "peak_dbfs": 20.0 * math.log10(max(peak, 1e-12)),
        "rms": rms,
        "rms_dbfs": 20.0 * math.log10(max(rms, 1e-12)),
        "silence_ratio_below_minus_60_dbfs": silence_ratio,
        "clipping_ratio": clipping_ratio,
        "ffmpeg_decode": "Pass",
    }


def download_model(model_dir: Path) -> dict[str, Any]:
    from huggingface_hub import snapshot_download

    model_dir.mkdir(parents=True, exist_ok=True)
    snapshot_download(
        repo_id="capleaf/viXTTS",
        revision=MODEL_REVISION,
        allow_patterns=["model.pth", "config.json", "vocab.json", "LICENSE.txt", "README.md"],
        local_dir=str(model_dir),
    )
    required = [model_dir / name for name in ("model.pth", "config.json", "vocab.json", "LICENSE.txt")]
    missing = [str(path) for path in required if not path.is_file() or path.stat().st_size == 0]
    if missing:
        raise FileNotFoundError(f"Model download is incomplete: {missing}")
    actual_model_hash = sha256_file(model_dir / "model.pth")
    if actual_model_hash.lower() != MODEL_SHA256.lower():
        raise ValueError(
            f"model.pth SHA-256 mismatch: expected {MODEL_SHA256}, got {actual_model_hash}"
        )
    return {
        "repo_id": "capleaf/viXTTS",
        "revision": MODEL_REVISION,
        "model_pth_sha256": actual_model_hash,
        "directory_size_bytes": directory_size(model_dir),
    }


def run_probe(args: argparse.Namespace) -> int:
    reference = args.reference.resolve()
    output = args.output.resolve()
    report_path = args.report.resolve()
    ffmpeg = args.ffmpeg.resolve()
    model_dir = args.model_dir.resolve()

    if os.environ.get("VOICE_DUB_AUTHORIZED_USE") != "1":
        payload = {
            "schema_version": 1,
            "status": "CONSENT_REQUIRED",
            "error_code": "CONSENT_REQUIRED",
            "message": "Explicit authorized-use confirmation is required before voice cloning.",
        }
        emit_marker(payload)
        return 3
    if not reference.is_file():
        raise FileNotFoundError(f"Reference WAV does not exist: {reference}")
    if not ffmpeg.is_file():
        raise FileNotFoundError(f"FFmpeg does not exist: {ffmpeg}")
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite existing output: {output}")
    if report_path.exists():
        raise FileExistsError(f"Refusing to overwrite existing report: {report_path}")
    if not (VENDOR_ROOT / "TTS" / "tts" / "models" / "xtts.py").is_file():
        raise FileNotFoundError(f"Pinned TTS source is missing: {VENDOR_ROOT}")

    os.environ.setdefault("HF_HOME", str(DEFAULT_HF_HOME))
    sys.path.insert(0, str(VENDOR_ROOT))

    stage_elapsed: dict[str, float] = {}
    started = time.perf_counter()
    result: dict[str, Any] = {
        "schema_version": 1,
        "status": "RUNNING",
        "device": "cpu",
        "text": TEST_TEXT,
        "language": "vi",
        "tts_revision": TTS_REVISION,
        "model_revision": MODEL_REVISION,
        "commercial_use_allowed": False,
        "license": "Coqui Public Model License 1.0.0",
        "agent_api_calls": 0,
        "audio_uploaded": False,
    }

    with PeakRssMonitor() as memory:
        stage = time.perf_counter()
        result["reference"] = validate_reference(reference)
        stage_elapsed["reference_validation"] = time.perf_counter() - stage

        stage = time.perf_counter()
        result["model"] = download_model(model_dir)
        stage_elapsed["model_download_and_hash"] = time.perf_counter() - stage

        log("Loading viXTTS on CPU")
        stage = time.perf_counter()
        import torch
        import torchaudio
        from TTS.tts.configs.xtts_config import XttsConfig
        from TTS.tts.models.xtts import Xtts

        config = XttsConfig()
        config.load_json(str(model_dir / "config.json"))
        model = Xtts.init_from_config(config)
        model.load_checkpoint(
            config,
            checkpoint_dir=str(model_dir),
            eval=True,
            use_deepspeed=False,
        )
        model.to("cpu")
        stage_elapsed["model_load"] = time.perf_counter() - stage
        emit_marker(
            {
                "schema_version": 1,
                "status": "MODEL_LOAD_PASS",
                "device": "cpu",
                "model_revision": MODEL_REVISION,
            }
        )

        log("Synthesizing the fixed Vietnamese probe sentence")
        stage = time.perf_counter()
        torch.manual_seed(42)
        with torch.inference_mode():
            synthesized = model.synthesize(
                TEST_TEXT,
                config,
                speaker_wav=[str(reference)],
                language="vi",
                temperature=0.3,
                length_penalty=1.0,
                repetition_penalty=10.0,
                top_k=30,
                top_p=0.85,
                enable_text_splitting=True,
            )
        waveform = torch.as_tensor(synthesized["wav"], dtype=torch.float32).unsqueeze(0)
        output.parent.mkdir(parents=True, exist_ok=True)
        torchaudio.save(
            str(output),
            waveform.cpu(),
            24000,
            encoding="PCM_S",
            bits_per_sample=16,
        )
        stage_elapsed["synthesis_and_save"] = time.perf_counter() - stage
        emit_marker(
            {
                "schema_version": 1,
                "status": "SYNTHESIS_PASS",
                "output": str(output),
            }
        )

        del waveform, synthesized, model
        gc.collect()

        stage = time.perf_counter()
        result["output"] = validate_output(output, ffmpeg)
        stage_elapsed["output_validation"] = time.perf_counter() - stage

    result["status"] = "ENGINE_PROBE_PASS"
    result["stage_elapsed_seconds"] = stage_elapsed
    result["elapsed_seconds"] = time.perf_counter() - started
    result["peak_rss_bytes"] = memory.peak_bytes
    result["peak_rss_gib"] = memory.peak_bytes / (1024**3)
    result["model_and_hf_cache_size_bytes"] = directory_size(model_dir) + directory_size(DEFAULT_HF_HOME)
    atomic_write_json(report_path, result)
    emit_marker(
        {
            "schema_version": 1,
            "status": "ENGINE_PROBE_PASS",
            "output": str(output),
            "report": str(report_path),
        }
    )
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the pinned viXTTS CPU research probe")
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--ffmpeg", type=Path, required=True)
    parser.add_argument("--model-dir", type=Path, default=DEFAULT_MODEL_DIR)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    started = time.perf_counter()
    try:
        with PeakRssMonitor() as failure_memory:
            return run_probe(args)
    except Exception as exc:  # noqa: BLE001 - CLI boundary must report stable failure.
        traceback.print_exc(file=sys.stderr)
        failure_report = {
            "schema_version": 1,
            "status": "ENGINE_PROBE_FAILED",
            "error_code": type(exc).__name__.upper(),
            "message": str(exc),
            "elapsed_seconds": time.perf_counter() - started,
            "peak_rss_bytes": failure_memory.peak_bytes,
            "peak_rss_gib": failure_memory.peak_bytes / (1024**3),
            "output_created": args.output.resolve().is_file(),
        }
        report_path = args.report.resolve()
        if not report_path.exists():
            atomic_write_json(report_path, failure_report)
        emit_marker(
            {
                "schema_version": 1,
                "status": "ENGINE_PROBE_FAILED",
                "error_code": type(exc).__name__.upper(),
                "message": str(exc),
                "report": str(report_path),
            }
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
