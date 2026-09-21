[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('prepare', 'start', 'status', 'stop')]
    [string]$Action = 'prepare'
)

$ErrorActionPreference = 'Stop'
$ScriptRoot = $PSScriptRoot
$RepoRoot = (Resolve-Path (Join-Path $ScriptRoot '..\..')).Path
$RuntimeRoot = Join-Path $RepoRoot 'runtime\vm-demo'
$SourceImage = Join-Path $RuntimeRoot 'ubuntu-24.04-cloud.img'
$PasswordPath = Join-Path $RuntimeRoot 'vm-password.txt'
$UserDataTemplate = Join-Path $ScriptRoot 'user-data.template'
$UserDataPath = Join-Path $RuntimeRoot 'user-data'
$MetaDataPath = Join-Path $ScriptRoot 'meta-data'
$SeedIsoPath = Join-Path $RuntimeRoot 'seed.iso'
$VmdkPath = Join-Path $RuntimeRoot 'ubuntu-24.04-guacamole-demo.vmdk'
$VmxTemplate = Join-Path $ScriptRoot 'ubuntu-demo.vmx.template'
$VmxPath = Join-Path $RuntimeRoot 'ubuntu-24.04-guacamole-demo.vmx'
$VmrunPath = 'H:\VMware\VMware Workstation\vmrun.exe'
$ExpectedImageHash = '612b2c0cc1bc413a6cb8c38fd611794caf0f2b436c50013d8b3794db12ad7354'
$WslDistro = 'Ubuntu-24.04'
$WslTemplateRoot = '/mnt/h/RemoteWorkspaces/guacamole-client/deploy-local/vm-demo'
$WslRuntimeRoot = '/mnt/h/RemoteWorkspaces/guacamole-client/runtime/vm-demo'
$WslExpandedDisk = "$WslRuntimeRoot/ubuntu-24.04-guacamole-demo-expanded.qcow2"
$WslTemporaryVmdk = "$WslRuntimeRoot/ubuntu-24.04-guacamole-demo.vmdk.tmp"

function Invoke-WslCommand {
    param([Parameter(Mandatory)][string]$Command)

    & wsl.exe -d $WslDistro -u root -- sh -lc "cd '$WslTemplateRoot' && $Command"
    if ($LASTEXITCODE -ne 0) {
        throw "WSL command failed with exit code $LASTEXITCODE."
    }
}

function Invoke-Vmrun {
    param([Parameter(Mandatory)][string[]]$Arguments)

    & $VmrunPath -T ws @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "vmrun failed with exit code $LASTEXITCODE."
    }
}

function Ensure-RuntimeDirectory {
    New-Item -ItemType Directory -Force -Path $RuntimeRoot | Out-Null
}

function Assert-Inputs {
    if (-not (Test-Path -LiteralPath $SourceImage -PathType Leaf)) {
        throw "Ubuntu cloud image not found: $SourceImage"
    }
    if (-not (Test-Path -LiteralPath $VmrunPath -PathType Leaf)) {
        throw "VMware vmrun not found: $VmrunPath"
    }

    $actualHash = (Get-FileHash -LiteralPath $SourceImage -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualHash -ne $ExpectedImageHash) {
        throw "The cloud image SHA-256 does not match the verified image. Expected $ExpectedImageHash, got $actualHash."
    }
}

function Ensure-Password {
    $existingPassword = Get-Item -LiteralPath $PasswordPath -Force -ErrorAction SilentlyContinue
    if ($null -ne $existingPassword -and $existingPassword.Length -gt 0) {
        return
    }
    if ($null -ne $existingPassword) {
        throw "The VM password file is empty: $PasswordPath"
    }

    Invoke-WslCommand "umask 077; od -An -N24 -tx1 /dev/urandom | tr -d ' \n' > '$WslRuntimeRoot/vm-password.txt'; test -s '$WslRuntimeRoot/vm-password.txt'; chmod 600 '$WslRuntimeRoot/vm-password.txt'"
}

