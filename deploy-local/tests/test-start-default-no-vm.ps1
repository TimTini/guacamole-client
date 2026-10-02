[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$recoveryPath = Join-Path $repoRoot 'deploy-local\recover-after-rollback.ps1'
$startLocalPath = Join-Path $repoRoot 'deploy-local\start-local.ps1'

function Assert-Calls {
    param([string]$Name, [string[]]$Actual, [string[]]$Expected)

    if (($Actual -join '|') -ne ($Expected -join '|')) {
        throw "$Name calls were '$($Actual -join ', ')'; expected '$($Expected -join ', ')'."
    }
}

# Intercept child PowerShell calls so the real recovery entrypoint can run
# without starting WSL, Docker, libvirt, any VM, or the tunnel.
$global:guacStartTestCalls = @()
$global:guacCutoverMarkerExists = $true
function powershell.exe {
    $fileIndex = [array]::IndexOf($args, '-File')
    $actionIndex = [array]::IndexOf($args, '-Action')
    if ($fileIndex -lt 0 -or $actionIndex -lt 0) {
        throw "Unexpected child PowerShell command: $($args -join ' ')"
    }
    $global:guacStartTestCalls += '{0}:{1}' -f (Split-Path -Leaf $args[$fileIndex + 1]), $args[$actionIndex + 1]
    $global:LASTEXITCODE = 0
}
function Test-Path {
    param([string]$LiteralPath, [string]$PathType)
    if ($LiteralPath -like '*libvirt-cutover.marker') { return $global:guacCutoverMarkerExists }
    Microsoft.PowerShell.Management\Test-Path @PSBoundParameters
}

try {
    & $recoveryPath -Action start
    Assert-Calls -Name 'Recovery start' -Actual $global:guacStartTestCalls -Expected @(
        'start-local.ps1:start',
        'libvirt.ps1:network',
        'libvirt.ps1:storage',
        'libvirt.ps1:connect-guacamole',
        'libvirt.ps1:prepare-tpm',
        'start-quick-tunnel.ps1:start'
    )

    $global:guacCutoverMarkerExists = $false
    $global:guacStartTestCalls = @()
    & $recoveryPath -Action start
    Assert-Calls -Name 'Pre-cutover recovery start' -Actual $global:guacStartTestCalls -Expected @(
        'start-local.ps1:start',
        'libvirt.ps1:network',
        'libvirt.ps1:storage',
        'libvirt.ps1:connect-guacamole',
        'start-quick-tunnel.ps1:start'
    )
} finally {
    Remove-Item Function:Test-Path -ErrorAction SilentlyContinue
    Remove-Variable -Name guacCutoverMarkerExists -Scope Global -ErrorAction SilentlyContinue
    Remove-Variable -Name guacStartTestCalls -Scope Global -ErrorAction SilentlyContinue
}

# Run the real local-start function with its external dependencies replaced.
# Parsing the function avoids executing start-local.ps1's top-level WSL checks.
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($startLocalPath, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count -gt 0) {
    throw "Could not parse start-local.ps1: $($parseErrors[0].Message)"
}
$startFunction = $ast.Find({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq 'Start-LocalDeployment'
}, $true)
if ($null -eq $startFunction) {
    throw 'Start-LocalDeployment function was not found.'
}
. ([scriptblock]::Create($startFunction.Extent.Text))

$script:localStartCalls = @()
$GuacamoleScript = 'guacamole.ps1'
$QemuScript = 'qemu-demo.ps1'
function Ensure-WslDistro { $script:localStartCalls += 'ensure-wsl' }
function Start-WslKeepAlive { $script:localStartCalls += 'keepalive' }
function Wait-ForSystemd { $script:localStartCalls += 'systemd' }
function Start-Docker { $script:localStartCalls += 'docker' }
function Invoke-LocalScript {
    param([string]$Path, [string]$ScriptAction)
    $script:localStartCalls += '{0}:{1}' -f (Split-Path -Leaf $Path), $ScriptAction
}

Start-LocalDeployment
Assert-Calls -Name 'Local start' -Actual $script:localStartCalls -Expected @(
    'ensure-wsl', 'keepalive', 'systemd', 'docker', 'guacamole.ps1:start'
)

Write-Host 'START_DEFAULT_NO_VM_TEST_PASS'
