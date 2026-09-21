# Cockpit Machines + libvirt Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cài Cockpit Machines và libvirt trong Ubuntu-24.04 WSL2, chuyển Windows 11 hiện tại sang persistent libvirt domain dùng đúng disk/UEFI/TPM trên H:, giữ Guacamole làm cổng remote và cung cấp quy trình tạo, backup, rollback, update và phục hồi cho agent sau.

**Architecture:** Cockpit chạy như system service trong WSL và chỉ bind `127.0.0.1:9090`; libvirt `qemu:///system` là lifecycle owner duy nhất của các VM sau cutover. Windows 11 chạy trên network NAT `guac-nat` với MAC cố định và IP reservation `192.168.250.11`; Guacamole trong Docker kết nối trực tiếp tới `192.168.250.11:3389`, còn Cloudflare Quick Tunnel chỉ proxy `127.0.0.1:8080` của Guacamole.

**Tech Stack:** Windows PowerShell 5+/7, WSL2 `Ubuntu-24.04`, systemd, Docker Compose, Apache Guacamole `1.6.0`, PostgreSQL `16-alpine`, Cockpit, `cockpit-machines`, libvirt, QEMU/KVM, OVMF, `swtpm`, `dnsmasq`, libvirt NAT, Cloudflare Quick Tunnel.

**Spec:** `H:\RemoteWorkspaces\guacamole-client\docs\superpowers\specs\2026-09-19-cockpit-machines-design.md`

## Global Constraints

- Repository và mọi asset lâu dài phải ở `H:\RemoteWorkspaces\guacamole-client` hoặc trong `runtime\ubuntu\ext4.vhdx` của repository.
- Không đặt database, disk VM, TPM, UEFI variables, ISO, secret hoặc log quan trọng trên C:.
- Không chạy QEMU script và libvirt đồng thời trên cùng qcow2 hoặc TPM state; sau cutover `qemu:///system` là lifecycle owner duy nhất.
- Không dùng `docker compose down -v`, không xóa PostgreSQL volume và không tạo password mới khi database cũ vẫn còn.
- Cockpit phải bind duy nhất `127.0.0.1:9090`; không đưa cổng 9090 vào Cloudflare Quick Tunnel.
- Quick Tunnel chỉ proxy `http://127.0.0.1:8080` của Guacamole.
- Password, token và URL tunnel không được ghi vào XML canonical, Git, connection description hoặc log.
- Guacamole là ranh giới phân quyền cho người dùng cuối; Cockpit chỉ dành cho quản trị host từ máy Windows cục bộ.
- Migration phải giữ Windows disk hiện tại, UEFI variables và TPM state; không cài lại Windows và không tạo lại Guacamole database.
- Domain `windows11` phải giữ 4 vCPU, 8192 MiB RAM, Q35, OVMF Secure Boot, TPM 2.0 qua external Unix socket do dedicated systemd swtpm unit cung cấp, disk qcow2 hiện tại, NIC `e1000e`, MAC `52:54:00:11:11:01`, network `guac-nat` và không gắn ISO sau cutover.
- Network canonical là `192.168.250.0/24`, bridge `virbr-guac`, gateway `192.168.250.1`, DHCP range `192.168.250.100-192.168.250.254`, reservation Windows 11 `192.168.250.11`.
- Agent phải ghi lại version thực tế từ `apt-cache policy`, `virsh version`, `cockpit-bridge --version` và Docker Compose; không đoán version package.
- Mọi PowerShell gọi WSL phải dùng `wsl.exe -d Ubuntu-24.04 -u root -- ...` với `-NoProfile`; mọi path host phải là path H tuyệt đối.
- Không sửa hoặc xóa thay đổi có sẵn trong `git status`; không commit trong quá trình thực hiện kế hoạch này.

## Review Focus

- **QEMU/libvirt ownership collision:** chạy migration khi transient QEMU hoặc swtpm còn sống phải dừng trước `virsh define`; test trong Task 5 phải chứng minh không còn process/unit giữ qcow2/TPM.
- **UEFI/TPM pairing:** dùng sai NVRAM hoặc tạo TPM mới có thể làm Windows vào recovery/BitLocker; test trong Task 5 phải boot bằng đúng cặp disk + NVRAM + TPM từ checkpoint.
- **Docker-to-libvirt routing:** host kết nối được VM chưa đủ; test trong Task 4 và Task 7 phải chạy từ chính Compose network tới `192.168.250.11:3389`.
- **Storage escaping H:** Cockpit storage mặc định hoặc VM mới ghi vào `/var/lib/libvirt/images` ngoài policy có thể nằm trên filesystem không được kiểm soát; test trong Task 4 và Task 8 phải kiểm tra `domblklist`/`qemu-img` không trỏ C:.
- **Recovery ownership drift:** recovery sau rollback nếu còn gọi `windows11.ps1 start` sẽ tạo hai owner; test trong Task 6 phải xác minh `recover-after-rollback.ps1` chỉ gọi libvirt cho Windows 11 và legacy script bị chặn bởi marker cutover.

---

## File Map Before Implementation

### Files to create

- `deploy-local/libvirt/networks/guac-nat.xml` — XML canonical cho NAT network, bridge, DHCP range và reservation Windows 11; không chứa secret.
- `deploy-local/libvirt/domains/windows11.xml.template` — XML domain canonical dùng các token path nội bộ do script render; không chứa password hoặc URL tunnel.
- `deploy-local/libvirt/README.md` — quy ước domain/network/storage, ownership và thao tác virsh/Cockpit.
- `deploy-local/libvirt.ps1` — wrapper PowerShell cho `preflight`, `install`, `configure`, `network`, `storage`, `start`, `stop`, `status`, `backup`, `export` và route Docker-to-libvirt.
- `deploy-local/migrate-windows11-to-libvirt.ps1` — orchestration có checkpoint cho preflight, backup, define, smoke test, cutover và rollback.
- `deploy-local/test-libvirt.ps1` — read-only verification gate cho host, Cockpit, libvirt, storage, network, RDP và Guacamole route.
- `deploy-local/new-libvirt-vm.ps1` — đường CLI deterministic khi Cockpit UI không dùng được; tạo disk/domain trên storage pool H-backed và network `guac-nat`.

### Files to modify

- `deploy-local/vm-windows11/windows11.ps1` — thêm guard marker để không start legacy QEMU sau cutover; chỉ cho rollback khi truyền cờ rõ ràng.
- `deploy-local/recover-after-rollback.ps1` — start/stop/status Windows 11 qua `libvirt.ps1`, theo đúng thứ tự WSL → Docker → Compose → network/storage/domain → tunnel.
- `deploy-local/START-REMOTE.cmd` — thông báo local Cockpit URL và ownership libvirt; vẫn gọi một entrypoint duy nhất.
- `deploy-local/README.md` — topology sau cutover và lệnh start/status/Cockpit/new VM.
- `deploy-local/RUNBOOK.md` — bootstrap từ máy gần như trống, migration không reinstall, tạo VM, assign Guacamole, backup/rollback/update và lỗi thường gặp.
- `deploy-local/export-maintenance-bundle.ps1` — đóng gói các script/XML/docs mới nhưng loại runtime data và kiểm tra archive.
- `.gitignore` — loại generated libvirt exports/temporary XML và giữ canonical XML/scripts có thể review.

### Files intentionally kept as legacy rollback assets

