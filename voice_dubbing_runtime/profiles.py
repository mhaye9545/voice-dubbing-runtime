"""Crash-safe, consent-gated reusable voice profile storage."""

from __future__ import annotations

import os
import re
import shutil
import unicodedata
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

from .errors import (
    ALREADY_EXISTS,
    BACKGROUND_AUDIO_DETECTED,
    BACKGROUND_AUDIO_DETECTED_PENDING_LISTENING,
    CONSENT_REQUIRED,
    INVALID_REFERENCE,
    INVALID_REQUEST,
    NEEDS_MANUAL_REFERENCE,
    PROFILE_REVISION_MISMATCH,
    PROFILE_NOT_FOUND,
    REFERENCE_APPROVAL_REQUIRED,
    REFERENCE_UPDATE_FAILED,
    SOURCE_SEPARATION_NO_EFFECT,
    VoiceRuntimeError,
)
from .io_utils import (
    atomic_replace_json,
    file_record,
    read_json,
    sha256_file,
    utc_now,
    write_json_exclusive,
)
from .paths import default_profiles_root


PROFILE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
PROFILE_TYPES = {"cloned", "preset"}
SOURCE_TYPES = {"video", "audio"}
REFERENCE_EXTENSIONS = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg"}
CONSENT_STATEMENT = "I confirm that I have the right to use and clone this voice."
VOICE_ONLY_POLICY = "voice_only_v1"
REFERENCE_STATES = {
    "READY",
    "BACKGROUND_AUDIO_DETECTED",
    "BACKGROUND_AUDIO_DETECTED_PENDING_LISTENING",
    "NEEDS_MANUAL_REFERENCE",
    "TECHNICAL_PASS_PENDING_LISTENING",
    "SOURCE_SEPARATION_NO_EFFECT",
}


def consent_is_confirmed(consent: Any) -> bool:
    return isinstance(consent, dict) and (
        consent.get("confirmed") is True
        or consent.get("accepted") is True
        or consent.get("authorized") is True
        or consent.get("authorized_use_confirmed") is True
    )


def make_profile_id(display_name: str) -> str:
    decomposed = unicodedata.normalize("NFKD", display_name)
    ascii_name = "".join(char for char in decomposed if not unicodedata.combining(char))
    ascii_name = ascii_name.encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "_", ascii_name).strip("_")
    return (slug or "profile")[:48]


def validate_profile_id(value: str) -> str:
    if not isinstance(value, str) or not PROFILE_ID_PATTERN.fullmatch(value):
        raise VoiceRuntimeError(
            INVALID_REQUEST,
            "profile_id must match ^[a-z0-9][a-z0-9_-]{0,63}$.",
            {"profile_id": value},
        )
    return value


