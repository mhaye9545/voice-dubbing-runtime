"""Repair only debug metadata after identifying resampler group-delay bias."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import uuid
from pathlib import Path

from scripts.debug_source_separation_candidates import _audio_metrics, _comparison


def _copy_exclusive(source: Path, target: Path) -> None:
    with source.open("rb") as incoming, target.open("xb") as outgoing:
        shutil.copyfileobj(incoming, outgoing, 1024 * 1024)
        outgoing.flush()
        os.fsync(outgoing.fileno())


def _replace_json(path: Path, value: dict) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    encoded = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    try:
        with temporary.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()
    candidate_root = args.candidate_root.resolve()
    output_root = args.output_root.resolve()
    updated: list[dict] = []
    for number in (2, 3):
        directory = output_root / f"debug_candidate_{number:02d}"
        metrics_path = directory / "debug_metrics.json"
        audit_path = directory / "debug_metrics_pre_alignment_fix.json"
        if not metrics_path.is_file() or audit_path.exists():
            raise RuntimeError(f"Unexpected debug metadata state: {directory}")
        _copy_exclusive(metrics_path, audit_path)
        report = json.loads(metrics_path.read_text(encoding="utf-8"))
        input_source = candidate_root / f"ref_source_mix_candidate_{number:02d}.wav"
        input_mix = directory / "input_mix.wav"
        vocals_raw = directory / "vocals_raw.wav"
        no_vocals_raw = directory / "no_vocals_raw.wav"
        vocals_normalized = directory / "vocals_normalized.wav"
        final_voice = directory / "final_voice_only.wav"
        report["files"] = {
            path.name: _audio_metrics(path)
            for path in (
                input_mix,
                vocals_raw,
                no_vocals_raw,
                vocals_normalized,
                final_voice,
            )
        }
        report["comparisons"] = {
            "input_vs_vocals_raw": _comparison(input_mix, vocals_raw),
            "input_vs_no_vocals_raw": _comparison(input_mix, no_vocals_raw),
            "input_vs_final_voice_only": _comparison(input_source, final_voice),
        }
        report["scaled_copy_gate"] = report["comparisons"][
            "input_vs_final_voice_only"
        ]["gate"]
        report["metadata_correction"] = (
            "final comparison uses aligned canonical mono 24 kHz candidate source; "
            "pre-fix metadata is retained for audit"
        )
        _replace_json(metrics_path, report)
        updated.append(report)

    root_report = output_root / "source_separation_debug_report.json"
    root_audit = output_root / "source_separation_debug_report_pre_alignment_fix.json"
    if not root_report.is_file() or root_audit.exists():
        raise RuntimeError("Unexpected root debug report state")
    _copy_exclusive(root_report, root_audit)
    payload = json.loads(root_report.read_text(encoding="utf-8"))
    payload["candidates"] = updated
    payload["metadata_correction"] = "aligned canonical source/final comparison"
    _replace_json(root_report, payload)
    print(
        json.dumps(
            {
                "status": "Pass",
                "gates": [item["scaled_copy_gate"] for item in updated],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
