[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('start', 'status', 'stop', 'finish-install')]
    [string]$Action = 'status',

    [int]$GracefulStopTimeoutSeconds = 120,

    [switch]$AllowLegacyQemuOwner
)

$ErrorActionPreference = 'Stop'
$ScriptRoot = $PSScriptRoot
$RepoRoot = (Resolve-Path (Join-Path $ScriptRoot '..\..')).Path
$RuntimeRoot = Join-Path $RepoRoot 'runtime\vm-windows11'
$IsoPath = Join-Path $RepoRoot 'runtime\iso\Windows11_23H2_UEFI.iso'
$AdminPasswordPath = Join-Path $RepoRoot 'deploy-local\secrets\windows11_password.txt'
$VarsPath = Join-Path $RuntimeRoot 'OVMF_VARS_4M.ms.fd'
$UnattendPath = Join-Path $RuntimeRoot 'Autounattend.xml'
$UnattendIsoPath = Join-Path $RuntimeRoot 'windows11-unattend.iso'
$TpmRoot = Join-Path $RuntimeRoot 'tpm'
$ConsoleLog = Join-Path $RuntimeRoot 'qemu-console.log'
$InstallMarker = Join-Path $RuntimeRoot 'install-finished.marker'
$ServiceName = 'guacamole-vm-windows11.service'
$TpmServiceName = 'guacamole-vm-windows11-tpm.service'
$WslDistro = 'Ubuntu-24.04'
$WslRepoRoot = '/mnt/h/RemoteWorkspaces/guacamole-client'
$WslRuntimeRoot = '/mnt/h/RemoteWorkspaces/guacamole-client/runtime/vm-windows11'
$WslIsoPath = '/mnt/h/RemoteWorkspaces/guacamole-client/runtime/iso/Windows11_23H2_UEFI.iso'
$WslUnattendPath = '/mnt/h/RemoteWorkspaces/guacamole-client/runtime/vm-windows11/Autounattend.xml'
$WslUnattendIsoPath = '/mnt/h/RemoteWorkspaces/guacamole-client/runtime/vm-windows11/windows11-unattend.iso'
$WslUnattendStage = '/mnt/h/RemoteWorkspaces/guacamole-client/runtime/vm-windows11/unattend-iso'
$WslVmDataRoot = '/var/lib/guacamole-vm-windows11'
$WslVarsPath = "$WslRuntimeRoot/OVMF_VARS_4M.ms.fd"
$WslTpmRoot = "$WslRuntimeRoot/tpm"
$WslTpmSocket = '/run/guacamole-vm-windows11/swtpm.sock'
$WslMonitorSocket = '/run/guacamole-vm-windows11/monitor.sock'
$WslTpmLog = "$WslTpmRoot/swtpm.log"
$WslConsoleLog = "$WslRuntimeRoot/qemu-console.log"
$WslInstallMarker = "$WslRuntimeRoot/install-finished.marker"
$WslDiskPath = "$WslVmDataRoot/windows11.qcow2"
$LibvirtCutoverMarker = Join-Path $RepoRoot 'runtime\vm-windows11\libvirt-cutover.marker'

function Assert-LegacyOwnerAllowed {
    if ((Test-Path -LiteralPath $LibvirtCutoverMarker -PathType Leaf) -and -not $AllowLegacyQemuOwner) {
        throw 'LEGACY_QEMU_OWNER_BLOCKED_AFTER_LIBVIRT_CUTOVER'
    }
}

function Invoke-WslCommand {
    param([Parameter(Mandatory)][string]$Command)

    & wsl.exe -d $WslDistro -u root -- sh -lc "cd '$WslRepoRoot' && $Command"
    if ($LASTEXITCODE -ne 0) {
        throw "WSL command failed with exit code $LASTEXITCODE."
    }
}

function Get-WslOutput {
    param([Parameter(Mandatory)][string]$Command)

    $output = @(& wsl.exe -d $WslDistro -u root -- sh -lc "cd '$WslRepoRoot' && $Command" 2>$null)
    [pscustomobject]@{
        Output = (($output | ForEach-Object { $_.ToString().Trim() }) -join "`n").Trim()
        ExitCode = $LASTEXITCODE
    }
}

function Ensure-RuntimeDirectory {
    New-Item -ItemType Directory -Force -Path $RuntimeRoot | Out-Null
    New-Item -ItemType Directory -Force -Path $TpmRoot | Out-Null
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $AdminPasswordPath) | Out-Null
}

