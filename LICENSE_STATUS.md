# Trạng thái license của project

Phần source code first-party của Voice Dubbing Runtime được cấp phép theo
**Apache License 2.0** (`Apache-2.0`); xem [LICENSE](LICENSE). Quyết định này áp
dụng cho source và tài liệu do project sở hữu, không tái cấp phép third-party
code, model, weights, data hoặc voice/reference của người dùng.

## Phạm vi license độc lập

- First-party project source: Apache License 2.0 (`Apache-2.0`).
- Vendored TTS source: Mozilla Public License 2.0 (`MPL-2.0`) và các nghĩa vụ
  file-level riêng; xem [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) và
  [vendor/TTS_PROVENANCE.md](vendor/TTS_PROVENANCE.md).
- viXTTS và XTTS-v2 weights: Coqui Public Model License 1.0.0 (`CPML`) theo
  evidence của revision đã pin; không thuộc Apache-2.0.
- PySide6/Qt, Demucs và các dependency khác: theo điều khoản third-party tương
  ứng; không thuộc Apache-2.0 chỉ vì được project sử dụng.
- Model weights/data, training data, input/output và voice/reference/user data:
  root code license không cấp quyền đối với các tài sản hay dữ liệu này.

## Ranh giới bắt buộc

```text
CODE LICENSE != MODEL LICENSE != WEIGHTS/DATA/VOICE RIGHTS
```

Apache-2.0 của first-party code không tạo quyền sử dụng thương mại cho
viXTTS/XTTS-v2, không thay thế consent của chủ thể giọng và không thay đổi
nghĩa vụ phân phối của vendor MPL hoặc PySide6/Qt.

Trước khi phát hành binary, owner vẫn phải audit artifact thực tế, chọn route
license Qt hợp lệ, giữ notice/source tương ứng và review riêng model/data/voice
rights. Tài liệu này mô tả ranh giới của repository, không phải tư vấn pháp lý.
