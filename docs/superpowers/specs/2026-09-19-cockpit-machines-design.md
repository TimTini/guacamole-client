# Cockpit Machines + libvirt: thiết kế áp dụng cho deployment local

**Ngày:** 2026-09-19
**Phạm vi:** Ubuntu-24.04 WSL2 trong Windows, QEMU/KVM, Apache Guacamole và các tài nguyên trên ổ H:
**Trạng thái:** Design spec. Tài liệu này chưa cài package, chưa migrate VM và chưa thay đổi lifecycle runtime.

## 1. Mục tiêu

Bổ sung Cockpit Machines làm giao diện quản trị VM, với libvirt là lifecycle owner duy nhất sau khi cutover. Người quản trị có thể tạo Windows 10/11 mới từ ISO, chỉnh CPU/RAM/disk, start/stop/reboot, clone và xem console từ giao diện web. Guacamole tiếp tục là cổng remote dành cho người dùng cuối, quản lý user, nhóm và quyền được dùng máy nào.

Windows 11 hiện tại phải được chuyển sang một domain libvirt persistent bằng chính disk, UEFI variables và TPM state đang có. Không cài lại Windows, không tạo lại database Guacamole và không đưa Cockpit ra Internet.

## 2. Bằng chứng trạng thái hiện tại

Đã kiểm tra trực tiếp trong checkout `H:\RemoteWorkspaces\guacamole-client`:

- `deploy-local\compose.yaml` chạy `postgres:16-alpine`, `guacamole/guacd:1.6.0` và `guacamole/guacamole:1.6.0`; web chỉ bind `127.0.0.1:8080`.
- Docker Engine và QEMU đang chạy trong distro `Ubuntu-24.04` WSL2. `/dev/kvm` có mặt.
- Windows 11 hiện do `deploy-local\vm-windows11\windows11.ps1` khởi động bằng `systemd-run` thành transient units `guacamole-vm-windows11.service` và `guacamole-vm-windows11-tpm.service`.
- QEMU hiện dùng 4 vCPU, 8 GiB RAM, UEFI OVMF, TPM 2.0, disk `/var/lib/guacamole-vm-windows11/windows11.qcow2`, NIC `e1000e`, RDP forward `3391 -> 3389` và VNC `5901`.
- Disk `/var/lib/guacamole-vm-windows11/windows11.qcow2` nằm trong filesystem của WSL; filesystem đó là `runtime\ubuntu\ext4.vhdx` trên H:.
- ISO, UEFI variables, TPM state, log và secret Windows nằm dưới `runtime\` hoặc `deploy-local\secrets\` trên H:.
- Các command `cockpit`, `cockpit-machines`, `virsh`, `libvirtd` và `virtqemud` chưa có trong distro ở thời điểm viết spec.
- Process Windows đang chạy vẫn cần được kiểm tra boot từ disk trước cutover. Không migrate khi QEMU còn gắn ISO installer hoặc khi Windows chưa bật RDP.

Đây là baseline để agent sau phân biệt rõ phần đã xác minh với phần chỉ là thiết kế.

## 3. Ràng buộc bất biến

1. Repository và mọi asset lâu dài nằm dưới `H:\RemoteWorkspaces\guacamole-client` hoặc trong `runtime\ubuntu\ext4.vhdx` của repository.
2. Không đặt database, disk VM, TPM, UEFI variables, ISO, secret hoặc log quan trọng trên C:.
3. Không chạy QEMU script và libvirt cùng lúc trên cùng qcow2 hoặc TPM state. Hai hypervisor owner đồng thời sẽ làm hỏng disk chain hoặc khóa TPM.
4. Không dùng `docker compose down -v`, không xóa volume PostgreSQL và không tạo password mới khi database cũ vẫn còn.
5. Cloudflare Quick Tunnel chỉ proxy `http://127.0.0.1:8080` của Guacamole. Cổng Cockpit `9090` không được đưa vào tunnel.
6. Password, token và URL tunnel không được ghi vào XML, Git, connection description hoặc output log.
7. Guacamole remains the end-user authorization boundary. Cockpit là host administration và chỉ cho administrator dùng ở local host.

## 4. Các phương án

### Phương án A: giữ QEMU script và chỉ cài Cockpit

Cockpit Machines lấy dữ liệu chính từ QEMU/libvirt. Các QEMU transient units hiện tại không phải persistent libvirt domains nên Cockpit không thể quản lý chúng đúng nghĩa: không có danh sách domain bền vững, storage pool, network XML hoặc lifecycle qua libvirt.

