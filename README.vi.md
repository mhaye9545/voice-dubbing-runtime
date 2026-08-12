# Voice Dubbing Runtime

> Runtime clone giọng và tổng hợp giọng nói chạy cục bộ, ưu tiên CPU, được phát triển theo hướng **standalone trước – tích hợp ứng dụng khác sau**.

**Trạng thái:** đang phát triển / public alpha research preview. First-party code dùng Apache-2.0.
**Nền tảng hiện được kiểm thử:** Windows x64, Python 3.11.x, CPU.  
**Không có tuyên bố về quyền sử dụng thương mại đối với các model đi kèm hoặc được tải bởi project.**

[English](README.md) | **Tiếng Việt**

---

## 1. Mục tiêu dự án

Voice Dubbing Runtime cung cấp một runtime độc lập để:

- tạo và quản lý **voice profile** từ audio hoặc video;
- chọn đoạn giọng tham chiếu ngắn, hiện tại giới hạn **8–15 giây**;
- tách giọng khỏi nhạc/nền bằng source separation khi cần;
- kiểm tra kỹ thuật reference trước khi cho phép sử dụng;
- yêu cầu người dùng nghe và xác nhận trước khi commit reference mới;
- clone giọng và tổng hợp speech từ text;
- hỗ trợ nhiều TTS engine phía sau cùng một runtime contract;
- chạy các engine nặng trong process/environment riêng để tránh xung đột dependency;
- cung cấp CLI/JSON worker protocol để GUI hoặc ứng dụng khác tích hợp mà không phải import trực tiếp ML stack.

Dự án ban đầu được tách ra từ luồng Voice Dubbing của FrameExtract Studio. Mục tiêu tiếp theo là biến runtime này thành một **ứng dụng desktop độc lập, có giao diện riêng và có thể open source**, sau đó FrameExtract Studio chỉ tích hợp thông qua API/adapter ổn định.

---

## 2. Trạng thái hiện tại

### Đã có trong source hiện tại

- Runtime package: `voice_dubbing_runtime/`.
- CLI quản lý capability, profile và worker jobs.
- Voice profile có revision, lock/hash validation và history khi cập nhật.
- Consent về quyền sử dụng giọng được lưu theo từng profile.
- Chuẩn hóa audio bằng FFmpeg.
- Auto-select hoặc manual-select reference 8–15 giây.
- Hai bước cập nhật reference:
  1. `prepare_profile_reference` tạo candidate và evidence;
  2. `commit_profile_reference` chỉ commit sau khi người dùng xác nhận nghe và xác nhận single-speaker.
- Source separation bằng **Demucs htdemucs**, chạy trong environment/process riêng.
- Technical quality gate cho voice-only reference: duration, silence, clipping, noise-floor proxy, speech/noise contrast, separation delta…
- Engine `vixtts_vi` cho tiếng Việt trên CPU.
- Engine `xtts_v2_multilingual` cho nhiều ngôn ngữ trên CPU.
- XTTS-v2 hỗ trợ persistent worker để giữ model trong RAM giữa nhiều lượt synthesis.
- Model/runtime integrity gate bằng version, manifest, file size và SHA-256.
- Cancellation, progress marker, durable `job.json`, `run.log`, `result.json` cho mỗi job.
- Bộ unit/contract tests hiện có **140 test cases** trong `tests/`, gồm GUI tests
  offscreen và toàn bộ runtime/source-separation/XTTS contract tests.
- GUI standalone PySide6 dạng thin client trong `voice_dubbing_app/`, gồm workspace
  profile/reference review và workspace tạo giọng.

### Chưa có / chưa public-ready

- GUI standalone chưa qua manual desktop UX và real-model acceptance để phát hành.
- Chưa có installer/portable release cho người dùng cuối.
- Một số script trong `scripts/` là evidence/debug/one-off script cho các profile thử nghiệm cụ thể và cần được tách khỏi public workflow.
- Metadata project/runtime/app đã đồng bộ ở `0.3.0` (public alpha 0.x, không phải 1.0).
- First-party project source dùng Apache License 2.0; third-party/model/data/voice giữ điều khoản riêng.
- `vendor/` chứa source TTS bên thứ ba và cần được audit/đóng gói lại rõ ràng trước public release.

---

## 3. Kiến trúc hiện tại

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

Nguyên tắc chính là **runtime parent không import trực tiếp toàn bộ ML stack**. Các engine có dependency khác nhau được cô lập bằng subprocess/environment riêng.

---

## 4. Runtime environments

### Runtime chính / viXTTS

- Python: `3.11.x` (snapshot hiện tại sử dụng `3.11.15`).
- Environment: `.venv-cpu`.
- PyTorch CPU: `torch==2.6.0`, `torchaudio==2.6.0`.
- `transformers==4.49.0`.
- viXTTS dùng TTS source được pin tại commit:
  `ff217b3f27b294de194cc59c5119d1e08b06413c`.
- Runtime contract hiện chỉ quảng bá `vixtts_vi` cho ngôn ngữ `vi` trên CPU.

### XTTS-v2 multilingual

- Environment riêng: `.venv-xtts`.
- `coqui-tts[ko,zh]==0.27.5`.
- `torch==2.6.0+cpu`, `torchaudio==2.6.0+cpu`.
- `transformers==4.57.6`.
- Model: `coqui/XTTS-v2`.
- Revision được pin:
  `6c2b0d75eae4b7047358e3b6bd9325f857d43f77`.
- Capability languages được đọc từ local model `config.json`.
- Engine chỉ được đánh dấu Available khi model files, health report và các gate cần thiết đều hợp lệ.
- Snapshot hiện tại có health evidence PASS cho `en`, `ko`, `zh-cn`.

### Source separation

- Environment riêng: `.venv-source-separation`.
- Engine: `demucs==4.1.0` / `htdemucs`.
- `torch==2.6.0+cpu`, `numpy==1.26.4`, `sphn==0.2.1`, `psutil==7.2.2`.
- Chạy CPU-only.
- Chỉ xử lý đoạn reference ngắn đã chọn, không đưa toàn bộ video dài vào Demucs.
- Model manifest được kiểm tra file set, size, SHA-256 và runtime contract trước khi deserialize model.

---

## 5. Luồng tạo voice profile

```text
Video / Audio
     |
     v
Normalize source for analysis
     |
     v
Choose target speaker window
     |
     +--> Auto select 8–15 s
     |
     +--> Manual select 8–15 s
     |
     v
Prepare source mix candidate
     |
     +--> nếu cần: Demucs vocals separation
     |
     v
Normalize voice-only reference -> mono 24 kHz PCM16 WAV
     |
     v
Technical validation
     |
     v
A/B listening + user confirmation
     |
     v
Commit profile revision
     |
     v
ref_source_mix.wav
ref_voice_only.wav
ref_primary.wav
```

Technical metrics chỉ là **proxy kỹ thuật**. Runtime không coi các metric này là speaker diarization và không tự khẳng định candidate chỉ chứa đúng một người nói. Human listening vẫn là gate riêng trước khi reference được commit.

---

## 6. Voice profile và dữ liệu runtime

Canonical standalone path trên Windows là:

```text
%LOCALAPPDATA%\VoiceDubbingRuntime\
├── profiles\
│   └── <profile_id>\
│       ├── profile.json
│       ├── consent.json
│       ├── quality.json
│       ├── profile.lock
│       └── references\
└── runs\
    └── <job_id>\
        ├── job.json
        ├── run.log
        └── result.json
```

Profile update sử dụng revision check để hạn chế việc commit trên dữ liệu đã thay đổi. Các lần thay reference có thể tạo history snapshot để phục vụ audit/rollback.

Dữ liệu cũ tại `%LOCALAPPDATA%\FrameExtractStudio\VoiceDubbing\` được hỗ trợ
bằng migration và fallback an toàn:

```powershell
& .\.venv-dev\Scripts\python.exe -m voice_dubbing_runtime storage status --json
& .\.venv-dev\Scripts\python.exe -m voice_dubbing_runtime storage migrate --json
```

Migration chỉ copy các subtree `profiles`, `runs`, `licenses`, `config` và
`state` nếu có, xác minh SHA-256 từng file rồi mới ghi marker hoàn tất. Source
legacy không bị move, rename, delete hoặc overwrite. Trước khi hash verify hoàn
tất, runtime tiếp tục fallback sang legacy store để profile cũ không biến mất.

---

## 7. Thiết lập contributor trên Windows

Yêu cầu Python 3.11 x64. Cả hai script bootstrap đều nhận
`-PythonExecutable <path>`; nếu bỏ qua, script dùng lệnh `python` hiện tại và
fail rõ nếu không phải Python 3.11. Script không xóa environment đang có.

```powershell
# Environment dev/GUI/test tự đủ, không ghép PYTHONPATH từ environment khác
& .\scripts\bootstrap_dev.ps1 -PythonExecutable C:\path\to\python.exe

