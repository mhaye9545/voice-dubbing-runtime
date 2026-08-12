# Kế hoạch branch protection

Đây là cấu hình owner cần áp dụng trên GitHub sau khi workflow đã chạy thành
công ở remote. Tài liệu không tuyên bố các setting này hiện đang active và
không tự thực thi remote write.

Required status check chính xác:

```text
windows-python311
```

## `develop`

- Require a pull request before merging.
- Require ít nhất 1 approval.
- Require conversation resolution before merging.
- Require status check `windows-python311`.
- Block force push.
- Block deletion.

## `main`

- Require a pull request before merging.
- Require ít nhất 1 approval.
- Dismiss stale pull request approvals when new commits are pushed.
- Require review from Code Owners.
- Require conversation resolution before merging.
- Require status check `windows-python311`.
- Restrict direct push.
- Block force push.
- Block deletion.

## Workflow canonical

```text
fork / feature branch
→ PR vào develop
→ CI windows-python311
→ review
→ merge vào develop
→ owner release review
→ owner PR/merge develop vào main
```

Contributor không merge trực tiếp vào `main`. Nếu remote chưa có `develop`,
owner chỉ tạo branch này sau khi review cumulative candidate, fetch remote mới
nhất và chọn baseline remote phù hợp; không tạo từ local `main` stale.
