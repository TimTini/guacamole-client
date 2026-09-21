# Windows Golden Template Cloning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an H-backed immutable Windows template workflow that creates a fully configured libvirt clone and assigns its Guacamole RDP connection from a dedicated Cockpit page.

**Architecture:** A Python 3 standard-library helper inside WSL owns template, clone, inventory, DHCP, and Guacamole operations behind a strict subcommand interface. PowerShell wrappers call that helper through WSL, while a repo-owned Cockpit package calls the installed helper with `cockpit.spawn()` and required administrative privileges. The golden qcow2 is immutable; each clone has a unique overlay, UUID, MAC, NVRAM, TPM state, reserved IP, and explicit Guacamole assignment.

**Tech Stack:** Windows PowerShell 5+, WSL2 Ubuntu 24.04, Python 3 standard library, QEMU qcow2, libvirt/virsh, swtpm, PostgreSQL 16 via Docker Compose, Apache Guacamole 1.6.0, Cockpit package HTML/CSS/JavaScript and `cockpit.js`.

**Spec:** `docs/superpowers/specs/2026-09-20-windows-golden-template-cloning-design.md`

## Global Constraints

- The template source is the persistent libvirt domain `windows11`, never `windows11-02`.
- Persistent template and clone state must remain inside `runtime/ubuntu/ext4.vhdx` on H:.
- Never copy the source UUID, NVRAM, or TPM state into a clone.
- Never copy or print Windows or Guacamole credentials.
- Cockpit remains local-only on `127.0.0.1:9090`; Cloudflare exposes only Guacamole on port 8080.
- Template versions are immutable and deletion is blocked while overlays depend on them.
- VM names match `^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$`; assignee names match `^[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}$`.
- IP allocation uses unused addresses in `192.168.250.20-249`; `192.168.250.11` and `.12` remain reserved.
- Browser code invokes only an allowlisted helper and never constructs shell commands.
- Existing `windows11`, `windows11-02`, their TPM state, disks, Guacamole connections, and user changes must be preserved.

## Review Focus

- Interrupted template creation must restart `windows11` and must not publish a partial template.
- A malicious VM or assignee name must fail validation before reaching virsh, psql, filenames, or logs.
- A collision in domain, disk, UUID, MAC, IP, DHCP reservation, connection, or inventory must fail without changing existing state.
- A partial clone failure must remove only artifacts recorded by that invocation and preserve the immutable template and unrelated VMs.
- Concurrent clone requests must serialize IP/MAC allocation and inventory writes through one lock.

---

### Task 1: Define the helper contract, inventory, and read-only discovery

**Files:**
- Create: `deploy-local/workspace-helper.py`
- Create: `deploy-local/tests/test-workspace-helper.py`
- Create: `deploy-local/templates/windows11/README.md`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: `virsh -c qemu:///system`, `/var/lib/guacamole-templates`, `/var/lib/guacamole-vms`, `guac-nat`, and `deploy-local/compose.yaml`.
- Produces: CLI `workspace-helper.py list --json`, data classes `TemplateRecord` and `CloneRecord`, `validate_vm_name()`, `validate_assignee()`, `load_inventory()`, `save_inventory_atomic()`, and lock `/run/lock/guacamole-workspace-helper.lock`.

- [ ] **Step 1: Write failing validation and inventory tests**

```python
class ValidationTests(unittest.TestCase):
    def test_rejects_shell_metacharacters_before_commands(self):
        for value in ("x;id", "$(id)", "../x", "x y", "x'OR'1"):
            with self.assertRaises(ValidationError):
                validate_vm_name(value)

    def test_atomic_inventory_rejects_duplicate_ip_and_mac(self):
        inventory = {"clones": [
            {"name": "vm-a", "ip": "192.168.250.20", "mac": "52:54:00:20:00:01"}
        ]}
        with self.assertRaises(ConflictError):
            assert_unique_clone(inventory, "vm-b", "192.168.250.20", "52:54:00:20:00:02")
```

- [ ] **Step 2: Run the focused test and confirm it fails**

Run:

```powershell
wsl.exe -d Ubuntu-24.04 -u root -- python3 -m unittest /mnt/h/RemoteWorkspaces/guacamole-client/deploy-local/tests/test-workspace-helper.py -v
```