- `deploy-local/vm-windows11/windows11.ps1` vẫn tồn tại để rollback, nhưng không được recovery gọi sau cutover.
- `deploy-local/vm-windows11/README.md` phải được cập nhật để nói rõ script là legacy rollback owner sau marker cutover.
- `deploy-local/vm-demo/qemu-demo.ps1` và VMware/Ubuntu demo không nằm trong migration Windows 11; chỉ thay đổi nếu recovery đang gọi nhầm Windows script.

---

### Task 1: Add canonical libvirt network, domain and operator contract

**Files:**
- Create: `deploy-local/libvirt/networks/guac-nat.xml`
- Create: `deploy-local/libvirt/domains/windows11.xml.template`
- Create: `deploy-local/libvirt/README.md`

**Interfaces:**
- Consumes: Existing WSL paths `/var/lib/guacamole-vm-windows11/windows11.qcow2`, `/var/lib/guacamole-vm-windows11/OVMF_VARS_4M.ms.fd`, `/var/lib/guacamole-vm-windows11/tpm`; dedicated systemd swtpm socket `/run/guacamole-vm-windows11/swtpm.sock`; existing Windows MAC/port assumptions from `deploy-local/vm-windows11/windows11.ps1`.
- Produces: `guac-nat` network XML, `windows11` domain template and operator rules consumed by Tasks 2–8.

- [ ] **Step 1: Write `guac-nat.xml` with the exact network contract.**

  Use this content, changing nothing except XML indentation:

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

- [ ] **Step 2: Write `windows11.xml.template` without secret-bearing fields.**

  The template must contain a persistent KVM domain with:

  ```xml
  <domain type='kvm'>
    <name>windows11</name>
    <memory unit='MiB'>8192</memory>
    <currentMemory unit='MiB'>8192</currentMemory>
    <vcpu placement='static'>4</vcpu>
    <os>
      <type arch='x86_64' machine='q35'>hvm</type>
      <loader readonly='yes' secure='yes' type='pflash'>/usr/share/OVMF/OVMF_CODE_4M.ms.fd</loader>
      <nvram template='/usr/share/OVMF/OVMF_VARS_4M.ms.fd'>__W11_NVRAM_PATH__</nvram>
    </os>
    <features><acpi/><apic/></features>
    <cpu mode='host-passthrough'/>
    <clock offset='localtime'/>
    <on_poweroff>destroy</on_poweroff>
    <on_reboot>restart</on_reboot>
    <on_crash>restart</on_crash>
    <devices>
      <disk type='file' device='disk'>
        <driver name='qemu' type='qcow2'/>
        <source file='__W11_DISK_PATH__'/>
        <target dev='sda' bus='sata'/>
      </disk>
      <interface type='network'>
        <mac address='52:54:00:11:11:01'/>
        <source network='guac-nat'/>
        <model type='e1000e'/>
      </interface>
      <tpm model='tpm-tis'>
        <backend type='external'>
          <source type='unix' mode='connect' path='__W11_TPM_SOCKET__'/>
        </backend>
      </tpm>
      <graphics type='spice' autoport='yes' listen='127.0.0.1'/>
      <video><model type='qxl' ram='65536' vram='65536' heads='1'/></video>
      <console type='pty'/>
    </devices>
  </domain>
  ```

  Do not include RDP passwords, Guacamole credentials, cloudflared URLs, ISO installer media, `-no-reboot`, or host-wide VNC listeners. `libvirt.ps1` must replace the three `__W11_*__` tokens with absolute Linux paths before calling `virsh define`; `__W11_TPM_SOCKET__` must resolve to `/run/guacamole-vm-windows11/swtpm.sock` after the dedicated systemd swtpm unit is started with the existing H-backed state.

- [ ] **Step 3: Document ownership and non-negotiable storage rules in `libvirt/README.md`.**

  State explicitly that Cockpit manages `qemu:///system`, `/var/lib/guacamole-vms` is the new VM storage pool inside WSL `ext4.vhdx`, `windows11` uses the existing qcow2/NVRAM/TPM pair, and `windows11.ps1` is legacy rollback-only after the cutover marker. Include these exact inspection commands:

  ```powershell
  wsl.exe -d Ubuntu-24.04 -u root -- virsh -c qemu:///system list --all
  wsl.exe -d Ubuntu-24.04 -u root -- virsh -c qemu:///system net-list --all
  wsl.exe -d Ubuntu-24.04 -u root -- virsh -c qemu:///system pool-list --all
  wsl.exe -d Ubuntu-24.04 -u root -- virsh -c qemu:///system domblklist windows11
  ```

- [ ] **Step 4: Validate the canonical XML as text.**

  Run:

  ```powershell
  rg -n "password|secret|token|trycloudflare|3391|-no-reboot|__W11_" H:\RemoteWorkspaces\guacamole-client\deploy-local\libvirt
  ```

  Expected: only the three documented path tokens and explanatory text are present; no password, token or public URL is present. Do not call `virsh define` yet because package installation and path preflight belong to later tasks.

---

### Task 2: Implement host package, Cockpit local bind and libvirt prerequisite helper

**Files:**
- Create: `deploy-local/libvirt.ps1`

**Interfaces:**
- Consumes: Task 1 XML files; `runtime\ubuntu\ext4.vhdx`; existing `start-local.ps1` WSL keepalive behavior.
- Produces: PowerShell actions `preflight`, `install`, `configure`, `network`, `storage`, `connect-guacamole`, `start`, `stop`, `status`, `backup`, `export`.

- [ ] **Step 1: Define the script parameter contract and H-backed paths.**

  The script must accept:

  ```powershell
  [ValidateSet('preflight','install','configure','network','storage','connect-guacamole','start','stop','status','backup','export')]
  [string]$Action = 'status'
  [string]$DomainName = 'windows11'
  [int]$TimeoutSeconds = 120
  ```

  Derive `RepoRoot` from `$PSScriptRoot\..`, set `WslDistro = 'Ubuntu-24.04'`, and use only `/mnt/h/RemoteWorkspaces/guacamole-client/...` or paths inside the WSL filesystem whose backing VHDX is `runtime\ubuntu\ext4.vhdx`. Do not use `$env:TEMP`, `C:\Windows\Temp`, or a host path for persistent VM state.

  Also define the contract paths `TpmStatePath = '/var/lib/guacamole-vm-windows11/tpm'`, `TpmSocketPath = '/run/guacamole-vm-windows11/swtpm.sock'`, and `TpmUnitName = 'guacamole-vm-windows11-libvirt-tpm.service'`. The helper must treat this systemd swtpm unit as the TPM owner and libvirt as the sole QEMU/VM owner; it must never initialize replacement TPM state.