function Protect-SecretFile {
    param([Parameter(Mandatory)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return
    }

    $currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    & icacls.exe $Path /reset | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Could not reset the ACL on '$Path'."
    }

    $rules = @(
        ('{0}:(F)' -f $currentUser),
        '*S-1-5-18:(F)',
        '*S-1-5-32-544:(F)'
    )
    & icacls.exe $Path /inheritance:r /grant:r @rules | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Could not restrict the ACL on '$Path'."
    }
}

function Ensure-AdminPassword {
    $existing = Get-Item -LiteralPath $AdminPasswordPath -Force -ErrorAction SilentlyContinue
    if ($null -ne $existing -and $existing.Length -gt 0) {
        Protect-SecretFile -Path $AdminPasswordPath
        return
    }
    if ($null -ne $existing) {
        throw "The Windows 11 password file is empty: $AdminPasswordPath"
    }

    $alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789'
    $bytes = New-Object byte[] 28
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    $password = -join ($bytes | ForEach-Object { $alphabet[$_ % $alphabet.Length] })
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($AdminPasswordPath, $password, $utf8)
    Protect-SecretFile -Path $AdminPasswordPath
}

function New-Autounattend {
    $password = (Get-Content -LiteralPath $AdminPasswordPath -Raw).Trim()
    if ([string]::IsNullOrWhiteSpace($password)) {
        throw "The Windows 11 password file is empty: $AdminPasswordPath"
    }

    $escapedPassword = [System.Security.SecurityElement]::Escape($password)
    $xml = @'
<?xml version="1.0" encoding="utf-8"?>
<unattend xmlns="urn:schemas-microsoft-com:unattend" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <settings pass="windowsPE">
    <component name="Microsoft-Windows-International-Core-WinPE" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <SetupUILanguage><UILanguage>en-US</UILanguage></SetupUILanguage>
      <InputLocale>en-US</InputLocale><SystemLocale>en-US</SystemLocale><UILanguage>en-US</UILanguage><UserLocale>en-US</UserLocale>
    </component>
    <component name="Microsoft-Windows-Setup" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <DiskConfiguration>
        <Disk wcm:action="add">
          <DiskID>0</DiskID><WillWipeDisk>true</WillWipeDisk>
          <CreatePartitions>
            <CreatePartition wcm:action="add"><Order>1</Order><Size>100</Size><Type>EFI</Type></CreatePartition>
            <CreatePartition wcm:action="add"><Order>2</Order><Size>16</Size><Type>MSR</Type></CreatePartition>
            <CreatePartition wcm:action="add"><Order>3</Order><Extend>true</Extend><Type>Primary</Type></CreatePartition>
          </CreatePartitions>
          <ModifyPartitions>
            <ModifyPartition wcm:action="add"><Order>1</Order><PartitionID>1</PartitionID><Format>FAT32</Format><Label>System</Label></ModifyPartition>
            <ModifyPartition wcm:action="add"><Order>2</Order><PartitionID>3</PartitionID><Format>NTFS</Format><Label>Windows</Label><Letter>C</Letter></ModifyPartition>
          </ModifyPartitions>
        </Disk>
        <WillShowUI>OnError</WillShowUI>
      </DiskConfiguration>
      <ImageInstall>
        <OSImage>
          <InstallFrom><MetaData wcm:action="add"><Key>/IMAGE/INDEX</Key><Value>6</Value></MetaData></InstallFrom>
          <InstallTo><DiskID>0</DiskID><PartitionID>3</PartitionID></InstallTo>
          <WillShowUI>OnError</WillShowUI>
        </OSImage>
      </ImageInstall>
      <UserData>
        <AcceptEula>true</AcceptEula>
        <ProductKey><Key>VK7JG-NPHTM-C97JM-9MPGT-3V66T</Key><WillShowUI>Never</WillShowUI></ProductKey>
      </UserData>
    </component>
  </settings>
  <settings pass="specialize">
    <component name="Microsoft-Windows-Shell-Setup" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <ComputerName>GUAC-WIN11</ComputerName><TimeZone>SE Asia Standard Time</TimeZone>
    </component>
    <component name="Microsoft-Windows-Deployment" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <RunSynchronous>
        <RunSynchronousCommand wcm:action="add"><Order>1</Order><Path>cmd /c reg add "HKLM\SYSTEM\CurrentControlSet\Control\Terminal Server" /v fDenyTSConnections /t REG_DWORD /d 0 /f</Path></RunSynchronousCommand>
        <RunSynchronousCommand wcm:action="add"><Order>2</Order><Path>cmd /c netsh advfirewall firewall set rule group="remote desktop" new enable=Yes</Path></RunSynchronousCommand>
        <RunSynchronousCommand wcm:action="add"><Order>3</Order><Path>cmd /c powercfg /hibernate off</Path></RunSynchronousCommand>
        <RunSynchronousCommand wcm:action="add"><Order>4</Order><Path>cmd /c powercfg /change standby-timeout-ac 0</Path></RunSynchronousCommand>
        <RunSynchronousCommand wcm:action="add"><Order>5</Order><Path>cmd /c powercfg /change standby-timeout-dc 0</Path></RunSynchronousCommand>
      </RunSynchronous>
    </component>
  </settings>
  <settings pass="oobeSystem">
    <component name="Microsoft-Windows-International-Core" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <InputLocale>en-US</InputLocale><SystemLocale>en-US</SystemLocale><UILanguage>en-US</UILanguage><UserLocale>en-US</UserLocale>
    </component>
    <component name="Microsoft-Windows-Shell-Setup" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <OOBE><HideEULAPage>true</HideEULAPage><HideOnlineAccountScreens>true</HideOnlineAccountScreens><HideWirelessSetupInOOBE>true</HideWirelessSetupInOOBE><ProtectYourPC>3</ProtectYourPC></OOBE>
      <UserAccounts><LocalAccounts><LocalAccount wcm:action="add"><Password><Value>__WINDOWS11_PASSWORD__</Value><PlainText>true</PlainText></Password><Description>Guacamole Windows 11 administrator</Description><DisplayName>Guacamole Admin</DisplayName><Group>Administrators</Group><Name>guacadmin</Name></LocalAccount></LocalAccounts></UserAccounts>
      <RegisteredOwner>Guacamole</RegisteredOwner><RegisteredOrganization>Guacamole Local</RegisteredOrganization>
    </component>
  </settings>
</unattend>
'@
    $xml = $xml.Replace('__WINDOWS11_PASSWORD__', $escapedPassword)
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($UnattendPath, $xml, $utf8)
}

