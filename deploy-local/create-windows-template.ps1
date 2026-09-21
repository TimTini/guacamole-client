[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$Source,

    [Parameter(Mandatory)]
    [string]$Version,

    [switch]$WhatIf
)

$ErrorActionPreference = 'Stop'
$WslDistro = 'Ubuntu-24.04'
$WslHelper = '/usr/local/libexec/guacamole-workspace-helper'
$WslExecutable = if ($env:GUACAMOLE_WSL_EXECUTABLE) { $env:GUACAMOLE_WSL_EXECUTABLE } else { 'wsl.exe' }

function Write-JsonResult {
    param(
        [Parameter(Mandatory)]
        [System.Collections.IDictionary]$Payload
    )

    $Payload | ConvertTo-Json -Compress
}

if ($Source -ne 'windows11') {
    Write-JsonResult ([ordered]@{
        ok = $false
        code = 'SOURCE_INVALID'
        stage = 'validate'
        message = 'Source must be windows11.'
    })
    exit 2
}

if ($Version -ne 'windows11-v1') {
    Write-JsonResult ([ordered]@{
        ok = $false
        code = 'VERSION_INVALID'
        stage = 'validate'
        message = 'Version must be windows11-v1.'
    })
    exit 2
}

Write-JsonResult ([ordered]@{
    ok = $true
    stage = 'preflight'
    whatIf = [bool]$WhatIf
    source = $Source
    version = $Version
    gates = @('source', 'domain', 'tpm', 'guacamole', 'storage')
})

$helperArguments = @(
    '-d', $WslDistro,
    '-u', 'root',
    '--',
    $WslHelper,
    'create-template',
    '--source', $Source,
    '--version', $Version
)
if ($WhatIf) {
    $helperArguments += '--what-if'
}

$helperOutput = @(& $WslExecutable @helperArguments 2>&1)
$helperExitCode = $LASTEXITCODE
foreach ($line in $helperOutput) {
    Write-Output $line
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
if ($helperExitCode -ne 0 -or $null -eq $result -or -not [bool]$result.ok) {
    if ($helperExitCode -eq 0) {
        exit 2
    }
    exit $helperExitCode
}

exit 0
