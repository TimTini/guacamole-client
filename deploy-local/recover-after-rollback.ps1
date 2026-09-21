[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('start', 'status', 'stop')]
    [string]$Action = 'start',

    [int]$SystemdTimeoutSeconds = 120,

    [int]$DockerTimeoutSeconds = 120
)

$ErrorActionPreference = 'Stop'
$StartScript = Join-Path $PSScriptRoot 'start-local.ps1'
$TunnelScript = Join-Path $PSScriptRoot 'start-quick-tunnel.ps1'
$LibvirtScript = Join-Path $PSScriptRoot 'libvirt.ps1'

function Invoke-DeploymentScript {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$ScriptAction
    )

    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Path `
        -Action $ScriptAction
    if ($LASTEXITCODE -ne 0) {
        throw "Deployment script failed with exit code ${LASTEXITCODE}: $Path $ScriptAction"
    }
}

function Invoke-LocalDeployment {
    param(
        [Parameter(Mandatory)][string]$ScriptAction
    )

    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $StartScript `
        -Action $ScriptAction `
        -SystemdTimeoutSeconds $SystemdTimeoutSeconds `
        -DockerTimeoutSeconds $DockerTimeoutSeconds
    if ($LASTEXITCODE -ne 0) {
        throw "Local recovery script failed with exit code $LASTEXITCODE."
    }
}

function Invoke-LibvirtDeployment {
    param(
        [Parameter(Mandatory)][ValidateSet('network', 'storage', 'connect-guacamole', 'start', 'status', 'stop')][string]$ScriptAction
    )

    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $LibvirtScript `
        -Action $ScriptAction `
        -TimeoutSeconds $SystemdTimeoutSeconds
    if ($LASTEXITCODE -ne 0) {
        throw "Libvirt recovery script failed with exit code ${LASTEXITCODE}: $ScriptAction"
    }
}

if ($Action -eq 'stop') {
    Invoke-DeploymentScript -Path $TunnelScript -ScriptAction 'stop'
    Invoke-LibvirtDeployment -ScriptAction 'stop'
    Invoke-LocalDeployment -ScriptAction 'stop'
    return
}

if ($Action -eq 'status') {
    Invoke-LocalDeployment -ScriptAction 'status'
    Invoke-LibvirtDeployment -ScriptAction 'status'
    Invoke-DeploymentScript -Path $TunnelScript -ScriptAction 'status'
    Write-Host 'Cockpit local URL: https://127.0.0.1:9090'
    return
}

Invoke-LocalDeployment -ScriptAction 'start'
Invoke-LibvirtDeployment -ScriptAction 'network'
Invoke-LibvirtDeployment -ScriptAction 'storage'
Invoke-LibvirtDeployment -ScriptAction 'connect-guacamole'
Invoke-LibvirtDeployment -ScriptAction 'start'
Invoke-DeploymentScript -Path $TunnelScript -ScriptAction 'start'
