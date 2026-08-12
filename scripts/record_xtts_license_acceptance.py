"""Persist an explicit user-local CPML acknowledgement for pinned XTTS-v2."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from voice_dubbing_runtime.paths import default_xtts_license_path


MODEL_ID = "coqui/XTTS-v2"
MODEL_REVISION = "6c2b0d75eae4b7047358e3b6bd9325f857d43f77"
LICENSE_ID = "Coqui Public Model License 1.0.0"
LICENSE_URL = "https://coqui.ai/cpml.txt"
LICENSE_SHA256 = "190F6D7C19B8984F91B97712B94CE92D2B2E640FC677DACAB966E955ECE9D043"
SCOPE = "research_personal_poc_noncommercial"


def acceptance_path() -> Path:
    return default_xtts_license_path()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--accept-cpml", action="store_true")
    args = parser.parse_args()
    if not args.accept_cpml:
        parser.error(
            "MODEL_LICENSE_NOT_ACCEPTED: pass --accept-cpml only after the user explicitly "
            f"accepts {LICENSE_ID} at {LICENSE_URL}"
        )
    path = acceptance_path()
    if path.exists():
        raise FileExistsError(f"Refusing to overwrite existing acceptance record: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "accepted": True,
        "accepted_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "model_id": MODEL_ID,
        "revision": MODEL_REVISION,
        "license_id": LICENSE_ID,
        "license_url": LICENSE_URL,
        "license_sha256": LICENSE_SHA256,
        "scope": SCOPE,
        "commercial_use_claimed": False,
        "portable_distribution_allowed_by_this_record": False,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

