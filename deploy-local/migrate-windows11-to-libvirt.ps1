[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('preflight', 'backup', 'define', 'smoke-test', 'cutover', 'rollback', 'status')]
    [string]$Action = 'status',

    [int]$TimeoutSeconds = 180,

    [ValidatePattern('^windows11-pre-libvirt-\d{8}-\d{6}$')]
    [string]$CheckpointName = '',

    [switch]$AllowRollbackAfterCutover
)

$ErrorActionPreference = 'Stop'
$ScriptRoot = $PSScriptRoot
$RepoRoot = (Resolve-Path (Join-Path $ScriptRoot '..')).Path
$WslDistro = 'Ubuntu-24.04'
$WslRepoRoot = '/mnt/h/RemoteWorkspaces/guacamole-client'
$WslDeployRoot = "$WslRepoRoot/deploy-local"
$DomainName = 'windows11'
$WslDiskPath = '/var/lib/guacamole-vm-windows11/windows11.qcow2'
$WslSourceNvramPath = "$WslRepoRoot/runtime/vm-windows11/OVMF_VARS_4M.ms.fd"
$WslActiveStateRoot = '/var/lib/guacamole-vm-windows11/libvirt'
$WslActiveNvramPath = "$WslActiveStateRoot/OVMF_VARS_4M.ms.fd"
$WslTpmSocketPath = '/run/guacamole-vm-windows11/swtpm.sock'
$TpmUnitName = 'guacamole-vm-windows11-libvirt-tpm.service'
$LegacyQemuUnitName = 'guacamole-vm-windows11.service'
$LegacyTpmUnitName = 'guacamole-vm-windows11-tpm.service'
$WslLegacyTpmPath = "$WslRepoRoot/runtime/vm-windows11/tpm"
$WslContractTpmPath = '/var/lib/guacamole-vm-windows11/tpm'
$TemplateWslPath = "$WslDeployRoot/libvirt/domains/windows11.xml.template"
$GeneratedWslPath = "$WslDeployRoot/libvirt/windows11.generated.xml"
$TpmUnitWslPath = "/etc/systemd/system/$TpmUnitName"
$MarkerPath = Join-Path $RepoRoot 'runtime\vm-windows11\libvirt-cutover.marker'
$StatePath = Join-Path $RepoRoot 'runtime\vm-windows11\libvirt-migration-state.json'
$EvidenceRoot = Join-Path $RepoRoot 'runtime\vm-windows11\libvirt-migration'
$script:EvidenceOverride = $null
$LegacyScriptPath = Join-Path $ScriptRoot 'vm-windows11\windows11.ps1'
$RdpTestPath = Join-Path $ScriptRoot 'vm-windows11\test-rdp.ps1'
$LibvirtHelperPath = Join-Path $ScriptRoot 'libvirt.ps1'
$ComposePath = "$WslDeployRoot/compose.yaml"

function Invoke-WslOutput {
    param(
        [Parameter(Mandatory)][string]$Command,
        [switch]$AllowFailure
    )

    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = @(& wsl.exe -d $WslDistro -u root -- sh -lc $Command 2>&1)
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
    $text = (($output | ForEach-Object { $_.ToString() }) -join [Environment]::NewLine).Trim()
    if ($exitCode -ne 0 -and -not $AllowFailure) {
        throw "WSL command failed ($exitCode): $Command`n$text"
    }
    [pscustomobject]@{ Output = $text; ExitCode = $exitCode }
}

function Invoke-WslCommand {
    param([Parameter(Mandatory)][string]$Command, [switch]$AllowFailure)
    Invoke-WslOutput -Command $Command -AllowFailure:$AllowFailure | Out-Null
}

function Invoke-WslScript {
    param(
        [Parameter(Mandatory)][string]$Script,
        [switch]$AllowFailure
    )
    $flat = (($Script -split [Environment]::NewLine | ForEach-Object { $_.TrimEnd() } | Where-Object { $_ }) -join ' ')
    $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($flat))
    Invoke-WslOutput -Command "printf '%s' '$encoded' | base64 -d | sh" -AllowFailure:$AllowFailure
}

function Write-Evidence {
    param([Parameter(Mandatory)][string]$Name, [Parameter(Mandatory)][AllowEmptyString()][string]$Text)
    $root = if ($null -ne $script:EvidenceOverride) { $script:EvidenceOverride } else { $EvidenceRoot }
    New-Item -ItemType Directory -Force -Path $root | Out-Null
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    [IO.File]::WriteAllText((Join-Path $root $Name), $Text, $utf8)
}

function Write-State {
    param([Parameter(Mandatory)][hashtable]$Values)
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $StatePath) | Out-Null
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($StatePath, (($Values | ConvertTo-Json -Depth 8)), $utf8)
}

function Read-State {
    if (-not (Test-Path -LiteralPath $StatePath -PathType Leaf)) { return @{} }
    $state = Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json
    $result = @{}
    foreach ($property in $state.PSObject.Properties) { $result[$property.Name] = $property.Value }
    return $result
}

function Get-LatestCheckpoint {
    $root = Join-Path $RepoRoot 'runtime\backups'
    $checkpoint = Get-ChildItem -LiteralPath $root -Directory -Filter 'windows11-pre-libvirt-*' -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($null -eq $checkpoint) { throw "CHECKPOINT_NOT_FOUND: no windows11-pre-libvirt-* checkpoint exists under $root." }
    return $checkpoint
}

function Get-RollbackCheckpoint {
    $state = Read-State
    $selectedName = $CheckpointName
    if ([string]::IsNullOrWhiteSpace($selectedName)) {
        if (-not $state.ContainsKey('backup_checkpoint')) {
            throw 'ROLLBACK_CHECKPOINT_REQUIRED: state does not select a backup checkpoint.'
        }
        $selectedName = [string]$state['backup_checkpoint']
    }
    if ($selectedName -notmatch '^windows11-pre-libvirt-\d{8}-\d{6}$') {
        throw "ROLLBACK_CHECKPOINT_NAME_INVALID: '$selectedName'."
    }
    $checkpointPath = Join-Path (Join-Path $RepoRoot 'runtime\backups') $selectedName
    if (-not (Test-Path -LiteralPath $checkpointPath -PathType Container)) {
        throw "ROLLBACK_CHECKPOINT_MISSING: '$checkpointPath'."
    }
    $manifestPath = Join-Path $checkpointPath 'checkpoint-manifest.json'
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        throw "ROLLBACK_CHECKPOINT_MANIFEST_MISSING: '$manifestPath'."
    }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    if ([string]$manifest.checkpoint -ne $selectedName -or
        [string]$manifest.nvram_checkpoint -ne 'OVMF_VARS_4M.ms.fd' -or
        [string]$manifest.tpm_checkpoint -ne 'tpm/' -or
        [string]$manifest.legacy_qcow2_wsl_path -ne $WslDiskPath -or
        [bool]$manifest.legacy_qcow2_copied) {
        throw "ROLLBACK_CHECKPOINT_MANIFEST_MISMATCH: '$manifestPath'."
    }
    [pscustomobject]@{
        Name = $selectedName
        Path = $checkpointPath
        WslPath = "$WslRepoRoot/runtime/backups/$selectedName"
    }
}

