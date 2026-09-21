# Windows golden template cloning and Guacamole assignment

## Goal

Create new Windows workspaces quickly from the existing libvirt domain
`windows11`. An administrator supplies a VM name and a Guacamole user or group.
The workflow creates a libvirt domain with independent hardware identity,
allocates a stable address on `guac-nat`, and creates the matching Guacamole RDP
connection and permissions.

All persistent state remains on drive H through the repo-owned Ubuntu WSL VHDX.
Cockpit remains local host administration. Guacamole remains the only public
user access boundary.

## Accepted constraints

- The source is `windows11`, not `windows11-02`.
- Clones retain the Windows local account `guacadmin` and its existing password.
- The shared Windows username/password may be written only to the six
  allowlisted rows in `guacamole_connection_parameter` for a managed clone.
  It is never written to scripts, logs, inventory, template metadata, job
  JSON, progress, reports, maintenance bundles, or command arguments.
- The username defaults to `guacadmin`. The password is supplied once through
  the interactive initializer and stored only at the fixed POSIX path
  `/var/lib/guacamole-workspace/secrets/windows11_guacadmin_password` inside
  the H-backed Ubuntu ext4 VHDX at
  `H:\RemoteWorkspaces\guacamole-client\runtime\ubuntu\ext4.vhdx`. Its parent
  is verified as root-owned `0700` and its regular file is root-owned `0600`.
  Production has no arbitrary secret-path override and never uses a `/mnt/h`
  DrvFs path for this secret.
- Guacamole users have distinct Guacamole passwords and receive access only to
  their assigned connections.
- Each clone must have a unique libvirt UUID, MAC address, UEFI variables, TPM
  2.0 state, DHCP reservation, and IP address.
- The source domain is never cloned while its disk is writable by QEMU.

## Storage layout

The Ubuntu distribution uses
`H:\RemoteWorkspaces\guacamole-client\runtime\ubuntu\ext4.vhdx`. Therefore the
following Linux paths are physically H-backed:

```text
/var/lib/guacamole-templates/windows11-v1.qcow2
/var/lib/guacamole-vms/<vm-name>.qcow2
/var/lib/libvirt/qemu/nvram/<vm-name>_VARS.fd
/var/lib/libvirt/swtpm/<domain-uuid>/tpm2/
/var/lib/guacamole-workspace/secrets/windows11_guacadmin_password
```

The credential path is inside the same H-backed ext4 VHDX, so VHDX backup and
rollback preserve the storage boundary while allowing exact POSIX ownership and
mode checks. It is excluded from source and maintenance exports.

`windows11-v1.qcow2` is an immutable, read-only golden base. Each VM disk is a
qcow2 overlay whose backing file is that exact version. A new template version
uses a new filename. Existing template versions cannot be modified or removed
while any overlay references them.

Repo-visible, non-secret inventory is stored under
`runtime/templates/windows11/` and records template version, source domain,
base path, SHA-256, creation time, and dependent domain names. It contains no
Windows or Guacamole credentials.

## Template creation

The template command performs these gates before any mutation:

1. Confirm `windows11` is persistent and owned by `qemu:///system`.
2. Confirm its source disk, UEFI NVRAM, TPM service/state, network, and current
   Guacamole connection are present.
3. Confirm the destination template version does not exist.
4. Request a graceful shutdown and wait for the domain to stop.
5. Confirm no QEMU process holds the source qcow2, then run
   `qemu-img check --read-only`.
6. Create the golden base with `qemu-img convert -O qcow2`, verify it, calculate
   SHA-256, and mark it read-only.
7. Restart `windows11` and verify its existing RDP route.

The process copies only the Windows disk. It never copies the source UUID,
NVRAM, or TPM state. The first template version preserves the installed local
Windows account and current software exactly as requested. Duplicate Windows
machine identity is an accepted limitation for local, non-domain workspaces;
future generalized template versions may use Sysprep without changing this
clone contract.

## Clone workflow

`clone-windows-vm.ps1` accepts:

```text
-Name <unique-domain-name>
-AssignUser <existing-guacamole-user>
-TemplateVersion windows11-v1
-MemoryMiB 4096
-Vcpus 2
```

An optional `-AssignGroup` is mutually exclusive with `-AssignUser`. The script:

1. Validates names, template hash/read-only state, H-backed storage, free space,
   libvirt services, and the Guacamole database.
2. Rejects an existing domain, disk, DHCP reservation, Guacamole connection,
   or assignment with the same identity.
3. Selects the first unused address from `192.168.250.20-249` and generates a
   locally administered, collision-free MAC address.
4. Creates a qcow2 overlay in `/var/lib/guacamole-vms/` with the immutable
   template as its backing file.
