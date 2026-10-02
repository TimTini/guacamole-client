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
