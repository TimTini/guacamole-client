[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('start', 'status', 'stop')]
    [string]$Action = 'status'
)

$ErrorActionPreference = 'Stop'
$ScriptRoot = $PSScriptRoot
$RepoRoot = (Resolve-Path (Join-Path $ScriptRoot '..\..')).Path
$RuntimeRoot = Join-Path $RepoRoot 'runtime\vm-demo'
$SourceImage = Join-Path $RuntimeRoot 'ubuntu-24.04-cloud.img'
$SeedIso = Join-Path $RuntimeRoot 'seed.iso'
$PasswordPath = Join-Path $RuntimeRoot 'vm-password.txt'
$ConsoleLog = Join-Path $RuntimeRoot 'qemu-console.log'
$ServiceName = 'guacamole-vm-demo.service'
$ExpectedImageHash = '612b2c0cc1bc413a6cb8c38fd611794caf0f2b436c50013d8b3794db12ad7354'
$WslDistro = 'Ubuntu-24.04'
$WslRoot = '/mnt/h/RemoteWorkspaces/guacamole-client'
$WslRuntimeRoot = '/mnt/h/RemoteWorkspaces/guacamole-client/runtime/vm-demo'
$WslVmDataRoot = '/var/lib/guacamole-vm-demo'
$WslOverlay = "$WslVmDataRoot/ubuntu-24.04-guacamole-qemu.qcow2"

function Invoke-WslCommand {
    param([Parameter(Mandatory)][string]$Command)

    & wsl.exe -d $WslDistro -u root -- sh -lc "cd '$WslRoot' && $Command"
    if ($LASTEXITCODE -ne 0) {
        throw "WSL command failed with exit code $LASTEXITCODE."
    }
}

function Assert-Inputs {
    if (-not (Test-Path -LiteralPath $SourceImage -PathType Leaf)) {
        throw "Ubuntu cloud image not found: $SourceImage"
    }
    if (-not (Test-Path -LiteralPath $SeedIso -PathType Leaf)) {
        throw "NoCloud seed ISO not found: $SeedIso. Run vm-demo.ps1 prepare first."
    }
    if (-not (Test-Path -LiteralPath $PasswordPath -PathType Leaf)) {
        throw "VM password file not found: $PasswordPath. Run vm-demo.ps1 prepare first."
    }

    $actualHash = (Get-FileHash -LiteralPath $SourceImage -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualHash -ne $ExpectedImageHash) {
        throw "The cloud image SHA-256 does not match the verified image. Expected $ExpectedImageHash, got $actualHash."
    }

    & wsl.exe -d $WslDistro -u root -- sh -lc 'command -v qemu-system-x86_64 >/dev/null && test -e /dev/kvm'
    if ($LASTEXITCODE -ne 0) {
        throw 'QEMU/KVM is not ready inside Ubuntu-24.04 WSL. Install qemu-system-x86 and ensure /dev/kvm exists.'
    }
}

function Ensure-Overlay {
    $diskInfo = & wsl.exe -d $WslDistro -u root -- sh -lc "qemu-img info --output=json '$WslOverlay' 2>/dev/null"
    if ($LASTEXITCODE -eq 0) {
        if (($diskInfo -join '') -notmatch '"virtual-size"\s*:\s*17179869184') {
            throw "The existing QEMU overlay is invalid or is not 16 GiB: $WslOverlay"
        }
        return
    }

    Write-Host 'Creating the sparse 16 GiB QEMU overlay inside the H:-backed WSL ext4 filesystem...'
    Invoke-WslCommand "mkdir -p '$WslVmDataRoot'; qemu-img create -f qcow2 -F qcow2 -b '$WslRuntimeRoot/ubuntu-24.04-cloud.img' '$WslOverlay' 16G"
}

function Start-Qemu {
    Assert-Inputs

    $active = & wsl.exe -d $WslDistro -u root -- systemctl is-active $ServiceName 2>$null
    if ($LASTEXITCODE -eq 0 -and ($active -join '').Trim() -match '^(active|activating)$') {
        Write-Host 'The QEMU VM is already running or booting.'
        return
    }

    Ensure-Overlay

    $qemuCommand = "systemctl reset-failed '$ServiceName' >/dev/null 2>&1 || true; systemd-run --quiet --unit=guacamole-vm-demo --collect --property=Restart=on-failure --property=RestartSec=3s /usr/bin/qemu-system-x86_64 -name guacamole-vm-demo -enable-kvm -cpu host -m 4096 -smp 2 -drive file='$WslOverlay',if=virtio,format=qcow2 -cdrom '$WslRuntimeRoot/seed.iso' -netdev user,id=n0,hostfwd=tcp:0.0.0.0:3390-:3389 -device virtio-net-pci,netdev=n0 -display none -serial file:'$WslRuntimeRoot/qemu-console.log' -no-reboot"
    Invoke-WslCommand $qemuCommand
    Write-Host 'Started the QEMU VM under systemd without exposing a public WAN port.'
    Write-Host 'The RDP forward is WSL TCP port 3390; use the Docker bridge gateway as the Guacamole host.'
}

function Show-Status {
    $active = & wsl.exe -d $WslDistro -u root -- systemctl is-active $ServiceName 2>$null
    if ($LASTEXITCODE -eq 0 -and ($active -join '').Trim() -match '^(active|activating)$') {
        Write-Host "QEMU VM status: $(($active -join '').Trim())"
        return
    }

    Write-Host 'QEMU VM status: stopped'
}

function Stop-Qemu {
    $active = & wsl.exe -d $WslDistro -u root -- systemctl is-active $ServiceName 2>$null
    if ($LASTEXITCODE -ne 0 -or ($active -join '').Trim() -notmatch '^(active|activating)$') {
        Write-Host 'QEMU VM is not running.'
        return
    }

    Invoke-WslCommand "systemctl stop '$ServiceName'"
    Write-Host 'Stopped the QEMU VM.'
}

switch ($Action) {
    'start'  { Start-Qemu }
    'status' { Show-Status }
    'stop'   { Stop-Qemu }
}