function Assert-CheckpointState {
    param([Parameter(Mandatory)][pscustomobject]$Checkpoint)

    $manifestPath = Join-Path $Checkpoint.Path 'state-sha256sums.txt'
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        throw "ROLLBACK_STATE_MANIFEST_MISSING: '$manifestPath'."
    }
    $manifestText = Get-Content -LiteralPath $manifestPath -Raw
    $allowedAbsolutePrefix = "/mnt/h/RemoteWorkspaces/guacamole-client/runtime/backups/$($Checkpoint.Name)/"
    foreach ($line in @($manifestText -split [Environment]::NewLine | Where-Object { $_ })) {
        if ($line -notmatch '^[0-9a-f]{64}\s{2}(.+)$') {
            throw "ROLLBACK_STATE_MANIFEST_ENTRY_INVALID: '$manifestPath'."
        }
        $entryPath = $Matches[1]
        if ($entryPath -match '\.incomplete|(^|/)\.\.?(/|$)' -or
            ($entryPath.StartsWith('/') -and -not $entryPath.StartsWith($allowedAbsolutePrefix)) -or
            (-not $entryPath.StartsWith('/') -and $entryPath -notmatch '^(tpm/|OVMF_VARS_4M\.ms\.fd$)')) {
            throw "ROLLBACK_STATE_MANIFEST_PATH_INVALID: '$manifestPath'."
        }
    }
    $verifyScript = @'
set -eu
checkpoint='__CHECKPOINT__'
test -f "$checkpoint/OVMF_VARS_4M.ms.fd"
test -d "$checkpoint/tpm"
test ! -L "$checkpoint/OVMF_VARS_4M.ms.fd"
if find "$checkpoint/tpm" -type l -print -quit | grep -q .; then
  echo 'ROLLBACK_CHECKPOINT_SYMLINK_FORBIDDEN' >&2
  exit 42
fi
(cd "$checkpoint" && sha256sum -c state-sha256sums.txt)
'@
    $verifyScript = $verifyScript.Replace('__CHECKPOINT__', $Checkpoint.WslPath)
    $result = Invoke-WslScript -Script $verifyScript -AllowFailure
    if ($result.ExitCode -ne 0 -or $result.Output -match '(?m)(FAILED|No such file|WARNING:.*mismatch)') {
        throw "ROLLBACK_CHECKPOINT_STATE_INVALID: $($result.Output)"
    }
    Write-Evidence -Name 'rollback-checkpoint-verify.txt' -Text ($result.Output + [Environment]::NewLine + "checkpoint=$($Checkpoint.Name)")
}

function ConvertTo-SqlLiteral {
    param([Parameter(Mandatory)][string]$Value)
    return "'" + $Value.Replace("'", "''") + "'"
}

function Get-CheckpointGuacamoleTarget {
    param([Parameter(Mandatory)][pscustomobject]$Checkpoint)

    $connectionsPath = Join-Path $Checkpoint.Path 'guacamole-connections.csv'
    if (-not (Test-Path -LiteralPath $connectionsPath -PathType Leaf)) {
        throw "ROLLBACK_GUACAMOLE_CONNECTION_EXPORT_MISSING: '$connectionsPath'."
    }
    $rows = @(Import-Csv -LiteralPath $connectionsPath | Where-Object { $_.connection_name -eq 'Windows 11' })
    if ($rows.Count -ne 1) { throw "ROLLBACK_GUACAMOLE_CONNECTION_NOT_EXACTLY_ONE: found $($rows.Count)." }
    $row = $rows[0]
    $connectionId = 0
    $port = 0
    if (-not [int]::TryParse([string]$row.connection_id, [ref]$connectionId) -or $connectionId -le 0 -or
        [string]$row.protocol -ne 'rdp' -or [string]::IsNullOrWhiteSpace([string]$row.hostname) -or
        -not [int]::TryParse([string]$row.port, [ref]$port) -or $port -lt 1 -or $port -gt 65535 -or
        [string]$row.hostname -notmatch '^[A-Za-z0-9_.:-]+$') {
        throw "ROLLBACK_GUACAMOLE_CONNECTION_EXPORT_INVALID: '$connectionsPath'."
    }
    [pscustomobject]@{ ConnectionId = $connectionId; Hostname = [string]$row.hostname; Port = $port }
}

function Restore-GuacamoleFromCheckpoint {
    param([Parameter(Mandatory)][pscustomobject]$Checkpoint)

    $target = Get-CheckpointGuacamoleTarget -Checkpoint $Checkpoint
    $before = Get-GuacamoleInventory
    Write-Evidence -Name 'rollback-guacamole-inventory-before.csv' -Text $before
    $beforeLines = @($before -split [Environment]::NewLine | Where-Object { $_ -and $_ -match '^(\d+),Windows 11,' })
    if ($beforeLines.Count -ne 1 -or ($beforeLines[0] -split ',', 2)[0] -ne [string]$target.ConnectionId) {
        throw 'ROLLBACK_GUACAMOLE_CONNECTION_ID_CHANGED'
    }
    $beforePermissions = ($before -split [Environment]::NewLine | Where-Object { $_ -match '^(connection|user|group),' }) -join "`n"
    $hostnameLiteral = ConvertTo-SqlLiteral -Value $target.Hostname
    $sql = @'
BEGIN;
DO $$ DECLARE selected_id integer; selected_count integer; BEGIN
  SELECT count(*) INTO selected_count FROM guacamole_connection WHERE connection_name='Windows 11';
  IF selected_count <> 1 THEN RAISE EXCEPTION 'Windows 11 connection count is %', selected_count; END IF;
  SELECT connection_id INTO selected_id FROM guacamole_connection WHERE connection_name='Windows 11';
  IF selected_id <> __CONNECTION_ID__ THEN RAISE EXCEPTION 'Windows 11 connection id changed to %', selected_id; END IF;
  INSERT INTO guacamole_connection_parameter(connection_id,parameter_name,parameter_value) VALUES(selected_id,'hostname',__HOSTNAME__) ON CONFLICT(connection_id,parameter_name) DO UPDATE SET parameter_value=EXCLUDED.parameter_value;
  INSERT INTO guacamole_connection_parameter(connection_id,parameter_name,parameter_value) VALUES(selected_id,'port','__PORT__') ON CONFLICT(connection_id,parameter_name) DO UPDATE SET parameter_value=EXCLUDED.parameter_value;
END $$;
COMMIT;
'@
    $sql = $sql.Replace('__CONNECTION_ID__', [string]$target.ConnectionId).Replace('__HOSTNAME__', $hostnameLiteral).Replace('__PORT__', [string]$target.Port)
    $result = Invoke-PostgresSql -Sql $sql -StopOnError
    Write-Evidence -Name 'rollback-guacamole-update-output.txt' -Text $result.Output
    $after = Get-GuacamoleInventory
    Write-Evidence -Name 'rollback-guacamole-inventory-after.csv' -Text $after
    if ($after -notmatch ("(?m)^" + [regex]::Escape([string]$target.ConnectionId) + ',Windows 11,rdp,' + [regex]::Escape($target.Hostname) + ',' + [regex]::Escape([string]$target.Port) + '$')) {
        throw 'ROLLBACK_GUACAMOLE_TARGET_RESTORE_FAILED'
    }
    $afterPermissions = ($after -split [Environment]::NewLine | Where-Object { $_ -match '^(connection|user|group),' }) -join "`n"
    if ($beforePermissions -ne $afterPermissions) { throw 'ROLLBACK_GUACAMOLE_PERMISSIONS_CHANGED' }
    Write-Host 'GUAC_CONNECTION_ID_PRESERVED'
    Write-Host 'GUAC_PERMISSIONS_PRESERVED'
    return $target
}

