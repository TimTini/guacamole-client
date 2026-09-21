# Guacamole local deployment runbook

Tài liệu này mô tả cách cài đặt và vận hành Guacamole cùng Windows workspaces
trên Windows/WSL2. Mục tiêu là để có thể cài từ máy gần như trống, vận hành,
phục hồi, cập nhật và xử lý lỗi bằng các bước có kiểm chứng.

## 1. Nguyên tắc bắt buộc

- Các script hiện dùng compatibility layout
  `H:\RemoteWorkspaces\guacamole-client`. Đây là giới hạn của phiên bản hiện
  tại, không phải yêu cầu của Guacamole, Cockpit hay libvirt.
- Không chạy docker compose down -v, không xóa Docker volume, không xóa
  runtime\ubuntu\ext4.vhdx, và không sinh secret mới khi database hoặc VM cũ
  còn tồn tại.
- Không đưa password, token, secret file hoặc URL tunnel vào commit, connection
  description hay chat.
- Trước mọi thay đổi, chạy git status --short và giữ nguyên thay đổi có sẵn.
- Windows 11 generic Pro key trong answer file chỉ chọn edition; nó không
  activate Windows.

## 2. Kiến trúc triển khai

    Browser ngoài máy
            |
            | HTTPS, Cloudflare Quick Tunnel (URL tạm thời)
            v
    127.0.0.1:8080 trên Windows
            |
            +-- Docker trong Ubuntu-24.04 WSL
                +-- guacamole:1.6.0
                +-- guacd:1.6.0
                +-- postgres:16-alpine
                         |
                         +-- volume trong ext4.vhdx trên H:

    Guacamole container -- 172.18.0.1:3390 --> Ubuntu QEMU RDP
                        -- 172.18.0.1:3391 --> Windows 11 RDP
                        -- 172.18.0.1:5901 --> Windows 11 VNC/recovery

| Thành phần | Vị trí hoặc cổng | Ghi chú |
|---|---|---|
| Repository và script | H:\RemoteWorkspaces\guacamole-client | Có thể clone lại từ Git |
| WSL disk | runtime\ubuntu\ext4.vhdx | Chứa Ubuntu WSL, Docker và database volume |
| PostgreSQL volume | guacamole-local-postgres-data trong WSL | Không phải thư mục init SQL trên H: |
| PostgreSQL init SQL | deploy-local\data\postgres-init\001-guacamole.sql | Chỉ dùng khi tạo database mới |
| Secret | deploy-local\secrets\*.txt | Git ignore và ACL giới hạn |
| Windows ISO | runtime\iso\Windows11_23H2_UEFI.iso | Đầu vào cài Windows |
| Windows state | runtime\vm-windows11 và /var/lib/guacamole-vm-windows11 | UEFI/TPM/log ở H:, qcow2 trong WSL ext4 |
| Ubuntu demo | runtime\vm-demo và /var/lib/guacamole-vm-demo | VMware hoặc QEMU fallback |
| Cloudflared | runtime\cloudflared\cloudflared.exe | Binary repo-local |
| Guacamole web | 127.0.0.1:8080 | Chỉ mở local; người ngoài đi qua tunnel |

Các thư mục runtime\, deploy-local\data\ và deploy-local\secrets\*.txt bị Git
ignore. Clone repository mới sẽ không có VM, database, password, ISO hoặc
cloudflared; phải khôi phục các dữ liệu này từ bản sao trên H:.

## 3. Script và trách nhiệm

Tất cả lệnh dưới đây chạy từ
H:\RemoteWorkspaces\guacamole-client\deploy-local.

| Script | Chức năng |
|---|---|
| recover-after-rollback.ps1 | Entrypoint chuẩn cho start/status/stop toàn bộ stack |
| ..\START-REMOTE.cmd | Double-click để mở menu start/stop/status cho toàn bộ stack |
| start-local.ps1 | Đăng ký WSL từ ext4.vhdx, giữ WSL sống, bật systemd/Docker, gọi Guacamole và Ubuntu QEMU |
| guacamole.ps1 | Tạo secret/schema lần đầu và quản lý Compose |
| start-quick-tunnel.ps1 | Start/status/stop Cloudflare Quick Tunnel |
| export-maintenance-bundle.ps1 | Đóng gói script triển khai và tài liệu để restore sau khi clone lại fork này |
| vm-windows11\windows11.ps1 | Tạo/chạy/dừng/status/đánh dấu hoàn tất Windows 11 |
| vm-windows11\test-rdp.ps1 | Kiểm tra port forward và xác thực Windows RDP |
| vm-demo\vm-demo.ps1 | Chuẩn bị/chạy/dừng Ubuntu VMware demo |
| vm-demo\qemu-demo.ps1 | Ubuntu QEMU fallback trong WSL |
| vm-demo\test-rdp.ps1 | Kiểm tra RDP Ubuntu demo |

Script tự làm các bước trong WSL và Docker. Việc cần quyền Administrator,
BIOS hoặc Windows GUI phải làm thủ công như ghi rõ bên dưới.

## 4. Cài từ máy Windows gần như trống

### 4.1. BIOS và Windows virtualization

Làm thủ công trước khi chạy WSL:

1. Vào UEFI/BIOS.
2. Bật Intel VT-x/VMX hoặc AMD SVM/AMD-V.
3. Lưu và khởi động lại Windows.

Mở PowerShell bằng Run as Administrator:

    dism.exe /online /enable-feature /featurename:Microsoft-Windows-Subsystem-Linux /all /norestart
    dism.exe /online /enable-feature /featurename:VirtualMachinePlatform /all /norestart
    dism.exe /online /enable-feature /featurename:Microsoft-Hyper-V /all /norestart

Microsoft-Hyper-V có thể không có trên một số edition Windows. Hai feature
WSL và VirtualMachinePlatform là phần bắt buộc. Khởi động lại nếu DISM yêu cầu.

Kiểm tra sau reboot:

    wsl.exe --status
    wsl.exe --version
    systeminfo.exe

Nếu systeminfo báo hypervisor đã chạy và WSL có version 2 là đủ phần host.
QEMU trong WSL còn cần /dev/kvm, kiểm tra sau khi có Ubuntu.

Kiểm tra phase: DISM phải kết thúc không lỗi và sau reboot `wsl.exe --status`
phải trả thông tin WSL2. Checkpoint: nếu virtualization chưa bật, dừng tại
đây; chưa có dữ liệu H: nào bị thay đổi nên chỉ cần sửa BIOS/Windows feature.

### 4.2. Chuẩn bị Ubuntu bootstrap, chưa tạo runtime repo

