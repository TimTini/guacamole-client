# Local Guacamole deployment

For the complete install, recovery, maintenance, update, and troubleshooting
procedure, see [RUNBOOK.md](RUNBOOK.md). This README summarizes the topology
and the shortest start path.

## Public release boundary

The entire `runtime\` directory is local operational state and is ignored by
Git. `deploy-local\data\` and `deploy-local\secrets\` are ignored as well.
The H-backed `runtime\ubuntu\ext4.vhdx` contains the Docker database volume
and credentials inside WSL; it must never be published. The same rule applies
to VM disks, ISO files, TPM/UEFI state, logs, backups, generated inventory,
private keys, and environment files.

When preparing a commit, use an explicit `git add` allowlist for source,
templates, tests, and documentation. Do not use `git add deploy-local` or
`git add .`. Run the root `public-release-audit.ps1` before every commit or
push. The audit always blocks an `origin` matching Apache
`apache/guacamole-client` with `origin:UPSTREAM_ORIGIN_FORBIDDEN`; publish
custom work from a repository you own or review the remote before pushing.

This fork tracks the `deploy-local` directory. The
`export-maintenance-bundle.ps1` output preserves the deployment source layer
(scripts, Compose, templates, and documentation) only; it does not preserve
runtime state. Back up `runtime\ubuntu\ext4.vhdx`, VM qcow2 disks, UEFI/NVRAM
and TPM state, ISO files, PostgreSQL volume/data and secrets separately on H:
before replacing a checkout. Then clone the fork again from
`https://github.com/TimTini/guacamole-client.git`. The Apache upstream
repository remains the source project and attribution reference; it does not
contain this fork's local deployment layer.

This compose setup runs the three services recommended by Apache Guacamole:

- `guacamole/guacd:1.6.0` for the RDP/VNC/SSH proxy;
- `guacamole/guacamole:1.6.0` for the browser application;
- `postgres:16-alpine` for users and connection configuration.

Docker Engine and Compose are expected inside the `Ubuntu-24.04` WSL
distribution. The PowerShell wrapper calls that WSL Docker installation; the
Windows `docker` command is not required.

The PostgreSQL data is stored in the named Docker volume
`guacamole-local-postgres-data`. With this setup that volume lives inside the
Ubuntu WSL filesystem, whose `ext4.vhdx` is kept at:

`H:\RemoteWorkspaces\guacamole-client\runtime\ubuntu\ext4.vhdx`

The generated schema and database secret remain under this directory on H:

- `data/postgres-init/001-guacamole.sql` — the one-time schema script generated from the pinned Guacamole image;
- `secrets/postgres_password.txt` — the generated database password.

The shared Windows `guacadmin` credential used by managed template clones is a
separate root-only file at
`/var/lib/guacamole-workspace/secrets/windows11_guacadmin_password`. This is a
Linux path inside the Ubuntu ext4 VHDX at
`H:\RemoteWorkspaces\guacamole-client\runtime\ubuntu\ext4.vhdx`, so it remains
physically H-backed and is included in VHDX backup/rollback handling. It is not
a `/mnt/h` DrvFs path and is not part of the inventory, job status, source
bundle, or maintenance ZIP. The installer creates and verifies its parent as
`root:root` `0700` and refuses to publish if the directory is not a regular
directory with exactly that ownership and mode. Do not initialize the password
until that check passes.

```powershell
.\initialize-windows-auth.ps1
```

The script prompts without echoing the password. A caller that already owns a
secure stdin channel may pipe it to
`.\initialize-windows-auth.ps1 -FromStdin`; the password is sent to the root
helper over stdin and is never a command argument or JSON value. Do not put the
password in a command line, inventory, log, or documentation.

The retained legacy `wtest` row (connection 14) has a separate, read-only
ownership proof. After the installed release has been published and the secret
initialized, the only adoption command is:

```powershell
wsl.exe -d Ubuntu-24.04 -u root -- /usr/local/libexec/guacamole-workspace-helper adopt-guacamole-connection --name wtest --connection-id 14 --json
```

It refuses a different name or ID, inventory identity conflicts, a second
same-name connection, hostname/protocol/assignee/admin-permission mismatches,
or an existing workflow attempt marker.

The web port is bound only to `127.0.0.1:8080`. PostgreSQL and guacd have no
host ports. Services use the normal Compose bridge network, which allows
Guacamole to connect to VMs or other reachable hosts outside the Compose
network.

## Start and recovery

For one-click control, double-click [START-REMOTE.cmd](../START-REMOTE.cmd) in
the repository root. The menu starts or stops the whole stack, or prints its
status. Cockpit stays local at `https://127.0.0.1:9090` and is never added to
the Quick Tunnel. The same file accepts `start`, `stop`, or `status` for
automation and returns the recovery script's exit code:

```cmd
H:\RemoteWorkspaces\guacamole-client\START-REMOTE.cmd start
H:\RemoteWorkspaces\guacamole-client\START-REMOTE.cmd status
H:\RemoteWorkspaces\guacamole-client\START-REMOTE.cmd stop
```