5. Defines a persistent Q35/UEFI domain with a new UUID, new writable NVRAM,
   libvirt-managed TPM 2.0 emulator, `e1000e`, and `guac-nat`.
6. Adds a persistent and live DHCP reservation, starts the domain, and waits for
   its lease and TCP 3389 from the `guacamole-local_default` Docker network.
7. Upserts a Guacamole RDP connection using exactly the six allowlisted
   parameters: hostname, port, `security=any`, `ignore-cert=true`, username,
   and password. Username/password are supplied only to the connection
   parameter table through the secure COPY transport.
8. Grants `READ` to the requested Guacamole user/group and grants
   `READ`, `UPDATE`, `DELETE`, and `ADMINISTER` to `guacadmin`.
9. Writes the non-secret inventory record and reports the VM name, IP, MAC,
   template version, connection ID, and assignee.

The operation is idempotent for an already completed matching clone. On partial
failure, it removes only artifacts created by the current run and never deletes
the template, source VM, another domain, or Guacamole users.

## Cockpit user interface

Do not patch or replace files owned by `cockpit-machines`. That package does not
provide a stable extension point for inserting a button beside **Create VM**, and
vendor-file changes would be lost or could break after an apt update.

Install a repo-owned Cockpit package named **Workspace Templates**. It appears as
a separate item in Cockpit's left navigation and contains:

- an immutable template list with version, creation time, size, hash state, and
  dependent clone count;
- a **Create Windows workspace** action;
- fields for VM name, Guacamole user or group, template version, memory, and
  vCPU count;
- progress for validation, disk overlay, domain, DHCP, RDP, Guacamole, and
  permission stages;
- a result containing VM name, IP, assignee, **Open in Virtual Machines**, and
  **Open in Guacamole** actions;
- actionable errors that identify the failed stage without showing credentials.

The page calls a privileged, allowlisted local helper through Cockpit. The
helper accepts structured arguments, validates them again, and invokes the same
clone/sync implementation used by the PowerShell CLI. It does not accept raw
shell commands from the browser. Cockpit authentication and privilege escalation
remain the administration boundary; the page is never exposed by Cloudflare.

## Guacamole synchronization

Discovery is explicit and deterministic rather than an unrestricted background
scan. The clone command creates the connection after RDP is reachable.
`sync-guacamole-vms.ps1` is a repair command that reads the inventory, verifies
the libvirt domain/MAC/IP, and restores missing or stale Guacamole connection
parameters and declared permissions.

The sync command never grants access to every discovered VM and never infers an
assignee from a VM name. A VM without an inventory assignment remains admin-only
and is reported for review.

## Template lifecycle

- Template files are immutable and versioned (`windows11-v1`, `windows11-v2`).
- Updating Windows or applications requires creating a new version.
- Existing overlays continue to use their original version.
- Deleting a template is blocked while `qemu-img info --backing-chain` or the
  inventory shows dependent clones.
- Full independent conversion is available as a maintenance action before a
  template version is retired.
- Backup/export includes template manifests and scripts. Large qcow2 files stay
  under H-backed runtime storage and are not added to Git or maintenance ZIPs.

## Security and isolation

The shared Windows `guacadmin` credential is an accepted convenience boundary,
not per-user Windows isolation. Isolation is enforced at Guacamole permissions:
users cannot see unassigned connections. Cockpit stays bound to
`127.0.0.1:9090`; Cloudflare exposes only Guacamole on port 8080.

Because all clones share a Windows credential, the credential values are
restricted to the Guacamole connection parameter table. They must not be
shown in inventory, command output, logs, reports, or maintenance bundles.
If a user learns it, Guacamole permissions alone cannot prevent that user from
using it against another reachable Windows guest. The `guac-nat` network must
therefore remain private and not be exposed to the LAN or Internet.

## Verification

Automated checks cover:

- source shutdown, unlocked disk, successful template hash, and source restart;
- immutable H-backed template and valid overlay backing chain;
- unique domain UUID, MAC, NVRAM, TPM state, DHCP reservation, and IP;
- domain start plus TCP 3389 from the Guacamole Docker network;
- managed Guacamole connections contain exactly the six allowlisted parameter
  names, with username/password present only in the connection parameter table;
- intended user/group has `READ`, unrelated users do not, and `guacadmin` has
  all administration permissions;
- credential values reach PostgreSQL only as COPY stdin data into a
  transaction-local temporary table; they are absent from SQL text, argv, and
  all non-parameter outputs;
- rerunning clone or sync is idempotent;
- rollback removes only current-run artifacts;
- template deletion is refused while dependents exist.

Manual verification opens one cloned connection as its assigned non-admin user
and confirms an unrelated connection is absent.
