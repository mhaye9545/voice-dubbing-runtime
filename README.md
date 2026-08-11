# Voice Dubbing Runtime

> Local, CPU-first voice cloning and speech synthesis runtime designed to become a **standalone application first** and an embeddable runtime later.

**Status:** active development / research preview.  
**Currently targeted:** Windows x64, Python 3.11.x, CPU.  
**No commercial-use claim is made for the ML models used or provisioned by this project.**

**English** | [Tiếng Việt](README.vi.md)

---

## Project goal

Voice Dubbing Runtime provides an isolated runtime for:

- creating and managing voice profiles from audio/video;
- selecting short 8–15 second reference segments;
- optionally separating vocals from background audio;
- performing deterministic technical reference checks;
- requiring human listening and explicit approval before committing a new voice-only reference;
- cloning a voice and synthesizing speech from text;
- supporting multiple TTS engines behind one runtime contract;
- isolating incompatible ML stacks in separate Python environments/processes;
- exposing a CLI/JSON worker protocol for a desktop GUI or other applications.

The project originated as the isolated Voice Dubbing runtime used by FrameExtract Studio. The next goal is to make it a **standalone open-source desktop application**. FrameExtract Studio should later integrate only through a stable adapter/protocol and must not become a dependency of this repository.

For the full project description, current implementation details and roadmap in Vietnamese, see **[README.vi.md](README.vi.md)**.

---

## Current implementation

### Runtime and profiles

- `voice_dubbing_runtime/` core package.
- Capability registry and deterministic engine selection.
- Voice profile create/update/delete/list flows.
- Profile-specific voice-rights consent.
- Profile revisions, integrity locks and update history.
- Job artifacts: `job.json`, `run.log`, `result.json`.
- Stable machine-readable markers prefixed with `@@VOICE_DUB|`.
- Standalone PySide6 thin-client GUI in `voice_dubbing_app/` with profile,
  reference-review and synthesis workspaces.

### Reference preparation

- FFmpeg source normalization.
- Automatic or manual 8–15 second reference selection.
- Two-phase reference update:
  - `prepare_profile_reference`;
  - `commit_profile_reference` after listening approval and single-speaker confirmation.
- Voice-only technical checks for duration, clipping, silence/noise proxies, speech/noise contrast and separation effectiveness.
- Technical metrics are not presented as speaker diarization and do not replace human listening.

### Engines

`vixtts_vi`

- CPU-only.
- `.venv-cpu`.
- `torch==2.6.0`, `torchaudio==2.6.0`.
- `transformers==4.49.0`.
- Runtime contract currently advertises Vietnamese (`vi`).

`xtts_v2_multilingual`

- Isolated `.venv-xtts`.
- `coqui-tts[ko,zh]==0.27.5`.
- `torch==2.6.0+cpu`, `torchaudio==2.6.0+cpu`.
- `transformers==4.57.6`.
- Pinned model: `coqui/XTTS-v2` revision `6c2b0d75eae4b7047358e3b6bd9325f857d43f77`.
- Capability languages are read from local model configuration.
- Current development snapshot contains PASS health evidence for `en`, `ko`, and `zh-cn`.
- Supports a persistent child worker to keep the model loaded between synthesis calls.

### Source separation

- Isolated `.venv-source-separation`.
- `demucs==4.1.0`, `htdemucs`, CPU-only.
- Pinned runtime/model manifest with file-size and SHA-256 checks before model deserialization.
- Only the selected short reference candidate is sent to Demucs, not the entire long source video.

---

## Architecture

```text
Standalone Desktop GUI / CLI / external client
                |
                v
      voice_dubbing_runtime
                |
     +----------+-----------+
     |          |           |
     v          v           v
 Profiles   Reference     Engine Registry
 Manager    Pipeline           |
                |              +--------------------+
                |              |                    |
                v              v                    v
         FFmpeg + Demucs    viXTTS              XTTS-v2
                |          .venv-cpu            .venv-xtts
                |                                   |
                v                                   v
     .venv-source-separation              persistent child worker
```