The older `deploy-local\START-REMOTE.cmd` path remains as a compatibility
forwarder to the root command and preserves the same arguments and exit code.
The menu treats Ctrl+C as a cancel without lifecycle changes; automation must
pass exactly one of the three actions. After a successful start, the command
prints the local Guacamole and Cockpit URLs; the temporary Quick Tunnel URL is
shown by the start output or the status action.

The `start` action calls `recover-after-rollback.ps1 start`, which re-registers
`runtime\ubuntu\ext4.vhdx` from H: when `Ubuntu-24.04` is missing, then starts
Docker/Guacamole, libvirt/Cockpit, the `windows11` domain, and the Quick Tunnel.

Run the recovery-safe entrypoint from PowerShell:

```powershell
Set-Location H:\RemoteWorkspaces\guacamole-client\deploy-local
.\recover-after-rollback.ps1 start
```

It registers `runtime\ubuntu\ext4.vhdx` in place when `Ubuntu-24.04` is
missing, waits for systemd, starts Docker and Compose, ensures the
`guac-nat` network and `guacamole-vms` pool, installs the Docker-to-libvirt
route, starts the persistent `windows11` domain through `qemu:///system`, and
starts the Quick Tunnel. The operation is idempotent and reuses the existing
WSL state, containers, database volume, qcow2, UEFI variables, and TPM state.

The first start pulls the images, creates the local secret, generates the
PostgreSQL schema, creates the ext4-backed database volume, and starts the
services. Open:

`http://127.0.0.1:8080/guacamole/`

On a new database the schema creates `guacadmin` with the initial password
`guacadmin`; change it immediately after the first login. On later starts the
script reports that the existing database was retained and does not print a
stale default password.

The scripts restrict every `deploy-local\secrets\*.txt` file, including
`demo_user_password.txt`, and the generated VM password to the current user,
SYSTEM, and local Administrators. Secret contents are never printed.

## Temporary Cloudflare Quick Tunnel

When a temporary external URL is needed, place the repo-local binary at
`runtime\cloudflared\cloudflared.exe` and run:

```powershell
.\start-quick-tunnel.ps1 start
.\start-quick-tunnel.ps1 status
```

The script proxies only `http://127.0.0.1:8080`, keeps `cloudflared.exe`
hidden after PowerShell exits, and writes its PID and logs under `runtime` on
H:. It prints the generated `trycloudflare.com` URL when available. Stop it
with `.\start-quick-tunnel.ps1 stop`; no binary is installed globally or on C:.

## Status and stop

```powershell
.\recover-after-rollback.ps1 status
.\recover-after-rollback.ps1 stop
```

`status` reports the libvirt domain and storage/network ownership, the
dedicated external-TPM unit, legacy-owner inactivity, local Cockpit URL, and
Quick Tunnel state. `stop` stops the Quick Tunnel, shuts down the libvirt
Windows 11 domain and its dedicated TPM unit, then stops Docker/Compose. It
keeps the containers and the named database volume. Do not remove the
Docker volume or the secret file unless you intentionally want to discard this
deployment.

To confirm that PostgreSQL is stored inside the WSL ext4 filesystem:

```powershell
wsl -d Ubuntu-24.04 -u root -- docker volume inspect guacamole-local-postgres-data
```

The volume mountpoint is inside WSL (normally under
`/var/lib/docker/volumes/`), not under `/mnt/h`.

## Manage and create VMs

Open Cockpit locally at `https://127.0.0.1:9090` for CPU, RAM, disks,
start/stop/reboot, console access, and VM creation. For a new Windows VM choose
pool `guacamole-vms` and network `guac-nat`; use an ISO under `runtime\iso`
only after recording its SHA-256. The CLI fallback is `new-libvirt-vm.ps1`;
it defines a shut-off domain so its capacity, ISO, MAC and reserved IP can be
checked before starting it.

After installation, enable RDP inside the guest, create a Guacamole RDP
connection to its reserved `192.168.250.x:3389` address, then assign it under
**Settings → Users/Groups → Permissions**. Do not put passwords in connection
descriptions, documentation, or maintenance inventories.

## Windows golden templates and assigned workspaces

The existing `windows11` domain is the only source for the golden-template
workflow. A template is a versioned, read-only qcow2 under
`/var/lib/guacamole-templates`; it contains the guest disk only. It never
contains the source UUID, UEFI variables, TPM state, Windows password, or a
Guacamole credential. Create a new version after changing Windows or its
applications; do not overwrite an existing version.

In Cockpit, open **Workspace Templates**, choose **Create Windows workspace**,
select a version such as `windows11-v1`, enter a unique VM name, and select an
existing Guacamole user or group. The page calls the allowlisted privileged
helper and queues a root-owned transient systemd job. The job continues when
the Cockpit page is refreshed or its WebSocket is disconnected. Progress is
written atomically to `/var/lib/guacamole-workspaces/jobs/<workspace>.json`
with mode `0600`; page reloads rehydrate the `Recent workspaces` table from
`list --json`, including name, template, IP, assignee, stage, result, and safe
error state. The CLI fallback is:

```powershell
.\create-windows-template.ps1 -Source windows11 -Version windows11-v1
.\clone-windows-vm.ps1 -Name windows-work-02 -AssignUser demo -TemplateVersion windows11-v1
```

For a detached CLI request, use the installed helper directly. Repeating a
request for the same workspace returns its existing job or inventory record;
it does not create a second VM. Pending, `waiting-rdp`, and `sync-failed`
records can be resumed with the idempotent repair command:

```powershell
wsl.exe -d Ubuntu-24.04 -u root -- /usr/local/libexec/guacamole-workspace-helper start --name windows-work-02 --assign-user demo --template windows11-v1 --json
wsl.exe -d Ubuntu-24.04 -u root -- /usr/local/libexec/guacamole-workspace-helper repair --name windows-work-02 --json
```

Repair proves the workspace ownership marker, template backing chain, domain
UUID/XML, NVRAM/TPM markers, MAC, DHCP lease, and RDP readiness before it can
write Guacamole. A same-name Guacamole row is never selected by name alone:
an existing row must carry the exact `syncAttemptId`, assignee marker, and
connection ID, otherwise the helper fails closed or creates only after the
pre-existing-name conflict check. If RDP is unavailable the durable state
remains `waiting-rdp` and no Guacamole write is attempted.

Immediately before a repair sync, the helper repeats the template record
check, including the canonical path, exact mode `0444`, recorded SHA-256, and
qcow2 backing chain. The job directory is published as root-owned `0750`; each
status file is root-owned `0600`, opened through a verified directory file
descriptor with no-follow flags, bounded before JSON parsing, and replaced
atomically. A `failed-without-inventory` row is shown as a diagnostic orphan
with no Repair button unless a separate no-artifact proof permits a safe
restart. Poll responses carry a generation guard so an older F5/repair response
cannot restart polling or overwrite a newer terminal state.

`list --json` treats inventory as the workspace authority and uses a matching
name/template/attempt/identity ledger only for progress and safe error details.
Missing or failed transient units and active ledgers older than the bounded
worker age become explicit `stale` or `failed` states, so Cockpit stops polling
and exposes Repair / resume. A `failed-without-inventory` state is diagnostic
only when artifacts cannot be disproved; restart is allowed only after the
read-only no-artifact proof succeeds.

The source is stopped only during the template conversion and is restarted
with its existing RDP route before the command returns. Each clone receives a
new UUID, MAC, writable UEFI variables, managed TPM state, DHCP reservation,
and address in `192.168.250.20-249`. Managed Guacamole connections store
exactly the six allowlisted parameters: `hostname`, `port`, `security`,
`ignore-cert`, `username`, and `password`. The shared `guacadmin`
username/password are written only to `guacamole_connection_parameter`; they
never appear in inventory, job JSON, progress, logs, reports, maintenance
bundles, or command arguments. The password reaches PostgreSQL as COPY stdin
data loaded into a transaction-local temporary table, never as SQL text.

To repair a connection after an interrupted sync, inspect the non-secret
inventory and run the read-only check first, then use the repair command only
for records that are present and assigned:

```powershell
wsl.exe -d Ubuntu-24.04 -u root -- /usr/local/libexec/guacamole-workspace-helper list --json
.\sync-guacamole-vms.ps1 -WhatIf
.\sync-guacamole-vms.ps1
```

Remove a disposable workspace only after recording its domain, overlay, DHCP
reservation, Guacamole connection, permissions, and inventory record. Delete
only that workspace's artifacts; never remove `windows11`, a template version,
or another user's connection. Before retiring a template, confirm that
`qemu-img info --backing-chain` and the inventory report no dependent clone.
Convert a clone to a full independent disk with a planned maintenance action
and verify the new disk hash before deleting its old backing relationship.

## Recovery after a C: rollback

The WSL disk image is on H:, so a C: rollback should not remove the Docker
volume or the files under this directory. Docker images may need to be pulled
again after the WSL registration or Docker installation is restored.

1. Confirm that this file still exists:

   `H:\RemoteWorkspaces\guacamole-client\runtime\ubuntu\ext4.vhdx`

2. Run the same entrypoint. It checks the registration and only imports the
   existing disk when `Ubuntu-24.04` is missing:

   ```powershell
   Set-Location H:\RemoteWorkspaces\guacamole-client\deploy-local
   .\recover-after-rollback.ps1 start
   ```

If the WSL distribution is listed but Docker is missing, reinstall Docker and
Compose inside that same distribution. Do not delete or recreate the existing
`ext4.vhdx`, Docker volume, qcow2, UEFI variables, TPM state, or H: secret file.
The normal entrypoint deliberately does not call the legacy Windows QEMU script;
after the cutover marker, `qemu:///system` is the only Windows 11 owner.

If the named database volume exists but `secrets/postgres_password.txt` is
missing, the script stops instead of generating a new password that would not
match the existing database. Restore the secret file from the H: backup, then
retry.

## Sources

The service split, Docker environment configuration, and PostgreSQL schema
initialization follow the Apache Guacamole manual:

- https://guacamole.apache.org/doc/gug/guacamole-docker.html
- https://guacamole.apache.org/doc/gug/postgresql-auth.html