function Ensure-UnattendIso {
    Ensure-AdminPassword
    New-Autounattend
    Invoke-WslCommand "set -eu; rm -rf '$WslUnattendStage'; mkdir -p '$WslUnattendStage'; cp '$WslUnattendPath' '$WslUnattendStage/Autounattend.xml'; genisoimage -quiet -iso-level 3 -J -R -volid GUAC_WIN11 -o '$WslUnattendIsoPath.tmp' '$WslUnattendStage'; test -s '$WslUnattendIsoPath.tmp'; mv -f '$WslUnattendIsoPath.tmp' '$WslUnattendIsoPath'; rm -rf '$WslUnattendStage'"
    Protect-SecretFile -Path $UnattendPath
    Protect-SecretFile -Path $UnattendIsoPath
}

function Assert-Inputs {
    if (-not (Test-Path -LiteralPath $IsoPath -PathType Leaf)) {
        throw "Windows 11 ISO not found: $IsoPath"
    }

    $tools = Get-WslOutput 'command -v qemu-system-x86_64 >/dev/null && command -v qemu-img >/dev/null && command -v swtpm >/dev/null && command -v genisoimage >/dev/null'
    if ($tools.ExitCode -ne 0) {
        throw 'QEMU and swtpm are required inside Ubuntu-24.04 WSL. Install qemu-system-x86, ovmf, swtpm, and swtpm-tools.'
    }

    $firmware = Get-WslOutput 'test -f /usr/share/OVMF/OVMF_CODE_4M.ms.fd && test -f /usr/share/OVMF/OVMF_VARS_4M.ms.fd && test -e /dev/kvm'
    if ($firmware.ExitCode -ne 0) {
        throw 'OVMF firmware or /dev/kvm is unavailable inside Ubuntu-24.04 WSL.'
    }
}