The parent runtime intentionally avoids importing every ML dependency directly. Engine-specific stacks run behind subprocess adapters.

---

## CLI contract

```powershell
& .\.venv-cpu\Scripts\python.exe -u -m voice_dubbing_runtime capabilities --json
& .\.venv-cpu\Scripts\python.exe -u -m voice_dubbing_runtime profiles list --json
& .\.venv-cpu\Scripts\python.exe -u -m voice_dubbing_runtime profiles create --request <request.json> --json
& .\.venv-cpu\Scripts\python.exe -u -m voice_dubbing_runtime profiles create-from-source --request <request.json>
& .\.venv-cpu\Scripts\python.exe -u -m voice_dubbing_runtime profiles update --request <request.json> --json
& .\.venv-cpu\Scripts\python.exe -u -m voice_dubbing_runtime profiles consent --request <request.json> --json
& .\.venv-cpu\Scripts\python.exe -u -m voice_dubbing_runtime worker --job <job.json>
& .\.venv-cpu\Scripts\python.exe -u -m voice_dubbing_runtime worker --jobs-jsonl <jobs.jsonl>
```

Worker actions currently supported:

```text
create_profile
prepare_profile_reference
commit_profile_reference
synthesize
```

---

## Standalone GUI (development)

Install the optional GUI dependency in a compatible Python 3.11 environment:

```powershell
python -m pip install -e ".[gui]"
python -m voice_dubbing_app
```

The GUI is a thin client. Startup performs capability discovery and profile
inventory only; it does not load or download a model. Heavy jobs are sent to
the existing `.venv-cpu` JSONL worker, and engine-specific ML stacks remain in
their isolated runtime processes.

---

## Verification

The repository currently contains **108 unit/contract test cases**, including
28 offscreen GUI tests plus the existing capability, CLI, media, profile,
reference, source-separation, worker and XTTS contract coverage.

Run tests with the project's pinned Python 3.11 environment:

```powershell
$env:PYTHONDONTWRITEBYTECODE = "1"
$env:PYTHONUTF8 = "1"
& .\.venv-cpu\Scripts\python.exe -m unittest discover -s tests -t . -p "test_*.py" -v
```

Check environments:

```powershell
& .\.tools\uv\uv.exe pip check --python .\.venv-cpu\Scripts\python.exe
& .\.tools\uv\uv.exe pip check --python .\.venv-xtts\Scripts\python.exe
& .\.tools\uv\uv.exe pip check --python .\.venv-source-separation\Scripts\python.exe
```

---

## Model licenses and voice consent

Voice-rights consent is profile-specific and separate from model-license acceptance.

The viXTTS and XTTS-v2 model snapshots currently used by the development runtime declare the **Coqui Public Model License 1.0.0 (CPML)** with non-commercial scope. Therefore, open-sourcing this project's code does **not** grant commercial rights to those model weights or their use.

The current source-separation manifest declares Demucs/htdemucs under the MIT license. The repository also contains vendored TTS source under Mozilla Public License 2.0.

The root source-code license for this project has **not yet been finalized**. A dependency/license audit and third-party notices are required before the first public release.

---

## Public-release blockers

Before calling the project public-ready, the current repository still needs:

- version synchronization (`pyproject.toml` vs runtime `0.3.0` contract);
- a root source-code license and `THIRD_PARTY_NOTICES.md`;
- cleanup of one-off/debug scripts tied to specific test profiles;
- a generic standalone user-data namespace instead of the legacy FrameExtract Studio path;
- a reproducible bootstrap for all three Python environments;
- a `doctor`/health-check command;
- Windows CI on Python 3.11;
- a policy for vendored TTS source;
- manual desktop and real-model acceptance for the standalone GUI;
- clean-machine packaging and release tests.

---

## Roadmap

### Phase 0 — Open-source cleanup and repository hardening **(current)**

- Documentation and architecture refresh.
- Version synchronization.
- Source-code license audit and third-party notices.
- Remove model weights from source distribution.
- Clean up test-persona/evidence scripts.
- Decouple runtime storage namespace from FrameExtract Studio.
- Add Windows/Python 3.11 CI.