Expected: import failure because `workspace-helper.py` and its interfaces do not exist.

- [ ] **Step 3: Implement the strict CLI shell and atomic inventory**

Use `argparse` subcommands only: `list`, `create-template`, `clone`, `sync`, and `check-template-delete`. Run external programs with `subprocess.run([arg0, arg1, ...], shell=False, check=True)`. Store inventory at `/var/lib/guacamole-templates/inventory.json`; write to a sibling temp file, `fsync`, then `os.replace`. Acquire the global lock with `fcntl.flock(LOCK_EX | LOCK_NB)` and emit JSON errors shaped as:

```json
{"ok":false,"code":"NAME_INVALID","stage":"validate","message":"VM name is invalid"}
```

The `list --json` result is:

```json
{"ok":true,"templates":[],"clones":[],"guacamoleAssignees":{"users":[],"groups":[]}}
```

- [ ] **Step 4: Add runtime exclusions and storage documentation**

Add only generated records to `.gitignore`:

```gitignore
runtime/templates/
runtime/workspace-operations/
```

Document that qcow2 files live inside the H-backed WSL ext4 filesystem and must not be copied to C: or committed.

- [ ] **Step 5: Run unit tests and read-only live discovery**

Run:

```powershell
wsl.exe -d Ubuntu-24.04 -u root -- python3 -m unittest /mnt/h/RemoteWorkspaces/guacamole-client/deploy-local/tests/test-workspace-helper.py -v
wsl.exe -d Ubuntu-24.04 -u root -- python3 /mnt/h/RemoteWorkspaces/guacamole-client/deploy-local/workspace-helper.py list --json
```

Expected: tests pass; JSON lists `windows11` and `windows11-02` as domains but no template or clone records; no runtime state changes.

### Task 2: Create and protect the `windows11-v1` golden template

**Files:**
- Modify: `deploy-local/workspace-helper.py`
- Modify: `deploy-local/tests/test-workspace-helper.py`
- Create: `deploy-local/create-windows-template.ps1`

**Interfaces:**
- Consumes: Task 1 lock/inventory functions and source domain `windows11`.
- Produces: `create-template --source windows11 --version windows11-v1`, immutable `/var/lib/guacamole-templates/windows11-v1.qcow2`, and manifest record `{version, sourceDomain, path, sha256, virtualSize, createdAt}`.

- [ ] **Step 1: Write failing transactional template tests**

Use a fake command runner and assert this exact order:

```python
self.assertEqual(events, [
    "validate-source", "shutdown-source", "wait-shutoff", "check-unlocked",
    "qemu-img-check-source", "convert-temp", "check-temp", "hash-temp",
    "chmod-readonly", "rename-publish", "save-inventory", "start-source",
    "wait-source-rdp"
])
```

Add failures at `convert-temp` and `save-inventory`; both tests must assert the temporary image is removed, the final version is absent, and `start-source` still runs. Add a test that an existing version fails before shutdown.

- [ ] **Step 2: Run tests and confirm the template tests fail**

Expected: failures reference missing `create_template()` and rollback behavior.

- [ ] **Step 3: Implement guarded template creation**

Implement `create_template(source, version, runner)` with `try/finally` around source restart. Use graceful `virsh shutdown`, bounded state polling, `qemu-img check --read-only`, and:

```text
qemu-img convert -p -O qcow2 SOURCE_DISK windows11-v1.qcow2.partial
qemu-img check --read-only windows11-v1.qcow2.partial
sha256sum windows11-v1.qcow2.partial
chmod 0444 windows11-v1.qcow2.partial
rename windows11-v1.qcow2.partial windows11-v1.qcow2
```

Before conversion, reject any open source-disk holder found through `/proc/*/fd` and confirm the dedicated source TPM unit stopped with the domain. After restart, require TCP 3389 from `guacamole-local_default`.

- [ ] **Step 4: Implement the PowerShell wrapper**

`create-windows-template.ps1` validates `-Source windows11` and `-Version windows11-v1`, invokes the helper through `wsl.exe -d Ubuntu-24.04 -u root --`, prints stage JSON without secrets, and exits nonzero on `{ok:false}`.