- [ ] **Step 2: Implement `preflight` as read-only checks.**

  Run exactly these checks from PowerShell and fail before any define/start mutation if one fails:

  ```powershell
  Test-Path -LiteralPath 'H:\RemoteWorkspaces\guacamole-client\runtime\ubuntu\ext4.vhdx'
  wsl.exe --list --verbose
  wsl.exe -d Ubuntu-24.04 -u root -- sh -lc "test -e /dev/kvm && qemu-img info /var/lib/guacamole-vm-windows11/windows11.qcow2"
  wsl.exe -d Ubuntu-24.04 -u root -- virsh -c qemu:///system uri
  wsl.exe -d Ubuntu-24.04 -u root -- systemctl is-system-running
  wsl.exe -d Ubuntu-24.04 -u root -- systemctl is-active cockpit.socket
  ```

  Read the existing TPM state directory and record the dedicated unit/socket
  state without starting either owner:

  ```powershell
  wsl.exe -d Ubuntu-24.04 -u root -- sh -lc "test -d /var/lib/guacamole-vm-windows11/tpm"
  wsl.exe -d Ubuntu-24.04 -u root -- systemctl is-active guacamole-vm-windows11-libvirt-tpm.service
  wsl.exe -d Ubuntu-24.04 -u root -- test -S /run/guacamole-vm-windows11/swtpm.sock
  ```

  The unit/socket may be absent before Task 5 migration; absence is recorded,
  not repaired or replaced, during this read-only preflight.

  Also record `apt-cache policy` for every package in Step 3, `virsh version`, `ss -ltnp`, `docker compose ps`, `virsh list --all`, and the legacy QEMU/TPM service states under `runtime\backups\`. Print `LIBVIRT_PREFLIGHT_OK` only after all checks pass.

- [ ] **Step 3: Implement `install` with the exact minimum package set and no version guessing.**

  Use the active Ubuntu repository:

  ```powershell
  wsl.exe -d Ubuntu-24.04 -u root -- apt-get update
  wsl.exe -d Ubuntu-24.04 -u root -- apt-get install -y cockpit cockpit-machines libvirt-daemon-system libvirt-daemon-driver-qemu libvirt-clients libvirt-dbus qemu-system-x86 qemu-utils ovmf swtpm swtpm-tools dnsmasq iptables netcat-openbsd
  ```

  Before and after installation, capture:

  ```powershell
  wsl.exe -d Ubuntu-24.04 -u root -- apt-cache policy cockpit cockpit-machines libvirt-daemon-system libvirt-daemon-driver-qemu libvirt-clients libvirt-dbus qemu-system-x86 qemu-utils ovmf swtpm swtpm-tools dnsmasq iptables netcat-openbsd
  wsl.exe -d Ubuntu-24.04 -u root -- virsh version
  wsl.exe -d Ubuntu-24.04 -u root -- cockpit-bridge --version
  ```

  Enable the services that exist on this Ubuntu package layout. If `libvirtd` exists, enable it; if modular sockets exist, enable `virtqemud.socket`, `virtnetworkd.socket`, `virtstoraged.socket`, `virtlogd.socket` and `virtlockd.socket` as available. Never mask or remove a daemon to force the other layout. Verify:

  ```powershell
  wsl.exe -d Ubuntu-24.04 -u root -- systemctl --failed
  wsl.exe -d Ubuntu-24.04 -u root -- virsh -c qemu:///system uri
  wsl.exe -d Ubuntu-24.04 -u root -- virsh -c qemu:///system list --all
  ```

- [ ] **Step 4: Implement `configure` with a local-only Cockpit socket drop-in.**

  Create `/etc/systemd/system/cockpit.socket.d/listen.conf` inside WSL with:

  ```ini
  [Socket]
  ListenStream=
  ListenStream=127.0.0.1:9090
  ```

  Then run:

  ```powershell
  wsl.exe -d Ubuntu-24.04 -u root -- systemctl daemon-reload
  wsl.exe -d Ubuntu-24.04 -u root -- systemctl enable --now cockpit.socket
  wsl.exe -d Ubuntu-24.04 -u root -- ss -ltnp
  Test-NetConnection 127.0.0.1 -Port 9090
  Invoke-WebRequest 'https://127.0.0.1:9090' -SkipCertificateCheck -UseBasicParsing
  ```

  Fail if `ss` shows `0.0.0.0:9090`, `[::]:9090`, or any listener other than `127.0.0.1:9090`. Do not modify `start-quick-tunnel.ps1` to proxy Cockpit. Print `COCKPIT_LOCAL_ONLY_OK` only after both WSL and Windows checks pass.

- [ ] **Step 5: Add `status` output that distinguishes legacy and libvirt owners.**

  `status` must print systemd state, Cockpit listener, libvirt URI, network/pool/domain state, `virsh domblklist`, dedicated `guacamole-vm-windows11-libvirt-tpm.service` and socket state, legacy `guacamole-vm-windows11.service` state, legacy TPM service state, Docker Compose state, and the H-backed VHDX check. It must never print the Windows password or database password. The output must include one of `LIBVIRT_DOMAIN_WINDOWS11_ACTIVE`, `LIBVIRT_DOMAIN_WINDOWS11_INACTIVE`, or `LIBVIRT_DOMAIN_WINDOWS11_MISSING`.

- [ ] **Step 6: Test Task 2 before continuing.**

  Run:

  ```powershell
  Set-Location H:\RemoteWorkspaces\guacamole-client\deploy-local
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\libvirt.ps1 preflight
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\libvirt.ps1 install
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\libvirt.ps1 configure
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\libvirt.ps1 status
  ```

  Expected: package installation is idempotent, `qemu:///system` works, Cockpit answers on localhost only, the existing TPM state is unchanged, and no VM/domain or swtpm owner state was modified.

---

### Task 3: Define H-backed libvirt network, storage pool and Docker route

**Files:**
- Modify: `deploy-local/libvirt.ps1` (`network`, `storage`, `connect-guacamole` actions)
- Use: `deploy-local/libvirt/networks/guac-nat.xml`

**Interfaces:**
- Consumes: Task 1 network XML and Task 2 `qemu:///system` connection.
- Produces: active/autostart `guac-nat`, active/autostart `guacamole-vms`, and repeatable Docker-to-`virbr-guac` forwarding used by `recover-after-rollback.ps1` and `test-libvirt.ps1`.

- [ ] **Step 1: Add idempotent network define/start logic.**

  The action must inspect `virsh net-list --all` first. If `guac-nat` is absent, run `virsh net-define` against the canonical XML. If it exists, compare `virsh net-dumpxml guac-nat` to the required name/bridge/subnet/MAC/IP and stop with a diff before changing a conflicting network. Then run:

  ```bash
  virsh -c qemu:///system net-autostart guac-nat
  virsh -c qemu:///system net-start guac-nat
  virsh -c qemu:///system net-info guac-nat
  virsh -c qemu:///system net-dhcp-leases guac-nat
  ```

  Do not attach a physical NIC to `virbr-guac` and do not reuse the libvirt `default` network.

- [ ] **Step 2: Add the H-backed storage pool.**

  Create `/var/lib/guacamole-vms` inside WSL, then define and start a dir pool named `guacamole-vms`:

  ```bash
  mkdir -p /var/lib/guacamole-vms
  virsh -c qemu:///system pool-define-as guacamole-vms dir --target /var/lib/guacamole-vms
  virsh -c qemu:///system pool-autostart guacamole-vms
  virsh -c qemu:///system pool-start guacamole-vms
  virsh -c qemu:///system pool-info guacamole-vms
  ```

  If the pool already exists, compare its target and refuse to use a target other than `/var/lib/guacamole-vms`. Do not change the default libvirt pool silently.

- [ ] **Step 3: Add Docker-to-libvirt forwarding as a repeatable recovery action.**

  Discover the Compose network and bridge instead of hardcoding its generated bridge name:

  ```bash
  docker network inspect guacamole-local_default --format '{{.Id}} {{(index .IPAM.Config 0).Subnet}} {{(index .IPAM.Config 0).Gateway}}'
  ip -o link show | grep 'br-'
  ip route show 192.168.250.0/24
  ```

  Enable forwarding and install only the required stateful rules for the discovered Docker bridge:

  ```bash
  sysctl -w net.ipv4.ip_forward=1
  iptables -I FORWARD -i <docker-bridge> -o virbr-guac -p tcp -d 192.168.250.0/24 --dport 3389 -j ACCEPT
  iptables -I FORWARD -i virbr-guac -o <docker-bridge> -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
  ```

  The implementation must substitute the discovered bridge in PowerShell/WSL code, check that `iptables -C` does not already find each rule, and avoid printing a placeholder into the command. Do not open `192.168.250.0/24` on the Windows LAN or add a broad `ACCEPT` rule for all ports.