Nếu đã có đúng distribution và disk tại
H:\RemoteWorkspaces\guacamole-client\runtime\ubuntu\ext4.vhdx, giữ nguyên và
chuyển sang mục 4.3. Không unregister hoặc import lại disk hiện có.

Với máy trống, cài Ubuntu tạm để tạo rootfs export. Ở bước này chưa được tạo
H:\RemoteWorkspaces\guacamole-client\runtime vì repository chưa được clone:

    wsl.exe --install -d Ubuntu-24.04

Khởi động lại nếu được yêu cầu, hoàn tất user ban đầu, rồi export ra một file
ngoài target repository:

    New-Item -ItemType Directory -Force H:\RemoteWorkspaces | Out-Null
    wsl.exe --export Ubuntu-24.04 H:\RemoteWorkspaces\ubuntu-24.04-rootfs.tar
    Get-Item H:\RemoteWorkspaces\ubuntu-24.04-rootfs.tar
    wsl.exe --unregister Ubuntu-24.04

wsl --unregister xóa bản đăng ký hiện tại; chỉ chạy sau khi export thành công.
Giữ rootfs tar trên H: cho tới khi import vào repo và boot thành công.

Checkpoint: nếu export hoặc unregister lỗi, dừng ở đây; không tạo repo runtime và
không tiếp tục import bằng file chưa kiểm tra.

### 4.3. Clone repository và khôi phục toolkit

Repository public của deployment này là fork có track đầy đủ `deploy-local`:
https://github.com/TimTini/guacamole-client.git. Apache Guacamole upstream
https://github.com/apache/guacamole-client.git vẫn là remote nguồn để đối chiếu
và giữ attribution, nhưng không phải nơi chứa lớp triển khai local này. Không
tạo runtime trước rồi clone vào đó, và không dùng clone upstream làm checkout
vận hành nếu chưa đưa các file `deploy-local` của fork vào.

Nếu có bản sao đầy đủ của deployment, khôi phục nguyên thư mục đó trước để giữ
runtime, database disk và secrets.
Clone fork vào thư mục hoàn toàn trống:

    New-Item -ItemType Directory -Force H:\RemoteWorkspaces | Out-Null
    git clone https://github.com/TimTini/guacamole-client.git H:\RemoteWorkspaces\guacamole-client
    Set-Location H:\RemoteWorkspaces\guacamole-client
    git status --short
    Test-Path .\deploy-local\recover-after-rollback.ps1

Nếu checkout cần phục hồi từ maintenance bundle, giải nén bundle sau khi clone
fork và kiểm tra lại file trước khi tạo runtime:

    $bundlePath = Get-ChildItem H:\Restore\guacamole-maintenance-*.zip | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($null -eq $bundlePath) { throw 'Copy a maintenance bundle to H:\Restore first.' }
    Expand-Archive -LiteralPath $bundlePath.FullName -DestinationPath H:\RemoteWorkspaces\guacamole-client -Force
    Test-Path .\deploy-local\recover-after-rollback.ps1

`H:\Restore` là thư mục chứa bundle được copy từ bản backup; không phải thư
mục mới tự sinh trong repository. Bundle được tạo bằng
export-maintenance-bundle.ps1 ở phần 10. Bundle chỉ chứa script, compose,
template và tài liệu; ISO, runtime, database, data và secret phải khôi phục
riêng từ backup H:.

Kiểm tra phase: `deploy-local\recover-after-rollback.ps1` phải tồn tại trước
khi sang bước runtime. Checkpoint: nếu clone hỏng, chỉ xóa
thư mục clone chưa có runtime; không đụng runtime hoặc secret của bản đang chạy.

### 4.4. Import Ubuntu WSL vào runtime trên H:

Sau khi deploy-local đã được khôi phục, tạo disk WSL đúng nơi mà script
start-local.ps1 kiểm tra. Nếu ext4.vhdx đã tồn tại, chỉ đăng ký/kiểm tra nó;
không import đè:

    New-Item -ItemType Directory -Force H:\RemoteWorkspaces\guacamole-client\runtime\ubuntu | Out-Null
    if (-not (Test-Path H:\RemoteWorkspaces\guacamole-client\runtime\ubuntu\ext4.vhdx)) {
        if (-not (Test-Path H:\RemoteWorkspaces\ubuntu-24.04-rootfs.tar)) { throw 'The Ubuntu rootfs export is missing.' }
        wsl.exe --import Ubuntu-24.04 H:\RemoteWorkspaces\guacamole-client\runtime\ubuntu H:\RemoteWorkspaces\ubuntu-24.04-rootfs.tar --version 2
    } else {
        $registered = @(& wsl.exe --list --quiet 2>$null | ForEach-Object { $_.ToString() -replace "`0", '' } | ForEach-Object { $_.Trim() }) -contains 'Ubuntu-24.04'
        if (-not $registered) { wsl.exe --import-in-place Ubuntu-24.04 H:\RemoteWorkspaces\guacamole-client\runtime\ubuntu\ext4.vhdx }
        Write-Host 'Existing H:-backed ext4.vhdx found; import-in-place used only when registration was missing.'
    }

Xác nhận disk và boot:

    wsl.exe --list --verbose
    Get-Item H:\RemoteWorkspaces\guacamole-client\runtime\ubuntu\ext4.vhdx
    wsl.exe -d Ubuntu-24.04 -u root -- sh -lc "df -h /; test -e /dev/kvm"

Bật systemd bên trong Ubuntu:

    wsl.exe -d Ubuntu-24.04 -u root -- sh -lc "if grep -q '^systemd=true' /etc/wsl.conf 2>/dev/null; then true; else printf '[boot]\nsystemd=true\n' > /etc/wsl.conf; fi"
    wsl.exe --shutdown
    wsl.exe -d Ubuntu-24.04 -u root -- sh -lc "readlink -f /proc/1/exe; systemctl is-system-running"

Expected: ext4.vhdx tồn tại trên H:, PID 1 là systemd và trạng thái là running
hoặc degraded.

Checkpoint: giữ rootfs tar và ext4.vhdx cho tới khi lệnh trên chạy được. Nếu
import lỗi, không unregister lần nữa; sửa đường dẫn hoặc WSL rồi thử lại từ
bản export đã giữ trên H:.

### 4.5. Cài package trong Ubuntu WSL

