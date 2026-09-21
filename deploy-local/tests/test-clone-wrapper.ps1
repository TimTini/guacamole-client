[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$wrapper = Join-Path $PSScriptRoot '..\clone-windows-vm.ps1'
$templateWrapper = Join-Path $PSScriptRoot '..\create-windows-template.ps1'
$syncWrapper = Join-Path $PSScriptRoot '..\sync-guacamole-vms.ps1'
$wrapperText = Get-Content -Raw -LiteralPath $wrapper
$templateWrapperText = Get-Content -Raw -LiteralPath $templateWrapper
$syncWrapperText = Get-Content -Raw -LiteralPath $syncWrapper
foreach ($runtimeWrapper in @($wrapperText, $templateWrapperText, $syncWrapperText)) {
    if ($runtimeWrapper -notmatch '/usr/local/libexec/guacamole-workspace-helper') {
        throw 'runtime wrapper does not target installed helper'
    }
    if ($runtimeWrapper -match '/mnt/h/RemoteWorkspaces/guacamole-client/deploy-local/workspace-helper\.py') {
        throw 'runtime wrapper still targets source helper'
    }
}
$temp = Join-Path ([System.IO.Path]::GetTempPath()) ('guac-clone-wrapper-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $temp | Out-Null
$fake = Join-Path $temp 'wsl.cmd'
$log = Join-Path $temp 'args.txt'
$result = Join-Path $temp 'result.json'

try {
    @('@echo off', 'echo %* > "%FAKE_WSL_LOG%"', 'echo {"ok":true,"clone":{"name":"vm-new"}}', 'exit /b 0') |
        Set-Content -LiteralPath $fake -Encoding ASCII
    $env:GUACAMOLE_WSL_EXECUTABLE = $fake
    $env:FAKE_WSL_LOG = $log

    $output = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $wrapper `
        -Name vm-new -AssignGroup developers -TemplateVersion windows11-v1 -MemoryMiB 8192 -Vcpus 4 -WaitRdpMinutes 3 -WhatIf 2>&1
    if ($LASTEXITCODE -ne 0) { throw "group WhatIf wrapper failed: $LASTEXITCODE" }
    $forwarded = Get-Content -Raw -LiteralPath $log
    foreach ($expected in @('--assign-group developers', '--template windows11-v1', '--memory-mib 8192', '--vcpus 4', '--wait-rdp-minutes 3', '--what-if')) {
        if ($forwarded -notmatch [regex]::Escape($expected)) { throw "missing forwarded argument: $expected" }
    }
    if (($output -join "`n") -match 'stage.*preflight') { throw 'wrapper emitted a misleading preflight success record' }

    @('@echo off', 'echo {"ok":false,"code":"RDP_NOT_READY","stage":"wait-rdp","message":"not ready"}', 'exit /b 7') |
        Set-Content -LiteralPath $fake -Encoding ASCII
    $oldErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $wrapper -Name vm-new -AssignUser demo 2>&1 | Set-Content -LiteralPath $result
    if ($LASTEXITCODE -ne 7) { throw "helper exit code was not propagated: $LASTEXITCODE" }

    @('@echo off', 'exit /b 0') | Set-Content -LiteralPath $fake -Encoding ASCII
    $emptyOutput = @(& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $wrapper -Name vm-new -AssignUser demo 2>&1)
    if ($LASTEXITCODE -ne 2) { throw "empty helper output was accepted: $LASTEXITCODE" }
    if (($emptyOutput -join "`n") -match '"ok"\s*:\s*true') { throw 'empty helper output reported success' }

    @('@echo off', 'echo definitely-not-json', 'exit /b 0') | Set-Content -LiteralPath $fake -Encoding ASCII
    $malformedOutput = @(& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $wrapper -Name vm-new -AssignUser demo 2>&1)
    if ($LASTEXITCODE -ne 2) { throw "malformed helper output was accepted: $LASTEXITCODE" }
    if (($malformedOutput -join "`n") -match '"ok"\s*:\s*true') { throw 'malformed helper output reported success' }

    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $wrapper -Name vm-new -AssignUser demo -AssignGroup developers 2>&1 | Set-Content -LiteralPath $result
    if ($LASTEXITCODE -eq 0) { throw 'AssignUser and AssignGroup were accepted together' }
    $ErrorActionPreference = $oldErrorActionPreference

    'CLONE_WRAPPER_TEST_PASS'
}
finally {
    Remove-Item Env:GUACAMOLE_WSL_EXECUTABLE -ErrorAction SilentlyContinue
    Remove-Item Env:FAKE_WSL_LOG -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $temp -Recurse -Force -ErrorAction SilentlyContinue
}
