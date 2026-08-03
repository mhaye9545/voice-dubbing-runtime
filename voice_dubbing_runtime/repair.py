"""Idempotent repair for the confirmed Lụa/Đức Bảo profile split."""

from __future__ import annotations

import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .errors import (
    ALREADY_EXISTS,
    CONSENT_REQUIRED,
    INVALID_REFERENCE,
    INVALID_REQUEST,
    PROFILE_NOT_FOUND,
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
from .profiles import VoiceProfileManager, build_consent_record, consent_is_confirmed


LUA_REFERENCE_SHA256 = "46CA9FD06C759ABCB3751D809B21F030EF1AE9682C7DB922A51431977859AE6C"
LUA_REFERENCE = "references/ref_primary.wav"
DUC_SOURCE_REFERENCE = "references/reference_001.wav"
DUC_REFERENCE = "references/ref_primary.wav"


def _safe_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _tree_manifest(root: Path) -> list[dict[str, Any]]:
    if not root.is_dir():
        return []
    records: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix().casefold()):
        if not path.is_file():
            continue
        stat = path.stat()
        records.append(
            {
                "path": path.relative_to(root).as_posix(),
                "size_bytes": stat.st_size,
                "sha256": sha256_file(path),
                "last_modified_utc": datetime.fromtimestamp(
                    stat.st_mtime, timezone.utc
                ).isoformat().replace("+00:00", "Z"),
            }
        )
    return records


def _assert_same_manifest(source: list[dict[str, Any]], backup: list[dict[str, Any]]) -> None:
    left = {(item["path"], item["size_bytes"], item["sha256"]) for item in source}
    right = {(item["path"], item["size_bytes"], item["sha256"]) for item in backup}
    if left != right:
        raise VoiceRuntimeError(INVALID_REQUEST, "Backup manifest does not match source profile.")


def _strict_consent_for_duc(consent: dict[str, Any] | None) -> dict[str, Any] | None:
    if consent is None:
        return None
    if not consent_is_confirmed(consent):
        raise VoiceRuntimeError(CONSENT_REQUIRED, "Đức Bảo consent is not confirmed.")
    identity = consent.get("profile_id") or consent.get("voice_profile_id")
    if identity not in (None, "", "duc_bao"):
        raise VoiceRuntimeError(
            CONSENT_REQUIRED,
            "Consent belonging to another profile cannot authorize Đức Bảo.",
            {"consent_profile_id": identity},
        )
    legacy = str(consent.get("legacy_phase1_consent", ""))
    if legacy == "consent.phase1.json":
        raise VoiceRuntimeError(
            CONSENT_REQUIRED, "Lụa's Phase 1 consent cannot be reused for Đức Bảo."
        )
    return build_consent_record(consent, "duc_bao")


