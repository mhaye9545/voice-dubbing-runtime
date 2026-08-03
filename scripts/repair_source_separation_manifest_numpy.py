"""Atomically add the pinned NumPy contract to an existing htdemucs manifest."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import uuid
from pathlib import Path


MODEL_FILENAME = "955717e8-8726e21a.th"
MODEL_SIZE = 84_141_911
MODEL_SHA256 = "8726E21A993978C7BA086D3872E7608D7D5BFCA646CA4ACA459FFDA844FAA8B4"
BAG_FILENAME = "htdemucs.yaml"
BAG_SIZE = 21
BAG_SHA256 = "239C445D0B14454D541AD8BD9BB271C9E536D267E8A4625208744CBB2E7BB66C"
LOCK_FILENAME = "requirements-source-separation.lock.txt"
NUMPY_VERSION = "1.26.4"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def verify_file(path: Path, size: int, expected_hash: str) -> None:
    if not path.is_file() or path.stat().st_size != size or sha256(path) != expected_hash:
        raise RuntimeError(f"Pinned source-separation artifact failed verification: {path}")


def main() -> int:
    runtime_root = Path(__file__).resolve().parents[1]
    model_dir = runtime_root / "models" / "source_separation" / "htdemucs"
    manifest_path = model_dir / "model_manifest.json"
    lock_path = runtime_root / LOCK_FILENAME

    verify_file(model_dir / MODEL_FILENAME, MODEL_SIZE, MODEL_SHA256)
    verify_file(model_dir / BAG_FILENAME, BAG_SIZE, BAG_SHA256)
    if not lock_path.is_file() or lock_path.stat().st_size <= 0:
        raise RuntimeError("Pinned source-separation lock is missing")

    with manifest_path.open("r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    if manifest.get("model_signature") != "955717e8" or manifest.get("package_revision") != "demucs==4.1.0":
        raise RuntimeError("Refusing to repair an unexpected source-separation manifest")
    runtime = manifest.get("runtime_contract")
    if not isinstance(runtime, dict) or runtime.get("torch") != "2.6.0+cpu":
        raise RuntimeError("Refusing to repair an unexpected source-separation runtime contract")

    runtime["numpy"] = NUMPY_VERSION
    manifest["requirements_lock"] = {
        "path": LOCK_FILENAME,
        "size_bytes": lock_path.stat().st_size,
        "sha256": sha256(lock_path),
    }
    temporary = model_dir / f".model_manifest.numpy-repair-{uuid.uuid4().hex}.json"
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(manifest, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, manifest_path)
    finally:
        temporary.unlink(missing_ok=True)

    print(
        json.dumps(
            {
                "status": "success",
                "manifest": str(manifest_path),
                "manifest_sha256": sha256(manifest_path),
                "requirements_lock": manifest["requirements_lock"],
                "numpy": NUMPY_VERSION,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
