[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('start', 'status', 'stop')]
    [string]$Action = 'start',

    [int]$SystemdTimeoutSeconds = 120,

    [int]$DockerTimeoutSeconds = 120
)

$ErrorActionPreference = 'Stop'
$ScriptRoot = $PSScriptRoot
$RepoRoot = (Resolve-Path (Join-Path $ScriptRoot '..')).Path
$RuntimeRoot = Join-Path $RepoRoot 'runtime'
$VhdxPath = Join-Path $RuntimeRoot 'ubuntu\ext4.vhdx'
$KeepAlivePidPath = Join-Path $RuntimeRoot 'ubuntu\wsl-keepalive.pid'
$WslDistro = 'Ubuntu-24.04'
$GuacamoleScript = Join-Path $ScriptRoot 'guacamole.ps1'
$QemuScript = Join-Path $ScriptRoot 'vm-demo\qemu-demo.ps1'

function Assert-WslCommand {
    if ($null -eq (Get-Command wsl.exe -ErrorAction SilentlyContinue)) {
        throw 'wsl.exe was not found. Install or repair WSL before starting the local deployment.'
    }
}

function Get-WslDistroNames {
    $output = @(& wsl.exe --list --quiet 2>$null)
    if ($LASTEXITCODE -ne 0) {
        throw 'Could not list WSL distributions.'
    }

    @($output |
        ForEach-Object { $_.ToString() -replace "`0", '' } |
        ForEach-Object { $_.Trim() } |
        Where-Object { $_ })
}

function Test-WslDistroRegistered {
    return @(Get-WslDistroNames) -contains $WslDistro
}

function Ensure-WslDistro {
    if (-not (Test-Path -LiteralPath $VhdxPath -PathType Leaf)) {
        throw "The H:-backed WSL disk was not found: $VhdxPath"
    }

    if (Test-WslDistroRegistered) {
        return
    }

    Write-Host "Registering $WslDistro from the existing H: disk..."
    & wsl.exe --import-in-place $WslDistro $VhdxPath
    if ($LASTEXITCODE -ne 0) {
        throw "WSL import-in-place failed for '$VhdxPath'."
    }
}

function Get-ProcessCommandLine {
    param([Parameter(Mandatory)][int]$ProcessId)

    try {
        $process = Get-CimInstance -ClassName Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction Stop
        return [string]$process.CommandLine
    } catch {
        return ''
    }
}

function Test-KeepAliveProcess {
    param([Parameter(Mandatory)][int]$ProcessId)

    $process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if ($null -eq $process -or $process.ProcessName -notmatch '^wsl$') {
        return $false
    }

    $commandLine = Get-ProcessCommandLine -ProcessId $ProcessId
    return $commandLine -match [regex]::Escape($WslDistro) -and $commandLine -match '(?:--|--exec)\s+sleep\s+(?:infinity|300)(?:\s|$)'
}

function Get-KeepAliveProcessIds {
    $processes = @()
    try {
        $processes = @(Get-CimInstance -ClassName Win32_Process -Filter "Name = 'wsl.exe'" -ErrorAction Stop)
    } catch {
        return @()
    }

    @($processes |
        Where-Object {
            $commandLine = [string]$_.CommandLine
            $commandLine -match [regex]::Escape($WslDistro) -and
                $commandLine -match '(?:--|--exec)\s+sleep\s+(?:infinity|300)(?:\s|$)'
        } |
        ForEach-Object { [int]$_.ProcessId })
}

function Start-WslKeepAlive {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $KeepAlivePidPath) | Out-Null

    $savedPid = 0
    if (Test-Path -LiteralPath $KeepAlivePidPath -PathType Leaf) {
        [void][int]::TryParse((Get-Content -LiteralPath $KeepAlivePidPath -Raw), [ref]$savedPid)
        if ($savedPid -gt 0 -and (Test-KeepAliveProcess -ProcessId $savedPid)) {
            return
        }
        Remove-Item -LiteralPath $KeepAlivePidPath -Force -ErrorAction SilentlyContinue
    }

    $existingPid = @(Get-KeepAliveProcessIds | Select-Object -First 1)
    if ($existingPid.Count -gt 0) {
        $existingPid[0] | Set-Content -LiteralPath $KeepAlivePidPath -NoNewline -Encoding ascii
        return
    }

    $arguments = @('-d', $WslDistro, '-u', 'root', '--', 'sleep', 'infinity')
    $process = Start-Process -FilePath 'wsl.exe' -ArgumentList $arguments -WindowStyle Hidden -PassThru
    Start-Sleep -Milliseconds 750

    if ($process.HasExited -or -not (Test-KeepAliveProcess -ProcessId $process.Id)) {
        if (-not $process.HasExited) {
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        }
        Remove-Item -LiteralPath $KeepAlivePidPath -Force -ErrorAction SilentlyContinue
        throw 'The hidden WSL keep-alive process exited immediately.'
    }

    $process.Id | Set-Content -LiteralPath $KeepAlivePidPath -NoNewline -Encoding ascii
}