function Get-TpmStatePath {
    $probe = Invoke-WslOutput -Command "if test -d '$WslLegacyTpmPath'; then printf '%s' '$WslLegacyTpmPath'; elif test -d '$WslContractTpmPath'; then printf '%s' '$WslContractTpmPath'; else exit 42; fi" -AllowFailure
    if ($probe.ExitCode -ne 0 -or [string]::IsNullOrWhiteSpace($probe.Output)) {
        throw 'LIBVIRT_TPM_STATE_MISSING: existing TPM state was not found; migration will not initialize or replace it.'
    }
    return $probe.Output.Trim()
}

function Get-OwnerEvidence {
    $script = @'
set -eu
for unit in guacamole-vm-windows11.service guacamole-vm-windows11-tpm.service guacamole-vm-windows11-libvirt-tpm.service; do
  state=$(systemctl show -p ActiveState --value "$unit" 2>/dev/null || true)
  pid=$(systemctl show -p MainPID --value "$unit" 2>/dev/null || printf 0)
  printf '%s=%s pid=%s\n' "$unit" "$state" "$pid"
done
qemu=$(pgrep -af '[q]emu-system-x86_64.*-name guacamole-vm-windows11([[:space:]]|$)' || true)
libvirt_qemu=$(pgrep -af '[q]emu-system-x86_64.*-name (guest=)?windows11([,[:space:]]|$)' || true)
swtpm=$(pgrep -af '[s]wtpm.*guacamole-vm-windows11' || true)
printf 'qemu_matches=%s\n' "$qemu"
printf 'libvirt_qemu_matches=%s\n' "$libvirt_qemu"
printf 'swtpm_matches=%s\n' "$swtpm"
if test -S '/run/guacamole-vm-windows11/swtpm.sock'; then printf 'tpm_socket=present\n'; else printf 'tpm_socket=absent\n'; fi
'@
    return (Invoke-WslScript -Script $script).Output
}

function Assert-NoLegacyOwner {
    $evidence = Get-OwnerEvidence
    Write-Evidence -Name 'owner-evidence.txt' -Text $evidence
    if ($evidence -match 'guacamole-vm-windows11\.service=(active|activating)(\s|$)|guacamole-vm-windows11-tpm\.service=(active|activating)(\s|$)|qemu_matches=\S') {
        throw "LEGACY_OWNER_STILL_ACTIVE: stop the legacy QEMU/TPM owner before migration.`n$evidence"
    }
}

function Assert-NoLibvirtRuntimeOwner {
    $evidence = Get-OwnerEvidence
    Write-Evidence -Name 'rollback-owner-gate.txt' -Text $evidence
    if ($evidence -match 'guacamole-vm-windows11-libvirt-tpm\.service=(active|activating)(\s|$)|libvirt_qemu_matches=\S|swtpm_matches=\S|tpm_socket=present') {
        throw "LIBVIRT_RUNTIME_OWNER_STILL_ACTIVE: stop the libvirt domain and dedicated TPM before restoring state.`n$evidence"
    }
}

function Invoke-RdpTest {
    param([Parameter(Mandatory)][string]$HostName, [Parameter(Mandatory)][int]$Port)
    if (-not (Test-Path -LiteralPath $RdpTestPath -PathType Leaf)) { throw "RDP_TEST_SCRIPT_MISSING: $RdpTestPath" }
    $result = @(& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $RdpTestPath -HostName $HostName -Port $Port 2>&1)
    $exitCode = $LASTEXITCODE
    $text = ($result | ForEach-Object { $_.ToString() }) -join [Environment]::NewLine
    Write-Host $text
    return [pscustomobject]@{ ExitCode = $exitCode; Output = $text }
}

function Invoke-Preflight {
    if (Test-Path -LiteralPath $MarkerPath -PathType Leaf) { throw 'LIBVIRT_CUTOVER_ALREADY_MARKED: legacy preflight is forbidden after cutover.' }
    if (-not (Test-Path -LiteralPath $RepoRoot\runtime\ubuntu\ext4.vhdx -PathType Leaf)) { throw 'H_BACKED_VHDX_MISSING' }
    if (-not (Test-Path -LiteralPath $RepoRoot\runtime\vm-windows11\install-finished.marker -PathType Leaf)) { throw 'WIN11_INSTALL_MARKER_MISSING' }
    # The qcow2 is live during this gate, so integrity checking is deferred to
    # the backup phase after the legacy owner has stopped and released its lock.
    Invoke-WslOutput -Command "test -f '$WslDiskPath'" | Out-Null
    $tpmPath = Get-TpmStatePath
    Write-Evidence -Name 'preflight-tpm-state.txt' -Text "tpm_state=$tpmPath`nowner=$(Get-OwnerEvidence)"
    $legacyStatus = @(& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $LegacyScriptPath status 2>&1)
    Write-Evidence -Name 'preflight-legacy-status.txt' -Text (($legacyStatus | ForEach-Object { $_.ToString() }) -join [Environment]::NewLine)
    $unitText = (Invoke-WslOutput -Command "systemctl cat '$LegacyQemuUnitName'" -AllowFailure).Output
    $tpmUnitText = (Invoke-WslOutput -Command "systemctl cat '$LegacyTpmUnitName'" -AllowFailure).Output
    Write-Evidence -Name 'preflight-legacy-units.txt' -Text "$unitText`n$tpmUnitText"
    if ($unitText -match '(?i)\.iso|windows11-unattend') { throw 'LEGACY_QEMU_INSTALL_MEDIA_PRESENT' }
    $rdp = Invoke-RdpTest -HostName '127.0.0.1' -Port 3391
    if ($rdp.Output -notmatch 'WIN11_RDP_AUTH_OK') { throw 'WIN11_RDP_AUTH_REQUIRED: legacy endpoint did not authenticate.' }
    Write-Host 'WIN11_LEGACY_PREFLIGHT_OK'
    Write-Host 'LIBVIRT_PREFLIGHT_OK'
}

