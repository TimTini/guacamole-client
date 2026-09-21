# Ubuntu 24.04 desktop VM demo

This demo converts the verified Ubuntu 24.04 cloud image into a writable
VMware VMDK, creates a NoCloud seed ISO, and provisions XFCE plus xrdp on the
first boot. The VM uses VMware bridged networking so it can receive a LAN
address that Guacamole can use for an RDP connection.

All generated VM files stay under:

`H:\RemoteWorkspaces\guacamole-client\runtime\vm-demo`

This includes the VMDK, seed ISO, rendered cloud-init user-data, VMX file, and
random `vm-password.txt`. The runtime directory is ignored by Git. The source
image is checked against SHA-256 before preparation:

`612b2c0cc1bc413a6cb8c38fd611794caf0f2b436c50013d8b3794db12ad7354`

The script does not read or print the password. Open the ignored password file
locally when entering the Ubuntu RDP connection credentials.

## Prerequisites

- VMware Workstation with `H:\VMware\VMware Workstation\vmrun.exe`;
- Ubuntu `24.04` WSL distribution with `qemu-img`, `cloud-localds`, and
  `openssl`;
- a working bridged VMware network and DHCP on the LAN;
- the verified `ubuntu-24.04-cloud.img` already in `runtime\vm-demo`.

The script never writes to `H:\VMs`.

## Prepare and run

Run from PowerShell:

```powershell
Set-Location H:\RemoteWorkspaces\guacamole-client\deploy-local\vm-demo
.\vm-demo.ps1 prepare
```

`prepare` creates a sparse 16 GiB writable VMDK, the random password, the
rendered cloud-init files, the NoCloud ISO, and the runtime VMX. It does not
start VMware.

After reviewing the generated files, start the VM:

```powershell
.\vm-demo.ps1 start
.\vm-demo.ps1 status
```

`start` also runs preparation when needed and starts the VM with `vmrun` in
`nogui` mode. The first boot needs Internet access and several minutes for apt
to install XFCE and xrdp.

Get the guest IP after cloud-init and `open-vm-tools` finish:

```powershell
& 'H:\VMware\VMware Workstation\vmrun.exe' -T ws getGuestIPAddress `
  'H:\RemoteWorkspaces\guacamole-client\runtime\vm-demo\ubuntu-24.04-guacamole-demo.vmx' -wait
```

In Guacamole, create an RDP connection with:

- Hostname: the guest IP returned above;
- Port: `3389`;
- Username: `ubuntu`;
- Password: the contents of `H:\RemoteWorkspaces\guacamole-client\runtime\vm-demo\vm-password.txt`.

After xrdp is ready, verify the forwarded RDP credentials without displaying
the password:

```powershell
.\test-rdp.ps1
```

The script runs `xfreerdp3` inside Ubuntu-24.04 WSL through a private PTY and
prints only a sanitized result token.

Stop it with:

```powershell
.\vm-demo.ps1 stop
```

## Reprovisioning

Cloud-init runs once for the instance ID in `meta-data`. To build a fresh VM,
stop it first, back up anything needed, then remove only the generated files
inside `runtime\vm-demo` (keep the verified source image and `SHA256SUMS`). Run
`prepare` again. This is intentionally not automated by the script.

## Known limits

- The VM is a small desktop demo: 2 vCPUs, 4 GiB RAM, and a 16 GiB sparse disk.
- Bridged networking exposes the VM to the local network; use the LAN firewall
  and do not publish Guacamole directly to the Internet.
- The Guacamole web service remains bound to localhost by the parent Compose
  setup. A reverse proxy or private tunnel is needed for other users.
- The first-boot package installation cannot complete without apt network
  access. Until it finishes, xrdp and the desktop are unavailable.
- No VM snapshot or backup is created automatically.

## Sources

- Ubuntu official cloud images: https://cloud-images.ubuntu.com/noble/
- cloud-init NoCloud datasource and `cidata` seed format:
  https://cloudinit.readthedocs.io/en/latest/topics/datasources/nocloud.html
- cloud-init user-data formats:
  https://cloudinit.readthedocs.io/en/latest/topics/format.html
