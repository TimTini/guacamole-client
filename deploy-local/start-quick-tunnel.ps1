[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('start', 'status', 'stop')]
    [string]$Action = 'start'
)

$ErrorActionPreference = 'Stop'
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$RuntimeRoot = Join-Path $RepoRoot 'runtime\cloudflared'
$CloudflaredPath = Join-Path $RuntimeRoot 'cloudflared.exe'
$PidPath = Join-Path $RuntimeRoot 'quick-tunnel.pid'
$StdoutLog = Join-Path $RuntimeRoot 'quick-tunnel.stdout.log'
$StderrLog = Join-Path $RuntimeRoot 'quick-tunnel.stderr.log'
$LocalUrl = 'http://127.0.0.1:8080'

function Get-TrackedProcess {
    if (-not (Test-Path -LiteralPath $PidPath -PathType Leaf)) {
        return $null
    }

    $pidValue = 0
    [void][int]::TryParse((Get-Content -LiteralPath $PidPath -Raw), [ref]$pidValue)
    if ($pidValue -le 0) {
        return $null
    }

    $process = Get-Process -Id $pidValue -ErrorAction SilentlyContinue
    if ($null -eq $process -or $process.ProcessName -notmatch '^cloudflared$') {
        return $null
    }

    try {
        $commandLine = [string](Get-CimInstance Win32_Process -Filter "ProcessId = $pidValue").CommandLine
    } catch {
        $commandLine = ''
    }
    if ($commandLine -and $commandLine -notmatch [regex]::Escape($CloudflaredPath)) {
        return $null
    }
    return $process
}

function Get-PublicUrl {
    foreach ($path in @($StderrLog, $StdoutLog)) {
        if (Test-Path -LiteralPath $path -PathType Leaf) {
            $content = Get-Content -LiteralPath $path -Raw -ErrorAction SilentlyContinue
            if ([string]::IsNullOrEmpty($content)) {
                continue
            }
            $match = [regex]::Match($content, 'https://[a-z0-9-]+\.trycloudflare\.com', [System.Text.RegularExpressions.RegexOptions]::IgnoreCase)
            if ($match.Success) {
                return $match.Value
            }
        }
    }
    return $null
}

function Show-Status {
    $process = Get-TrackedProcess
    if ($null -eq $process) {
        Write-Host 'Quick Tunnel: stopped'
        return
    }

    $url = Get-PublicUrl
    Write-Host "Quick Tunnel: running (PID $($process.Id))"
    if ($url) {
        Write-Host "Public URL: $url"
    } else {
        Write-Host "Public URL: waiting for cloudflared; inspect $StderrLog"
    }
}

function Start-Tunnel {
    if (-not (Test-Path -LiteralPath $CloudflaredPath -PathType Leaf)) {
        throw "Repo-local cloudflared was not found: $CloudflaredPath"
    }

    New-Item -ItemType Directory -Force -Path $RuntimeRoot | Out-Null
    $existing = Get-TrackedProcess
    if ($null -ne $existing) {
        Show-Status
        return
    }

    Remove-Item -LiteralPath $PidPath -Force -ErrorAction SilentlyContinue
    $arguments = @('tunnel', '--no-autoupdate', '--url', $LocalUrl)
    $process = Start-Process -FilePath $CloudflaredPath -ArgumentList $arguments -WindowStyle Hidden `
        -RedirectStandardOutput $StdoutLog -RedirectStandardError $StderrLog -PassThru
    $process.Id | Set-Content -LiteralPath $PidPath -NoNewline -Encoding ascii

    $deadline = [DateTime]::UtcNow.AddSeconds(15)
    do {
        Start-Sleep -Milliseconds 500
        if ($process.HasExited) {
            throw "cloudflared exited with code $($process.ExitCode); inspect $StderrLog"
        }
        $url = Get-PublicUrl
        if ($url) {
            Write-Host "Public URL: $url"
            return
        }
    } while ([DateTime]::UtcNow -lt $deadline)

    Write-Host "Quick Tunnel started (PID $($process.Id)); URL is still pending in $StderrLog"
}

function Stop-Tunnel {
    $process = Get-TrackedProcess
    if ($null -ne $process) {
        Stop-Process -Id $process.Id -Force
        Write-Host 'Quick Tunnel stopped.'
    } else {
        Write-Host 'Quick Tunnel is not running.'
    }
    Remove-Item -LiteralPath $PidPath -Force -ErrorAction SilentlyContinue
}

switch ($Action) {
    'start'  { Start-Tunnel }
    'status' { Show-Status }
    'stop'   { Stop-Tunnel }
}