function Ensure-Disk {
    $diskInfo = Get-WslOutput "qemu-img info --output=json '$WslDiskPath' 2>/dev/null"
    if ($diskInfo.ExitCode -eq 0) {
        if ($diskInfo.Output -notmatch '"virtual-size"\s*:\s*107374182400') {
            throw "The existing Windows 11 disk is invalid or is not 100 GiB: $WslDiskPath"
        }
        return
    }

    Write-Host 'Creating the sparse 100 GiB Windows 11 disk inside the H:-backed WSL filesystem...'
    Invoke-WslCommand "mkdir -p '$WslVmDataRoot'; qemu-img create -f qcow2 '$WslDiskPath' 100G"
}

function Ensure-UefiVariables {
    if (Test-Path -LiteralPath $VarsPath -PathType Leaf) {
        return
    }

    Write-Host 'Creating the persistent UEFI variables file on H:...'
    Invoke-WslCommand "cp /usr/share/OVMF/OVMF_VARS_4M.ms.fd '$WslVarsPath.tmp'; mv '$WslVarsPath.tmp' '$WslVarsPath'"
}

function Ensure-TpmState {
    Ensure-RuntimeDirectory
    Invoke-WslCommand "mkdir -p '$WslTpmRoot'; chmod 700 '$WslTpmRoot'"
    $tpmState = Get-WslOutput "test -f '$WslTpmRoot/tpm2-00.permall'"
    if ($tpmState.ExitCode -eq 0) {
        return
    }

    Write-Host 'Initializing the persistent software TPM 2.0 state on H:...'
    Invoke-WslCommand "swtpm_setup --tpm2 --tpmstate '$WslTpmRoot' --createek --create-ek-cert --create-platform-cert --lock-nvram"
}

function Assert-NotRunning {
    $active = Get-WslOutput "systemctl is-active '$ServiceName'"
    if ($active.ExitCode -eq 0 -and $active.Output -match '^(active|activating)$') {
        throw 'The Windows 11 VM is running. Stop it before changing installation mode.'
    }
}

function Ensure-TpmService {
    $active = Get-WslOutput "systemctl is-active '$TpmServiceName'"
    if ($active.ExitCode -eq 0 -and $active.Output -match '^(active|activating)$') {
        $socket = Get-WslOutput "test -S '$WslTpmSocket'"
        if ($socket.ExitCode -eq 0) {
            return
        }
        Invoke-WslCommand "systemctl stop '$TpmServiceName' >/dev/null 2>&1 || true; systemctl reset-failed '$TpmServiceName' >/dev/null 2>&1 || true"
    }

    Invoke-WslCommand "mkdir -p /run/guacamole-vm-windows11; rm -f '$WslTpmSocket'; systemd-run --quiet --unit=guacamole-vm-windows11-tpm --collect --property=Restart=on-failure --property=RestartSec=3s /usr/bin/swtpm socket --tpm2 --tpmstate dir='$WslTpmRoot' --ctrl type=unixio,path='$WslTpmSocket' --log file='$WslTpmLog'"

    for ($i = 0; $i -lt 30; $i++) {
        $socket = Get-WslOutput "test -S '$WslTpmSocket'"
        if ($socket.ExitCode -eq 0) {
            return
        }
        Start-Sleep -Milliseconds 250
    }

    throw "Software TPM socket did not become ready: $WslTpmSocket"
}