Đây là prerequisite; script không tự cài apt package:

    wsl.exe -d Ubuntu-24.04 -u root -- apt-get update
    wsl.exe -d Ubuntu-24.04 -u root -- apt-get install -y docker.io docker-compose-v2 qemu-system-x86 qemu-utils ovmf swtpm swtpm-tools genisoimage wimtools vncsnapshot freerdp3-x11 python3
    wsl.exe -d Ubuntu-24.04 -u root -- systemctl enable --now docker

Kiểm tra đầy đủ:

    wsl.exe -d Ubuntu-24.04 -u root -- sh -lc "docker info >/dev/null && docker compose version && command -v qemu-system-x86_64 && command -v qemu-img && command -v swtpm && command -v genisoimage && command -v wimlib-imagex && command -v vncsnapshot && command -v xfreerdp3 && test -e /dev/kvm"

Expected: exit code 0. Nếu package freerdp3-x11 không có, cài gói FreeRDP 3
tương ứng của bản Ubuntu và xác nhận binary thực tế là xfreerdp3.

Checkpoint: khi các command đã tồn tại, apt có thể chạy lại an toàn. Nếu Docker
chưa ready, chỉ sửa package/service trong WSL; không xóa ext4.vhdx.

### 4.6. Tạo thư mục runtime và đặt asset

    New-Item -ItemType Directory -Force H:\RemoteWorkspaces\guacamole-client\runtime\ubuntu, H:\RemoteWorkspaces\guacamole-client\runtime\iso, H:\RemoteWorkspaces\guacamole-client\runtime\cloudflared | Out-Null

Kiểm tra phase: runtime nằm dưới repository H: và ext4.vhdx vẫn là file vừa
import. Checkpoint: nếu path sai, dừng trước khi copy ISO hoặc tạo VM state.

### 4.7. ISO Windows 11

Đặt ISO tại:

    H:\RemoteWorkspaces\guacamole-client\runtime\iso\Windows11_23H2_UEFI.iso

Kiểm tra file và lưu hash:

    Get-Item H:\RemoteWorkspaces\guacamole-client\runtime\iso\Windows11_23H2_UEFI.iso
    Get-FileHash H:\RemoteWorkspaces\guacamole-client\runtime\iso\Windows11_23H2_UEFI.iso -Algorithm SHA256

Script chọn image index 6, Windows 11 Pro en-US 23H2. Kiểm tra trước khi cài:

    wsl.exe -d Ubuntu-24.04 -u root -- sh -lc "mkdir -p /tmp/guac-iso-check && mount -o loop,ro /mnt/h/RemoteWorkspaces/guacamole-client/runtime/iso/Windows11_23H2_UEFI.iso /tmp/guac-iso-check && wimlib-imagex info /tmp/guac-iso-check/sources/install.esd; umount /tmp/guac-iso-check"

Expected: index 6 là Windows 11 Pro. Nếu ISO dùng install.wim thay vì install.esd,
hoặc index 6 là edition khác, dừng và cập nhật answer index có kiểm chứng.
Đây là giới hạn của unattended script hiện tại.

Checkpoint: giữ nguyên ISO và hash đã ghi. Không sửa install-finished.marker,
qcow2 hoặc Autounattend để ép chạy với ISO khác.

Profile mặc định của script: 4 vCPU, 8 GiB RAM, sparse qcow2 virtual size 100 GiB,
UEFI OVMF Secure Boot, software TPM 2.0, e1000e NIC, VNC 5901, RDP
3391 -> 3389. Disk qcow2 nằm trong /var/lib/guacamole-vm-windows11 bên trong
ext4.vhdx.

### 4.8. Cloudflare Quick Tunnel

Đặt binary repo-local tại
H:\RemoteWorkspaces\guacamole-client\runtime\cloudflared\cloudflared.exe:

    New-Item -ItemType Directory -Force H:\RemoteWorkspaces\guacamole-client\runtime\cloudflared | Out-Null
    Invoke-WebRequest -Uri https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe -OutFile H:\RemoteWorkspaces\guacamole-client\runtime\cloudflared\cloudflared.exe
    & H:\RemoteWorkspaces\guacamole-client\runtime\cloudflared\cloudflared.exe version
    Get-FileHash H:\RemoteWorkspaces\guacamole-client\runtime\cloudflared\cloudflared.exe -Algorithm SHA256

Khi có checksum chính thức, đối chiếu hash trước khi dùng. Quick Tunnel không
có URL cố định, không có account tunnel và không thay thế Guacamole login.

Kiểm tra phase: lệnh version phải chạy được từ binary trên H:. Checkpoint: nếu
binary lỗi hoặc sai kiến trúc, giữ binary cũ (nếu có), không cài cloudflared
global vào C:.

### 4.9. Khởi tạo Compose/database

    Set-Location H:\RemoteWorkspaces\guacamole-client\deploy-local
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\guacamole.ps1 start
    wsl.exe -d Ubuntu-24.04 -u root -- sh -lc "cd /mnt/h/RemoteWorkspaces/guacamole-client/deploy-local && docker compose --file compose.yaml ps"
    Invoke-WebRequest http://127.0.0.1:8080/guacamole/ -UseBasicParsing | Select-Object StatusCode

Expected: postgres healthy, guacd running, guacamole running, HTTP 200. Lần
đầu database dùng guacadmin / guacadmin; đổi ngay sau login. Database cũ giữ
nguyên accounts/connections.

Checkpoint: volume đã có, postgres_password.txt có ACL giới hạn, HTTP local 200.

Nếu phase này lỗi, chỉ đọc docker compose logs và giữ nguyên volume/secret; có
thể chạy lại guacamole.ps1 start sau khi sửa prerequisite.

### 4.10. Start recovery entrypoint

    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\recover-after-rollback.ps1 start
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\recover-after-rollback.ps1 status

Entrypoint gọi WSL/disk, keepalive, systemd, Docker, Compose, Ubuntu QEMU,
Windows QEMU và sau đó Quick Tunnel. Đường dẫn `start` yêu cầu asset
`runtime\cloudflared\cloudflared.exe`; nếu chỉ cần truy cập local, start
`start-local.ps1` và các action libvirt riêng (`network`, `storage`,
`connect-guacamole`, `start`) rồi bỏ qua `start-quick-tunnel.ps1`. Không chuyển
dữ liệu sang C:.

Kiểm tra phase: status phải cho thấy WSL systemd, Docker, Compose, QEMU và
tunnel theo đúng asset đang có. Checkpoint: nếu start dừng giữa chừng, chạy
status và log trước; không chạy dọn dẹp hoặc tạo lại volume.

### 4.11. Windows unattended lifecycle

