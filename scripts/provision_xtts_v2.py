"""Provision one pinned official XTTS-v2 snapshot after the CPML gate."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import shutil
import sys
import uuid
from pathlib import Path
from typing import Any

from voice_dubbing_runtime.paths import default_xtts_license_path


MODEL_ID = "coqui/XTTS-v2"
MODEL_REVISION = "6c2b0d75eae4b7047358e3b6bd9325f857d43f77"
LICENSE_ID = "Coqui Public Model License 1.0.0"
LICENSE_URL = "https://coqui.ai/cpml.txt"
LICENSE_SHA256 = "190F6D7C19B8984F91B97712B94CE92D2B2E640FC677DACAB966E955ECE9D043"
SCOPE = "research_personal_poc_noncommercial"
FILES = (
    "LICENSE.txt",
    "README.md",
    "config.json",
    "dvae.pth",
    "hash.md5",
    "mel_stats.pth",
    "model.pth",
    "speakers_xtts.pth",
    "vocab.json",
)
RUNTIME_REQUIRED = {"LICENSE.txt", "config.json", "model.pth", "vocab.json"}


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
    return default_xtts_license_path()


def _require_acceptance() -> dict[str, Any]:
    path = _acceptance_path()
    if not path.is_file():
        raise RuntimeError(f"MODEL_LICENSE_NOT_ACCEPTED: missing {path}")
    payload = _read_json(path)
    expected = {
        "accepted": True,
        "model_id": MODEL_ID,
        "revision": MODEL_REVISION,
        "license_id": LICENSE_ID,
        "license_url": LICENSE_URL,
        "license_sha256": LICENSE_SHA256,
        "scope": SCOPE,
    }
    mismatches = {key: {"expected": value, "actual": payload.get(key)} for key, value in expected.items() if payload.get(key) != value}
    if mismatches:
        raise RuntimeError(f"MODEL_LICENSE_NOT_ACCEPTED: acceptance mismatch {mismatches}")
    return payload


def main() -> int:
    runtime_root = Path(__file__).resolve().parents[1]
    final = runtime_root / "models" / "xtts_v2"
    if final.exists():
        raise FileExistsError(f"Refusing to overwrite existing model directory: {final}")
    acceptance = _require_acceptance()
    from huggingface_hub import snapshot_download

    stage = final.with_name(f".xtts_v2.staging-{uuid.uuid4().hex}")
    stage.mkdir(parents=True, exist_ok=False)
    try:
        snapshot_download(
            repo_id=MODEL_ID,
            revision=MODEL_REVISION,
            allow_patterns=list(FILES),
            local_dir=stage,
        )
        missing = [name for name in FILES if not (stage / name).is_file()]
        if missing:
            raise RuntimeError(f"Pinned snapshot is incomplete: {missing}")
        if _sha256(stage / "LICENSE.txt") != LICENSE_SHA256:
            raise RuntimeError("Downloaded CPML hash does not match the accepted license")
        cache = stage / ".cache"
        if cache.exists():
            resolved = cache.resolve()
            resolved.relative_to(stage.resolve())
            shutil.rmtree(resolved)
        records = []
        total = 0
        for name in FILES:
            path = stage / name
            size = path.stat().st_size
            total += size
            records.append(
                {
                    "path": name,
                    "size_bytes": size,
                    "sha256": _sha256(path),
                    "runtime_required": name in RUNTIME_REQUIRED,
                }
            )
        manifest = {
            "schema_version": 1,
            "model_id": MODEL_ID,
            "revision": MODEL_REVISION,
            "license_id": LICENSE_ID,
            "license_url": LICENSE_URL,
            "license_sha256": LICENSE_SHA256,
            "license_scope": SCOPE,
            "license_accepted_at": acceptance.get("accepted_at"),
            "package_revision": f"coqui-tts=={importlib.metadata.version('coqui-tts')}",
            "python_version": sys.version.split()[0],
            "total_size_bytes": total,
            "files": records,
        }
        manifest_path = stage / "model_manifest.json"
        with manifest_path.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(manifest, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(stage, final)
        print(json.dumps({"status": "success", "model_dir": str(final), **manifest}, ensure_ascii=False, indent=2))
        return 0
    except Exception:
        print(f"Provisioning failed; staging preserved for diagnosis: {stage}", file=sys.stderr)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
