# Vendored TTS provenance

The authoritative TTS source for the `vixtts_vi` CPU runtime is the vendored
tree at:

```text
vendor/TTS-ff217b3f27b294de194cc59c5119d1e08b06413c/
```

## Source

- Project: Coqui TTS
- Fork: https://github.com/thinhlpg/TTS
- Commit: `ff217b3f27b294de194cc59c5119d1e08b06413c`
- Commit subject: `fix: add missing char_limits for Vietnamese`
- Package version: `0.22.0`
- Code license: Mozilla Public License 2.0 (`MPL-2.0`)
- License file: `TTS-ff217b3f27b294de194cc59c5119d1e08b06413c/LICENSE.txt`

The fork commit is based on Coqui TTS. Its local `CITATION.cff`, `README.md`,
and `setup.py` identify the upstream project as
https://github.com/coqui-ai/TTS.

## Local changes

No semantic source patches were found when this snapshot was compared with the
fork commit above. Byte-level differences in eight tracked files were line
ending normalization only; a diff using `--ignore-cr-at-eol` was empty.

The repository intentionally omits ten non-runtime files from the upstream
commit: eight model documentation pages and two recipe/test JSON fixtures. All
437 Python files from the commit, including the nested `TTS/*/models` packages,
are retained. No model weights or checkpoints belong in this source tree.

## Why this source is vendored

The isolated `.venv-cpu` bootstrap installs the pinned requirements from this
tree and places this exact directory first on `PYTHONPATH`. The `vixtts_vi`
backend also prepends this directory before importing `TTS`. Keeping this
snapshot authoritative preserves the validated Vietnamese fork behavior and
supports an offline runtime after dependencies have been provisioned.

The separate `xtts_v2_multilingual` engine is not a fallback for `vixtts_vi`.
It runs in `.venv-xtts` and uses the separately pinned `coqui-tts` package.

## Verification and update procedure

1. Clone `https://github.com/thinhlpg/TTS` outside this repository.
2. Check out the exact commit recorded above in detached-HEAD mode.
3. Compare its files with the vendored tree, ignoring only CRLF/LF differences
   and the ten documented non-runtime omissions.
4. Report every semantic difference as a local patch before changing the
   pinned commit.
5. Run the TTS source-resolution probe, viXTTS-targeted tests, compatibility
   tests, and the full regression suite before accepting an update.

The source license does not grant rights to any model weights or training data.
Model and data licenses must be reviewed independently.