Ưu điểm là không phải migrate ngay. Nhược điểm là có hai lifecycle owner, Cockpit không tạo được VM dùng cùng chuẩn, reboot/rollback dễ khởi động sai owner và mục tiêu quản trị bằng web không đạt. Phương án này bị loại.

### Phương án B: libvirt system connection + NAT network riêng + DHCP reservation

Cài Cockpit Machines và libvirt trong cùng Ubuntu WSL2. Định nghĩa domain `windows11` trên `qemu:///system`, giữ disk/UEFI/TPM hiện tại, và cấp mạng qua một libvirt NAT network riêng `guac-nat` trên `virbr-guac`. Windows 11 có MAC cố định và lease `192.168.250.11`; Guacamole kết nối thẳng tới `192.168.250.11:3389`.

Docker Guacamole được kiểm tra route và firewall từ Compose bridge tới mạng `192.168.250.0/24`. Chỉ cho phép luồng Guacamole tới các cổng remote cần thiết; không expose mạng VM ra Windows LAN hoặc Internet. Mỗi VM mới được Cockpit tạo trên cùng network, sau đó được cấp MAC/IP reservation và connection Guacamole.

Ưu điểm là Cockpit quản lý đúng persistent domains, tạo VM mới từ UI được, địa chỉ VM ổn định, không phải duy trì một lớp port-forward cho từng VM và mô hình phù hợp với QEMU/libvirt. Nhược điểm là phải kiểm tra forwarding giữa Docker bridge và libvirt bridge trong WSL; network và firewall là cổng kiểm chứng bắt buộc trước cutover.

Đây là phương án được chọn.

### Phương án C: libvirt + host-side RDP proxy cho từng VM

VM vẫn ở libvirt NAT nhưng mỗi máy được map qua một host port như `3391`, `3392`, `3393`; Guacamole tiếp tục kết nối tới WSL host `172.18.0.1`. Proxy có thể là nftables redirect, `socat` hoặc một service riêng.

Ưu điểm là Docker chỉ cần tới một địa chỉ host đã có, dễ tương thích nếu WSL chặn route Docker-to-virbr. Nhược điểm là phải cấp phát và giữ bảng port, thêm process/service ngoài Cockpit, UI tạo VM không tự hoàn thiện connection, và lỗi port collision khó chẩn đoán. Đây là fallback nếu gate route trực tiếp của Phương án B thất bại; không dùng làm kiến trúc chính.

## 5. Kiến trúc được chọn

```text
Windows host
└── WSL2 Ubuntu-24.04
    ├── ext4.vhdx tại H:\RemoteWorkspaces\guacamole-client\runtime\ubuntu\ext4.vhdx
    │   ├── Docker Engine
    │   │   ├── guacamole : 127.0.0.1:8080
    │   │   ├── guacd
    │   │   └── PostgreSQL + named volume
    │   ├── Cockpit + cockpit-machines : 127.0.0.1:9090
    │   ├── libvirt qemu:///system
    │   │   ├── guac-nat / virbr-guac : 192.168.250.1/24
    │   │   ├── windows11 : 192.168.250.11, RDP 3389
    │   │   └── windows10-* / windows11-* tạo sau bằng Cockpit
    │   └── VM disk, UEFI, TPM, libvirt metadata và logs
    └── cloudflared
        └── chỉ proxy 127.0.0.1:8080

Internet -> Cloudflare Quick Tunnel -> Guacamole -> guacd -> 192.168.250.11:3389
Windows browser -> https://127.0.0.1:9090 -> Cockpit -> libvirt -> VM
```

Cockpit không chạy trong Docker. Nó chạy như service trong Ubuntu WSL và nói chuyện với libvirt system daemon. Guacamole không cần cài vào Windows guest; Windows guest chỉ cần Remote Desktop bật.

### 5.1. Storage

Phải giữ các file sau ở vùng H-backed:

| Tài nguyên | Vị trí active sau cutover | Ghi chú |
|---|---|---|
| Windows disk | `/var/lib/guacamole-vm-windows11/windows11.qcow2` | Nằm trong `ext4.vhdx`; không copy lại trong migrate |
| UEFI NVRAM | `/var/lib/guacamole-vm-windows11/OVMF_VARS_4M.ms.fd` | Copy nhất quán từ file hiện tại sau khi tắt VM; giữ bản gốc dưới `runtime` làm backup |
| TPM 2.0 | `/var/lib/guacamole-vm-windows11/tpm/` | Dedicated systemd unit giữ swtpm bằng state này và mở socket `/run/guacamole-vm-windows11/swtpm.sock`; không tạo state mới hoặc chạy swtpm thứ hai |
| Libvirt domain/network | `/etc/libvirt` trong WSL và bản XML canonical dưới `deploy-local/libvirt/` | `/etc/libvirt` là runtime state; XML trong repo dùng cho recreate/review |
| VM mới | `/var/lib/guacamole-vms/` trong WSL | Storage pool dành cho Cockpit; vẫn thuộc `ext4.vhdx` trên H: |
| ISO | `/mnt/h/RemoteWorkspaces/guacamole-client/runtime/iso/` | Read-only input; ghi hash trước khi cài |
| Database | Docker named volume trong `ext4.vhdx` | Không đổi volume hoặc auth trong cutover |

Nếu quyền của libvirt-qemu không đọc được `/var/lib/guacamole-vm-windows11`, sửa ownership/ACL ở chính filesystem WSL sau khi đã backup và kiểm tra disk không còn được QEMU script giữ mở. Không mở live qcow2 qua SMB/UNC.

### 5.2. Domain Windows 11

Domain persistent phải giữ các đặc tính tương thích với guest hiện tại:

- name ổn định `windows11`, autostart tùy chọn chỉ sau khi recovery gate pass;
- 4 vCPU, 8192 MiB RAM;
- Q35 + OVMF UEFI Secure Boot, dùng bản UEFI variables đã backup;
- TPM 2.0 qua external Unix socket `__W11_TPM_SOCKET__` nối tới dedicated systemd unit; unit này chạy swtpm bằng state H-backed `/var/lib/guacamole-vm-windows11/tpm` và socket runtime `/run/guacamole-vm-windows11/swtpm.sock`; libvirt chỉ là lifecycle owner của QEMU/VM và không tự spawn swtpm;
- qcow2 hiện tại, giữ bus SATA/IDE tương thích với Windows đã cài. Chỉ đổi sang virtio sau khi cài driver và có backup boot-tested;
- NIC `e1000e` và MAC cố định để Windows không tạo thiết bị mạng mới ngoài dự kiến;
- không gắn ISO khi VM đã hoàn tất cài đặt;
- không thêm `-no-reboot`; reboot của guest phải được libvirt giữ trong cùng domain;
- không mở VNC/RDP listener trên mọi interface của host. RDP chỉ nằm trên libvirt network và được Guacamole truy cập.

TPM state là một phần của danh tính Windows 11. Không tạo TPM mới trong migration vì có thể làm guest mất trạng thái mã hóa/Windows Hello. Dedicated systemd unit phải được kiểm tra đang dùng đúng state H-backed và mở đúng socket trước khi define domain. Nếu libvirt version không hỗ trợ external TPM Unix socket, phase preflight phải dừng, không chuyển sang backend khác hoặc TPM mới và không tuyên bố migration thành công.

### 5.3. Network `guac-nat`

Tạo network riêng để tránh phụ thuộc network `default` và giảm nguy cơ đụng dải. XML canonical nên tương đương:

```xml
<network>
  <name>guac-nat</name>
  <bridge name='virbr-guac'/>
  <forward mode='nat'/>
  <ip address='192.168.250.1' netmask='255.255.255.0'>
    <dhcp>
      <range start='192.168.250.100' end='192.168.250.254'/>
      <host mac='52:54:00:11:11:01' name='windows11' ip='192.168.250.11'/>
    </dhcp>
  </ip>
</network>
```

Địa chỉ và MAC trên là giá trị thiết kế; implementation phải kiểm tra không xung đột với network hiện có trước `net-start`. Không attach physical NIC vào `virbr-guac`. NAT network của libvirt cho phép guest ra ngoài qua host và cho host truy cập guest; Docker-to-libvirt traffic phải được chứng minh bằng test thực tế từ cùng Compose network.

