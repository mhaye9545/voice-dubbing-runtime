"""Isolated official XTTS-v2 CPU worker used only by ``.venv-xtts``.

The module supports a one-request process and a persistent JSON-lines process.
It deliberately owns every ML import so the control runtime and FrameExtract
Studio remain thin clients.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import importlib.metadata
import json
import os
import re
import sys
import threading
import time
import traceback
import uuid
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


MARKER = "@@XTTS_ENGINE|"
MODEL_ID = "coqui/XTTS-v2"
MODEL_REVISION = "6c2b0d75eae4b7047358e3b6bd9325f857d43f77"
LICENSE_ID = "Coqui Public Model License 1.0.0"
LICENSE_URL = "https://coqui.ai/cpml.txt"
IDLE_TIMEOUT_SECONDS = 15 * 60
SAMPLE_RATE = 24000

XTTS_IMPORT_FAILED = "XTTS_IMPORT_FAILED"
XTTS_MODEL_NOT_FOUND = "XTTS_MODEL_NOT_FOUND"
XTTS_MODEL_LOAD_FAILED = "XTTS_MODEL_LOAD_FAILED"
XTTS_PROFILE_LOAD_FAILED = "XTTS_PROFILE_LOAD_FAILED"
XTTS_REFERENCE_INVALID = "XTTS_REFERENCE_INVALID"
XTTS_CONDITIONING_FAILED = "XTTS_CONDITIONING_FAILED"
XTTS_INFERENCE_FAILED = "XTTS_INFERENCE_FAILED"
XTTS_OUTPUT_WRITE_FAILED = "XTTS_OUTPUT_WRITE_FAILED"
XTTS_OUT_OF_MEMORY = "XTTS_OUT_OF_MEMORY"
DUPLICATE_JOB_REJECTED = "DUPLICATE_JOB_REJECTED"


def _emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(MARKER + json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n")
    sys.stdout.flush()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def _acceptance_path() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    root = Path(local) if local else Path.home() / ".local" / "share"
    return root / "FrameExtractStudio" / "VoiceDubbing" / "licenses" / "coqui_xtts_v2_cpml.json"


class XttsStageError(RuntimeError):
    def __init__(self, code: str, stage: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.stage = stage


def _verify_model_and_license(runtime_root: Path) -> tuple[Path, dict[str, Any]]:
    model_dir = runtime_root / "models" / "xtts_v2"
    manifest_path = model_dir / "model_manifest.json"
    acceptance_path = _acceptance_path()
    if not manifest_path.is_file():
        raise RuntimeError("MODEL_MANIFEST_MISSING")
    if not acceptance_path.is_file():
        raise RuntimeError("MODEL_LICENSE_NOT_ACCEPTED")
    manifest = _read_json(manifest_path)
    acceptance = _read_json(acceptance_path)
    if manifest.get("model_id") != MODEL_ID or manifest.get("revision") != MODEL_REVISION:
        raise RuntimeError("MODEL_REVISION_MISMATCH")
    if not (
        acceptance.get("accepted") is True
        and acceptance.get("model_id") == MODEL_ID
        and acceptance.get("revision") == MODEL_REVISION
        and acceptance.get("license_id") == LICENSE_ID
        and acceptance.get("license_url") == LICENSE_URL
        and acceptance.get("scope") == "research_personal_poc_noncommercial"
    ):
        raise RuntimeError("MODEL_LICENSE_NOT_ACCEPTED")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise RuntimeError("MODEL_MANIFEST_INVALID")
    for record in files:
        if not isinstance(record, dict) or not isinstance(record.get("path"), str):
            raise RuntimeError("MODEL_MANIFEST_INVALID")
        candidate = (model_dir / record["path"]).resolve()
        try:
            candidate.relative_to(model_dir.resolve())
        except ValueError as exc:
            raise RuntimeError("MODEL_MANIFEST_PATH_ESCAPE") from exc
        if not candidate.is_file() or candidate.stat().st_size != int(record.get("size_bytes", -1)):
            raise RuntimeError(f"MODEL_FILE_MISSING_OR_SIZE_MISMATCH:{record['path']}")
        if _sha256(candidate) != str(record.get("sha256", "")).upper():
            raise RuntimeError(f"MODEL_FILE_HASH_MISMATCH:{record['path']}")
    license_path = model_dir / "LICENSE.txt"
    if _sha256(license_path) != str(acceptance.get("license_sha256", "")).upper():
        raise RuntimeError("MODEL_LICENSE_HASH_MISMATCH")
    return model_dir, manifest


def _save_pcm16_exclusive(path: Path, waveform: Any, sample_rate: int) -> None:
    import numpy as np

    samples = np.asarray(waveform, dtype=np.float32).reshape(-1)
    if samples.size == 0 or not np.isfinite(samples).all():
        raise ValueError("XTTS-v2 returned empty or non-finite audio")
    pcm = np.round(np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(f"Refusing to overwrite output: {path}")
    temporary = path.with_name(f".{path.stem}.{uuid.uuid4().hex}.wav")
    try:
        with wave.open(str(temporary), "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(sample_rate)
            writer.writeframes(pcm.tobytes())
        # Windows _commit requires a writable descriptor. This is the targeted
        # post-save fix retained from the approved English retry.
        with temporary.open("r+b") as handle:
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            raise FileExistsError(f"Refusing to overwrite output: {path}")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


class PeakMemoryMonitor:
    def __init__(self) -> None:
        self.peak_bytes = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="xtts-peak-memory", daemon=True)

    def _sample(self) -> None:
        try:
            import psutil

            process = psutil.Process(os.getpid())
            total = process.memory_info().rss
            for child in process.children(recursive=True):
                try:
                    total += child.memory_info().rss
                except psutil.Error:
                    pass
            self.peak_bytes = max(self.peak_bytes, total)
        except Exception:
            pass

    def _run(self) -> None:
        while not self._stop.wait(0.05):
            self._sample()

    def __enter__(self) -> "PeakMemoryMonitor":
        self._sample()
        self._thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)
        self._sample()


def _package_version(distribution: str) -> str:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return "MISSING"


def _environment_report(torch: Any | None = None, torchaudio: Any | None = None, transformers: Any | None = None) -> dict[str, Any]:
    return {
        "python_executable": str(Path(sys.executable).resolve()),
        "python_version": ".".join(str(value) for value in sys.version_info[:3]),
        "virtual_environment": str(Path(sys.prefix).resolve()),
        "coqui_tts_version": _package_version("coqui-tts"),
        "torch_version": getattr(torch, "__version__", _package_version("torch")),
        "torchaudio_version": getattr(torchaudio, "__version__", _package_version("torchaudio")),
        "transformers_version": getattr(transformers, "__version__", _package_version("transformers")),
        "torch_cuda_available": bool(torch.cuda.is_available()) if torch is not None else False,
        "pid": os.getpid(),
        "cwd": str(Path.cwd().resolve()),
        "model_revision": MODEL_REVISION,
    }


def _assert_environment(report: dict[str, Any]) -> None:
    expected = {
        "python_version": "3.11.15",
        "coqui_tts_version": "0.27.5",
        "torch_version": "2.6.0+cpu",
        "torchaudio_version": "2.6.0+cpu",
        "transformers_version": "4.57.6",
    }
    mismatches = {
        key: {"expected": value, "actual": report.get(key)}
        for key, value in expected.items()
        if report.get(key) != value
    }
    if report.get("torch_cuda_available") is not False:
        mismatches["torch_cuda_available"] = {"expected": False, "actual": report.get("torch_cuda_available")}
    if mismatches:
        raise RuntimeError("XTTS environment mismatch: " + json.dumps(mismatches, sort_keys=True))


def _split_oversized_unit(unit: str, limit: int) -> list[str]:
    unit = unit.strip()
    if len(unit) <= limit:
        return [unit] if unit else []
    clauses = [part.strip() for part in re.split(r"(?<=[,;:])\s+", unit) if part.strip()]
    if len(clauses) == 1:
        clauses = unit.split()
    chunks: list[str] = []
    current = ""
    for clause in clauses:
        candidate = f"{current} {clause}".strip()
        if current and len(candidate) > limit:
            chunks.append(current)
            current = clause
        else:
            current = candidate
        while len(current) > limit:
            cut = current.rfind(" ", 0, limit + 1)
            if cut <= 0:
                cut = limit
            chunks.append(current[:cut].strip())
            current = current[cut:].strip()
    if current:
        chunks.append(current)
    return chunks


def _split_text_without_spacy(text: str, char_limit: int) -> list[str]:
    """Split on natural boundaries without importing optional spaCy.

    XTTS's bundled splitter imports spaCy. The pinned runtime intentionally has
    no spaCy dependency, so long passages are split here and each chunk is sent
    to ``inference(..., enable_text_splitting=False)``.
    """

    limit = max(40, int(char_limit * 0.82))
    normalized = re.sub(r"[\t ]+", " ", text.strip())
    units = [part.strip() for part in re.split(r"(?:\r?\n)+|(?<=[.!?…])\s+", normalized) if part.strip()]
    atomic: list[str] = []
    for unit in units:
        atomic.extend(_split_oversized_unit(unit, limit))
    chunks: list[str] = []
    current = ""
    for unit in atomic:
        candidate = f"{current} {unit}".strip()
        if current and len(candidate) > limit:
            chunks.append(current)
            current = unit
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def _validate_paths(request: dict[str, Any]) -> tuple[list[Path], Path, list[dict[str, Any]]]:
    values = request.get("references")
    if not isinstance(values, list) or not values:
        raise XttsStageError(XTTS_REFERENCE_INVALID, "load_reference", "XTTS-v2 reference is missing")
    references = [Path(str(value)).expanduser().resolve() for value in values]
    expected_records = request.get("reference_records")
    records = expected_records if isinstance(expected_records, list) else []
    for index, reference in enumerate(references):
        if not reference.is_file() or reference.stat().st_size <= 44:
            raise XttsStageError(XTTS_REFERENCE_INVALID, "load_reference", f"Reference is missing or empty: {reference}")
        try:
            with wave.open(str(reference), "rb") as reader:
                if reader.getnframes() <= 0 or reader.getframerate() <= 0:
                    raise ValueError("empty PCM stream")
        except (OSError, EOFError, wave.Error, ValueError) as exc:
            raise XttsStageError(XTTS_REFERENCE_INVALID, "load_reference", f"Reference WAV is invalid: {reference}: {exc}") from exc
        actual_hash = _sha256(reference)
        if index < len(records) and isinstance(records[index], dict):
            expected_hash = str(records[index].get("sha256", "")).upper()
            if expected_hash and actual_hash != expected_hash:
                raise XttsStageError(XTTS_REFERENCE_INVALID, "load_reference", f"Reference hash mismatch: {reference}")
    output = Path(str(request.get("output_path", ""))).expanduser().resolve()
    if not output.name:
        raise XttsStageError(XTTS_OUTPUT_WRITE_FAILED, "write_output", "XTTS-v2 output path is empty")
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise XttsStageError(XTTS_OUTPUT_WRITE_FAILED, "write_output", f"Refusing to overwrite output: {output}")
    probe = output.parent / f".xtts-write-probe-{uuid.uuid4().hex}.tmp"
    try:
        with probe.open("xb") as handle:
            handle.write(b"ok")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise XttsStageError(XTTS_OUTPUT_WRITE_FAILED, "write_output", f"Output directory is not writable: {output.parent}: {exc}") from exc
    finally:
        if probe.exists():
            probe.unlink()
    actual_records = [
        {"path": str(path), "sha256": _sha256(path), "size_bytes": path.stat().st_size}
        for path in references
    ]
    return references, output, actual_records


@dataclass
class ConditioningEntry:
    gpt_cond_latent: Any
    speaker_embedding: Any
    created_at: float


class XttsRuntime:
    def __init__(self) -> None:
        self.runtime_root = Path(__file__).resolve().parents[1]
        self.model_dir: Path | None = None
        self.manifest: dict[str, Any] = {}
        self.config: Any = None
        self.model: Any = None
        self.np: Any = None
        self.torch: Any = None
        self.environment: dict[str, Any] = _environment_report()
        self.model_load_count = 0
        self.model_load_elapsed_seconds = 0.0
        self.conditioning_cache: dict[str, ConditioningEntry] = {}

    @staticmethod
    def _stage(job_id: str, name: str, progress: float, **extra: Any) -> None:
        _emit({"schema_version": 1, "type": "stage", "job_id": job_id, "name": name, "progress": progress, **extra})

    def load_model(self, job_id: str) -> float:
        if self.model is not None:
            self._stage(job_id, "model_ready", 0.38, model_reused=True, model_load_count=self.model_load_count)
            return 0.0
        started = time.perf_counter()
        self._stage(job_id, "worker_started", 0.18, worker_pid=os.getpid())
        self._stage(job_id, "import_dependencies", 0.21)
        try:
            os.environ["TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD"] = "1"
            import numpy as np
            import torch
            import torchaudio
            import transformers
            from TTS.tts.configs.xtts_config import XttsConfig
            from TTS.tts.models.xtts import Xtts
            self.np = np
            self.torch = torch
            self.environment = _environment_report(torch, torchaudio, transformers)
            _assert_environment(self.environment)
        except Exception as exc:
            raise XttsStageError(XTTS_IMPORT_FAILED, "import_dependencies", str(exc)) from exc
        self._stage(job_id, "resolve_model", 0.24)
        try:
            self.model_dir, self.manifest = _verify_model_and_license(self.runtime_root)
        except Exception as exc:
            raise XttsStageError(XTTS_MODEL_NOT_FOUND, "resolve_model", str(exc)) from exc
        self._stage(job_id, "load_config", 0.27)
        try:
            config = XttsConfig()
            config.load_json(str(self.model_dir / "config.json"))
        except Exception as exc:
            raise XttsStageError(XTTS_MODEL_LOAD_FAILED, "load_config", str(exc)) from exc
        self._stage(job_id, "load_checkpoint", 0.30)
        try:
            model = Xtts.init_from_config(config)
            model.load_checkpoint(config, checkpoint_dir=str(self.model_dir), eval=True, use_deepspeed=False)
            self._stage(job_id, "move_model_cpu", 0.35)
            model.to("cpu")
        except Exception as exc:
            code = XTTS_OUT_OF_MEMORY if _is_oom(exc) else XTTS_MODEL_LOAD_FAILED
            raise XttsStageError(code, "load_checkpoint", str(exc)) from exc
        self.config = config
        self.model = model
        self.model_load_count += 1
        self.model_load_elapsed_seconds = time.perf_counter() - started
        self._stage(job_id, "model_ready", 0.38, model_reused=False, model_load_count=self.model_load_count)
        return self.model_load_elapsed_seconds

    def _conditioning_key(self, request: dict[str, Any], records: list[dict[str, Any]]) -> str:
        payload = {
            "profile_id": str(request.get("profile_id", "")),
            "profile_revision": int(request.get("profile_revision", 0)),
            "references": [record["sha256"] for record in records],
            "model_revision": MODEL_REVISION,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest().upper()

    def condition(self, request: dict[str, Any], references: list[Path], records: list[dict[str, Any]], job_id: str) -> tuple[Any, Any, float, bool, str]:
        key = self._conditioning_key(request, records)
        entry = self.conditioning_cache.get(key)
        if entry is not None:
            self._stage(job_id, "conditioning_ready", 0.52, conditioning_cache_hit=True)
            return entry.gpt_cond_latent, entry.speaker_embedding, 0.0, True, key
        self._stage(job_id, "load_profile", 0.42, profile_id=str(request.get("profile_id", "")))
        self._stage(job_id, "load_reference", 0.46, references=[str(path) for path in references])
        self._stage(job_id, "compute_conditioning", 0.49)
        started = time.perf_counter()
        try:
            assert self.model is not None and self.torch is not None
            with self.torch.inference_mode():
                gpt_cond_latent, speaker_embedding = self.model.get_conditioning_latents(
                    audio_path=[str(path) for path in references]
                )
        except Exception as exc:
            code = XTTS_OUT_OF_MEMORY if _is_oom(exc) else XTTS_CONDITIONING_FAILED
            raise XttsStageError(code, "compute_conditioning", str(exc)) from exc
        elapsed = time.perf_counter() - started
        self.conditioning_cache[key] = ConditioningEntry(gpt_cond_latent, speaker_embedding, time.time())
        self._stage(job_id, "conditioning_ready", 0.52, conditioning_cache_hit=False)
        return gpt_cond_latent, speaker_embedding, elapsed, False, key

    def handle(self, request: Any) -> dict[str, Any]:
        request_started = time.perf_counter()
        if not isinstance(request, dict) or request.get("schema_version") != 1:
            raise XttsStageError(XTTS_PROFILE_LOAD_FAILED, "load_profile", "Invalid XTTS-v2 request schema")
        action = str(request.get("action", ""))
        if action not in {"probe", "synthesize"}:
            raise XttsStageError(XTTS_PROFILE_LOAD_FAILED, "load_profile", f"Unsupported XTTS-v2 action: {action}")
        job_id = str(request.get("job_id") or uuid.uuid4())
        if str(request.get("device", "cpu")).lower() != "cpu":
            raise XttsStageError(XTTS_MODEL_LOAD_FAILED, "load_model", "XTTS-v2 worker is CPU-only")
        text = str(request.get("text", "")).strip()
        if action == "synthesize" and not text:
            raise XttsStageError(XTTS_INFERENCE_FAILED, "synthesize", "XTTS-v2 text is empty")
        references, output, reference_records = _validate_paths(request)
        language = str(request.get("language", "")).strip().lower().replace("_", "-")
        load_elapsed = self.load_model(job_id)
        assert self.model_dir is not None and self.model is not None and self.config is not None
        config_payload = _read_json(self.model_dir / "config.json")
        languages = [str(value).strip().lower().replace("_", "-") for value in config_payload.get("languages", []) if isinstance(value, str)]
        if language not in languages:
            raise XttsStageError(XTTS_PROFILE_LOAD_FAILED, "load_profile", f"Unsupported XTTS-v2 language: {language}")
        gpt_cond_latent, speaker_embedding, conditioning_elapsed, cache_hit, cache_key = self.condition(
            request, references, reference_records, job_id
        )
        common = {
            "worker_pid": os.getpid(),
            "model_load_count": self.model_load_count,
            "model_load_elapsed_seconds": load_elapsed,
            "conditioning_elapsed_seconds": conditioning_elapsed,
            "conditioning_cache_hit": cache_hit,
            "conditioning_cache_key": cache_key,
            "model_id": MODEL_ID,
            "model_revision": MODEL_REVISION,
            "model_manifest_sha256": _sha256(self.model_dir / "model_manifest.json"),
            "package_revision": "coqui-tts==0.27.5",
            "environment": self.environment,
            "reference_records": reference_records,
            "license_id": LICENSE_ID,
            "license_url": LICENSE_URL,
            "license_scope": "research_personal_poc_noncommercial",
            "manifest_total_size_bytes": self.manifest.get("total_size_bytes"),
        }
        if action == "probe":
            return {**common, "probe": "Pass", "elapsed_seconds": time.perf_counter() - request_started}

        speed = float(request.get("speed", 1.0))
        seed = int(request.get("seed", 42))
        language_key = language.split("-")[0]
        char_limit = int(self.model.tokenizer.char_limits[language_key])
        chunks = _split_text_without_spacy(text, char_limit)
        if not chunks:
            raise XttsStageError(XTTS_INFERENCE_FAILED, "synthesize", "XTTS-v2 text produced no chunks")
        self._stage(job_id, "synthesize", 0.58, text_chunks=len(chunks))
        synthesis_started = time.perf_counter()
        waveforms: list[Any] = []
        try:
            self.torch.manual_seed(seed)
            with self.torch.inference_mode():
                for index, chunk in enumerate(chunks, 1):
                    result = self.model.inference(
                        chunk,
                        language,
                        gpt_cond_latent,
                        speaker_embedding,
                        temperature=0.65,
                        length_penalty=1.0,
                        repetition_penalty=2.0,
                        top_k=50,
                        top_p=0.8,
                        do_sample=True,
                        speed=speed,
                        enable_text_splitting=False,
                    )
                    waveforms.append(self.np.asarray(result["wav"], dtype=self.np.float32).reshape(-1))
                    self._stage(job_id, "synthesize", 0.58 + 0.25 * index / len(chunks), text_chunk=index, text_chunks=len(chunks))
            waveform = self.np.concatenate(waveforms)
        except Exception as exc:
            code = XTTS_OUT_OF_MEMORY if _is_oom(exc) else XTTS_INFERENCE_FAILED
            raise XttsStageError(code, "synthesize", str(exc)) from exc
        synthesis_elapsed = time.perf_counter() - synthesis_started
        self._stage(job_id, "write_output", 0.86)
        write_started = time.perf_counter()
        try:
            _save_pcm16_exclusive(output, waveform, SAMPLE_RATE)
        except Exception as exc:
            raise XttsStageError(XTTS_OUTPUT_WRITE_FAILED, "write_output", str(exc)) from exc
        write_elapsed = time.perf_counter() - write_started
        self._stage(job_id, "completed", 1.0)
        return {
            **common,
            "synthesis_elapsed_seconds": synthesis_elapsed,
            "output_write_elapsed_seconds": write_elapsed,
            "elapsed_seconds": time.perf_counter() - request_started,
            "seed": seed,
            "speed": speed,
            "text_chunks": len(chunks),
            "inference_parameters": {
                "temperature": 0.65,
                "length_penalty": 1.0,
                "repetition_penalty": 2.0,
                "top_k": 50,
                "top_p": 0.8,
                "do_sample": True,
                "enable_text_splitting": False,
                "splitter": "frameextract_stdlib_v1",
            },
        }

    def close(self) -> None:
        self.conditioning_cache.clear()
        self.model = None
        self.config = None
        self.np = None
        self.torch = None
        gc.collect()


def _is_oom(exc: BaseException) -> bool:
    text = str(exc).casefold()
    return isinstance(exc, MemoryError) or "out of memory" in text or "not enough memory" in text


def _failure_payload(exc: BaseException, *, job_id: str, stage: str = "worker_started", environment: dict[str, Any] | None = None) -> dict[str, Any]:
    if isinstance(exc, XttsStageError):
        code = exc.code
        stage = exc.stage
    elif _is_oom(exc):
        code = XTTS_OUT_OF_MEMORY
    else:
        code = XTTS_INFERENCE_FAILED
    return {
        "schema_version": 1,
        "type": "result",
        "status": "failed",
        "job_id": job_id,
        "error": {
            "code": code,
            "message": str(exc),
            "details": {
                "exception_type": type(exc).__name__,
                "traceback": traceback.format_exc(),
                "stage": stage,
                "worker_pid": os.getpid(),
                "environment": environment or _environment_report(),
            },
        },
    }


def _execute(runtime: XttsRuntime, request: Any) -> tuple[int, dict[str, Any]]:
    job_id = str(request.get("job_id", "")) if isinstance(request, dict) else ""
    try:
        with PeakMemoryMonitor() as memory:
            metrics = runtime.handle(request)
        metrics["peak_ram_gib"] = memory.peak_bytes / (1024**3)
        payload = {"schema_version": 1, "type": "result", "status": "success", "job_id": job_id, "metrics": metrics}
        _emit(payload)
        return 0, payload
    except Exception as exc:
        payload = _failure_payload(exc, job_id=job_id, environment=runtime.environment)
        _emit(payload)
        return 2, payload


def _persistent_main() -> int:
    runtime = XttsRuntime()
    seen_job_ids: set[str] = set()
    final_code = 0
    try:
        # Read synchronously. A background TextIO reader thread can interact
        # badly with Torch/OpenMP on Windows after checkpoint load. The GUI
        # owns the 15-minute timer and closes this channel on timeout.
        for line in sys.stdin:
            if not line.strip():
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError as exc:
                payload = _failure_payload(exc, job_id="", stage="worker_started", environment=runtime.environment)
                _emit(payload)
                final_code = 2
                continue
            if isinstance(request, dict) and request.get("action") == "shutdown":
                _emit({"schema_version": 1, "type": "lifecycle", "status": "shutdown", "worker_pid": os.getpid(), "model_load_count": runtime.model_load_count})
                break
            job_id = str(request.get("job_id", "")) if isinstance(request, dict) else ""
            if not job_id or job_id in seen_job_ids:
                exc = XttsStageError(DUPLICATE_JOB_REJECTED, "worker_started", f"Duplicate XTTS job rejected: {job_id}")
                _emit(_failure_payload(exc, job_id=job_id, environment=runtime.environment))
                final_code = 2
                continue
            seen_job_ids.add(job_id)
            if request.get("action") == "ping":
                _emit(
                    {
                        "schema_version": 1,
                        "type": "result",
                        "status": "success",
                        "job_id": job_id,
                        "metrics": {
                            "worker_pid": os.getpid(),
                            "model_load_count": runtime.model_load_count,
                            "protocol_ping": "Pass",
                        },
                    }
                )
                continue
            code, _payload = _execute(runtime, request)
            final_code = max(final_code, code)
    finally:
        runtime.close()
    return final_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--persistent", action="store_true")
    args = parser.parse_args(argv)
    if args.persistent:
        return _persistent_main()
    runtime = XttsRuntime()
    try:
        try:
            request = json.loads(sys.stdin.read())
        except Exception as exc:
            _emit(_failure_payload(exc, job_id="", environment=runtime.environment))
            return 2
        code, _payload = _execute(runtime, request)
        return code
    finally:
        runtime.close()


if __name__ == "__main__":
    raise SystemExit(main())
