"""Isolated CPU-only Demucs worker.

Only this child-process module imports Demucs, Torch (indirectly), sphn, or
psutil.  The model manifest and full checkpoint hash are verified before any
legacy Torch checkpoint deserialization.
"""

from __future__ import annotations

import gc
import importlib.metadata
import json
import os
import sys
import threading
import time
import traceback
import wave
from pathlib import Path
from typing import Any

from .source_separation import (
    ENGINE_MARKER,
    MODEL_NAME,
    MODEL_SIGNATURE,
    NUMPY_REVISION,
    PACKAGE_REVISION,
    PSUTIL_REVISION,
    PYTHON_SERIES,
    SOURCE_SEPARATION_FAILED,
    SPHN_REVISION,
    TORCH_REVISION,
    verify_model_manifest,
)


def _emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(
        ENGINE_MARKER + json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"
    )
    sys.stdout.flush()


def _verify_runtime_packages() -> dict[str, str]:
    if f"{sys.version_info.major}.{sys.version_info.minor}" != PYTHON_SERIES:
        raise RuntimeError("SOURCE_SEPARATION_PYTHON_MISMATCH")
    expected = {
        "demucs": PACKAGE_REVISION.split("==", 1)[1],
        "torch": TORCH_REVISION,
        "numpy": NUMPY_REVISION,
        "sphn": SPHN_REVISION,
        "psutil": PSUTIL_REVISION,
    }
    installed: dict[str, str] = {}
    for distribution, version in expected.items():
        try:
            actual = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError as exc:
            raise RuntimeError(f"SOURCE_SEPARATION_PACKAGE_MISSING:{distribution}") from exc
        if actual != version:
            raise RuntimeError(
                f"SOURCE_SEPARATION_PACKAGE_MISMATCH:{distribution}:{actual}:{version}"
            )
        installed[distribution] = actual
    try:
        unexpected = importlib.metadata.version("torchaudio")
    except importlib.metadata.PackageNotFoundError:
        pass
    else:
        raise RuntimeError(f"SOURCE_SEPARATION_UNEXPECTED_TORCHAUDIO:{unexpected}")
    return installed


class _PeakMemoryMonitor:
    def __init__(self) -> None:
        self.peak_bytes = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "_PeakMemoryMonitor":
        import psutil

        root = psutil.Process(os.getpid())

        def sample() -> None:
            while not self._stop.wait(0.05):
                total = 0
                for process in (root, *root.children(recursive=True)):
                    try:
                        total += process.memory_info().rss
                    except (psutil.AccessDenied, psutil.NoSuchProcess):
                        continue
                self.peak_bytes = max(self.peak_bytes, total)

        sample_once = root.memory_info().rss
        self.peak_bytes = max(self.peak_bytes, sample_once)
        self._thread = threading.Thread(target=sample, name="demucs-peak-memory", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)


def _verify_wav(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size <= 44:
        raise RuntimeError("SOURCE_SEPARATION_OUTPUT_EMPTY")
    with wave.open(str(path), "rb") as reader:
        channels = reader.getnchannels()
        sample_width = reader.getsampwidth()
        sample_rate = reader.getframerate()
        frame_count = reader.getnframes()
    if channels <= 0 or sample_width != 2 or sample_rate <= 0 or frame_count <= 0:
        raise RuntimeError("SOURCE_SEPARATION_OUTPUT_WAV_INVALID")
    return {
        "channels": channels,
        "sample_width_bytes": sample_width,
        "sample_rate": sample_rate,
        "frame_count": frame_count,
        "duration_seconds": frame_count / sample_rate,
        "size_bytes": path.stat().st_size,
    }


def _commit_wav_exclusive(temporary: Path, output: Path) -> dict[str, Any]:
    """Durably commit a complete WAV without overwriting an existing output."""

    metrics = _verify_wav(temporary)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite separated vocals: {output}")
    with temporary.open("r+b") as handle:
        handle.flush()
        os.fsync(handle.fileno())
    os.link(temporary, output)
    return metrics


def _path_from_request(request: dict[str, Any], key: str) -> Path:
    raw = request.get(key)
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"Missing source-separation path: {key}")
    return Path(raw).resolve()


