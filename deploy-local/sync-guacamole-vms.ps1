[CmdletBinding()]
param(
    [switch]$WhatIf
)

$ErrorActionPreference = 'Stop'
$WslDistro = 'Ubuntu-24.04'
$WslHelper = '/usr/local/libexec/guacamole-workspace-helper'
$WslExecutable = if ($env:GUACAMOLE_WSL_EXECUTABLE) { $env:GUACAMOLE_WSL_EXECUTABLE } else { 'wsl.exe' }

$helperArguments = @(
    '-d', $WslDistro,
    '-u', 'root',
    '--',
    $WslHelper,
    'sync',
    '--all'
)
if ($WhatIf) {
    $helperArguments += '--what-if'
}

function Write-ProtocolError {
    param(
        [Parameter(Mandatory)][string]$Code,
        [Parameter(Mandatory)][string]$Stage,
        [Parameter(Mandatory)][string]$Message
    )

    [ordered]@{
        ok = $false
        code = $Code
        stage = $Stage
        message = $Message
    } | ConvertTo-Json -Compress
}

try {
    $helperOutput = @(& $WslExecutable @helperArguments 2>&1)
    $helperExitCode = $LASTEXITCODE
}
catch {
    Write-ProtocolError -Code 'LAUNCH_FAILED' -Stage 'wrapper' -Message 'Could not start the workspace helper.'
    exit 2
}

$jsonResults = @(
    $helperOutput |
        ForEach-Object { $_.ToString().Trim() } |
        Where-Object { $_ } |
        ForEach-Object {
            try { $_ | ConvertFrom-Json } catch { $null }
        } |
        Where-Object { $null -ne $_ -and $_.PSObject.Properties.Name -contains 'ok' }
)
$result = $jsonResults | Select-Object -Last 1
if ($null -eq $result) {
    Write-ProtocolError -Code 'HELPER_OUTPUT_INVALID' -Stage 'wrapper' -Message 'The workspace helper returned no structured JSON result.'
    if ($helperExitCode -eq 0) {
        exit 2
    }
    exit $helperExitCode
}

if ($helperExitCode -ne 0 -or -not [bool]$result.ok) {
    $result | ConvertTo-Json -Compress
    if ($helperExitCode -eq 0) {
        exit 2
    }
    exit $helperExitCode
}

foreach ($line in $helperOutput) {
    Write-Output $line
}

exit 0
