# Security policy

## Supported versions

Project đang ở public alpha/research preview 0.x và chưa có stable release. Chỉ
HEAD của development line đang được owner review; không có version phát hành
nào được cam kết hỗ trợ.

| Version | Supported |
| --- | --- |
| Development HEAD | Best-effort review |
| Mọi snapshot/release cũ | Không có cam kết |

## Báo cáo lỗ hổng

Không mở public issue nếu báo cáo chứa exploit, secret, personal data, voice
data hoặc chi tiết có thể gây hại.

1. Nếu repository hiển thị GitHub **Private vulnerability reporting**, dùng
   kênh đó.
2. Nếu kênh đó không hiển thị, repository chưa công bố private security
   contact. Hãy liên hệ owner qua GitHub mà không gửi chi tiết nhạy cảm và yêu
   cầu một kênh riêng trước khi chia sẻ báo cáo.
3. Không gửi model weights, voice/reference thật hoặc credential làm proof.

Một báo cáo hữu ích gồm affected revision, platform/Python version, impact,
minimal reproduction đã redact và đề xuất mitigation nếu có.

## Scope

Bao gồm path traversal, unsafe deserialization, model/manifest integrity,
subprocess boundary, secret exposure, dependency confusion, storage migration,
consent bypass và việc ghi đè dữ liệu. Model quality, pronunciation hoặc điều
kiện license không phải vulnerability trừ khi dẫn đến security/authorization
impact cụ thể.

## Kỳ vọng xử lý

Owner sẽ triage theo best effort, giữ thông tin riêng trong giới hạn công cụ
hiện có và phối hợp disclosure sau khi có mitigation. Project public alpha chưa
cam kết SLA hoặc thời hạn phản hồi; tài liệu không bịa một security contact hay
khả năng remote chưa được xác minh.
