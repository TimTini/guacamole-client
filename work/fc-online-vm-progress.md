# FC Online in Windows 11 VM

## Goal and scope

Run FC Online inside the existing Windows 11 VM, reusing host game files if practical. Verify whether the launcher/game and XIGNCODE start in the guest. Preserve the host installation and VM state.

## Baseline, 2026-10-02

- Repo `main...origin/main` was clean before this investigation; no repo `AGENTS.md` found.
- Current `windows11` libvirt domain is running at `192.168.250.11/24`.
- Guest has 8 GiB RAM, 4 vCPU, QXL/SPICE video, and no GPU passthrough or QEMU guest agent channel (live `virsh dumpxml`).
- Existing `work/manual-vm-start-progress.md` records the TPM startup fix and prior live VM start. It did not test authenticated RDP or game launch.
- Host FC Online file inventory completed. Do not disclose credentials or copy live state blindly.

## Remaining work

1. From Windows, confirm the `FCONLINE` volume has a drive letter and that `Games\32837` and `Garena` are visible. The user requested no further GUI operation by the agent.
2. Free space on guest C: before Garena installation or Windows updates; it had under 1 MiB free.
3. Import/repair the copied files in Garena. The original game root had broken symlinks, so launcher files at that level need reconstruction or verification by Garena.
4. Launch the game in the guest and record launcher/XIGNCODE/DirectX outcome. Gameplay has not been tested; do not bypass anti-cheat.

## Checkpoint: host files and guest access

- Read-only host inventory found game data at `H:\Garena\Games\32837` (2,512 files, 31,752,773,039 bytes, about 29.57 GiB), with real payload mainly under `mojitodata` and `launcher`. Garena client is at `H:\Garena\Garena` (about 0.18 GiB).
- Top-level symlinks such as `data`, `fczf.exe`, and `Xigncode` point at missing `H:\Garena\game\32837\mojitodata\...`. Transfer must not preserve these broken absolute links. Do not copy `C:\ProgramData\Garena\gxx\user` session data.
- Live `windows11` remains running at `192.168.250.11`; Guacamole HTTP responds 200. `test-rdp.ps1 -HostName 192.168.250.11 -Port 3389` returned `WIN11_RDP_AUTH_OK` (authenticated transport only, no GUI/game).
- `qemu-img info -U` reports a 100 GiB virtual disk with 62.5 GiB allocated on host. This does not establish free space inside Windows.
- Guest XML has no qemu-guest-agent channel. SMB/WinRM/SSH ports are closed from WSL; RDP 3389 is open. A bounded FreeRDP alternate-shell probe authenticated but did not produce a guest result file. It was terminated after 30 seconds and did not transfer or run the game.
- The user logged in to Guacamole. A guest CLI check via the session showed C: had only 983,552 bytes free. The user then required CLI only for game data movement and no further GUI interaction.

## Checkpoint: CLI-only transfer, 2026-10-02

- User requires CLI only for moving game data; if that cannot work, stop and let the user copy. No further GUI interaction.
- Earlier guest CLI showed C: has only 983,552 bytes free. A separate NTFS data disk is required for the ~29.57 GiB game payload.
- WSL host has 829 GiB free under `/var/lib/guacamole-vm-windows11`; `qemu-img`, `losetup`, `parted`, `mkfs.ntfs`, `ntfs-3g`, and `rsync` are installed. The VM is running and currently has only `sda` attached.
- Plan: create a new 60 GiB sparse raw disk, format it NTFS while offline, copy only real host game/client directories, unmount, attach it to VM, and verify libvirt block state. The existing Windows disk and host game installation remain untouched.
- Outstanding: inspect the disk copy result, Windows drive assignment, launcher/game and XIGNCODE behavior. Guest C: capacity remains an independent blocker.

## Checkpoint: NTFS staging in progress

- Saved the existing domain XML to `/var/lib/guacamole-vm-windows11/windows11-before-fc-disk-20261002.xml`.
- Created `/var/lib/guacamole-vm-windows11/fc-online-data.raw` as a sparse 60 GiB disk, made a GPT partition and NTFS `FCONLINE` volume, mounted at `/mnt/fc-online-stage` via `/dev/loop0p1`.
- CLI `rsync` is currently copying the real `mojitodata`, `launcher`, `game.json`, and `launch_fco.exe` to `Games/32837`, then Garena client files to `Garena`. The host files are read-only sources.
- Original game root has 40 broken absolute symlinks (3 directories and 37 files). They are intentionally excluded. Garena may need to repair/rebuild root links during import or update; launch has not been verified.

## Checkpoint: transfer and disk attach completed

- First `rsync` exited 0: 2,467 regular game files, 31,751,968,483 bytes. Second `rsync` exited 0: 292 regular Garena client files, 194,840,056 bytes.
- Read-only `rsync -rtn --stats` against both destinations found 0 files needing transfer. This checks metadata/size/time, not every content hash.
- NTFS `FCONLINE` had 31 GiB available after the copy. It was unmounted and `ntfsfix -n /dev/loop0p1` completed successfully; the loop device was detached. `qemu-img info` reported a 60 GiB virtual disk using about 29.8 GiB physically.
- SATA hotplug attempt failed with `disk bus 'sata' cannot be hotplugged`; no SATA disk was attached. USB attach via the existing qemu-xhci controller succeeded with `virsh attach-disk ... sdb --targetbus usb --subdriver raw --sourcetype file --live --config`.
- `virsh domblklist windows11` now shows `sdb` pointing to `/var/lib/guacamole-vm-windows11/fc-online-data.raw`; inactive XML also records it, and VM remains running. This confirms libvirt attachment, not Windows drive-letter assignment.
- Guest has no QEMU guest agent, SMB/WinRM/SSH are not available, and the user prohibited GUI interaction. Therefore Windows recognition of the new volume, Garena import, launcher/XIGNCODE startup, and actual gameplay remain unverified. C: having under 1 MiB free is a known independent blocker for installs/updates.
- Host game data at `H:\Garena` remains intact. No VM OS disk data was changed.
