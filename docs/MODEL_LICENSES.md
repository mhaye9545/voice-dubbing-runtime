# Model, weights, data và voice rights

## Bốn lớp quyền độc lập

1. First-party source code: Apache License 2.0 (`Apache-2.0`); xem
   [LICENSE](../LICENSE) và [LICENSE_STATUS.md](../LICENSE_STATUS.md).
2. Third-party source code: theo license riêng; xem
   [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md).
3. Model weights/data: theo model/data license riêng.
4. Voice/reference: cần quyền và consent hợp lệ của người dùng/chủ thể giọng.

Một lớp được phép không làm các lớp còn lại tự động được phép.

```text
CODE LICENSE != MODEL LICENSE != WEIGHTS/DATA/VOICE RIGHTS
```

Apache-2.0 chỉ áp dụng cho first-party project source. Nó không tái cấp phép
model, weights/data, output model, third-party code hoặc voice/reference.

## viXTTS

Runtime dùng snapshot `capleaf/viXTTS` đã pin. Model card upstream và
`LICENSE.txt` đi kèm snapshot khai báo Coqui Public Model License 1.0.0. Bằng
chứng license hiện tại giới hạn model và output vào mục đích non-commercial.

Nguồn chính thức: <https://huggingface.co/capleaf/viXTTS>

## XTTS-v2

Runtime pin `coqui/XTTS-v2` tại revision
`6c2b0d75eae4b7047358e3b6bd9325f857d43f77`. Model card và license file khai
báo Coqui Public Model License 1.0.0, với phạm vi non-commercial theo evidence
hiện tại.

Nguồn chính thức: <https://huggingface.co/coqui/XTTS-v2>

Provisioning yêu cầu explicit acceptance record. Việc có record chỉ chứng minh
workflow đã ghi nhận acceptance cho revision đã pin; nó không mở rộng license.

## Demucs/htdemucs

Demucs source được upstream công bố theo MIT. Model manifest local cũng ghi
MIT cho `htdemucs`, nhưng source-code license không nên được suy diễn thành
quyền đối với training corpus, input media hoặc output. Nếu bundle checkpoint,
owner phải review artifact và nguồn phân phối cụ thể trong release đó.

Nguồn chính thức: <https://github.com/facebookresearch/demucs>

## Quy tắc phân phối

- Không gọi CPML model là “open-source model”.
- Không quảng cáo commercial-ready dựa trên code license.
- Không bundle/download model chỉ vì import package, mở GUI hoặc discovery.
- Luôn giữ model revision, manifest, SHA-256 và explicit license gate.
- Không đưa voice/reference của người dùng vào test fixture hoặc release.
- Người dùng phải tự đánh giá điều khoản hiện hành và quyền tại jurisdiction của họ.