function Ensure-Vmdk {
    if (Test-Path -LiteralPath $VmdkPath -PathType Leaf) {
        $diskInfo = & wsl.exe -d $WslDistro -u root -- sh -lc "qemu-img info --output=json '$WslRuntimeRoot/ubuntu-24.04-guacamole-demo.vmdk' 2>/dev/null"
        if ($LASTEXITCODE -eq 0 -and ($diskInfo -join '') -match '"virtual-size"\s*:\s*17179869184') {
            return
        }
    }

    Write-Host 'Converting the verified Ubuntu cloud image to a writable VMware VMDK...'
    Invoke-WslCommand "set -eu; rm -f '$WslExpandedDisk' '$WslTemporaryVmdk'; qemu-img create -f qcow2 -F qcow2 -b '$WslRuntimeRoot/ubuntu-24.04-cloud.img' '$WslExpandedDisk' 16G; qemu-img convert -f qcow2 -O vmdk -o subformat=monolithicSparse,adapter_type=lsilogic '$WslExpandedDisk' '$WslTemporaryVmdk'; qemu-img info '$WslTemporaryVmdk' >/dev/null; mv -f '$WslTemporaryVmdk' '$WslRuntimeRoot/ubuntu-24.04-guacamole-demo.vmdk'; rm -f '$WslExpandedDisk'"
}

function Render-UserData {
    $hash = (& wsl.exe -d $WslDistro -u root -- sh -lc "openssl passwd -6 -in '$WslRuntimeRoot/vm-password.txt'").Trim()
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($hash)) {
        throw 'Could not create the cloud-init password hash.'
    }

    $template = Get-Content -LiteralPath $UserDataTemplate -Raw
    $rendered = $template.Replace('__VM_PASSWORD_HASH__', $hash)
    if ($rendered.Contains('__VM_PASSWORD_HASH__')) {
        throw 'The cloud-init password marker was not rendered.'
    }

    $utf8 = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($UserDataPath, $rendered, $utf8)
}

function Ensure-SeedIso {
    Write-Host 'Generating the NoCloud seed ISO...'
    Invoke-WslCommand "set -eu; rm -f '$WslRuntimeRoot/seed.iso.tmp'; cloud-localds -f iso '$WslRuntimeRoot/seed.iso.tmp' '$WslRuntimeRoot/user-data' '$WslTemplateRoot/meta-data'; test -s '$WslRuntimeRoot/seed.iso.tmp'; mv '$WslRuntimeRoot/seed.iso.tmp' '$WslRuntimeRoot/seed.iso'"
}

function Ensure-Vmx {
    if (-not (Test-Path -LiteralPath $VmxPath -PathType Leaf)) {
        Copy-Item -LiteralPath $VmxTemplate -Destination $VmxPath
    }
}

function Prepare-Assets {
    Ensure-RuntimeDirectory
    Assert-Inputs
    Ensure-Password
    Ensure-Vmdk
    Render-UserData
    Ensure-SeedIso
    Ensure-Vmx
}

switch ($Action) {
    'prepare' {
        Prepare-Assets
        Write-Host "Prepared the VM files under $RuntimeRoot"
    }
    'start' {
        Prepare-Assets
        Invoke-Vmrun @('start', $VmxPath, 'nogui')
        Write-Host 'Started the Ubuntu VM without opening a VMware window.'
    }
    'status' {
        if (-not (Test-Path -LiteralPath $VmxPath -PathType Leaf)) {
            Write-Host 'The VM has not been prepared yet.'
            return
        }
        & $VmrunPath list
        if ($LASTEXITCODE -ne 0) {
            throw "vmrun list failed with exit code $LASTEXITCODE."
        }
    }
    'stop' {
        if (-not (Test-Path -LiteralPath $VmxPath -PathType Leaf)) {
            Write-Host 'The VM has not been prepared yet; nothing to stop.'
            return
        }
        Invoke-Vmrun @('stop', $VmxPath, 'soft')
        Write-Host 'Requested a clean shutdown of the Ubuntu VM.'
    }
}