- [ ] **Step 4: Prove the route from the Compose network, not only from WSL host.**

  After `windows11` is running, run:

  ```powershell
  wsl.exe -d Ubuntu-24.04 -u root -- sh -lc "docker run --rm --network guacamole-local_default busybox:1.36 sh -c 'nc -zvw5 192.168.250.11 3389'"
  ```

  Expected: TCP connection succeeds. If WSL host can connect but this command fails, stop at this task and record route/firewall evidence; do not silently switch to a host proxy.

- [ ] **Step 5: Test Task 3.**

  Run:

  ```powershell
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\libvirt.ps1 network
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\libvirt.ps1 storage
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\libvirt.ps1 connect-guacamole
  wsl.exe -d Ubuntu-24.04 -u root -- virsh -c qemu:///system net-list --all
  wsl.exe -d Ubuntu-24.04 -u root -- virsh -c qemu:///system pool-list --all
  ```

  Expected: `guac-nat` and `guacamole-vms` are active/autostart, pool target is inside WSL ext4, and route rules are idempotent.

---

### Task 4: Implement consistent backup/export checkpointing

**Files:**
- Modify: `deploy-local/libvirt.ps1` (`backup`, `export` actions)
- Modify: `deploy-local/export-maintenance-bundle.ps1`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: Existing Guacamole Compose volume/secret, Windows disk/NVRAM/TPM, Task 1 XML and Task 3 network/pool.
- Produces: timestamped H-backed checkpoint under `runtime\backups\windows11-pre-libvirt-<timestamp>`, redacted inventory, package manifest and verified maintenance bundle.

- [ ] **Step 1: Add a backup action that refuses a live qcow2/TPM copy.**

  The action must first run:

  ```powershell
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\vm-windows11\windows11.ps1 stop -AllowLegacyQemuOwner
  wsl.exe -d Ubuntu-24.04 -u root -- systemctl is-active guacamole-vm-windows11
  wsl.exe -d Ubuntu-24.04 -u root -- systemctl is-active guacamole-vm-windows11-tpm
  wsl.exe -d Ubuntu-24.04 -u root -- pgrep -af 'qemu-system-x86_64.*windows11'
  ```

  Continue only when both units are inactive and no matching process remains. Then run:

  ```bash
  qemu-img check --read-only /var/lib/guacamole-vm-windows11/windows11.qcow2
  qemu-img info /var/lib/guacamole-vm-windows11/windows11.qcow2
  sha256sum /var/lib/guacamole-vm-windows11/windows11.qcow2
  cp --reflink=auto /var/lib/guacamole-vm-windows11/OVMF_VARS_4M.ms.fd <checkpoint>/OVMF_VARS_4M.ms.fd
  cp -a /var/lib/guacamole-vm-windows11/tpm <checkpoint>/tpm
  ```

  Copy the qcow2 only when the user explicitly asks for a full disk checkpoint and after `qemu-img check`; otherwise record its path/hash and keep the active disk in place to avoid a multi-hour copy. Store the PostgreSQL dump and Guacamole connection/user/group inventory in the same checkpoint without passwords.

- [ ] **Step 2: Define the redacted Guacamole inventory.**

  Query and save only connection ID/name/protocol/hostname/port and permission rows. Exclude rows whose `parameter_name` is `password`, `private-key`, `passphrase`, `token`, or any secret value. Keep the inventory outside the public maintenance ZIP under H-backed `runtime\backups` with restricted ACL.

- [ ] **Step 3: Add XML/package/state exports.**

  Save:

  ```powershell
  wsl.exe -d Ubuntu-24.04 -u root -- virsh -c qemu:///system net-dumpxml guac-nat
  wsl.exe -d Ubuntu-24.04 -u root -- virsh -c qemu:///system pool-dumpxml guacamole-vms
  wsl.exe -d Ubuntu-24.04 -u root -- virsh -c qemu:///system dumpxml windows11
  wsl.exe -d Ubuntu-24.04 -u root -- dpkg-query -W cockpit cockpit-machines libvirt-daemon-system libvirt-daemon-driver-qemu qemu-system-x86 qemu-utils ovmf swtpm swtpm-tools dnsmasq iptables netcat-openbsd
  wsl.exe -d Ubuntu-24.04 -u root -- systemctl cat guacamole-vm-windows11 guacamole-vm-windows11-tpm
  ```

  XML exports may contain absolute paths but must not contain passwords or tunnel URLs. Export a manifest with SHA-256 for every copied state file.

- [ ] **Step 4: Fix maintenance bundle selection and ignore generated exports.**

  In `export-maintenance-bundle.ps1`, make the exclusion predicate parenthesized and explicit: exclude `deploy-local\data`, `deploy-local\secrets`, all `runtime`, all files matching `password|secret|token|credential|\.env`, and generated libvirt export directories; include `libvirt\*.xml`, all new scripts and docs. Reopen the resulting ZIP and fail if any forbidden path is present.

  In `.gitignore`, add only generated libvirt export/temporary paths, for example:

  ```gitignore
  deploy-local/libvirt/exports/
  deploy-local/libvirt/*.generated.xml
  ```

  Keep canonical XML templates and scripts tracked.

- [ ] **Step 5: Test Task 4.**

  Run:

  ```powershell
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\libvirt.ps1 backup
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\export-maintenance-bundle.ps1
  $latest = Get-ChildItem H:\RemoteWorkspaces\guacamole-client\runtime\backups\guacamole-maintenance-*.zip | Sort-Object LastWriteTime -Descending | Select-Object -First 1
  Add-Type -AssemblyName System.IO.Compression.FileSystem
  $z = [IO.Compression.ZipFile]::OpenRead($latest.FullName); try { $z.Entries.FullName } finally { $z.Dispose() }
  ```

  Expected: checkpoint exists on H:, qcow2 check succeeded while stopped, ZIP contains scripts/XML/docs, and ZIP contains no `runtime`, `secrets`, `password`, `token`, or database data path.

---

### Task 5: Migrate Windows 11 from transient QEMU to persistent libvirt

**Files:**
- Create: `deploy-local/migrate-windows11-to-libvirt.ps1`
- Modify: `deploy-local/vm-windows11/windows11.ps1`
- Modify: `deploy-local/vm-windows11/README.md`

**Interfaces:**
- Consumes: Task 1 domain template, Task 2 package/helper, Task 3 network/pool, Task 4 checkpoint, existing qcow2/NVRAM/TPM/password, and the dedicated systemd swtpm contract.
- Produces: persistent domain `windows11`, dedicated `guacamole-vm-windows11-libvirt-tpm.service` using existing H-backed TPM state and `/run/guacamole-vm-windows11/swtpm.sock`, marker `runtime\vm-windows11\libvirt-cutover.marker`, and migration output tokens `LIBVIRT_MIGRATION_OK` or a rollback-ready failure.

- [ ] **Step 1: Add explicit migration action contract.**

  `migrate-windows11-to-libvirt.ps1` must accept:

  ```powershell
  [ValidateSet('preflight','backup','define','smoke-test','cutover','rollback','status')]
  [string]$Action = 'status'
  [int]$TimeoutSeconds = 180
  ```

  `preflight`, `backup`, `define`, `smoke-test`, `cutover`, and `rollback` must be separate actions so a failed phase leaves evidence and does not silently continue. `cutover` may run only after successful current-run `backup` and `smoke-test`; it must not delete the disk.