Lần đầu tạo disk qcow2, UEFI variables, TPM state, Autounattend.xml và answer
ISO:

    Set-Location H:\RemoteWorkspaces\guacamole-client\deploy-local\vm-windows11
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\windows11.ps1 start
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\windows11.ps1 status

Installer dùng VNC 172.18.0.1:5901. Script gửi phím cho ISO boot prompt. Nếu
QEMU dừng ở UEFI Boot Manager, chọn ISO hoặc Windows Boot Manager qua VNC một
lần. Chờ OOBE/desktop, rồi test:

    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\test-rdp.ps1

Chỉ khi kết quả là WIN11_RDP_AUTH_OK mới đánh dấu hoàn tất:

    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\windows11.ps1 stop
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\windows11.ps1 finish-install
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\windows11.ps1 start
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\test-rdp.ps1

Không gọi finish-install khi RDP chưa xác thực. Marker làm các lần sau boot disk
mà không gắn ISO.

windows11.ps1 stop gửi ACPI system_powerdown qua QEMU monitor và chờ tối đa 120
giây để Windows tự shutdown. Chỉ khi guest không dừng mới gọi systemctl stop
và in warning; có thể đổi giới hạn bằng:

    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\windows11.ps1 stop -GracefulStopTimeoutSeconds 180

Checkpoint: trước stop/finish-install phải giữ VNC connection và các file
OVMF/TPM. Nếu installer lỗi, dừng VM, giữ qcow2 để điều tra; chỉ cài lại từ
đầu khi đã sao lưu state và xác nhận muốn mất dữ liệu guest.

## 5. Tạo connection trong Guacamole

Đăng nhập user quản trị, vào Settings, Connections, New Connection.

Windows RDP:

- Protocol: RDP
- Hostname: 172.18.0.1 hoặc gateway thật của Docker network
- Port: 3391
- Username: guacadmin
- Username/password: initialize the canonical root-only secret at
  `/var/lib/guacamole-workspace/secrets/windows11_guacadmin_password` inside
  the Ubuntu ext4 VHDX with `initialize-windows-auth.ps1`; managed sync
  writes them only to the connection parameter table.

The installer verifies that `/var/lib/guacamole-workspace/secrets` is a regular
directory reported as `root:root:0700`. This directory is inside
`H:\RemoteWorkspaces\guacamole-client\runtime\ubuntu\ext4.vhdx`; keeping the
secret in that POSIX filesystem preserves root-only ownership and modes. A
`/mnt/h` DrvFs path is never used for
the credential. If the directory reports another owner, mode, or type,
installation stops before release publication and the password must remain
uninitialized.
- Bật bỏ qua certificate nếu dùng certificate tự ký.

Windows VNC cài đặt/cứu hộ:

- Protocol: VNC
- Hostname: 172.18.0.1
- Port: 5901

Ubuntu QEMU fallback:

- Protocol: RDP
- Hostname: 172.18.0.1
- Port: 3390
- Username: ubuntu
- Password: đọc cục bộ từ runtime\vm-demo\vm-password.txt

Nếu gateway khác:

    wsl.exe -d Ubuntu-24.04 -u root -- docker network inspect guacamole-local_default

Không ghi password vào connection description. Cấp quyền READ cho user dùng
remote sau khi tạo connection.

### Ubuntu demo optional

Ubuntu demo là VM riêng, không phải Windows VM. VMware mode cần:

- VMware Workstation và đường dẫn hợp lệ tới `vmrun.exe`;
- source image tại `runtime\vm-demo\ubuntu-24.04-cloud.img`;
- bridged network và DHCP nếu cần IP LAN;
- trong WSL: `qemu-img`, `cloud-localds` và `openssl`.

Cài prerequisite cho demo nếu thiếu:

    wsl.exe -d Ubuntu-24.04 -u root -- apt-get update
    wsl.exe -d Ubuntu-24.04 -u root -- apt-get install -y qemu-utils cloud-image-utils openssl

Script đối chiếu SHA-256 source image với giá trị hiện tại
612b2c0cc1bc413a6cb8c38fd611794caf0f2b436c50013d8b3794db12ad7354.
Chuẩn bị và chạy VMware:

    Set-Location H:\RemoteWorkspaces\guacamole-client\deploy-local\vm-demo
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\vm-demo.ps1 prepare
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\vm-demo.ps1 start
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\vm-demo.ps1 status
    & '<VMWARE_INSTALL>\vmrun.exe' -T ws getGuestIPAddress '<REPO_ROOT>\runtime\vm-demo\ubuntu-24.04-guacamole-demo.vmx' -wait

VMware mode tạo VMDK, NoCloud seed, VMX và password trong `runtime\vm-demo`.
Sau khi cloud-init cài XFCE/xrdp xong, dùng connection RDP host
IP LAN, port 3389, user ubuntu và password trong vm-password.txt. Test:

    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\test-rdp.ps1

Nếu VMware không chạy được, dùng QEMU fallback; fallback giữ
overlay trong WSL ext4 và forward RDP port 3390:

    Set-Location H:\RemoteWorkspaces\guacamole-client\deploy-local\vm-demo
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\qemu-demo.ps1 start
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\qemu-demo.ps1 status
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\test-rdp.ps1

QEMU fallback cần `/dev/kvm`, seed.iso, source image và password đã chuẩn bị.
Log là runtime\vm-demo\qemu-console.log. Dừng VMware bằng vm-demo.ps1 stop,
dừng QEMU bằng qemu-demo.ps1 stop; không xóa overlay khi còn cần VM.

## 6. Vận hành hằng ngày

    Set-Location H:\RemoteWorkspaces\guacamole-client\deploy-local
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\recover-after-rollback.ps1 start
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\recover-after-rollback.ps1 status
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\recover-after-rollback.ps1 stop

Stop giữ database volume và disk. Kiểm tra riêng:

    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\guacamole.ps1 status
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\start-quick-tunnel.ps1 status
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\vm-windows11\windows11.ps1 status
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\vm-windows11\test-rdp.ps1
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\vm-demo\qemu-demo.ps1 status

Quick Tunnel:

    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\start-quick-tunnel.ps1 start
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\start-quick-tunnel.ps1 status
    Get-Content .\..\runtime\cloudflared\quick-tunnel.stderr.log -Tail 50

URL có thể đổi sau mỗi restart. Chỉ chia sẻ sau khi local Guacamole và login
đã được kiểm tra.

