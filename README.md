# Voice Dubbing Runtime

```text
RESEARCH / PERSONAL POC ONLY — NO COMMERCIAL-USE CLAIM
```

This sibling project is the isolated Voice Dubbing runtime used by FrameExtract Studio. The app GUI is a thin subprocess client: neither ML packages nor model checkpoints are imported into FrameExtract Studio's `.venv`.

## Environments and engines

- Shared runtime Python: `3.11.15`, managed by project-local `uv 0.11.32`.
- `vixtts_vi` runs in `.venv-cpu` and remains pinned to `transformers==4.49.0`.
- `xtts_v2_multilingual` runs in the separate `.venv-xtts` environment.
- `.venv-xtts` is locked by `requirements-xtts.in.txt` and `requirements-xtts.lock.txt`:
  - `coqui-tts[ko,zh]==0.27.5` (maintained Windows-compatible package);
  - `torch==2.6.0+cpu`, `torchaudio==2.6.0+cpu`;
  - `transformers==4.57.6`;
  - Python `3.11.15`.
- The official multilingual model is pinned to `coqui/XTTS-v2` revision `6c2b0d75eae4b7047358e3b6bd9325f857d43f77`.
- Capability languages are read from the local model `config.json`; the engine core is not limited to only `en`, `ko`, and `zh-cn`.
- XTTS-v2 is never advertised as Available until its pinned files, model load, real `en`/`ko`/`zh-cn` synthesis, FFmpeg decode and engine health report all pass.
- Capability discovery and opening the GUI never download a model.

No Faster Whisper, Silero VAD, source separation, third TTS engine, Agent SDK, external API, Portable build, git initialization, or model bundling is part of this implementation.

## License and consent gates

Voice-rights consent is profile-specific. Consent for `lua_china_base` cannot authorize `duc_bao`; a pending profile remains visible with `CONSENT_RECONFIRM_REQUIRED` but cannot synthesize until the user confirms it explicitly in the GUI.

XTTS-v2 uses the [Coqui Public Model License 1.0.0](https://coqui.ai/cpml.txt). Model-license acceptance is separate from voice-rights consent. The provisioner refuses to download or load revision `6c2b0d75...` without a matching per-user acceptance record:

```powershell
# Run only after the user explicitly accepts the pinned CPML/revision.
& .\.venv-cpu\Scripts\python.exe .\scripts\record_xtts_license_acceptance.py --accept-cpml
& .\.venv-xtts\Scripts\python.exe .\scripts\provision_xtts_v2.py
```

The record is scoped to `research_personal_poc_noncommercial`; it does not claim commercial permission or Portable redistribution. A revision or LICENSE SHA-256 change invalidates the gate.

## CLI contract

```powershell
& .\.venv-cpu\Scripts\python.exe -u -m voice_dubbing_runtime capabilities --json
& .\.venv-cpu\Scripts\python.exe -u -m voice_dubbing_runtime profiles list --json
& .\.venv-cpu\Scripts\python.exe -u -m voice_dubbing_runtime profiles create --request <request.json> --json
& .\.venv-cpu\Scripts\python.exe -u -m voice_dubbing_runtime profiles update --request <request.json> --json
& .\.venv-cpu\Scripts\python.exe -u -m voice_dubbing_runtime profiles consent --request <request.json> --json
& .\.venv-cpu\Scripts\python.exe -u -m voice_dubbing_runtime profiles repair-known --confirm-app-closed --json
& .\.venv-cpu\Scripts\python.exe -u -m voice_dubbing_runtime worker --job <job.json>
```

The worker emits UTF-8 JSON markers prefixed with `@@VOICE_DUB|`. Profiles and job runs live under:

```text
%LOCALAPPDATA%\FrameExtractStudio\VoiceDubbing\profiles\<profile_id>
%LOCALAPPDATA%\FrameExtractStudio\VoiceDubbing\runs\<job_id>
```

Profile creation allocates a new unique ID by default (`name`, `name_2`, ...). Update requires an explicit target/action. Invalid and consent-pending profile directories remain visible in inventory results.

## Verification

```powershell
$env:PYTHONDONTWRITEBYTECODE = "1"
$env:PYTHONUTF8 = "1"
& .\.venv-cpu\Scripts\python.exe -m unittest discover -s tests -t . -p "test_*.py" -v
& .\.tools\uv\uv.exe pip check --python .\.venv-cpu\Scripts\python.exe
& .\.tools\uv\uv.exe pip check --python .\.venv-xtts\Scripts\python.exe

# Only after CPML acceptance and model provisioning; exactly one call/language.
& .\.venv-cpu\Scripts\python.exe .\scripts\run_xtts_multilingual_smoke.py
```

Repair evidence is written to `runs/profile_repair_*`; XTTS smoke/health evidence is written to `runs/xtts_v2_multilingual_smoke` and `models/xtts_v2/engine_health.json`.

## Legacy Phase 1 evidence

Historical Phase 0/1 outputs and tuning reports remain untouched. The repair keeps `profile.phase1.json`, `consent.phase1.json`, duplicate source references, a hash-verified backup, and the pre-repair directory. `probe_vixtts_cpu.py` remains historical and is not the reusable worker path.