- [ ] **Step 2: Add the preflight gate that proves Windows is installed and QEMU is disk-only.**

  Run:

  ```powershell
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\vm-windows11\windows11.ps1 status
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\vm-windows11\test-rdp.ps1
  wsl.exe -d Ubuntu-24.04 -u root -- systemctl cat guacamole-vm-windows11
  wsl.exe -d Ubuntu-24.04 -u root -- systemctl cat guacamole-vm-windows11-tpm
  wsl.exe -d Ubuntu-24.04 -u root -- systemctl is-active guacamole-vm-windows11-libvirt-tpm.service
  ```

  Refuse migration if the Windows test is not `WIN11_RDP_AUTH_OK`, if the QEMU command contains either installer ISO, if `install-finished.marker` is missing, or if the dedicated systemd swtpm unit/socket is already active before the legacy owner has been stopped. The marker alone is insufficient evidence.

- [ ] **Step 3: Add safe legacy-owner shutdown and checkpoint copying.**

  Run the legacy stop path only before the marker exists:

  ```powershell
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\vm-windows11\windows11.ps1 stop -AllowLegacyQemuOwner -GracefulStopTimeoutSeconds 180
  ```

  Wait until QEMU and legacy transient swtpm are stopped, verify no qcow2/TPM process remains, run `qemu-img check --read-only`, and copy only UEFI NVRAM to a new active libvirt state directory inside `/var/lib/guacamole-vm-windows11/libvirt/`. Keep the existing TPM state at `/var/lib/guacamole-vm-windows11/tpm` and the timestamped checkpoint untouched; do not create a second TPM directory or run `swtpm_setup`. Create the dedicated `guacamole-vm-windows11-libvirt-tpm.service` to run swtpm with `--tpmstate dir=/var/lib/guacamole-vm-windows11/tpm` and `--ctrl type=unixio,path=/run/guacamole-vm-windows11/swtpm.sock`, using a runtime directory under `/run`. Use `rsync -a --delete` only on the new NVRAM directory after verifying the source and destination are distinct; never use it against the source directory.

- [ ] **Step 4: Render and validate the domain XML before define.**

  Replace:

  ```text
  __W11_DISK_PATH__   -> /var/lib/guacamole-vm-windows11/windows11.qcow2
  __W11_NVRAM_PATH__  -> /var/lib/guacamole-vm-windows11/libvirt/OVMF_VARS_4M.ms.fd
  __W11_TPM_SOCKET__  -> /run/guacamole-vm-windows11/swtpm.sock
  ```

  Start the dedicated systemd swtpm unit with the existing H-backed state, verify the socket is present and accessible to libvirt, and check `libvirt-qemu` can read disk and NVRAM. Use the actual account/group returned by `getent passwd libvirt-qemu` and `getent group kvm` rather than assuming a distro UID. Then run:

  ```powershell
  wsl.exe -d Ubuntu-24.04 -u root -- virsh -c qemu:///system define /var/lib/guacamole-vm-windows11/libvirt/windows11.generated.xml
  wsl.exe -d Ubuntu-24.04 -u root -- virsh -c qemu:///system dominfo windows11
  wsl.exe -d Ubuntu-24.04 -u root -- virsh -c qemu:///system dumpxml windows11
  ```

  Refuse `define` if `virsh` rejects the external TPM Unix socket, if the swtpm socket is absent, if domain XML has an ISO, if disk/NVRAM paths resolve outside the intended WSL directories, or if MAC/network differ from the contract. Do not replace the external TPM socket with another backend or fresh TPM when this gate fails.

- [ ] **Step 5: Start and verify the domain before changing Guacamole.**

  Run:

  ```powershell
  wsl.exe -d Ubuntu-24.04 -u root -- virsh -c qemu:///system net-start guac-nat
  wsl.exe -d Ubuntu-24.04 -u root -- virsh -c qemu:///system pool-start guacamole-vms
  wsl.exe -d Ubuntu-24.04 -u root -- systemctl start guacamole-vm-windows11-libvirt-tpm.service
  wsl.exe -d Ubuntu-24.04 -u root -- test -S /run/guacamole-vm-windows11/swtpm.sock
  wsl.exe -d Ubuntu-24.04 -u root -- virsh -c qemu:///system start windows11
  wsl.exe -d Ubuntu-24.04 -u root -- virsh -c qemu:///system domstate windows11
  wsl.exe -d Ubuntu-24.04 -u root -- virsh -c qemu:///system net-dhcp-leases guac-nat
  wsl.exe -d Ubuntu-24.04 -u root -- virsh -c qemu:///system domblklist windows11
  ```

  Expected: persistent `running`, dedicated swtpm unit active with socket `/run/guacamole-vm-windows11/swtpm.sock`, DHCP lease `192.168.250.11`, disk is the existing qcow2, no installer media is attached, and no legacy QEMU/TPM unit is active. RDP must respond at `192.168.250.11:3389` before the next step.

- [ ] **Step 6: Update Guacamole target without changing ID or permissions.**

  Before the update, save a redacted inventory of the connection named exactly `Windows 11`: connection ID, name, protocol, user/group permission rows, hostname and port. Through the existing PostgreSQL Compose service, update only `hostname` and `port` parameters in one transaction:

  ```sql
  BEGIN;
  -- Select exactly one connection named Windows 11; abort if the count is not 1.
  -- UPDATE its hostname to 192.168.250.11 and port to 3389.
  -- INSERT the parameter only when that parameter is absent.
  -- Do not touch guacamole_connection, guacamole_connection_permission,
  -- guacamole_user_permission, guacamole_system_permission, or password rows.
  COMMIT;
  ```

  The implementation must execute this through `docker compose exec -T postgres`, obtain the database password from the existing mounted secret inside the container, and never print the command including the password. Afterward query the same inventory and assert connection ID/name and all permission rows are byte-for-byte unchanged; only hostname/port may differ.

- [ ] **Step 7: Add the cutover marker and ownership guard.**

  Only after a real Guacamole RDP session to the migrated VM succeeds, create `runtime\vm-windows11\libvirt-cutover.marker` containing a timestamp, domain name and source checkpoint path but no secret. Modify `windows11.ps1` so `start`, `finish-install`, and legacy `stop` fail with `LEGACY_QEMU_OWNER_BLOCKED_AFTER_LIBVIRT_CUTOVER` when the marker exists unless `-AllowLegacyQemuOwner` is explicitly supplied. The recovery script must never pass that flag.

- [ ] **Step 8: Test migration before claiming success.**

  Run:

  ```powershell
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\test-libvirt.ps1 -Mode migration
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\migrate-windows11-to-libvirt.ps1 status
  ```

  Expected tokens: `LIBVIRT_DOMAIN_PERSISTENT_OK`, `LIBVIRT_TPM_OK`, `LIBVIRT_NVRAM_OK`, `LIBVIRT_RDP_OK`, `GUAC_CONNECTION_ID_PRESERVED`, `GUAC_PERMISSIONS_PRESERVED`, and `LIBVIRT_MIGRATION_OK`. If any gate fails, do not create the marker and continue to Task 8 rollback.

---

### Task 6: Switch recovery and one-click startup to libvirt ownership

**Files:**
- Modify: `deploy-local/recover-after-rollback.ps1`
- Modify: `deploy-local/START-REMOTE.cmd`
- Modify: `deploy-local/README.md`
- Modify: `deploy-local/libvirt.ps1`

