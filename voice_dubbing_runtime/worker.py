"""JSON job worker with JSONL marker output for the FrameExtract thin client."""

from __future__ import annotations

import json
import math
import os
import signal
import subprocess
import sys
import threading
import time
import traceback
import uuid
import wave
from pathlib import Path
from typing import Any, Callable, TextIO

from .capabilities import EngineRegistry
from .errors import (
    BACKGROUND_AUDIO_DETECTED,
    BACKGROUND_AUDIO_DETECTED_PENDING_LISTENING,
    CANCELLED,
    CONSENT_REQUIRED,
    INVALID_REQUEST,
    INVALID_REFERENCE,
    NEEDS_MANUAL_REFERENCE,
    OUTPUT_VALIDATION_FAILED,
    PROFILE_ID_CONFLICT,
    PROFILE_REVISION_MISMATCH,
    REFERENCE_APPROVAL_REQUIRED,
    REFERENCE_UPDATE_FAILED,
    REFERENCE_VALIDATION_FAILED,
    SOURCE_SEPARATION_FAILED,
    SOURCE_SEPARATION_NO_EFFECT,
    SYNTHESIS_FAILED,
    DUPLICATE_JOB_REJECTED,
    VoiceRuntimeError,
)
from .io_utils import (
    canonical_json,
    file_record,
    read_json,
    sha256_file,
    utc_now,
    write_json_exclusive,
)
from .media import (
    choose_reference_auto,
    cut_reference,
    inspect_pcm_wav,
    normalize_voice_only,
    normalize_source,
    prepare_separation_candidate,
    resolve_ffmpeg,
    validate_reference_duration,
)
from .paths import default_runs_root, runtime_root
from .profiles import VoiceProfileManager, consent_is_confirmed, make_profile_id, validate_profile_id
from .reference_quality import validate_voice_only_reference


MARKER_PREFIX = "@@VOICE_DUB|"


class CancellationToken:
    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self.is_cancelled():
            raise VoiceRuntimeError(CANCELLED, "Voice-dubbing job was cancelled.")


class PeakMemoryMonitor:
    def __init__(self) -> None:
        self.peak_bytes = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="voice-peak-memory", daemon=True)
        try:
            import psutil

            self._psutil: Any = psutil
            self._process: Any = psutil.Process(os.getpid())
        except ImportError:
            self._psutil = None
            self._process = None

    def _sample(self) -> None:
        if self._process is None:
            return
        processes = [self._process]
        try:
            processes.extend(self._process.children(recursive=True))
        except (self._psutil.NoSuchProcess, self._psutil.AccessDenied):
            pass
        total = 0
        for process in processes:
            try:
                total += process.memory_info().rss
            except (self._psutil.NoSuchProcess, self._psutil.AccessDenied):
                pass
        self.peak_bytes = max(self.peak_bytes, total)

    def _run(self) -> None:
        while not self._stop.wait(0.1):
            self._sample()
        self._sample()

    def __enter__(self) -> "PeakMemoryMonitor":
        self._sample()
        self._thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)


class MarkerEmitter:
    def __init__(self, stream: TextIO | None = None) -> None:
        self.stream = stream or sys.stdout
        self.log_path: Path | None = None

    def set_log_path(self, path: Path) -> None:
        if path.exists():
            raise FileExistsError(f"Refusing to overwrite run log: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8", newline="\n"):
            pass
        self.log_path = path

    def clear_log_path(self) -> None:
        self.log_path = None

    def emit(self, payload: dict[str, Any]) -> None:
        enriched = {"schema_version": 1, **payload}
        line = canonical_json(enriched)
        # The GUI parses JSON escapes back into Unicode, while an inherited
        # Windows console may use cp1252 and reject Vietnamese characters.
        # Keep UTF-8 in the durable log, but make the marker wire ASCII-safe.
        wire_line = json.dumps(
            enriched, ensure_ascii=True, separators=(",", ":"), sort_keys=True
        )
        self.stream.write(MARKER_PREFIX + wire_line + "\n")
        self.stream.flush()
        if self.log_path is not None:
            with self.log_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line + "\n")


def validate_job(job: Any) -> dict[str, Any]:
    if not isinstance(job, dict) or job.get("schema_version") != 1:
        raise VoiceRuntimeError(INVALID_REQUEST, "Job schema_version must be 1.")
    identifier = str(job.get("job_id", ""))
    try:
        uuid.UUID(identifier)
    except (ValueError, TypeError, AttributeError) as exc:
        raise VoiceRuntimeError(INVALID_REQUEST, "job_id must be a UUID.") from exc
    if job.get("action") not in {
        "synthesize",
        "create_profile",
        "prepare_profile_reference",
        "commit_profile_reference",
    }:
        raise VoiceRuntimeError(INVALID_REQUEST, f"Unsupported action: {job.get('action')}")
    return dict(job)


def _prepare_run(job: dict[str, Any], runs_root: Path) -> tuple[Path, Path]:
    expected = (runs_root / str(job["job_id"])).resolve()
    output_value = job.get("output_dir")
    run_dir = (
        Path(output_value).expanduser().resolve()
        if output_value
        else expected
    )
    if run_dir != expected:
        raise VoiceRuntimeError(
            INVALID_REQUEST,
            "output_dir must be the configured VoiceDubbing runs root plus job_id.",
            {"expected_output_dir": str(expected), "submitted_output_dir": str(run_dir)},
        )
    run_dir.mkdir(parents=True, exist_ok=True)
    result_path = run_dir / "result.json"
    for reserved in (result_path, run_dir / "run.log"):
        if reserved.exists():
            raise FileExistsError(f"Refusing to overwrite run artifact: {reserved}")
    job_path = run_dir / "job.json"
    if job_path.exists():
        try:
            if read_json(job_path) != job:
                raise FileExistsError(f"Existing job.json does not match submitted job: {job_path}")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise FileExistsError(f"Could not safely reuse job.json: {job_path}") from exc
    else:
        write_json_exclusive(job_path, job)
    return run_dir, result_path


def _ffmpeg_decode(path: Path, ffmpeg: Path) -> None:
    sink = "NUL" if os.name == "nt" else "/dev/null"
    try:
        completed = subprocess.run(
            [
                str(ffmpeg), "-hide_banner", "-nostdin", "-v", "error", "-xerror",
                "-err_detect", "explode", "-i", str(path), "-map", "0:a:0",
                "-f", "null", sink,
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
    except subprocess.TimeoutExpired as exc:
        raise VoiceRuntimeError(OUTPUT_VALIDATION_FAILED, "FFmpeg output decode timed out.") from exc
    if completed.returncode != 0:
        raise VoiceRuntimeError(
            OUTPUT_VALIDATION_FAILED,
            f"FFmpeg output decode failed: {completed.stderr.strip()[-1000:]}",
            {"ffmpeg_exit_code": completed.returncode},
        )


def validate_generated_wav(path: Path, ffmpeg: Path) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size <= 44:
        raise VoiceRuntimeError(OUTPUT_VALIDATION_FAILED, "Generated WAV is missing or empty.")
    try:
        with wave.open(str(path), "rb") as reader:
            channels = reader.getnchannels()
            rate = reader.getframerate()
            width = reader.getsampwidth()
            frames = reader.getnframes()
            raw = reader.readframes(frames)
    except (wave.Error, OSError) as exc:
        raise VoiceRuntimeError(OUTPUT_VALIDATION_FAILED, f"Generated WAV is invalid: {exc}") from exc
    if channels <= 0 or rate <= 0 or width != 2 or frames <= 0:
        raise VoiceRuntimeError(OUTPUT_VALIDATION_FAILED, "Generated WAV has invalid PCM metadata.")
    import array

    samples = array.array("h")
    samples.frombytes(raw)
    if os.sys.byteorder != "little":
        samples.byteswap()
    peak = max((abs(value) for value in samples), default=0) / 32768.0
    rms = math.sqrt(sum(float(value) * float(value) for value in samples) / max(1, len(samples))) / 32768.0
    clipping_ratio = sum(1 for value in samples if abs(value) >= 32760) / max(1, len(samples))
    if peak <= 1e-4 or rms <= 1e-5:
        raise VoiceRuntimeError(OUTPUT_VALIDATION_FAILED, "Generated WAV is effectively silent.")
    if clipping_ratio > 0.001:
        raise VoiceRuntimeError(
            OUTPUT_VALIDATION_FAILED,
            f"Generated WAV clipping ratio is too high: {clipping_ratio:.6f}.",
            {"clipping_ratio": clipping_ratio, "maximum": 0.001},
        )
    _ffmpeg_decode(path, ffmpeg)
    return {
        **file_record(path),
        "duration_seconds": frames / rate,
        "sample_rate": rate,
        "channels": channels,
        "peak": peak,
        "peak_dbfs": 20.0 * math.log10(max(peak, 1e-12)),
        "rms": rms,
        "rms_dbfs": 20.0 * math.log10(max(rms, 1e-12)),
        "clipping_ratio": clipping_ratio,
        "ffmpeg_decode": "Pass",
    }


class VoiceWorker:
    def __init__(
        self,
        *,
        profile_manager: VoiceProfileManager | None = None,
        registry: EngineRegistry | None = None,
        emitter: MarkerEmitter | None = None,
        cancel_token: CancellationToken | None = None,
        backend_factory: Callable[[Any], Any] | None = None,
        separator_factory: Callable[[Path], Any] | None = None,
        root: Path | None = None,
        runs_root: Path | None = None,
    ) -> None:
        self.root = (root or runtime_root()).resolve()
        self.profiles = profile_manager or VoiceProfileManager()
        self.registry = registry or EngineRegistry(self.root)
        self.emitter = emitter or MarkerEmitter()
        self.cancel_token = cancel_token or CancellationToken()
        self.backend_factory = backend_factory or self.registry.instantiate_backend
        self.separator_factory = separator_factory
        self.runs_root = (runs_root or default_runs_root()).resolve()
        self._last_stage_progress = 0.0
        self._seen_job_ids: set[str] = set()
        self._persistent_backends: dict[str, Any] = {}

    def _stage(self, name: str, progress: float) -> None:
        self.cancel_token.raise_if_cancelled()
        self._last_stage_progress = max(0.0, min(1.0, float(progress)))
        self.emitter.emit({"type": "stage", "name": name, "progress": progress})

    def _source_separator(self) -> Any:
        if self.separator_factory is not None:
            return self.separator_factory(self.root)
        # The import stays lazy so the CPU control environment and FrameExtract
        # thin client never import Demucs/Torch merely by opening the feature.
        from .source_separation import SourceSeparationRunner

        return SourceSeparationRunner(self.root)

    def _create_profile(self, job: dict[str, Any], run_dir: Path) -> dict[str, Any]:
        legacy_profile_type = str(job.get("profile_type", ""))
        legacy_profile_id = str(job.get("profile_id", ""))
        legacy_update = bool(job.get("update_existing", False))
        if legacy_update and legacy_profile_id:
            legacy_profile_type = str(
                self.profiles.load(legacy_profile_id).get("profile_type", legacy_profile_type)
            )
        if legacy_profile_type == "cloned":
            if legacy_update:
                self.profiles.consent(legacy_profile_id)
            elif not consent_is_confirmed(job.get("consent")):
                raise VoiceRuntimeError(
                    CONSENT_REQUIRED,
                    "Explicit rights confirmation is required before processing source media.",
                )
            raise VoiceRuntimeError(
                REFERENCE_APPROVAL_REQUIRED,
                "Cloned profiles must use prepare_profile_reference followed by commit_profile_reference.",
            )
        source = Path(str(job.get("input_path", "")))
        source_type = str(job.get("source_type", ""))
        profile_id = job.get("profile_id")
        if bool(job.get("update_existing", False)):
            if not profile_id:
                raise VoiceRuntimeError(INVALID_REQUEST, "update_existing requires profile_id.")
            self.profiles.consent(str(profile_id))
        elif not consent_is_confirmed(job.get("consent")):
            raise VoiceRuntimeError(
                CONSENT_REQUIRED,
                "Explicit rights confirmation is required before processing source media.",
            )
        self._stage("probe_source", 0.10)
        ffmpeg = resolve_ffmpeg(self.root, job.get("ffmpeg_path"))
        normalized = run_dir / "source_normalized.wav"
        self._stage("normalize_audio", 0.25)
        source_info = normalize_source(
            source, source_type, normalized, ffmpeg, self.cancel_token
        )
        self._stage("select_reference", 0.45)
        selection = job.get("selection") or {"mode": "auto"}
        mode = str(selection.get("mode", "auto"))
        auto_evidence: dict[str, Any] | None = None
        if mode == "auto":
            start, end, auto_evidence = choose_reference_auto(normalized)
        elif mode == "manual":
            try:
                start = float(selection["start_seconds"])
                end = float(selection["end_seconds"])
            except (KeyError, TypeError, ValueError) as exc:
                raise VoiceRuntimeError(
                    INVALID_REQUEST,
                    "Manual selection requires numeric start_seconds and end_seconds.",
                ) from exc
        else:
            raise VoiceRuntimeError(INVALID_REQUEST, f"Invalid selection mode: {mode}")
        preview = run_dir / "reference_preview.wav"
        reference_info = cut_reference(normalized, preview, start, end)
        reference_quality = {
            "status": "technical_pass_pending_listening",
            "selection_mode": mode,
            "selection_start_seconds": reference_info["start_seconds"],
            "selection_end_seconds": reference_info["end_seconds"],
            "selection_evidence": auto_evidence,
            "source_info": source_info,
        }
        self._stage("create_profile", 0.80)
        if bool(job.get("update_existing", False)):
            profile = self.profiles.update(
                str(profile_id),
                display_name=job.get("display_name"),
                default_language=job.get("default_language"),
                engine_preference=job.get("engine_preference"),
                reference_files=[preview],
                quality=reference_quality,
                is_base_voice_preset=job.get("is_base_voice_preset"),
            )
            operation = "updated"
        else:
            profile = self.profiles.create(
                profile_id=str(profile_id) if profile_id else None,
                display_name=str(job.get("display_name", "")),
                profile_type=str(job.get("profile_type", "")),
                source_type=source_type,
                default_language=str(job.get("default_language", "auto")),
                engine_preference=str(job.get("engine_preference", "auto")),
                reference_files=[preview],
                consent=job.get("consent"),
                quality=reference_quality,
                is_base_voice_preset=bool(job.get("is_base_voice_preset", False)),
            )
            operation = "created"
        return {
            "schema_version": 1,
            "status": "success",
            "action": "create_profile",
            "operation": operation,
            "profile": profile,
            "reference_preview": str(preview),
            "reference": reference_info,
        }

    @staticmethod
    def _submitted_revision(job: dict[str, Any]) -> int | None:
        if job.get("expected_profile_revision") is None:
            return None
        try:
            revision = int(job["expected_profile_revision"])
        except (TypeError, ValueError) as exc:
            raise VoiceRuntimeError(
                INVALID_REQUEST,
                "expected_profile_revision must be a positive integer when supplied.",
            ) from exc
        if revision < 1:
            raise VoiceRuntimeError(INVALID_REQUEST, "expected_profile_revision must be positive.")
        return revision

    @staticmethod
    def _target_speaker_window(job: dict[str, Any]) -> dict[str, float] | None:
        raw = job.get("target_speaker_window")
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise VoiceRuntimeError(
                INVALID_REQUEST, "target_speaker_window must be an object."
            )
        try:
            start = float(raw["start_seconds"])
            end = float(raw["end_seconds"])
        except (KeyError, TypeError, ValueError) as exc:
            raise VoiceRuntimeError(
                INVALID_REQUEST,
                "Target speaker window requires numeric start_seconds and end_seconds.",
            ) from exc
        if start < 0.0 or end <= start:
            raise VoiceRuntimeError(
                INVALID_REQUEST,
                "Target speaker window end must be greater than its non-negative start.",
                {"start_seconds": start, "end_seconds": end},
            )
        return {"start_seconds": start, "end_seconds": end}

    @staticmethod
    def _selection_preflight(
        job: dict[str, Any], target_window: dict[str, float] | None
    ) -> tuple[str, float | None, float | None]:
        selection = job.get("selection") or {"mode": "auto"}
        if not isinstance(selection, dict):
            raise VoiceRuntimeError(INVALID_REQUEST, "selection must be an object.")
        mode = str(selection.get("mode", "auto"))
        if mode == "auto":
            return mode, None, None
        if mode != "manual":
            raise VoiceRuntimeError(INVALID_REQUEST, f"Invalid selection mode: {mode}")
        try:
            start = float(selection["start_seconds"])
            end = float(selection["end_seconds"])
        except (KeyError, TypeError, ValueError) as exc:
            raise VoiceRuntimeError(
                INVALID_REQUEST,
                "Manual selection requires numeric start_seconds and end_seconds.",
            ) from exc
        validate_reference_duration(start, end)
        if target_window is not None and (
            start < target_window["start_seconds"] - 0.0005
            or end > target_window["end_seconds"] + 0.0005
        ):
            raise VoiceRuntimeError(
                INVALID_REQUEST,
                "Manual reference must be completely inside the target speaker window.",
                {
                    "selection": {"start_seconds": start, "end_seconds": end},
                    "target_speaker_window": target_window,
                },
            )
        return mode, start, end

    def _prepare_profile_reference(
        self, job: dict[str, Any], run_dir: Path
    ) -> dict[str, Any]:
        """Prepare immutable mix/voice-only evidence without activating it."""
        target_window = self._target_speaker_window(job)
        mode, manual_start, manual_end = self._selection_preflight(job, target_window)
        profile_id = str(job.get("profile_id", ""))
        update_existing = bool(job.get("update_existing", False))
        current_revision: int | None = None
        existing_mixed_primary: Path | None = None
        existing_quality: dict[str, Any] = {}
        profile: dict[str, Any]
        if update_existing:
            if not profile_id:
                raise VoiceRuntimeError(INVALID_REQUEST, "Existing profile repair requires profile_id.")
            profile = self.profiles.load(profile_id)
            if profile.get("profile_type") != "cloned":
                raise VoiceRuntimeError(
                    INVALID_REQUEST,
                    "Existing presets use the legacy preset update flow, not voice-only repair.",
                )
            if job.get("reported_background_audio") is True:
                primary_record = profile.get("reference_files", [None])[0]
                primary_relative = (
                    primary_record.get("path") if isinstance(primary_record, dict) else primary_record
                )
                if not isinstance(primary_relative, str):
                    raise VoiceRuntimeError(INVALID_REFERENCE, "Existing mixed primary is invalid.")
                existing_mixed_primary = (
                    self.profiles.root / profile_id / primary_relative
                ).resolve()
                quality_path = self.profiles.root / profile_id / "quality.json"
                existing_quality = read_json(quality_path) if quality_path.is_file() else {}
            self.profiles.consent(profile_id)
            if profile.get("enabled") is not True:
                raise VoiceRuntimeError(INVALID_REQUEST, f"Profile is disabled: {profile_id}")
            actual_revision = self.profiles.profile_revision(profile_id)
            submitted_revision = self._submitted_revision(job)
            if submitted_revision is not None and submitted_revision != actual_revision:
                raise VoiceRuntimeError(
                    PROFILE_REVISION_MISMATCH,
                    "Profile changed before reference preparation.",
                    {
                        "expected_revision": submitted_revision,
                        "actual_revision": actual_revision,
                    },
                )
            expected_identity = job.get("expected_profile_identity")
            if isinstance(expected_identity, dict):
                for key in ("profile_id", "display_name", "created_at"):
                    expected = expected_identity.get(key)
                    if expected is not None and profile.get(key) != expected:
                        raise VoiceRuntimeError(
                            PROFILE_REVISION_MISMATCH,
                            f"Profile identity changed before reference preparation: {key}",
                            {"field": key, "expected": expected, "actual": profile.get(key)},
                        )
            initial_state = (
                BACKGROUND_AUDIO_DETECTED
                if job.get("reported_background_audio") is True
                else NEEDS_MANUAL_REFERENCE
            )
            quarantine = self.profiles.set_reference_state(
                profile_id,
                initial_state,
                evidence={
                    "preparation_id": job["job_id"],
                    "reason": "reported_background_audio"
                    if initial_state == BACKGROUND_AUDIO_DETECTED
                    else "reference_repair_started",
                },
                expected_revision=actual_revision,
            )
            current_revision = int(quarantine["profile_revision"])
        else:
            if str(job.get("profile_type", "")) != "cloned":
                raise VoiceRuntimeError(
                    INVALID_REQUEST,
                    "prepare_profile_reference creates only cloned profiles; presets use create_profile.",
                )
            if not consent_is_confirmed(job.get("consent")):
                raise VoiceRuntimeError(
                    CONSENT_REQUIRED,
                    "Explicit rights confirmation is required before processing source media.",
                )
            display_name = str(job.get("display_name", "")).strip()
            if not display_name:
                raise VoiceRuntimeError(INVALID_REQUEST, "display_name must not be empty.")
            profile_id = (
                validate_profile_id(str(job["profile_id"]))
                if job.get("profile_id")
                else make_profile_id(display_name)
            )
            if (self.profiles.root / profile_id).exists():
                raise VoiceRuntimeError(
                    PROFILE_ID_CONFLICT,
                    (
                        f"Profile ID {profile_id} đã tồn tại. "
                        "Hãy đổi tên hoặc chọn Cập nhật reference."
                    ),
                    {"profile_id": profile_id, "preflight": True},
                )
            profile = {
                "profile_id": profile_id,
                "display_name": display_name,
                "created_at": None,
            }

        source = Path(str(job.get("input_path", "")))
        source_type = str(job.get("source_type", ""))
        ffmpeg = resolve_ffmpeg(self.root, job.get("ffmpeg_path"))
        normalized = run_dir / ".source_analysis.wav"
        separation_input = run_dir / ".separation_candidate.wav"
        separated_raw = run_dir / ".separated_vocals.wav"
        source_mix = run_dir / "ref_source_mix.wav"
        voice_only = run_dir / "ref_voice_only.wav"
        source_info: dict[str, Any] | None = None
        separator_metrics: dict[str, Any] | None = None
        try:
            self._stage("probe_source", 0.10)
            source_info = normalize_source(
                source, source_type, normalized, ffmpeg, self.cancel_token
            )
            if target_window is not None:
                source_duration = float(source_info["duration_seconds"])
                frame_tolerance = 1.0 / max(1, int(source_info["sample_rate"]))
                if target_window["end_seconds"] > source_duration + frame_tolerance:
                    raise VoiceRuntimeError(
                        INVALID_REFERENCE,
                        "Target speaker window exceeds the source duration.",
                        {
                            "target_speaker_window": target_window,
                            "source_duration_seconds": source_duration,
                        },
                    )
            self._stage("select_reference", 0.20)
            auto_evidence: dict[str, Any] | None = None
            if mode == "auto":
                prior_start = existing_quality.get("selection_start_seconds")
                prior_end = existing_quality.get("selection_end_seconds")
                if (
                    existing_mixed_primary is not None
                    and isinstance(prior_start, (int, float))
                    and isinstance(prior_end, (int, float))
                    and (
                        target_window is None
                        or (
                            float(prior_start) >= target_window["start_seconds"] - 0.0005
                            and float(prior_end) <= target_window["end_seconds"] + 0.0005
                        )
                    )
                ):
                    start, end = float(prior_start), float(prior_end)
                    auto_evidence = {
                        "source": "existing_profile_quality",
                        "audit_primary_sha256": sha256_file(existing_mixed_primary),
                    }
                else:
                    start, end, auto_evidence = choose_reference_auto(
                        normalized,
                        window_start_seconds=(
                            target_window["start_seconds"] if target_window else 0.0
                        ),
                        window_end_seconds=(
                            target_window["end_seconds"] if target_window else None
                        ),
                    )
            elif mode == "manual":
                assert manual_start is not None and manual_end is not None
                start, end = manual_start, manual_end

            self._stage("cut_candidate", 0.32)
            prior_start = existing_quality.get("selection_start_seconds")
            prior_end = existing_quality.get("selection_end_seconds")
            reuse_existing_mix = bool(
                existing_mixed_primary is not None
                and isinstance(prior_start, (int, float))
                and isinstance(prior_end, (int, float))
                and abs(float(prior_start) - start) <= 0.01
                and abs(float(prior_end) - end) <= 0.01
            )
            if reuse_existing_mix:
                assert existing_mixed_primary is not None
                existing_metadata = inspect_pcm_wav(existing_mixed_primary)
                existing_duration = float(existing_metadata["duration_seconds"])
                if not 8.0 <= existing_duration <= 15.0:
                    raise VoiceRuntimeError(
                        INVALID_REFERENCE,
                        "Existing mixed primary must be 8–15 seconds for audit preservation.",
                    )
                with existing_mixed_primary.open("rb") as input_handle, source_mix.open("xb") as output_handle:
                    while True:
                        chunk = input_handle.read(1024 * 1024)
                        if not chunk:
                            break
                        output_handle.write(chunk)
                    output_handle.flush()
                    os.fsync(output_handle.fileno())
                if sha256_file(source_mix) != sha256_file(existing_mixed_primary):
                    source_mix.unlink(missing_ok=True)
                    raise VoiceRuntimeError(INVALID_REFERENCE, "Mixed-primary audit copy hash mismatch.")
                mix_info = {
                    **existing_metadata,
                    "start_seconds": start,
                    "end_seconds": end,
                    "file": file_record(source_mix),
                    "audit_source": str(existing_mixed_primary),
                }
            else:
                mix_info = cut_reference(normalized, source_mix, start, end)
            candidate_info = prepare_separation_candidate(
                source,
                source_type,
                separation_input,
                start,
                end,
                ffmpeg,
                self.cancel_token,
            )
            self._stage("separate_background", 0.50)
            try:
                raw_metrics = self._source_separator().separate_vocals(
                    input_path=separation_input,
                    output_path=separated_raw,
                    work_dir=run_dir / ".source_separation",
                    progress=lambda name, value: self._stage(
                        name, 0.50 + max(0.0, min(1.0, float(value))) * 0.20
                    ),
                    cancel_token=self.cancel_token,
                )
                separator_metrics = raw_metrics if isinstance(raw_metrics, dict) else {}
            except VoiceRuntimeError:
                raise
            except Exception as exc:
                raise VoiceRuntimeError(
                    SOURCE_SEPARATION_FAILED,
                    f"Pinned Demucs 4.1.0 worker failed: {exc}",
                ) from exc

            normalize_voice_only(separated_raw, voice_only, ffmpeg, self.cancel_token)
            self._stage("validate_voice_only", 0.72)
            validation = validate_voice_only_reference(
                voice_only,
                source_mix,
                ffmpeg,
                background_was_reported=job.get("reported_background_audio") is True,
            )
            quality_status = str(validation.get("status", ""))
            review_only = job.get("candidate_review_only") is True
            candidate_status = quality_status
            if quality_status == BACKGROUND_AUDIO_DETECTED:
                candidate_status = BACKGROUND_AUDIO_DETECTED_PENDING_LISTENING

            # Demucs removes accompaniment; it does not prove that only one
            # human is speaking.  Auto selection is therefore always routed to
            # the explicit manual fallback in this implementation.  A review
            # batch is deliberately non-committable even when all technical
            # proxies pass: the user must first choose a winner.
            if quality_status == SOURCE_SEPARATION_NO_EFFECT:
                final_state = SOURCE_SEPARATION_NO_EFFECT
                ready_for_commit = False
            elif review_only:
                final_state = "TECHNICAL_PASS_PENDING_LISTENING"
                ready_for_commit = False
            elif quality_status == BACKGROUND_AUDIO_DETECTED:
                final_state = BACKGROUND_AUDIO_DETECTED_PENDING_LISTENING
                ready_for_commit = False
            elif mode == "auto" or quality_status == NEEDS_MANUAL_REFERENCE:
                final_state = NEEDS_MANUAL_REFERENCE
                ready_for_commit = False
            else:
                final_state = "TECHNICAL_PASS_PENDING_LISTENING"
                ready_for_commit = True
            # Review batches are immutable previews. They must never bump the
            # live profile revision or replace a previously approved reference.
            if update_existing and not review_only:
                assert current_revision is not None
                state_result = self.profiles.set_reference_state(
                    profile_id,
                    final_state,
                    evidence={
                        "preparation_id": job["job_id"],
                        "selection_mode": mode,
                        "selection_start_seconds": mix_info["start_seconds"],
                        "selection_end_seconds": mix_info["end_seconds"],
                        "candidate_status": candidate_status,
                        "candidate_review_only": review_only,
                        "target_speaker_window": target_window,
                        "validation": validation,
                    },
                    expected_revision=current_revision,
                )
                current_revision = int(state_result["profile_revision"])
            self._stage(
                "await_manual_reference" if not ready_for_commit else "await_listening",
                0.90,
            )
            identity = {
                key: profile.get(key)
                for key in ("profile_id", "display_name", "created_at")
            }
            primary_preview: Path | None = None
            if ready_for_commit:
                primary_preview = run_dir / "ref_primary.wav"
                with voice_only.open("rb") as source_handle, primary_preview.open("xb") as output_handle:
                    while True:
                        chunk = source_handle.read(1024 * 1024)
                        if not chunk:
                            break
                        output_handle.write(chunk)
                    output_handle.flush()
                    os.fsync(output_handle.fileno())
                if sha256_file(primary_preview) != sha256_file(voice_only):
                    primary_preview.unlink(missing_ok=True)
                    raise VoiceRuntimeError(INVALID_REFERENCE, "Prepared primary copy hash mismatch.")
            background_detected = quality_status == BACKGROUND_AUDIO_DETECTED
            artifacts = {
                "source_mix": {
                    **file_record(source_mix),
                    "background_status": "DETECTED"
                    if background_detected or job.get("reported_background_audio") is True
                    else "UNKNOWN",
                    "has_background": bool(
                        background_detected or job.get("reported_background_audio") is True
                    ),
                },
                "voice_only": {
                    **file_record(voice_only),
                    "validation_status": candidate_status,
                },
            }
            if primary_preview is not None:
                artifacts["primary"] = {
                    **file_record(primary_preview),
                    "committed": False,
                }
            message: str | None = None
            if candidate_status == BACKGROUND_AUDIO_DETECTED_PENDING_LISTENING:
                window_description = ""
                if target_window is not None:
                    window_description = (
                        " trong vùng "
                        f"{target_window['start_seconds']:g}–{target_window['end_seconds']:g} giây"
                    )
                message = (
                    f"Đoạn {start:g}–{end:g} giây vẫn còn âm thanh nền sau khi tách. "
                    f"Hãy nghe bản voice-only và chọn một đoạn khác{window_description}."
                )
            elif candidate_status == SOURCE_SEPARATION_NO_EFFECT:
                message = (
                    "Source separation produced a gain-scaled copy of the input mix; "
                    "the voice-only preview is excluded from commit."
                )
            return {
                "schema_version": 1,
                "status": "success",
                "action": "prepare_profile_reference",
                "preparation_id": job["job_id"],
                "profile_id": profile_id,
                "profile_identity": identity,
                "profile_status": final_state,
                "candidate_status": candidate_status,
                "profile_revision": current_revision,
                "ready_for_commit": ready_for_commit,
                "candidate_review_only": review_only,
                "message": message,
                "reference_preview": str(voice_only),
                "ref_source_mix": artifacts["source_mix"],
                "ref_voice_only": artifacts["voice_only"],
                "reference_artifacts": artifacts,
                "target_speaker_window": target_window,
                "selection": {
                    "mode": mode,
                    "start_seconds": mix_info["start_seconds"],
                    "end_seconds": mix_info["end_seconds"],
                    "auto_evidence": auto_evidence,
                },
                "source": {
                    "path": str(source.expanduser().resolve()),
                    "source_type": source_type,
                    "analysis": source_info,
                    "candidate": candidate_info,
                    "analysis_file_cleaned_after_run": True,
                },
                "separation": {
                    "package": "demucs==4.1.0",
                    "model": "htdemucs",
                    "two_stems": "vocals",
                    "device": "cpu",
                    "metrics": separator_metrics or {},
                },
                "reference_validation": validation,
            }
        finally:
            for temporary in (normalized, separation_input, separated_raw):
                temporary.unlink(missing_ok=True)

    def _commit_profile_reference(
        self, job: dict[str, Any], run_dir: Path
    ) -> dict[str, Any]:
        """Activate exactly one heard, manually confirmed preparation run."""
        profile_id = str(job.get("profile_id", ""))
        preparation_id = str(job.get("preparation_id", ""))
        try:
            uuid.UUID(preparation_id)
        except (ValueError, TypeError, AttributeError) as exc:
            raise VoiceRuntimeError(INVALID_REQUEST, "preparation_id must be a UUID.") from exc
        if job.get("user_listening_confirmed") is not True:
            raise VoiceRuntimeError(
                REFERENCE_APPROVAL_REQUIRED, "Explicit listening approval is required."
            )
        if job.get("single_speaker_confirmed") is not True:
            raise VoiceRuntimeError(
                REFERENCE_APPROVAL_REQUIRED,
                "Explicit confirmation of one speaker with no overlapping voice is required.",
            )
        if job.get("use_voice_only") is not True:
            raise VoiceRuntimeError(
                REFERENCE_APPROVAL_REQUIRED, "The user must explicitly choose the voice-only reference."
            )
        prepare_dir = (self.runs_root / preparation_id).resolve()
        try:
            prepare_dir.relative_to(self.runs_root)
        except ValueError as exc:
            raise VoiceRuntimeError(INVALID_REQUEST, "Preparation run escapes the runs root.") from exc
        prepare_job_path = prepare_dir / "job.json"
        prepare_result_path = prepare_dir / "result.json"
        if not prepare_job_path.is_file() or not prepare_result_path.is_file():
            raise VoiceRuntimeError(INVALID_REFERENCE, "Preparation job evidence is missing.")
        prepare_job = read_json(prepare_job_path)
        prepared = read_json(prepare_result_path)
        if (
            prepare_job.get("action") != "prepare_profile_reference"
            or prepared.get("status") != "success"
            or prepared.get("action") != "prepare_profile_reference"
            or prepared.get("job_id") != preparation_id
            or prepared.get("preparation_id") != preparation_id
        ):
            raise VoiceRuntimeError(INVALID_REQUEST, "Preparation evidence does not match this profile.")
        prepared_profile_id = str(prepared.get("profile_id", ""))
        if profile_id and profile_id != prepared_profile_id:
            raise VoiceRuntimeError(INVALID_REQUEST, "Preparation evidence does not match this profile.")
        profile_id = prepared_profile_id
        if prepared.get("ready_for_commit") is not True or prepared.get("profile_status") != "TECHNICAL_PASS_PENDING_LISTENING":
            raise VoiceRuntimeError(
                REFERENCE_APPROVAL_REQUIRED,
                "Preparation is not ready for listening approval and commit.",
            )
        selection = prepared.get("selection")
        if not isinstance(selection, dict) or selection.get("mode") != "manual":
            raise VoiceRuntimeError(
                NEEDS_MANUAL_REFERENCE,
                "Single-speaker commit requires a manually selected reference interval.",
            )
        prepared_revision = prepared.get("profile_revision")
        expected_revision = int(prepared_revision) if prepared_revision is not None else None
        source_mix = prepare_dir / "ref_source_mix.wav"
        voice_only = prepare_dir / "ref_voice_only.wav"
        primary_preview = prepare_dir / "ref_primary.wav"
        prepared_artifacts = prepared.get("reference_artifacts")
        submitted_artifacts = job.get("reference_artifacts")
        if not isinstance(prepared_artifacts, dict) or not isinstance(submitted_artifacts, dict):
            raise VoiceRuntimeError(INVALID_REFERENCE, "Preparation reference records are invalid.")
        resolved_paths = {
            "source_mix": source_mix,
            "voice_only": voice_only,
            "primary": primary_preview,
        }
        for role, artifact_path in resolved_paths.items():
            prepared_record = prepared_artifacts.get(role)
            submitted_record = submitted_artifacts.get(role)
            if not isinstance(prepared_record, dict) or not isinstance(submitted_record, dict):
                raise VoiceRuntimeError(INVALID_REFERENCE, f"Missing reference artifact role: {role}")
            prepared_hash = str(prepared_record.get("sha256", "")).upper()
            submitted_hash = str(submitted_record.get("sha256", "")).upper()
            if (
                not artifact_path.is_file()
                or prepared_hash != submitted_hash
                or sha256_file(artifact_path) != prepared_hash
            ):
                raise VoiceRuntimeError(
                    INVALID_REFERENCE, f"Approved preparation hash does not match: {role}"
                )
        if sha256_file(voice_only) != sha256_file(primary_preview):
            raise VoiceRuntimeError(INVALID_REFERENCE, "Prepared primary is not the voice-only artifact.")

        self._stage("revalidate_reference", 0.30)
        ffmpeg = resolve_ffmpeg(
            self.root, job.get("ffmpeg_path") or prepare_job.get("ffmpeg_path")
        )
        validation = validate_voice_only_reference(
            voice_only,
            source_mix,
            ffmpeg,
            background_was_reported=prepare_job.get("reported_background_audio") is True,
        )
        if validation.get("status") != "PASS":
            state = str(validation.get("status") or BACKGROUND_AUDIO_DETECTED)
            if state not in {
                BACKGROUND_AUDIO_DETECTED,
                NEEDS_MANUAL_REFERENCE,
                SOURCE_SEPARATION_NO_EFFECT,
            }:
                state = BACKGROUND_AUDIO_DETECTED
            if expected_revision is not None:
                self.profiles.set_reference_state(
                    profile_id,
                    state,
                    evidence={"preparation_id": preparation_id, "validation": validation},
                    expected_revision=expected_revision,
                )
            raise VoiceRuntimeError(
                state,
                "The prepared voice-only reference no longer passes validation.",
                {"validation": validation},
            )

        prepared_source = prepared.get("source")
        prepared_selection = prepared.get("selection")
        source_candidate: dict[str, Any] | None = None
        if isinstance(prepared_source, dict) and isinstance(
            prepared_source.get("candidate"), dict
        ):
            candidate_file = prepared_source["candidate"].get("file")
            if isinstance(candidate_file, dict):
                source_candidate = {
                    key: candidate_file.get(key)
                    for key in ("size_bytes", "sha256")
                    if candidate_file.get(key) is not None
                }
        preparation_provenance = {
            "schema_version": 1,
            "preparation_id": preparation_id,
            "submitted_source_path": (
                prepared_source.get("path") if isinstance(prepared_source, dict) else None
            ),
            "submitted_source_type": (
                prepared_source.get("source_type")
                if isinstance(prepared_source, dict)
                else None
            ),
            "selection": dict(prepared_selection)
            if isinstance(prepared_selection, dict)
            else {},
            "target_speaker_window": (
                dict(prepared["target_speaker_window"])
                if isinstance(prepared.get("target_speaker_window"), dict)
                else None
            ),
            "candidate_file": source_candidate,
            "reported_background_audio": (
                prepare_job.get("reported_background_audio") is True
            ),
            "original_profile_source_preserved": expected_revision is not None,
        }

        self._stage("commit_profile_reference", 0.70)
        if expected_revision is None:
            installed = self.profiles.create_voice_only_profile(
                profile_id=profile_id,
                display_name=str(prepare_job.get("display_name", "")),
                source_type=str(prepare_job.get("source_type", "")),
                source_language=prepare_job.get("source_language"),
                default_language=str(prepare_job.get("default_language", "auto")),
                engine_preference=str(prepare_job.get("engine_preference", "auto")),
                source_mix=source_mix,
                voice_only=voice_only,
                consent=prepare_job.get("consent"),
                preparation_id=preparation_id,
                validation=validation,
                preparation_provenance=preparation_provenance,
                user_listening_confirmed=True,
                manual_selection_confirmed=True,
            )
        else:
            installed = self.profiles.install_voice_only_reference_set(
                profile_id,
                source_mix=source_mix,
                voice_only=voice_only,
                expected_revision=expected_revision,
                prepare_job_id=preparation_id,
                validation=validation,
                preparation_provenance=preparation_provenance,
                user_listening_approved=True,
                single_speaker_confirmed=True,
            )
        profile = installed["profile"]
        profile_dir = self.profiles.root / profile_id
        self._stage("reference_ready", 0.95)
        return {
            "schema_version": 1,
            "status": "success",
            "action": "commit_profile_reference",
            "operation": "updated" if expected_revision is not None else "created",
            "profile": profile,
            "profile_id": profile_id,
            "profile_status": "READY",
            "profile_revision": installed["profile_revision"],
            "history_path": installed.get("history_path"),
            "ref_source_mix": file_record(
                profile_dir / "references" / "ref_source_mix.wav"
            ),
            "ref_voice_only": file_record(
                profile_dir / "references" / "ref_voice_only.wav"
            ),
            "ref_primary": file_record(profile_dir / "references" / "ref_primary.wav"),
            "reference_validation": validation,
        }

    def _synthesize(self, job: dict[str, Any], run_dir: Path, memory: PeakMemoryMonitor) -> dict[str, Any]:
        self._stage("load_profile", 0.10)
        profile_id = str(job.get("profile_id", ""))
        profile = self.profiles.assert_synthesis_ready(profile_id)
        if profile.get("enabled") is not True:
            raise VoiceRuntimeError(INVALID_REQUEST, f"Profile is disabled: {profile_id}")
        self.profiles.consent(profile_id)
        references = self.profiles.resolve_references(profile_id)
        profile = {
            **profile,
            "_profile_path": str((self.profiles.root / profile_id).resolve()),
            "_profile_revision": self.profiles.profile_revision(profile_id),
        }
        text = str(job.get("text", "")).strip()
        if not text:
            raise VoiceRuntimeError(INVALID_REQUEST, "Synthesis text must not be empty.")
        language = str(job.get("language", "auto")).strip().lower().replace("_", "-")
        if language == "auto":
            language = str(profile.get("default_language", "auto")).strip().lower().replace("_", "-")
        device = str(job.get("device", "cpu"))
        requested_engine = str(job.get("engine", "auto"))
        preferred = str(profile.get("engine_preference", "auto"))
        if language == "auto":
            # Resolve an unspecified profile language from runtime capability
            # order. No language list or Vietnamese special case lives here.
            capabilities = list(self.registry.engines())
            if requested_engine != "auto":
                capabilities = [item for item in capabilities if item.id == requested_engine]
            elif preferred != "auto":
                preferred_items = [item for item in capabilities if item.id == preferred]
                capabilities = preferred_items + [
                    item for item in capabilities if item.id != preferred
                ]
            for item in capabilities:
                if (
                    bool(getattr(item, "available", False))
                    and device in tuple(getattr(item, "devices", ()))
                    and tuple(getattr(item, "languages", ()))
                ):
                    language = str(item.languages[0])
                    break
        if requested_engine == "auto":
            if preferred != "auto":
                try:
                    capability = self.registry.select(preferred, language, device)
                except VoiceRuntimeError:
                    capability = self.registry.select("auto", language, device)
            else:
                capability = self.registry.select("auto", language, device)
        else:
            capability = self.registry.select(requested_engine, language, device)
        effective_job = {
            **job,
            "text": text,
            "language": language,
            "engine": capability.id,
            "speed": float(job.get("speed", 1.0)),
            "seed": int(job.get("seed", 42)),
            "keep_model_loaded": bool(
                job.get("keep_model_loaded", job.get("keep_model_warm", False))
            ),
        }
        effective_job["keep_model_warm"] = effective_job["keep_model_loaded"]
        output = run_dir / "generated.wav"
        if output.exists():
            raise FileExistsError(f"Refusing to overwrite generated output: {output}")
        # Finish the complete profile/reference/output preflight before a heavy
        # XTTS child can load its checkpoint.
        ffmpeg = resolve_ffmpeg(self.root, job.get("ffmpeg_path"))
        for reference in references:
            _ffmpeg_decode(reference, ffmpeg)
        write_probe = run_dir / f".write-probe-{uuid.uuid4().hex}.tmp"
        try:
            with write_probe.open("xb") as handle:
                handle.write(b"ok")
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            if write_probe.exists():
                write_probe.unlink()

        keep_loaded = effective_job["keep_model_loaded"]
        if keep_loaded and not bool(getattr(capability, "supports_keep_model_loaded", False)):
            raise VoiceRuntimeError(
                INVALID_REQUEST,
                f"Engine does not support keeping the model loaded: {capability.id}",
            )
        if not keep_loaded:
            stale = self._persistent_backends.pop(capability.id, None)
            if stale is not None:
                close_stale = getattr(stale, "close", None)
                if callable(close_stale):
                    close_stale()
        backend = self._persistent_backends.get(capability.id)
        if backend is None:
            backend = self.backend_factory(capability)
            if keep_loaded:
                self._persistent_backends[capability.id] = backend
        try:
            backend_metrics = backend.synthesize(
                job=effective_job,
                profile=profile,
                references=references,
                output_path=output,
                progress=self._stage,
                cancel_token=self.cancel_token,
            )
        except Exception:
            if keep_loaded:
                self._persistent_backends.pop(capability.id, None)
                close_failed = getattr(backend, "close", None)
                if callable(close_failed):
                    close_failed()
            raise
        finally:
            close = getattr(backend, "close", None)
            if callable(close) and not keep_loaded:
                close()
        self._stage("validate", 0.90)
        validation = validate_generated_wav(output, ffmpeg)
        return {
            "schema_version": 1,
            "status": "success",
            "profile_id": profile_id,
            "engine": capability.id,
            "language": language,
            "duration_seconds": validation["duration_seconds"],
            "peak_ram_gib": memory.peak_bytes / (1024**3),
            "output_audio": str(output),
            "output_validation": validation,
            "backend_metrics": backend_metrics,
        }

    def execute(self, raw_job: Any) -> tuple[int, dict[str, Any]]:
        started = time.perf_counter()
        result_path: Path | None = None
        memory = PeakMemoryMonitor()
        self._last_stage_progress = 0.0
        try:
            job = validate_job(raw_job)
            if job["job_id"] in self._seen_job_ids:
                self.emitter.clear_log_path()
                raise VoiceRuntimeError(
                    DUPLICATE_JOB_REJECTED,
                    f"Duplicate job rejected: {job['job_id']}",
                    {"job_id": job["job_id"]},
                )
            self._seen_job_ids.add(job["job_id"])
            run_dir, result_path = _prepare_run(job, self.runs_root)
            self.emitter.set_log_path(run_dir / "run.log")
            with memory:
                if job["action"] == "create_profile":
                    result = self._create_profile(job, run_dir)
                elif job["action"] == "prepare_profile_reference":
                    result = self._prepare_profile_reference(job, run_dir)
                elif job["action"] == "commit_profile_reference":
                    result = self._commit_profile_reference(job, run_dir)
                else:
                    result = self._synthesize(job, run_dir, memory)
            result["job_id"] = job["job_id"]
            result["elapsed_seconds"] = time.perf_counter() - started
            result["peak_ram_gib"] = memory.peak_bytes / (1024**3)
            result["completed_at"] = utc_now()
            write_json_exclusive(result_path, result)
            self.emitter.emit(
                {
                    "type": "result",
                    "status": "success",
                    "job_id": job["job_id"],
                    "action": result.get("action"),
                    "operation": result.get("operation"),
                    "preparation_id": result.get("preparation_id"),
                    "output_audio": result.get("output_audio"),
                    "reference_preview": result.get("reference_preview"),
                    "ref_source_mix": result.get("ref_source_mix"),
                    "ref_voice_only": result.get("ref_voice_only"),
                    "ref_primary": result.get("ref_primary"),
                    "reference_artifacts": result.get("reference_artifacts"),
                    "reference_validation": result.get("reference_validation"),
                    "ready_for_commit": result.get("ready_for_commit"),
                    "profile_status": result.get("profile_status"),
                    "candidate_status": result.get("candidate_status"),
                    "candidate_review_only": result.get("candidate_review_only"),
                    "target_speaker_window": result.get("target_speaker_window"),
                    "message": result.get("message"),
                    "profile_revision": result.get("profile_revision"),
                    "result_path": str(result_path),
                    "profile": result.get("profile"),
                    "elapsed_seconds": result.get("elapsed_seconds"),
                    "peak_ram_gib": result.get("peak_ram_gib"),
                }
            )
            return 0, result
        except VoiceRuntimeError as exc:
            error = exc
        except FileExistsError as exc:
            error = VoiceRuntimeError(INVALID_REQUEST, str(exc))
        except Exception as exc:
            action = raw_job.get("action") if isinstance(raw_job, dict) else None
            code = (
                SYNTHESIS_FAILED
                if action == "synthesize"
                else REFERENCE_UPDATE_FAILED
                if action == "commit_profile_reference"
                else REFERENCE_VALIDATION_FAILED
                if action == "prepare_profile_reference"
                else INVALID_REQUEST
            )
            error = VoiceRuntimeError(code, f"Unexpected worker failure: {exc}", {"traceback": traceback.format_exc()})
        failure = {
            "schema_version": 1,
            "status": "failed",
            "error_code": error.code,
            "message": error.message,
            "details": error.details,
            "elapsed_seconds": time.perf_counter() - started,
            "peak_ram_gib": memory.peak_bytes / (1024**3),
            "completed_at": utc_now(),
        }
        if result_path is not None and not result_path.exists():
            write_json_exclusive(result_path, failure)
        self.emitter.emit(
            {
                "type": "stage",
                "name": "failed",
                "progress": 0.0,
                "terminal": True,
                "failed_after_progress": self._last_stage_progress,
            }
        )
        self.emitter.emit(error.as_dict())
        self.emitter.emit({"type": "result", "status": "failed", "error_code": error.code})
        return (130 if error.code == CANCELLED else 2), failure

    def shutdown(self) -> None:
        """Release every cached engine exactly once."""
        backends = list(self._persistent_backends.values())
        self._persistent_backends.clear()
        for backend in backends:
            close = getattr(backend, "close", None)
            if callable(close):
                close()


def install_signal_handlers(token: CancellationToken) -> None:
    def request_cancel(_signum: int, _frame: Any) -> None:
        token.cancel()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        value = getattr(signal, name, None)
        if value is not None:
            try:
                signal.signal(value, request_cancel)
            except (OSError, ValueError):
                pass