## 7. Chẩn đoán read-only

    git status --short
    wsl.exe --list --verbose
    wsl.exe -d Ubuntu-24.04 -u root -- systemctl is-system-running
    wsl.exe -d Ubuntu-24.04 -u root -- docker info
    wsl.exe -d Ubuntu-24.04 -u root -- sh -lc "cd /mnt/h/RemoteWorkspaces/guacamole-client/deploy-local && docker compose --file compose.yaml ps"
    wsl.exe -d Ubuntu-24.04 -u root -- docker volume inspect guacamole-local-postgres-data
    wsl.exe -d Ubuntu-24.04 -u root -- df -h /
    wsl.exe -d Ubuntu-24.04 -u root -- ss -ltnp
    Test-NetConnection 127.0.0.1 -Port 8080
    Test-NetConnection 127.0.0.1 -Port 3390
    Test-NetConnection 127.0.0.1 -Port 3391
    Test-NetConnection 127.0.0.1 -Port 5901

Log:

    wsl.exe -d Ubuntu-24.04 -u root -- journalctl -u guacamole-vm-windows11 --no-pager -n 100
    wsl.exe -d Ubuntu-24.04 -u root -- journalctl -u guacamole-vm-windows11-tpm --no-pager -n 100
    Get-Content .\..\runtime\vm-windows11\qemu-console.log -Tail 100
    Get-Content .\..\runtime\vm-demo\qemu-console.log -Tail 100
    Get-Content .\..\runtime\cloudflared\quick-tunnel.stderr.log -Tail 100
    wsl.exe -d Ubuntu-24.04 -u root -- sh -lc "cd /mnt/h/RemoteWorkspaces/guacamole-client/deploy-local && docker compose logs --tail=100 guacamole guacd postgres"

3390, 3391 và 5901 chỉ có listener khi VM tương ứng chạy. 8080 chỉ cần cho
Guacamole local.

## 8. Phục hồi WSL và dịch vụ

Không xóa hoặc tạo lại runtime đang có. Kiểm tra:

    Test-Path H:\RemoteWorkspaces\guacamole-client\runtime\ubuntu\ext4.vhdx
    Test-Path H:\RemoteWorkspaces\guacamole-client\runtime\iso\Windows11_23H2_UEFI.iso
    Test-Path H:\RemoteWorkspaces\guacamole-client\deploy-local\secrets\postgres_password.txt
    wsl.exe -d Ubuntu-24.04 -u root -- stat -c '%U:%G:%a:%F' /var/lib/guacamole-workspace/secrets

Chạy entrypoint phục hồi:

    Set-Location H:\RemoteWorkspaces\guacamole-client\deploy-local
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\recover-after-rollback.ps1 start
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\recover-after-rollback.ps1 status

Nếu distribution mất đăng ký, script import in place disk H:. Nếu WSL có nhưng
Docker mất, cài lại trong chính Ubuntu-24.04. Không tạo distribution mới và
không thay ext4.vhdx.

Nếu volume còn nhưng postgres_password.txt mất, script phải dừng. Phục hồi
đúng secret từ backup rồi chạy lại; không tạo password mới.

Nếu /dev/kvm mất, kiểm tra BIOS/Virtual Machine Platform/WSL. Quick Tunnel
thường nhận URL mới; đọc bằng start-quick-tunnel.ps1 status.

## 9. Lỗi thường gặp

| Triệu chứng | Kiểm tra | Xử lý |
|---|---|---|
| wsl.exe was not found | Get-Command wsl.exe | Sửa/cài WSL; không sửa H: |
| Ubuntu-24.04 không đăng ký | wsl --list --verbose, ext4.vhdx | Dùng recovery start để import |
| systemd is not running | readlink /proc/1/exe, systemctl | Bật systemd, wsl --shutdown |
| Docker không ready | systemctl status docker, docker info | enable --now docker, cài lại apt |
| Compose không có container | guacamole.ps1 status, docker compose ps | Xem log; không down -v |
| Port 8080 bị chiếm | Get-NetTCPConnection -LocalPort 8080 | Xác định process trước khi dừng |
| Database lỗi password | log postgres/guacamole, secret tồn tại | Dùng secret cũ; không sinh mới |
| Windows không có VNC 5901 | status, journal QEMU/TPM | Kiểm tra /dev/kvm, TPM, console log |
| Windows RDP CONNECTION_FAILED | status, Test-NetConnection 3391 | Chờ boot/OOBE, xem VNC, chưa finish-install |
| Windows RDP AUTH_FAILED | canonical H secret metadata and managed connection parameters | Reinitialize through the root helper; do not print or copy the password |
| Guacamole không nối được nhưng host port OK | docker network inspect | Dùng gateway thật, thường 172.18.0.1 |
| TPM lock/address in use | systemctl TPM, ss -lx | Đảm bảo VM dừng graceful hoặc đã timeout; không xóa TPM đang dùng |
| VM tạo mới lỗi `swtpm_setup` hoặc `.lock.swtpm-localca` | `stat -c '%U:%G' /var/lib/swtpm-localca` | Chạy `powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\libvirt.ps1 install`; script sửa toàn bộ state CA về `swtpm:swtpm`, rồi đăng nhập lại Cockpit |
| Installer đứng boot menu | VNC 5901, qemu-console.log | Chọn ISO/Windows Boot Manager thủ công |
| Quick Tunnel không có URL | status, quick-tunnel.stderr.log | Kiểm tra binary/mạng, stop/start |
| H: đầy | Get-PSDrive H, df -h, qemu-img info | Dọn log/backup; không xóa disk/volume |
| ISO sai edition | wimlib-imagex info install.esd | Dừng, đổi ISO/answer index có kiểm chứng |

Lệnh xem service:

    wsl.exe -d Ubuntu-24.04 -u root -- systemctl status guacamole-vm-windows11 --no-pager
    wsl.exe -d Ubuntu-24.04 -u root -- systemctl status guacamole-vm-windows11-tpm --no-pager
    wsl.exe -d Ubuntu-24.04 -u root -- journalctl -u guacamole-vm-windows11 -b --no-pager

## 10. Sao lưu trước bảo trì

### Maintenance bundle

Tạo bundle chứa các file local cần để cài lại toolkit. Script chỉ lấy script,
compose, template và docs trong deploy-local; loại trừ deploy-local\data,
deploy-local\secrets, runtime và các file có tên password/secret/token.

    Set-Location H:\RemoteWorkspaces\guacamole-client\deploy-local
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\export-maintenance-bundle.ps1
    Get-ChildItem .\..\runtime\backups\guacamole-maintenance-*.zip | Sort-Object LastWriteTime -Descending | Select-Object -First 1