- [ ] **Step 5: Run simulated failure tests and preflight only**

Run unit tests plus:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\deploy-local\create-windows-template.ps1 -Source windows11 -Version windows11-v1 -WhatIf
```

Expected: source/domain/storage gates pass and no shutdown or template file occurs. Do not run the real template conversion until the execution phase explicitly reaches the controlled outage step.

### Task 3: Clone libvirt domains with unique identity and deterministic rollback

**Files:**
- Modify: `deploy-local/workspace-helper.py`
- Modify: `deploy-local/tests/test-workspace-helper.py`
- Create: `deploy-local/clone-windows-vm.ps1`
- Create: `deploy-local/libvirt/domains/windows-clone.xml.template`

**Interfaces:**
- Consumes: verified template record and lock from Tasks 1-2.
- Produces: `clone --name NAME --assign-user USER --template windows11-v1 --memory-mib 4096 --vcpus 2`, overlay disk, persistent domain, DHCP reservation, RDP-ready IP, and incomplete clone record ready for Guacamole sync.

- [ ] **Step 1: Write failing allocation, XML, and rollback tests**

Cover the five Review Focus cases. Assert allocation skips `.11`, `.12`, existing reservations, live leases, and inventory values. Parse generated XML and assert:

```python
self.assertNotEqual(clone_uuid, source_uuid)
self.assertEqual(tpm_backend, {"type": "emulator", "version": "2.0"})
self.assertEqual(network_source, "guac-nat")
self.assertEqual(firmware, "efi")
self.assertNotIn("windows11_VARS", xml)
self.assertNotIn("/run/guacamole-vm-windows11/swtpm.sock", xml)
```

Inject failures after overlay, DHCP, define, and start. The rollback ledger must remove only entries created by that invocation in reverse order.

- [ ] **Step 2: Run focused tests and confirm they fail**

Expected: failures identify missing allocator, XML renderer, and rollback ledger.

- [ ] **Step 3: Implement overlay and domain creation**

Create overlays with:

```text
qemu-img create -f qcow2 -F qcow2 -b /var/lib/guacamole-templates/windows11-v1.qcow2 /var/lib/guacamole-vms/<name>.qcow2
```

Render Q35/UEFI XML with a new `uuid.uuid4()`, `secrets.token_bytes()` MAC under a fixed locally administered prefix, writable NVRAM created from `/usr/share/OVMF/OVMF_VARS_4M.ms.fd`, libvirt-managed TPM emulator 2.0, e1000e, and `guac-nat`. Use `virsh net-update ... --live --config` for the selected address and record each created resource in the rollback ledger.

- [ ] **Step 4: Implement start, lease, and RDP gates**

Start the domain, match its MAC in `virsh net-dhcp-leases guac-nat`, and test port 3389 from a one-shot container on `guacamole-local_default`. Timeout must return `RDP_NOT_READY` while retaining the valid VM and assignment metadata with status `waiting-rdp`; it must not delete a booting clone.

- [ ] **Step 5: Implement and test the PowerShell wrapper**

Expose parameters from the spec, with mutually exclusive `-AssignUser`/`-AssignGroup`. Add `-WhatIf` and `-WaitRdpMinutes` but no retry/fallback flags. Run parser checks, unit tests, and create a fake-runner clone test; do not create a live production clone yet.

### Task 4: Upsert Guacamole connections and explicit permissions

**Files:**
- Modify: `deploy-local/workspace-helper.py`
- Modify: `deploy-local/tests/test-workspace-helper.py`
- Create: `deploy-local/sync-guacamole-vms.ps1`

**Interfaces:**
- Consumes: clone record `{name, mac, ip, assigneeType, assigneeName, status}` and live libvirt/network state.
- Produces: connection named exactly as the domain, safe RDP parameters, explicit permissions, connection ID, and clone status `ready`.

- [ ] **Step 1: Write failing SQL contract and permission tests**

Test generated SQL with hostile but rejected names, absent assignee, user assignment, group assignment, rerun idempotency, stale hostname repair, and unrelated permission preservation. Assert parameter names are exactly:

```python
{"hostname", "port", "security", "ignore-cert", "username", "password"}
```

Assert `username` and `password` appear only in the connection parameter table through COPY stdin data; the password never appears in SQL text, arguments, JSON, reports, maintenance bundles, or logs. Gateway credentials, tokens, and hashes remain forbidden everywhere else.

- [ ] **Step 2: Run tests and confirm sync behavior is absent**

Expected: failures reference missing `build_guacamole_sync_sql()` and `sync_guacamole()`.

- [ ] **Step 3: Implement parameterized psql synchronization**

Pass the SQL program on stdin to `docker compose exec -T postgres psql -X -q -v ON_ERROR_STOP=1` and pass validated values as psql variables. The transaction must:

1. find or create one root RDP connection;
2. upsert hostname, port 3389, security `any`, and ignore-cert `true`;
3. resolve the declared USER or USER_GROUP entity and fail if absent;
4. grant only `READ` to the assignee;
5. grant `READ`, `UPDATE`, `DELETE`, and `ADMINISTER` to `guacadmin`;
6. return the connection ID and effective permissions.

Credential-enabled creates and managed repairs load the default `guacadmin`
username plus the initialized password from the fixed POSIX path
`/var/lib/guacamole-workspace/secrets/windows11_guacadmin_password` inside the
H-backed Ubuntu ext4 VHDX. The password is sent as
COPY stdin data into a transaction-local temporary table, so it is present only
in the six managed connection parameter rows and absent from SQL text, argv,
JSON, logs, reports, and maintenance bundles. The release installer and
initializer must be published before that secret is initialized.

- [ ] **Step 4: Implement repair sync and dry run**

`sync --all` reads inventory only; it does not discover and grant unknown VMs. A missing inventory assignee is reported as `ADMIN_ONLY`. `sync-guacamole-vms.ps1 -WhatIf` prints intended changes without writing PostgreSQL.

The retained `wtest`/connection 14 row is handled only by the explicit audited
adoption command. Its read-only preflight requires the inventory identity,
`parent_id IS NULL`, exact name/protocol/hostname/IP, complete `guacadmin`
permissions, an exact assignee marker, no conflicting same-name row, and no
attempt marker. Marker and credentials are then committed in one transaction;
the adoption path performs no later inventory write.

- [ ] **Step 5: Run database integration on a disposable transaction**

Use a temporary connection name inside `BEGIN`, verify parameters/permissions, and `ROLLBACK`. Then run sync against existing inventory in dry-run mode. Confirm current connections `Windows 11` and `Windows 11-02` are unchanged.

### Task 5: Add the Cockpit `Workspace Templates` package

**Files:**
- Create: `deploy-local/cockpit/workspace_templates/manifest.json`
- Create: `deploy-local/cockpit/workspace_templates/index.html`
- Create: `deploy-local/cockpit/workspace_templates/workspace-templates.js`
- Create: `deploy-local/cockpit/workspace_templates/workspace-templates.css`
- Create: `deploy-local/tests/test-cockpit-workspace-package.ps1`
- Modify: `deploy-local/libvirt.ps1`

**Interfaces:**
- Consumes: installed `/usr/local/libexec/guacamole-workspace-helper` with `list` and `clone` subcommands.
- Produces: Cockpit menu entry **Workspace Templates**, create form, stage progress, and result links.

- [ ] **Step 1: Write failing package manifest and security tests**

The PowerShell test parses `manifest.json`, HTML, and JavaScript and asserts:

```powershell
$manifest.menu.'workspace-templates'.label | Should -Be 'Workspace Templates'
$manifest.menu.'workspace-templates'.path | Should -Be 'index.html'
$js | Should -Match 'cockpit\.spawn'
$js | Should -Match 'superuser:\s*["'']require["'']'
$js | Should -Not -Match 'sh\s+-c|bash\s+-c|eval\(|innerHTML\s*='
```

Also assert the form fields use select/options loaded from helper JSON for template and assignee; user input is passed as individual argv elements.

- [ ] **Step 2: Run the package test and confirm files are missing**

Expected: test fails on missing manifest and page assets.

- [ ] **Step 3: Implement the vanilla Cockpit page**

Use `<script src="../base1/cockpit.js"></script>` and no new build dependency. Register:

```json
{
  "version": 1,
  "requires": {"cockpit": "314"},
  "menu": {
    "workspace-templates": {
      "label": "Workspace Templates",
      "order": 55,
      "path": "index.html"
    }
  }
}
```

Call only:

```javascript
cockpit.spawn([HELPER, "list", "--json"], { superuser: "require", err: "message" })
cockpit.spawn([HELPER, "clone", "--name", name, "--assign-user", user,
               "--template", template, "--memory-mib", memory, "--vcpus", vcpus,
               "--json"], { superuser: "require", err: "message" })
