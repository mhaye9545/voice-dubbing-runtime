"""Runtime CLI including official profile migration and repair entry points."""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path
from typing import Any

from .capabilities import EngineRegistry
from .errors import INVALID_REQUEST, VoiceRuntimeError
from .io_utils import canonical_json, read_json
from .media import resolve_ffmpeg
from .paths import runtime_root
from .profiles import VoiceProfileManager
from .repair import KnownProfileRepair, ffmpeg_audio_validator
from .worker import CancellationToken, MarkerEmitter, VoiceWorker, install_signal_handlers


def _print_json(payload: Any) -> None:
    rendered = canonical_json(payload, pretty=True)
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is not None:
        buffer.write(rendered.encode("utf-8"))
        buffer.flush()
    else:  # StringIO and other text streams used by tests.
        sys.stdout.write(rendered)
        sys.stdout.flush()


def _load_request(path_text: str) -> Any:
    if path_text == "-":
        return json.load(sys.stdin)
    return read_json(Path(path_text).expanduser().resolve())


def _manager(args: argparse.Namespace) -> VoiceProfileManager:
    return VoiceProfileManager(Path(args.profiles_root).resolve() if args.profiles_root else None)


def _run_repair(args: argparse.Namespace, manager: VoiceProfileManager) -> int:
    root = Path(args.runtime_root).resolve() if args.runtime_root else runtime_root()
    backup_root = (
        Path(args.backup_root).resolve() if args.backup_root else root.parent / "download"
    )
    report_root = Path(args.report_root).resolve() if args.report_root else root / "runs"
    ffmpeg = resolve_ffmpeg(root, args.ffmpeg)
    consent = _load_request(args.duc_bao_consent) if args.duc_bao_consent else None
    if consent is not None and not isinstance(consent, dict):
        raise VoiceRuntimeError(INVALID_REQUEST, "Đức Bảo consent must be a JSON object.")
    service = KnownProfileRepair(
        manager,
        backup_root=backup_root,
        report_root=report_root,
        audio_validator=ffmpeg_audio_validator(ffmpeg),
    )
    result = service.execute(
        duc_bao_consent=consent,
        application_closed_confirmed=args.confirm_app_closed,
    )
    _print_json(result)
    return 0


def _run_profiles(args: argparse.Namespace) -> int:
    manager = _manager(args)
    if args.profiles_command == "list":
        payload = {"schema_version": 1, "profiles": manager.list()}
    elif args.profiles_command == "get":
        payload = manager.load(args.profile_id)
    elif args.profiles_command == "delete":
        payload = manager.delete(args.profile_id)
    elif args.profiles_command == "repair-known":
        return _run_repair(args, manager)
    else:
        request = _load_request(args.request)
        if not isinstance(request, dict):
            raise VoiceRuntimeError(INVALID_REQUEST, "Profile request must be a JSON object.")
        if args.profiles_command == "create":
            payload = manager.create(
                profile_id=request.get("profile_id"),
                display_name=request.get("display_name", ""),
                profile_type=request.get("profile_type", ""),
                source_type=request.get("source_type", ""),
                source_language=request.get("source_language"),
                default_language=request.get("default_language", "auto"),
                engine_preference=request.get("engine_preference", "auto"),
                reference_files=request.get("reference_files", []),
                consent=request.get("consent"),
                quality=request.get("quality"),
                is_base_voice_preset=bool(request.get("is_base_voice_preset", False)),
                collision_policy=request.get("collision_policy", "suffix"),
            )
        elif args.profiles_command == "consent":
            payload = manager.confirm_consent(
                str(request.get("profile_id", "")), request.get("consent")
            )
        elif args.profiles_command == "migrate-legacy":
            payload = manager.migrate_legacy(
                str(request.get("profile_id", "")),
                display_name=request.get("display_name", ""),
                profile_type=request.get("profile_type", "cloned"),
                source_type=request.get("source_type", "audio"),
                source_language=request.get("source_language"),
                default_language=request.get("default_language", "auto"),
                engine_preference=request.get("engine_preference", "auto"),
                canonical_reference=request.get("canonical_reference"),
                is_base_voice_preset=bool(request.get("is_base_voice_preset", False)),
            )
        elif args.profiles_command == "update":
            payload = manager.update(
                str(request.get("profile_id", "")),
                display_name=request.get("display_name"),
                source_language=request.get("source_language"),
                default_language=request.get("default_language"),
                engine_preference=request.get("engine_preference"),
                enabled=request.get("enabled"),
                reference_files=request.get("reference_files"),
                quality=request.get("quality"),
                is_base_voice_preset=request.get("is_base_voice_preset"),
            )
        else:
            raise VoiceRuntimeError(
                INVALID_REQUEST, f"Unknown profiles command: {args.profiles_command}"
            )
    _print_json(payload)
    return 0