Script tự mở lại ZIP để kiểm tra path cấm. Nếu thấy lỗi, không dùng bundle đó;
kiểm tra `deploy-local` và chạy lại. Copy bundle sang kho backup riêng trên H:
hoặc ổ khác trước khi rollback. Không coi bundle là backup database/VM.

### Database

Dump trên H: qua Compose, không in password:

    $backupDir = 'H:\RemoteWorkspaces\guacamole-client\runtime\backups'
    New-Item -ItemType Directory -Force $backupDir | Out-Null
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $backupPath = Join-Path $backupDir "guacamole-db-$stamp.sql"
    Set-Location H:\RemoteWorkspaces\guacamole-client\deploy-local
    $dumpCommand = 'cd /mnt/h/RemoteWorkspaces/guacamole-client/deploy-local && docker compose exec -T postgres sh -lc ''PGPASSWORD="$(cat /run/secrets/postgres_password)" pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB"'''
    wsl.exe -d Ubuntu-24.04 -u root -- sh -lc $dumpCommand | Set-Content -LiteralPath $backupPath -Encoding utf8
    Get-Item -LiteralPath $backupPath

Kiểm tra dump có SQL trước cập nhật. Không xóa volume để sửa lỗi.

### VM và secret

Giữ bản sao của:

- runtime\ubuntu\ext4.vhdx;
- deploy-local\secrets\postgres_password.txt;
- `/var/lib/guacamole-workspace/secrets/windows11_guacadmin_password` inside
  `runtime\ubuntu\ext4.vhdx` (root-only, outside maintenance bundles);
- runtime\vm-windows11\OVMF_VARS_4M.ms.fd và thư mục tpm;
- Windows ISO và Ubuntu source image;
- runtime\vm-demo\vm-password.txt nếu dùng Ubuntu demo.

Secret chỉ lưu trong kho backup có quyền giới hạn.

## 11. Cập nhật

### Compose/Guacamole

1. Chạy status và backup database.
2. Đọc release notes phiên bản mới.
3. Nâng guacamole và guacd cùng phiên bản; giữ PostgreSQL volume.
4. Pull và recreate container, không xóa volume:

    Set-Location H:\RemoteWorkspaces\guacamole-client\deploy-local
    wsl.exe -d Ubuntu-24.04 -u root -- sh -lc "cd /mnt/h/RemoteWorkspaces/guacamole-client/deploy-local && docker compose pull && docker compose up -d"
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\guacamole.ps1 status

001-guacamole.sql chỉ chạy khi volume mới. Database hiện có phải nâng cấp theo
migration/release notes; không ghi đè init SQL hoặc tạo volume mới.

### Script

    Set-Location H:\RemoteWorkspaces\guacamole-client
    git status --short
    git diff -- .gitignore deploy-local
    git pull --ff-only
    Get-ChildItem .\deploy-local -Filter *.ps1 -Recurse | ForEach-Object {
        $errors = $null
        [System.Management.Automation.Language.Parser]::ParseFile($_.FullName, [ref]$null, [ref]$errors) | Out-Null
        if ($errors) { throw "Syntax error in $($_.FullName)" }
    }

Sau đó chạy recovery status, local HTTP và RDP test. Không gọi finish-install
trên VM đã cài và không xóa runtime chỉ vì script cập nhật.

Nếu git status còn thay đổi do máy hiện tại, dừng trước khi pull và lưu patch
hoặc commit theo quy trình của repository. Không dùng git reset --hard để làm
sạch; source trong deploy-local đã được track, còn runtime, deploy-local\data,
deploy-local\secrets và generated state bị ignore nên không được xem là được
bảo vệ bởi Git.

### Cloudflared

    Set-Location H:\RemoteWorkspaces\guacamole-client\deploy-local
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\start-quick-tunnel.ps1 stop
    & .\..\runtime\cloudflared\cloudflared.exe version
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\start-quick-tunnel.ps1 start

URL thay đổi sau restart; kiểm tra HTTP 200 trước khi gửi URL mới.

### QEMU/Windows guest

    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\vm-windows11\windows11.ps1 stop
    wsl.exe -d Ubuntu-24.04 -u root -- apt-get update
    wsl.exe -d Ubuntu-24.04 -u root -- apt-get install -y qemu-system-x86 qemu-utils ovmf swtpm swtpm-tools genisoimage wimtools vncsnapshot freerdp3-x11
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\vm-windows11\windows11.ps1 start

Windows Update chạy từ bên trong Windows 11. Không thay qcow2, UEFI vars hoặc
TPM state bằng file mới nếu mục tiêu là giữ nguyên VM.

## 12. Checklist bàn giao và smoke test

1. Xác nhận cwd là H:\RemoteWorkspaces\guacamole-client\deploy-local.
2. Đọc root `..\README.md`, `README.md`, `RUNBOOK.md` và README của từng VM.
3. Chạy git status --short và recovery status.
4. Kiểm tra ext4.vhdx, secret, ISO, package và cloudflared.
5. Xem Compose status, QEMU status, port và log.
6. Khi sửa, giữ nguyên đường dẫn H: và không xóa state để cài lại.
7. Chạy parser check, status và smoke test phù hợp.
8. Báo rõ đã kiểm chứng source, build, local HTTP, RDP hay public end-to-end.

Smoke test:

    Set-Location H:\RemoteWorkspaces\guacamole-client\deploy-local
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\recover-after-rollback.ps1 status
    Invoke-WebRequest http://127.0.0.1:8080/guacamole/ -UseBasicParsing | Select-Object StatusCode
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\vm-windows11\test-rdp.ps1
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\start-quick-tunnel.ps1 status

WIN11_RDP_AUTH_OK chứng minh port forward và credentials RDP đã xác thực. Để
xác nhận trải nghiệm người dùng, vẫn phải mở connection trong Guacamole qua
local URL hoặc Quick Tunnel.
## Cockpit Machines, new VMs, and assignment

1. Open `https://127.0.0.1:9090` on the Windows host and accept the local
   self-signed certificate once. Never publish port 9090 through Cloudflare.
2. Sign in as the host administrator, open **Virtual Machines** using the
   system connection, and choose **Create VM**.
3. Select an ISO under
   `/mnt/h/RemoteWorkspaces/guacamole-client/runtime/iso/` and record
   `Get-FileHash <ISO> -Algorithm SHA256` before use.
4. Choose UEFI plus TPM 2.0 for Windows 11, pool `guacamole-vms`, and network
   `guac-nat`. Do not allocate more CPU/RAM than the host can sustain.
5. Use a unique MAC and add a DHCP reservation before relying on a stable IP.
   The CLI fallback `new-libvirt-vm.ps1` performs these checks and leaves the
   new domain shut off for inspection.