def _run(request: Any) -> dict[str, Any]:
    if not isinstance(request, dict) or request.get("schema_version") != 1:
        raise ValueError("Invalid source-separation request schema")
    if request.get("action") != "separate_vocals":
        raise ValueError("Unsupported source-separation worker action")
    expected_parameters = {
        "model_name": MODEL_NAME,
        "model_signature": MODEL_SIGNATURE,
        "device": "cpu",
        "two_stems": "vocals",
        "shifts": 0,
        "jobs": 0,
        "split": True,
        "overlap": 0.25,
    }
    mismatches = {
        key: {"expected": value, "actual": request.get(key)}
        for key, value in expected_parameters.items()
        if request.get(key) != value
    }
    if mismatches:
        raise ValueError(f"SOURCE_SEPARATION_PARAMETERS_MISMATCH:{mismatches}")

    runtime_root = Path(__file__).resolve().parents[1]
    expected_model_dir = (
        runtime_root / "models" / "source_separation" / "htdemucs"
    ).resolve()
    model_dir = _path_from_request(request, "model_dir")
    if model_dir != expected_model_dir:
        raise RuntimeError("SOURCE_SEPARATION_MODEL_DIR_MISMATCH")
    source = _path_from_request(request, "input_path")
    output = _path_from_request(request, "output_path")
    temporary = _path_from_request(request, "temporary_output_path")
    work_dir = _path_from_request(request, "work_dir")
    if not source.is_file() or source.stat().st_size == 0:
        raise FileNotFoundError(f"Source-separation input is missing: {source}")
    if output.exists() or temporary.exists():
        raise FileExistsError("Source-separation output or temporary output already exists")
    if not work_dir.is_dir():
        raise RuntimeError("SOURCE_SEPARATION_WORK_DIR_MISSING")
    if temporary.parent != output.parent:
        raise RuntimeError("SOURCE_SEPARATION_TEMP_OUTPUT_VOLUME_MISMATCH")

    manifest = verify_model_manifest(model_dir)
    packages = _verify_runtime_packages()
    os.environ["CUDA_VISIBLE_DEVICES"] = ""
    os.environ["HF_HUB_OFFLINE"] = "1"

    with _PeakMemoryMonitor() as memory:
        load_started = time.perf_counter()
        from demucs.api import Separator, save_audio

        separator = Separator(
            model=MODEL_NAME,
            repo=model_dir,
            device="cpu",
            shifts=0,
            split=True,
            overlap=0.25,
            progress=False,
            jobs=0,
        )
        model_load_elapsed = time.perf_counter() - load_started
        separation_started = time.perf_counter()
        _, stems = separator.separate_audio_file(source)
        if not isinstance(stems, dict) or "vocals" not in stems:
            raise RuntimeError("SOURCE_SEPARATION_VOCALS_STEM_MISSING")
        save_audio(
            stems["vocals"],
            str(temporary),
            samplerate=separator.samplerate,
            clip="rescale",
            bits_per_sample=16,
            as_float=False,
        )
        separation_elapsed = time.perf_counter() - separation_started
        wav_metrics = _commit_wav_exclusive(temporary, output)
        del stems, separator
        gc.collect()

    return {
        "engine_id": manifest["engine_id"],
        "model_id": manifest["model_id"],
        "model_name": MODEL_NAME,
        "model_signature": MODEL_SIGNATURE,
        "package_versions": packages,
        "device": "cpu",
        "two_stems": "vocals",
        "shifts": 0,
        "jobs": 0,
        "split": True,
        "overlap": 0.25,
        "model_load_elapsed_seconds": model_load_elapsed,
        "separation_elapsed_seconds": separation_elapsed,
        "peak_ram_bytes": memory.peak_bytes,
        "peak_ram_gib": memory.peak_bytes / (1024**3),
        "output": wav_metrics,
    }


def main() -> int:
    temporary: Path | None = None
    try:
        request = json.loads(sys.stdin.read())
        if isinstance(request, dict) and isinstance(request.get("temporary_output_path"), str):
            temporary = Path(request["temporary_output_path"])
        metrics = _run(request)
        _emit({"schema_version": 1, "status": "success", "metrics": metrics})
        return 0
    except Exception as exc:
        _emit(
            {
                "schema_version": 1,
                "status": "failed",
                "error": {
                    "code": SOURCE_SEPARATION_FAILED,
                    "message": str(exc),
                    "details": {"traceback": traceback.format_exc(limit=12)},
                },
            }
        )
        return 2
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
