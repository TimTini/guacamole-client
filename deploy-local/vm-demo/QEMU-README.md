# QEMU/KVM fallback

This is the fallback for the Ubuntu desktop VM when VMware Workstation cannot
start its VMX runtime. It uses QEMU/KVM inside the
`Ubuntu-24.04` WSL distribution and leaves the existing VMware VMDK/VMX files
untouched.

The QEMU overlay is created inside the Ubuntu WSL ext4 filesystem at:

`/var/lib/guacamole-vm-demo/ubuntu-24.04-guacamole-qemu.qcow2`

That ext4 filesystem is stored in the deployment runtime:

`runtime\ubuntu\ext4.vhdx`

Keeping the writable overlay there avoids QEMU write amplification through
`/mnt/h` DrvFS. The source cloud image and seed ISO remain read-only inputs.

The source cloud image, NoCloud seed ISO, rendered user-data, and random
password are the existing files from this directory. The overlay is sparse and
has a 16 GiB virtual size.

## Prerequisites

Inside Ubuntu WSL, install QEMU system emulation if it is not already present:

```bash
sudo apt-get update
sudo apt-get install -y qemu-system-x86
```

The script also requires `/dev/kvm`. It runs QEMU as a systemd transient
service, so the process is managed independently of the PowerShell terminal.

## Start, status, stop

Run from PowerShell; the Windows `qemu-system-x86_64` command is not required:

```powershell
Set-Location <REPO_ROOT>\deploy-local\vm-demo
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\qemu-demo.ps1 start
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\qemu-demo.ps1 status
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\qemu-demo.ps1 stop
```

`start` creates the overlay if needed, then runs:

- 2 vCPUs and 4 GiB RAM;
- KVM acceleration with `-cpu host`;
- the existing NoCloud seed ISO;
- QEMU user-mode NAT with TCP `3390` forwarded to guest RDP `3389`;
- headless display and a console log at `runtime\vm-demo\qemu-console.log`.

The forward binds inside WSL. It is not a public Internet listener; the
Windows/WSL NAT boundary and Guacamole's localhost-only web binding remain in
place. The VM has no bridged LAN interface in this fallback.

## Guacamole connection

After the first boot finishes installing XFCE and xrdp, create an RDP
connection in Guacamole with:

- Hostname: normally `172.18.0.1` from the Guacamole container;
- Port: `3390`;
- Username: `ubuntu`;
- Password: the contents of
  `runtime\vm-demo\vm-password.txt`.

If the Compose network uses a different host gateway, inspect it inside WSL:

```bash
docker network inspect guacamole-local_default
```

Use the network's gateway address as the Guacamole hostname. The QEMU NAT
forward remains TCP port `3390`.

After xrdp is ready, verify the forwarded RDP credentials without displaying
the password:

```powershell
.\test-rdp.ps1
```

The script runs `xfreerdp3` inside Ubuntu-24.04 WSL through a private PTY and
prints only a sanitized result token.

## Recovery and limitations

- The VM overlay and QEMU systemd unit live inside Ubuntu WSL. Back up the WSL
  `ext4.vhdx`, source image, seed ISO and generated runtime state together.
- The first boot needs apt network access. Wait for cloud-init to finish before
  trying RDP; inspect `qemu-console.log` if the VM does not respond.
- Verify the overlay location after recovery with:

  ```powershell
  wsl -d Ubuntu-24.04 -u root -- qemu-img info /var/lib/guacamole-vm-demo/ubuntu-24.04-guacamole-qemu.qcow2
  ```

- This fallback uses QEMU user networking, so the VM cannot directly receive a
  LAN address. It is suitable for Guacamole through the forwarded port.
- Do not remove the source image or seed ISO while the overlay is in use.
- No VM snapshot or backup is created automatically.
