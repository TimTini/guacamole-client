# Manual VM start progress

## Goal and scope

Make the default whole-stack `start` bring up Guacamole, networking, storage, Cockpit, and the tunnel without automatically starting any VM. Keep explicit VM start commands available. Update operator documentation and verify the changed startup behavior without launching live services.

## Baseline (source inspection, 2026-10-02)

- `START-REMOTE.cmd start` calls `deploy-local/recover-after-rollback.ps1 start`.
- Recovery `start` calls `start-local.ps1 start` and `libvirt.ps1 start`.
- `start-local.ps1 start` currently launches `vm-demo/qemu-demo.ps1 start`; `libvirt.ps1 start` launches the `windows11` domain when shut off.
- Windows template allocates 8192 MiB and 4 vCPU; demo QEMU command allocates 4096 MiB and 2 vCPU.
- Git `main...origin/main` was clean before edits. No repo `AGENTS.md` found.

## Plan

1. Add a regression check for the default start path and observe it fail against the current scripts.
2. Remove both automatic VM start calls while preserving service setup and explicit VM start actions.
3. Update README/runbook instructions and command output to match the new behavior.
4. Run focused regression checks and relevant repository gates; inspect the diff, commit scoped files, then push the working branch if the public release audit allows it.

## Progress

- [x] Regression check red: `test-start-default-no-vm.ps1` failed on the original recovery path because it started Windows 11.
- [x] Implementation and docs: removed automatic Ubuntu demo and Windows 11 start calls; documented manual VM start in root/deploy READMEs and RUNBOOK; updated command text.
- [x] Verification and review: root ran the mocked non-live startup regression, parser, clone-wrapper test, libvirt task-3 test, and `git diff --check`; all exited 0. Read-only review found stale RDP smoke instructions in RUNBOOK, which were updated to run only after a manual Windows VM start.
- [x] Commit and push: code/docs/test committed as `aafd4c7` and pushed to `origin/main`; local HEAD and remote `refs/heads/main` both resolved to `aafd4c7adaec228d64846692533e45f6bd6b6bfe` after push.

## Evidence and limits

- The original regression check failed before implementation because recovery started Windows 11, then passed after the two automatic start calls were removed. It was replaced with a non-live behavior check that intercepts child PowerShell calls and runs `Start-LocalDeployment` with mocked dependencies.
- Root verification: `test-start-default-no-vm.ps1` -> `START_DEFAULT_NO_VM_TEST_PASS`; PowerShell parser -> `POWERSHELL_PARSE_OK`; `test-clone-wrapper.ps1` -> `CLONE_WRAPPER_TEST_PASS`; `test-libvirt-task3-validation.ps1` -> `TASK3_FOCUSED_TESTS_OK: 5 assertions`; `git diff --check` exited 0.
- Review scope: no blocking code defect found. Manual libvirt/Ubuntu start and root CMD forwarding were verified by source inspection, not live execution.
- Public release audit exited 0 before the code commit. Staged set was limited to the seven code, test, and documentation files for this task. The checkpoint log will be stored in a separate commit.
- At the time of the original code commit, no WSL, Docker, libvirt, VM, RDP, or live remote test had been run. The live follow-up below supersedes this limit for stack startup only.

## Live verification follow-up (2026-10-02)

- User authorized a real start test. Before the call, `wsl.exe --list --verbose` reported no installed distributions; the H: WSL VHDX, cutover marker, and repo-local cloudflared executable exist. No cloudflared Windows process was found.
- Next: run `START-REMOTE.cmd start` with output captured under ignored `runtime/logs`, inspect exit code and sanitized output, then read Windows and Ubuntu VM states and service status. Do not print the temporary tunnel URL or credentials.
- Do not stop pre-existing VMs for the test. With no WSL distro registered, no WSL-hosted VM is currently running.
- First real `START-REMOTE.cmd start` registered/started WSL, Docker/Compose, libvirt network/storage and route, and Quick Tunnel. Its final success message appeared in the captured log, but the PowerShell output-capture process remained open after the CMD child exited; only that output-capture process was stopped. Cloudflared stayed running. The full first-run exit code was therefore not captured.
- A second real `START-REMOTE.cmd start` returned exit code 0 and printed the final success message. Post-run: Compose services `guacamole`, `guacd`, and `postgres` each `running`; Guacamole HTTP returned 200; local ports 8080 and 9090 accepted connections; `virsh list --state-running --name` returned 0 domains, `windows11` was `shut off`, Ubuntu demo service was `inactive`, and dedicated libvirt TPM service was `inactive`.
- No VM was manually started, and no RDP or authenticated Guacamole session was attempted. The temporary Quick Tunnel URL was not printed in the verification record. The live stack remains running after verification.

