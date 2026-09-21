[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$MigrationScript = Join-Path $RepoRoot 'deploy-local\migrate-windows11-to-libvirt.ps1'
$LegacyScript = Join-Path $RepoRoot 'deploy-local\vm-windows11\windows11.ps1'
$GeneratedXml = Join-Path $RepoRoot 'deploy-local\libvirt\windows11.generated.xml'
$StatePath = Join-Path $RepoRoot 'runtime\vm-windows11\libvirt-migration-state.json'
$MarkerPath = Join-Path $RepoRoot 'runtime\vm-windows11\libvirt-cutover.marker'

foreach ($path in @($MigrationScript, $LegacyScript, $GeneratedXml, $StatePath)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "TASK5_REQUIRED_FILE_MISSING: $path" }
}

foreach ($path in @($MigrationScript, $LegacyScript)) {
    $errors = $null
    [System.Management.Automation.Language.Parser]::ParseFile($path, [ref]$null, [ref]$errors) | Out-Null
    if ($errors) { throw "TASK5_PARSE_FAILED: $path" }
}

$migrationText = Get-Content -LiteralPath $MigrationScript -Raw
$legacyText = Get-Content -LiteralPath $LegacyScript -Raw
$xmlText = Get-Content -LiteralPath $GeneratedXml -Raw
$state = Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json

if ($migrationText -match '(?i)swtpm_setup|docker compose down -v|rm -rf .*(qcow2|tpm)') { throw 'TASK5_FORBIDDEN_DESTRUCTIVE_ACTION_FOUND' }
if ($migrationText -notmatch "ValidateSet\('preflight', 'backup', 'define', 'smoke-test', 'cutover', 'rollback', 'status'\)") { throw 'TASK5_ACTION_CONTRACT_MISSING' }
if ($migrationText -notmatch 'GUACAMOLE_SESSION_EVIDENCE_REQUIRED') { throw 'TASK5_GUACAMOLE_GATE_MISSING' }
if ($legacyText -notmatch 'LEGACY_QEMU_OWNER_BLOCKED_AFTER_LIBVIRT_CUTOVER') { throw 'TASK5_LEGACY_GUARD_MISSING' }
if ($xmlText -match '(?i)iso|password|trycloudflare|3391|__W11_') { throw 'TASK5_XML_CONTRACT_FAILED' }
foreach ($required in @('/var/lib/guacamole-vm-windows11/windows11.qcow2', '/var/lib/guacamole-vm-windows11/libvirt/OVMF_VARS_4M.ms.fd', '/run/guacamole-vm-windows11/swtpm.sock', "mac address='52:54:00:11:11:01'", "network='guac-nat'")) {
    if ($xmlText -notmatch [regex]::Escape($required)) { throw "TASK5_XML_REQUIRED_FIELD_MISSING: $required" }
}
if ([string]$state.phase -notin @('define', 'smoke-test', 'guacamole-updated', 'cutover')) { throw "TASK5_STATE_PHASE_UNEXPECTED: $($state.phase)" }
if ((Test-Path -LiteralPath $MarkerPath -PathType Leaf) -and [string]$state.phase -ne 'cutover') { throw 'TASK5_MARKER_STATE_MISMATCH' }

Write-Host 'TASK5_STATIC_VALIDATION_OK'