class KnownProfileRepair:
    """Builds and validates both repaired profiles before committing any split."""

    def __init__(
        self,
        manager: VoiceProfileManager,
        *,
        backup_root: Path,
        report_root: Path,
        audio_validator: Callable[[Path], dict[str, Any]],
        expected_lua_reference_sha256: str = LUA_REFERENCE_SHA256,
    ) -> None:
        self.manager = manager
        self.backup_root = backup_root.resolve()
        self.report_root = report_root.resolve()
        self.audio_validator = audio_validator
        self.expected_lua_reference_sha256 = expected_lua_reference_sha256.upper()

    def _state(self) -> tuple[dict[str, Any], dict[str, Any]] | None:
        try:
            lua = self.manager.load("lua_china_base")
            duc = self.manager.load("duc_bao")
        except VoiceRuntimeError:
            return None
        lua_reference = self.manager.resolve_references("lua_china_base")
        duc_reference = self.manager.resolve_references("duc_bao")
        if (
            lua.get("display_name") == "Lụa ở China"
            and lua.get("source_language") == "vi"
            and [item["path"] for item in lua["reference_files"]] == [LUA_REFERENCE]
            and len(lua_reference) == 1
            and sha256_file(lua_reference[0]) == self.expected_lua_reference_sha256
            and duc.get("display_name") == "Đức Bảo"
            and duc.get("source_language") == "vi"
            and [item["path"] for item in duc["reference_files"]] == [DUC_REFERENCE]
            and len(duc_reference) == 1
        ):
            return lua, duc
        return None

    def _write_report(self, payload: dict[str, Any]) -> dict[str, Any]:
        run_id = payload["run_id"]
        run_dir = self.report_root / f"profile_repair_{run_id}"
        run_dir.mkdir(parents=True, exist_ok=False)
        json_path = run_dir / "profile_repair_report.json"
        markdown_path = run_dir / "profile_repair_report.md"
        payload["report_json"] = str(json_path)
        payload["report_markdown"] = str(markdown_path)
        write_json_exclusive(json_path, payload)
        lines = [
            "# Voice profile repair report",
            "",
            f"- Status: `{payload['status']}`",
            f"- Run ID: `{run_id}`",
            f"- Profile root: `{payload['profile_root']}`",
            f"- Backup: `{payload.get('backup_path') or 'Not created (idempotent run)'}`",
            "",
            "## Profiles",
            "",
        ]
        for profile_id, item in payload.get("profiles", {}).items():
            lines.extend(
                [
                    f"### {profile_id}",
                    "",
                    f"- Display name: {item.get('display_name')}",
                    f"- Reference: `{item.get('reference')}`",
                    f"- SHA-256: `{item.get('reference_sha256')}`",
                    f"- Load: `{item.get('load')}`",
                    f"- Consent: `{item.get('consent')}`",
                    f"- Audio validation: `{item.get('audio_validation', {}).get('status', 'Pass')}`",
                    "",
                ]
            )
        markdown_path.write_text("\n".join(lines), encoding="utf-8", newline="\n")
        return payload

    def _backup(self, source: Path, run_id: str) -> tuple[Path, dict[str, Any]]:
        self.backup_root.mkdir(parents=True, exist_ok=True)
        destination = self.backup_root / f"voice_profile_repair_backup_{_safe_timestamp()}_{run_id[:8]}"
        staging = self.backup_root / f".{destination.name}.creating-{uuid.uuid4().hex}"
        if destination.exists() or staging.exists():
            raise VoiceRuntimeError(ALREADY_EXISTS, f"Backup destination exists: {destination}")
        source_manifest = _tree_manifest(source)
        staging.mkdir()
        try:
            backup_profile = staging / "lua_china_base"
            shutil.copytree(source, backup_profile, copy_function=shutil.copy2)
            backup_manifest = _tree_manifest(backup_profile)
            _assert_same_manifest(source_manifest, backup_manifest)
            manifest = {
                "schema_version": 1,
                "source": str(source),
                "backup": str(destination / "lua_china_base"),
                "created_at": utc_now(),
                "source_files": source_manifest,
                "backup_files": backup_manifest,
                "hash_validation": "Pass",
            }
            write_json_exclusive(staging / "backup_manifest.json", manifest)
            os.rename(staging, destination)
            return destination, manifest
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    def _build_duc(
        self,
        transaction_manager: VoiceProfileManager,
        source_reference: Path,
        consent_record: dict[str, Any] | None,
        validation: dict[str, Any],
    ) -> dict[str, Any]:
        directory = transaction_manager.root / "duc_bao"
        references = directory / "references"
        references.mkdir(parents=True)
        destination = references / "ref_primary.wav"
        with source_reference.open("rb") as source, destination.open("xb") as target:
            shutil.copyfileobj(source, target, 1024 * 1024)
            target.flush()
            os.fsync(target.fileno())
        if sha256_file(destination) != sha256_file(source_reference):
            raise VoiceRuntimeError(INVALID_REFERENCE, "Đức Bảo reference copy hash mismatch.")
        timestamp = utc_now()
        profile = {
            "schema_version": 1,
            "profile_id": "duc_bao",
            "display_name": "Đức Bảo",
            "profile_type": "cloned",
            "source_type": "video",
            "source_language": "vi",
            "default_language": "vi",
            "engine_preference": "vixtts_vi",
            "is_base_voice_preset": False,
            "reference_files": [file_record(destination, relative_to=directory)],
            "created_at": timestamp,
            "updated_at": timestamp,
            "enabled": True,
            "status": (
                "READY" if consent_record is not None else "CONSENT_RECONFIRM_REQUIRED"
            ),
            "repair_provenance": {
                "source_profile_id": "lua_china_base",
                "source_reference": DUC_SOURCE_REFERENCE,
                "operation": "copy",
            },
        }
        quality = {
            "schema_version": 1,
            "profile_id": "duc_bao",
            "status": "technical_pass_pending_listening",
            "repair_audio_validation": validation,
        }
        pending_consent = {
            "schema_version": 1,
            "profile_id": "duc_bao",
            "authorized": False,
            "status": "CONSENT_RECONFIRM_REQUIRED",
            "reason": "No Đức Bảo-specific consent record was supplied to the repair API.",
            "created_at": timestamp,
        }
        write_json_exclusive(directory / "profile.json", profile)
        write_json_exclusive(directory / "quality.json", quality)
        write_json_exclusive(directory / "consent.json", consent_record or pending_consent)
        write_json_exclusive(
            directory / "profile.lock",
            transaction_manager._lock_payload(profile, directory, 1),
        )
        return transaction_manager.load("duc_bao")

    def _confirm_existing_duc(
        self, consent_record: dict[str, Any], run_id: str
    ) -> dict[str, Any]:
        self.manager.confirm_consent("duc_bao", consent_record)
        state = self._state()
        assert state is not None
        payload = self._base_payload(run_id)
        payload.update(
            {
                "status": "CONSENT_CONFIRMED",
                "actions": ["Updated Đức Bảo consent from an explicit profile-specific record."],
                "backup_path": None,
                "profiles": self._profile_evidence(*state),
                "post_repair_tree": _tree_manifest(self.manager.root),
            }
        )
        return self._write_report(payload)

    def _base_payload(self, run_id: str) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "run_id": run_id,
            "created_at": utc_now(),
            "profile_root": str(self.manager.root),
            "files_deleted": [],
            "api_calls": 0,
            "synthesis_calls": 0,
        }

    def _profile_evidence(
        self, lua: dict[str, Any], duc: dict[str, Any]
    ) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for profile in (lua, duc):
            identifier = profile["profile_id"]
            reference = self.manager.resolve_references(identifier)[0]
            try:
                self.manager.consent(identifier)
                consent_status = "Pass"
            except VoiceRuntimeError as exc:
                consent_status = exc.details.get("status", exc.code)
            result[identifier] = {
                "display_name": profile["display_name"],
                "folder": str(self.manager.root / identifier),
                "reference": profile["reference_files"][0]["path"],
                "reference_sha256": sha256_file(reference),
                "load": "Pass",
                "consent": consent_status,
                "source_language": profile["source_language"],
                "audio_validation": {"status": "Pass", **self.audio_validator(reference)},
            }
        return result

    def execute(
        self,
        *,
        duc_bao_consent: dict[str, Any] | None = None,
        application_closed_confirmed: bool = False,
    ) -> dict[str, Any]:
        if not application_closed_confirmed:
            raise VoiceRuntimeError(
                INVALID_REQUEST,
                "Repair requires explicit confirmation that FrameExtract Studio is closed.",
            )
        run_id = str(uuid.uuid4())
        strict_duc_consent = _strict_consent_for_duc(duc_bao_consent)
        existing = self._state()
        if existing is not None:
            try:
                self.manager.consent("duc_bao")
                duc_authorized = True
            except VoiceRuntimeError:
                duc_authorized = False
            if strict_duc_consent is not None and not duc_authorized:
                return self._confirm_existing_duc(strict_duc_consent, run_id)
            payload = self._base_payload(run_id)
            payload.update(
                {
                    "status": "ALREADY_REPAIRED",
                    "actions": [],
                    "backup_path": None,
                    "profiles": self._profile_evidence(*existing),
                    "post_repair_tree": _tree_manifest(self.manager.root),
                }
            )
            return self._write_report(payload)

        lua_directory = self.manager.root / "lua_china_base"
        if not lua_directory.is_dir():
            raise VoiceRuntimeError(PROFILE_NOT_FOUND, "lua_china_base directory is missing.")
        if (self.manager.root / "duc_bao").exists():
            raise VoiceRuntimeError(
                ALREADY_EXISTS,
                "duc_bao already exists but does not match the canonical repaired profile.",
            )
        lua_reference = lua_directory / LUA_REFERENCE
        duc_source = lua_directory / DUC_SOURCE_REFERENCE
        if sha256_file(lua_reference) != self.expected_lua_reference_sha256:
            raise VoiceRuntimeError(
                INVALID_REFERENCE,
                "Lụa canonical reference hash does not match the Phase 1 reference.",
            )
        if not duc_source.is_file() or duc_source.stat().st_size == 0:
            raise VoiceRuntimeError(INVALID_REFERENCE, "Đức Bảo source reference is missing.")

        pre_tree = _tree_manifest(self.manager.root)
        backup_path, backup_manifest = self._backup(lua_directory, run_id)
        transaction_root = self.manager.root / f".profile-repair-txn-{uuid.uuid4().hex}"
        transaction_profiles = transaction_root / "profiles"
        transaction_profiles.mkdir(parents=True)
        transaction_manager = VoiceProfileManager(transaction_profiles)
        preserved_original: Path | None = None
        committed_lua = False
        committed_duc = False
        try:
            shutil.copytree(lua_directory, transaction_profiles / "lua_china_base")
            migrated_lua = transaction_manager.migrate_legacy(
                "lua_china_base",
                display_name="Lụa ở China",
                profile_type="cloned",
                source_type="video",
                source_language="vi",
                default_language="vi",
                engine_preference="vixtts_vi",
                canonical_reference=LUA_REFERENCE,
            )
            lua_validation = self.audio_validator(
                transaction_manager.resolve_references("lua_china_base")[0]
            )
            duc_validation = self.audio_validator(duc_source)
            migrated_duc = self._build_duc(
                transaction_manager, duc_source, strict_duc_consent, duc_validation
            )
            transaction_manager.load("lua_china_base")
            transaction_manager.load("duc_bao")

            preserved_original = self.manager.root / (
                f".lua_china_base.pre-repair-{run_id[:8]}"
            )
            os.rename(lua_directory, preserved_original)
            os.rename(transaction_profiles / "lua_china_base", lua_directory)
            committed_lua = True
            os.rename(transaction_profiles / "duc_bao", self.manager.root / "duc_bao")
            committed_duc = True
            final_lua = self.manager.load("lua_china_base")
            final_duc = self.manager.load("duc_bao")
            if final_lua["reference_files"][0]["sha256"] != self.expected_lua_reference_sha256:
                raise VoiceRuntimeError(INVALID_REFERENCE, "Committed Lụa reference hash changed.")
        except Exception:
            if committed_duc and (self.manager.root / "duc_bao").exists():
                os.rename(
                    self.manager.root / "duc_bao", transaction_profiles / "duc_bao.failed"
                )
            if committed_lua and lua_directory.exists():
                os.rename(lua_directory, transaction_profiles / "lua_china_base.failed")
            if preserved_original is not None and preserved_original.exists():
                os.rename(preserved_original, lua_directory)
            shutil.rmtree(transaction_root, ignore_errors=True)
            raise
        shutil.rmtree(transaction_root, ignore_errors=True)
        profiles = self._profile_evidence(final_lua, final_duc)
        profiles["lua_china_base"]["audio_validation"] = {
            "status": "Pass", **lua_validation
        }
        profiles["duc_bao"]["audio_validation"] = {
            "status": "Pass", **duc_validation
        }
        payload = self._base_payload(run_id)
        payload.update(
            {
                "status": (
                    "PASS" if strict_duc_consent is not None else "PASS_CONSENT_RECONFIRM_REQUIRED"
                ),
                "backup_path": str(backup_path),
                "backup_manifest": backup_manifest,
                "pre_repair_tree": pre_tree,
                "post_repair_tree": _tree_manifest(self.manager.root),
                "profiles": profiles,
                "actions": [
                    "Migrated profile.phase1.json into canonical lua_china_base metadata.",
                    "Listed only references/ref_primary.wav for Lụa ở China.",
                    "Copied references/reference_001.wav to duc_bao/references/ref_primary.wav.",
                    "Preserved historical and duplicate source files; deleted nothing.",
                ],
                "preserved_original_path": str(preserved_original),
                "files_kept": [
                    str(lua_directory / DUC_SOURCE_REFERENCE),
                    str(preserved_original),
                ],
                "files_copied": [str(self.manager.root / "duc_bao" / DUC_REFERENCE)],
                "metadata_mapping": {
                    "lua_china_base": migrated_lua,
                    "duc_bao": migrated_duc,
                },
            }
        )
        return self._write_report(payload)


def ffmpeg_audio_validator(ffmpeg: Path) -> Callable[[Path], dict[str, Any]]:
    """Create the production strict-decode validator without importing ML code."""

    from .worker import validate_generated_wav

    ffmpeg = ffmpeg.resolve()
    if not ffmpeg.is_file():
        raise VoiceRuntimeError(INVALID_REQUEST, f"FFmpeg was not found: {ffmpeg}")

    def validate(path: Path) -> dict[str, Any]:
        result = validate_generated_wav(path, ffmpeg)
        return {
            "duration_seconds": result["duration_seconds"],
            "sample_rate": result["sample_rate"],
            "channels": result["channels"],
            "peak": result["peak"],
            "rms": result["rms"],
            "clipping_ratio": result["clipping_ratio"],
            "ffmpeg_decode": result["ffmpeg_decode"],
        }

    return validate