function Invoke-Backup {
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $LibvirtHelperPath backup
    if ($LASTEXITCODE -ne 0) { throw 'LIBVIRT_BACKUP_FAILED' }
    $checkpoint = Get-LatestCheckpoint
    $state = Read-State
    $state['backup_checkpoint'] = $checkpoint.Name
    $state['backup_path'] = $checkpoint.FullName
    $state['backup_utc'] = [DateTime]::UtcNow.ToString('o')
    $state['phase'] = 'backup'
    Write-State -Values $state
    Write-Evidence -Name 'backup-checkpoint.txt' -Text "checkpoint=$($checkpoint.FullName)`nbackup_utc=$($state['backup_utc'])"
    Write-Host "CHECKPOINT=$($checkpoint.FullName)"
    Write-Host 'LIBVIRT_BACKUP_PHASE_OK'
}

function Install-TpmUnit {
    $tpmPath = Get-TpmStatePath
    $identityCommand = 'printf ''qemu_uid=%s\nkvm_gid=%s\n'' "$(id -u libvirt-qemu)" "$(getent group kvm | cut -d: -f3)"'
    $identity = Invoke-WslOutput -Command $identityCommand
    if ($identity.Output -notmatch '(?m)^qemu_uid=\d+$' -or $identity.Output -notmatch '(?m)^kvm_gid=\d+$') {
        throw "LIBVIRT_IDENTITY_MISSING: expected libvirt-qemu and kvm IDs. $($identity.Output)"
    }
    $qemuUid = (($identity.Output -split [Environment]::NewLine | Where-Object { $_ -match '^qemu_uid=' }) -split '=', 2)[1]
    $kvmGid = (($identity.Output -split [Environment]::NewLine | Where-Object { $_ -match '^kvm_gid=' }) -split '=', 2)[1]
    $unit = @"
[Unit]
Description=Windows 11 external TPM for libvirt migration
After=local-fs.target
Before=libvirtd.service

[Service]
Type=simple
User=root
Group=root
RuntimeDirectory=guacamole-vm-windows11
RuntimeDirectoryMode=0777
ExecStartPre=/bin/rm -f $WslTpmSocketPath
ExecStartPre=/bin/chown libvirt-qemu:kvm /run/guacamole-vm-windows11
ExecStart=/usr/bin/swtpm socket --tpm2 --tpmstate dir=$tpmPath --ctrl type=unixio,path=$WslTpmSocketPath,mode=0660,uid=$qemuUid,gid=$kvmGid --log file=$tpmPath/swtpm-libvirt.log
KillSignal=SIGTERM
Restart=on-failure
RestartSec=3s
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
"@
    $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($unit))
    $script = "set -eu; printf '%s' '$encoded' | base64 -d > '$TpmUnitWslPath'; chmod 0644 '$TpmUnitWslPath'; systemctl daemon-reload"
    Invoke-WslCommand -Command $script
    Write-Evidence -Name 'libvirt-tpm.service.txt' -Text ($unit -replace '(?i)(password|secret|token)=\S+', '$1=<redacted>')
    return $tpmPath
}

function Copy-ActiveNvram {
    $source = $WslSourceNvramPath
    $script = @"
set -eu
test -f '$source'
mkdir -p '$WslActiveStateRoot'
test '$source' != '$WslActiveNvramPath'
if test -e '$WslActiveNvramPath'; then cmp -s '$source' '$WslActiveNvramPath' || { echo 'LIBVIRT_NVRAM_CONFLICT' >&2; exit 43; }; else cp --reflink=auto '$source' '$WslActiveNvramPath'; fi
chmod 0660 '$WslActiveNvramPath'
chown libvirt-qemu:kvm '$WslActiveNvramPath'
test -r '$WslActiveNvramPath'
"@
    Invoke-WslScript -Script $script | Out-Null
}

function Render-DomainXml {
    $tpmPath = Get-TpmStatePath
    $render = @"
set -eu
test -f '$TemplateWslPath'
sed -e 's#__W11_DISK_PATH__#$WslDiskPath#g' -e 's#__W11_NVRAM_PATH__#$WslActiveNvramPath#g' -e 's#__W11_TPM_SOCKET__#$WslTpmSocketPath#g' '$TemplateWslPath' > '$GeneratedWslPath.tmp'
mv -f '$GeneratedWslPath.tmp' '$GeneratedWslPath'
test -s '$GeneratedWslPath'
grep -F '$WslDiskPath' '$GeneratedWslPath'
grep -F '$WslActiveNvramPath' '$GeneratedWslPath'
grep -F '$WslTpmSocketPath' '$GeneratedWslPath'
if grep -Eiq 'iso|password|secret|trycloudflare|3391|__W11_' '$GeneratedWslPath'; then echo 'LIBVIRT_DOMAIN_XML_FORBIDDEN_FIELD' >&2; exit 44; fi
python3 -c "import xml.etree.ElementTree as ET; ET.parse('$GeneratedWslPath')"
"@
    $result = Invoke-WslScript -Script $render
    if ($result.ExitCode -ne 0) { throw "LIBVIRT_DOMAIN_XML_INVALID: $($result.Output)" }
    Write-Evidence -Name 'windows11.generated.xml' -Text (Invoke-WslOutput -Command "cat '$GeneratedWslPath'").Output
}

