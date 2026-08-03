"""Pinned, local-only viXTTS CPU backend.

Imports model libraries only inside a synthesis job.  It never downloads a
model and never changes the runtime environment.
"""

from __future__ import annotations

import gc
import hashlib
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Sequence

from .errors import (
    CANCELLED,
    INVALID_REFERENCE,
    MODEL_LOAD_FAILED,
    SYNTHESIS_FAILED,
    VoiceRuntimeError,
)


TTS_REVISION = "ff217b3f27b294de194cc59c5119d1e08b06413c"
MODEL_REVISION = "c06f4378883110615941aab481532a9802440b05"
MODEL_SHA256 = "534670e4b752002b7d7224e6ea1f467bd608c8dd3c36efaa45e1f4696e8bd1d2"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class VixttsBackend:
    engine_id = "vixtts_vi"

    def __init__(self, runtime_root: Path) -> None:
        self.runtime_root = runtime_root.resolve()
        self.vendor_root = self.runtime_root / "vendor" / f"TTS-{TTS_REVISION}"
        self.model_dir = self.runtime_root / "models" / f"capleaf_viXTTS_{MODEL_REVISION[:12]}"
        self._model: Any = None
        self._config: Any = None

    @staticmethod
    def _cancelled(cancel_token: Any) -> bool:
        checker = getattr(cancel_token, "is_cancelled", None)
        return bool(checker()) if checker else False

    def _check_cancelled(self, cancel_token: Any) -> None:
        if self._cancelled(cancel_token):
            raise VoiceRuntimeError(CANCELLED, "Voice synthesis was cancelled.")

    def _verify_local_install(self) -> None:
        required = [
            self.vendor_root / "TTS" / "tts" / "models" / "xtts.py",
            self.model_dir / "model.pth",
            self.model_dir / "config.json",
            self.model_dir / "vocab.json",
        ]
        missing = [str(path) for path in required if not path.is_file() or path.stat().st_size == 0]
        if missing:
            raise VoiceRuntimeError(
                MODEL_LOAD_FAILED,
                "Pinned viXTTS runtime files are missing.",
                {"missing": missing},
            )
        actual_hash = _sha256(self.model_dir / "model.pth")
        if actual_hash.lower() != MODEL_SHA256:
            raise VoiceRuntimeError(
                MODEL_LOAD_FAILED,
                "Pinned viXTTS model hash does not match the validated Phase 1 model.",
                {"expected_sha256": MODEL_SHA256.upper(), "actual_sha256": actual_hash.upper()},
            )

    def _load(self, cancel_token: Any) -> float:
        if self._model is not None:
            return 0.0
        self._check_cancelled(cancel_token)
        self._verify_local_install()
        started = time.perf_counter()
        try:
            vendor_text = str(self.vendor_root)
            if vendor_text not in sys.path:
                sys.path.insert(0, vendor_text)
            from TTS.tts.configs.xtts_config import XttsConfig
            from TTS.tts.models.xtts import Xtts

            config = XttsConfig()
            config.load_json(str(self.model_dir / "config.json"))
            model = Xtts.init_from_config(config)
            model.load_checkpoint(
                config,
                checkpoint_dir=str(self.model_dir),
                eval=True,
                use_deepspeed=False,
            )
            model.to("cpu")
            self._config = config
            self._model = model
        except VoiceRuntimeError:
            raise
        except Exception as exc:
            self.close()
            raise VoiceRuntimeError(
                MODEL_LOAD_FAILED,
                f"viXTTS model load failed: {exc}",
                {"engine": self.engine_id, "device": "cpu"},
            ) from exc
        self._check_cancelled(cancel_token)
        return time.perf_counter() - started

    def synthesize(
        self,
        *,
        job: dict[str, Any],
        profile: dict[str, Any],
        references: Sequence[Path],
        output_path: Path,
        progress: Callable[[str, float], None],
        cancel_token: Any,
    ) -> dict[str, Any]:
        if job.get("language") != "vi":
            raise VoiceRuntimeError(SYNTHESIS_FAILED, "viXTTS accepts only language='vi'.")
        if not references or any(not path.is_file() for path in references):
            raise VoiceRuntimeError(INVALID_REFERENCE, "viXTTS requires at least one existing reference.")
        speed = float(job.get("speed", 1.0))
        if not 0.5 <= speed <= 2.0:
            raise VoiceRuntimeError(SYNTHESIS_FAILED, "speed must be between 0.5 and 2.0.")
        seed = int(job.get("seed", 42))
        keep_warm = bool(job.get("keep_model_warm", False))
        text = str(job.get("text", "")).strip()
        if not text:
            raise VoiceRuntimeError(SYNTHESIS_FAILED, "Synthesis text must not be empty.")

        load_elapsed = 0.0
        synthesis_elapsed = 0.0
        try:
            progress("load_model", 0.30)
            load_elapsed = self._load(cancel_token)
            self._check_cancelled(cancel_token)
            progress("synthesize", 0.70)
            started = time.perf_counter()
            try:
                import torch
                import torchaudio

                torch.manual_seed(seed)
                with torch.inference_mode():
                    result = self._model.synthesize(
                        text,
                        self._config,
                        speaker_wav=[str(path) for path in references],
                        language="vi",
                        temperature=0.30,
                        length_penalty=1.0,
                        repetition_penalty=10.0,
                        top_k=30,
                        top_p=0.85,
                        do_sample=True,
                        speed=speed,
                        enable_text_splitting=True,
                    )
                waveform = torch.as_tensor(result["wav"], dtype=torch.float32).unsqueeze(0)
                if waveform.numel() == 0 or not torch.isfinite(waveform).all():
                    raise ValueError("viXTTS returned an empty or non-finite waveform")
                output_path.parent.mkdir(parents=True, exist_ok=True)
                if output_path.exists():
                    raise FileExistsError(f"Refusing to overwrite output: {output_path}")
                temporary = output_path.with_name(f".{output_path.stem}.{uuid.uuid4().hex}.wav")
                try:
                    torchaudio.save(
                        str(temporary), waveform.cpu(), 24000, encoding="PCM_S", bits_per_sample=16
                    )
                    os.link(temporary, output_path)
                finally:
                    if temporary.exists():
                        temporary.unlink()
                del waveform, result
            except VoiceRuntimeError:
                raise
            except Exception as exc:
                raise VoiceRuntimeError(
                    SYNTHESIS_FAILED,
                    f"viXTTS synthesis failed: {exc}",
                    {"engine": self.engine_id},
                ) from exc
            synthesis_elapsed = time.perf_counter() - started
            self._check_cancelled(cancel_token)
            return {
                "model_load_elapsed_seconds": load_elapsed,
                "synthesis_elapsed_seconds": synthesis_elapsed,
                "seed": seed,
                "speed": speed,
                "inference_parameters": {
                    "temperature": 0.30,
                    "length_penalty": 1.0,
                    "repetition_penalty": 10.0,
                    "top_k": 30,
                    "top_p": 0.85,
                    "do_sample": True,
                    "enable_text_splitting": True,
                },
                "model_revision": MODEL_REVISION,
                "tts_revision": TTS_REVISION,
            }
        finally:
            if not keep_warm:
                self.close()

    def close(self) -> None:
        self._model = None
        self._config = None
        gc.collect()
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass
