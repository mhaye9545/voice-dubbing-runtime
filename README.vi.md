# Voice Dubbing Runtime

> Runtime clone giọng và tổng hợp giọng nói chạy cục bộ, ưu tiên CPU, được phát triển theo hướng **standalone trước – tích hợp ứng dụng khác sau**.

**Trạng thái:** đang phát triển / research preview.  
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
- Bộ unit/contract tests hiện có **80 test cases** trong `tests/`.

### Chưa có / chưa public-ready

- Chưa có standalone desktop GUI trong repo này.
- Chưa có installer/portable release cho người dùng cuối.
- Chưa có một lệnh bootstrap duy nhất dựng toàn bộ runtime từ máy sạch.
- Storage path vẫn còn tên legacy của FrameExtract Studio.
- Một số script trong `scripts/` là evidence/debug/one-off script cho các profile thử nghiệm cụ thể và cần được tách khỏi public workflow.
- Metadata version chưa đồng bộ hoàn toàn: runtime code đang báo `0.3.0`, trong khi `pyproject.toml` vẫn là `0.1.0`.
- Repo root hiện chưa có license chính thức cho phần code của project.
- `vendor/` chứa source TTS bên thứ ba và cần được audit/đóng gói lại rõ ràng trước public release.

---

## 3. Kiến trúc hiện tại

```text
CLI / future Desktop GUI / external client
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

Hiện tại dữ liệu user được lưu theo legacy path:

```text
%LOCALAPPDATA%\FrameExtractStudio\VoiceDubbing\
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

> Trước standalone public release, path này sẽ được đổi thành namespace riêng của project và có migration tương thích cho dữ liệu cũ.

---

## 7. CLI contract hiện tại

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

## 8. Kiểm thử

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

Chạy test bằng **Python 3.11 environment của project**:

```powershell
$env:PYTHONDONTWRITEBYTECODE = "1"
$env:PYTHONUTF8 = "1"
& .\.venv-cpu\Scripts\python.exe -m unittest discover -s tests -t . -p "test_*.py" -v
```

Kiểm tra dependency environments:

```powershell
& .\.tools\uv\uv.exe pip check --python .\.venv-cpu\Scripts\python.exe
& .\.tools\uv\uv.exe pip check --python .\.venv-xtts\Scripts\python.exe
& .\.tools\uv\uv.exe pip check --python .\.venv-source-separation\Scripts\python.exe
```

---

## 9. Model, license và voice consent

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

Repo hiện có vendored TTS source mang Mozilla Public License 2.0. Trước public release cần hoàn tất `THIRD_PARTY_NOTICES`, quyết định cách phân phối `vendor/`, và chọn license phù hợp cho code do project tự viết.

> **Root project license hiện chưa được chốt. Không nên public release với nhãn MIT/Apache/MPL cho toàn bộ repo cho tới khi hoàn tất dependency/license audit.**

---

## 10. Roadmap

### Phase 0 — Open-source cleanup & repository hardening **(đang làm)**

- [ ] Viết lại README và tài liệu kiến trúc.
- [ ] Đồng bộ version giữa `pyproject.toml`, `__version__` và capability protocol.
- [ ] Chọn root source-code license sau dependency/license audit.
- [ ] Thêm `THIRD_PARTY_NOTICES.md`.
- [ ] Tách model weights khỏi source repository.
- [ ] Rà soát `vendor/` và cơ chế lấy pinned TTS source.
- [ ] Chuyển các script hard-coded cho profile thử nghiệm sang `devtools/` hoặc archive evidence riêng.
- [ ] Xóa tên/path/persona thử nghiệm khỏi public workflow.
- [ ] Đổi storage namespace khỏi `FrameExtractStudio` sang tên độc lập và có migration.
- [ ] Thêm CI cho Windows + Python 3.11.

### Phase 1 — Reproducible runtime bootstrap

- [ ] Tạo một bootstrap command duy nhất cho máy sạch.
- [ ] Tự dựng `.venv-cpu`, `.venv-xtts`, `.venv-source-separation`.
- [ ] Kiểm tra/download FFmpeg theo policy rõ ràng.
- [ ] Provision model theo explicit license gate.
- [ ] Thêm lệnh `doctor` để kiểm tra Python, FFmpeg, model, SHA-256, dependency và engine health.
- [ ] Không download model chỉ vì mở app hoặc gọi capability discovery.

### Phase 2 — Standalone Desktop GUI

Mục tiêu GUI đầu tiên:

- [ ] chọn video/audio;
- [ ] hiển thị metadata nguồn;
- [ ] chọn target-speaker window;
- [ ] auto/manual reference 8–15 giây;
- [ ] bật source separation khi source có nhạc/nền;
- [ ] nghe A/B `source mix` và `voice only`;
- [ ] xác nhận consent và single-speaker trước commit;
- [ ] tạo/cập nhật/xóa voice profile;
- [ ] nhập text, language, engine, speed;
- [ ] synthesize, nghe preview và lưu WAV;
- [ ] progress, cancel, lỗi có mã rõ ràng;
- [ ] xem log/job result khi cần chẩn đoán.

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

## 11. Nguyên tắc phát triển

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

## 12. Cấu trúc source hiện tại

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

## 13. Current development focus

Công việc hiện tại tập trung vào việc chuyển project từ **runtime nghiên cứu phục vụ FrameExtract** thành **open-source standalone voice dubbing application** mà không làm mất các contract đã kiểm thử:

1. đóng băng và mô tả đúng core hiện tại;
2. dọn repository cho public release;
3. làm bootstrap có thể tái lập;
4. xây standalone GUI trên runtime contract hiện tại;
5. test thực tế bằng nhiều loại audio/video;
6. chỉ sau khi workflow standalone ổn định mới xây adapter tích hợp lại FrameExtract Studio.

---

## 14. Disclaimer

Voice cloning có thể tạo ra nội dung giống giọng của người thật. Người dùng có trách nhiệm bảo đảm mình có quyền và sự đồng ý cần thiết đối với voice reference, nội dung tạo ra và cách sử dụng output.

Project hiện ở giai đoạn nghiên cứu/phát triển và không đưa ra bảo đảm về chất lượng, tính phù hợp cho sản xuất, quyền thương mại của model, hay quyền sử dụng giọng của bất kỳ cá nhân nào.
