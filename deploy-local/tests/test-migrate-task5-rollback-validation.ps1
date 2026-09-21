[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$MigrationScript = Join-Path $RepoRoot 'deploy-local\migrate-windows11-to-libvirt.ps1'
$source = Get-Content -LiteralPath $MigrationScript -Raw

foreach ($required in @(
        'AllowRollbackAfterCutover',
        'Get-RollbackCheckpoint',
        'Assert-CheckpointState',
        'Stop-LibvirtRuntime',
        'Restore-StagedState',
        'Restore-GuacamoleFromCheckpoint',
        'ROLLBACK_AFTER_CUTOVER_REQUIRES_ALLOWROLLBACKAFTERCUTOVER',
        'sha256sum -c state-sha256sums.txt',
        'virsh -c qemu:///system destroy',
        'systemctl stop ''$TpmUnitName''',
        'virsh -c qemu:///system undefine ''$DomainName'' --keep-nvram',
        'qemu-img check --read-only -f qcow2',
        'rollback-domain.xml',
        'start -AllowLegacyQemuOwner',
        "Invoke-RdpTest -HostName '127.0.0.1' -Port 3391",
        'LEGACY_ROLLBACK_RDP_OK',
        'rollback-failure.txt',
        'no_disk_or_tpm_deleted=true')) {
    if ($source.IndexOf($required, [StringComparison]::Ordinal) -lt 0) {
        throw "TASK5_ROLLBACK_CONTRACT_MISSING: $required"
    }
}

if ($source -match '(?i)rm\s+-rf.*(qcow2|tpm)|swtpm_setup|docker compose down -v') {
    throw 'TASK5_ROLLBACK_DESTRUCTIVE_OPERATION_FOUND'
}
if ($source -notmatch 'Test-Path -LiteralPath \$MarkerPath.*AllowRollbackAfterCutover') {
    throw 'TASK5_ROLLBACK_MARKER_GATE_MISSING'
}
if ($source -notmatch 'beforePermissions.*afterPermissions') {
    throw 'TASK5_ROLLBACK_PERMISSION_PRESERVATION_MISSING'
}

Write-Host 'TASK5_ROLLBACK_STATIC_VALIDATION_OK'