# Environment viXTTS CPU cô lập, dùng lock có hash và vendored TTS
& .\scripts\bootstrap_cpu.ps1 -PythonExecutable C:\path\to\python.exe

# Doctor read-only; --deep import thêm các class XTTS nhưng không load model
& .\.venv-dev\Scripts\python.exe -m voice_dubbing_runtime doctor --json
& .\.venv-cpu\Scripts\python.exe -m voice_dubbing_runtime doctor --deep
```

Bootstrap base không download model. XTTS-v2 và Demucs vẫn được cô lập và chỉ
provision rõ ràng bằng `scripts/provision_xtts_v2.py` và
`scripts/provision_demucs_htdemucs.py` sau license gate tương ứng.

---

## 8. CLI contract hiện tại

```powershell
# Runtime capabilities
& .\.venv-cpu\Scripts\python.exe -u -m voice_dubbing_runtime capabilities --json

# Profiles
& .\.venv-cpu\Scripts\python.exe -u -m voice_dubbing_runtime profiles list --json
& .\.venv-cpu\Scripts\python.exe -u -m voice_dubbing_runtime profiles get --profile-id <profile_id> --json
& .\.venv-cpu\Scripts\python.exe -u -m voice_dubbing_runtime profiles create --request <request.json> --json
& .\.venv-cpu\Scripts\python.exe -u -m voice_dubbing_runtime profiles create-from-source --request <request.json>
& .\.venv-cpu\Scripts\python.exe -u -m voice_dubbing_runtime profiles update --request <request.json> --json
& .\.venv-cpu\Scripts\python.exe -u -m voice_dubbing_runtime profiles consent --request <request.json> --json
& .\.venv-cpu\Scripts\python.exe -u -m voice_dubbing_runtime profiles delete --profile-id <profile_id> --json

# Storage status và migration copy/hash-verify
& .\.venv-dev\Scripts\python.exe -m voice_dubbing_runtime storage status --json
& .\.venv-dev\Scripts\python.exe -m voice_dubbing_runtime storage migrate --json

# Worker - one job
& .\.venv-cpu\Scripts\python.exe -u -m voice_dubbing_runtime worker --job <job.json>