**Interfaces:**
- Consumes: Task 2 helper actions and Task 3 network/storage; existing `start-local.ps1` still owns WSL/Docker/Compose and optional Ubuntu demo.
- Produces: idempotent `start/status/stop` behavior where Windows 11 is started by `virsh`, not `windows11.ps1`.

- [ ] **Step 1: Replace Windows legacy calls in recovery.**

  Remove the `$WindowsScript` invocation from the normal `start`, `status`, and `stop` branches. The new sequence must be:

  ```text
  start: start-local.ps1 start -> libvirt.ps1 network -> libvirt.ps1 storage -> libvirt.ps1 connect-guacamole -> libvirt.ps1 start -> start-quick-tunnel.ps1 start
  status: start-local.ps1 status -> libvirt.ps1 status -> start-quick-tunnel.ps1 status
  stop: start-quick-tunnel.ps1 stop -> libvirt.ps1 stop -> start-local.ps1 stop
  ```

  `libvirt.ps1 start` must start `guac-nat`, `guacamole-vms`, and `windows11` through `virsh`; it must refuse to start a legacy transient QEMU service or a domain whose XML still has installer ISO media. Keep existing timeout handling and fail fast on each nonzero child exit.

- [ ] **Step 2: Preserve WSL H-backed recovery behavior.**

  Do not remove `start-local.ps1`'s `ext4.vhdx` import-in-place, keepalive, systemd wait, Docker startup, or Compose startup. `recover-after-rollback.ps1` must still work when C: rollback removed the WSL registration, provided `runtime\ubuntu\ext4.vhdx` remains on H:. Add the local Cockpit URL `https://127.0.0.1:9090` to status output only; do not add it to Quick Tunnel.

- [ ] **Step 3: Update the one-click CMD output.**

  Change `START-REMOTE.cmd` title and messages to say `Guacamole + libvirt Windows 11 + Cloudflare Quick Tunnel`, and print that Cockpit is local at `https://127.0.0.1:9090`. The CMD must still call only `recover-after-rollback.ps1 start` and `status`; do not duplicate lifecycle commands in the batch file.

- [ ] **Step 4: Test recovery ownership.**

  Run:

  ```powershell
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\recover-after-rollback.ps1 stop
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\recover-after-rollback.ps1 start
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\recover-after-rollback.ps1 status
  wsl.exe -d Ubuntu-24.04 -u root -- systemctl is-active guacamole-vm-windows11
  wsl.exe -d Ubuntu-24.04 -u root -- virsh -c qemu:///system domstate windows11
  ```

  Expected: libvirt domain is the only Windows owner; `systemctl is-active guacamole-vm-windows11` is inactive/not-found, no legacy TPM service is active, Compose and tunnel remain healthy, and all state remains under H/WSL ext4.

---

### Task 7: Add read-only verification gates and maintenance checks

**Files:**
- Create: `deploy-local/test-libvirt.ps1`
- Modify: `deploy-local/libvirt.ps1` if helper functions need to be shared

**Interfaces:**
- Consumes: Task 2–6 output and current WSL/Docker/libvirt state.
- Produces: deterministic verification modes `preflight`, `migration`, `recovery`, `new-vm`, and `all`; no state mutation and no secret output.

- [ ] **Step 1: Define test parameters and token output.**

  Accept:

  ```powershell
  [ValidateSet('preflight','migration','recovery','new-vm','all')]
  [string]$Mode = 'all'
  [string]$DomainName = 'windows11'
  ```

  Stop on failed assertions and print one unique token per passed gate. Do not call `virsh start`, `virsh destroy`, `docker compose down`, or any destructive command.

- [ ] **Step 2: Implement Gate A/B checks.**

  Verify:

  ```powershell
  wsl.exe --list --verbose
  wsl.exe -d Ubuntu-24.04 -u root -- test -e /dev/kvm
  Test-Path -LiteralPath 'H:\RemoteWorkspaces\guacamole-client\runtime\ubuntu\ext4.vhdx'
  wsl.exe -d Ubuntu-24.04 -u root -- virsh -c qemu:///system uri
  wsl.exe -d Ubuntu-24.04 -u root -- ss -ltnp
  Test-NetConnection 127.0.0.1 -Port 9090
  Invoke-WebRequest 'https://127.0.0.1:9090' -SkipCertificateCheck -UseBasicParsing
  Invoke-WebRequest 'http://127.0.0.1:8080/guacamole/' -UseBasicParsing
  ```

  Assert Cockpit listener is exactly `127.0.0.1:9090`, Guacamole local HTTP is 200, and Quick Tunnel command/log references only port 8080 when a tunnel is running. Emit `HOST_STORAGE_OK`, `COCKPIT_LOCAL_ONLY_OK`, and `GUACAMOLE_LOCAL_HTTP_OK`.

- [ ] **Step 3: Implement Gate C/D checks.**

  Verify:

  ```powershell
  wsl.exe -d Ubuntu-24.04 -u root -- virsh -c qemu:///system net-list --all
  wsl.exe -d Ubuntu-24.04 -u root -- virsh -c qemu:///system pool-list --all
  wsl.exe -d Ubuntu-24.04 -u root -- virsh -c qemu:///system list --all
  wsl.exe -d Ubuntu-24.04 -u root -- virsh -c qemu:///system dumpxml windows11
  wsl.exe -d Ubuntu-24.04 -u root -- virsh -c qemu:///system domblklist windows11
  wsl.exe -d Ubuntu-24.04 -u root -- virsh -c qemu:///system net-dhcp-leases guac-nat
  wsl.exe -d Ubuntu-24.04 -u root -- sh -lc "docker run --rm --network guacamole-local_default busybox:1.36 sh -c 'nc -zvw5 192.168.250.11 3389'"
  ```

  Assert `windows11` is persistent, network/pool are active/autostart, XML has OVMF/TPM2 external socket/e1000e/fixed MAC, the dedicated swtpm unit owns `/run/guacamole-vm-windows11/swtpm.sock`, `domblklist` has the existing qcow2 and no ISO, lease is `.11`, and Compose namespace reaches RDP. Emit `LIBVIRT_STATE_OK`, `WINDOWS11_RDP_ROUTE_OK`.

- [ ] **Step 4: Implement Gate E/F checks.**

  Query Guacamole inventory through PostgreSQL without passwords and assert connection name/ID/target and permission rows; assert legacy service units are inactive/not-found; then run:

  ```powershell
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\recover-after-rollback.ps1 status
  ```

  Emit `GUAC_PERMISSION_INVENTORY_OK`, `RECOVERY_OWNER_OK`. A real authenticated Guacamole RDP session remains a required manual check and must be recorded in the final handoff; a port listener alone is not sufficient.

- [ ] **Step 5: Test all modes.**

  Run `test-libvirt.ps1 -Mode preflight` before migration, `-Mode migration` after cutover, and `-Mode recovery` after one stop/start cycle. Expected failures must include an actionable command and must not modify state.

---

### Task 8: Add new VM creation workflow for Cockpit and CLI recovery

**Files:**
- Create: `deploy-local/new-libvirt-vm.ps1`
- Modify: `deploy-local/libvirt/README.md`
- Modify: `deploy-local/README.md`
- Modify: `deploy-local/RUNBOOK.md`

**Interfaces:**
- Consumes: Task 1 `guac-nat`, Task 2 libvirt packages, Task 3 `guacamole-vms` pool and Docker route.
- Produces: Cockpit UI procedure and deterministic CLI action with parameters `-Name`, `-IsoPath`, `-MemoryMiB`, `-Vcpus`, `-DiskGiB`, optional `-Mac`, optional `-Ip`.

