# Windows 11 template storage

The future `windows11-v1` qcow2 template and its inventory belong under
`/var/lib/guacamole-templates` inside the `Ubuntu-24.04` WSL distribution. The
WSL distribution uses the H-backed ext4 disk at
`H:\RemoteWorkspaces\guacamole-client\runtime\ubuntu\ext4.vhdx`; qcow2 files
must stay inside that WSL filesystem.

Do not copy template or clone qcow2 files to `C:` and do not commit them to
Git. The repository stores only this documentation and generated record paths
are excluded by the root `.gitignore` entries for `runtime/templates/` and
`runtime/workspace-operations/`.