```

Render user values with `textContent`. Disable submit while running. Show stage, safe error code/message, and links to `/machines` and `/guacamole/` only after `{ok:true,status:"ready"}`.

- [ ] **Step 4: Install from the repo without modifying vendor packages**

Add `libvirt.ps1 cockpit-workspaces` to copy assets to `/usr/local/share/cockpit/workspace_templates/`, helper to `/usr/local/libexec/guacamole-workspace-helper`, set root ownership/modes, and run `cockpit-bridge --packages`. Never write under `/usr/share/cockpit/machines`.

- [ ] **Step 5: Verify local UI and privilege boundary**

Run static tests, install the package, sign into Cockpit as `cockpitadmin`, and verify the menu/page/list operation. Verify an unprivileged helper invocation cannot mutate state, Cockpit prompts for administrative access if needed, and port 9090 remains bound only to `127.0.0.1`.

### Task 6: Execute the controlled template creation and end-to-end clone

**Files:**
- Modify: `deploy-local/test-libvirt.ps1`
- Modify: `deploy-local/README.md`
- Modify: `deploy-local/RUNBOOK.md`
- Modify: `deploy-local/libvirt/README.md`
- Modify: `deploy-local/export-maintenance-bundle.ps1`

**Interfaces:**
- Consumes: all prior tasks.
- Produces: live `windows11-v1`, one disposable verified clone, updated recovery documentation, and a secrets-free maintenance bundle.

- [ ] **Step 1: Add failing end-to-end gates before live mutation**

Extend `test-libvirt.ps1` with a `templates` mode that checks template hash/read-only state, backing chains, unique clone UUID/MAC/NVRAM/TPM, DHCP consistency, Docker-to-RDP, Guacamole parameter allowlist, assignee isolation, admin permissions, and Cockpit package discovery.

- [ ] **Step 2: Run existing gates and record the baseline**

Run `test-libvirt.ps1 -Mode all`, capture domain/network/pool/connection inventory, and create a timestamped backup of `windows11` metadata plus Guacamole configuration without secrets. Abort the outage if any existing gate fails.

- [ ] **Step 3: Create `windows11-v1` during a controlled outage**

Run the real template command once. Verify `windows11` restarts, its RDP route and Guacamole connection still work, the published template hash matches inventory, and no partial file remains.

- [ ] **Step 4: Create and verify a disposable clone through Cockpit**

Create `windows-template-test-01`, assign only `demo`, wait for RDP, and verify:

- `demo` sees the new connection and does not gain access to unrelated machines;
- `guacadmin` can administer it;
- the managed connection contains the six allowlisted RDP parameters, including
  `username=guacadmin` and the shared password only in
  `guacamole_connection_parameter`; the password is transported by COPY
  stdin and is absent from SQL text, logs, argv, inventory, and reports;
- source/template disk, UUID, NVRAM, and TPM are not shared writable state.

After recording evidence, remove only the disposable domain, overlay, DHCP reservation, Guacamole connection/permissions, and inventory record. Keep `windows11-v1`.

- [ ] **Step 5: Document operations and failure recovery**

Document UI creation, CLI fallback, template versioning, shared Windows credential boundary, IP allocation, sync repair, clone removal, conversion to full independent disk, template retirement checks, rollback ledger, and recovery after WSL registration loss. Include exact read-only diagnosis commands and safe actions for each error code.

- [ ] **Step 6: Run final verification and export maintenance bundle**

Run Python/unit/static tests, all PowerShell parser checks, `test-libvirt.ps1 -Mode all`, `test-libvirt.ps1 -Mode templates`, local Cockpit/Guacamole HTTP checks, and a real assigned-user RDP session. Export the maintenance ZIP and reopen it to prove runtime qcow2 files, secrets, credentials, database data, and generated inventory are excluded.