function Invoke-Define {
    $state = Read-State
    if (-not $state.ContainsKey('backup_checkpoint')) { throw 'LIBVIRT_DEFINE_REQUIRES_CURRENT_BACKUP' }
    Assert-NoLegacyOwner
    Copy-ActiveNvram
    Install-TpmUnit | Out-Null
    $active = Invoke-WslOutput -Command "systemctl is-active '$TpmUnitName'" -AllowFailure
    if ($active.ExitCode -ne 0) { Invoke-WslCommand -Command "systemctl start '$TpmUnitName'" }
    $socket = Invoke-WslOutput -Command "test -S '$WslTpmSocketPath'" -AllowFailure
    if ($socket.ExitCode -ne 0) { throw 'LIBVIRT_TPM_SOCKET_MISSING' }
    Render-DomainXml
    $existing = Invoke-WslOutput -Command "virsh -c qemu:///system domstate '$DomainName'" -AllowFailure
    if ($existing.ExitCode -eq 0) {
        if ($existing.Output -notmatch '^shut off$') { throw "LIBVIRT_DEFINE_DOMAIN_ACTIVE: '$DomainName' is $($existing.Output); refusing to redefine it." }
        # libvirt 10.0 rejects a second define for this existing UUID and
        # requires --nvram to undefine a domain with pflash variables. Make a
        # same-filesystem copy first, undefine metadata, then restore it before
        # the new definition. The source checkpoint and active file remain.
        $redefineNvramBackup = "$WslActiveNvramPath.redefine-backup"
        Invoke-WslCommand -Command "cp --reflink=auto '$WslActiveNvramPath' '$redefineNvramBackup'; virsh -c qemu:///system undefine '$DomainName' --nvram; cp --reflink=auto '$redefineNvramBackup' '$WslActiveNvramPath'; rm -f '$redefineNvramBackup'; chown libvirt-qemu:kvm '$WslActiveNvramPath'"
    }
    $define = Invoke-WslOutput -Command "virsh -c qemu:///system define '$GeneratedWslPath'"
    Write-Evidence -Name 'define-output.txt' -Text $define.Output
    $dominfo = Invoke-WslOutput -Command "virsh -c qemu:///system dominfo '$DomainName'"
    Write-Evidence -Name 'dominfo.txt' -Text $dominfo.Output
    $state['phase'] = 'define'
    $state['tpm_state_path'] = Get-TpmStatePath
    $state['nvram_path'] = $WslActiveNvramPath
    Write-State -Values $state
    Write-Host 'LIBVIRT_DOMAIN_DEFINED_OK'
}

function Invoke-SmokeTest {
    $network = Invoke-WslOutput -Command "virsh -c qemu:///system net-info guac-nat"
    if ($network.Output -notmatch '(?m)^Active:\s+yes\s*$') { Invoke-WslCommand -Command 'virsh -c qemu:///system net-start guac-nat' }
    $pool = Invoke-WslOutput -Command "virsh -c qemu:///system pool-info guacamole-vms"
    if ($pool.Output -notmatch '(?m)^State:\s+running\s*$') { Invoke-WslCommand -Command 'virsh -c qemu:///system pool-start guacamole-vms' }
    $domainState = Invoke-WslOutput -Command "virsh -c qemu:///system domstate '$DomainName'"
    if ($domainState.Output -notmatch '^running$') { Invoke-WslCommand -Command "virsh -c qemu:///system start '$DomainName'" }
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        $state = (Invoke-WslOutput -Command "virsh -c qemu:///system domstate '$DomainName'" -AllowFailure).Output
        if ($state -match '^running$') { break }
        Start-Sleep -Seconds 2
    } while ([DateTime]::UtcNow -lt $deadline)
    if ($state -notmatch '^running$') { throw "LIBVIRT_DOMAIN_NOT_RUNNING: $state" }
    $leases = Invoke-WslOutput -Command "virsh -c qemu:///system net-dhcp-leases guac-nat"
    Write-Evidence -Name 'dhcp-leases.txt' -Text $leases.Output
    # The pinned Guacamole image has bash but no netcat. Bash's TCP device
    # opens a socket from inside the Compose network without adding a package.
    $dockerCommand = 'docker compose --project-directory ''__DEPLOY__'' --file ''__COMPOSE__'' exec -T guacamole bash -c ''exec 3<>/dev/tcp/192.168.250.11/3389'''
    $dockerCommand = $dockerCommand.Replace('__DEPLOY__', $WslDeployRoot).Replace('__COMPOSE__', $ComposePath)
    $dockerProbe = $null
    $dockerDeadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        $dockerProbe = Invoke-WslOutput -Command $dockerCommand -AllowFailure
        if ($dockerProbe.ExitCode -eq 0) { break }
        Start-Sleep -Seconds 3
    } while ([DateTime]::UtcNow -lt $dockerDeadline)
    Write-Evidence -Name 'docker-rdp-port.txt' -Text $dockerProbe.Output
    if ($dockerProbe.ExitCode -ne 0) { throw "DOCKER_TO_GUEST_RDP_PORT_FAILED: $($dockerProbe.Output)" }
    $rdp = Invoke-RdpTest -HostName '192.168.250.11' -Port 3389
    Write-Evidence -Name 'libvirt-rdp-test.txt' -Text $rdp.Output
    if ($rdp.Output -notmatch 'WIN11_RDP_AUTH_OK') { throw 'LIBVIRT_RDP_AUTH_FAILED' }
    $xml = (Invoke-WslOutput -Command "virsh -c qemu:///system dumpxml '$DomainName'").Output
    Write-Evidence -Name 'windows11.dumpxml' -Text $xml
    if ($xml -match '(?i)<disk[^>]+cdrom|\.iso|3391|password|trycloudflare') { throw 'LIBVIRT_DOMAIN_CONTRACT_FAILED' }
    $stateRecord = Read-State
    $stateRecord['phase'] = 'smoke-test'
    $stateRecord['smoke_test_utc'] = [DateTime]::UtcNow.ToString('o')
    $stateRecord['direct_rdp'] = 'WIN11_RDP_AUTH_OK'
    $stateRecord['docker_rdp_port'] = 'OK'
    Write-State -Values $stateRecord
    Write-Host 'LIBVIRT_DOMAIN_PERSISTENT_OK'
    Write-Host 'LIBVIRT_TPM_OK'
    Write-Host 'LIBVIRT_NVRAM_OK'
    Write-Host 'LIBVIRT_RDP_OK'
    Write-Host 'GUACAMOLE_SESSION_EVIDENCE_REQUIRED: open Windows 11 through Guacamole before cutover.'
}

function Invoke-PostgresSql {
    param([Parameter(Mandatory)][string]$Sql, [switch]$StopOnError)
    $sqlB64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Sql))
    $flags = if ($StopOnError) { '-v ON_ERROR_STOP=1' } else { '' }
    $inner = 'set -eu; export PGPASSWORD=$(cat /run/secrets/postgres_password); printf ''%s'' ''__SQL__'' | base64 -d | psql -X -q __FLAGS__ -U guacamole_user -d guacamole_db --csv --pset footer=off'
    $inner = $inner.Replace('__SQL__', $sqlB64).Replace('__FLAGS__', $flags)
    $innerB64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($inner))
    $outer = "printf '%s' '$innerB64' | base64 -d | docker compose --project-directory '$WslDeployRoot' --file '$ComposePath' exec -T postgres sh"
    return (Invoke-WslOutput -Command $outer)
}

