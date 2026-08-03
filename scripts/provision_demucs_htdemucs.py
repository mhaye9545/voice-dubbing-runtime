"""Provision the pinned official htdemucs checkpoint into an offline local repo.

Run this script only with ``.venv-source-separation`` after generating the
hash-locked dependency file.  It never overwrites an existing model directory
and never deserializes the legacy Torch checkpoint.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import shutil
import sys
import urllib.request
import uuid
from pathlib import Path
from typing import Any, BinaryIO


ENGINE_ID = "demucs_htdemucs_vocals"
MODEL_ID = "demucs/htdemucs"
MODEL_NAME = "htdemucs"
MODEL_SIGNATURE = "955717e8"
MODEL_FILENAME = "955717e8-8726e21a.th"
MODEL_URL = (
    "https://dl.fbaipublicfiles.com/demucs/hybrid_transformer/"
    "955717e8-8726e21a.th"
)
MODEL_SIZE_BYTES = 84_141_911
MODEL_SHA256 = "8726E21A993978C7BA086D3872E7608D7D5BFCA646CA4ACA459FFDA844FAA8B4"
BAG_FILENAME = "htdemucs.yaml"
BAG_BYTES = b"models: ['955717e8']\n"
BAG_SHA256 = "239C445D0B14454D541AD8BD9BB271C9E536D267E8A4625208744CBB2E7BB66C"
PACKAGE_REVISION = "demucs==4.1.0"
PACKAGE_WHEEL_SHA256 = "4916A804702033CE934A6CDFA7E38DDE03F7A7A6E85F41D0120EEFE9E2966758"
EXPECTED_PACKAGES = {
    "demucs": "4.1.0",
    "torch": "2.6.0+cpu",
    "numpy": "1.26.4",
    "sphn": "0.2.1",
    "psutil": "7.2.2",
}
LOCK_FILENAME = "requirements-source-separation.lock.txt"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _file_record(path: Path, root: Path) -> dict[str, Any]:
    return {
        "path": path.relative_to(root).as_posix(),
        "size_bytes": path.stat().st_size,
        "sha256": _sha256(path),
    }


def _verify_runtime() -> dict[str, str]:
    if (sys.version_info.major, sys.version_info.minor) != (3, 11):
        raise RuntimeError("Source-separation provisioning requires Python 3.11")
    installed: dict[str, str] = {}
    for name, expected in EXPECTED_PACKAGES.items():
        try:
            actual = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError as exc:
            raise RuntimeError(f"Missing pinned source-separation package: {name}") from exc
        if actual != expected:
            raise RuntimeError(f"Package mismatch for {name}: expected {expected}, got {actual}")
        installed[name] = actual
    try:
        torchaudio = importlib.metadata.version("torchaudio")
    except importlib.metadata.PackageNotFoundError:
        pass
    else:
        raise RuntimeError(f"torchaudio must not be installed in this environment: {torchaudio}")
    return installed


def _copy_stream(source: BinaryIO, destination: Path) -> None:
    with destination.open("xb") as handle:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            handle.write(chunk)
        handle.flush()
        os.fsync(handle.fileno())


def _download_model(destination: Path) -> None:
    request = urllib.request.Request(
        MODEL_URL,
        headers={"User-Agent": "FrameExtract-VoiceDubbing-Demucs-Provision/1"},
    )
    with urllib.request.urlopen(request, timeout=120) as response:  # nosec B310
        _copy_stream(response, destination)


def _write_json_exclusive(path: Path, payload: dict[str, Any]) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def _safe_remove_stage(stage: Path, parent: Path) -> None:
    if not stage.exists():
        return
    resolved = stage.resolve()
    resolved.relative_to(parent.resolve())
    if not stage.name.startswith(".htdemucs.staging-"):
        raise RuntimeError(f"Refusing to remove unexpected staging path: {stage}")
    shutil.rmtree(stage)


def main() -> int:
    runtime_root = Path(__file__).resolve().parents[1]
    lock_path = runtime_root / LOCK_FILENAME
    if not lock_path.is_file() or lock_path.stat().st_size == 0:
        raise RuntimeError(f"Hash-locked dependencies are required before model provisioning: {lock_path}")
    packages = _verify_runtime()
    model_parent = runtime_root / "models" / "source_separation"
    final = model_parent / MODEL_NAME
    if final.exists():
        raise FileExistsError(f"Refusing to overwrite existing model directory: {final}")
    model_parent.mkdir(parents=True, exist_ok=True)
    stage = model_parent / f".htdemucs.staging-{uuid.uuid4().hex}"
    stage.mkdir(parents=False, exist_ok=False)
    try:
        partial = stage / f".{MODEL_FILENAME}.partial"
        _download_model(partial)
        if partial.stat().st_size != MODEL_SIZE_BYTES:
            raise RuntimeError(
                f"Model size mismatch: expected {MODEL_SIZE_BYTES}, got {partial.stat().st_size}"
            )
        if _sha256(partial) != MODEL_SHA256:
            raise RuntimeError("Model SHA-256 mismatch")
        model_path = stage / MODEL_FILENAME
        os.replace(partial, model_path)
        bag_path = stage / BAG_FILENAME
        with bag_path.open("xb") as handle:
            handle.write(BAG_BYTES)
            handle.flush()
            os.fsync(handle.fileno())
        if _sha256(bag_path) != BAG_SHA256:
            raise RuntimeError("htdemucs bag SHA-256 mismatch")
        manifest = {
            "schema_version": 1,
            "engine_id": ENGINE_ID,
            "model_id": MODEL_ID,
            "model_name": MODEL_NAME,
            "model_signature": MODEL_SIGNATURE,
            "model_format": "legacy_torch_checkpoint",
            "model_source_url": MODEL_URL,
            "model_filename_checksum_prefix": "8726e21a",
            "license_id": "MIT",
            "license_url": "https://github.com/adefossez/demucs/blob/v4.1.0/LICENSE",
            "package_revision": PACKAGE_REVISION,
            "package_wheel_sha256": PACKAGE_WHEEL_SHA256,
            "runtime_contract": {
                "python_series": "3.11",
                "python_version": sys.version.split()[0],
                "torch": packages["torch"],
                "numpy": packages["numpy"],
                "sphn": packages["sphn"],
                "psutil": packages["psutil"],
                "torchaudio": None,
                "device": "cpu",
            },
            "requirements_lock": {
                "path": LOCK_FILENAME,
                "size_bytes": lock_path.stat().st_size,
                "sha256": _sha256(lock_path),
            },
            "inference_contract": {
                "two_stems": "vocals",
                "shifts": 0,
                "jobs": 0,
                "split": True,
                "overlap": 0.25,
                "output_format": "pcm_s16le_wav",
            },
            "files": [
                _file_record(model_path, stage),
                _file_record(bag_path, stage),
            ],
            "provision_status": "HASH_VERIFIED",
        }
        _write_json_exclusive(stage / "model_manifest.json", manifest)
        os.replace(stage, final)
        print(
            json.dumps(
                {"status": "success", "model_dir": str(final), **manifest},
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    except Exception:
        _safe_remove_stage(stage, model_parent)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