- [ ] **Step 1: Document the primary Cockpit UI procedure.**

  Add exact steps:

  1. Open `https://127.0.0.1:9090` from the Windows host and accept the first self-signed certificate warning.
  2. Sign in with the host administrator, open `Virtual Machines` on system connection, and choose `Create VM`.
  3. Select an ISO under `/mnt/h/RemoteWorkspaces/guacamole-client/runtime/iso/`; record `Get-FileHash` before use.
  4. Choose UEFI and TPM 2.0 for Windows 11, or UEFI as required by the Windows 10 ISO; set capacity without exceeding available RAM/CPU.
  5. Choose pool `guacamole-vms`, so disk is under `/var/lib/guacamole-vms` inside WSL `ext4.vhdx`.
  6. Choose network `guac-nat`, keep a unique MAC, and add a DHCP reservation in the canonical network XML before expecting a stable IP.
  7. Install Windows, enable RDP and firewall inside the guest, and test from both WSL and the Compose network.
  8. Add a Guacamole RDP connection to the reserved IP port 3389 and assign it under `Settings → Users/Groups → Permissions`.

  State that Cockpit is VM administration, Guacamole is end-user authorization, and no Cockpit public tunnel is allowed.

- [ ] **Step 2: Implement safe CLI creation.**

  Validate `-Name` against `^[a-zA-Z0-9][a-zA-Z0-9._-]{0,62}$`, require `-IsoPath` to resolve under `H:\RemoteWorkspaces\guacamole-client\runtime\iso`, reject an existing domain/disk, and require `-DiskGiB >= 64`, `-Vcpus >= 1`, `-MemoryMiB >= 2048`. Choose the next unused MAC/IP beginning at `52:54:00:11:11:02` / `192.168.250.12`, unless explicit values pass uniqueness checks.

  Create only inside the H-backed pool:

  ```bash
  qemu-img create -f qcow2 /var/lib/guacamole-vms/<name>.qcow2 <disk-size>G
  virsh -c qemu:///system define /var/lib/guacamole-vms/<name>.generated.xml
  virsh -c qemu:///system pool-refresh guacamole-vms
  virsh -c qemu:///system dominfo <name>
  ```

  The generated domain must use `guac-nat`, unique MAC, UEFI for Windows, no password, and no public listener. Print `LIBVIRT_VM_CREATED name=<name> mac=<mac> ip=<ip>` without any secret. Do not auto-start a new production VM until the operator has verified ISO hash and capacity.

- [ ] **Step 3: Add post-create Guacamole/permission instructions.**

  Document target inventory fields, connection creation, certificate setting, and user/group assignment. Require the operator to save VM name/MAC/IP/ISO hash and assigned users in a redacted H-backed maintenance artifact; never save guest passwords in that artifact.

- [ ] **Step 4: Test Task 8 with an isolated disposable VM.**

  Use a small test ISO or a test domain name, verify:

  ```powershell
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\new-libvirt-vm.ps1 -Name windows-test-01 -IsoPath 'H:\RemoteWorkspaces\guacamole-client\runtime\iso\Windows11_23H2_UEFI.iso' -MemoryMiB 4096 -Vcpus 2 -DiskGiB 64
  wsl.exe -d Ubuntu-24.04 -u root -- virsh -c qemu:///system domblklist windows-test-01
  wsl.exe -d Ubuntu-24.04 -u root -- virsh -c qemu:///system dumpxml windows-test-01
  ```

  Expected: disk path begins `/var/lib/guacamole-vms/`, network is `guac-nat`, MAC/IP are unique, and no secret is emitted. Remove only this test domain through Cockpit or `virsh undefine` after recording its XML; never use the test cleanup command against `windows11`.

---

### Task 9: Implement rollback and legacy-owner recovery

**Files:**
- Modify: `deploy-local/migrate-windows11-to-libvirt.ps1`
- Modify: `deploy-local/vm-windows11/windows11.ps1`
- Modify: `deploy-local/RUNBOOK.md`

**Interfaces:**
- Consumes: Task 4 checkpoint, Task 5 marker/domain, Task 6 recovery entrypoint.
- Produces: deterministic rollback path that restores the pre-libvirt owner without deleting the qcow2 or creating a TPM.

- [ ] **Step 1: Implement rollback preconditions.**

  Refuse rollback if `virsh domstate windows11` is `running` or if the dedicated `guacamole-vm-windows11-libvirt-tpm.service` is active or its socket still exists. Stop the domain and then stop the dedicated swtpm unit gracefully first:

  ```powershell
  wsl.exe -d Ubuntu-24.04 -u root -- virsh -c qemu:///system shutdown windows11
  wsl.exe -d Ubuntu-24.04 -u root -- virsh -c qemu:///system domstate windows11
  wsl.exe -d Ubuntu-24.04 -u root -- systemctl stop guacamole-vm-windows11-libvirt-tpm.service
  ```

  Use `virsh destroy windows11` only when shutdown does not complete and record the reason in the checkpoint. Export XML before `virsh undefine`; do not use `virsh undefine --nvram` because the NVRAM checkpoint is needed for recovery.

- [ ] **Step 2: Restore the exact disk/NVRAM/TPM checkpoint.**

  Verify checkpoint manifest and hashes, restore the matching NVRAM and TPM directory into the legacy paths, and check `qemu-img check --read-only` before starting QEMU. Do not mix disk from one checkpoint with NVRAM/TPM from another. Keep the libvirt copies for diagnosis until the legacy RDP smoke test passes.

- [ ] **Step 3: Remove marker only at the ownership boundary.**

  After the libvirt domain is undefined, the dedicated systemd swtpm unit is stopped, its runtime socket is gone, and legacy paths are restored, remove `runtime\vm-windows11\libvirt-cutover.marker`. Then run the explicitly authorized legacy owner:

  ```powershell
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\vm-windows11\windows11.ps1 start -AllowLegacyQemuOwner
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\vm-windows11\test-rdp.ps1
  ```

  Only after `WIN11_RDP_AUTH_OK` may the Guacamole connection be returned to `172.18.0.1:3391`; preserve ID and permission rows.

- [ ] **Step 4: Test rollback on the real domain only with a fresh checkpoint.**

  Do not delete the production disk. Perform one controlled stop/restore/start test, then immediately stop legacy QEMU and restore the libvirt owner if the test passes. Record output from `virsh list --all`, `systemctl status guacamole-vm-windows11*`, `qemu-img check`, RDP auth, Guacamole target and permissions. Expected token: `LEGACY_ROLLBACK_RDP_OK`.

---

### Task 10: Rewrite documentation and maintenance/update procedures

**Files:**
- Modify: `deploy-local/README.md`
- Modify: `deploy-local/RUNBOOK.md`
- Modify: `deploy-local/vm-windows11/README.md`
- Modify: `deploy-local/libvirt/README.md`

**Interfaces:**
- Consumes: All scripts and tokens from Tasks 1–9.
- Produces: agent-readable documentation that can restore from nothing, operate daily, create VM, assign users, update packages and recover from each listed fault.