def _new_worker(args: argparse.Namespace, token: CancellationToken) -> VoiceWorker:
    profiles = _manager(args)
    root = Path(args.runtime_root).resolve() if args.runtime_root else runtime_root()
    return VoiceWorker(
        profile_manager=profiles,
        registry=EngineRegistry(root),
        emitter=MarkerEmitter(),
        cancel_token=token,
        root=root,
    )


def _run_worker(args: argparse.Namespace) -> int:
    token = CancellationToken()
    install_signal_handlers(token)
    worker = _new_worker(args, token)
    if args.job:
        try:
            code, _result = worker.execute(_load_request(args.job))
            return code
        finally:
            worker.shutdown()
    source = (
        sys.stdin
        if args.jobs_jsonl == "-"
        else Path(args.jobs_jsonl).open("r", encoding="utf-8-sig")
    )
    final_code = 0
    try:
        for line_number, line in enumerate(source, 1):
            if not line.strip():
                continue
            try:
                job = json.loads(line)
            except json.JSONDecodeError as exc:
                worker.emitter.emit(
                    VoiceRuntimeError(
                        INVALID_REQUEST, f"Invalid JSONL job on line {line_number}: {exc}"
                    ).as_dict()
                )
                final_code = 2
                continue
            code, _result = worker.execute(job)
            final_code = max(final_code, code)
    finally:
        if source is not sys.stdin:
            source.close()
        worker.shutdown()
    return final_code


def _run_create_from_source(args: argparse.Namespace) -> int:
    request = _load_request(args.request)
    if not isinstance(request, dict):
        raise VoiceRuntimeError(INVALID_REQUEST, "Source profile request must be a JSON object.")
    request = {
        **request,
        "schema_version": 1,
        "job_id": str(request.get("job_id") or uuid.uuid4()),
        "action": "create_profile",
    }
    token = CancellationToken()
    install_signal_handlers(token)
    code, _result = _new_worker(args, token).execute(request)
    return code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="voice-dubbing-runtime")
    parser.add_argument("--profiles-root", help=argparse.SUPPRESS)
    parser.add_argument("--runtime-root", help=argparse.SUPPRESS)
    commands = parser.add_subparsers(dest="command", required=True)
    capability_parser = commands.add_parser("capabilities")
    capability_parser.add_argument("--json", action="store_true", required=True)

    profiles_parser = commands.add_parser("profiles")
    profile_commands = profiles_parser.add_subparsers(dest="profiles_command", required=True)
    list_parser = profile_commands.add_parser("list")
    list_parser.add_argument("--json", action="store_true", required=True)
    get_parser = profile_commands.add_parser("get")
    get_parser.add_argument("--profile-id", required=True)
    get_parser.add_argument("--json", action="store_true", required=True)
    delete_parser = profile_commands.add_parser("delete")
    delete_parser.add_argument("--profile-id", required=True)
    delete_parser.add_argument("--json", action="store_true", required=True)
    for name in ("create", "update", "migrate-legacy", "consent"):
        sub = profile_commands.add_parser(name)
        sub.add_argument("--request", required=True)
        sub.add_argument("--json", action="store_true", required=True)
    source_parser = profile_commands.add_parser("create-from-source")
    source_parser.add_argument("--request", required=True)
    repair_parser = profile_commands.add_parser("repair-known")
    repair_parser.add_argument("--backup-root")
    repair_parser.add_argument("--report-root")
    repair_parser.add_argument("--ffmpeg")
    repair_parser.add_argument("--duc-bao-consent")
    repair_parser.add_argument("--confirm-app-closed", action="store_true", required=True)
    repair_parser.add_argument("--json", action="store_true", required=True)

    worker_parser = commands.add_parser("worker")
    inputs = worker_parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--job")
    inputs.add_argument("--jobs-jsonl")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "capabilities":
            root = Path(args.runtime_root).resolve() if args.runtime_root else runtime_root()
            _print_json(EngineRegistry(root).as_dict())
            return 0
        if args.command == "profiles":
            if args.profiles_command == "create-from-source":
                return _run_create_from_source(args)
            return _run_profiles(args)
        if args.command == "worker":
            return _run_worker(args)
        return 2
    except VoiceRuntimeError as exc:
        _print_json({"schema_version": 1, "status": "failed", **exc.as_dict()})
        return 2
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        _print_json(
            {
                "schema_version": 1,
                "status": "failed",
                "type": "error",
                "code": INVALID_REQUEST,
                "message": str(exc),
                "details": {},
            }
        )
        return 2