function Invoke-WslRoot {
    param([Parameter(Mandatory)][string]$Command)

    & wsl.exe -d $WslDistro -u root -- sh -lc $Command
    if ($LASTEXITCODE -ne 0) {
        throw "WSL command failed with exit code ${LASTEXITCODE}: $Command"
    }
}

function Get-WslRootOutput {
    param([Parameter(Mandatory)][string]$Command)

    $output = @(& wsl.exe -d $WslDistro -u root -- sh -lc $Command 2>$null)
    [pscustomobject]@{
        Output = (($output | ForEach-Object { $_.ToString().Trim() }) -join "`n").Trim()
        ExitCode = $LASTEXITCODE
    }
}

function Wait-ForSystemd {
    $deadline = [DateTime]::UtcNow.AddSeconds($SystemdTimeoutSeconds)
    $lastState = ''

    do {
        $probe = Get-WslRootOutput 'systemctl is-system-running'
        $lastState = $probe.Output
        if ($probe.ExitCode -eq 0 -and $lastState -match '^(running|degraded)$') {
            return
        }

        if ($lastState -match 'not been booted with systemd|command not found|offline') {
            $pidOne = (Get-WslRootOutput "readlink -f /proc/1/exe").Output
            throw "systemd is not running as WSL PID 1 (state '$lastState', PID 1 '$pidOne'). Enable systemd in /etc/wsl.conf and restart this distro."
        }

        Start-Sleep -Seconds 1
    } while ([DateTime]::UtcNow -lt $deadline)

    throw "Timed out waiting for systemd in $WslDistro (last state: '$lastState')."
}

function Start-Docker {
    Invoke-WslRoot 'command -v docker >/dev/null'
    Invoke-WslRoot 'systemctl enable --now docker'

    $deadline = [DateTime]::UtcNow.AddSeconds($DockerTimeoutSeconds)
    do {
        $probe = Get-WslRootOutput 'docker info >/dev/null'
        if ($probe.ExitCode -eq 0) {
            return
        }
        Start-Sleep -Seconds 1
    } while ([DateTime]::UtcNow -lt $deadline)

    throw 'Docker did not become ready before the timeout.'
}

function Invoke-LocalScript {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$ScriptAction
    )

    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Path $ScriptAction
    if ($LASTEXITCODE -ne 0) {
        throw "Local script failed with exit code ${LASTEXITCODE}: $Path $ScriptAction"
    }
}

function Stop-WslKeepAlive {
    $savedPid = 0
    if (Test-Path -LiteralPath $KeepAlivePidPath -PathType Leaf) {
        [void][int]::TryParse((Get-Content -LiteralPath $KeepAlivePidPath -Raw), [ref]$savedPid)
    }

    $processIds = @(Get-KeepAliveProcessIds)
    if ($savedPid -gt 0 -and $processIds -notcontains $savedPid -and (Test-KeepAliveProcess -ProcessId $savedPid)) {
        $processIds += $savedPid
    }
    foreach ($processId in @($processIds | Select-Object -Unique)) {
        Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
    }
    Remove-Item -LiteralPath $KeepAlivePidPath -Force -ErrorAction SilentlyContinue
}

function Start-LocalDeployment {
    Ensure-WslDistro
    Start-WslKeepAlive
    Wait-ForSystemd
    Start-Docker

    Invoke-LocalScript -Path $GuacamoleScript -ScriptAction 'start'
    Invoke-LocalScript -Path $QemuScript -ScriptAction 'start'

    Write-Host 'Local deployment is running at http://127.0.0.1:8080/guacamole/'
}

function Show-LocalStatus {
    if (-not (Test-WslDistroRegistered)) {
        Write-Host "$WslDistro is not registered."
        return
    }

    $systemd = Get-WslRootOutput 'systemctl is-system-running'
    Write-Host "WSL systemd: $($systemd.Output)"
    $docker = Get-WslRootOutput 'docker info >/dev/null'
    Write-Host "Docker: $(if ($docker.ExitCode -eq 0) { 'ready' } else { 'not ready' })"
    Invoke-LocalScript -Path $GuacamoleScript -ScriptAction 'status'
    Invoke-LocalScript -Path $QemuScript -ScriptAction 'status'
    Write-Host 'Local URL: http://127.0.0.1:8080/guacamole/'
}

function Stop-LocalDeployment {
    if (Test-WslDistroRegistered) {
        try { Invoke-LocalScript -Path $QemuScript -ScriptAction 'stop' } catch { Write-Warning $_ }
        try { Invoke-LocalScript -Path $GuacamoleScript -ScriptAction 'stop' } catch { Write-Warning $_ }
    }
    Stop-WslKeepAlive
}

Assert-WslCommand
switch ($Action) {
    'start'  { Start-LocalDeployment }
    'status' { Show-LocalStatus }
    'stop'   { Stop-LocalDeployment }
}
