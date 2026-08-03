"""Capability discovery and deterministic engine selection.

Languages come from engine/model configuration, and a provisioned engine may
also require a real health report before it is advertised as Available.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ENGINE_UNAVAILABLE, UNSUPPORTED_LANGUAGE, VoiceRuntimeError
from .io_utils import read_json
from .paths import runtime_root


@dataclass(frozen=True, slots=True)
class EngineCapability:
    id: str
    display_name: str
    available: bool
    languages: tuple[str, ...]
    devices: tuple[str, ...]
    profile_types: tuple[str, ...]
    unavailable_reason: str | None
    supports_keep_model_loaded: bool
    priority: int
    backend: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "available": self.available,
            "languages": list(self.languages),
            "devices": list(self.devices),
            "profile_types": list(self.profile_types),
            "unavailable_reason": self.unavailable_reason,
            "supports_keep_model_loaded": self.supports_keep_model_loaded,
            # Kept for older thin clients during the protocol migration.
            "supports_keep_model_warm": self.supports_keep_model_loaded,
        }

    @property
    def supports_keep_model_warm(self) -> bool:
        return self.supports_keep_model_loaded


def _normalise_codes(values: Any) -> tuple[str, ...]:
    if not isinstance(values, list):
        return ()
    result: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            continue
        code = value.strip().lower().replace("_", "-")
        if code not in result:
            result.append(code)
    return tuple(result)


def _read_nested(payload: Any, dotted_key: str) -> Any:
    current = payload
    for part in dotted_key.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


class EngineRegistry:
    def __init__(self, root: Path | None = None, config_path: Path | None = None) -> None:
        self.root = (root or runtime_root()).resolve()
        self.config_path = (
            config_path or Path(__file__).with_name("config") / "engines.json"
        ).resolve()
        self._raw = read_json(self.config_path)
        if self._raw.get("schema_version") != 1 or not isinstance(self._raw.get("engines"), list):
            raise ValueError(f"Unsupported engine registry schema: {self.config_path}")

    def _resolve_relative(self, value: str) -> Path | None:
        candidate = (self.root / value).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError:
            return None
        return candidate

    def _capability(self, item: dict[str, Any]) -> EngineCapability:
        reasons: list[str] = []
        missing: list[str] = []
        for relative in item.get("required_paths", []):
            if not isinstance(relative, str):
                reasons.append("INVALID_REQUIRED_PATH")
                continue
            candidate = self._resolve_relative(relative)
            if candidate is None:
                reasons.append("INVALID_REQUIRED_PATH")
                continue
            if not candidate.is_file() or candidate.stat().st_size == 0:
                missing.append(relative)
        if missing:
            reasons.append("MODEL_OR_RUNTIME_FILES_MISSING: " + ", ".join(missing))

        language_source = item.get("language_source", "engine_contract")
        languages = _normalise_codes(item.get("languages", []))
        if language_source == "model_config":
            relative_config = item.get("model_config_path")
            if not isinstance(relative_config, str):
                reasons.append("MODEL_CONFIG_NOT_DECLARED")
                languages = ()
            else:
                config_path = self._resolve_relative(relative_config)
                if config_path is not None and config_path.is_file():
                    try:
                        model_config = read_json(config_path)
                        languages = _normalise_codes(
                            _read_nested(model_config, item.get("model_languages_key", "languages"))
                        )
                        if not languages:
                            reasons.append("MODEL_LANGUAGES_EMPTY")
                    except (OSError, ValueError, TypeError):
                        reasons.append("MODEL_CONFIG_INVALID")
                        languages = ()
                else:
                    languages = ()
        elif language_source != "engine_contract":
            reasons.append("INVALID_LANGUAGE_SOURCE")

        health_relative = item.get("health_report_path")
        if health_relative is not None:
            if not isinstance(health_relative, str):
                reasons.append("INVALID_HEALTH_REPORT_PATH")
            else:
                health_path = self._resolve_relative(health_relative)
                if health_path is None or not health_path.is_file():
                    reasons.append("ENGINE_HEALTH_NOT_PASSED")
                else:
                    try:
                        health = read_json(health_path)
                        if health.get("status") != "PASS":
                            reasons.append("ENGINE_HEALTH_NOT_PASSED")
                        for key, expected in item.get("health_required_values", {}).items():
                            if _read_nested(health, key) != expected:
                                reasons.append(f"ENGINE_HEALTH_MISMATCH:{key}")
                        validated = set(_normalise_codes(health.get("languages_validated", [])))
                        required = set(_normalise_codes(item.get("health_required_languages", [])))
                        if not required.issubset(validated):
                            reasons.append("ENGINE_HEALTH_LANGUAGES_INCOMPLETE")
                    except (OSError, ValueError, TypeError):
                        reasons.append("ENGINE_HEALTH_REPORT_INVALID")

        backend = item.get("backend")
        if backend is not None and not isinstance(backend, str):
            reasons.append("INVALID_BACKEND_DECLARATION")
            backend = None
        if not backend:
            reasons.append("BACKEND_NOT_CONFIGURED")

        available = not reasons
        return EngineCapability(
            id=str(item["id"]),
            display_name=str(item.get("display_name", item["id"])),
            available=available,
            languages=languages,
            devices=_normalise_codes(item.get("devices", [])),
            profile_types=_normalise_codes(item.get("profile_types", ["cloned", "preset"])),
            unavailable_reason="; ".join(dict.fromkeys(reasons)) if reasons else None,
            supports_keep_model_loaded=bool(
                item.get("supports_keep_model_loaded", item.get("supports_keep_model_warm", False))
            ),
            priority=int(item.get("priority", 100)),
            backend=backend,
        )

    def engines(self) -> tuple[EngineCapability, ...]:
        capabilities = [self._capability(item) for item in self._raw["engines"]]
        return tuple(sorted(capabilities, key=lambda item: (item.priority, item.id)))

    def get(self, engine_id: str) -> EngineCapability:
        for capability in self.engines():
            if capability.id == engine_id:
                return capability
        raise VoiceRuntimeError(
            ENGINE_UNAVAILABLE,
            f"Engine is not registered: {engine_id}",
            {"engine": engine_id},
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "runtime": {"available": True, "version": "0.3.0"},
            "engines": [item.as_dict() for item in self.engines()],
        }

    def select(self, requested_engine: str, language: str, device: str = "cpu") -> EngineCapability:
        normal_language = language.strip().lower().replace("_", "-")
        normal_device = device.strip().lower()
        if not normal_language or normal_language == "auto":
            raise VoiceRuntimeError(
                UNSUPPORTED_LANGUAGE,
                "A concrete output language is required after profile resolution.",
            )
        if requested_engine != "auto":
            capability = self.get(requested_engine)
            if not capability.available:
                raise VoiceRuntimeError(
                    ENGINE_UNAVAILABLE,
                    f"Engine is unavailable: {requested_engine}",
                    {"engine": requested_engine, "reason": capability.unavailable_reason},
                )
            if normal_language not in capability.languages:
                raise VoiceRuntimeError(
                    UNSUPPORTED_LANGUAGE,
                    f"{requested_engine} does not declare language '{normal_language}'.",
                    {
                        "engine": requested_engine,
                        "language": normal_language,
                        "supported_languages": list(capability.languages),
                    },
                )
            if normal_device not in capability.devices:
                raise VoiceRuntimeError(
                    ENGINE_UNAVAILABLE,
                    f"{requested_engine} does not declare device '{normal_device}'.",
                    {"engine": requested_engine, "supported_devices": list(capability.devices)},
                )
            return capability

        candidates = [
            item
            for item in self.engines()
            if item.available and normal_language in item.languages and normal_device in item.devices
        ]
        if candidates:
            return candidates[0]
        supporting = [
            item.id for item in self.engines() if normal_language in item.languages and item.available
        ]
        raise VoiceRuntimeError(
            UNSUPPORTED_LANGUAGE,
            f"No available engine declares language '{normal_language}' on {normal_device}.",
            {"language": normal_language, "device": normal_device, "supporting_engines": supporting},
        )

    def instantiate_backend(self, capability: EngineCapability) -> Any:
        if not capability.available or not capability.backend:
            raise VoiceRuntimeError(
                ENGINE_UNAVAILABLE,
                f"Engine backend is unavailable: {capability.id}",
                {"reason": capability.unavailable_reason},
            )
        module_name, separator, attribute = capability.backend.partition(":")
        if not separator:
            raise VoiceRuntimeError(ENGINE_UNAVAILABLE, f"Invalid backend path: {capability.backend}")
        try:
            backend_type = getattr(importlib.import_module(module_name), attribute)
            return backend_type(self.root)
        except VoiceRuntimeError:
            raise
        except Exception as exc:
            raise VoiceRuntimeError(
                ENGINE_UNAVAILABLE,
                f"Could not initialise backend for {capability.id}: {exc}",
                {"engine": capability.id},
            ) from exc