Nguồn tham khảo chính thức: [libvirt network XML](https://libvirt.org/formatnetwork.html) và [libvirt domain XML](https://libvirt.org/formatdomain.html).

## 6. Lộ trình triển khai

### Phase 0: preflight, không thay đổi state

Chạy từ `H:\RemoteWorkspaces\guacamole-client\deploy-local`:

```powershell
git -C H:\RemoteWorkspaces\guacamole-client status --short
.\recover-after-rollback.ps1 status
Test-Path H:\RemoteWorkspaces\guacamole-client\runtime\ubuntu\ext4.vhdx
Test-Path H:\RemoteWorkspaces\guacamole-client\runtime\iso\Windows11_23H2_UEFI.iso
wsl.exe -d Ubuntu-24.04 -u root -- sh -lc "test -e /dev/kvm && qemu-img info /var/lib/guacamole-vm-windows11/windows11.qcow2"
wsl.exe -d Ubuntu-24.04 -u root -- virsh -c qemu:///system uri
```

Phase dừng nếu thiếu H-backed disk, `/dev/kvm`, Windows password/ISO, hoặc libvirt system connection chưa hoạt động. Lưu lại package versions, `ss -ltnp`, Docker Compose status, VM command line và connection/permission inventory của Guacamole.

Đặc biệt, phải xác nhận Windows đã boot từ disk và RDP hoạt động trước khi bắt đầu migrate. `install-finished.marker` một mình không đủ bằng chứng nếu command line QEMU vẫn gắn ISO.

### Phase 1: backup nhất quán

1. Ghi database dump PostgreSQL vào `runtime\backups\` bằng script hiện có; không in password.
2. Dùng Guacamole API hoặc query read-only để lưu inventory gồm connection id/name/protocol/hostname/port và quan hệ user/group permission. Không lưu password connection vào artifact.
3. Tắt Windows qua ACPI (`windows11.ps1 stop`), đợi QEMU và TPM đều stopped, kiểm tra không còn process mở qcow2.
4. Chạy `qemu-img check --read-only` và `qemu-img info`; lưu hash qcow2.
5. Copy UEFI vars và toàn bộ TPM state vào `runtime\backups\windows11-pre-libvirt-<timestamp>\`; lưu hash/manifest.
6. Lưu `systemctl cat`/journal của hai transient units, XML network/domain nếu có và output `windows11 status`.

Không copy qcow2 khi guest đang chạy. Không gọi `virsh undefine --nvram` trên domain chưa được backup.

### Phase 2: cài host packages trong WSL

Agent implementation phải dùng Ubuntu repository hiện hành và ghi lại version thực tế, không tự đoán version. Bộ tối thiểu cần kiểm tra qua `apt-cache policy` gồm:

```text
cockpit
cockpit-machines
libvirt-daemon-system
libvirt-daemon-driver-qemu
libvirt-clients
qemu-system-x86
qemu-utils
ovmf
swtpm
swtpm-tools
dnsmasq
libvirt-dbus
```

Sau khi cài, bật systemd sockets/services theo layout package thực tế (`libvirtd` hoặc modular `virtqemud`, `virtnetworkd`, `virtstoraged`, cùng `virtlogd`/`virtlockd`). Cổng kiểm chứng là:

```bash
virsh -c qemu:///system uri
virsh -c qemu:///system list --all
test -e /dev/kvm
systemctl --failed
```

Nếu package hoặc service không hoạt động trong WSL2, dừng tại phase này; không sửa domain và không xóa QEMU state.

### Phase 3: bind Cockpit local-only

Cài `cockpit.socket` nhưng tạo socket drop-in để `ListenStream` chỉ là `127.0.0.1:9090`. Không dùng bind `0.0.0.0`, không thêm port 9090 vào Cloudflare command và không tạo tunnel riêng cho Cockpit.

Kiểm chứng từ Windows và WSL:

```powershell
Test-NetConnection 127.0.0.1 -Port 9090
```

```bash
ss -ltnp | grep ':9090'
curl -k -I https://127.0.0.1:9090
```

URL quản trị local là `https://127.0.0.1:9090`. Self-signed certificate lần đầu là hành vi bình thường; chỉ truy cập từ chính Windows host. Cloudflare Quick Tunnel tiếp tục chỉ kiểm tra `http://127.0.0.1:8080`.

Cockpit Machines dùng QEMU/libvirt làm datasource chính; tham khảo [Cockpit Virtual Machines](https://cockpit-project.org/guide/195/feature-virtualmachines.html).

### Phase 4: define network và storage pool

1. Kiểm tra `virsh net-list --all` và dải đang dùng.
2. `net-define` XML `guac-nat`, `net-autostart`, `net-start`.
3. Tạo storage pool `guacamole-vms` tại `/var/lib/guacamole-vms` trong WSL ext4; `pool-define`, `pool-build` nếu cần, `pool-autostart`, `pool-start`.
4. Tạo reservation cho MAC Windows 11 và ghi lease sau khi VM boot.
5. Thiết lập forwarding/firewall tối thiểu giữa Compose bridge của Guacamole và `virbr-guac`. Chỉ cho phép TCP 3389 tới các địa chỉ VM được cấp. Không mở dải này trên Windows LAN.
6. Từ chính container/network namespace của Guacamole, kiểm tra DNS/route và kết nối tới RDP guest. Test host tới VM không thay thế test từ Guacamole.

Nếu Docker bridge không tới được `192.168.250.0/24`, không chuyển sang host proxy âm thầm. Ghi lại lỗi, kiểm tra ip_forward/firewall của WSL, rồi dùng Phương án C chỉ khi runbook có port registry và test collision.

### Phase 5: migrate Windows 11 thành persistent domain

1. Xác nhận Phase 1 backup và QEMU/legacy swtpm stopped.
2. Trong cùng WSL ext4, tạo thư mục libvirt state và copy UEFI vars hiện tại thành active NVRAM path; giữ nguyên TPM state hiện có tại `/var/lib/guacamole-vm-windows11/tpm` và bản backup, không copy sang TPM directory mới.
3. Tạo dedicated systemd unit giữ swtpm với state `/var/lib/guacamole-vm-windows11/tpm` và socket `/run/guacamole-vm-windows11/swtpm.sock`; kiểm tra owner/permission để libvirt kết nối được socket và đọc được disk/NVRAM, không chmod rộng hơn cần thiết.
4. Render domain XML canonical với disk hiện tại, UEFI, TPM2 external socket `__W11_TPM_SOCKET__`, e1000e, MAC cố định và `guac-nat`; không gắn installer ISO.
5. Chạy `virsh define` rồi `virsh dominfo windows11`; domain phải là persistent và không transient.
6. `virsh start windows11`; kiểm tra `virsh domstate`, `virsh net-dhcp-leases guac-nat`, log libvirt/QEMU và RDP 3389.
7. Đăng nhập Windows bằng secret hiện có; không đổi user/password trong bước migration.
8. Cập nhật connection Guacamole `Windows 11` từ `172.18.0.1:3391` sang `192.168.250.11:3389` nếu connection hiện tại đang dùng host forward. Giữ nguyên connection id/name và toàn bộ user/group permission rows.
9. Đăng nhập bằng administrator và user demo để xác nhận đúng máy được hiển thị. Đăng nhập user không được cấp quyền để xác nhận không thấy connection.
10. Chỉ sau hai lần start/reboot thành công và một phiên RDP Guacamole thành công mới chuyển recovery entrypoint sang libvirt owner.

### Phase 6: chuyển lifecycle owner

`recover-after-rollback.ps1` sau implementation phải làm các việc sau theo thứ tự:

1. import/register đúng `ext4.vhdx` nếu WSL distribution mất đăng ký;
2. chờ systemd và Docker;
3. start Compose;
4. start libvirt network/storage/domain `windows11`;
5. start Quick Tunnel vào Guacamole;
6. report Cockpit local URL, Guacamole local URL, VM state và public Guacamole URL.

Script không được gọi `vm-windows11\windows11.ps1 start` sau cutover. File QEMU script hiện tại vẫn giữ làm rollback tool có điều kiện, nhưng không còn được gọi trong đường chạy bình thường. Tất cả VM mới tạo từ UI cũng phải thuộc `qemu:///system` và network `guac-nat`.

## 7. Tạo Windows 10/11 mới bằng giao diện

Sau khi Cockpit local-only đã hoạt động:

1. Mở `https://127.0.0.1:9090`, đăng nhập tài khoản host administrator.
2. Chọn **Virtual Machines** trên system connection.
3. Chọn **Create VM**, đặt tên duy nhất như `windows11-02` hoặc `windows10-01`.
4. Chọn ISO trong `/mnt/h/RemoteWorkspaces/guacamole-client/runtime/iso/`; kiểm tra hash ISO trước đó.
5. Chọn UEFI cho Windows 11, TPM 2.0 nếu wizard hỗ trợ; đặt RAM/vCPU theo capacity còn lại.
6. Chọn storage pool `guacamole-vms`, đặt disk dưới `/var/lib/guacamole-vms/` trong ext4.vhdx.
7. Chọn network `guac-nat`, giữ MAC cố định và tạo DHCP reservation.
8. Cài guest, bật RDP/firewall trong guest, rồi test từ WSL host và từ Guacamole container.
9. Tạo connection RDP trong Guacamole tới IP reservation port 3389; gán connection cho đúng user/group trong **Settings → Users/Groups → Permissions**.
10. Lưu inventory gồm VM name, MAC, IP, RDP port, ISO hash và người được gán trong artifact maintenance; không lưu password.

Clone chỉ được dùng sau khi tạo một template sạch đã shutdown và backup. Không clone disk đang chạy hoặc clone một Windows instance đã chứa dữ liệu người dùng mà chưa có mục đích rõ ràng.

## 8. Backup và rollback

### Backup bắt buộc trước mỗi migration/update host

- PostgreSQL dump và `postgres_password.txt`;
- `windows11.qcow2` sau shutdown sạch;
- UEFI vars;
- toàn bộ TPM state;
- domain XML/network XML và package versions;
- ISO hash và Windows password file;
- inventory Guacamole user/group/connection permissions.

Backup phải ở H: hoặc kho backup riêng có ACL giới hạn. Không đưa secret vào maintenance ZIP công khai.

### Rollback khi libvirt domain không boot hoặc RDP không hoạt động

1. Dừng domain qua ACPI; chỉ dùng `virsh destroy` để recovery khi guest không phản hồi và ghi rõ lý do.
2. Xác nhận domain stopped, QEMU/libvirt không còn mở qcow2/TPM.
3. Không xóa disk. Nếu active NVRAM/TPM đã được copy sang path mới, giữ nguyên bản backup và khôi phục đúng cặp disk + NVRAM + TPM từ cùng checkpoint.
4. `virsh undefine windows11` chỉ sau khi export XML và bảo đảm lệnh không xóa bản state cần dùng.
5. Khôi phục service QEMU script cũ, start Windows bằng `windows11.ps1`, khôi phục connection Guacamole về `172.18.0.1:3391` nếu đã đổi.
6. Xác nhận Guacamole login/permission và RDP trước khi tiếp tục điều tra libvirt.

Không chạy script QEMU trong khi domain libvirt còn defined/running hoặc khi dedicated systemd swtpm unit còn giữ socket/state TPM. Rollback phải luôn có bằng chứng owner đã chuyển hoàn toàn.

## 9. Cập nhật các file của project sau implementation

Implementation của design này cần tạo hoặc sửa tối thiểu các file sau:

| File | Trách nhiệm |
|---|---|
| `deploy-local/libvirt/networks/guac-nat.xml` | XML network canonical, không secret |
| `deploy-local/libvirt/domains/windows11.xml.template` | Domain XML canonical, path/tokenized rõ ràng, không password |
| `deploy-local/libvirt/README.md` | Quy ước network, storage, Cockpit và lifecycle |
| `deploy-local/libvirt.ps1` | Install/check/start/stop/status/backup helper cho libvirt qua WSL |
| `deploy-local/migrate-windows11-to-libvirt.ps1` | Preflight, backup, define, smoke test, cutover và rollback checkpoint |
| `deploy-local/test-libvirt.ps1` | Read-only gate cho Cockpit, libvirt, network, domain, storage và RDP |
| `deploy-local/recover-after-rollback.ps1` | Gọi libvirt owner sau cutover; không gọi QEMU script thường lệ |
| `deploy-local/README.md` | Topology, local Cockpit URL, start/status/rollback nhanh |
| `deploy-local/RUNBOOK.md` | Cài từ máy trống, migrate, tạo VM, backup/update/troubleshooting đầy đủ |
| `.gitignore` | Ignore runtime/XML export có secret nếu implementation tạo ra |

Các script PowerShell phải gọi WSL với `-NoProfile`, không in secret, dùng path H rõ ràng và fail fast khi state/owner không đúng. XML canonical có thể dùng placeholder cho path nhưng script phải render thành absolute Linux path và chạy `virsh define` validation trước khi start.

## 10. Verification gates

Không báo hoàn tất nếu thiếu một gate. Mỗi gate ghi command, kết quả và thời điểm.

### Gate A: host và storage

- `wsl.exe --list --verbose` cho thấy `Ubuntu-24.04` version 2.
- `test -e /dev/kvm` thành công.
- `runtime\ubuntu\ext4.vhdx` tồn tại trên H:, không có VM state mới trên C:.
- `df -h /` và `qemu-img info` xác nhận disk nằm trong WSL ext4.
- `virsh -c qemu:///system uri` trả `qemu:///system`.

### Gate B: Cockpit exposure

- `ss -ltnp` cho Cockpit chỉ có `127.0.0.1:9090`.
- `https://127.0.0.1:9090` mở được từ Windows host.
- Quick Tunnel command chỉ chứa `http://127.0.0.1:8080`; không có 9090.
- Không có test public nào truy cập được Cockpit.

### Gate C: libvirt state

- `virsh net-list --all` có `guac-nat` active/autostart.
- `virsh pool-list --all` có `guacamole-vms`.
- `virsh list --all` có persistent `windows11`, không có QEMU transient service owner.
- Domain XML cho thấy UEFI, TPM2 external socket, disk path cũ và MAC cố định; dedicated swtpm unit active với socket `/run/guacamole-vm-windows11/swtpm.sock`.
- `virsh domblklist windows11` không trỏ vào C: hoặc ISO installer sau cutover.

### Gate D: guest/RDP

- DHCP lease là `192.168.250.11` cho MAC Windows 11.
- RDP 3389 mở từ WSL host.
- Guacamole container thực sự tạo được phiên RDP tới guest.
- Windows user và password cũ hoạt động; không tạo lại Windows account trong migration.
- Reboot domain bằng Cockpit/`virsh reboot` xong RDP quay lại.

### Gate E: Guacamole authorization

- Admin vẫn thấy và quản lý `Windows 11`.
- `demo` chỉ thấy đúng connection đã được gán.
- User không có permission không thấy connection.
- Connection target mới đúng IP/MAC inventory; connection name/id và permission rows không bị tạo bản sao ngoài ý muốn.

### Gate F: recovery

- `recover-after-rollback.ps1 status` report Docker, Guacamole, libvirt network/domain và tunnel.
- Chạy restart/WSL shutdown theo maintenance procedure rồi start lại; domain tự lên từ libvirt.
- QEMU script cũ không tự khởi động cùng libvirt.
- Sau C: rollback giả lập, script chỉ import/register `ext4.vhdx` hiện có trên H: và không tạo disk/secret mới.

### Gate G: VM mới từ Cockpit

- Tạo một VM test nhỏ hoặc Windows ISO test trong storage pool H-backed.
- VM nhận network `guac-nat`, có reservation và RDP test.
- Xóa VM test qua Cockpit chỉ sau khi đã lưu output; không xóa Windows 11 production.
- Có hướng dẫn tạo connection và assign user/group trong RUNBOOK.

## 11. Lỗi thường gặp và cách xử lý

| Triệu chứng | Nguyên nhân thường gặp | Kiểm tra và xử lý |
|---|---|---|
| Cockpit 9090 không mở | socket chưa bật hoặc bind sai | `systemctl status cockpit.socket`, `ss -ltnp`; sửa bind về `127.0.0.1:9090`, không mở public |
| Cockpit mở nhưng không có Virtual Machines | `cockpit-machines` hoặc libvirt D-Bus thiếu | kiểm tra package, `virsh -c qemu:///system uri`, daemon/socket; không dùng session connection nhầm |
| `virsh` không connect `qemu:///system` | libvirt daemon/socket chưa chạy hoặc WSL systemd lỗi | `systemctl --failed`, `systemctl status libvirtd virtqemud`; sửa host service trước khi define domain |
| `/dev/kvm` mất | BIOS/Virtual Machine Platform/WSL nested virtualization | kiểm tra `wsl --status`, BIOS và `/dev/kvm`; không chuyển sang slow TCG cho production migration |
| Domain boot vào UEFI shell | NVRAM không đúng cặp với disk hoặc disk bus đổi | khôi phục cùng checkpoint disk+NVRAM, giữ SATA/IDE, kiểm tra `domblklist`; không cài lại ngay |
| TPM lock hoặc `swtpm` không start | QEMU script còn giữ TPM hoặc state path quyền sai | dừng cả hai owner, kiểm tra process/socket/ACL; chỉ một swtpm do libvirt spawn |
| Windows yêu cầu recovery/BitLocker | TPM/NVRAM bị thay hoặc disk snapshot không nhất quán | rollback cặp state cùng checkpoint; không tạo TPM mới để chữa nhanh |
| Host ping được VM nhưng Guacamole không connect | Docker bridge không route tới `virbr-guac` hoặc firewall FORWARD chặn | test từ cùng Compose network, kiểm tra route/iptables; sửa rule tối thiểu hoặc chuyển fallback proxy có port registry |
| DHCP IP thay đổi | MAC không cố định hoặc reservation thiếu | kiểm tra domain XML và `net-dumpxml`; giữ MAC và reservation |
| RDP port mở nhưng login fail | RDP service/user policy/password | test bằng account file, xem Windows event/guest; không đổi secret trong host script |
| Guacamole mất máy sau migrate | connection vẫn trỏ `172.18.0.1:3391` | cập nhật hostname/port thành IP reservation 3389, giữ permission rows |
| User thấy máy không được cấp | permission rows bị duplicate/wrong identifier | backup DB, kiểm tra connection/user/group permission inventory, sửa qua Guacamole admin |
| Quick Tunnel không vào được | chỉ Guacamole local 8080 mới được proxy | kiểm tra `start-quick-tunnel.ps1 status` và HTTP 200; không expose Cockpit qua tunnel |
| VM mới lưu trên C: | Cockpit chọn storage pool mặc định ngoài H | không start VM đó; sửa default pool thành `/var/lib/guacamole-vms` trong ext4.vhdx và xác nhận path |
| Rollback khởi động hai VM | recover script còn gọi QEMU sau cutover | stop cả owner, sửa lifecycle script, kiểm tra systemd units và `virsh list`; tuyệt đối không để cùng qcow2 chạy hai lần |

## 12. Update và maintenance

### Host packages

Trước update Cockpit/libvirt/QEMU: backup DB, domain XML, network XML, disk/NVRAM/TPM và package versions. Shutdown Windows sạch, update apt trong đúng `Ubuntu-24.04`, rồi chạy toàn bộ gates A-F. Không chạy `apt autoremove` nếu chưa kiểm tra package dependency của libvirt/swtpm/OVMF.

### Cockpit/libvirt domain

Thay đổi CPU/RAM/NIC/disk bằng Cockpit hoặc `virsh` persistent config; sau thay đổi export XML vào backup và ghi changelog. Không sửa transient QEMU command line. Không đổi disk bus, firmware hoặc TPM source khi chưa có backup boot-tested.

### Windows guest

Windows Update chạy trong Windows 10/11. Sau update, test login, RDP, reboot và Guacamole. Windows license/activation là trách nhiệm license key của người dùng; việc migrate không tự activate Windows.

### Guacamole

Nâng `guacamole` và `guacd` cùng phiên bản; giữ PostgreSQL volume và chạy migration theo release notes. Kiểm tra connection target/permissions sau mỗi update. Cockpit không thay thế database auth của Guacamole.

## 13. Tài liệu và bàn giao

Sau implementation, `deploy-local\README.md` phải có sơ đồ ngắn và lệnh start/status. `deploy-local\RUNBOOK.md` phải có đầy đủ bootstrap từ máy trống, cài package, bind local-only, migrate không reinstall, tạo VM từ UI, cấp IP, thêm connection/permission, backup, rollback, update và bảng lỗi ở trên.

Mỗi lần maintenance agent phải báo riêng:

1. source/config đã đọc;
2. command đã chạy và kết quả;
3. local HTTP/Cockpit/Guacamole verification;
4. domain/storage/network/RDP verification;
5. user permission và public Guacamole verification;
6. phần chưa kiểm chứng hoặc còn cần thao tác trong guest.

Không dùng chữ “đã hoàn tất” chỉ dựa trên `virsh define` hoặc port listener. Cần có phiên RDP thực tế qua Guacamole và recovery test sau cutover.

## 14. Cơ sở tài liệu chính thức

- [Cockpit Machines / Virtual Machines](https://cockpit-project.org/guide/195/feature-virtualmachines.html): Cockpit Machines quản lý QEMU/libvirt và system connection.
- [libvirt Domain XML](https://libvirt.org/formatdomain.html): disk, network, UEFI và external TPM Unix socket.
- [libvirt Network XML](https://libvirt.org/formatnetwork.html): NAT network, bridge, DHCP và reservation.
- [Apache Guacamole Docker](https://guacamole.apache.org/doc/gug/guacamole-docker.html): service split và Docker deployment.
- [Apache Guacamole PostgreSQL authentication](https://guacamole.apache.org/doc/gug/postgresql-auth.html): database auth và persistence.
