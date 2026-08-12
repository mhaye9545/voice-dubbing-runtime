# Third-party notices

Tài liệu này tách third-party code khỏi model weights/data. First-party project
source dùng Apache License 2.0; xem [LICENSE](LICENSE) và
[LICENSE_STATUS.md](LICENSE_STATUS.md). Apache-2.0 không tái cấp phép bất kỳ
component nào liệt kê dưới đây.

## Vendored TTS source

- Component: Coqui TTS fork dùng bởi engine `vixtts_vi`.
- Fork: <https://github.com/thinhlpg/TTS>
- Upstream: <https://github.com/coqui-ai/TTS>
- Exact commit: `ff217b3f27b294de194cc59c5119d1e08b06413c`.
- License: Mozilla Public License 2.0 (`MPL-2.0`).
- License copy: `vendor/TTS-ff217b3f27b294de194cc59c5119d1e08b06413c/LICENSE.txt`.
- Provenance: [vendor/TTS_PROVENANCE.md](vendor/TTS_PROVENANCE.md).
- Local semantic patches: **NO**; các khác biệt byte đã audit chỉ là line ending,
  cùng mười non-runtime file omission đã ghi trong provenance.

Snapshot hiện giữ upstream tests, media fixtures, notebooks, docs, recipes và
workflows. P8 không prune vì chưa có đủ bằng chứng để tách chúng mà không làm
sai provenance hoặc nghĩa vụ riêng của fixture. Chúng được phân loại
**KEEP + NOTICE/REVIEW** và không được package bởi setuptools.

## Demucs

- Component: `demucs==4.1.0`, engine/model `htdemucs`.
- Upstream: <https://github.com/facebookresearch/demucs>
- Source license: MIT; xem upstream `LICENSE`.
- Local model manifest:
  `models/source_separation/htdemucs/model_manifest.json` (model files không
  thuộc source distribution).

MIT của source code không được dùng để suy diễn quyền đối với training data,
input media hoặc output của người dùng. Release owner phải kiểm tra riêng các
điều khoản áp dụng cho model artifact được phân phối, nếu có.

## PySide6 / Qt for Python

- Component được khóa trong dev lock: `PySide6==6.11.1`, cùng
  `PySide6-Addons`, `PySide6-Essentials` và `shiboken6` tương ứng.
- Official project: <https://doc.qt.io/qtforpython-6/>
- License choices theo Qt: community LGPLv3/GPLv3 hoặc commercial.

Source checkout không bundle wheel Qt. Bất kỳ binary/installer nào bundle Qt
phải chọn một license route hợp lệ, giữ notices/license texts cần thiết và đáp
ứng nghĩa vụ relinking/source tương ứng. Không được quảng cáo commercial-ready
chỉ vì first-party code dùng Apache-2.0.

## Model weights và dữ liệu

### viXTTS

- Model: `capleaf/viXTTS`, pinned revision prefix `c06f43788831` trong runtime.
- Official model card: <https://huggingface.co/capleaf/viXTTS>
- License: Coqui Public Model License 1.0.0 (CPML).
- Evidence hiện tại nêu phạm vi non-commercial.

### XTTS-v2

- Model: `coqui/XTTS-v2`.
- Exact revision: `6c2b0d75eae4b7047358e3b6bd9325f857d43f77`.
- Official model card: <https://huggingface.co/coqui/XTTS-v2>
- License: Coqui Public Model License 1.0.0 (CPML).
- Evidence hiện tại nêu phạm vi non-commercial.

Model weights được provision riêng sau explicit acceptance; chúng không nằm
trong setuptools source distribution. Apache-2.0 code license không cấp quyền thương mại
cho model, output model, training data hay voice/reference của người dùng.
Xem thêm [docs/MODEL_LICENSES.md](docs/MODEL_LICENSES.md).

```text
CODE LICENSE != MODEL LICENSE != WEIGHTS/DATA/VOICE RIGHTS
```

## Dependency khác

Các lockfile ghi version/hash phục vụ tái lập, không thay thế license notice
của từng package. Trước binary release, owner phải tạo inventory từ artifact
thực tế và kèm các notice/license text mà từng dependency yêu cầu.
