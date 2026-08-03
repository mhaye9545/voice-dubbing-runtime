"""Create a hash-verified, atomic backup of one voice profile."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

RUNTIME_ROOT = Path(__file__).resolve().parents[1]
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

from voice_dubbing_runtime.io_utils import utc_now, write_json_exclusive
from voice_dubbing_runtime.paths import user_data_root
from voice_dubbing_runtime.repair import _assert_same_manifest, _tree_manifest


EXPECTED_PROFILE_ID = "lestehrolt_en_clean"
EXPECTED_DISPLAY_NAME = "Lestehrolt_en_clean"
EXPECTED_CREATED_AT = "2026-08-02T14:17:26Z"
EXPECTED_REFERENCE_SHA256 = "A80349C7CC5162CC029BC9DD0CF4B819BA8655C0D41E07C58FDEF527875ECF1C"
EXPECTED_CREATION_JOB_ID = "c2e576a4-50a7-4fc3-9451-637898594ff7"
EXPECTED_SOURCE_PATH = r"C:\Users\akita\Downloads\Video\Lestehrolt_en_clean.wav"


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile-id", required=True)
    args = parser.parse_args()
    if args.profile_id != EXPECTED_PROFILE_ID:
        raise ValueError(f"Unexpected profile id: {args.profile_id}")

    data_root = user_data_root()
    profile_root = data_root / "profiles" / args.profile_id
    profile_path = profile_root / "profile.json"
    consent_path = profile_root / "consent.json"
    if not profile_path.is_file() or not consent_path.is_file():
        raise FileNotFoundError(f"Profile is incomplete: {profile_root}")
    profile = json.loads(profile_path.read_text(encoding="utf-8-sig"))
    references = profile.get("reference_files")
    if not (
        profile.get("profile_id") == EXPECTED_PROFILE_ID
        and profile.get("display_name") == EXPECTED_DISPLAY_NAME
        and profile.get("created_at") == EXPECTED_CREATED_AT
        and isinstance(references, list)
        and len(references) == 1
        and references[0].get("sha256") == EXPECTED_REFERENCE_SHA256
    ):
        raise RuntimeError("PROFILE_IDENTITY_GATE_FAILED")

    creation_run = data_root / "runs" / EXPECTED_CREATION_JOB_ID
    job_path = creation_run / "job.json"
    result_path = creation_run / "result.json"
    job = json.loads(job_path.read_text(encoding="utf-8-sig"))
    if job.get("input_path") != EXPECTED_SOURCE_PATH:
        raise RuntimeError("PROFILE_SOURCE_PATH_GATE_FAILED")
    if job.get("display_name") != EXPECTED_DISPLAY_NAME:
        raise RuntimeError("PROFILE_CREATION_JOB_GATE_FAILED")

    backup_root = RUNTIME_ROOT.parent / "download"
    destination = backup_root / f"voice_profile_background_backup_{_timestamp()}_{uuid.uuid4().hex[:8]}"
    staging = backup_root / f".{destination.name}.creating-{uuid.uuid4().hex}"
    if destination.exists() or staging.exists():
        raise FileExistsError("Backup destination collision")
    backup_root.mkdir(parents=True, exist_ok=True)
    source_manifest = _tree_manifest(profile_root)
    try:
        staging.mkdir(parents=False, exist_ok=False)
        backup_profile = staging / EXPECTED_PROFILE_ID
        shutil.copytree(profile_root, backup_profile, copy_function=shutil.copy2)
        backup_manifest = _tree_manifest(backup_profile)
        _assert_same_manifest(source_manifest, backup_manifest)
        provenance = staging / "creation_provenance"
        provenance.mkdir(parents=False, exist_ok=False)
        for source in (job_path, result_path, creation_run / "run.log"):
            if source.is_file():
                shutil.copy2(source, provenance / source.name)
        manifest = {
            "schema_version": 1,
            "status": "PASS",
            "created_at": utc_now(),
            "operation": "pre_background_removal_profile_backup",
            "profile_id": EXPECTED_PROFILE_ID,
            "display_name": EXPECTED_DISPLAY_NAME,
            "profile_created_at": EXPECTED_CREATED_AT,
            "source_path": EXPECTED_SOURCE_PATH,
            "creation_job_id": EXPECTED_CREATION_JOB_ID,
            "source_profile": str(profile_root),
            "backup_profile": str(destination / EXPECTED_PROFILE_ID),
            "source_files": source_manifest,
            "backup_files": backup_manifest,
            "creation_provenance_files": _tree_manifest(provenance),
            "source_tree_preserved": True,
        }
        write_json_exclusive(staging / "backup_manifest.json", manifest)
        os.replace(staging, destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    print(json.dumps({"backup_path": str(destination), **manifest}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    raise SystemExit(main())
