# Guacamole Client: Windows remote workspaces

Đây là fork public của [Apache Guacamole](https://github.com/apache/guacamole-client), kèm bộ triển khai self-host để chạy và cấp phát workspace Windows 11 từ một máy Windows.

Mã Guacamole upstream vẫn nằm trong các module Maven gốc. Lớp triển khai cho môi trường này nằm trong [`deploy-local/`](deploy-local/): script PowerShell, Compose, libvirt, Cockpit **Workspace Templates** và runbook vận hành.

```text
origin:   https://github.com/TimTini/guacamole-client.git
upstream: https://github.com/apache/guacamole-client.git
```

File [`README`](README) không có phần mở rộng vẫn là hướng dẫn build upstream của Apache Guacamole. File `README.md` này mô tả mục tiêu và bộ triển khai local của fork.

## Mục tiêu và kiến trúc

Windows chạy WSL2 với Ubuntu 24.04. Ubuntu chạy Docker Compose cho Guacamole,
guacd và PostgreSQL; QEMU/KVM + libvirt chạy các VM; Cockpit Machines quản trị
VM; Guacamole cung cấp web UI, xác thực và phân quyền RDP. Dữ liệu vận hành
được tách khỏi source Git trong thư mục `runtime\`.

```text
Browser -> 127.0.0.1:8080/guacamole/ -> guacd -> RDP -> Windows/Ubuntu VM
                      |
                      +-> PostgreSQL

Cockpit https://127.0.0.1:9090 -> libvirt -> QEMU/KVM -> VM
Ubuntu-24.04 WSL2 -> Docker, libvirt và dữ liệu local trong runtime/
Cloudflare Quick Tunnel (tùy chọn) -> chỉ proxy Guacamole localhost
```

Thành phần chính:

- `guacamole/guacamole:1.6.0`, `guacamole/guacd:1.6.0` và `postgres:16-alpine`;
- QEMU/KVM, libvirt, network riêng `guac-nat`, DHCP reservation và RDP route;
- Cockpit Machines và trang **Workspace Templates**;
- WSL `runtime\ubuntu\ext4.vhdx` chứa Docker volume và Linux state.

## Tính năng

- Chạy Guacamole local trên Windows qua WSL2 và Docker Compose.
- Lưu PostgreSQL trong WSL ext4 VHDX, tách khỏi source Git.
- Quản lý VM Windows 11 và Ubuntu demo qua libvirt/Cockpit.
- Tạo template read-only theo phiên bản, clone với UUID, MAC, NVRAM, TPM và overlay riêng.
- Tạo workspace trong Cockpit, chọn template, đặt tên và gán Guacamole user/group.
- Có CLI fallback, repair idempotent, start/status/stop và runbook clean install.

## Chạy deployment đã có

Phần này dành cho deployment đã có `runtime\ubuntu\ext4.vhdx`, VM và secret.
Cài mới từ máy gần như trống xem [clean install trong RUNBOOK](deploy-local/RUNBOOK.md).

```powershell
Set-Location <REPO_ROOT>
.\START-REMOTE.cmd start
.\START-REMOTE.cmd status
.\START-REMOTE.cmd stop
```

`START-REMOTE.cmd start` là đường dẫn whole-stack và luôn gọi Quick Tunnel;
đường dẫn này yêu cầu có `runtime\cloudflared\cloudflared.exe`. Lệnh này không
tự bật Windows 11 hoặc Ubuntu demo. Nếu chỉ cần truy cập local và chưa có
cloudflared, hãy start các thành phần riêng rồi bỏ qua Quick Tunnel:

```powershell
Set-Location <REPO_ROOT>\deploy-local
.\start-local.ps1 -Action start
.\libvirt.ps1 -Action network
.\libvirt.ps1 -Action storage
.\libvirt.ps1 -Action connect-guacamole
```

Sau khi đã migration Windows 11 sang libvirt (có marker
`runtime\vm-windows11\libvirt-cutover.marker`), chuẩn bị socket TPM để Cockpit
có thể bật VM:

```powershell
.\libvirt.ps1 -Action prepare-tpm
```

Các lệnh trên không tự bật Windows 11 hoặc Ubuntu demo. Khi cần dùng VM, bật
thủ công Windows 11 qua Cockpit hoặc chạy:

```powershell
.\libvirt.ps1 -Action start
```

Ubuntu demo có thể bật thủ công bằng:

```powershell
.\vm-demo\qemu-demo.ps1 -Action start
```

Entrypoint PowerShell tương đương:

```powershell
Set-Location <REPO_ROOT>\deploy-local
.\recover-after-rollback.ps1 start
.\recover-after-rollback.ps1 status
.\recover-after-rollback.ps1 stop
```

URL sau khi start:

| Dịch vụ | URL/phạm vi |
|---|---|
| Guacamole | `http://127.0.0.1:8080/guacamole/` |
| Cockpit Machines | `https://127.0.0.1:9090` |
| Windows RDP | `192.168.250.x:3389`, chỉ sẵn sàng sau khi bật VM, mạng private qua Guacamole |
| Quick Tunnel | URL `trycloudflare.com` tạm thời do script in ra nếu có `cloudflared.exe` |

Guacamole mặc định chỉ bind vào `127.0.0.1:8080`. Quick Tunnel chỉ proxy
Guacamole; không mở Cockpit hoặc RDP trực tiếp. Xem [deploy-local/README.md](deploy-local/README.md)
cho topology/start path và [deploy-local/RUNBOOK.md](deploy-local/RUNBOOK.md)
cho recovery, maintenance và troubleshooting.

## Cài mới

[RUNBOOK.md](deploy-local/RUNBOOK.md) mô tả toàn bộ
đường dẫn cài mới: bật WSL2/Virtual Machine Platform, kiểm tra `/dev/kvm`, clone
fork, chuẩn bị Ubuntu WSL, cài Docker/QEMU/libvirt/Cockpit, cung cấp ISO Windows 11 và kiểm tra
Guacamole/RDP. Không unregister hoặc import đè `ext4.vhdx` đang được sử dụng.

## Workspace Templates

Sau khi domain nguồn `windows11` và template version đã được tạo local:

1. Mở Cockpit tại `https://127.0.0.1:9090`, vào **Workspace Templates**.
2. Chọn template, thường là `windows11-v1`.
3. Nhập tên workspace duy nhất và chọn Guacamole user hoặc group.
4. Gửi yêu cầu, theo dõi **Recent workspaces** và chờ RDP readiness.
5. Helper tạo connection Guacamole và gán quyền cho assignee.

Job root-owned tiếp tục sau refresh hoặc mất WebSocket. Với `pending`,
`waiting-rdp` hoặc `sync-failed`, dùng **Repair / resume** hoặc lệnh `repair`
trong [RUNBOOK](deploy-local/RUNBOOK.md#golden-template-operation-and-recovery-contract).
CLI fallback:

```powershell
Set-Location <REPO_ROOT>\deploy-local
.\create-windows-template.ps1 -Source windows11 -Version windows11-v1
.\clone-windows-vm.ps1 -Name windows-work-02 -AssignUser demo -TemplateVersion windows11-v1
```

Golden template của mỗi deployment nằm local trong WSL tại `/var/lib/guacamole-templates`;
nó không nằm trong GitHub và không được đưa vào public release.

## Public boundary và licensing

Repository public chỉ chứa source Apache Guacamole, script/cấu hình triển khai,
UI, test và tài liệu. Repository không chứa Windows ISO/image, VHDX, QCOW2,
VMDK hoặc golden template; license/activation key của người dùng; database,
WSL `ext4.vhdx`, UEFI/NVRAM/TPM state, logs/backup; hay password, database
secret, private key, token, tunnel URL và credential local.

Người dùng phải tự tải ISO Windows 11 từ nguồn chính thức của Microsoft, có
license hợp lệ cho cách sử dụng của mình và tự đặt password guest local.
Generic Pro key trong answer file chỉ chọn edition, không activate Windows.
Không dùng lại password của máy khác và không đưa secret vào commit, inventory,
connection description hoặc URL công khai.

`runtime\`, `deploy-local\data\` và `deploy-local\secrets\` bị Git ignore.
Trước commit/push, stage theo allowlist và chạy:

```powershell
Set-Location <REPO_ROOT>
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\public-release-audit.ps1
```

`origin` phải là fork/repository do bạn kiểm soát. Apache upstream chỉ dùng làm
`upstream` để lấy source và đối chiếu. Xem [public release boundary](deploy-local/README.md#public-release-boundary)
và [runbook boundary](deploy-local/RUNBOOK.md#public-release-boundary).

## Apache Guacamole upstream

Fork này giữ attribution, `LICENSE`, `NOTICE` và các module/source upstream.
Apache Guacamole cung cấp web application, guacd, Docker images và tài liệu;
`deploy-local` là lớp vận hành riêng cho self-hosted Windows workspace.

- [Apache Guacamole manual](https://guacamole.apache.org/doc/gug/)
- [Guacamole Docker](https://guacamole.apache.org/doc/gug/guacamole-docker.html)
- [PostgreSQL authentication](https://guacamole.apache.org/doc/gug/postgresql-auth.html)
