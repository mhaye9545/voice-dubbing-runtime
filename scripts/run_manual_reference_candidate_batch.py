"""Create non-committable reference A/B pairs inside a target window.

This audit helper performs real, isolated Demucs source separation on each
short candidate.  It never edits a profile, creates ``ref_primary.wav``, or
invokes a TTS engine.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

RUNTIME_ROOT = Path(__file__).resolve().parents[1]
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

from voice_dubbing_runtime.io_utils import sha256_file, utc_now, write_json_exclusive
from voice_dubbing_runtime.media import (
    cut_reference,
    normalize_voice_only,
    prepare_separation_candidate,
    resolve_ffmpeg,
)
from voice_dubbing_runtime.reference_quality import validate_voice_only_reference
from voice_dubbing_runtime.source_separation import SourceSeparationRunner
from voice_dubbing_runtime.worker import CancellationToken, PeakMemoryMonitor


def _write_text_exclusive(path: Path, text: str) -> None:
    with path.open("xb") as handle:
        handle.write(text.encode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())


def _remaining_descendants() -> list[dict[str, Any]]:
    try:
        import psutil

        parent = psutil.Process(os.getpid())
        return [
            {"pid": child.pid, "name": child.name()}
            for child in parent.children(recursive=True)
            if child.is_running()
        ]
    except (ImportError, OSError):
        return []


def _background_assessment(validation: dict[str, Any]) -> str:
    status = str(validation.get("status") or "")
    if status == "BACKGROUND_AUDIO_DETECTED":
        return "BACKGROUND_AUDIO_DETECTED_PENDING_LISTENING"
    if status == "PASS":
        return "TECHNICAL_PROXY_PASS_PENDING_LISTENING"
    return f"{status or 'UNVERIFIED'}_PENDING_LISTENING"


def _time_range(value: str) -> tuple[float, float]:
    try:
        start_text, end_text = value.split(":", 1)
        start = float(start_text)
        end = float(end_text)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("expected START:END in seconds") from exc
    if start < 0.0 or end <= start:
        raise argparse.ArgumentTypeError("time range must satisfy 0 <= START < END")
    return start, end


def _candidate_report(
    *,
    index: int,
    start: float,
    end: float,
    source_mix: Path,
    voice_only: Path,
    validation: dict[str, Any],
    separation: dict[str, Any],
    elapsed: float,
    peak_ram_gib: float,
    remaining: list[dict[str, Any]],
) -> dict[str, Any]:
    voice = dict(validation["voice_only"])
    mix = dict(validation["source_mix"])
    return {
        "candidate": f"candidate_{index:02d}",
        "start_seconds": start,
        "end_seconds": end,
        "duration_seconds": voice["duration_seconds"],
        "boundary_basis": "pause_aligned_low_energy_boundary_within_user_target_window",
        "speaker_identity": "UNVERIFIED_PENDING_USER_LISTENING",
        "source_mix_path": str(source_mix),
        "voice_only_path": str(voice_only),
        "source_mix_sha256": sha256_file(source_mix),
        "voice_only_sha256": sha256_file(voice_only),
        "ffmpeg_strict_decode": validation["ffmpeg_decode"],
        "finite_sample_ratio": voice["finite_sample_ratio"],
        "peak": voice["peak"],
        "peak_dbfs": voice["peak_dbfs"],
        "rms": voice["rms"],
        "rms_dbfs": voice["rms_dbfs"],
        "clipping_ratio": voice["clipping_ratio"],
        "speech_ratio": voice["speech_ratio"],
        "quiet_block_ratio": voice["quiet_block_ratio"],
        "noise_floor_dbfs": voice["noise_floor_dbfs"],
        "speech_noise_contrast_db": voice["speech_noise_contrast_db"],
        "source_mix_peak": mix["peak"],
        "source_mix_rms": mix["rms"],
        "background_assessment": _background_assessment(validation),
        "background_gate": validation.get("background_gate", {}),
        "single_speaker_gate": validation.get("single_speaker", {}),
        "elapsed_seconds": elapsed,
        "peak_ram_gib": peak_ram_gib,
        "processes_remaining": remaining,
        "processes_remaining_count": len(remaining),
        "source_separation": separation,
    }


def _markdown(report: dict[str, Any]) -> str:
    target = report["target_speaker_window"]
    lines = [
        "# Manual Reference Candidate Round",
        "",
        f"Status: `{report['status']}`",
        "",
        "No candidate is selected. No profile/reference was committed and no synthesis was run.",
        "",
        (
            "Target speaker window: "
            f"`{target['start_seconds']:.6f}–{target['end_seconds']:.6f} s`."
        ),
        "",
        "| Candidate | Start–end | Duration | Voice-only peak/RMS | Clipping | Speech ratio | Background | Decode | Elapsed | Peak RAM | Leftover |",
        "|---|---:|---:|---:|---:|---:|---|---|---:|---:|---:|",
    ]
    for candidate in report["candidates"]:
        lines.append(
            "| {candidate} | {start_seconds:.6f}–{end_seconds:.6f} | "
            "{duration_seconds:.6f}s | {peak:.6f}/{rms:.6f} | "
            "{clipping_ratio:.8f} | {speech_ratio:.6f} | {background_assessment} | "
            "{ffmpeg_strict_decode} | {elapsed_seconds:.3f}s | {peak_ram_gib:.3f} GiB | "
            "{processes_remaining_count} |".format(**candidate)
        )
        lines.extend(
            [
                "",
                f"- `{candidate['candidate']}` source mix: `{candidate['source_mix_path']}`",
                f"- `{candidate['candidate']}` voice-only: `{candidate['voice_only_path']}`",
            ]
        )
    lines.extend(
        [
            "",
            "Technical acoustic metrics do not prove speaker identity or inaudible background.",
            "The user must listen to all candidate pairs before any winner or commit decision.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Create non-committable source-mix/voice-only reference pairs for "
            "manual listening review. This command never updates a voice profile."
        )
    )
    parser.add_argument("--runtime-root", type=Path, required=True, help="Runtime checkout root")
    parser.add_argument("--source", type=Path, required=True, help="Source audio or video")
    parser.add_argument(
        "--source-type",
        choices=("audio", "video"),
        required=True,
        help="How the source should be decoded",
    )
    parser.add_argument(
        "--output-root", type=Path, required=True, help="Parent directory for a new batch"
    )
    parser.add_argument("--ffmpeg", type=Path, required=True, help="FFmpeg executable")
    parser.add_argument(
        "--target-window",
        type=_time_range,
        required=True,
        metavar="START:END",
        help="Allowed target-speaker window in seconds",
    )
    parser.add_argument(
        "--candidate",
        dest="candidates",
        action="append",
        type=_time_range,
        required=True,
        metavar="START:END",
        help="Candidate interval in seconds; repeat for each candidate",
    )
    args = parser.parse_args()

    target_start, target_end = args.target_window
    target_window = {"start_seconds": target_start, "end_seconds": target_end}
    candidates = list(args.candidates)
    for index, (start, end) in enumerate(candidates, 1):
        if not (
            target_window["start_seconds"] <= start < end <= target_window["end_seconds"]
            and 12.0 <= end - start <= 20.0
        ):
            parser.error(
                f"candidate {index} must stay inside --target-window and last 12–20 seconds"
            )

    runtime_root = args.runtime_root.expanduser().resolve()
    source = args.source.expanduser().resolve()
    output_root = args.output_root.expanduser().resolve()
    ffmpeg = resolve_ffmpeg(runtime_root, args.ffmpeg)
    if not source.is_file() or source.stat().st_size == 0:
        raise FileNotFoundError(source)
    batch_id = str(uuid.uuid4())
    output_dir = output_root / batch_id
    output_dir.mkdir(parents=True, exist_ok=False)
    runner = SourceSeparationRunner(runtime_root)
    cancel = CancellationToken()
    reports: list[dict[str, Any]] = []
    batch_started = time.perf_counter()

    for index, (start, end) in enumerate(candidates, 1):
        source_mix = output_dir / f"ref_source_mix_candidate_{index:02d}.wav"
        voice_only = output_dir / f"ref_voice_only_candidate_{index:02d}.wav"
        separation_input = output_dir / f".candidate_{index:02d}_input.wav"
        separated_raw = output_dir / f".candidate_{index:02d}_vocals_raw.wav"
        work_dir = output_dir / "diagnostics" / f"candidate_{index:02d}"
        started = time.perf_counter()
        monitor = PeakMemoryMonitor()
        try:
            with monitor:
                cut_reference(source, source_mix, start, end)
                prepare_separation_candidate(
                    source,
                    args.source_type,
                    separation_input,
                    start,
                    end,
                    ffmpeg,
                    cancel,
                )
                separation = runner.separate_vocals(
                    input_path=separation_input,
                    output_path=separated_raw,
                    work_dir=work_dir,
                    progress=lambda _name, _value: None,
                    cancel_token=cancel,
                )
                normalize_voice_only(separated_raw, voice_only, ffmpeg, cancel)
                validation = validate_voice_only_reference(
                    voice_only,
                    source_mix,
                    ffmpeg,
                    background_was_reported=True,
                )
            time.sleep(0.2)
            remaining = _remaining_descendants()
            reports.append(
                _candidate_report(
                    index=index,
                    start=start,
                    end=end,
                    source_mix=source_mix,
                    voice_only=voice_only,
                    validation=validation,
                    separation=separation,
                    elapsed=time.perf_counter() - started,
                    peak_ram_gib=monitor.peak_bytes / (1024**3),
                    remaining=remaining,
                )
            )
        finally:
            separation_input.unlink(missing_ok=True)
            separated_raw.unlink(missing_ok=True)

    report = {
        "schema_version": 1,
        "status": "TECHNICAL_PASS_PENDING_LISTENING",
        "batch_id": batch_id,
        "created_at": utc_now(),
        "source": str(source),
        "source_sha256": sha256_file(source),
        "target_speaker_window": target_window,
        "candidate_count": len(reports),
        "candidates": reports,
        "candidate_winner": None,
        "listening_gate": "REQUIRED",
        "profile_updated": False,
        "ref_primary_updated": False,
        "synthesis_calls": 0,
        "total_elapsed_seconds": time.perf_counter() - batch_started,
    }
    write_json_exclusive(output_dir / "candidate_batch_report.json", report)
    _write_text_exclusive(output_dir / "candidate_batch_report.md", _markdown(report))
    print(json.dumps({"status": report["status"], "output_dir": str(output_dir)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
