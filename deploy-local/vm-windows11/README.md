# Windows 11 QEMU/KVM VM

This VM is separate from `vm-demo`. Its UEFI variables, TPM state, installer
answer ISO, and logs are under `runtime\vm-windows11` on H:. The writable
100 GiB disk is inside `/var/lib/guacamole-vm-windows11` in the WSL
`ext4.vhdx`, which is itself stored at `runtime\ubuntu\ext4.vhdx` on H:. The
Windows 11 ISO is read from `runtime\iso\Windows11_23H2_UEFI.iso`.

The VM uses 4 vCPUs, 8 GiB RAM, a sparse 100 GiB qcow2 disk, UEFI Secure Boot
firmware, and a persistent software TPM 2.0. QEMU uses the inbox-compatible
`e1000e` network adapter for the installer. The unattended answer file selects
image index 6 (Windows 11 Pro), creates the local `guacadmin` administrator,
enables Remote Desktop and its firewall rules, and disables sleep.

## Start the installation

After libvirt cutover, this script is a legacy rollback owner only. If
`runtime\vm-windows11\libvirt-cutover.marker` exists, `start`, `stop`, and
`finish-install` fail with `LEGACY_QEMU_OWNER_BLOCKED_AFTER_LIBVIRT_CUTOVER`.
An operator performing the documented rollback may pass
`-AllowLegacyQemuOwner` only after the libvirt domain and its external TPM
unit have been stopped and undefined.

Run from PowerShell:

```powershell
Set-Location H:\RemoteWorkspaces\guacamole-client\deploy-local\vm-windows11
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\windows11.ps1 start
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\windows11.ps1 status
```

The first start creates the disk, UEFI variables, TPM state, and unattended
ISO, then boots the official Windows ISO. The installer normally completes
without keyboard input; the script also sends the VNC space key repeatedly to
pass the ISO boot prompt. During installation connect Guacamole to VNC at:

- Host: `172.18.0.1`
- Port: `5901`

The QEMU VNC listener is only inside the WSL/Guacamole path. It is not a
public Windows listener. The generic Pro installation key in the answer file
only selects the edition; it does not activate Windows.

The generated Windows administrator password is stored only in:

`H:\RemoteWorkspaces\guacamole-client\deploy-local\secrets\windows11_password.txt`

The script restricts this file and the generated answer media to the current
Windows user, SYSTEM, and local Administrators. Do not put the password in
chat or a public connection description.

## Finish installation and use RDP

After Windows has reached the desktop and RDP responds, stop the VM and mark
the installation complete. This removes both CD images on the next start:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\windows11.ps1 stop
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\windows11.ps1 finish-install
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\windows11.ps1 start
```

For Guacamole, create an RDP connection with host `172.18.0.1`, port `3391`,
username `guacadmin`, and the password in the protected file above. The
Windows guest NAT forward is `3391 -> 3389`; it does not collide with the
Ubuntu demo's `3390` forward. Keep the VNC connection available for recovery
until RDP has been tested.

From PowerShell, test only the RDP transport and credentials without printing
the password:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\test-rdp.ps1
```

Success prints `WIN11_RDP_AUTH_OK`; before Windows has enabled RDP it prints a
connection failure token.

Use the same script for idempotent `status` and `stop` operations. Stopping
the VM also stops its TPM service; it does not delete the disk or TPM state.

## Prerequisites

Inside `Ubuntu-24.04` WSL, the script needs QEMU/KVM, OVMF, `swtpm`,
`swtpm-tools`, and `genisoimage`. The current local deployment has these
packages. If a rollback removes them, restore them with:

```powershell
wsl.exe -d Ubuntu-24.04 -u root -- apt-get update
wsl.exe -d Ubuntu-24.04 -u root -- apt-get install -y qemu-system-x86 ovmf swtpm swtpm-tools genisoimage
```

The host must expose `/dev/kvm` inside WSL. The script deliberately does not
touch the existing VMware VM or Ubuntu QEMU demo.

## Migration to Cockpit/libvirt

The approved migration is phase based and keeps the qcow2, UEFI variables, and
existing TPM state together:

```powershell
Set-Location H:\RemoteWorkspaces\guacamole-client\deploy-local
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\migrate-windows11-to-libvirt.ps1 preflight
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\migrate-windows11-to-libvirt.ps1 backup
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\migrate-windows11-to-libvirt.ps1 define
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\migrate-windows11-to-libvirt.ps1 smoke-test
```

`define` installs the external `swtpm` unit and defines the persistent
`windows11` domain on `qemu:///system`; it does not create a replacement TPM.
The smoke test waits for Docker-to-guest TCP 3389 and then checks RDP at
`192.168.250.11:3389`. Update Guacamole only after that check and one real
Guacamole RDP session. The cutover marker is the ownership boundary; after it
exists this legacy script is blocked unless rollback explicitly passes
`-AllowLegacyQemuOwner`.

### Rollback

Rollback is checkpoint based and must not be run as a casual stop command. It
validates the selected checkpoint's relative hash manifest, stops the libvirt
domain and dedicated TPM, stages the checkpoint, then undefines the domain
only after the state and owner gates pass. It restores the checkpoint NVRAM and
TPM while keeping the replaced state under per-run evidence, restores the
Windows 11 Guacamole target while preserving the connection ID and
permissions, starts the legacy owner with its explicit authorization flag, and
requires `WIN11_RDP_AUTH_OK` on `127.0.0.1:3391` before clearing the cutover
marker.

Use the checkpoint recorded in migration state:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\migrate-windows11-to-libvirt.ps1 rollback
```

After a cutover marker exists, rollback refuses by default. An emergency
rollback must name the authorization explicitly:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\migrate-windows11-to-libvirt.ps1 rollback -AllowRollbackAfterCutover
```

Rollback failures leave non-secret evidence under
`runtime\vm-windows11\libvirt-migration\rollback-*`; no disk or TPM state is
deleted. Do not remove that evidence or start the legacy owner manually while
the rollback is incomplete.