function Get-GuacamoleInventory {
    $connectionQuery = "select c.connection_id,c.connection_name,c.protocol,coalesce(max(case when p.parameter_name='hostname' then p.parameter_value end),''),coalesce(max(case when p.parameter_name='port' then p.parameter_value end),'') from guacamole_connection c left join guacamole_connection_parameter p on p.connection_id=c.connection_id where c.connection_name='Windows 11' group by c.connection_id,c.connection_name,c.protocol order by c.connection_id;"
    $permissionQuery = "select 'connection' as scope,p.entity_id,e.name,p.connection_id,c.connection_name,'' as affected_id,p.permission::text from guacamole_connection_permission p join guacamole_entity e on e.entity_id=p.entity_id join guacamole_connection c on c.connection_id=p.connection_id union all select 'user' as scope,p.entity_id,e.name,null,'',p.affected_user_id::text,p.permission::text from guacamole_user_permission p join guacamole_entity e on e.entity_id=p.entity_id union all select 'group' as scope,p.entity_id,e.name,null,'',p.affected_user_group_id::text,p.permission::text from guacamole_user_group_permission p join guacamole_entity e on e.entity_id=p.entity_id order by 1,2,4,6,7;"
    $query = "$connectionQuery`n$permissionQuery"
    return (Invoke-PostgresSql -Sql $query).Output
}

function Update-Guacamole {
    $before = Get-GuacamoleInventory
    $lines = @($before -split [Environment]::NewLine | Where-Object { $_ -and $_ -match '^\d+,Windows 11,' })
    if ($lines.Count -ne 1) { throw "GUAC_CONNECTION_NOT_EXACTLY_ONE: found $($lines.Count)" }
    $connectionId = ($lines[0] -split ',', 2)[0]
    Write-Evidence -Name 'guacamole-inventory-before.csv' -Text $before
    $sql = @'
BEGIN;
DO $$ DECLARE selected_id integer; selected_count integer; BEGIN
  SELECT count(*) INTO selected_count FROM guacamole_connection WHERE connection_name='Windows 11';
  IF selected_count <> 1 THEN RAISE EXCEPTION 'Windows 11 connection count is %', selected_count; END IF;
  SELECT connection_id INTO selected_id FROM guacamole_connection WHERE connection_name='Windows 11';
  INSERT INTO guacamole_connection_parameter(connection_id,parameter_name,parameter_value) VALUES(selected_id,'hostname','192.168.250.11') ON CONFLICT(connection_id,parameter_name) DO UPDATE SET parameter_value=EXCLUDED.parameter_value;
  INSERT INTO guacamole_connection_parameter(connection_id,parameter_name,parameter_value) VALUES(selected_id,'port','3389') ON CONFLICT(connection_id,parameter_name) DO UPDATE SET parameter_value=EXCLUDED.parameter_value;
END $$;
COMMIT;
'@
    $result = Invoke-PostgresSql -Sql $sql -StopOnError
    Write-Evidence -Name 'guacamole-update-output.txt' -Text $result.Output
    $after = Get-GuacamoleInventory
    Write-Evidence -Name 'guacamole-inventory-after.csv' -Text $after
    if ($after -notmatch ("(?m)^" + [regex]::Escape($connectionId) + ',Windows 11,rdp,192\.168\.250\.11,3389')) { throw 'GUAC_CONNECTION_TARGET_UPDATE_FAILED' }
    $beforePermissions = ($before -split [Environment]::NewLine | Where-Object { $_ -match '^(connection|user|group),' }) -join "`n"
    $afterPermissions = ($after -split [Environment]::NewLine | Where-Object { $_ -match '^(connection|user|group),' }) -join "`n"
    if ($beforePermissions -ne $afterPermissions) { throw 'GUAC_PERMISSIONS_CHANGED' }
    if (($after -split [Environment]::NewLine | Where-Object { $_ -match ('^' + [regex]::Escape($connectionId) + ',Windows 11,') }).Count -ne 1) { throw 'GUAC_CONNECTION_ID_NOT_PRESERVED' }
    Write-Host 'GUAC_CONNECTION_ID_PRESERVED'
    Write-Host 'GUAC_PERMISSIONS_PRESERVED'
}

function Invoke-Cutover {
    $state = Read-State
    if (-not $state.ContainsKey('backup_checkpoint') -or ($state.phase -ne 'smoke-test' -and $state.phase -ne 'guacamole-updated')) { throw 'LIBVIRT_CUTOVER_REQUIRES_BACKUP_AND_SMOKE_TEST' }
    $sessionEvidencePath = Join-Path $EvidenceRoot 'guacamole-session-success.txt'
    if (-not (Test-Path -LiteralPath $sessionEvidencePath -PathType Leaf)) {
        Update-Guacamole
        $state['phase'] = 'guacamole-updated'
        $state['guacamole_target'] = '192.168.250.11:3389'
        Write-State -Values $state
        throw "GUACAMOLE_SESSION_EVIDENCE_REQUIRED: Guacamole now targets the migrated VM. Complete one real session, write GUACAMOLE_RDP_SESSION_OK to '$sessionEvidencePath', then rerun cutover."
    }
    $evidence = Get-Content -LiteralPath $sessionEvidencePath -Raw
    if ($evidence -notmatch 'GUACAMOLE_RDP_SESSION_OK') { throw 'GUACAMOLE_SESSION_EVIDENCE_INVALID' }
    Update-Guacamole
    $markerLines = @(
        "created_utc=$([DateTime]::UtcNow.ToString('o'))",
        "domain=$DomainName",
        "checkpoint=$($state['backup_checkpoint'])",
        'ownership=libvirt-qemu:///system; external-swtpm-unit',
        'guacamole_session=GUACAMOLE_RDP_SESSION_OK'
    )
    Write-Evidence -Name 'cutover-marker.txt' -Text ($markerLines -join [Environment]::NewLine)
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($MarkerPath, ($markerLines -join [Environment]::NewLine), $utf8)
    $state['phase'] = 'cutover'
    $state['cutover_utc'] = [DateTime]::UtcNow.ToString('o')
    Write-State -Values $state
    Write-Host 'GUAC_CONNECTION_ID_PRESERVED'
    Write-Host 'GUAC_PERMISSIONS_PRESERVED'
    Write-Host 'LIBVIRT_MIGRATION_OK'
}