### Phase 1 — Reproducible runtime bootstrap

- One bootstrap command for a clean Windows machine.
- Reproducible `.venv-cpu`, `.venv-xtts`, `.venv-source-separation` setup.
- FFmpeg verification.
- Explicit model-license gates and model provisioning.
- `doctor` command for runtime/model/dependency health.

### Phase 2 — Standalone desktop GUI

The first thin-client implementation is now present in source. Offscreen GUI
tests cover runtime marker parsing, profile create/update state isolation,
manual-review commit gates and synthesis capability filtering. Normal desktop
UX and real-model acceptance are still required before a release claim.

First GUI scope:

- choose audio/video source;
- select target-speaker window;
- auto/manual 8–15 second reference;
- optional source separation;
- A/B listen to source mix vs voice-only candidate;
- explicit consent and single-speaker confirmation;
- create/update/delete voice profiles;
- enter text/language/engine/speed;
- synthesize, preview and save WAV;
- progress, cancellation and diagnostic logs.

The GUI must remain a thin client. ML and business logic stay in the runtime.

### Phase 3 — Runtime/API stabilization

- Version job/result schemas.
- Separate public runtime API from debug/migration tools.
- Stabilize progress events and error codes.
- Define profile-schema migration policy.
- Add a small public Python API alongside the CLI/JSON protocol.

### Phase 4 — Voice/reference quality

- Better candidate ranking for long media.
- GUI A/B candidate comparison.
- Optional separation/denoise adapters.
- CPU time/RAM benchmarks.
- Licensed quality-regression fixtures.

Human listening remains a required authority for audible background/speaker suitability.

### Phase 5 — Dubbing workflow

After single-utterance cloning is stable:

- segment/subtitle input;
- batch synthesis;
- timing/duration adaptation;
- dubbing-track assembly;
- video preview;
- audio export or video muxing;
- optional ASR/translation modules without forcing them into the core runtime.

### Phase 6 — Packaging and public release

- Windows desktop build.
- Separate model provisioner/downloader.
- No restricted model bundling without permission.
- Checksums/signing policy.
- Clean-machine smoke test.
- Contributor documentation.

### Phase 7 — FrameExtract Studio integration

Only after the standalone runtime/API is stable:

```text
FrameExtract Studio
        |
        v
Voice Dubbing Adapter
        |
        v
stable Voice Dubbing Runtime protocol
```

This repository must not depend on FrameExtract Studio.

---

## Development principles

1. Standalone first, embeddable later.
2. Core/runtime does not depend on the GUI.
3. FrameExtract Studio is a client, not a dependency.
4. Keep incompatible ML stacks isolated.
5. Capability discovery must not silently download models.
6. Pin model revisions and verify integrity before loading.
7. Do not silently overwrite profiles, references or run artifacts.
8. Voice-rights consent and model-license acceptance are separate gates.
9. Technical quality metrics do not replace human listening.
10. Do not commit private voice samples, user-generated audio or model weights to the public source repository.

---

## Current source layout

```text
voice-dubbing-runtime/
├── voice_dubbing_runtime/
│   ├── capabilities.py
│   ├── cli.py
│   ├── media.py
│   ├── profiles.py
│   ├── reference_quality.py
│   ├── source_separation.py
│   ├── source_separation_worker.py
│   ├── vixtts_backend.py
│   ├── worker.py
│   ├── xtts_backend.py
│   ├── xtts_engine_worker.py
│   └── config/engines.json
├── scripts/
├── tests/
├── vendor/
├── requirements-cpu.txt
├── requirements-xtts.in.txt
├── requirements-xtts.lock.txt
├── requirements-source-separation.in.txt
├── requirements-source-separation.lock.txt
├── pyproject.toml
└── uv.lock
```

---

## Disclaimer

Voice cloning can produce audio resembling a real person's voice. Users are responsible for obtaining the rights and consent required for the reference voice, generated content and intended use.

This project is currently a research/development preview. It makes no warranty regarding production suitability, output quality, model commercial rights, or rights to any person's voice.
