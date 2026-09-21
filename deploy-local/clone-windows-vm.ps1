[CmdletBinding(DefaultParameterSetName = 'User')]
param(
    [Parameter(Mandatory)]
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$')]
    [string]$Name,

    [Parameter(Mandatory, ParameterSetName = 'User')]
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}$')]
    [string]$AssignUser,

    [Parameter(Mandatory, ParameterSetName = 'Group')]
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}$')]
    [string]$AssignGroup,

    [Alias('Template')]
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$')]
    [string]$TemplateVersion = 'windows11-v1',

    [ValidateRange(1, 1048576)]
    [int]$MemoryMiB = 4096,

    [ValidateRange(1, 256)]
    [int]$Vcpus = 2,

    [ValidateRange(0, 1440)]
    [double]$WaitRdpMinutes = 20,

    [switch]$WhatIf
)

$ErrorActionPreference = 'Stop'
$WslDistro = 'Ubuntu-24.04'
$WslHelper = '/usr/local/libexec/guacamole-workspace-helper'
$WslExecutable = if ($env:GUACAMOLE_WSL_EXECUTABLE) { $env:GUACAMOLE_WSL_EXECUTABLE } else { 'wsl.exe' }

function Write-JsonResult {
    param([Parameter(Mandatory)][object]$Payload)
    $Payload | ConvertTo-Json -Compress
}

if ($TemplateVersion -ne 'windows11-v1') {
    Write-JsonResult ([ordered]@{
        ok = $false
        code = 'VERSION_INVALID'
        stage = 'validate'
        message = 'Template must be windows11-v1.'
    })
    exit 2
}

$helperArguments = @(
    '-d', $WslDistro,
    '-u', 'root',
    '--',
    $WslHelper,
    'clone',
    '--name', $Name,
    '--template', $TemplateVersion,
    '--memory-mib', $MemoryMiB,
    '--vcpus', $Vcpus,
    '--wait-rdp-minutes', $WaitRdpMinutes
)
if ($PSCmdlet.ParameterSetName -eq 'Group') {
    $helperArguments += @('--assign-group', $AssignGroup)
}
else {
    $helperArguments += @('--assign-user', $AssignUser)
}
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