function Stop-LibvirtRuntime {
    $records = New-Object System.Collections.Generic.List[string]
    $activeDomainPattern = '^(running|paused|blocked|in shutdown|shutting down)$'
    $domain = Invoke-WslOutput -Command "virsh -c qemu:///system domstate '$DomainName'" -AllowFailure
    $records.Add("domain_before=$($domain.Output)")
    if ($domain.ExitCode -eq 0 -and $domain.Output -match $activeDomainPattern) {
        $shutdown = Invoke-WslOutput -Command "virsh -c qemu:///system shutdown '$DomainName'" -AllowFailure
        $records.Add("domain_shutdown=$($shutdown.Output)")
        $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
        do {
            Start-Sleep -Seconds 2
            $domain = Invoke-WslOutput -Command "virsh -c qemu:///system domstate '$DomainName'" -AllowFailure
        } while ($domain.ExitCode -eq 0 -and $domain.Output -match $activeDomainPattern -and [DateTime]::UtcNow -lt $deadline)
        if ($domain.ExitCode -eq 0 -and $domain.Output -match $activeDomainPattern) {
            $destroy = Invoke-WslOutput -Command "virsh -c qemu:///system destroy '$DomainName'" -AllowFailure
            $records.Add("domain_destroy=$($destroy.Output)")
            Start-Sleep -Seconds 2
        }
    }
    $domain = Invoke-WslOutput -Command "virsh -c qemu:///system domstate '$DomainName'" -AllowFailure
    $records.Add("domain_after=$($domain.Output)")
    if ($domain.ExitCode -eq 0 -and $domain.Output -match $activeDomainPattern) {
        throw "ROLLBACK_DOMAIN_STOP_FAILED: $($domain.Output)"
    }
    $tpm = Invoke-WslOutput -Command "systemctl stop '$TpmUnitName'" -AllowFailure
    $records.Add("dedicated_tpm_stop=$($tpm.Output)")
    $tpmDeadline = [DateTime]::UtcNow.AddSeconds(30)
    do {
        Start-Sleep -Seconds 1
        $owner = Get-OwnerEvidence
    } while ($owner -match 'guacamole-vm-windows11-libvirt-tpm\.service=(active|activating)(\s|$)|libvirt_qemu_matches=\S|swtpm_matches=\S|tpm_socket=present' -and [DateTime]::UtcNow -lt $tpmDeadline)
    Write-Evidence -Name 'rollback-shutdown.txt' -Text (($records -join [Environment]::NewLine) + [Environment]::NewLine + $owner)
    Assert-NoLibvirtRuntimeOwner
}

function Stage-RollbackState {
    param(
        [Parameter(Mandatory)][pscustomobject]$Checkpoint,
        [Parameter(Mandatory)][string]$RollbackWslPath
    )

    $script = @'
set -eu
checkpoint='__CHECKPOINT__'
rollback='__ROLLBACK__'
source_nvram='__SOURCE_NVRAM__'
active_nvram='__ACTIVE_NVRAM__'
source_tpm='__SOURCE_TPM__'
test -f "$checkpoint/OVMF_VARS_4M.ms.fd"
test -d "$checkpoint/tpm"
test -f "$source_nvram"
test -d "$source_tpm"
test ! -L "$source_nvram"
test ! -L "$source_tpm"
test ! -e "$rollback/stage"
mkdir -p "$rollback/stage/tpm"
cp --reflink=auto "$checkpoint/OVMF_VARS_4M.ms.fd" "$rollback/stage/OVMF_VARS_4M.ms.fd"
cp -a "$checkpoint/tpm/." "$rollback/stage/tpm/"
cmp -s "$checkpoint/OVMF_VARS_4M.ms.fd" "$rollback/stage/OVMF_VARS_4M.ms.fd"
diff -qr "$checkpoint/tpm" "$rollback/stage/tpm"
if test -e "$active_nvram"; then
  test ! -L "$active_nvram"
fi
'@
    $script = $script.Replace('__CHECKPOINT__', $Checkpoint.WslPath).Replace('__ROLLBACK__', $RollbackWslPath).Replace('__SOURCE_NVRAM__', $WslSourceNvramPath).Replace('__ACTIVE_NVRAM__', $WslActiveNvramPath).Replace('__SOURCE_TPM__', $WslLegacyTpmPath)
    $result = Invoke-WslScript -Script $script -AllowFailure
    if ($result.ExitCode -ne 0) { throw "ROLLBACK_STATE_STAGE_FAILED: $($result.Output)" }
    Write-Evidence -Name 'rollback-state-stage.txt' -Text ($result.Output + [Environment]::NewLine + "checkpoint=$($Checkpoint.Name)")
}

function Restore-StagedState {
    param(
        [Parameter(Mandatory)][pscustomobject]$Checkpoint,
        [Parameter(Mandatory)][string]$RollbackWslPath
    )

    Assert-NoLibvirtRuntimeOwner
    $script = @'
set -eu
checkpoint='__CHECKPOINT__'
rollback='__ROLLBACK__'
source_nvram='__SOURCE_NVRAM__'
active_nvram='__ACTIVE_NVRAM__'
source_tpm='__SOURCE_TPM__'
stage="$rollback/stage"
test -f "$stage/OVMF_VARS_4M.ms.fd"
test -d "$stage/tpm"
test ! -e "$rollback/previous-source-tpm"
test ! -e "$rollback/previous-source-nvram.fd"
cp --reflink=auto "$source_nvram" "$rollback/previous-source-nvram.fd"
if test -e "$active_nvram"; then cp --reflink=auto "$active_nvram" "$rollback/previous-active-nvram.fd"; fi
mv "$source_tpm" "$rollback/previous-source-tpm"
mkdir -p "$source_tpm"
cp -a "$stage/tpm/." "$source_tpm/"
cp --reflink=auto "$stage/OVMF_VARS_4M.ms.fd" "$source_nvram"
cmp -s "$checkpoint/OVMF_VARS_4M.ms.fd" "$source_nvram"
diff -qr "$checkpoint/tpm" "$source_tpm"
test ! -S '__TPM_SOCKET__'
'@
    $script = $script.Replace('__CHECKPOINT__', $Checkpoint.WslPath).Replace('__ROLLBACK__', $RollbackWslPath).Replace('__SOURCE_NVRAM__', $WslSourceNvramPath).Replace('__ACTIVE_NVRAM__', $WslActiveNvramPath).Replace('__SOURCE_TPM__', $WslLegacyTpmPath).Replace('__TPM_SOCKET__', $WslTpmSocketPath)
    $result = Invoke-WslScript -Script $script -AllowFailure
    if ($result.ExitCode -ne 0) { throw "ROLLBACK_STATE_RESTORE_FAILED: $($result.Output)" }
    Write-Evidence -Name 'rollback-state-restore.txt' -Text ($result.Output + [Environment]::NewLine + "checkpoint=$($Checkpoint.Name)")
}

