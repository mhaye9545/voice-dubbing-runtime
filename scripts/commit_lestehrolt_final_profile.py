"""Commit the locked Lester Holt final reference exactly once (revision 7 -> 8)."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from voice_dubbing_runtime.io_utils import file_record, read_json, sha256_file, utc_now, write_json_exclusive
from voice_dubbing_runtime.profiles import VoiceProfileManager


PROFILE_ID = "lestehrolt_en_clean"
OLD_REFERENCE_SHA256 = "A80349C7CC5162CC029BC9DD0CF4B819BA8655C0D41E07C58FDEF527875ECF1C"
EXPECTED_OLD_REVISION = 7


def _strict_decode(path: Path, ffmpeg: Path) -> None:
    completed = subprocess.run(
        [
            str(ffmpeg), "-hide_banner", "-nostdin", "-v", "error", "-xerror",
            "-err_detect", "explode", "-i", str(path), "-map", "0:a:0", "-f",
            "null", "NUL",
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode != 0:
        raise RuntimeError(f"Final reference strict decode failed: {completed.stderr[-1000:]}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--profiles-root", type=Path, required=True)
    parser.add_argument("--ffmpeg", type=Path, required=True)
    parser.add_argument("--canonical-source", type=Path, required=True)
    args = parser.parse_args()

    runtime = args.runtime_root.expanduser().resolve()
    profiles_root = args.profiles_root.expanduser().resolve()
    ffmpeg = args.ffmpeg.expanduser().resolve()
    canonical_source = args.canonical_source.expanduser().resolve()
    run_root = runtime / "runs" / "lestehrolt_en_final"
    variants_report_path = run_root / "final_reference_variant_report.json"
    debug_report_path = run_root / "source_separation_debug_report.json"
    commit_report_path = run_root / "final_profile_commit_report.json"
    if commit_report_path.exists():
        raise FileExistsError(f"Refusing duplicate final commit report: {commit_report_path}")
    if not canonical_source.is_file() or canonical_source.stat().st_size == 0:
        raise FileNotFoundError(f"Canonical source is unavailable: {canonical_source}")

    variants = read_json(variants_report_path)
    debug = read_json(debug_report_path)
    if variants.get("status") != "Pass":
        raise RuntimeError("Final variant report has not passed")
    selected = dict(variants.get("selected_reference") or {})
    if selected.get("candidate_number") != 2 or selected.get("variant") != "CLEAN_SOURCE_MIX":
        raise RuntimeError("Unexpected final reference selection")
    if not (0.0 <= float(selected["start_seconds"]) < float(selected["end_seconds"]) <= 50.0):
        raise RuntimeError("Final reference is outside target speaker window")
    primary = Path(str(selected["path"])).resolve()
    source_mix = run_root / "candidate_02_source_mix.wav"
    if primary != source_mix.resolve():
        raise RuntimeError("Final primary must be Candidate 02 source mix")
    if sha256_file(primary) != str(selected["sha256"]).upper():
        raise RuntimeError("Final variant hash mismatch")
    _strict_decode(primary, ffmpeg)

    manager = VoiceProfileManager(profiles_root)
    old_revision = manager.profile_revision(PROFILE_ID)
    if old_revision != EXPECTED_OLD_REVISION:
        raise RuntimeError(f"Expected revision 7, got {old_revision}; refusing a second commit")
    live = profiles_root / PROFILE_ID
    old_reference = live / "references" / "reference_001.wav"
    if sha256_file(old_reference) != OLD_REFERENCE_SHA256:
        raise RuntimeError("Historical reference_001.wav hash changed")

    candidate_debug = next(
        item for item in debug.get("candidates", []) if item.get("candidate") == 2
    )
    comparison = candidate_debug["comparisons"]["input_vs_final_voice_only"]
    validation = {
        "schema_version": 1,
        "status": "PASS",
        "ffmpeg_strict_decode": "Pass",
        "file": file_record(primary),
        "codec": selected["codec"],
        "sample_rate": selected["sample_rate"],
        "channels": selected["channels"],
        "duration_seconds": selected["duration_seconds"],
        "finite_sample_ratio": selected["finite_sample_ratio"],
        "nonzero_sample_ratio": selected["nonzero_sample_ratio"],
        "peak": selected["peak"],
        "rms": selected["rms"],
        "clipping_ratio": selected["clipping_ratio"],
        "single_speaker": "CONFIRMED_FROM_TARGET_WINDOW_AND_USER_LISTENING_HISTORY",
    }
    selected_metadata = {
        "candidate_number": 2,
        "start_seconds": float(selected["start_seconds"]),
        "end_seconds": float(selected["end_seconds"]),
        "duration_seconds": float(selected["duration_seconds"]),
        "variant": "CLEAN_SOURCE_MIX",
        "sha256": sha256_file(primary),
        "path_before_commit": str(primary),
        "selection_reason": selected["selection_reason"],
    }
    processing = {
        "schema_version": 1,
        "canonical_source": str(canonical_source),
        "separation_engine": "demucs==4.1.0/htdemucs",
        "separation_model_signature": "955717e8",
        "correct_vocals_stem": bool(candidate_debug.get("correct_vocals_stem")),
        "separation_effective": False,
        "separation_status": "SOURCE_SEPARATION_NO_EFFECT",
        "input_final_correlation": comparison["correlation"],
        "best_fit_gain": comparison["best_fit_gain"],
        "gain_fitted_residual_ratio": comparison["gain_fitted_residual_ratio"],
        "background_assessment": selected["background_assessment"],
        "listening_history": [
            "USER_LISTENING_REJECTED previous wrong-speaker/short voice-only candidate",
            "User reported source mixes preserve the natural target voice better than current voice-only outputs",
            "Candidate 02 is inside the locked 0-50 second target-speaker window",
        ],
        "true_vocals_status": "UNAVAILABLE_SOURCE_SEPARATION_NO_EFFECT",
        "debug_report": str(debug_report_path),
        "variant_report": str(variants_report_path),
    }
    installed = manager.install_final_reference_set(
        PROFILE_ID,
        source_mix=source_mix,
        primary=primary,
        voice_only=None,
        expected_revision=EXPECTED_OLD_REVISION,
        prepare_job_id="lestehrolt-en-final-20260803",
        validation=validation,
        selected_reference=selected_metadata,
        reference_processing=processing,
        target_speaker_window={"start_seconds": 0.0, "end_seconds": 50.0},
        single_speaker_confirmed=True,
        display_name="Lester Holt EN",
    )
    new_revision = manager.profile_revision(PROFILE_ID)
    profile = manager.load(PROFILE_ID)
    primary_live = live / "references" / "ref_primary.wav"
    source_live = live / "references" / "ref_source_mix.wav"
    if new_revision != 8 or installed.get("profile_revision") != 8:
        raise RuntimeError(f"Revision did not advance exactly once: {new_revision}")
    if profile.get("status") != "READY" or profile.get("reference_policy", {}).get("state") != "READY":
        raise RuntimeError("Committed profile is not READY")
    if sha256_file(primary_live) != sha256_file(primary):
        raise RuntimeError("Live primary hash mismatch")
    if sha256_file(source_live) != sha256_file(source_mix):
        raise RuntimeError("Live source mix hash mismatch")
    if sha256_file(old_reference) != OLD_REFERENCE_SHA256:
        raise RuntimeError("Historical reference changed during commit")
    if (live / "references" / "ref_voice_only.wav").exists():
        raise RuntimeError("Ineffective ref_voice_only.wav was unexpectedly created")

    report = {
        "schema_version": 1,
        "status": "Pass",
        "completed_at": utc_now(),
        "profile_id": PROFILE_ID,
        "live_path": str(live),
        "old_revision": old_revision,
        "new_revision": new_revision,
        "profile_status": "READY",
        "history_path": installed.get("history_path"),
        "staging_policy": installed.get("staging_policy"),
        "selected_reference": selected_metadata,
        "reference_validation": validation,
        "live_files": {
            "ref_source_mix": file_record(source_live),
            "ref_primary": file_record(primary_live),
            "reference_001": file_record(old_reference),
            "ref_voice_only": {"status": "UNAVAILABLE_SOURCE_SEPARATION_NO_EFFECT"},
        },
        "profile_json_sha256": sha256_file(live / "profile.json"),
        "quality_json_sha256": sha256_file(live / "quality.json"),
        "profile_lock_sha256": sha256_file(live / "profile.lock"),
        "processes_remaining": [],
    }
    write_json_exclusive(commit_report_path, report)
    print(json.dumps({"status": "Pass", "revision": new_revision, "report": str(commit_report_path)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
