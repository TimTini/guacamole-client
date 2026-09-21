[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$LibvirtScript = Join-Path $RepoRoot 'deploy-local\libvirt.ps1'
$Gitignore = Join-Path $RepoRoot '.gitignore'
$source = Get-Content -LiteralPath $LibvirtScript -Raw

if ($source -notmatch '\$stagingName\s*=\s*"\.\$checkpointName\.incomplete"') {
    throw 'TASK4_TRANSACTIONAL_STAGING_MISSING'
}
if ($source.IndexOf('Protect-Checkpoint -Path $stagingPath', [StringComparison]::Ordinal) -lt 0) {
    throw 'TASK4_STAGING_PROTECTION_MISSING'
}
if ($source.IndexOf('Remove-Item -LiteralPath $stagingPath -Recurse -Force', [StringComparison]::Ordinal) -lt 0) {
    throw 'TASK4_STAGING_CLEANUP_MISSING'
}
if ($source.IndexOf('Write-BackupFailureEvidence', [StringComparison]::Ordinal) -lt 0) {
    throw 'TASK4_FAILURE_EVIDENCE_MISSING'
}
if ($source.IndexOf('cd "$checkpoint"', [StringComparison]::Ordinal) -lt 0 -or
    $source.IndexOf('sha256sum -c state-sha256sums.txt', [StringComparison]::Ordinal) -lt 0) {
    throw 'TASK4_RELATIVE_STATE_MANIFEST_VERIFICATION_MISSING'
}
if ($source -match 'Set-Content\s+-LiteralPath\s+\$Path') {
    throw 'TASK4_CHECKPOINT_TEXT_MAY_HAVE_BOM'
}
if ($source -notmatch '\[System\.IO\.File\]::WriteAllText\(\$Path,\s*\$Text,\s*\$utf8NoBom\)') {
    throw 'TASK4_CHECKPOINT_NO_BOM_WRITER_MISSING'
}
if ($source -notmatch 'function Ensure-SwtpmLocalCaOwnership') {
    throw 'TASK4_SWTPM_LOCALCA_OWNERSHIP_REPAIR_MISSING'
}
if ($source -notmatch 'chown -R --no-dereference swtpm:swtpm') {
    throw 'TASK4_SWTPM_LOCALCA_RECURSIVE_OWNERSHIP_REPAIR_MISSING'
}
if ($source -notmatch 'Ensure-SwtpmLocalCaOwnership\s*\r?\n\s*Disable-ConflictingGlobalDnsmasq') {
    throw 'TASK4_SWTPM_LOCALCA_INSTALL_HOOK_MISSING'
}
if ($source -notmatch 'function Configure-Cockpit\s*\{\s*Ensure-SwtpmLocalCaOwnership') {
    throw 'TASK4_SWTPM_LOCALCA_CONFIGURE_HOOK_MISSING'
}

$ignoreLines = @(Get-Content -LiteralPath $Gitignore | Where-Object { $_ -and $_ -notmatch '^\s*#' })
foreach ($required in @(
    'deploy-local/libvirt/exports/',
    'deploy-local/libvirt/*.generated.xml',
    'runtime/',
    'deploy-local/data/',
    'deploy-local/secrets/'
)) {
    if ($ignoreLines -notcontains $required) {
        throw "TASK4_GITIGNORE_REQUIRED_ENTRY_MISSING: $required"
    }
}

Write-Host 'TASK4_TRANSACTIONAL_VALIDATION_OK'