function Send-InstallerVncKey {
    $pythonCode = @'
import struct
import socket
import time

host = '127.0.0.1'
port = 5901
last_error = None
sent = 0

def recv_exact(sock, size):
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise RuntimeError('VNC connection closed during handshake')
        data.extend(chunk)
    return bytes(data)

time.sleep(0.5)
for attempt in range(30):
    try:
        with socket.create_connection((host, port), timeout=3) as vnc:
            vnc.settimeout(3)
            server_version = recv_exact(vnc, 12)
            if not server_version.startswith(b'RFB '):
                raise RuntimeError('unexpected VNC protocol')
            vnc.sendall(b'RFB 003.008\n')
            security_count = recv_exact(vnc, 1)
            if len(security_count) != 1 or security_count[0] == 0:
                raise RuntimeError('VNC server offered no security type')
            security_types = recv_exact(vnc, security_count[0])
            if 1 not in security_types:
                raise RuntimeError('VNC server requires unsupported authentication')
            vnc.sendall(b'\x01')
            if recv_exact(vnc, 4) != b'\x00\x00\x00\x00':
                raise RuntimeError('VNC security handshake failed')
            vnc.sendall(b'\x01')
            server_init = recv_exact(vnc, 24)
            if len(server_init) != 24:
                raise RuntimeError('VNC server init was incomplete')
            name_length = struct.unpack('>I', server_init[20:24])[0]
            if name_length:
                recv_exact(vnc, name_length)
            vnc.sendall(struct.pack('>BBHI', 4, 1, 0, 0x20))
            vnc.sendall(struct.pack('>BBHI', 4, 0, 0, 0x20))
        sent += 1
    except (ConnectionRefusedError, TimeoutError, OSError, RuntimeError) as error:
        last_error = error
    if attempt < 29:
        time.sleep(0.75)

if sent:
    print(f'WINDOWS11_INSTALL_VNC_KEY_SENT ({sent} attempts)')
    raise SystemExit(0)

print(f'VNC server was not ready: {last_error}', file=sys.stderr)
raise SystemExit(1)
'@
    $encodedPython = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($pythonCode))
    $pythonRunner = "import base64;exec(base64.b64decode('$encodedPython'))"
    $result = @(& wsl.exe -d $WslDistro -u root -- python3 -c $pythonRunner 2>&1)
    if ($LASTEXITCODE -ne 0) {
        throw "Could not send the Windows installer boot key through VNC: $(($result -join ' ').Trim())"
    }
    $result | ForEach-Object { Write-Host $_ }
}

function Start-Qemu {
    Assert-LegacyOwnerAllowed
    Ensure-RuntimeDirectory
    Assert-Inputs

    $active = Get-WslOutput "systemctl is-active '$ServiceName'"
    if ($active.ExitCode -eq 0 -and $active.Output -match '^(active|activating)$') {
        Write-Host 'The Windows 11 VM is already running or booting.'
        return
    }

    Ensure-AdminPassword
    Ensure-UnattendIso
    Ensure-Disk
    Ensure-UefiVariables
    Ensure-TpmState

    Ensure-TpmService

    $installerMode = -not (Test-Path -LiteralPath $InstallMarker -PathType Leaf)
    if ($installerMode) {
        Write-Host 'Starting the Windows 11 installer through VNC on port 5901.'
    } else {
        Write-Host 'Starting the installed Windows 11 disk through VNC on port 5901.'
    }

    $answerIsoArgument = "-drive file='$WslUnattendIsoPath',media=cdrom,readonly=on,if=ide,index=2"
    $installIsoArgument = "-drive file='$WslIsoPath',media=cdrom,readonly=on,if=ide,index=1"
    $bootArgument = '-boot order=d,menu=on'
    $qemuBase = "rm -f '$WslMonitorSocket'; systemctl reset-failed '$ServiceName' >/dev/null 2>&1 || true; systemd-run --quiet --unit=guacamole-vm-windows11 --collect --property=Restart=on-failure --property=RestartSec=5s /usr/bin/qemu-system-x86_64 -name guacamole-vm-windows11 -enable-kvm -machine q35,accel=kvm -cpu host -m 8192 -smp 4 -drive if=pflash,format=raw,readonly=on,file=/usr/share/OVMF/OVMF_CODE_4M.ms.fd -drive if=pflash,format=raw,unit=1,file='$WslVarsPath' -drive file='$WslDiskPath',if=ide,format=qcow2 -chardev socket,id=chrtpm,path='$WslTpmSocket' -tpmdev emulator,id=tpm0,chardev=chrtpm -device tpm-tis,tpmdev=tpm0 -netdev user,id=n0,hostfwd=tcp:0.0.0.0:3391-:3389 -device e1000e,netdev=n0 -vnc 0.0.0.0:1 -monitor unix:$WslMonitorSocket,server=on,wait=off -display none -serial file:'$WslConsoleLog'"
    if ($installerMode) {
        $qemuCommand = "$qemuBase $bootArgument $installIsoArgument $answerIsoArgument"
    } else {
        $qemuCommand = $qemuBase
    }
    Invoke-WslCommand $qemuCommand
    if ($installerMode) {
        Send-InstallerVncKey
    }
    Write-Host 'Windows 11 QEMU/KVM is running. Guacamole VNC uses host 172.18.0.1 port 5901; RDP will use port 3391 after Windows enables RDP.'
}