6. Install Windows, enable RDP and its firewall rule, then test from WSL and
   from `guacamole-local_default` to the reserved IP on port 3389.
7. In Guacamole, create the RDP connection and assign it under **Settings →
   Users/Groups → Permissions**. Record name/MAC/IP/ISO hash/assignees under H
   without recording the guest password.

Cockpit controls VM hardware and lifecycle. Guacamole controls which end
users can see and open each machine.

If **Virtual Machines** reports `Virtualization service (libvirt) is not active`
while `virsh -c qemu:///system list --all` still works, reload the D-Bus policy
installed by `libvirt-dbus`, then sign out of Cockpit and sign in again:

    wsl.exe -d Ubuntu-24.04 -u root -- systemctl reload dbus
    wsl.exe -d Ubuntu-24.04 -u root -- busctl call org.libvirt /org/libvirt/QEMU org.libvirt.Connect ListDomains u 0

The second command must return an `ao` array. `libvirt.ps1 install` now performs
this reload and probe automatically.

## Golden template operation and recovery contract

### Create and verify a template

Use **Workspace Templates** in Cockpit for the normal flow. Select the
`windows11-v1` entry, enter a unique workspace name, and choose one existing
Guacamole `USER` or `USER_GROUP` assignee. The UI uses only the allowlisted
helper arguments and keeps Cockpit on `https://127.0.0.1:9090`.

Workspace creation is submitted through the helper's `start` command. It
launches a deterministic, root-owned transient unit named
`guacamole-workspace-<name>.service` with an argv vector; no shell, browser
stream, credential, or raw subprocess output is part of the job. The helper
stores bounded status documents under
`/var/lib/guacamole-workspaces/jobs/` using atomic replacement and mode `0600`.
The Recent workspaces table reads those documents through `list --json`, polls
queued/running jobs, and survives F5 or Cockpit WebSocket disconnects.

If an older clone was left in `pending`, `waiting-rdp`, or `sync-failed`, use
the row's **Repair / resume** action or run:

    wsl.exe -d Ubuntu-24.04 -u root -- /usr/local/libexec/guacamole-workspace-helper repair --name <workspace> --json

Repair is idempotent and ownership-scoped. A repeated start or repair for the
same name returns the existing job/record instead of calling clone again.
The repair worker re-checks the live domain UUID and managed XML, overlay and
template backing chain, NVRAM/TPM ownership markers, MAC, DHCP lease, and RDP
port before any Guacamole operation. It accepts an existing Guacamole row only
when the durable `syncAttemptId`, assignee marker, and exact connection ID
match; a foreign same-name row is a conflict and is never retargeted. An RDP
failure remains `waiting-rdp` and performs no Guacamole write.

The final repair gate repeats the immutable template record check immediately
before the Guacamole sync: the canonical image path must still be a regular
file with exact mode `0444`, its SHA-256 must match inventory, and the live
qcow2 backing chain must still contain that path. Status reads and writes use
the root-owned `0750` job directory and `0600` files through no-follow
directory descriptors, with a bounded read and atomic same-directory rename;
directory or file substitution is rejected. A detached repair that loses its
inventory is persisted as `failed-without-inventory` and is visible as a
non-repairable diagnostic until a read-only no-artifact proof authorizes a new
clone. Cockpit ignores stale polling responses after a reload or repair.

The inventory is authoritative in `list --json`; a ledger enriches only a
matching name/template/attempt/identity and cannot create a ready workspace
row. `queued`/`running` ledgers are reconciled against their deterministic
systemd unit and a bounded age. Missing, failed, or terminal units become
repairable `stale`/`failed` results and Cockpit stops polling. A failed job
without inventory is marked `failed-without-inventory` unless a read-only
no-artifact proof permits a safe restart; the UI action follows that API
decision.

The CLI fallback is equivalent:

    .\create-windows-template.ps1 -Source windows11 -Version windows11-v1
    .\clone-windows-vm.ps1 -Name windows-template-test-01 -AssignUser demo -TemplateVersion windows11-v1

The only valid template source is `windows11`; `windows11-02` is not a source
for this workflow. The template transaction records the source shutdown,
read-only disk check, conversion, SHA-256, mode `0444`, publish, source
restart, and RDP recovery in its transaction output. A failure before publish
removes only the current `.partial` file. A source recovery failure is a
separate error and requires the operator to inspect libvirt state before any
retry.

### One-time ownership migration for the retained v1 template

The verifier requires the golden image to be a canonical, regular, no-symlink
file owned by `root:root` with exact mode `0444`. If an older conversion left
`windows11-v1.qcow2` owned by the QEMU service account, run this fixed,
audited migration only as root:

    wsl.exe -d Ubuntu-24.04 -u root -- python3 /mnt/h/RemoteWorkspaces/guacamole-client/deploy-local/migrate-template-ownership.py

The command is intentionally fixed to `/var/lib/guacamole-templates/windows11-v1.qcow2`.
Before changing ownership it verifies the canonical no-follow path, regular
file type, exact `0444` mode, inventory path and SHA-256, no qcow2 backing, and
each dependent clone's independent disk plus read-only backing reference. It
then uses descriptor-level `fchown(0,0)` and reloads the inventory, rechecks
the owner/mode, exact content hash, no-backing state, and clone references.
It never writes inventory, changes image contents, or starts/stops/redefines a
VM. Require `TEMPLATE_OWNERSHIP_MIGRATION_OK` and the JSON before running the
installed clone WhatIf and the read-only template/status gates.

### Clone identity, access, and cleanup

Clone IP allocation skips `192.168.250.11`, `192.168.250.12`, all live and
persistent DHCP reservations, leases, and inventory records, then selects the
first free address from `192.168.250.20-249`. Every clone has its own UUID,
locally administered MAC, writable NVRAM, libvirt-managed TPM 2.0 state, and
qcow2 overlay. Managed Guacamole connections use exactly six parameter names:
`hostname`, `port`, `security`, `ignore-cert`, `username`, and `password`.
The shared Windows username/password are permitted only in the
`guacamole_connection_parameter` table. They are excluded from inventory, job
JSON, progress, logs, reports, maintenance bundles, and argv. The helper loads
them into a transaction-local temporary table through COPY stdin so the
password is not part of SQL text.

The selected user or group receives exactly `READ`; `guacadmin` receives
`READ`, `UPDATE`, `DELETE`, and `ADMINISTER`. A user sees only assigned
connections. The shared Windows `guacadmin` credential is initialized only
through the root-only secret file and is written to the managed
connection parameter table during sync. It is never placed in a script,
inventory, job status, logs, a report, a maintenance bundle, or a command
argument.