# Worker - multiple JSONL jobs
& .\.venv-cpu\Scripts\python.exe -u -m voice_dubbing_runtime worker --jobs-jsonl <jobs.jsonl>
```

Worker hiện hỗ trợ các action:

```text
create_profile
prepare_profile_reference
commit_profile_reference
synthesize
```

Machine-readable marker được prefix bằng:

```text
@@VOICE_DUB|
```

Đây là contract chính để standalone GUI và sau này FrameExtract Studio giao tiếp với runtime mà không phải import ML engine trực tiếp.

---

### Chạy standalone GUI trong development

Bootstrap environment development tự đủ rồi chạy:

```powershell
& .\scripts\bootstrap_dev.ps1
& .\.venv-dev\Scripts\python.exe -m voice_dubbing_app
```

GUI chỉ là thin client. Khi mở app, GUI chỉ đọc capability và profile inventory;
không load/download model. Các job nặng được gửi tới JSONL worker hiện có trong
`.venv-cpu`, còn ML stack của từng engine vẫn nằm trong process/environment cô lập.

---

## 9. Kiểm thử

Test suite hiện bao phủ các nhóm chính:

- engine capabilities;
- CLI contract;
- media normalization và timestamp repair;
- profile creation/update/consent/revision/robustness;
- known-profile repair migration;
- reference preparation và commit flow;
- voice-only technical quality gate;
- source-separation manifest/runtime contract;
- worker lifecycle, duplicate job và cancellation;
- XTTS worker contract.

Chạy full suite bằng `.venv-dev`, không cần ghép package từ environment khác:

```powershell
& .\scripts\run_tests.ps1
```

Kiểm tra dependency environments:

```powershell
& .\.venv-dev\Scripts\python.exe -m pip check
& .\.venv-cpu\Scripts\python.exe -m pip check
& .\.venv-xtts\Scripts\python.exe -m pip check
& .\.venv-source-separation\Scripts\python.exe -m pip check
```

---

## 10. Model, license và voice consent

### Voice consent

Project yêu cầu consent theo từng profile. Consent của một voice profile không được tự động tái sử dụng để cấp quyền cho profile khác.

### viXTTS và XTTS-v2

Các model snapshot hiện có trong môi trường phát triển ghi nhận **Coqui Public Model License 1.0.0 (CPML)** và scope non-commercial. Vì vậy:

- open-source **code** của project không đồng nghĩa với quyền sử dụng thương mại model;
- model weights không nên được commit/bundle vào public repository mặc định;
- người dùng phải tự đọc và chấp nhận license model tương ứng trước khi provision/use;
- project không tuyên bố cấp lại quyền thương mại cho model hoặc output.

### Demucs

Model/source-separation manifest hiện khai báo Demucs `htdemucs` theo MIT license.

### Third-party source

Repo hiện có vendored TTS source mang Mozilla Public License 2.0. Audit giữ
vendor fixtures theo quyết định **KEEP + NOTICE/REVIEW**; source này không được
setuptools package. Xem [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) và
[vendor/TTS_PROVENANCE.md](vendor/TTS_PROVENANCE.md).

> **First-party project source dùng Apache License 2.0 (`Apache-2.0`). License
> này không tái cấp phép vendor, model, weights/data hay voice/reference.** Xem
> [LICENSE](LICENSE), [LICENSE_STATUS.md](LICENSE_STATUS.md) và
> [docs/MODEL_LICENSES.md](docs/MODEL_LICENSES.md).

Heavy runtime/model được provision riêng sau explicit license gate. Người dùng
phải tự đọc điều khoản model hiện hành; Apache-2.0 không cấp quyền model,
weights/data, output hoặc voice/reference.

```text
CODE LICENSE != MODEL LICENSE != WEIGHTS/DATA/VOICE RIGHTS
```

Tài liệu cộng đồng: [CONTRIBUTING.md](CONTRIBUTING.md),
[SECURITY.md](SECURITY.md), [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

---

## 11. Roadmap

### Phase 0 — Open-source cleanup & repository hardening **(đang làm)**

- [x] Viết lại README và tài liệu public-policy chính.
- [x] Đồng bộ version `0.3.0` giữa `pyproject.toml`, `__version__` và capability protocol.
- [x] First-party source dùng Apache-2.0 sau ownership/license audit.
- [x] Thêm `THIRD_PARTY_NOTICES.md` và model-license policy.
- [x] Loại model weights khỏi source distribution/repository tracking.
- [x] Rà soát `vendor/` và khóa exact provenance/source resolution.
- [x] Chuyển one-off profile scripts khỏi public workflow.
- [x] Genericize public core nhưng giữ compatibility identifiers.
- [x] Dùng namespace standalone và migration copy/hash-verify có legacy fallback.
- [x] Thêm CI Windows + Python 3.11 với required check `windows-python311`.

### Phase 1 — Reproducible runtime bootstrap

- [x] Có bootstrap công khai cho `.venv-dev` và `.venv-cpu` sạch.
- [x] Khóa dependency bằng hash và dùng PyTorch CPU cho dev/viXTTS.
- [ ] Kiểm tra/download FFmpeg theo policy rõ ràng.
- [ ] Provision model theo explicit license gate.
- [x] Thêm lệnh `doctor` read-only để kiểm tra runtime, vendor, GUI, model, dependency và storage status.
- [ ] Không download model chỉ vì mở app hoặc gọi capability discovery.

### Phase 2 — Standalone Desktop GUI

Thin-client GUI đầu tiên hiện đã có trong source. GUI tests offscreen bao phủ
marker parser, tách state create/update, manual-review commit gate và capability
filter cho synthesis. Manual desktop UX và real-model acceptance vẫn còn pending.

Mục tiêu GUI đầu tiên:

- [x] chọn video/audio;
- [ ] hiển thị metadata nguồn;
- [x] chọn target-speaker window;
- [x] auto/manual reference 8–15 giây;
- [x] ghi nhận background audio để runtime tự quyết định source separation;
- [x] nghe A/B `source mix` và `voice only`;
- [x] xác nhận consent và single-speaker trước commit;
- [x] tạo/cập nhật/xóa voice profile;
- [x] nhập text, language, engine, speed;
- [x] synthesize, nghe preview và lưu WAV;
- [x] progress, cancel, lỗi có mã rõ ràng;
- [x] xem log/job result khi cần chẩn đoán.

GUI phải là **thin client**: không chứa ML/business logic vốn đã nằm trong runtime.

### Phase 3 — Runtime/API stabilization

- [ ] Version hóa job/result schema.
- [ ] Tách public API khỏi migration/debug API.
- [ ] Chuẩn hóa progress event và error codes.
- [ ] Backward-compatibility policy cho profile schema.
- [ ] Public Python API mỏng bên cạnh CLI/JSON protocol.
- [ ] Regression fixtures không chứa voice data riêng tư.

### Phase 4 — Reference quality & voice cloning quality

- [ ] Candidate ranking tốt hơn cho audio/video dài.
- [ ] A/B candidate comparison trực tiếp trong GUI.
- [ ] Optional denoise/source-separation engines qua adapter.
- [ ] Metrics/evidence dễ hiểu hơn cho user.
- [ ] Benchmark CPU time/RAM theo engine.
- [ ] Quality regression suite với dữ liệu có license rõ ràng.

Không tự động thay human listening bằng heuristic speaker-identity claim.

### Phase 5 — Dubbing workflow

Sau khi single-utterance voice cloning ổn định:

- [ ] hỗ trợ danh sách đoạn text/subtitle;
- [ ] batch synthesis;
- [ ] timing/duration adaptation;
- [ ] ghép các đoạn audio thành dubbing track;
- [ ] preview với video gốc;
- [ ] export audio track hoặc mux vào video;
- [ ] thiết kế ASR/translation thành module tùy chọn, không buộc runtime core phải phụ thuộc chúng.

### Phase 6 — Packaging & public release

- [ ] Desktop build cho Windows.
- [ ] Model downloader/provisioner tách khỏi app binary.
- [ ] Không bundle model có license hạn chế nếu chưa được phép.
- [ ] Release checksum/signing policy.
- [ ] Clean-machine smoke test.
- [ ] Documentation cho contributors.
- [ ] Issue/PR templates và contribution guide.

### Phase 7 — FrameExtract Studio integration

Chỉ thực hiện sau khi standalone runtime/API ổn định:

```text
FrameExtract Studio
        |
        v