def _validate_language(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise VoiceRuntimeError(INVALID_REQUEST, f"{field} must not be empty.")
    language = value.strip().lower().replace("_", "-")
    if language != "auto" and not re.fullmatch(r"[a-z]{2,3}(?:-[a-z0-9]{2,8})*", language):
        raise VoiceRuntimeError(INVALID_REQUEST, f"Invalid {field}: {value}", {field: value})
    return language


def build_consent_record(
    consent: dict[str, Any] | None, profile_id: str, timestamp: str | None = None
) -> dict[str, Any]:
    if not consent_is_confirmed(consent):
        raise VoiceRuntimeError(
            CONSENT_REQUIRED,
            "Explicit rights confirmation is required when creating a voice profile.",
        )
    assert isinstance(consent, dict)
    now = timestamp or utc_now()
    return {
        **consent,
        "schema_version": 1,
        "profile_id": profile_id,
        "authorized": True,
        "statement": str(consent.get("statement") or CONSENT_STATEMENT).strip(),
        "granted_at": str(
            consent.get("granted_at") or consent.get("confirmed_at") or now
        ),
        "source": str(consent.get("source") or "user_checkbox"),
    }


class VoiceProfileManager:
    """Owns one profile root and installs new profiles by same-parent rename."""

    def __init__(
        self,
        profiles_root: Path | None = None,
        *,
        staged_validator: Callable[[Path], None] | None = None,
    ) -> None:
        self.root = (profiles_root or default_profiles_root()).resolve()
        self._staged_validator = staged_validator

    def _profile_dir(self, profile_id: str) -> Path:
        validate_profile_id(profile_id)
        return self.root / profile_id

    def _require_profile_dir(self, profile_id: str) -> Path:
        directory = self._profile_dir(profile_id)
        if not directory.is_dir():
            raise VoiceRuntimeError(
                PROFILE_NOT_FOUND,
                f"Voice profile was not found: {profile_id}",
                {"profile_id": profile_id},
            )
        return directory

    @contextmanager
    def _exclusive_lock(self, lock: Path, subject: str) -> Iterator[None]:
        try:
            descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise VoiceRuntimeError(ALREADY_EXISTS, f"Already being modified: {subject}") from exc
        try:
            os.write(descriptor, f"pid={os.getpid()}\n".encode("ascii"))
            os.close(descriptor)
            yield
        finally:
            lock.unlink(missing_ok=True)

    @contextmanager
    def _creation_lock(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        with self._exclusive_lock(self.root / ".profile-create.lock", "profile store"):
            yield

    @contextmanager
    def _mutation_lock(self, directory: Path) -> Iterator[None]:
        # Keep the lock outside the profile so readers never mistake it for
        # profile content and the live directory stays stable during updates.
        with self._exclusive_lock(
            self.root / f".{directory.name}.mutation.lock", f"profile {directory.name}"
        ):
            yield

    def allocate_unique_id(self, requested: str) -> str:
        base = validate_profile_id(requested)
        if not (self.root / base).exists():
            return base
        suffix = 2
        while suffix < 100000:
            stem = base[: max(1, 64 - len(str(suffix)) - 1)].rstrip("_")
            candidate = f"{stem}_{suffix}"
            if not (self.root / candidate).exists():
                return candidate
            suffix += 1
        raise VoiceRuntimeError(ALREADY_EXISTS, f"No unique profile ID is available for: {base}")

    @staticmethod
    def _validate_reference_sources(reference_files: Sequence[str | Path]) -> list[Path]:
        if not reference_files:
            raise VoiceRuntimeError(INVALID_REFERENCE, "At least one reference file is required.")
        sources: list[Path] = []
        for raw in reference_files:
            path = Path(raw).expanduser().resolve()
            if not path.is_file() or path.stat().st_size == 0:
                raise VoiceRuntimeError(
                    INVALID_REFERENCE, f"Reference file is missing or empty: {path}"
                )
            if path.suffix.lower() not in REFERENCE_EXTENSIONS:
                raise VoiceRuntimeError(
                    INVALID_REFERENCE,
                    f"Unsupported reference extension: {path.suffix}",
                    {"supported_extensions": sorted(REFERENCE_EXTENSIONS)},
                )
            sources.append(path)
        return sources

    @staticmethod
    def _copy_references(
        sources: Sequence[Path], references_dir: Path, *, canonical_name: str | None = None
    ) -> list[dict[str, Any]]:
        references_dir.mkdir(parents=True, exist_ok=False)
        records: list[dict[str, Any]] = []
        for index, source in enumerate(sources, 1):
            filename = (
                f"{canonical_name}{source.suffix.lower()}"
                if canonical_name and len(sources) == 1
                else f"reference_{index:03d}{source.suffix.lower()}"
            )
            target = references_dir / filename
            with source.open("rb") as input_handle, target.open("xb") as output_handle:
                shutil.copyfileobj(input_handle, output_handle, 1024 * 1024)
                output_handle.flush()
                os.fsync(output_handle.fileno())
            if target.stat().st_size == 0 or sha256_file(target) != sha256_file(source):
                raise VoiceRuntimeError(
                    INVALID_REFERENCE, f"Reference copy verification failed: {source}"
                )
            records.append(file_record(target, relative_to=references_dir.parent))
        return records

    @staticmethod
    def _copy_evidence_once(source: Path, target: Path) -> None:
        """Preserve one immutable legacy metadata file before replacing it."""
        if target.exists():
            return
        try:
            with source.open("rb") as input_handle, target.open("xb") as output_handle:
                shutil.copyfileobj(input_handle, output_handle, 1024 * 1024)
                output_handle.flush()
                os.fsync(output_handle.fileno())
            if sha256_file(target) != sha256_file(source):
                raise VoiceRuntimeError(
                    INVALID_REQUEST, f"Legacy evidence backup hash mismatch: {source}"
                )
        except Exception:
            target.unlink(missing_ok=True)
            raise

    @staticmethod
    def _lock_payload(profile: dict[str, Any], directory: Path, revision: int) -> dict[str, Any]:
        references: list[dict[str, Any]] = []
        for item in profile["reference_files"]:
            relative = item["path"] if isinstance(item, dict) else item
            path = directory / relative
            if path.is_file():
                references.append(file_record(path, relative_to=directory))
        assets: dict[str, dict[str, Any]] = {}
        raw_assets = profile.get("reference_assets")
        if isinstance(raw_assets, dict):
            for name, item in raw_assets.items():
                relative = item.get("path") if isinstance(item, dict) else None
                path = directory / str(relative or "")
                if relative and path.is_file():
                    assets[str(name)] = file_record(path, relative_to=directory)
        payload = {
            "schema_version": 1,
            "profile_id": profile["profile_id"],
            "revision": revision,
            "profile_json_sha256": sha256_file(directory / "profile.json"),
            "reference_files": references,
            "updated_at": profile["updated_at"],
        }
        if assets:
            payload["reference_assets"] = assets
        quality_path = directory / "quality.json"
        if quality_path.is_file() and profile.get("reference_policy"):
            payload["quality_json_sha256"] = sha256_file(quality_path)
        return payload

    @staticmethod
    def _read_profile_metadata(directory: Path, expected_id: str | None = None) -> dict[str, Any]:
        profile_path = directory / "profile.json"
        if not profile_path.is_file():
            raise VoiceRuntimeError(INVALID_REQUEST, "profile.json is missing.")
        profile = read_json(profile_path)
        if not isinstance(profile, dict) or profile.get("schema_version") != 1:
            raise VoiceRuntimeError(INVALID_REQUEST, "profile.json schema is invalid.")
        profile_id = profile.get("profile_id")
        if not isinstance(profile_id, str) or not PROFILE_ID_PATTERN.fullmatch(profile_id):
            raise VoiceRuntimeError(INVALID_REQUEST, "profile.json profile_id is invalid.")
        if expected_id is not None and profile_id != expected_id:
            raise VoiceRuntimeError(
                INVALID_REQUEST,
                f"Profile folder/id mismatch: {expected_id} != {profile_id}",
            )
        required = (
            "display_name",
            "profile_type",
            "source_type",
            "source_language",
            "default_language",
            "engine_preference",
            "reference_files",
            "created_at",
            "updated_at",
            "enabled",
        )
        missing = [field for field in required if field not in profile]
        if missing:
            raise VoiceRuntimeError(
                INVALID_REQUEST, "profile.json is missing required fields.", {"missing": missing}
            )
        if profile["profile_type"] not in PROFILE_TYPES or profile["source_type"] not in SOURCE_TYPES:
            raise VoiceRuntimeError(INVALID_REQUEST, "Profile type/source type is invalid.")
        _validate_language(profile["source_language"], "source_language")
        _validate_language(profile["default_language"], "default_language")
        if not isinstance(profile["reference_files"], list) or not profile["reference_files"]:
            raise VoiceRuntimeError(INVALID_REFERENCE, "Profile has no references.")
        return profile

    @classmethod
    def _validate_profile_tree(
        cls, directory: Path, expected_id: str | None = None, *, verify_lock: bool = True
    ) -> dict[str, Any]:
        profile = cls._read_profile_metadata(directory, expected_id)
        for record in profile["reference_files"]:
            relative = record.get("path") if isinstance(record, dict) else record
            if not isinstance(relative, str):
                raise VoiceRuntimeError(INVALID_REFERENCE, "Reference path is invalid.")
            reference = (directory / relative).resolve()
            try:
                reference.relative_to(directory.resolve())
            except ValueError as exc:
                raise VoiceRuntimeError(
                    INVALID_REFERENCE, "Reference path escapes the profile directory."
                ) from exc
            if not reference.is_file() or reference.stat().st_size == 0:
                raise VoiceRuntimeError(INVALID_REFERENCE, f"Reference is missing: {reference}")
            expected_hash = record.get("sha256") if isinstance(record, dict) else None
            if expected_hash and sha256_file(reference) != str(expected_hash).upper():
                raise VoiceRuntimeError(
                    INVALID_REFERENCE, f"Reference hash mismatch: {reference}"
                )
        raw_assets = profile.get("reference_assets")
        if raw_assets is not None:
            if not isinstance(raw_assets, dict):
                raise VoiceRuntimeError(INVALID_REFERENCE, "reference_assets must be an object.")
            for name, record in raw_assets.items():
                relative = record.get("path") if isinstance(record, dict) else None
                if not isinstance(relative, str):
                    raise VoiceRuntimeError(
                        INVALID_REFERENCE, f"Reference asset path is invalid: {name}"
                    )
                asset = (directory / relative).resolve()
                try:
                    asset.relative_to(directory.resolve())
                except ValueError as exc:
                    raise VoiceRuntimeError(
                        INVALID_REFERENCE, "Reference asset escapes the profile directory."
                    ) from exc
                if not asset.is_file() or asset.stat().st_size == 0:
                    raise VoiceRuntimeError(INVALID_REFERENCE, f"Reference asset is missing: {asset}")
                expected_hash = record.get("sha256") if isinstance(record, dict) else None
                if expected_hash and sha256_file(asset) != str(expected_hash).upper():
                    raise VoiceRuntimeError(
                        INVALID_REFERENCE, f"Reference asset hash mismatch: {asset}"
                    )
        for name in ("quality.json", "consent.json", "profile.lock"):
            if not (directory / name).is_file():
                raise VoiceRuntimeError(INVALID_REQUEST, f"{name} is missing.")
        if verify_lock:
            lock = read_json(directory / "profile.lock")
            if lock.get("profile_id") != profile["profile_id"]:
                raise VoiceRuntimeError(INVALID_REQUEST, "profile.lock profile_id mismatch.")
            if lock.get("profile_json_sha256") != sha256_file(directory / "profile.json"):
                raise VoiceRuntimeError(INVALID_REQUEST, "profile.lock metadata hash mismatch.")
            expected_quality_hash = lock.get("quality_json_sha256")
            if expected_quality_hash and expected_quality_hash != sha256_file(
                directory / "quality.json"
            ):
                raise VoiceRuntimeError(INVALID_REQUEST, "profile.lock quality hash mismatch.")
        return profile

    def create(
        self,
        *,
        display_name: str,
        profile_type: str,
        source_type: str,
        default_language: str,
        engine_preference: str,
        reference_files: Sequence[str | Path],
        consent: dict[str, Any] | None,
        profile_id: str | None = None,
        source_language: str | None = None,
        quality: dict[str, Any] | None = None,
        is_base_voice_preset: bool = False,
        collision_policy: str = "suffix",
    ) -> dict[str, Any]:
        name = display_name.strip() if isinstance(display_name, str) else ""
        if not name:
            raise VoiceRuntimeError(INVALID_REQUEST, "display_name must not be empty.")
        if profile_type not in PROFILE_TYPES or source_type not in SOURCE_TYPES:
            raise VoiceRuntimeError(INVALID_REQUEST, "Invalid profile_type or source_type.")
        if profile_type == "preset" and source_type != "audio":
            raise VoiceRuntimeError(INVALID_REQUEST, "Preset profiles must use an audio source.")
        if is_base_voice_preset and profile_type != "preset":
            raise VoiceRuntimeError(
                INVALID_REQUEST, "Only preset profiles can be marked as a base voice preset."
            )
        output_language = _validate_language(default_language, "default_language")
        source_code = _validate_language(
            source_language if source_language is not None else output_language,
            "source_language",
        )
        sources = self._validate_reference_sources(reference_files)
        requested_id = validate_profile_id(profile_id) if profile_id else make_profile_id(name)
        if collision_policy not in {"suffix", "error"}:
            raise VoiceRuntimeError(INVALID_REQUEST, f"Invalid collision policy: {collision_policy}")
        timestamp = utc_now()

        with self._creation_lock():
            if collision_policy == "error" and (self.root / requested_id).exists():
                raise VoiceRuntimeError(ALREADY_EXISTS, f"Profile already exists: {requested_id}")
            identifier = self.allocate_unique_id(requested_id)
            consent_record = build_consent_record(consent, identifier, timestamp)
            staging = self.root / f".{identifier}.creating-{uuid.uuid4().hex}"
            destination = self.root / identifier
            staging.mkdir()
            installed = False
            try:
                reference_records = self._copy_references(sources, staging / "references")
                profile = {
                    "schema_version": 1,
                    "profile_id": identifier,
                    "display_name": name,
                    "profile_type": profile_type,
                    "source_type": source_type,
                    "source_language": source_code,
                    "default_language": output_language,
                    "engine_preference": str(engine_preference or "auto").strip(),
                    "is_base_voice_preset": bool(is_base_voice_preset),
                    "reference_files": reference_records,
                    "created_at": timestamp,
                    "updated_at": timestamp,
                    "enabled": True,
                }
                quality_record = {
                    "schema_version": 1,
                    "profile_id": identifier,
                    "status": "not_rated",
                    **(quality or {}),
                }
                write_json_exclusive(staging / "profile.json", profile)
                write_json_exclusive(staging / "quality.json", quality_record)
                write_json_exclusive(staging / "consent.json", consent_record)
                write_json_exclusive(
                    staging / "profile.lock", self._lock_payload(profile, staging, 1)
                )
                self._validate_profile_tree(staging, identifier)
                if self._staged_validator is not None:
                    self._staged_validator(staging)
                os.rename(staging, destination)
                installed = True
                self._validate_profile_tree(destination, identifier)
                return profile
            except Exception:
                if installed and destination.exists() and not staging.exists():
                    os.rename(destination, staging)
                shutil.rmtree(staging, ignore_errors=True)
                raise

    def load(self, profile_id: str) -> dict[str, Any]:
        directory = self._require_profile_dir(profile_id)
        return self._validate_profile_tree(directory, profile_id)

    def profile_revision(self, profile_id: str) -> int:
        directory = self._require_profile_dir(profile_id)
        self._validate_profile_tree(directory, profile_id)
        lock = read_json(directory / "profile.lock")
        try:
            revision = int(lock.get("revision", 1))
        except (TypeError, ValueError) as exc:
            raise VoiceRuntimeError(INVALID_REQUEST, "profile.lock revision is invalid.") from exc
        if revision < 1:
            raise VoiceRuntimeError(INVALID_REQUEST, "profile.lock revision is invalid.")
        return revision

    def consent(self, profile_id: str) -> dict[str, Any]:
        directory = self._require_profile_dir(profile_id)
        path = directory / "consent.json"
        if not path.is_file():
            raise VoiceRuntimeError(CONSENT_REQUIRED, f"Profile has no consent record: {profile_id}")
        payload = read_json(path)
        if (
            payload.get("schema_version") != 1
            or payload.get("authorized") is not True
            or payload.get("profile_id") != profile_id
        ):
            raise VoiceRuntimeError(
                CONSENT_REQUIRED,
                f"Profile requires rights reconfirmation: {profile_id}",
                {"status": "CONSENT_RECONFIRM_REQUIRED"},
            )
        return payload

    def resolve_references(self, profile_id: str) -> list[Path]:
        directory = self._require_profile_dir(profile_id)
        profile = self.assert_synthesis_ready(profile_id)
        return [(directory / item["path"]).resolve() for item in profile["reference_files"]]

    def assert_synthesis_ready(self, profile_id: str) -> dict[str, Any]:
        """Reject non-ready voice-only profiles even if a caller bypasses the GUI."""
        profile = self.load(profile_id)
        policy = profile.get("reference_policy")
        if not isinstance(policy, dict) or policy.get("kind") != VOICE_ONLY_POLICY:
            return profile  # Backward-compatible legacy profile.
        state = str(policy.get("state", "")).upper()
        if state == "READY":
            return profile
        if state == BACKGROUND_AUDIO_DETECTED:
            raise VoiceRuntimeError(
                BACKGROUND_AUDIO_DETECTED,
                "Đoạn tham chiếu vẫn còn nhạc hoặc âm thanh nền. Hãy chọn đoạn khác hoặc chạy lại tách giọng.",
                {"profile_id": profile_id, "profile_status": state},
            )
        if state == NEEDS_MANUAL_REFERENCE:
            raise VoiceRuntimeError(
                NEEDS_MANUAL_REFERENCE,
                "A manual single-speaker reference selection is required.",
                {"profile_id": profile_id, "profile_status": state},
            )
        raise VoiceRuntimeError(
            REFERENCE_APPROVAL_REQUIRED,
            "The voice-only reference must be heard and explicitly approved before synthesis.",
            {"profile_id": profile_id, "profile_status": state or "UNKNOWN"},
        )

    @staticmethod
    def _invalid_row(directory: Path, error: Exception) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "profile_id": directory.name,
            "display_name": directory.name,
            "profile_type": "unknown",
            "source_type": "unknown",
            "source_language": "auto",
            "default_language": "auto",
            "engine_preference": "auto",
            "reference_files": [],
            "enabled": False,
            "profile_path": str(directory),
            "valid": False,
            "profile_status": "PROFILE_ERROR",
            "profile_error": str(error),
            "error": str(error),
            "status": "PROFILE_ERROR",
        }

    def list(self) -> list[dict[str, Any]]:
        if not self.root.is_dir():
            return []
        rows: list[dict[str, Any]] = []
        for directory in self.root.iterdir():
            if directory.name.startswith(".") or not directory.is_dir():
                continue
            try:
                profile = self._validate_profile_tree(directory, directory.name)
                try:
                    self.consent(directory.name)
                    policy = profile.get("reference_policy")
                    if isinstance(policy, dict) and policy.get("kind") == VOICE_ONLY_POLICY:
                        profile_status = str(policy.get("state") or "").upper()
                        if profile_status not in REFERENCE_STATES:
                            raise VoiceRuntimeError(
                                INVALID_REFERENCE,
                                f"Unknown voice-only reference state: {profile_status}",
                            )
                    else:
                        profile_status = "READY"
                    profile_error = None
                except VoiceRuntimeError as consent_error:
                    if consent_error.code != CONSENT_REQUIRED:
                        raise
                    profile_status = "CONSENT_RECONFIRM_REQUIRED"
                    profile_error = str(consent_error)
                rows.append(
                    {
                        **profile,
                        "profile_path": str(directory),
                        "profile_revision": self.profile_revision(directory.name),
                        "valid": True,
                        "profile_status": profile_status,
                        "profile_error": profile_error,
                        "error": profile_error,
                    }
                )
            except Exception as exc:
                rows.append(self._invalid_row(directory, exc))
        return sorted(rows, key=lambda row: str(row.get("display_name", "")).casefold())

    def migrate_legacy(
        self,
        profile_id: str,
        *,
        display_name: str,
        profile_type: str,
        source_type: str,
        default_language: str,
        engine_preference: str,
        source_language: str | None = None,
        canonical_reference: str | None = None,
        is_base_voice_preset: bool = False,
    ) -> dict[str, Any]:
        directory = self._require_profile_dir(profile_id)
        try:
            current = self.load(profile_id)
            if canonical_reference is None or current["reference_files"] == [
                file_record(directory / canonical_reference, relative_to=directory)
            ]:
                return current
        except VoiceRuntimeError:
            pass
        profile_path = directory / "profile.json"
        legacy_path = directory / "profile.phase1.json"
        source_profile = read_json(legacy_path if legacy_path.is_file() else profile_path)
        current_consent_path = directory / "consent.json"
        consent_path = directory / "consent.phase1.json"
        source_consent = read_json(
            consent_path if consent_path.is_file() else current_consent_path
        )
        if not consent_is_confirmed(source_consent):
            raise VoiceRuntimeError(CONSENT_REQUIRED, f"Legacy consent is invalid: {profile_id}")
        relative_references: list[str]
        if canonical_reference:
            relative_references = [canonical_reference]
        else:
            relative_references = [
                item.get("path") if isinstance(item, dict) else item
                for item in source_profile.get("reference_files", [])
            ]
        reference_records: list[dict[str, Any]] = []
        for relative in relative_references:
            if not isinstance(relative, str):
                raise VoiceRuntimeError(INVALID_REFERENCE, "Legacy reference path is invalid.")
            reference = (directory / relative).resolve()
            try:
                reference.relative_to(directory.resolve())
            except ValueError as exc:
                raise VoiceRuntimeError(INVALID_REFERENCE, "Legacy reference escapes profile.") from exc
            if not reference.is_file() or reference.stat().st_size == 0:
                raise VoiceRuntimeError(INVALID_REFERENCE, f"Legacy reference is missing: {reference}")
            reference_records.append(file_record(reference, relative_to=directory))
        timestamp = utc_now()
        language = _validate_language(default_language, "default_language")
        source_code = _validate_language(source_language or language, "source_language")
        migrated = {
            **source_profile,
            "schema_version": 1,
            "profile_id": profile_id,
            "display_name": display_name.strip(),
            "profile_type": profile_type,
            "source_type": source_type,
            "source_language": source_code,
            "default_language": language,
            "engine_preference": str(engine_preference or "auto"),
            "is_base_voice_preset": bool(is_base_voice_preset),
            "reference_files": reference_records,
            "created_at": str(source_profile.get("created_at") or timestamp),
            "updated_at": timestamp,
            "enabled": True,
            "legacy_phase1_profile": "profile.phase1.json",
        }
        consent_record = build_consent_record(source_consent, profile_id, timestamp)
        consent_record["legacy_phase1_consent"] = "consent.phase1.json"
        quality_path = directory / "quality.json"
        with self._mutation_lock(directory):
            self._copy_evidence_once(profile_path, legacy_path)
            self._copy_evidence_once(current_consent_path, consent_path)
            if not quality_path.is_file():
                write_json_exclusive(
                    quality_path,
                    {"schema_version": 1, "profile_id": profile_id, "status": "not_rated"},
                )
            atomic_replace_json(profile_path, migrated)
            atomic_replace_json(current_consent_path, consent_record)
            lock_path = directory / "profile.lock"
            lock_payload = self._lock_payload(migrated, directory, 1)
            if lock_path.exists():
                atomic_replace_json(lock_path, lock_payload)
            else:
                write_json_exclusive(lock_path, lock_payload)
        return self.load(profile_id)

    def confirm_consent(
        self, profile_id: str, consent: dict[str, Any] | None
    ) -> dict[str, Any]:
        """Bind one explicit rights confirmation to exactly one profile.

        Existing authorized consent is returned without rewriting timestamps or
        metadata, making retries safe after a lost CLI/UI response.
        """
        directory = self._require_profile_dir(profile_id)
        profile = self.load(profile_id)
        consent_path = directory / "consent.json"
        existing = read_json(consent_path) if consent_path.is_file() else {}
        if (
            existing.get("schema_version") == 1
            and existing.get("profile_id") == profile_id
            and existing.get("authorized") is True
        ):
            return {
                "schema_version": 1,
                "status": "ALREADY_CONFIRMED",
                "changed": False,
                "profile": profile,
                "consent": existing,
            }
        if not isinstance(consent, dict) or not (
            consent.get("confirmed") is True or consent.get("accepted") is True
        ):
            raise VoiceRuntimeError(
                CONSENT_REQUIRED,
                "A new explicit confirmation is required for this profile.",
            )
        asserted_identity = consent.get("profile_id") or consent.get("voice_profile_id")
        if asserted_identity not in (None, "", profile_id):
            raise VoiceRuntimeError(
                CONSENT_REQUIRED,
                "Consent identity does not match the requested profile.",
                {"profile_id": profile_id, "consent_profile_id": asserted_identity},
            )
        confirmed = build_consent_record(consent, profile_id)
        with self._mutation_lock(directory):
            # Recheck after acquiring the lock so concurrent retries remain idempotent.
            latest = read_json(consent_path) if consent_path.is_file() else {}
            if latest.get("profile_id") == profile_id and latest.get("authorized") is True:
                return {
                    "schema_version": 1,
                    "status": "ALREADY_CONFIRMED",
                    "changed": False,
                    "profile": self.load(profile_id),
                    "consent": latest,
                }
            old_profile = read_json(directory / "profile.json")
            old_consent = latest
            old_lock = read_json(directory / "profile.lock")
            updated = dict(old_profile)
            updated["status"] = "READY"
            updated["updated_at"] = utc_now()
            try:
                atomic_replace_json(consent_path, confirmed)
                atomic_replace_json(directory / "profile.json", updated)
                atomic_replace_json(
                    directory / "profile.lock",
                    self._lock_payload(
                        updated, directory, int(old_lock.get("revision", 1)) + 1
                    ),
                )
            except Exception:
                atomic_replace_json(consent_path, old_consent)
                atomic_replace_json(directory / "profile.json", old_profile)
                atomic_replace_json(directory / "profile.lock", old_lock)
                raise
        return {
            "schema_version": 1,
            "status": "CONSENT_CONFIRMED",
            "changed": True,
            "profile": self.load(profile_id),
            "consent": self.consent(profile_id),
        }

    @staticmethod
    def _copy_verified_replace(source: Path, target: Path) -> dict[str, Any]:
        """Copy through a same-directory temp, fsync, hash-check and replace."""
        source = source.expanduser().resolve()
        if not source.is_file() or source.stat().st_size == 0:
            raise VoiceRuntimeError(INVALID_REFERENCE, f"Reference source is missing: {source}")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            with source.open("rb") as input_handle, temporary.open("xb") as output_handle:
                shutil.copyfileobj(input_handle, output_handle, 1024 * 1024)
                output_handle.flush()
            # Windows can reject fsync on a descriptor that is not open for
            # both reading and writing. Reopen the completed temporary file in
            # r+b mode before the durability barrier, then atomically replace.
            with temporary.open("r+b") as durable_handle:
                durable_handle.flush()
                os.fsync(durable_handle.fileno())
            if temporary.stat().st_size != source.stat().st_size:
                raise VoiceRuntimeError(INVALID_REFERENCE, "Reference copy size verification failed.")
            if sha256_file(temporary) != sha256_file(source):
                raise VoiceRuntimeError(INVALID_REFERENCE, "Reference copy hash verification failed.")
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return file_record(target, relative_to=target.parent.parent)

    def install_final_reference_set(
        self,
        profile_id: str,
        *,
        source_mix: Path,
        primary: Path,
        voice_only: Path | None,
        expected_revision: int,
        prepare_job_id: str,
        validation: dict[str, Any],
        selected_reference: dict[str, Any],
        reference_processing: dict[str, Any],
        target_speaker_window: dict[str, float],
        single_speaker_confirmed: bool,
        display_name: str = "Lester Holt EN",
    ) -> dict[str, Any]:
        """Commit a validated natural-reference set in one profile revision.

        The primary may be the clean source mix or a lightly cleaned mix.  A
        voice-only asset is installed only when the caller proves separation
        was effective; a scaled copy is never materialized as ref_voice_only.
        """
        if single_speaker_confirmed is not True:
            raise VoiceRuntimeError(
                REFERENCE_APPROVAL_REQUIRED,
                "A confirmed single-speaker reference is required.",
            )
        if validation.get("status") != "PASS":
            raise VoiceRuntimeError(
                REFERENCE_UPDATE_FAILED,
                "Final reference technical validation did not pass.",
                {"validation": validation},
            )
        if not isinstance(target_speaker_window, dict):
            raise VoiceRuntimeError(INVALID_REQUEST, "target_speaker_window is required.")
        window_start = float(target_speaker_window.get("start_seconds", -1.0))
        window_end = float(target_speaker_window.get("end_seconds", -1.0))
        selected_start = float(selected_reference.get("start_seconds", -1.0))
        selected_end = float(selected_reference.get("end_seconds", -1.0))
        if (
            window_start < 0.0
            or window_end <= window_start
            or selected_start < window_start
            or selected_end > window_end
            or selected_end <= selected_start
        ):
            raise VoiceRuntimeError(
                REFERENCE_UPDATE_FAILED,
                "Selected reference is outside the target speaker window.",
            )
        sources = self._validate_reference_sources(
            [source_mix, primary] + ([voice_only] if voice_only is not None else [])
        )
        source_mix = sources[0]
        primary = sources[1]
        voice_only = sources[2] if len(sources) == 3 else None
        separation_effective = reference_processing.get("separation_effective") is True
        if voice_only is not None and not separation_effective:
            raise VoiceRuntimeError(
                SOURCE_SEPARATION_NO_EFFECT,
                "An ineffective separation output cannot be installed as ref_voice_only.wav.",
            )
        self.consent(profile_id)

        def mutate(
            staging: Path,
            profile: dict[str, Any],
            quality: dict[str, Any],
            revision: int,
        ) -> None:
            references = staging / "references"
            mix_target = references / "ref_source_mix.wav"
            primary_target = references / "ref_primary.wav"
            self._copy_verified_replace(source_mix, mix_target)
            self._copy_verified_replace(primary, primary_target)
            assets: dict[str, Any] = {
                "source_mix": file_record(mix_target, relative_to=staging),
                "primary": file_record(primary_target, relative_to=staging),
            }
            if voice_only is not None:
                voice_target = references / "ref_voice_only.wav"
                self._copy_verified_replace(voice_only, voice_target)
                assets["voice_only"] = file_record(voice_target, relative_to=staging)

            now = utc_now()
            profile.update(
                {
                    "status": "READY",
                    "display_name": str(display_name).strip(),
                    "profile_type": "cloned",
                    "source_type": "audio",
                    "source_language": "en",
                    "default_language": "en",
                    "engine_preference": "xtts_v2_multilingual",
                    "reference_files": [file_record(primary_target, relative_to=staging)],
                    "reference_assets": assets,
                    "target_speaker_window": {
                        "start_seconds": window_start,
                        "end_seconds": window_end,
                    },
                    "selected_reference": selected_reference,
                    "reference_processing": reference_processing,
                    "reference_policy": {
                        "schema_version": 1,
                        "kind": VOICE_ONLY_POLICY,
                        "state": "READY",
                        "primary_variant": selected_reference.get("variant"),
                        "voice_only_status": (
                            "AVAILABLE" if voice_only is not None else "UNAVAILABLE"
                        ),
                        "prepare_job_id": str(prepare_job_id),
                        "committed_at": now,
                    },
                    "updated_at": now,
                    "enabled": True,
                }
            )
            quality.update(
                {
                    "schema_version": 1,
                    "profile_id": profile_id,
                    "status": "READY",
                    "final_reference_validation": validation,
                    "single_speaker_confirmed": True,
                    "prepare_job_id": str(prepare_job_id),
                    "selected_reference": selected_reference,
                    "reference_processing": reference_processing,
                }
            )
            atomic_replace_json(staging / "profile.json", profile)
            atomic_replace_json(staging / "quality.json", quality)
            atomic_replace_json(
                staging / "profile.lock", self._lock_payload(profile, staging, revision)
            )

        return self._replace_profile_tree(
            profile_id,
            expected_revision=expected_revision,
            operation="final-reference-commit",
            mutate_staging=mutate,
        )

    def _replace_profile_tree(
        self,
        profile_id: str,
        *,
        expected_revision: int | None,
        operation: str,
        mutate_staging: Callable[[Path, dict[str, Any], dict[str, Any], int], None],
    ) -> dict[str, Any]:
        """Validate in UUID staging, snapshot history, then update in place.

        The live profile directory is never renamed or replaced.  This keeps
        its identity stable for an open GUI and prevents a failed update from
        making the profile disappear.  Individual files use same-directory
        temporary files plus ``os.replace``; ``profile.lock`` is installed
        last as the commit marker.
        """
        directory = self._require_profile_dir(profile_id)
        with self._mutation_lock(directory):
            profile = self._validate_profile_tree(directory, profile_id)
            old_lock = read_json(directory / "profile.lock")
            try:
                revision = int(old_lock.get("revision", 1))
            except (TypeError, ValueError) as exc:
                raise VoiceRuntimeError(INVALID_REQUEST, "profile.lock revision is invalid.") from exc
            if expected_revision is not None and revision != int(expected_revision):
                raise VoiceRuntimeError(
                    PROFILE_REVISION_MISMATCH,
                    "Voice profile changed after reference preparation.",
                    {
                        "profile_id": profile_id,
                        "expected_revision": int(expected_revision),
                        "actual_revision": revision,
                    },
                )
            staging_root = self.root / ".staging"
            staging_root.mkdir(parents=True, exist_ok=True)
            staging = staging_root / uuid.uuid4().hex
            shutil.copytree(directory, staging, copy_function=shutil.copy2)
            snapshot: Path | None = None
            original_files = {
                path.relative_to(directory)
                for path in directory.rglob("*")
                if path.is_file()
            }

            def sync_tree(source_tree: Path, target_tree: Path) -> None:
                files = [path for path in source_tree.rglob("*") if path.is_file()]
                files.sort(
                    key=lambda path: (
                        2
                        if path.name == "profile.lock"
                        else 1
                        if path.name in {"profile.json", "quality.json", "consent.json"}
                        else 0,
                        path.as_posix(),
                    )
                )
                for source_file in files:
                    relative = source_file.relative_to(source_tree)
                    self._copy_verified_replace(source_file, target_tree / relative)

            try:
                staged_profile = read_json(staging / "profile.json")
                staged_quality = read_json(staging / "quality.json")
                mutate_staging(staging, staged_profile, staged_quality, revision + 1)
                self._validate_profile_tree(staging, profile_id)
                if self._staged_validator is not None:
                    self._staged_validator(staging)

                history_root = self.root / ".history" / profile_id
                history_root.mkdir(parents=True, exist_ok=True)
                timestamp = utc_now().replace(":", "").replace("-", "")
                snapshot = history_root / (
                    f"r{revision:06d}-{timestamp}-{operation}-{uuid.uuid4().hex[:8]}"
                )
                shutil.copytree(directory, snapshot, copy_function=shutil.copy2)
                try:
                    sync_tree(staging, directory)
                    installed_profile = self._validate_profile_tree(directory, profile_id)
                except Exception:
                    if snapshot.exists():
                        sync_tree(snapshot, directory)
                        for live_file in [
                            path for path in directory.rglob("*") if path.is_file()
                        ]:
                            relative = live_file.relative_to(directory)
                            if relative not in original_files:
                                live_file.unlink(missing_ok=True)
                        self._validate_profile_tree(directory, profile_id)
                    raise
                return {
                    "schema_version": 1,
                    "status": "success",
                    "operation": operation,
                    "profile": installed_profile,
                    "profile_revision": revision + 1,
                    "history_path": str(snapshot),
                    "staging_policy": "uuid_in_place_update",
                }
            finally:
                if staging.exists():
                    shutil.rmtree(staging, ignore_errors=True)
                try:
                    staging_root.rmdir()
                except OSError:
                    pass

    def set_reference_state(
        self,
        profile_id: str,
        state: str,
        *,
        evidence: dict[str, Any] | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        state = str(state).upper()
        if state not in REFERENCE_STATES or state == "READY":
            raise VoiceRuntimeError(
                INVALID_REQUEST,
                "Only a validated reference commit may activate READY.",
                {"submitted_state": state},
            )
        self.consent(profile_id)

        def mutate(
            staging: Path,
            profile: dict[str, Any],
            quality: dict[str, Any],
            revision: int,
        ) -> None:
            policy = dict(profile.get("reference_policy") or {})
            policy.update(
                {
                    "schema_version": 1,
                    "kind": VOICE_ONLY_POLICY,
                    "state": state,
                    "updated_at": utc_now(),
                }
            )
            if evidence:
                policy["state_evidence"] = evidence
            profile["reference_policy"] = policy
            profile["updated_at"] = utc_now()
            quality.update(
                {
                    "schema_version": 1,
                    "profile_id": profile_id,
                    "status": state,
                    "reference_state_evidence": evidence or {},
                }
            )
            atomic_replace_json(staging / "profile.json", profile)
            atomic_replace_json(staging / "quality.json", quality)
            atomic_replace_json(
                staging / "profile.lock", self._lock_payload(profile, staging, revision)
            )

        return self._replace_profile_tree(
            profile_id,
            expected_revision=expected_revision,
            operation="reference-state",
            mutate_staging=mutate,
        )

    def install_voice_only_reference_set(
        self,
        profile_id: str,
        *,
        source_mix: Path,
        voice_only: Path,
        expected_revision: int,
        prepare_job_id: str,
        validation: dict[str, Any],
        preparation_provenance: dict[str, Any],
        user_listening_approved: bool,
        single_speaker_confirmed: bool,
    ) -> dict[str, Any]:
        """Commit the canonical three-file layout through a staged in-place update."""
        if user_listening_approved is not True or single_speaker_confirmed is not True:
            raise VoiceRuntimeError(
                REFERENCE_APPROVAL_REQUIRED,
                "Listening approval and single-speaker confirmation are both required.",
            )
        if validation.get("status") != "PASS":
            raise VoiceRuntimeError(
                INVALID_REFERENCE,
                "Only a voice-only reference that passed the pinned quality gate can be committed.",
            )
        self.consent(profile_id)
        sources = self._validate_reference_sources([source_mix, voice_only])
        source_mix, voice_only = sources

        def mutate(
            staging: Path,
            profile: dict[str, Any],
            quality: dict[str, Any],
            revision: int,
        ) -> None:
            references = staging / "references"
            mix_target = references / "ref_source_mix.wav"
            voice_target = references / "ref_voice_only.wav"
            primary_target = references / "ref_primary.wav"
            self._copy_verified_replace(source_mix, mix_target)
            self._copy_verified_replace(voice_only, voice_target)
            self._copy_verified_replace(voice_only, primary_target)
            if sha256_file(voice_target) != sha256_file(primary_target):
                raise VoiceRuntimeError(
                    INVALID_REFERENCE, "ref_primary.wav is not byte-identical to ref_voice_only.wav."
                )
            profile["reference_files"] = [file_record(primary_target, relative_to=staging)]
            profile["reference_assets"] = {
                "source_mix": file_record(mix_target, relative_to=staging),
                "voice_only": file_record(voice_target, relative_to=staging),
            }
            profile["reference_policy"] = {
                "schema_version": 1,
                "kind": VOICE_ONLY_POLICY,
                "state": "READY",
                "validation_revision": validation.get("validation_revision"),
                "prepare_job_id": str(prepare_job_id),
                "committed_at": utc_now(),
            }
            profile["updated_at"] = utc_now()
            quality.update(
                {
                    "schema_version": 1,
                    "profile_id": profile_id,
                    "status": "READY",
                    "voice_only_validation": validation,
                    "user_listening_approved": True,
                    "single_speaker_confirmed": True,
                    "prepare_job_id": str(prepare_job_id),
                    "voice_only_reference_provenance": preparation_provenance,
                }
            )
            atomic_replace_json(staging / "profile.json", profile)
            atomic_replace_json(staging / "quality.json", quality)
            atomic_replace_json(
                staging / "profile.lock", self._lock_payload(profile, staging, revision)
            )

        return self._replace_profile_tree(
            profile_id,
            expected_revision=expected_revision,
            operation="voice-only-commit",
            mutate_staging=mutate,
        )

    def create_voice_only_profile(
        self,
        *,
        profile_id: str,
        display_name: str,
        source_type: str,
        source_language: str | None,
        default_language: str,
        engine_preference: str,
        source_mix: Path,
        voice_only: Path,
        consent: dict[str, Any] | None,
        preparation_id: str,
        validation: dict[str, Any],
        preparation_provenance: dict[str, Any],
        user_listening_confirmed: bool,
        manual_selection_confirmed: bool,
    ) -> dict[str, Any]:
        """Atomically create a cloned profile from one approved preparation."""
        identifier = validate_profile_id(profile_id)
        name = str(display_name).strip()
        if not name:
            raise VoiceRuntimeError(INVALID_REQUEST, "display_name must not be empty.")
        if source_type not in SOURCE_TYPES:
            raise VoiceRuntimeError(INVALID_REQUEST, "Invalid source_type.")
        if user_listening_confirmed is not True or manual_selection_confirmed is not True:
            raise VoiceRuntimeError(
                REFERENCE_APPROVAL_REQUIRED,
                "Listening and manual single-speaker confirmation are required.",
            )
        if validation.get("status") != "PASS":
            raise VoiceRuntimeError(INVALID_REFERENCE, "Voice-only validation did not pass.")
        sources = self._validate_reference_sources([source_mix, voice_only])
        source_mix, voice_only = sources
        language = _validate_language(default_language, "default_language")
        source_code = _validate_language(source_language or language, "source_language")
        timestamp = utc_now()
        consent_record = build_consent_record(consent, identifier, timestamp)

        with self._creation_lock():
            destination = self.root / identifier
            if destination.exists():
                raise VoiceRuntimeError(ALREADY_EXISTS, f"Profile already exists: {identifier}")
            staging = self.root / f".{identifier}.voice-only-{uuid.uuid4().hex}"
            staging.mkdir(parents=False, exist_ok=False)
            installed = False
            try:
                references = staging / "references"
                references.mkdir(parents=False, exist_ok=False)
                mix_target = references / "ref_source_mix.wav"
                voice_target = references / "ref_voice_only.wav"
                primary_target = references / "ref_primary.wav"
                self._copy_verified_replace(source_mix, mix_target)
                self._copy_verified_replace(voice_only, voice_target)
                self._copy_verified_replace(voice_only, primary_target)
                profile = {
                    "schema_version": 1,
                    "profile_id": identifier,
                    "display_name": name,
                    "profile_type": "cloned",
                    "source_type": source_type,
                    "source_language": source_code,
                    "default_language": language,
                    "engine_preference": str(engine_preference or "auto"),
                    "is_base_voice_preset": False,
                    "reference_files": [file_record(primary_target, relative_to=staging)],
                    "reference_assets": {
                        "source_mix": file_record(mix_target, relative_to=staging),
                        "voice_only": file_record(voice_target, relative_to=staging),
                    },
                    "reference_policy": {
                        "schema_version": 1,
                        "kind": VOICE_ONLY_POLICY,
                        "state": "READY",
                        "validation_revision": validation.get("validation_revision"),
                        "prepare_job_id": str(preparation_id),
                        "committed_at": timestamp,
                    },
                    "created_at": timestamp,
                    "updated_at": timestamp,
                    "enabled": True,
                }
                quality = {
                    "schema_version": 1,
                    "profile_id": identifier,
                    "status": "READY",
                    "voice_only_validation": validation,
                    "user_listening_approved": True,
                    "single_speaker_confirmed": True,
                    "prepare_job_id": str(preparation_id),
                    "voice_only_reference_provenance": preparation_provenance,
                }
                write_json_exclusive(staging / "profile.json", profile)
                write_json_exclusive(staging / "quality.json", quality)
                write_json_exclusive(staging / "consent.json", consent_record)
                write_json_exclusive(
                    staging / "profile.lock", self._lock_payload(profile, staging, 1)
                )
                self._validate_profile_tree(staging, identifier)
                if self._staged_validator is not None:
                    self._staged_validator(staging)
                os.rename(staging, destination)
                installed = True
                installed_profile = self._validate_profile_tree(destination, identifier)
                return {
                    "schema_version": 1,
                    "status": "success",
                    "operation": "voice-only-create",
                    "profile": installed_profile,
                    "profile_revision": 1,
                    "history_path": None,
                }
            except Exception:
                if installed and destination.exists() and not staging.exists():
                    os.rename(destination, staging)
                shutil.rmtree(staging, ignore_errors=True)
                raise

    def update(
        self,
        profile_id: str,
        *,
        display_name: str | None = None,
        source_language: str | None = None,
        default_language: str | None = None,
        engine_preference: str | None = None,
        enabled: bool | None = None,
        reference_files: Sequence[str | Path] | None = None,
        quality: dict[str, Any] | None = None,
        is_base_voice_preset: bool | None = None,
    ) -> dict[str, Any]:
        directory = self._require_profile_dir(profile_id)
        self.consent(profile_id)
        new_sources = self._validate_reference_sources(reference_files) if reference_files else []
        with self._mutation_lock(directory):
            profile = self.load(profile_id)
            policy = profile.get("reference_policy")
            if (
                new_sources
                and isinstance(policy, dict)
                and policy.get("kind") == VOICE_ONLY_POLICY
            ):
                raise VoiceRuntimeError(
                    REFERENCE_APPROVAL_REQUIRED,
                    "Voice-only profile references must be changed through commit_profile_reference.",
                )
            old_lock = read_json(directory / "profile.lock")
            if display_name is not None:
                if not display_name.strip():
                    raise VoiceRuntimeError(INVALID_REQUEST, "display_name must not be empty.")
                profile["display_name"] = display_name.strip()
            if source_language is not None:
                profile["source_language"] = _validate_language(
                    source_language, "source_language"
                )
            if default_language is not None:
                profile["default_language"] = _validate_language(
                    default_language, "default_language"
                )
            if engine_preference is not None:
                profile["engine_preference"] = str(engine_preference or "auto")
            if enabled is not None:
                profile["enabled"] = bool(enabled)
            if is_base_voice_preset is not None:
                if is_base_voice_preset and profile["profile_type"] != "preset":
                    raise VoiceRuntimeError(
                        INVALID_REQUEST, "Only preset profiles can be a base voice preset."
                    )
                profile["is_base_voice_preset"] = bool(is_base_voice_preset)
            if new_sources:
                references_dir = directory / "references"
                next_index = len(list(references_dir.glob("reference_*"))) + 1
                copied: list[dict[str, Any]] = []
                for offset, source in enumerate(new_sources):
                    target = references_dir / f"reference_{next_index + offset:03d}{source.suffix.lower()}"
                    with source.open("rb") as input_handle, target.open("xb") as output_handle:
                        shutil.copyfileobj(input_handle, output_handle, 1024 * 1024)
                    copied.append(file_record(target, relative_to=directory))
                profile["reference_files"] = copied
            profile["updated_at"] = utc_now()
            atomic_replace_json(directory / "profile.json", profile)
            if quality is not None:
                quality_payload = read_json(directory / "quality.json")
                quality_payload.update(quality)
                quality_payload.update({"schema_version": 1, "profile_id": profile_id})
                atomic_replace_json(directory / "quality.json", quality_payload)
            atomic_replace_json(
                directory / "profile.lock",
                self._lock_payload(profile, directory, int(old_lock.get("revision", 1)) + 1),
            )
            return self.load(profile_id)

    def delete(self, profile_id: str) -> dict[str, Any]:
        directory = self._require_profile_dir(profile_id)
        trash = self.root / ".trash"
        trash.mkdir(parents=True, exist_ok=True)
        destination = trash / f"{profile_id}-{utc_now().replace(':', '').replace('-', '')}-{uuid.uuid4().hex[:8]}"
        os.rename(directory, destination)
        return {
            "schema_version": 1,
            "status": "deleted",
            "profile_id": profile_id,
            "recoverable_path": str(destination),
        }
