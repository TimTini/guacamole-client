[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$sourcePath = (Resolve-Path (Join-Path $PSScriptRoot '..\libvirt.ps1')).Path
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($sourcePath, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count -gt 0) { throw $parseErrors[0].Message }
$function = $ast.Find({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq 'Start-LibvirtTpmOwner'
}, $true)
if ($null -eq $function) { throw 'Start-LibvirtTpmOwner was not found.' }
. ([scriptblock]::Create($function.Extent.Text))

$CutoverMarkerPath = 'test-cutover.marker'
$TpmUnitName = 'guacamole-vm-windows11-libvirt-tpm.service'
$TpmSocketPath = '/run/guacamole-vm-windows11/swtpm.sock'
$TimeoutSeconds = 0
$script:markerExists = $false
$script:ownerReady = $false
$script:systemctlStarts = 0
function Test-Path { param($LiteralPath, $PathType) return $script:markerExists }
function Invoke-LegacyOwnerGuard { }
function Invoke-WslCommand { param($Command) $script:systemctlStarts++; return $null }
function Get-TpmOwnerEvidence {
    return [pscustomobject]@{
        DedicatedOwnerReady = $script:ownerReady
        OwnerState = if ($script:ownerReady) { 'DEDICATED_TPM_OWNER' } else { 'TPM_OWNER_MISMATCH' }
    }
}

try {
    Start-LibvirtTpmOwner
    throw 'Missing cutover marker did not block TPM start.'
} catch {
    if ($_.Exception.Message -notmatch 'LIBVIRT_CUTOVER_MARKER_REQUIRED') { throw }
}
if ($script:systemctlStarts -ne 0) { throw 'TPM service was started before cutover.' }

$script:markerExists = $true
try {
    Start-LibvirtTpmOwner
    throw 'Mismatched TPM owner was accepted.'
} catch {
    if ($_.Exception.Message -notmatch 'LIBVIRT_TPM_OWNER_NOT_READY') { throw }
}
if ($script:systemctlStarts -ne 1) { throw 'Expected one TPM service start attempt.' }

$script:ownerReady = $true
Start-LibvirtTpmOwner
if ($script:systemctlStarts -ne 2) { throw 'TPM service start was not invoked.' }
Write-Host 'PREPARE_TPM_TEST_PASS'