Voice Dubbing Adapter
        |
        v
stable Voice Dubbing Runtime protocol
```

`voice-dubbing-runtime` không được import hoặc phụ thuộc ngược vào FrameExtract Studio. FrameExtract chỉ là một client của runtime.

---

## 12. Nguyên tắc phát triển

1. **Standalone first, embeddable later.**
2. Core/runtime không phụ thuộc GUI.
3. FrameExtract không phải dependency của open-source project.
4. Engine-specific ML dependencies phải được cô lập.
5. Capability discovery không được ngầm download model.
6. Model phải có revision/integrity contract rõ ràng.
7. Không overwrite profile/reference/run artifact một cách âm thầm.
8. Voice consent và model-license acceptance là hai gate khác nhau.
9. Technical quality gate không thay thế human listening.
10. Không commit private voice samples, generated user audio hoặc model weights vào public source repo.

---

## 13. Cấu trúc source hiện tại

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

Các thư mục runtime-generated như `.venv-*`, `.python`, `.cache`, `models/` và `runs/` không phải source distribution và đã được ignore.

---

## 14. Current development focus

Công việc hiện tại tập trung vào việc chuyển project từ **runtime nghiên cứu phục vụ FrameExtract** thành **open-source standalone voice dubbing application** mà không làm mất các contract đã kiểm thử:

1. đóng băng và mô tả đúng core hiện tại;
2. dọn repository cho public release;
3. làm bootstrap có thể tái lập;
4. xây standalone GUI trên runtime contract hiện tại;
5. test thực tế bằng nhiều loại audio/video;
6. chỉ sau khi workflow standalone ổn định mới xây adapter tích hợp lại FrameExtract Studio.

---

## 15. Disclaimer

Voice cloning có thể tạo ra nội dung giống giọng của người thật. Người dùng có trách nhiệm bảo đảm mình có quyền và sự đồng ý cần thiết đối với voice reference, nội dung tạo ra và cách sử dụng output.

Project hiện ở giai đoạn nghiên cứu/phát triển và không đưa ra bảo đảm về chất lượng, tính phù hợp cho sản xuất, quyền thương mại của model, hay quyền sử dụng giọng của bất kỳ cá nhân nào.
