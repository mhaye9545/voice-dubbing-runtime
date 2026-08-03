"""Thin launcher and integrity contract for isolated Demucs separation.

The GUI/runtime parent imports this module, so it intentionally uses only the
Python standard library plus the runtime's small error/JSON helpers.  PyTorch,
Demucs, sphn, and psutil are imported only by ``source_separation_worker`` in
the dedicated ``.venv-source-separation`` process.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .errors import CANCELLED, ENGINE_UNAVAILABLE, VoiceRuntimeError
from .io_utils import read_json, sha256_file, write_json_exclusive


ENGINE_ID = "demucs_htdemucs_vocals"
ENGINE_MARKER = "@@SOURCE_SEPARATION|"
PACKAGE_REVISION = "demucs==4.1.0"
PACKAGE_WHEEL_SHA256 = "4916A804702033CE934A6CDFA7E38DDE03F7A7A6E85F41D0120EEFE9E2966758"
LOCK_FILENAME = "requirements-source-separation.lock.txt"
PYTHON_SERIES = "3.11"
TORCH_REVISION = "2.6.0+cpu"
NUMPY_REVISION = "1.26.4"
SPHN_REVISION = "0.2.1"
PSUTIL_REVISION = "7.2.2"
MODEL_ID = "demucs/htdemucs"
MODEL_NAME = "htdemucs"
MODEL_SIGNATURE = "955717e8"
MODEL_FILENAME = "955717e8-8726e21a.th"
MODEL_SIZE_BYTES = 84_141_911
MODEL_SHA256 = "8726E21A993978C7BA086D3872E7608D7D5BFCA646CA4ACA459FFDA844FAA8B4"
MODEL_SOURCE_URL = (
    "https://dl.fbaipublicfiles.com/demucs/hybrid_transformer/"
    "955717e8-8726e21a.th"
)
BAG_FILENAME = "htdemucs.yaml"
BAG_BYTES = b"models: ['955717e8']\n"
BAG_SIZE_BYTES = 21
BAG_SHA256 = "239C445D0B14454D541AD8BD9BB271C9E536D267E8A4625208744CBB2E7BB66C"
SOURCE_SEPARATION_FAILED = "SOURCE_SEPARATION_FAILED"


class ModelManifestError(RuntimeError):
    """Raised before model deserialization when the pinned contract differs."""


def _expected_file_records() -> dict[str, tuple[int, str]]:
    return {
        MODEL_FILENAME: (MODEL_SIZE_BYTES, MODEL_SHA256),
        BAG_FILENAME: (BAG_SIZE_BYTES, BAG_SHA256),
    }


def verify_model_manifest(model_dir: Path) -> dict[str, Any]:
    """Verify manifest identity, path containment, size, and full SHA-256.

    This gate is deliberately safe to call in both parent and child processes.
    No ML package is imported and the legacy pickle checkpoint is not opened by
    Torch until this function succeeds.
    """

    root = model_dir.resolve()
    manifest_path = root / "model_manifest.json"
    if not manifest_path.is_file() or manifest_path.stat().st_size == 0:
        raise ModelManifestError("MODEL_MANIFEST_MISSING")
    allowed_names = {MODEL_FILENAME, BAG_FILENAME, "model_manifest.json"}
    actual_names: set[str] = set()
    for child in root.iterdir():
        if child.is_symlink() or not child.is_file():
            raise ModelManifestError(f"MODEL_DIRECTORY_UNSAFE_ENTRY:{child.name}")
        actual_names.add(child.name)
    if actual_names != allowed_names:
        raise ModelManifestError(
            "MODEL_DIRECTORY_FILE_SET_MISMATCH:"
            f"expected={sorted(allowed_names)}:actual={sorted(actual_names)}"
        )
    payload = read_json(manifest_path)
    if not isinstance(payload, dict):
        raise ModelManifestError("MODEL_MANIFEST_INVALID")
    expected_values = {
        "schema_version": 1,
        "engine_id": ENGINE_ID,
        "model_id": MODEL_ID,
        "model_name": MODEL_NAME,
        "model_signature": MODEL_SIGNATURE,
        "model_format": "legacy_torch_checkpoint",
        "model_source_url": MODEL_SOURCE_URL,
        "model_filename_checksum_prefix": "8726e21a",
        "license_id": "MIT",
        "license_url": "https://github.com/adefossez/demucs/blob/v4.1.0/LICENSE",
        "package_revision": PACKAGE_REVISION,
        "package_wheel_sha256": PACKAGE_WHEEL_SHA256,
        "provision_status": "HASH_VERIFIED",
    }
    mismatches = {
        key: {"expected": value, "actual": payload.get(key)}
        for key, value in expected_values.items()
        if payload.get(key) != value
    }
    if mismatches:
        raise ModelManifestError(f"MODEL_MANIFEST_IDENTITY_MISMATCH:{mismatches}")
    runtime = payload.get("runtime_contract")
    expected_runtime = {
        "python_series": PYTHON_SERIES,
        "torch": TORCH_REVISION,
        "numpy": NUMPY_REVISION,
        "sphn": SPHN_REVISION,
        "psutil": PSUTIL_REVISION,
        "torchaudio": None,
        "device": "cpu",
    }
    if not isinstance(runtime, dict) or any(
        runtime.get(key) != value for key, value in expected_runtime.items()
    ):
        raise ModelManifestError("MODEL_MANIFEST_RUNTIME_MISMATCH")
    inference = payload.get("inference_contract")
    expected_inference = {
        "two_stems": "vocals",
        "shifts": 0,
        "jobs": 0,
        "split": True,
        "overlap": 0.25,
        "output_format": "pcm_s16le_wav",
    }
    if not isinstance(inference, dict) or any(
        inference.get(key) != value for key, value in expected_inference.items()
    ):
        raise ModelManifestError("MODEL_MANIFEST_INFERENCE_MISMATCH")
    lock_record = payload.get("requirements_lock")
    if not isinstance(lock_record, dict) or lock_record.get("path") != LOCK_FILENAME:
        raise ModelManifestError("MODEL_MANIFEST_LOCK_RECORD_INVALID")
    lock_path = (root.parents[2] / LOCK_FILENAME).resolve()
    try:
        lock_path.relative_to(root.parents[2])
    except ValueError as exc:
        raise ModelManifestError("MODEL_MANIFEST_LOCK_PATH_ESCAPE") from exc
    lock_size = lock_record.get("size_bytes")
    lock_hash = str(lock_record.get("sha256", "")).upper()
    if not isinstance(lock_size, int) or lock_size <= 0 or len(lock_hash) != 64:
        raise ModelManifestError("MODEL_MANIFEST_LOCK_RECORD_INVALID")
    if not lock_path.is_file() or lock_path.stat().st_size != lock_size:
        raise ModelManifestError("DEPENDENCY_LOCK_MISSING_OR_SIZE_MISMATCH")
    if sha256_file(lock_path) != lock_hash:
        raise ModelManifestError("DEPENDENCY_LOCK_HASH_MISMATCH")
    records = payload.get("files")
    if not isinstance(records, list):
        raise ModelManifestError("MODEL_MANIFEST_FILES_INVALID")
    expected_files = _expected_file_records()
    seen: set[str] = set()
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("path"), str):
            raise ModelManifestError("MODEL_MANIFEST_FILE_RECORD_INVALID")
        relative = record["path"]
        if relative in seen or relative not in expected_files:
            raise ModelManifestError(f"MODEL_MANIFEST_UNEXPECTED_FILE:{relative}")
        seen.add(relative)
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise ModelManifestError("MODEL_MANIFEST_PATH_ESCAPE") from exc
        expected_size, expected_hash = expected_files[relative]
        if record.get("size_bytes") != expected_size:
            raise ModelManifestError(f"MODEL_MANIFEST_SIZE_MISMATCH:{relative}")
        if str(record.get("sha256", "")).upper() != expected_hash:
            raise ModelManifestError(f"MODEL_MANIFEST_HASH_MISMATCH:{relative}")
        if not candidate.is_file() or candidate.stat().st_size != expected_size:
            raise ModelManifestError(f"MODEL_FILE_MISSING_OR_SIZE_MISMATCH:{relative}")
        if sha256_file(candidate) != expected_hash:
            raise ModelManifestError(f"MODEL_FILE_HASH_MISMATCH:{relative}")
    if seen != set(expected_files):
        missing = sorted(set(expected_files) - seen)
        raise ModelManifestError(f"MODEL_MANIFEST_FILES_MISSING:{missing}")
    return payload


class SourceSeparationRunner:
    """Launch one offline, CPU-only vocals separation job in an isolated child."""

    def __init__(
        self,
        runtime_root: Path,
        *,
        python_executable: Path | None = None,
        model_dir: Path | None = None,
        worker_module: str = "voice_dubbing_runtime.source_separation_worker",
        timeout_seconds: float = 1800.0,
    ) -> None:
        self.runtime_root = runtime_root.resolve()
        self.python = (
            python_executable.resolve()
            if python_executable is not None
            else self.runtime_root
            / ".venv-source-separation"
            / "Scripts"
            / "python.exe"
        )
        self.model_dir = (
            model_dir.resolve()
            if model_dir is not None
            else self.runtime_root / "models" / "source_separation" / "htdemucs"
        )
        self.worker_module = worker_module
        self.timeout_seconds = float(timeout_seconds)

    @staticmethod
    def _cancelled(cancel_token: Any) -> bool:
        checker = getattr(cancel_token, "is_cancelled", None)
        return bool(checker()) if checker else False

    @staticmethod
    def _kill_tree(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        else:
            process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)

    @staticmethod
    def _cleanup_descendant(path: Path, parent: Path) -> None:
        if not path.exists() and not path.is_symlink():
            return
        resolved_parent = parent.resolve()
        resolved = path.resolve()
        try:
            resolved.relative_to(resolved_parent)
        except ValueError:
            return
        if path.is_symlink() or path.is_file():
            path.unlink(missing_ok=True)
        else:
            shutil.rmtree(path)

    @staticmethod
    def _captured_text(value: str | bytes | None) -> str:
        """Normalize TimeoutExpired output, which may be bytes in text mode."""

        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return value

    def _verify_launcher(self) -> dict[str, Any]:
        if not self.python.is_file() or self.python.stat().st_size == 0:
            raise VoiceRuntimeError(
                ENGINE_UNAVAILABLE,
                "The isolated Demucs Python runtime is not provisioned.",
                {"engine": ENGINE_ID, "python": str(self.python)},
            )
        try:
            return verify_model_manifest(self.model_dir)
        except (OSError, ValueError, ModelManifestError) as exc:
            raise VoiceRuntimeError(
                ENGINE_UNAVAILABLE,
                "The pinned Demucs htdemucs model is unavailable or failed integrity checks.",
                {"engine": ENGINE_ID, "model_dir": str(self.model_dir), "reason": str(exc)},
            ) from exc

    def separate_vocals(
        self,
        *,
        input_path: Path,
        output_path: Path,
        work_dir: Path,
        progress: Callable[[str, float], None],
        cancel_token: Any,
    ) -> dict[str, Any]:
        """Separate one candidate WAV and atomically commit a raw vocals WAV."""

        manifest = self._verify_launcher()
        source = input_path.resolve()
        output = output_path.resolve()
        work_root = work_dir.resolve()
        if not source.is_file() or source.stat().st_size == 0:
            raise VoiceRuntimeError(
                SOURCE_SEPARATION_FAILED,
                "Source-separation input is missing or empty.",
                {"input_path": str(source)},
            )
        if output.exists():
            raise FileExistsError(f"Refusing to overwrite separated vocals: {output}")
        if self._cancelled(cancel_token):
            raise VoiceRuntimeError(CANCELLED, "Source separation was cancelled before launch.")
        output.parent.mkdir(parents=True, exist_ok=True)
        work_root.mkdir(parents=True, exist_ok=True)
        request_id = uuid.uuid4().hex
        job_temp = work_root / f".source-separation-{request_id}"
        output_temp = output.with_name(f".{output.stem}.{request_id}.demucs.wav")
        diagnostic_log = work_root / f"source_separation_{request_id}.log"
        job_temp.mkdir(parents=False, exist_ok=False)
        request = {
            "schema_version": 1,
            "action": "separate_vocals",
            "request_id": request_id,
            "input_path": str(source),
            "output_path": str(output),
            "temporary_output_path": str(output_temp),
            "work_dir": str(job_temp),
            "model_dir": str(self.model_dir),
            "model_name": MODEL_NAME,
            "model_signature": MODEL_SIGNATURE,
            "device": "cpu",
            "two_stems": "vocals",
            "shifts": 0,
            "jobs": 0,
            "split": True,
            "overlap": 0.25,
        }
        environment = dict(os.environ)
        environment.update(
            {
                "PYTHONUTF8": "1",
                "PYTHONIOENCODING": "utf-8",
                "CUDA_VISIBLE_DEVICES": "",
                "HF_HUB_OFFLINE": "1",
                "TORCH_HOME": str(
                    self.runtime_root / "models" / "source_separation" / "torch_cache"
                ),
                "HF_HOME": str(
                    self.runtime_root / "models" / "source_separation" / "hf_cache"
                ),
            }
        )
        progress("source_separation_load_model", 0.30)
        process: subprocess.Popen[str] | None = None
        succeeded = False
        started = time.perf_counter()
        stdout = ""
        stderr = ""
        diagnostic_status = "failed"

        def persist_diagnostic(error: dict[str, Any] | None = None) -> str | None:
            payload = {
                "schema_version": 1,
                "engine_id": ENGINE_ID,
                "request_id": request_id,
                "status": diagnostic_status,
                "exit_code": process.returncode if process is not None else None,
                "elapsed_seconds": time.perf_counter() - started,
                "worker_module": self.worker_module,
                "model_id": MODEL_ID,
                "model_name": MODEL_NAME,
                "model_signature": MODEL_SIGNATURE,
                "input_path": str(source),
                "output_path": str(output),
                "stdout_tail": stdout[-12000:],
                "stderr_tail": stderr[-12000:],
                "error": error,
            }
            try:
                write_json_exclusive(diagnostic_log, payload)
            except OSError as exc:
                return f"{type(exc).__name__}: {exc}"
            return None

        try:
            process = subprocess.Popen(
                [str(self.python), "-u", "-m", self.worker_module],
                cwd=str(self.runtime_root),
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            encoded_request: str | None = json.dumps(request, ensure_ascii=False)
            while True:
                if self._cancelled(cancel_token):
                    self._kill_tree(process)
                    diagnostic_status = "cancelled"
                    raise VoiceRuntimeError(CANCELLED, "Source separation was cancelled.")
                elapsed = time.perf_counter() - started
                if elapsed >= self.timeout_seconds:
                    self._kill_tree(process)
                    diagnostic_status = "timeout"
                    raise VoiceRuntimeError(
                        SOURCE_SEPARATION_FAILED,
                        "Source-separation worker timed out.",
                        {"reason": "WORKER_TIMEOUT", "timeout_seconds": self.timeout_seconds},
                    )
                try:
                    stdout, stderr = process.communicate(
                        input=encoded_request,
                        timeout=min(0.2, max(0.01, self.timeout_seconds - elapsed)),
                    )
                    break
                except subprocess.TimeoutExpired as exc:
                    if exc.output is not None:
                        stdout = self._captured_text(exc.output)
                    if exc.stderr is not None:
                        stderr = self._captured_text(exc.stderr)
                    encoded_request = None
            marker_payload: dict[str, Any] | None = None
            for line in stdout.splitlines():
                if not line.startswith(ENGINE_MARKER):
                    continue
                try:
                    candidate = json.loads(line[len(ENGINE_MARKER) :])
                except json.JSONDecodeError:
                    continue
                if isinstance(candidate, dict):
                    marker_payload = candidate
            if marker_payload is None:
                raise VoiceRuntimeError(
                    SOURCE_SEPARATION_FAILED,
                    "The isolated Demucs worker failed.",
                    {
                        "exit_code": process.returncode,
                        "stderr_tail": stderr[-8000:],
                        "stdout_tail": stdout[-4000:],
                    },
                )
            if marker_payload.get("status") != "success":
                error = (
                    marker_payload.get("error")
                    if isinstance(marker_payload.get("error"), dict)
                    else {}
                )
                details = (
                    dict(error.get("details"))
                    if isinstance(error.get("details"), dict)
                    else {}
                )
                details.update(
                    {
                        "exit_code": process.returncode,
                        "stderr_tail": stderr[-8000:],
                        "stdout_tail": stdout[-4000:],
                    }
                )
                raise VoiceRuntimeError(
                    str(error.get("code") or SOURCE_SEPARATION_FAILED),
                    str(error.get("message") or "Demucs vocals separation failed."),
                    details,
                )
            if process.returncode != 0:
                raise VoiceRuntimeError(
                    SOURCE_SEPARATION_FAILED,
                    "The isolated Demucs worker exited non-zero after a success marker.",
                    {
                        "exit_code": process.returncode,
                        "stderr_tail": stderr[-8000:],
                        "stdout_tail": stdout[-4000:],
                    },
                )
            if not output.is_file() or output.stat().st_size <= 44:
                raise VoiceRuntimeError(
                    SOURCE_SEPARATION_FAILED,
                    "Demucs reported success without a non-empty vocals WAV.",
                    {"output_path": str(output)},
                )
            metrics = marker_payload.get("metrics")
            if not isinstance(metrics, dict):
                raise VoiceRuntimeError(
                    SOURCE_SEPARATION_FAILED,
                    "Demucs worker returned invalid metrics.",
                )
            progress("source_separation_complete", 0.70)
            diagnostic_status = "success"
            log_error = persist_diagnostic()
            if log_error is not None:
                diagnostic_status = "failed"
                raise VoiceRuntimeError(
                    SOURCE_SEPARATION_FAILED,
                    "Demucs succeeded but its technical diagnostic log could not be saved.",
                    {
                        "diagnostic_log_path": str(diagnostic_log),
                        "diagnostic_log_write_error": log_error,
                    },
                )
            succeeded = True
            return {
                **metrics,
                "elapsed_seconds": time.perf_counter() - started,
                "diagnostic_log_path": str(diagnostic_log),
                "model_manifest_sha256": sha256_file(
                    self.model_dir / "model_manifest.json"
                ),
                "requirements_lock_sha256": (
                    manifest.get("requirements_lock", {}).get("sha256")
                    if isinstance(manifest.get("requirements_lock"), dict)
                    else None
                ),
            }
        except VoiceRuntimeError as exc:
            exc.details.setdefault("diagnostic_log_path", str(diagnostic_log))
            log_error = None
            if not diagnostic_log.exists():
                log_error = persist_diagnostic(
                    {"code": exc.code, "message": exc.message, "details": exc.details}
                )
            if log_error is not None:
                exc.details["diagnostic_log_write_error"] = log_error
            raise
        except Exception as exc:
            details = {
                "reason": f"{type(exc).__name__}: {exc}",
                "diagnostic_log_path": str(diagnostic_log),
            }
            log_error = persist_diagnostic(
                {
                    "code": SOURCE_SEPARATION_FAILED,
                    "message": "The isolated Demucs launcher failed.",
                    "details": details,
                }
            )
            if log_error is not None:
                details["diagnostic_log_write_error"] = log_error
            raise VoiceRuntimeError(
                SOURCE_SEPARATION_FAILED,
                "The isolated Demucs launcher failed.",
                details,
            ) from exc
        finally:
            if process is not None and process.poll() is None:
                self._kill_tree(process)
            output_temp.unlink(missing_ok=True)
            self._cleanup_descendant(job_temp, work_root)
            if not succeeded and output.exists():
                output.unlink(missing_ok=True)


__all__ = [
    "ENGINE_ID",
    "MODEL_ID",
    "MODEL_NAME",
    "MODEL_SIGNATURE",
    "SourceSeparationRunner",
    "verify_model_manifest",
]
