# Libvirt contract

Libvirt owns the QEMU/VM lifecycle through the system connection
`qemu:///system` after the Windows 11 cutover; Cockpit Machines operates that
connection. The canonical network is
`guac-nat` on `virbr-guac`, with Windows 11 reserved at
`192.168.250.11` for MAC `52:54:00:11:11:01`.

The existing Windows 11 domain keeps its current disk, UEFI variables, and TPM
state as one matched set. A dedicated systemd unit owns the `swtpm` process,
using the existing local state and a runtime Unix socket:

- disk: `/var/lib/guacamole-vm-windows11/windows11.qcow2`;
- UEFI variables: `/var/lib/guacamole-vm-windows11/OVMF_VARS_4M.ms.fd`;
- TPM state: `/var/lib/guacamole-vm-windows11/tpm`.
- TPM socket: `/run/guacamole-vm-windows11/swtpm.sock`.

The domain template uses path placeholders so the render step can insert the
absolute Linux paths after preflight. Its TPM backend is external and connects
to the socket supplied by that systemd unit. Libvirt does not create or
regenerate TPM state. The template does not contain guest credentials, tunnel
addresses, or installer media. The domain keeps the existing SATA disk bus and
`e1000e` adapter for Windows compatibility, and uses Q35 with OVMF Secure Boot.

New VMs created by Cockpit use the `guacamole-vms` storage pool at
`/var/lib/guacamole-vms` inside the WSL `ext4.vhdx`. Keep all VM disks,
metadata, UEFI variables, TPM state, and logs inside the WSL
filesystem. Do not expose `virbr-guac` through a physical NIC or a public
listener. Guacamole remains the end-user access boundary, while Cockpit is
for local host administration.

`windows11.ps1` remains a legacy rollback owner only after the cutover marker
has been created. It must not run concurrently with the persistent
`windows11` domain or the systemd `swtpm` unit, and must not access the same
disk or TPM state.

## Inspection

Run these read-only checks from PowerShell:

```powershell
wsl.exe -d Ubuntu-24.04 -u root -- virsh -c qemu:///system list --all
wsl.exe -d Ubuntu-24.04 -u root -- virsh -c qemu:///system net-list --all
wsl.exe -d Ubuntu-24.04 -u root -- virsh -c qemu:///system pool-list --all
wsl.exe -d Ubuntu-24.04 -u root -- virsh -c qemu:///system domblklist windows11
```

Before defining or starting a domain, verify that the network, storage pool,
disk, UEFI variables, and TPM state are present, that the dedicated
systemd `swtpm` unit owns the runtime socket, and that no legacy QEMU owner is
running. Define the network from `networks/guac-nat.xml` and render the domain
from `domains/windows11.xml.template`; later automation replaces the three
`__W11_*__` path placeholders with validated absolute Linux paths. Start the
systemd `swtpm` unit with the existing state before starting the domain; never
initialize a replacement TPM to bypass a lock or missing socket.

## Create another VM

Open `https://127.0.0.1:9090`, sign in as the local host administrator, open
**Virtual Machines** on the system connection, then choose **Create VM**. Use
an ISO below `/mnt/h/RemoteWorkspaces/guacamole-client/runtime/iso`, pool
`guacamole-vms`, network `guac-nat`, UEFI, and TPM 2.0 for Windows 11.
Cockpit is host administration and stays local; Guacamole owns end-user
access and permissions.

The CLI fallback creates a shut-off domain and does not start the installer:

```powershell
Get-FileHash <REPO_ROOT>\runtime\iso\Windows11_23H2_UEFI.iso -Algorithm SHA256
.\new-libvirt-vm.ps1 -Name windows-work-02 -IsoPath <REPO_ROOT>\runtime\iso\Windows11_23H2_UEFI.iso -MemoryMiB 4096 -Vcpus 2 -DiskGiB 64
```

It allocates a unique MAC/IP, adds the DHCP reservation, and stores the qcow2
only in `/var/lib/guacamole-vms`. After installation, enable RDP/firewall,
test port 3389 from WSL and the Compose network, then create the Guacamole
connection and assign users/groups. Save VM name, MAC, IP, ISO hash, and
assignees under H; never save the guest password in that inventory.

## Golden template and clone contract

`windows11` is the only source domain. The controlled template command stops
that persistent domain gracefully, confirms that QEMU no longer holds its
qcow2, runs a read-only image check, converts the disk to
`/var/lib/guacamole-templates/windows11-v1.qcow2`, verifies its SHA-256, and
publishes it mode `0444`. It then starts `windows11` and checks its existing
RDP route through the Guacamole Docker network. A later version gets a new
filename; an existing version is never overwritten.

The template contains only the guest disk. Do not copy the source UUID, UEFI
variables, TPM state, Windows credentials, or Guacamole parameters. A clone
uses a qcow2 overlay with that exact template as its backing file and receives
a fresh UUID, MAC, writable NVRAM file, libvirt-managed TPM 2.0 directory,
DHCP reservation, and IP. The clone domain must use `guac-nat`, e1000e, and a
private graphics listener; its disk, NVRAM, and TPM paths must differ from the
source and from every other clone.

Create from Cockpit's **Workspace Templates** page when possible. The CLI
fallback is:

```powershell
.\create-windows-template.ps1 -Source windows11 -Version windows11-v1
.\clone-windows-vm.ps1 -Name windows-template-test-01 -AssignUser demo -TemplateVersion windows11-v1
```

The helper validates the user/group again, grants the assignee exactly `READ`,
and grants `guacadmin` the four administration permissions. Managed Guacamole
parameters are limited to the six names `hostname`, `port`, `security`,
`ignore-cert`, `username`, and `password`; the Windows username/password are
stored only in `guacamole_connection_parameter`. Gateway credentials, private
keys, and tokens remain forbidden. The password is transported to PostgreSQL
through COPY stdin and must not appear in SQL text, argv, inventory, logs,
documentation, or maintenance reports.

Before retiring `windows11-v1`, verify that every overlay's
`qemu-img info --backing-chain` and the inventory dependent count are empty.
If a clone must outlive the template, stop it, convert its overlay to a new
independent qcow2, check and hash the new disk, update the domain, and verify
the old backing file is no longer referenced. Remove disposable clones by
their exact inventory record only; never delete by a broad path or VM name
that was not read from the current transaction ledger.