function Show-Status {
    $disk = Get-WslOutput "test -f '$WslDiskPath'"
    if ($disk.ExitCode -ne 0) {
        Write-Host 'Windows 11 VM has not been prepared yet.'
        return
    }

    $qemu = Get-WslOutput "systemctl is-active '$ServiceName'"
    $tpm = Get-WslOutput "systemctl is-active '$TpmServiceName'"
    Write-Host "Windows 11 QEMU: $(if ($qemu.ExitCode -eq 0) { $qemu.Output } else { 'stopped' })"
    Write-Host "Windows 11 TPM: $(if ($tpm.ExitCode -eq 0) { $tpm.Output } else { 'stopped' })"
    Write-Host "Installer finished: $(if (Test-Path -LiteralPath $InstallMarker -PathType Leaf) { 'yes' } else { 'no; first start attaches the ISO' })"
    Write-Host 'VNC: 172.18.0.1:5901 | RDP forward: 172.18.0.1:3391'
    Write-Host "Disk: $WslDiskPath (inside H:-backed runtime\\ubuntu\\ext4.vhdx)"
}

function Request-QemuPowerdown {
    $pythonCode = @'
import socket
import sys

socket_path = sys.argv[1]
with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as monitor:
    monitor.settimeout(5)
    monitor.connect(socket_path)
    monitor.sendall(b'system_powerdown\n')
'@
    $encodedPython = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($pythonCode))
    $pythonRunner = "import base64;exec(base64.b64decode('$encodedPython'))"
    $result = @(& wsl.exe -d $WslDistro -u root -- python3 -c $pythonRunner $WslMonitorSocket 2>&1)
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Could not request a graceful Windows shutdown through the QEMU monitor: $(($result -join ' ').Trim())"
        return $false
    }
    return $true
}

function Wait-ForQemuStop {
    $deadline = [DateTime]::UtcNow.AddSeconds($GracefulStopTimeoutSeconds)
    do {
        $active = Get-WslOutput "systemctl is-active '$ServiceName'"
        if ($active.ExitCode -ne 0 -or $active.Output -notmatch '^(active|activating|deactivating)$') {
            return $true
        }
        Start-Sleep -Seconds 1
    } while ([DateTime]::UtcNow -lt $deadline)

    return $false
}

function Stop-Qemu {
    Assert-LegacyOwnerAllowed
    if ($AllowLegacyQemuOwner) {
        Write-Host 'LEGACY_QEMU_OWNER_AUTHORIZED'
    }

    $active = Get-WslOutput "systemctl is-active '$ServiceName'"
    if ($active.ExitCode -eq 0 -and $active.Output -match '^(active|activating)$') {
        $monitor = Get-WslOutput "test -S '$WslMonitorSocket'"
        $gracefulRequested = $false
        if ($monitor.ExitCode -eq 0) {
            $gracefulRequested = Request-QemuPowerdown
        } else {
            Write-Warning 'The QEMU monitor socket is unavailable; a forced service stop may be required.'
        }

        if ($gracefulRequested -and (Wait-ForQemuStop)) {
            Write-Host 'Windows 11 shut down gracefully through QEMU ACPI.'
        } else {
            Write-Warning "Windows 11 did not stop gracefully within $GracefulStopTimeoutSeconds seconds; requesting a service stop."
            Invoke-WslCommand "systemctl stop '$ServiceName'"
            Write-Host 'Stopped the Windows 11 VM after the graceful-stop timeout.'
        }
    } else {
        Write-Host 'Windows 11 VM is not running.'
    }

    $tpm = Get-WslOutput "systemctl is-active '$TpmServiceName'"
    if ($tpm.ExitCode -eq 0 -and $tpm.Output -match '^(active|activating)$') {
        Invoke-WslCommand "systemctl stop '$TpmServiceName'"
    }
}

function Finish-Install {
    Assert-LegacyOwnerAllowed
    Assert-NotRunning
    $disk = Get-WslOutput "test -f '$WslDiskPath'"
    if ($disk.ExitCode -ne 0) {
        throw 'The Windows 11 disk does not exist yet. Run start once to create it and install Windows.'
    }

    Set-Content -LiteralPath $InstallMarker -Value 'Windows 11 installation completed; the next start omits the ISO.' -NoNewline -Encoding ascii
    Write-Host 'Marked the Windows 11 installation as finished. The next start boots from the disk and keeps VNC on port 5901.'
}

switch ($Action) {
    'start'          { Start-Qemu }
    'status'         { Show-Status }
    'stop'           { Stop-Qemu }
    'finish-install' { Finish-Install }
}
