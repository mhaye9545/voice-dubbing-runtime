"""Read-only environment and source diagnostics for contributors."""

from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from .paths import user_data_root
from .vixtts_backend import TTS_REVISION


@dataclass(frozen=True)
class DoctorCheck:
    group: str
    name: str
    status: str
    message: str
    details: dict[str, Any]


def _check(group: str, name: str, status: str, message: str, **details: Any) -> DoctorCheck:
    return DoctorCheck(group, name, status, message, details)


def _distribution_versions(python: Path, names: Iterable[str]) -> tuple[dict[str, str], str | None]:
    code = (
        "import importlib.metadata as m,json;"
        f"names={list(names)!r};"
        "print(json.dumps({n:m.version(n) for n in names}))"
    )
    try:
        completed = subprocess.run(
            [str(python), "-c", code],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {}, str(exc)
    if completed.returncode:
        return {}, (completed.stderr or completed.stdout).strip()
    try:
        return json.loads(completed.stdout), None
    except json.JSONDecodeError as exc:
        return {}, str(exc)


def _optional_environment(
    root: Path,
    directory: str,
    distributions: dict[str, str],
    group: str,
) -> DoctorCheck:
    python = root / directory / "Scripts" / "python.exe"
    if not python.is_file():
        return _check(group, "environment", "WARN", f"Optional environment is missing: {directory}")
    versions, error = _distribution_versions(python, distributions)
    if error:
        return _check(group, "environment", "WARN", "Environment metadata could not be read.", error=error)
    mismatches = {
        name: {"expected": expected, "actual": versions.get(name)}
        for name, expected in distributions.items()
        if versions.get(name) != expected
    }
    if mismatches:
        return _check(group, "environment", "WARN", "Optional environment versions differ.", mismatches=mismatches)
    return _check(group, "environment", "PASS", "Optional environment versions match.", versions=versions)


def _vendor_import_check(root: Path, vendor: Path, deep: bool) -> DoctorCheck:
    code = [
        "import json,pathlib,TTS",
        "p=pathlib.Path(TTS.__file__).resolve()",
        "result={'tts_file':str(p)}",
    ]
    if deep:
        code.extend(
            [
                "from TTS.tts.configs.xtts_config import XttsConfig",
                "from TTS.tts.models.xtts import Xtts",
                "result['xtts_import']=True",
            ]
        )
    code.append("print(json.dumps(result))")
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(vendor)
    try:
        completed = subprocess.run(
            [sys.executable, "-c", ";".join(code)],
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=90 if deep else 20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _check("vixtts", "source_resolution", "FAIL", "Vendor import probe failed.", error=str(exc))
    if completed.returncode:
        return _check(
            "vixtts",
            "source_resolution",
            "FAIL",
            "Vendor import probe returned non-zero.",
            error=(completed.stderr or completed.stdout).strip(),
        )
    payload = json.loads(completed.stdout)
    resolved = Path(payload["tts_file"]).resolve()
    if not resolved.is_relative_to(vendor.resolve()):
        return _check("vixtts", "source_resolution", "FAIL", "TTS resolved outside the vendor root.", **payload)
    return _check("vixtts", "source_resolution", "PASS", "TTS resolves to the authoritative vendor.", **payload)


def run_doctor(root: Path | None = None, *, deep: bool = False) -> dict[str, Any]:
    runtime_root = (root or Path(__file__).resolve().parents[1]).resolve()
    vendor = runtime_root / "vendor" / f"TTS-{TTS_REVISION}"
    checks: list[DoctorCheck] = []

    version_ok = sys.version_info[:2] == (3, 11)
    checks.append(
        _check(
            "core",
            "python",
            "PASS" if version_ok else "FAIL",
            "Python 3.11 is active." if version_ok else "Python 3.11 is required.",
            executable=sys.executable,
            version=sys.version.split()[0],
        )
    )
    config = runtime_root / "voice_dubbing_runtime" / "config" / "engines.json"
    checks.append(
        _check("core", "config", "PASS" if config.is_file() else "FAIL", "Engine config is present." if config.is_file() else "Engine config is missing.", path=str(config))
    )
    checks.append(_check("core", "runtime_import", "PASS", "voice_dubbing_runtime imported successfully."))

    provenance = runtime_root / "vendor" / "TTS_PROVENANCE.md"
    required_vendor = [
        vendor / "TTS" / "__init__.py",
        vendor / "TTS" / "tts" / "configs" / "xtts_config.py",
        vendor / "TTS" / "tts" / "models" / "xtts.py",
        vendor / "TTS" / "vocoder" / "models" / "hifigan_generator.py",
    ]
    missing_vendor = [str(path) for path in [provenance, *required_vendor] if not path.is_file()]
    checks.append(
        _check(
            "vixtts",
            "vendor_files",
            "PASS" if not missing_vendor else "FAIL",
            "Required vendor source/provenance is present." if not missing_vendor else "Required vendor source/provenance is missing.",
            vendor=str(vendor),
            missing=missing_vendor,
        )
    )
    installed_tts = sorted(
        {
            str(dist.metadata.get("Name", ""))
            for dist in importlib.metadata.distributions()
            if str(dist.metadata.get("Name", "")).lower() in {"tts", "coqui-tts"}
        }
    )
    checks.append(
        _check(
            "vixtts",
            "duplicate_distribution",
            "PASS" if not installed_tts else "FAIL",
            "No installed TTS distribution can shadow the vendor." if not installed_tts else "An installed TTS distribution can shadow the vendor.",
            distributions=installed_tts,
        )
    )
    if not missing_vendor:
        checks.append(_vendor_import_check(runtime_root, vendor, deep))

    gui_available = importlib.util.find_spec("PySide6") is not None and importlib.util.find_spec("voice_dubbing_app") is not None
    checks.append(
        _check("gui", "imports", "PASS" if gui_available else "WARN", "GUI imports are available." if gui_available else "GUI dependencies are not installed in this environment.")
    )

    checks.append(
        _optional_environment(
            runtime_root,
            ".venv-xtts",
            {"coqui-tts": "0.27.5", "transformers": "4.57.6"},
            "xtts",
        )
    )
    xtts_manifest = runtime_root / "models" / "xtts_v2" / "model_manifest.json"
    checks.append(
        _check("xtts", "model_manifest", "PASS" if xtts_manifest.is_file() else "WARN", "XTTS-v2 model manifest is present." if xtts_manifest.is_file() else "XTTS-v2 model is not provisioned.", path=str(xtts_manifest))
    )

    checks.append(
        _optional_environment(
            runtime_root,
            ".venv-source-separation",
            {"demucs": "4.1.0", "numpy": "1.26.4"},
            "demucs",
        )
    )
    demucs_manifest = runtime_root / "models" / "source_separation" / "htdemucs" / "model_manifest.json"
    checks.append(
        _check("demucs", "model_manifest", "PASS" if demucs_manifest.is_file() else "WARN", "Demucs model manifest is present." if demucs_manifest.is_file() else "Demucs model is not provisioned.", path=str(demucs_manifest))
    )

    storage = user_data_root()
    checks.append(
        _check("storage", "path", "PASS", "Storage status is read-only.", path=str(storage), exists=storage.exists())
    )

    serialized = [asdict(item) for item in checks]
    counts = {status: sum(item.status == status for item in checks) for status in ("PASS", "WARN", "FAIL", "SKIP")}
    overall = "FAIL" if counts["FAIL"] else ("WARN" if counts["WARN"] else "PASS")
    return {"schema_version": 1, "status": overall, "deep": deep, "summary": counts, "checks": serialized}


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="voice-dubbing-doctor")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--deep", action="store_true")
    args = parser.parse_args(argv)
    payload = run_doctor(deep=args.deep)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for item in payload["checks"]:
            print(f"[{item['status']}] {item['group']}.{item['name']}: {item['message']}")
        print(f"Doctor status: {payload['status']}")
    return 1 if payload["status"] == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