Before removing `windows-template-test-01`, save the non-secret evidence from
`test-libvirt.ps1 -Mode templates`, then remove only its domain, overlay,
NVRAM/TPM state, DHCP reservation, Guacamole connection and permissions, and
inventory record, plus the exact matching workspace ownership markers under
`/var/lib/guacamole-vms/.guacamole-workspace`, the NVRAM directory, and the
clone TPM UUID directory. Verify that `windows11-v1.qcow2` remains and is
still mode `0444`. Never remove the source VM, another domain, another user's
connection, or a template used by a remaining overlay.

Run the read-only retirement gate before changing a template:

    wsl.exe -d Ubuntu-24.04 -u root -- /usr/local/libexec/guacamole-workspace-helper check-template-delete --version windows11-v1

It joins durable workspace ownership markers with inventory dependents and
actual template backing chains, domain metadata, the helper-owned DHCP/MAC
pool, overlay, NVRAM/TPM, and Guacamole workflow state. Clone names are
user-supplied and are not treated as an ownership boundary. Exit code `0` with
`deletable:true` and an empty `blockers` list is the only clean result; an
inspection error or any blocker is a refusal. This command never deletes a
resource. The `templates` verification mode runs the same audit so an orphan
that is missing from inventory cannot produce a false clean result.

To convert a clone before retiring its template, stop the clone, run
`qemu-img convert -O qcow2` to a new independent file, run `qemu-img check`
and `sha256sum` on the new file, update the domain to that file, and verify the
old backing chain is gone before deleting the old overlay. Template retirement
is allowed only after the retirement gate reports zero blockers and both
`qemu-img info --backing-chain` and the non-secret inventory show zero
dependents.

### Sync repair, rollback ledger, and error actions

Start diagnosis with read-only commands:

    .\test-libvirt.ps1 -Mode all
    .\test-libvirt.ps1 -Mode templates
    wsl.exe -d Ubuntu-24.04 -u root -- virsh -c qemu:///system list --all
    wsl.exe -d Ubuntu-24.04 -u root -- virsh -c qemu:///system net-dumpxml guac-nat
    wsl.exe -d Ubuntu-24.04 -u root -- /usr/local/libexec/guacamole-workspace-helper list --json
    .\sync-guacamole-vms.ps1 -WhatIf

Use these safe actions for helper outcomes:

| Code | Read-only diagnosis | Safe action |
|---|---|---|
| `TEMPLATE_MISSING` / `TEMPLATE_HASH_INVALID` | Check the inventory path, mode, and `sha256sum`; inspect the template with `qemu-img info` | Do not start a clone; restore the versioned template from the H: backup or create a new version after the source is healthy |
| `RDP_NOT_READY` | Check `virsh domstate`, `net-dhcp-leases`, and TCP 3389 from `guacamole-local_default` | Keep the valid `waiting-rdp` clone for repair; do not delete the template or retry with a second name until boot is understood |
| `GUAC_CONNECTION_CONFLICT` | Query only connection name/protocol/parameter names with the fixed read-only check | Choose a new VM name or repair the existing inventory record; do not overwrite an unknown connection |
| `SYNC_COMMIT_UNKNOWN` / `SYNC_OWNERSHIP_UNAVAILABLE` | Inspect the clone's `syncAttemptId`, owned marker, and connection identity using the read-only helper | Retain the clone in `sync-failed`, run `sync --all --what-if`, and let the ownership check decide compensation; never delete by name alone |
| `ROLLBACK_FAILED` | Read the rollback ledger, domain state, DHCP XML, and inventory record | Stop automated retries; retain the repairable artifacts and repair only the resources named by the ledger |
| `LOCK_BUSY` | Check whether another helper process owns `/run/lock/guacamole-workspace-helper.lock` | Wait for the other operation to finish; do not remove the lock file while a process may hold it |
| `TPM_*` / `SOURCE_TPM_INVALID` | Inspect the dedicated systemd unit, socket, and state directory | Do not initialize or delete TPM state; fix the owner/service and re-run read-only preflight |

The helper rollback ledger is LIFO and contains only resources created by the
current attempt. It must never remove `windows11`, a template, an unrelated
domain, or a pre-existing Guacamole row. If WSL registration is lost, verify
`runtime\ubuntu\ext4.vhdx`, run `recover-after-rollback.ps1 start`, and use
`wsl.exe --import-in-place` only when the distribution is absent. Do not
unregister or recreate the existing runtime disk, Docker volume, database,
VM disk, UEFI variables, TPM state, or secret file.

## Final topology and verification

```text
Windows host
└── WSL2 Ubuntu-24.04 in runtime\ubuntu\ext4.vhdx
    ├── Docker: Guacamole / guacd / PostgreSQL -> 127.0.0.1:8080
    ├── Cockpit + libvirt qemu:///system -> 127.0.0.1:9090 only
    └── guac-nat 192.168.250.0/24 -> windows11 192.168.250.11:3389
Cloudflare Quick Tunnel -> http://127.0.0.1:8080 only
```

Run `test-libvirt.ps1 -Mode all`. Required tokens include
`COCKPIT_LOCAL_ONLY_OK`, `LIBVIRT_STATE_OK`, `WINDOWS11_RDP_ROUTE_OK`,
`GUAC_PERMISSION_INVENTORY_OK`, and `RECOVERY_OWNER_OK`. A port probe alone is
not proof of a working remote desktop; the maintenance report must separately
record a real authenticated Guacamole session, permission checks, and any
guest/manual work still unverified.

## Public release boundary

Before any public commit, treat the complete `runtime\` tree and
`deploy-local\data\` as ignored operational state. The `deploy-local\secrets\`
tree and every password, private key, environment file, VM disk, ISO, TPM/UEFI
state file, log, backup, archive, database dump, generated inventory, and
Python cache are also excluded from publication. The local
`runtime\ubuntu\ext4.vhdx` contains the WSL Docker database volume and
credentials; never publish or attach it to a commit.

Use an explicit `git add` allowlist for source, templates, tests, and docs.
Run `powershell.exe -NoProfile -ExecutionPolicy Bypass -File
..\public-release-audit.ps1` before every commit or push. The audit reports
only `file:rule` entries and always blocks `origin` pointing to the Apache
`apache/guacamole-client` repository with
`origin:UPSTREAM_ORIGIN_FORBIDDEN`, including a clean working tree. Review the
remote and publish custom changes from a repository you own.