- [ ] **Step 1: Update `deploy-local/README.md` with the final topology.**

  Include this diagram and the exact local URLs:

  ```text
  Windows host
  └── WSL2 Ubuntu-24.04 on H:\...\runtime\ubuntu\ext4.vhdx
      ├── Docker: Guacamole / guacd / PostgreSQL -> 127.0.0.1:8080
      ├── Cockpit + libvirt qemu:///system -> 127.0.0.1:9090 only
      └── guac-nat 192.168.250.0/24 -> windows11 192.168.250.11:3389
  Cloudflare Quick Tunnel -> http://127.0.0.1:8080 only
  ```

  Show one-click and command-line starts:

  ```powershell
  H:\RemoteWorkspaces\guacamole-client\deploy-local\START-REMOTE.cmd
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File H:\RemoteWorkspaces\guacamole-client\deploy-local\recover-after-rollback.ps1 start
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File H:\RemoteWorkspaces\guacamole-client\deploy-local\recover-after-rollback.ps1 status
  ```

- [ ] **Step 2: Add full bootstrap from nothing to `RUNBOOK.md`.**

  Cover in order: Windows virtualization/WSL prerequisites; clone to H; import/register `ext4.vhdx`; enable WSL systemd; install Docker/Compose; restore repo-local secrets/ISO/cloudflared from the H backup; start Guacamole; install exact apt packages; configure Cockpit local-only; define network/pool; migrate Windows 11 without reinstall; update Guacamole connection; set recovery owner; run all gates.

  Every section must include the exact command, expected output/token, and stop condition. State that a fresh clone does not contain ignored runtime data and that a full restore needs the H-backed VHDX (including /var/lib/guacamole-workspace/secrets), secret files, ISO and cloudflared binary in addition to the maintenance ZIP.

- [ ] **Step 3: Add daily operations, update and assign-user procedures.**

  Document Cockpit CPU/RAM/disk/start/stop/reboot/console, CLI equivalents, Windows Update verification, apt package update with backups, Guacamole version-coupled update of `guacamole` and `guacd`, connection creation at `192.168.250.11:3389`, and Settings → Users/Groups → Permissions assignment. Explicitly say never put passwords in descriptions or docs.

- [ ] **Step 4: Add the complete error table.**

  Include at least: Cockpit 9090 not listening or exposed publicly; missing Virtual Machines; `qemu:///system` failure; `/dev/kvm` absent; UEFI shell; TPM lock; BitLocker/recovery; Docker bridge cannot reach `virbr-guac`; DHCP IP changed; RDP auth failure; Guacamole still points to `172.18.0.1:3391`; user sees unauthorized machine; Quick Tunnel unavailable; VM storage on C; duplicate QEMU/libvirt owners; WSL registration lost after rollback. For each, list exact read-only commands and the safe next action.

- [ ] **Step 5: Add a maintenance reporting template.**

  Require every agent report separately: source/config read; commands and results; local HTTP/Cockpit/Guacamole; domain/storage/network/RDP; Guacamole permission/public verification; unverified guest/manual steps. Prohibit claiming completion from `virsh define` or a port listener alone.

---

### Task 11: Final verification-before-completion and handoff

**Files:**
- Modify only files listed above if a verification finding requires a narrow fix.
- Do not add generated runtime state or secrets to Git.

**Interfaces:**
- Consumes: Tasks 1–10 and current H-backed runtime.
- Produces: evidence-backed handoff with all gates and a clear residual-risk list.

- [ ] **Step 1: Run PowerShell parser checks for every deployment script.**

  ```powershell
  Set-Location H:\RemoteWorkspaces\guacamole-client
  Get-ChildItem .\deploy-local -Filter *.ps1 -Recurse | ForEach-Object {
      $errors = $null
      [System.Management.Automation.Language.Parser]::ParseFile($_.FullName, [ref]$null, [ref]$errors) | Out-Null
      if ($errors) { throw "Syntax error in $($_.FullName)" }
  }
  ```

  Expected: no parser errors.

- [ ] **Step 2: Run all read-only verification modes and recovery cycle.**

  ```powershell
  Set-Location H:\RemoteWorkspaces\guacamole-client\deploy-local
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\test-libvirt.ps1 -Mode preflight
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\test-libvirt.ps1 -Mode migration
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\recover-after-rollback.ps1 stop
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\recover-after-rollback.ps1 start
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\test-libvirt.ps1 -Mode recovery
  ```

  Expected: no legacy owner starts, libvirt network/storage/domain return, Compose and Quick Tunnel recover, and no state is recreated on C.

- [ ] **Step 3: Perform the two user-facing checks.**

  1. Open `https://127.0.0.1:9090` locally and verify Cockpit lists `windows11`, shows its UEFI/TPM/disk/network and can reboot it.
  2. Open the current Quick Tunnel Guacamole URL, authenticate as administrator and as an assigned non-admin user, start a real RDP session to Windows 11, verify clipboard/display, then close the session. Confirm an unassigned user cannot see the connection.

  Record URL/status without recording credentials or tokens.

- [ ] **Step 4: Run final storage/ownership assertions.**

  ```powershell
  wsl.exe -d Ubuntu-24.04 -u root -- virsh -c qemu:///system domblklist windows11
  wsl.exe -d Ubuntu-24.04 -u root -- virsh -c qemu:///system dumpxml windows11
  wsl.exe -d Ubuntu-24.04 -u root -- systemctl is-active guacamole-vm-windows11
  wsl.exe -d Ubuntu-24.04 -u root -- systemctl is-active guacamole-vm-windows11-tpm
  git -C H:\RemoteWorkspaces\guacamole-client status --short
  ```

  Expected: disk path is the existing H-backed WSL path, XML has no ISO/password/public listener, both legacy services are inactive/not-found, and `git status` shows only intentional source/docs changes. Do not commit.

- [ ] **Step 5: Produce the handoff summary.**

  State the exact files changed, package versions actually installed, backup checkpoint path, migration/cutover time, domain/network/MAC/IP, Guacamole ID/permission preservation, commands run and their outputs, real user-facing checks, and anything still requiring Windows guest interaction. If a gate failed, report the failure and leave the system at the last known safe owner; do not call the implementation complete.

## Rollback Summary

If any migration gate fails, keep the old QEMU owner stopped, preserve the checkpoint, and run the scripted rollback in Task 9. The only valid order is:

```text
stop libvirt domain -> confirm dedicated systemd swtpm stopped -> export/undefine domain
-> restore matching NVRAM + TPM checkpoint -> verify qcow2 read-only
-> remove cutover marker -> explicitly start legacy QEMU with -AllowLegacyQemuOwner
-> prove WIN11_RDP_AUTH_OK -> restore Guacamole target 172.18.0.1:3391
```

Never run legacy QEMU while `windows11` remains defined/running in libvirt, never create a replacement TPM to bypass a lock, never delete the qcow2, and never use `docker compose down -v` as a recovery step.

## Plan Self-Review

- **Spec coverage:** All spec phases 0–6 map to Tasks 2–6; new VM workflow maps to Task 8; backup/rollback maps to Tasks 4 and 9; required project files map to the File Map; verification gates A–G map to Task 7 and Task 11; common failures and maintenance map to Task 10.
- **Placeholder scan:** The only angle-bracket text is used in command examples to denote values discovered at runtime, and every such value has a preceding discovery command. There are no `TODO`, `TBD`, or deferred implementation steps.
- **Interface consistency:** `libvirt.ps1` actions are defined once in Task 2 and consumed by Tasks 3–6; `test-libvirt.ps1 -Mode` values are defined in Task 7 and consumed by Tasks 5 and 11; the marker and legacy flag are defined in Task 5 and used by Task 9.
- **Review focus coverage:** Ownership collision is tested in Tasks 5/6/9; UEFI/TPM pairing in Tasks 4/5/9; Docker routing in Tasks 3/7; H-backed storage in Tasks 3/7/8/11; recovery ownership in Tasks 5/6/7/11.