## Manual Windows 11 start failure follow-up (2026-10-02)

- User reported Cockpit/libvirt failure: QEMU could not connect to `/run/guacamole-vm-windows11/swtpm.sock` because it did not exist.
- Read-only live evidence: `windows11` is `shut off`; the dedicated `guacamole-vm-windows11-libvirt-tpm.service` is `inactive` and disabled; `test -S /run/guacamole-vm-windows11/swtpm.sock` exited 1. The domain XML uses an external TPM backend that connects to that socket. Legacy QEMU/TPM units are inactive.
- Source ownership: `libvirt.ps1 -Action start` starts the TPM service and waits for the socket before starting the domain. The default stack start no longer calls that action, so a direct Cockpit Start currently skips the prerequisite.
- Next: test the single hypothesis by starting the dedicated TPM service and retrying libvirt VM start; then implement the smallest persistent setup that keeps the TPM socket ready while the stack is up without auto-starting a VM. Keep the existing VM disk, NVRAM, and TPM state intact.
- Hypothesis test: `systemctl start guacamole-vm-windows11-libvirt-tpm.service` exited 0, `systemctl is-active` returned `active`, and `test -S /run/guacamole-vm-windows11/swtpm.sock` exited 0. A direct `virsh -c qemu:///system start windows11` then exited 0 and reported `running`. The Windows VM is now running and was left on for the user.
- Source fix: added `libvirt.ps1 -Action prepare-tpm` using the existing ownership guard and socket wait; default recovery `start` calls it after network/storage/route and does not start a domain. Local-only startup documentation now includes the TPM preparation action.
- TDD: the updated startup regression failed before the source fix because recovery omitted `libvirt.ps1:prepare-tpm`, then passed after implementation (`START_DEFAULT_NO_VM_TEST_PASS`). PowerShell parser, libvirt task-3 and task-4 tests, and `git diff --check` exited 0.
- Live verification after source fix: `libvirt.ps1 -Action prepare-tpm` returned `LIBVIRT_TPM_READY` and the socket existed. `START-REMOTE.cmd start` returned exit code 0 with `LIBVIRT_TPM_READY` in the captured log; the socket remained present and Windows 11 remained `running`.
- The exact off-to-ready-to-Cockpit-Start sequence was established by the separate TPM-start plus direct `virsh start` hypothesis test. It was not repeated after the source fix because the Windows VM is now running; stopping it would interrupt the user. No RDP or authenticated Guacamole test has been run for this fix.
- Read-only diff review and final public release audit/commit/push are pending.

## Review fixes and full live reproduction

- Review found that unconditional TPM preparation would break clean install before libvirt cutover and could accept a stale/wrong-owner socket. Recovery now calls `prepare-tpm` only when the cutover marker exists. The TPM preparation action itself refuses to start without the marker and waits for `Get-TpmOwnerEvidence.DedicatedOwnerReady`, which verifies active service, PID, command line, socket and owner.
- Added focused `test-prepare-tpm.ps1` for missing cutover marker, wrong TPM owner, and ready owner. Updated `test-start-default-no-vm.ps1` to cover both post-cutover and pre-cutover recovery call orders. Both tests passed locally.
- A live recheck initially failed before TPM preparation because libvirt network `guac-nat` was inactive while bridge `virbr-guac` remained as an orphan. No libvirt domain or interface was attached to that bridge; deleting the orphan bridge and starting `guac-nat` succeeded. This was a separate runtime network state issue and did not require source changes in this fix.
- Full live sequence after the fix: `windows11` was `shut off`; `START-REMOTE.cmd start` returned exit code 0 on the second run and logged `LIBVIRT_TPM_READY`; socket test exited 0 while Windows was still off; direct `virsh -c qemu:///system start windows11` then exited 0 and `windows11` became `running`. The initial post-repair start completed its CMD child but the PowerShell output capture stayed open because cloudflared inherited the redirected handles; only that capture process was stopped. Tunnel was left running.
- Final focused test set: `test-start-default-no-vm.ps1` covered both marker present and absent; `test-prepare-tpm.ps1` covered missing marker, owner mismatch, and ready owner; libvirt task-3 and task-4 validation, PowerShell parse, and whitespace check all passed. The tests do not run a full authenticated RDP login.
- The Windows VM is running at this checkpoint. No authenticated RDP session was used. Source review, final audit, commit and push remain to finish.