function Assert-RollbackDisk {
    $result = Invoke-WslOutput -Command "qemu-img check --read-only -f qcow2 '$WslDiskPath'" -AllowFailure
    Write-Evidence -Name 'rollback-qemu-img-check.txt' -Text $result.Output
    if ($result.ExitCode -ne 0 -or $result.Output -match '(?i)corrupt|errors were found') {
        throw "ROLLBACK_QCOW2_CHECK_FAILED: $($result.Output)"
    }
}

function Invoke-Rollback {
    $rollbackId = 'rollback-{0}' -f (Get-Date -Format 'yyyyMMdd-HHmmss')
    $rollbackEvidenceRoot = Join-Path $EvidenceRoot $rollbackId
    $rollbackWslPath = "$WslRepoRoot/runtime/vm-windows11/libvirt-migration/$rollbackId"
    New-Item -ItemType Directory -Force -Path $rollbackEvidenceRoot | Out-Null
    $script:EvidenceOverride = $rollbackEvidenceRoot
    $checkpoint = $null
    try {
        if ((Test-Path -LiteralPath $MarkerPath -PathType Leaf) -and -not $AllowRollbackAfterCutover) {
            throw 'ROLLBACK_AFTER_CUTOVER_REQUIRES_ALLOWROLLBACKAFTERCUTOVER'
        }
        $checkpoint = Get-RollbackCheckpoint
        Write-Evidence -Name 'rollback-request.txt' -Text "rollback_id=$rollbackId`ncheckpoint=$($checkpoint.Name)`nallow_after_cutover=$AllowRollbackAfterCutover"
        Assert-CheckpointState -Checkpoint $checkpoint
        Assert-NoLegacyOwner
        Stop-LibvirtRuntime
        Stage-RollbackState -Checkpoint $checkpoint -RollbackWslPath $rollbackWslPath
        $domainXml = Invoke-WslOutput -Command "virsh -c qemu:///system dumpxml '$DomainName'" -AllowFailure
        Write-Evidence -Name 'rollback-domain.xml' -Text $domainXml.Output
        Assert-RollbackDisk
        $undefine = Invoke-WslOutput -Command "virsh -c qemu:///system undefine '$DomainName' --keep-nvram" -AllowFailure
        Write-Evidence -Name 'rollback-undefine.txt' -Text $undefine.Output
        if ($undefine.ExitCode -ne 0 -and $undefine.Output -notmatch '(?i)failed to get domain|domain not found|no domain') {
            throw "ROLLBACK_DOMAIN_UNDEFINE_FAILED: $($undefine.Output)"
        }
        Assert-NoLibvirtRuntimeOwner
        Restore-StagedState -Checkpoint $checkpoint -RollbackWslPath $rollbackWslPath
        $target = Restore-GuacamoleFromCheckpoint -Checkpoint $checkpoint
        $legacyResult = @(& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $LegacyScriptPath start -AllowLegacyQemuOwner 2>&1)
        $legacyText = ($legacyResult | ForEach-Object { $_.ToString() }) -join [Environment]::NewLine
        Write-Evidence -Name 'rollback-legacy-start.txt' -Text $legacyText
        if ($LASTEXITCODE -ne 0) { throw "ROLLBACK_LEGACY_START_FAILED: $legacyText" }
        $rdp = Invoke-RdpTest -HostName '127.0.0.1' -Port 3391
        Write-Evidence -Name 'rollback-legacy-rdp.txt' -Text $rdp.Output
        if ($rdp.Output -notmatch 'WIN11_RDP_AUTH_OK') { throw 'ROLLBACK_LEGACY_RDP_AUTH_FAILED' }
        Write-Evidence -Name 'rollback-legacy-rdp-token.txt' -Text 'LEGACY_ROLLBACK_RDP_OK'
        if (Test-Path -LiteralPath $MarkerPath -PathType Leaf) { Remove-Item -LiteralPath $MarkerPath -Force }
        $state = Read-State
        $state['phase'] = 'rolled-back'
        $state['rollback_utc'] = [DateTime]::UtcNow.ToString('o')
        $state['rollback_checkpoint'] = $checkpoint.Name
        $state['rollback_evidence'] = $rollbackEvidenceRoot
        $state['guacamole_target'] = "$($target.Hostname):$($target.Port)"
        Write-State -Values $state
        Write-Evidence -Name 'rollback-success.txt' -Text "rollback_id=$rollbackId`ncheckpoint=$($checkpoint.Name)`nlegacy_rdp=WIN11_RDP_AUTH_OK`nno_disk_or_tpm_deleted=true"
        Write-Host 'GUAC_CONNECTION_ID_PRESERVED'
        Write-Host 'GUAC_PERMISSIONS_PRESERVED'
        Write-Host 'LEGACY_ROLLBACK_RDP_OK'
        Write-Host 'LIBVIRT_ROLLBACK_OK'
    } catch {
        $checkpointName = if ($null -ne $checkpoint) { $checkpoint.Name } else { $CheckpointName }
        Write-Evidence -Name 'rollback-failure.txt' -Text "rollback_id=$rollbackId`ncheckpoint=$checkpointName`nerror=$($_.Exception.Message)`nlegacy_start_not_verified=true"
        throw "LIBVIRT_ROLLBACK_FAILED: $($_.Exception.Message)"
    } finally {
        $script:EvidenceOverride = $null
    }
}

function Show-Status {
    $state = Read-State
    Write-Host "phase=$([string]$state['phase'])"
    Write-Host "marker=$(Test-Path -LiteralPath $MarkerPath -PathType Leaf)"
    Write-Host (Get-OwnerEvidence)
    $domain = Invoke-WslOutput -Command "virsh -c qemu:///system domstate '$DomainName'" -AllowFailure
    Write-Host "domain=$($domain.Output)"
    $tpm = Invoke-WslOutput -Command "test -S '$WslTpmSocketPath'" -AllowFailure
    Write-Host "tpm_socket=$($tpm.ExitCode -eq 0)"
}

switch ($Action) {
    'preflight' { Invoke-Preflight }
    'backup' { Invoke-Backup }
    'define' { Invoke-Define }
    'smoke-test' { Invoke-SmokeTest }
    'cutover' { Invoke-Cutover }
    'rollback' { Invoke-Rollback }
    'status' { Show-Status }
}
