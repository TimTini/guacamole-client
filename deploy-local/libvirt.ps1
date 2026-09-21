[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('preflight', 'install', 'configure', 'cockpit-workspaces', 'network', 'storage', 'connect-guacamole', 'start', 'stop', 'status', 'backup', 'export')]
    [string]$Action = 'status',

    [string]$DomainName = 'windows11',

    [int]$TimeoutSeconds = 120
)

$ErrorActionPreference = 'Stop'

$ScriptRoot = $PSScriptRoot
$RepoRoot = (Resolve-Path (Join-Path $ScriptRoot '..')).Path
$WslDistro = 'Ubuntu-24.04'
$WslRepoRoot = '/mnt/h/RemoteWorkspaces/guacamole-client'
$WslDeployRoot = "$WslRepoRoot/deploy-local"
$VhdxPath = Join-Path $RepoRoot 'runtime\ubuntu\ext4.vhdx'
$WslWindowsDiskPath = '/var/lib/guacamole-vm-windows11/windows11.qcow2'
$WslTpmStatePath = '/var/lib/guacamole-vm-windows11/tpm'
$WslTpmSocketPath = '/run/guacamole-vm-windows11/swtpm.sock'
$TpmStatePath = $WslTpmStatePath
$TpmHBackedStatePath = "$WslRepoRoot/runtime/vm-windows11/tpm"
$TpmSocketPath = $WslTpmSocketPath
$TpmUnitName = 'guacamole-vm-windows11-libvirt-tpm.service'
$CockpitUrl = 'https://127.0.0.1:9090'
$LibvirtDomainNamePattern = '^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$'
$LibvirtNetworkName = 'guac-nat'
$LibvirtNetworkXmlPath = Join-Path $ScriptRoot 'libvirt\networks\guac-nat.xml'
$LibvirtBridgeName = 'virbr-guac'
$LibvirtNetworkSubnet = '192.168.250.0/24'
$LibvirtNetworkGateway = '192.168.250.1'
$LibvirtReservedIp = '192.168.250.11'
$LibvirtReservedMac = '52:54:00:11:11:01'
$LibvirtReservedHostName = 'windows11'
$LibvirtStoragePoolName = 'guacamole-vms'
$LibvirtStoragePoolTarget = '/var/lib/guacamole-vms'
$ComposeProjectName = 'guacamole-local'
$ComposeNetworkName = "$ComposeProjectName`_default"

if ($DomainName -notmatch $LibvirtDomainNamePattern) {
    throw "DomainName '$DomainName' is invalid. Use 1-128 characters matching ^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$ before invoking libvirt."
}

$PackageNames = @(
    'cockpit',
    'cockpit-machines',
    'libvirt-daemon-system',
    'libvirt-daemon-driver-qemu',
    'libvirt-clients',
    'libvirt-dbus',
    'qemu-system-x86',
    'qemu-utils',
    'ovmf',
    'swtpm',
    'swtpm-tools',
    'dnsmasq',
    'iptables',
    'netcat-openbsd'
)

$BackupRoot = Join-Path $RepoRoot 'runtime\backups'
$LegacyWindowsScriptPath = Join-Path $ScriptRoot 'vm-windows11\windows11.ps1'
$LegacyWindowsRuntimeRoot = Join-Path $RepoRoot 'runtime\vm-windows11'
$CutoverMarkerPath = Join-Path $LegacyWindowsRuntimeRoot 'libvirt-cutover.marker'
$LegacyNVRAMPath = Join-Path $LegacyWindowsRuntimeRoot 'OVMF_VARS_4M.ms.fd'
$LegacyTpmStatePath = Join-Path $LegacyWindowsRuntimeRoot 'tpm'
$ExportMaintenanceScriptPath = Join-Path $ScriptRoot 'export-maintenance-bundle.ps1'
$CockpitWorkspaceSourcePath = Join-Path $ScriptRoot 'cockpit\workspace_templates'
$WorkspaceHelperSourcePath = Join-Path $ScriptRoot 'workspace-helper.py'
$CockpitWorkspaceTargetPath = '/usr/local/share/cockpit/workspace_templates'
$WorkspaceHelperTargetPath = '/usr/local/libexec/guacamole-workspace-helper'
$WorkspaceHelperBundleTargetPath = '/usr/local/libexec/guacamole-workspace'
$CockpitWorkspacePackageVersion = '3'

function Invoke-WslCommand {
    param(
        [Parameter(Mandatory)]
        [string]$Command,

        [switch]$AllowFailure
    )

    $previousErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = @(& wsl.exe -d $WslDistro -u root -- sh -lc $Command 2>&1)
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    $text = (($output | ForEach-Object { $_.ToString() }) -join [Environment]::NewLine).Trim()

    if ($exitCode -ne 0 -and -not $AllowFailure) {
        $detail = if ([string]::IsNullOrWhiteSpace($text)) { '(no output)' } else { $text }
        throw ("WSL command failed with exit code {0}: {1}{2}{3}" -f $exitCode, $Command, [Environment]::NewLine, $detail)
    }

    [pscustomobject]@{
        Command = $Command
        Output = $text
        ExitCode = $exitCode
    }
}

function Invoke-WslProbe {
    param(
        [Parameter(Mandatory)]
        [string]$Label,

        [Parameter(Mandatory)]
        [string]$Command
    )

    $result = Invoke-WslCommand -Command $Command -AllowFailure
    Write-Host "[$Label] exit=$($result.ExitCode)"
    if (-not [string]::IsNullOrWhiteSpace($result.Output)) {
        $result.Output -split [Environment]::NewLine | ForEach-Object { Write-Host "  $_" }
    }
    return $result
}

function Invoke-HostProbe {
    param(
        [Parameter(Mandatory)]
        [string]$Label,

        [Parameter(Mandatory)]
        [scriptblock]$Script
    )

    try {
        $value = & $Script
        Write-Host "[$Label] OK"
        return [pscustomobject]@{ Label = $Label; Success = $true; Value = $value; Error = $null }
    } catch {
        Write-Host "[$Label] FAILED: $($_.Exception.Message)"
        return [pscustomobject]@{ Label = $Label; Success = $false; Value = $null; Error = $_.Exception.Message }
    }
}

function Assert-WslAndHStorage {
    if (-not (Test-Path -LiteralPath $VhdxPath -PathType Leaf)) {
        throw "The H-backed WSL disk was not found: $VhdxPath"
    }

    $distroList = @(& wsl.exe --list --quiet 2>&1)
    if ($LASTEXITCODE -ne 0) {
        throw 'Could not list WSL distributions.'
    }

    $names = @($distroList |
        ForEach-Object { $_.ToString() -replace [string][char]0, '' } |
        ForEach-Object { $_.Trim() } |
        Where-Object { $_ })
    if ($names -notcontains $WslDistro) {
        throw "WSL distribution '$WslDistro' is not registered."
    }
}

function Invoke-PreflightVersions {
    $packageText = $PackageNames -join ' '
    Invoke-WslProbe -Label 'apt-cache policy' -Command "apt-cache policy $packageText" | Out-Null
    Invoke-WslProbe -Label 'virsh version' -Command 'virsh version' | Out-Null
    Invoke-WslProbe -Label 'Cockpit bridge version' -Command 'cockpit-bridge --version' | Out-Null
}

function Invoke-ComposeStatus {
    Invoke-WslProbe -Label 'Docker Compose' -Command "docker compose --project-directory '$WslDeployRoot' --file '$WslDeployRoot/compose.yaml' ps" | Out-Null
}

function Invoke-LegacyOwnerStatus {
    Invoke-WslProbe -Label 'legacy QEMU unit' -Command "systemctl is-active 'guacamole-vm-windows11.service'" | Out-Null
    Invoke-WslProbe -Label 'legacy TPM unit' -Command "systemctl is-active 'guacamole-vm-windows11-tpm.service'" | Out-Null
}

function Get-TpmOwnerEvidence {
    $evidenceScript = @'
active_state=$(systemctl show -p ActiveState --value __TPM_UNIT__ 2>/dev/null || true)
main_pid=$(systemctl show -p MainPID --value __TPM_UNIT__ 2>/dev/null || printf '0')
legacy_active_state=$(systemctl show -p ActiveState --value __LEGACY_TPM_UNIT__ 2>/dev/null || true)
legacy_pid=$(systemctl show -p MainPID --value __LEGACY_TPM_UNIT__ 2>/dev/null || printf '0')
socket_exists=0
if test -S '__TPM_SOCKET__'; then socket_exists=1; fi
socket_owner_pid=$(ss -xlpnH 2>/dev/null | grep -F '__TPM_SOCKET__' | grep -o 'pid=[0-9]*' | head -n 1 | cut -d= -f2 || true)
dedicated_cmdline=''
if test "$main_pid" -gt 0 2>/dev/null && test -r "/proc/$main_pid/cmdline"; then
    dedicated_cmdline=$(tr '\0' ' ' < "/proc/$main_pid/cmdline")
fi
legacy_cmdline=''
if test "$legacy_pid" -gt 0 2>/dev/null && test -r "/proc/$legacy_pid/cmdline"; then
    legacy_cmdline=$(tr '\0' ' ' < "/proc/$legacy_pid/cmdline")
fi
printf 'active_state=%s\n' "$active_state"
printf 'main_pid=%s\n' "$main_pid"
printf 'legacy_active_state=%s\n' "$legacy_active_state"
printf 'legacy_pid=%s\n' "$legacy_pid"
printf 'socket_exists=%s\n' "$socket_exists"
printf 'socket_owner_pid=%s\n' "$socket_owner_pid"
printf 'dedicated_cmdline=%s\n' "$dedicated_cmdline"
printf 'legacy_cmdline=%s\n' "$legacy_cmdline"
'@
    $evidenceScript = $evidenceScript.Replace('__TPM_UNIT__', $TpmUnitName)
    $evidenceScript = $evidenceScript.Replace('__LEGACY_TPM_UNIT__', 'guacamole-vm-windows11-tpm.service')
    $evidenceScript = $evidenceScript.Replace('__TPM_SOCKET__', $TpmSocketPath)
    $evidenceScript = (($evidenceScript -split [Environment]::NewLine |
        ForEach-Object { $_.Trim() } |
        Where-Object { $_ }) -join ' ')
    # PowerShell 5 can expand/strip `$` expressions when a native command receives
    # a shell program directly. Transport the read-only probe as base64 so shell
    # variables and command substitutions reach `sh` unchanged.
    $encodedEvidenceScript = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($evidenceScript))
    $result = Invoke-WslCommand -Command "printf '%s' '$encodedEvidenceScript' | base64 -d | sh" -AllowFailure
    $values = @{}
    foreach ($line in ($result.Output -split [Environment]::NewLine)) {
        if ($line -match '^([^=]+)=(.*)$') {
            $values[$matches[1]] = $matches[2]
        }
    }

    $mainPid = 0
    $legacyPid = 0
    $socketOwnerPid = 0
    [void][int]::TryParse([string]$values['main_pid'], [ref]$mainPid)
    [void][int]::TryParse([string]$values['legacy_pid'], [ref]$legacyPid)
    [void][int]::TryParse([string]$values['socket_owner_pid'], [ref]$socketOwnerPid)
    $socketExists = [string]$values['socket_exists'] -eq '1'
    $dedicatedCommandLine = [string]$values['dedicated_cmdline']
    $legacyCommandLine = [string]$values['legacy_cmdline']
    $dedicatedProcessMatches = $dedicatedCommandLine -match [regex]::Escape('/usr/bin/swtpm') -and
        $dedicatedCommandLine -match [regex]::Escape("--ctrl type=unixio,path=$TpmSocketPath") -and
        ($dedicatedCommandLine -match [regex]::Escape("--tpmstate dir=$TpmStatePath") -or
         $dedicatedCommandLine -match [regex]::Escape("--tpmstate dir=$TpmHBackedStatePath"))
    $socketOwnedByDedicated = $mainPid -gt 0 -and $socketOwnerPid -eq $mainPid
    $socketOwnedByLegacy = $legacyPid -gt 0 -and $socketOwnerPid -eq $legacyPid
    $dedicatedOwnerReady = [string]$values['active_state'] -eq 'active' -and
        $mainPid -gt 0 -and $dedicatedProcessMatches -and $socketExists -and $socketOwnedByDedicated
    $legacyUnitActive = [string]$values['legacy_active_state'] -eq 'active'

    $ownerState = if ($legacyUnitActive -or $socketOwnedByLegacy) {
        'LEGACY_TPM_OWNER'
    } elseif ($dedicatedOwnerReady) {
        'DEDICATED_TPM_OWNER'
    } elseif (-not $socketExists -and [string]$values['active_state'] -ne 'active') {
        'TPM_SOCKET_ABSENT'
    } elseif ($socketExists) {
        'TPM_OWNER_MISMATCH'
    } else {
        'DEDICATED_TPM_UNIT_INACTIVE'
    }

    [pscustomobject]@{
        ActiveState = [string]$values['active_state']
        MainPid = $mainPid
        LegacyActiveState = [string]$values['legacy_active_state']
        LegacyPid = $legacyPid
        SocketExists = $socketExists
        SocketOwnerPid = $socketOwnerPid
        DedicatedProcessMatches = $dedicatedProcessMatches
        SocketOwnedByDedicated = $socketOwnedByDedicated
        SocketOwnedByLegacy = $socketOwnedByLegacy
        LegacyUnitActive = $legacyUnitActive
        DedicatedOwnerReady = $dedicatedOwnerReady
        OwnerState = $ownerState
        DedicatedCommandLine = $dedicatedCommandLine
        LegacyCommandLine = $legacyCommandLine
        ProbeExitCode = $result.ExitCode
    }
}

function Invoke-TpmOwnerStatus {
    Write-Host "TPM contract owner: $TpmUnitName"
    Write-Host "TPM state path: $TpmStatePath"
    Write-Host "TPM socket path: $TpmSocketPath"
    Invoke-WslProbe -Label 'existing TPM state directory' -Command "test -d '$TpmStatePath' || test -d '$TpmHBackedStatePath'" | Out-Null
    $evidence = Get-TpmOwnerEvidence
    Write-Host "TPM owner state: $($evidence.OwnerState)"
    Write-Host "TPM dedicated unit: ActiveState=$($evidence.ActiveState) MainPID=$($evidence.MainPid)"
    Write-Host "TPM socket: exists=$($evidence.SocketExists) ownerPID=$($evidence.SocketOwnerPid) dedicatedPID=$($evidence.MainPid) legacyPID=$($evidence.LegacyPid)"
    if ($evidence.OwnerState -eq 'LEGACY_TPM_OWNER') {
        Write-Host 'LEGACY_TPM_OWNER: the shared TPM socket is still owned by the legacy swtpm lifecycle.'
    } elseif ($evidence.OwnerState -eq 'DEDICATED_TPM_OWNER') {
        Write-Host 'DEDICATED_TPM_OWNER: active unit, expected swtpm command line, and socket PID match verified.'
    } elseif ($evidence.OwnerState -eq 'TPM_OWNER_MISMATCH') {
        Write-Host 'TPM_OWNER_MISMATCH: socket exists but is not owned by the active dedicated unit.'
    }
    return $evidence
}

function Invoke-Preflight {
    Assert-WslAndHStorage
    $failures = [System.Collections.Generic.List[string]]::new()

    Write-Host "H-backed WSL VHDX: $VhdxPath"
    Write-Host "WSL distro: $WslDistro"
    Write-Host 'Preflight is read-only; it will not define/start a VM or start swtpm.'

    $wslVerbose = Invoke-HostProbe -Label 'wsl --list --verbose' -Script {
        & wsl.exe --list --verbose
        if ($LASTEXITCODE -ne 0) { throw 'wsl --list --verbose failed.' }
    }
    if (-not $wslVerbose.Success) { $failures.Add($wslVerbose.Label) }

    $requiredChecks = @(
        [pscustomobject]@{ Label = '/dev/kvm and current Windows disk'; Command = "test -e /dev/kvm && qemu-img info '$WslWindowsDiskPath'" },
        [pscustomobject]@{ Label = 'libvirt system URI'; Command = 'virsh -c qemu:///system uri' },
        [pscustomobject]@{ Label = 'systemd state'; Command = 'systemctl is-system-running' },
        [pscustomobject]@{ Label = 'Cockpit socket state'; Command = 'systemctl is-active cockpit.socket' },
        [pscustomobject]@{ Label = 'existing TPM state directory'; Command = "test -d '$TpmStatePath' || test -d '$TpmHBackedStatePath'" }
    )
    foreach ($check in $requiredChecks) {
        $result = Invoke-WslProbe -Label $check.Label -Command $check.Command
        if ($result.ExitCode -ne 0) {
            $failures.Add($check.Label)
        }
    }

    $tpmEvidence = Invoke-TpmOwnerStatus
    if ($tpmEvidence.OwnerState -eq 'LEGACY_TPM_OWNER') {
        $failures.Add('LEGACY_TPM_OWNER')
    } elseif ($tpmEvidence.OwnerState -eq 'TPM_OWNER_MISMATCH') {
        $failures.Add('TPM_OWNER_MISMATCH')
    }

    Invoke-PreflightVersions
    Invoke-WslProbe -Label 'listeners' -Command 'ss -ltnp' | Out-Null
    Invoke-ComposeStatus
    Invoke-WslProbe -Label 'libvirt domains' -Command 'virsh -c qemu:///system list --all' | Out-Null
    Invoke-LegacyOwnerStatus

    $backupRoot = Join-Path $RepoRoot 'runtime\backups'
    if (Test-Path -LiteralPath $backupRoot -PathType Container) {
        Write-Host "Legacy backup directory present: $backupRoot"
    } else {
        Write-Host "Legacy backup directory not present yet: $backupRoot"
    }

    if ($failures.Count -gt 0) {
        throw "LIBVIRT_PREFLIGHT_BLOCKED: $($failures -join ', ')"
    }

    Write-Host 'LIBVIRT_PREFLIGHT_OK'
}

function Enable-LibvirtServices {
    $serviceScript = @'
set -eu
for unit in libvirtd.service virtqemud.socket virtnetworkd.socket virtstoraged.socket virtlogd.socket virtlockd.socket; do
    if systemctl cat "$unit" >/dev/null 2>&1; then
        systemctl enable --now "$unit"
    fi
done
'@
    $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($serviceScript))
    Invoke-WslCommand -Command "printf '%s' '$encoded' | base64 -d | sh" | Out-Null
}

function Disable-ConflictingGlobalDnsmasq {
    $result = Invoke-WslCommand -Command "if systemctl is-enabled dnsmasq.service >/dev/null 2>&1 || systemctl is-failed dnsmasq.service >/dev/null 2>&1; then systemctl disable --now dnsmasq.service >/dev/null 2>&1 || true; systemctl reset-failed dnsmasq.service >/dev/null 2>&1 || true; printf '%s' disabled; else printf '%s' absent; fi"
    if ($result.Output -eq 'disabled') {
        Write-Host 'Disabled the package-level dnsmasq.service because WSL owns 10.255.255.254:53; libvirt networks use their own managed dnsmasq instance.'
    }
}

function Ensure-SwtpmLocalCaOwnership {
    $stateScript = @'
set -eu
localca=/var/lib/swtpm-localca
getent passwd swtpm >/dev/null
getent group swtpm >/dev/null
if test -e "$localca" && ! test -d "$localca"; then
    echo "SWTPM_LOCALCA_NOT_DIRECTORY: $localca" >&2
    exit 1
fi
install -d -m 0750 -o swtpm -g swtpm "$localca"
chown -R --no-dereference swtpm:swtpm "$localca"
if find "$localca" -xdev \( ! -user swtpm -o ! -group swtpm \) -print -quit | grep -q .; then
    echo "SWTPM_LOCALCA_OWNERSHIP_FAILED: $localca contains an entry not owned by swtpm:swtpm" >&2
    exit 1
fi
printf '%s\n' SWTPM_LOCALCA_OWNERSHIP_OK
'@
    $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($stateScript))
    $result = Invoke-WslCommand -Command "printf '%s' '$encoded' | base64 -d | sh"
    if ($result.Output) { Write-Host $result.Output }
}

function Install-Packages {
    $packageText = $PackageNames -join ' '
    Write-Host 'Package policy before installation:'
    $result = Invoke-WslCommand -Command "apt-cache policy $packageText"
    if ($result.Output) { Write-Host $result.Output }

    $result = Invoke-WslCommand -Command 'apt-get update'
    if ($result.Output) { Write-Host $result.Output }

    $result = Invoke-WslCommand -Command "DEBIAN_FRONTEND=noninteractive apt-get install -y $packageText"
    if ($result.Output) { Write-Host $result.Output }
    Ensure-SwtpmLocalCaOwnership
    Disable-ConflictingGlobalDnsmasq
    Enable-LibvirtServices

    Write-Host 'Package policy after installation:'
    $result = Invoke-WslCommand -Command "apt-cache policy $packageText"
    if ($result.Output) { Write-Host $result.Output }

    $result = Invoke-WslCommand -Command 'virsh version'
    if ($result.Output) { Write-Host $result.Output }
    $result = Invoke-WslCommand -Command 'cockpit-bridge --version'
    if ($result.Output) { Write-Host $result.Output }

    $failed = Invoke-WslCommand -Command 'systemctl --failed --no-legend' -AllowFailure
    if (-not [string]::IsNullOrWhiteSpace($failed.Output)) {
        Write-Warning ("systemd failed units reported:" + [Environment]::NewLine + $failed.Output)
    }

    $result = Invoke-WslCommand -Command 'virsh -c qemu:///system uri'
    if ($result.Output) { Write-Host $result.Output }
    $result = Invoke-WslCommand -Command 'virsh -c qemu:///system list --all'
    if ($result.Output) { Write-Host $result.Output }
    Write-Host 'LIBVIRT_INSTALL_OK'
}

function Install-CockpitWorkspaces {
    if (-not (Test-Path -LiteralPath $CockpitWorkspaceSourcePath -PathType Container)) {
        throw "COCKPIT_WORKSPACE_SOURCE_MISSING: '$CockpitWorkspaceSourcePath'."
    }
    if (-not (Test-Path -LiteralPath $WorkspaceHelperSourcePath -PathType Leaf)) {
        throw "WORKSPACE_HELPER_SOURCE_MISSING: '$WorkspaceHelperSourcePath'."
    }

    $packageSource = "$WslDeployRoot/cockpit/workspace_templates"
    $helperSource = "$WslDeployRoot/workspace-helper.py"
    $initializerSource = "$WslDeployRoot/initialize-windows-auth.ps1"
    $composeSource = "$WslDeployRoot/compose.yaml"
    $xmlSource = "$WslDeployRoot/libvirt/domains/windows-clone.xml.template"
    $tpmStateSource = "$WslRepoRoot/runtime/vm-windows11/tpm"
    $installScript = @'
set -eu
package_source='__PACKAGE_SOURCE__'
helper_source='__HELPER_SOURCE__'
initializer_source='__INITIALIZER_SOURCE__'
compose_source='__COMPOSE_SOURCE__'
xml_source='__XML_SOURCE__'
tpm_state_source='__TPM_STATE_SOURCE__'
package_target='/usr/local/share/cockpit/workspace_templates'
helper_target='/usr/local/libexec/guacamole-workspace-helper'
bundle_target='/usr/local/libexec/guacamole-workspace'
job_status_dir='/var/lib/guacamole-workspaces/jobs'
version='__VERSION__'
install_lock='/run/lock/guacamole-workspace-install.lock'
mkdir -p "$(dirname "$install_lock")"
exec 9>"$install_lock"
flock -n 9 || { echo 'WORKSPACE_INSTALL_BUSY' >&2; exit 1; }
release_stage="/usr/local/libexec/.guacamole-workspace-release.stage.$$"
release_root="/usr/local/libexec"
current_link="${release_root}/guacamole-workspace-release.current"
package_stage="${release_stage}/package"
bundle_stage="${release_stage}/bundle"
launcher_stage="${release_stage}/launcher"
current_new="${current_link}.new.$$"
release_version=""
cache_key=""
package_link="${package_target}.new.$$"
bundle_link="${bundle_target}.new.$$"
helper_link="${helper_target}.new.$$"
package_backup="${package_target}.legacy.$$.old"
bundle_backup="${bundle_target}.legacy.$$.old"
helper_backup="${helper_target}.legacy.$$.old"
current_backup="${current_link}.legacy.$$.old"
package_kind='absent'
bundle_kind='absent'
helper_kind='absent'
current_kind='absent'
package_old_link=''
bundle_old_link=''
helper_old_link=''
current_old_link=''
package_backed_up=0
bundle_backed_up=0
helper_backed_up=0
current_backed_up=0
package_swapped=0
bundle_swapped=0
helper_swapped=0
pointer_swapped=0
publication_committed=0
release_created=0

cleanup() {
    status=$?
    trap - EXIT HUP INT TERM
    rm -rf -- "$release_stage" "$current_new" "$package_link" "$bundle_link" "$helper_link"
    if [ "$publication_committed" -eq 0 ]; then
        restore_entry() {
            target="$1"
            kind="$2"
            old_link="$3"
            backup="$4"
            swapped="$5"
            backed_up="$6"
            if [ "$swapped" -eq 1 ] || {
                [ "$backed_up" -eq 1 ] && [ ! -e "$target" ] && [ ! -L "$target" ]
            }; then
                rm -rf -- "$target"
                case "$kind" in
                    regular)
                        if [ "$backed_up" -eq 1 ] && { [ -e "$backup" ] || [ -L "$backup" ]; }; then
                            mv -T -- "$backup" "$target"
                        fi
                        ;;
                    link) ln -s -- "$old_link" "$target" ;;
                esac
            fi
        }
        restore_entry "$current_link" "$current_kind" "$current_old_link" "$current_backup" "$pointer_swapped" "$current_backed_up"
        restore_entry "$package_target" "$package_kind" "$package_old_link" "$package_backup" "$package_swapped" "$package_backed_up"
        restore_entry "$bundle_target" "$bundle_kind" "$bundle_old_link" "$bundle_backup" "$bundle_swapped" "$bundle_backed_up"
        restore_entry "$helper_target" "$helper_kind" "$helper_old_link" "$helper_backup" "$helper_swapped" "$helper_backed_up"
        if [ -n "$release_version" ] && [ "$release_created" -eq 1 ]; then
            rm -rf -- "$release_version"
        fi
    else
        rm -rf -- "$package_backup" "$bundle_backup" "$helper_backup" "$current_backup"
    fi
    exit "$status"
}
trap cleanup EXIT HUP INT TERM

for file in manifest.json index.html workspace-templates.js workspace-templates.css; do
    test -f "$package_source/$file"
done
test -f "$helper_source"
grep -q "set-windows-credential" "$helper_source"
grep -q "/var/lib/guacamole-workspace/secrets/windows11_guacadmin_password" "$helper_source"
test -f "$initializer_source"
grep -q "/usr/local/libexec/guacamole-workspace-helper" "$initializer_source"
test -f "$compose_source"
test -f "$xml_source"
test -d "$tpm_state_source"
secret_root='/var/lib/guacamole-workspace/secrets'
if [ -L "$secret_root" ]; then
    echo 'WORKSPACE_SECRET_ROOT_UNSAFE: canonical secret root must not be a symlink' >&2
    exit 1
fi
install -d -o root -g root -m 0700 "$secret_root"
chown root:root "$secret_root"
chmod 0700 "$secret_root"
secret_root_metadata="$(stat -c '%U:%G:%a:%F' "$secret_root")"
if [ "$secret_root_metadata" != 'root:root:700:directory' ]; then
    echo "WORKSPACE_SECRET_ROOT_UNSAFE: expected root:root:700:directory, got $secret_root_metadata" >&2
    exit 1
fi
install -d -o root -g root -m 0750 "$job_status_dir"
rm -rf -- "$release_stage"
install -d -o root -g root -m 0755 "$package_stage"
install -d -o root -g root -m 0755 "$bundle_stage/libvirt/domains"
cache_key="$(sha256sum "$package_source/manifest.json" "$package_source/index.html" "$package_source/workspace-templates.js" "$package_source/workspace-templates.css" "$helper_source" "$initializer_source" "$compose_source" "$xml_source" | sha256sum | cut -c1-16)"
release_id="${version}.${cache_key}"
release_version="${release_root}/guacamole-workspace-release.v${release_id}"
for file in manifest.json workspace-templates.js workspace-templates.css; do
    install -o root -g root -m 0644 "$package_source/$file" "$package_stage/$file"
done
sed -E "s/(workspace-templates\\.(css|js)\\?v=)[A-Za-z0-9._-]*/\\1${cache_key}/g" "$package_source/index.html" > "$package_stage/index.html.tmp"
install -o root -g root -m 0644 "$package_stage/index.html.tmp" "$package_stage/index.html"
rm -f -- "$package_stage/index.html.tmp"
printf '%s\n' "$version" | install -o root -g root -m 0644 /dev/stdin "$package_stage/VERSION"
printf '%s\n' "version=${version}" "cacheKey=${cache_key}" "release=${release_id}" | install -o root -g root -m 0644 /dev/stdin "$release_stage/RELEASE"
install -o root -g root -m 0755 "$helper_source" "$bundle_stage/workspace-helper.py"
install -o root -g root -m 0755 "$initializer_source" "$bundle_stage/initialize-windows-auth.ps1"
install -o root -g root -m 0644 "$compose_source" "$bundle_stage/compose.yaml"
install -o root -g root -m 0644 "$xml_source" "$bundle_stage/libvirt/domains/windows-clone.xml.template"
printf '%s\n' "$tpm_state_source" | install -o root -g root -m 0644 /dev/stdin "$bundle_stage/source-tpm-state.path"
printf '%s\n' "$version" | install -o root -g root -m 0644 /dev/stdin "$bundle_stage/VERSION"
python3 -m py_compile "$bundle_stage/workspace-helper.py"
test -s "$bundle_stage/compose.yaml"
test -s "$bundle_stage/libvirt/domains/windows-clone.xml.template"
printf '%s\n' '#!/usr/bin/python3' 'import runpy' 'runpy.run_path("/usr/local/libexec/guacamole-workspace-release.current/bundle/workspace-helper.py", run_name="__main__")' > "$launcher_stage"
chown root:root "$launcher_stage"
chmod 0755 "$launcher_stage"

if [ -e "$release_version" ]; then
    rm -rf -- "$release_stage"
else
    mv -T "$release_stage" "$release_version"
    release_created=1
fi

if [ -L "$package_target" ]; then
    package_kind='link'
    package_old_link="$(readlink "$package_target")"
elif [ -e "$package_target" ]; then
    package_kind='regular'
    package_backed_up=1
    mv -T -- "$package_target" "$package_backup"
fi
if [ -L "$bundle_target" ]; then
    bundle_kind='link'
    bundle_old_link="$(readlink "$bundle_target")"
elif [ -e "$bundle_target" ]; then
    bundle_kind='regular'
    bundle_backed_up=1
    mv -T -- "$bundle_target" "$bundle_backup"
fi
if [ -L "$helper_target" ]; then
    helper_kind='link'
    helper_old_link="$(readlink "$helper_target")"
elif [ -e "$helper_target" ]; then
    helper_kind='regular'
    helper_backed_up=1
    mv -T -- "$helper_target" "$helper_backup"
fi
ln -s "$current_link/package" "$package_link"
ln -s "$current_link/bundle" "$bundle_link"
ln -s "$current_link/launcher" "$helper_link"
package_swapped=1
mv -Tf "$package_link" "$package_target"
bundle_swapped=1
mv -Tf "$bundle_link" "$bundle_target"
helper_swapped=1
mv -Tf "$helper_link" "$helper_target"
if [ -L "$current_link" ]; then
    current_kind='link'
    current_old_link="$(readlink "$current_link")"
elif [ -e "$current_link" ]; then
    current_kind='regular'
    current_backed_up=1
    mv -T -- "$current_link" "$current_backup"
fi
ln -s "$release_version" "$current_new"
pointer_swapped=1
mv -Tf "$current_new" "$current_link"
cockpit-bridge --packages
publication_committed=1
rm -rf -- /usr/local/share/cockpit/workspace_templates.v* /usr/local/share/cockpit/workspace_templates.legacy.*.old /usr/local/libexec/guacamole-workspace-helper*.old
find /usr/local/libexec -maxdepth 1 -type d -name 'guacamole-workspace-release.v*' ! -path "$release_version" -exec rm -rf -- {} +
find /usr/local/libexec -maxdepth 1 -type d -name 'guacamole-workspace.v*' -exec rm -rf -- {} +
find /usr/local/libexec -maxdepth 1 -type d -name 'guacamole-workspace-package.v*' -exec rm -rf -- {} +
'@
    $installScript = $installScript.Replace('__PACKAGE_SOURCE__', $packageSource)
    $installScript = $installScript.Replace('__HELPER_SOURCE__', $helperSource)
    $installScript = $installScript.Replace('__INITIALIZER_SOURCE__', $initializerSource)
    $installScript = $installScript.Replace('__COMPOSE_SOURCE__', $composeSource)
    $installScript = $installScript.Replace('__XML_SOURCE__', $xmlSource)
    $installScript = $installScript.Replace('__TPM_STATE_SOURCE__', $tpmStateSource)
    $installScript = $installScript.Replace('__VERSION__', $CockpitWorkspacePackageVersion)
    $installScript = (($installScript -split [Environment]::NewLine |
        ForEach-Object { $_.Trim() } |
        Where-Object { $_ }) -join ' ')
    $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($installScript))
    $result = Invoke-WslCommand -Command "printf '%s' '$encoded' | base64 -d | sh"
    if ($result.Output) { Write-Host $result.Output }

    $modeCheck = Invoke-WslCommand -Command "stat -c '%U:%G:%a %n' '$CockpitWorkspaceTargetPath/manifest.json' '$CockpitWorkspaceTargetPath/index.html' '$CockpitWorkspaceTargetPath/workspace-templates.js' '$CockpitWorkspaceTargetPath/workspace-templates.css' '$WorkspaceHelperTargetPath'"
    if ($modeCheck.Output) { Write-Host $modeCheck.Output }
    Write-Host 'COCKPIT_WORKSPACES_INSTALL_OK'
}

function Configure-Cockpit {
    Ensure-SwtpmLocalCaOwnership
    $dropInScript = @'
set -eu
if getent passwd libvirtdbus >/dev/null 2>&1; then
    usermod -aG libvirt,kvm libvirtdbus
fi
# libvirt-dbus installs a system-bus policy after dbus may already be running.
# Reload it so Cockpit Machines can activate org.libvirt without a reboot.
systemctl reload dbus
install -d -m 0755 /etc/systemd/system/cockpit.socket.d
printf '%s\n' '[Socket]' 'ListenStream=' 'ListenStream=127.0.0.1:9090' > /etc/systemd/system/cockpit.socket.d/listen.conf
systemctl daemon-reload
systemctl enable cockpit.socket
systemctl restart cockpit.socket
'@
    $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($dropInScript))
    Invoke-WslCommand -Command "printf '%s' '$encoded' | base64 -d | sh" | Out-Null

    $dbusProbe = Invoke-WslCommand -Command "busctl call org.libvirt /org/libvirt/QEMU org.libvirt.Connect ListDomains u 0"
    if ($dbusProbe.Output -notmatch '^ao\s') {
        throw "COCKPIT_LIBVIRT_DBUS_FAILED: org.libvirt did not return a domain list: $($dbusProbe.Output)"
    }

    $ss = Invoke-WslCommand -Command 'ss -ltnp'
    Write-Host $ss.Output
    $cockpitListeners = @($ss.Output -split [Environment]::NewLine | Where-Object { $_ -match ':9090\b' })
    if ($cockpitListeners.Count -ne 1 -or $cockpitListeners[0] -notmatch '127\.0\.0\.1:9090\b') {
        throw "COCKPIT_LOCAL_ONLY_FAILED: expected exactly 127.0.0.1:9090, observed: $($cockpitListeners -join ' | ')"
    }
    if ($cockpitListeners -match '0\.0\.0\.0:9090|\[::\]:9090') {
        throw 'COCKPIT_LOCAL_ONLY_FAILED: wildcard Cockpit listener detected.'
    }

    $tcpCheck = Test-NetConnection -ComputerName '127.0.0.1' -Port 9090 -InformationLevel Quiet
    if (-not $tcpCheck) {
        throw 'COCKPIT_LOCAL_ONLY_FAILED: Windows could not connect to 127.0.0.1:9090.'
    }

    $responseStatusCode = 0
    $oldCallback = [System.Net.ServicePointManager]::ServerCertificateValidationCallback
    try {
        try {
            if ((Get-Command Invoke-WebRequest).Parameters.ContainsKey('SkipCertificateCheck')) {
                $response = Invoke-WebRequest -Uri $CockpitUrl -SkipCertificateCheck -UseBasicParsing -TimeoutSec $TimeoutSeconds
            } else {
                [System.Net.ServicePointManager]::ServerCertificateValidationCallback = { $true }
                $response = Invoke-WebRequest -Uri $CockpitUrl -UseBasicParsing -TimeoutSec $TimeoutSeconds
            }
            $responseStatusCode = [int]$response.StatusCode
        } catch {
            Write-Warning "PowerShell Invoke-WebRequest could not complete the Cockpit TLS response; using Windows curl.exe for the same localhost HTTP check. Error: $($_.Exception.Message)"
            $curlStatus = (& curl.exe --insecure --silent --show-error --max-time $TimeoutSeconds --output NUL --write-out '%{http_code}' $CockpitUrl 2>&1 | Out-String).Trim()
            if ($LASTEXITCODE -ne 0 -or $curlStatus -notmatch '^2\d\d$') {
                throw "COCKPIT_LOCAL_ONLY_FAILED: curl.exe returned status '$curlStatus' for $CockpitUrl."
            }
            $responseStatusCode = [int]$curlStatus
        }
    } finally {
        [System.Net.ServicePointManager]::ServerCertificateValidationCallback = $oldCallback
    }
    if ($responseStatusCode -lt 200 -or $responseStatusCode -ge 500) {
        throw "COCKPIT_LOCAL_ONLY_FAILED: unexpected HTTP status from $CockpitUrl."
    }

    Write-Host "Cockpit local URL: $CockpitUrl"
    Write-Host 'COCKPIT_LOCAL_ONLY_OK'
}

function Get-LibvirtNetworkContract {
    param(
        [Parameter(Mandatory)]
        [string]$XmlText,

        [Parameter(Mandatory)]
        [string]$Source
    )

    try {
        $xml = [xml]$XmlText
    } catch {
        throw "LIBVIRT_NETWORK_XML_INVALID: could not parse ${Source}: $($_.Exception.Message)"
    }

    if ($null -eq $xml.network) {
        throw "LIBVIRT_NETWORK_XML_INVALID: ${Source} has no network root element."
    }

    $dhcpHost = @($xml.network.ip.dhcp.host | Where-Object { $_.name -eq $LibvirtReservedHostName }) | Select-Object -First 1
    [pscustomobject]@{
        Name = [string]$xml.network.name
        Bridge = [string]$xml.network.bridge.name
        ForwardMode = [string]$xml.network.forward.mode
        IpAddress = [string]$xml.network.ip.address
        Netmask = [string]$xml.network.ip.netmask
        DhcpStart = [string]$xml.network.ip.dhcp.range.start
        DhcpEnd = [string]$xml.network.ip.dhcp.range.end
        HostMac = [string]$dhcpHost.mac
        HostName = [string]$dhcpHost.name
        HostIp = [string]$dhcpHost.ip
    }
}

function Assert-LibvirtNetworkContract {
    param(
        [Parameter(Mandatory)]
        [string]$XmlText,

        [Parameter(Mandatory)]
        [string]$Source
    )

    $actual = Get-LibvirtNetworkContract -XmlText $XmlText -Source $Source
    $expected = [ordered]@{
        Name = $LibvirtNetworkName
        Bridge = $LibvirtBridgeName
        ForwardMode = 'nat'
        IpAddress = $LibvirtNetworkGateway
        Netmask = '255.255.255.0'
        DhcpStart = '192.168.250.100'
        DhcpEnd = '192.168.250.254'
        HostMac = $LibvirtReservedMac
        HostName = $LibvirtReservedHostName
        HostIp = $LibvirtReservedIp
    }
    $differences = [System.Collections.Generic.List[string]]::new()
    foreach ($property in $expected.Keys) {
        $expectedValue = [string]$expected[$property]
        $actualValue = [string]$actual.$property
        if ($actualValue -ne $expectedValue) {
            $differences.Add("$property expected='$expectedValue' actual='$actualValue'")
        }
    }
    if ($differences.Count -gt 0) {
        throw "LIBVIRT_NETWORK_CONFLICT: $LibvirtNetworkName differs from the canonical contract in $Source.`n$($differences -join [Environment]::NewLine)"
    }
    return $actual
}

function Assert-LibvirtNetworkHostConflicts {
    param(
        [Parameter(Mandatory)]
        [bool]$NetworkExists
    )

    $bridge = Invoke-WslCommand -Command "ip -o link show dev '$LibvirtBridgeName'" -AllowFailure
    if (-not $NetworkExists -and $bridge.ExitCode -eq 0 -and -not [string]::IsNullOrWhiteSpace($bridge.Output)) {
        throw "LIBVIRT_NETWORK_BRIDGE_CONFLICT: host bridge '$LibvirtBridgeName' already exists while libvirt network '$LibvirtNetworkName' is absent."
    }

    $route = Invoke-WslCommand -Command "ip -o route show '$LibvirtNetworkSubnet'" -AllowFailure
    $foreignRoutes = @($route.Output -split [Environment]::NewLine |
        Where-Object { $_ -and $_ -notmatch ("\bdev\s+" + [regex]::Escape($LibvirtBridgeName) + "\b") })
    if ($foreignRoutes.Count -gt 0) {
        throw "LIBVIRT_NETWORK_SUBNET_CONFLICT: route '$LibvirtNetworkSubnet' is already owned by another interface.`n$($foreignRoutes -join [Environment]::NewLine)"
    }

    $gateway = Invoke-WslCommand -Command "ip -o addr show | grep -F -w '$LibvirtNetworkGateway'" -AllowFailure
    $foreignGateway = @($gateway.Output -split [Environment]::NewLine |
        Where-Object { $_ -and $_ -notmatch ("\b" + [regex]::Escape($LibvirtBridgeName) + "\b") })
    if ($foreignGateway.Count -gt 0) {
        throw "LIBVIRT_NETWORK_GATEWAY_CONFLICT: address '$LibvirtNetworkGateway' is already assigned outside '$LibvirtBridgeName'.`n$($foreignGateway -join [Environment]::NewLine)"
    }
}

function Invoke-LibvirtNetwork {
    if (-not (Test-Path -LiteralPath $LibvirtNetworkXmlPath -PathType Leaf)) {
        throw "LIBVIRT_NETWORK_XML_MISSING: $LibvirtNetworkXmlPath"
    }

    $canonicalXml = Get-Content -Raw -LiteralPath $LibvirtNetworkXmlPath
    Assert-LibvirtNetworkContract -XmlText $canonicalXml -Source $LibvirtNetworkXmlPath | Out-Null

    $listed = Invoke-WslCommand -Command "virsh -c qemu:///system net-list --all --name"
    $networkNames = @($listed.Output -split [Environment]::NewLine |
        ForEach-Object { $_.Trim() } |
        Where-Object { $_ })
    $networkExists = $networkNames -contains $LibvirtNetworkName

    if (-not $networkExists) {
        Assert-LibvirtNetworkHostConflicts -NetworkExists:$false
        Invoke-WslCommand -Command "virsh -c qemu:///system net-define '$WslDeployRoot/libvirt/networks/guac-nat.xml'" | Out-Null
        Write-Host "Defined libvirt network '$LibvirtNetworkName' from the canonical XML."
    } else {
        $dump = Invoke-WslCommand -Command "virsh -c qemu:///system net-dumpxml '$LibvirtNetworkName'"
        Assert-LibvirtNetworkContract -XmlText $dump.Output -Source "virsh net-dumpxml $LibvirtNetworkName" | Out-Null
        Assert-LibvirtNetworkHostConflicts -NetworkExists:$true
    }

    Invoke-WslCommand -Command "virsh -c qemu:///system net-autostart '$LibvirtNetworkName'" | Out-Null
    $networkInfo = Invoke-WslCommand -Command "virsh -c qemu:///system net-info '$LibvirtNetworkName'"
    if ($networkInfo.Output -notmatch '(?m)^Active:\s+yes\s*$') {
        Invoke-WslCommand -Command "virsh -c qemu:///system net-start '$LibvirtNetworkName'" | Out-Null
    }

    $finalInfo = Invoke-WslCommand -Command "virsh -c qemu:///system net-info '$LibvirtNetworkName'"
    if ($finalInfo.Output -notmatch '(?m)^Active:\s+yes\s*$' -or $finalInfo.Output -notmatch '(?m)^Autostart:\s+yes\s*$') {
        throw "LIBVIRT_NETWORK_NOT_READY: expected active/autostart network '$LibvirtNetworkName'.`n$($finalInfo.Output)"
    }
    Write-Host $finalInfo.Output
    Invoke-WslProbe -Label "libvirt DHCP leases ($LibvirtNetworkName)" -Command "virsh -c qemu:///system net-dhcp-leases '$LibvirtNetworkName'" | Out-Null
    Write-Host 'LIBVIRT_NETWORK_OK'
}

function Get-LibvirtStoragePoolTarget {
    param(
        [Parameter(Mandatory)]
        [string]$XmlText
    )

    try {
        $xml = [xml]$XmlText
    } catch {
        throw "LIBVIRT_STORAGE_POOL_XML_INVALID: could not parse pool XML: $($_.Exception.Message)"
    }
    if ($null -eq $xml.pool) {
        throw 'LIBVIRT_STORAGE_POOL_XML_INVALID: pool XML has no pool root element.'
    }
    [pscustomobject]@{
        Type = [string]$xml.pool.type
        Target = [string]$xml.pool.target.path
    }
}

function Assert-LibvirtStoragePoolContract {
    param(
        [Parameter(Mandatory)]
        [psobject]$PoolContract
    )

    if ([string]$PoolContract.Type -ne 'dir' -or [string]$PoolContract.Target -ne $LibvirtStoragePoolTarget) {
        throw "LIBVIRT_STORAGE_POOL_CONFLICT: '$LibvirtStoragePoolName' has type='$($PoolContract.Type)' target='$($PoolContract.Target)', expected type='dir' target='$LibvirtStoragePoolTarget'. The existing pool was not changed."
    }
    return $PoolContract
}

function Invoke-LibvirtStorage {
    Invoke-WslCommand -Command "install -d -m 0755 '$LibvirtStoragePoolTarget'" | Out-Null

    $listed = Invoke-WslCommand -Command "virsh -c qemu:///system pool-list --all --name"
    $poolNames = @($listed.Output -split [Environment]::NewLine |
        ForEach-Object { $_.Trim() } |
        Where-Object { $_ })
    $poolExists = $poolNames -contains $LibvirtStoragePoolName
    if (-not $poolExists) {
        Invoke-WslCommand -Command "virsh -c qemu:///system pool-define-as '$LibvirtStoragePoolName' dir --target '$LibvirtStoragePoolTarget'" | Out-Null
        Write-Host "Defined libvirt storage pool '$LibvirtStoragePoolName'."
    } else {
        $dump = Invoke-WslCommand -Command "virsh -c qemu:///system pool-dumpxml '$LibvirtStoragePoolName'"
        $poolContract = Get-LibvirtStoragePoolTarget -XmlText $dump.Output
        Assert-LibvirtStoragePoolContract -PoolContract $poolContract | Out-Null
    }

    Invoke-WslCommand -Command "virsh -c qemu:///system pool-autostart '$LibvirtStoragePoolName'" | Out-Null
    $poolInfo = Invoke-WslCommand -Command "virsh -c qemu:///system pool-info '$LibvirtStoragePoolName'"
    if ($poolInfo.Output -notmatch '(?m)^State:\s+running\s*$') {
        Invoke-WslCommand -Command "virsh -c qemu:///system pool-start '$LibvirtStoragePoolName'" | Out-Null
    }

    $finalInfo = Invoke-WslCommand -Command "virsh -c qemu:///system pool-info '$LibvirtStoragePoolName'"
    if ($finalInfo.Output -notmatch '(?m)^State:\s+running\s*$' -or $finalInfo.Output -notmatch '(?m)^Autostart:\s+yes\s*$') {
        throw "LIBVIRT_STORAGE_POOL_NOT_READY: expected running/autostart pool '$LibvirtStoragePoolName'.`n$($finalInfo.Output)"
    }
    Write-Host $finalInfo.Output
    Write-Host "H-backed libvirt pool target: $LibvirtStoragePoolTarget"
    Write-Host 'LIBVIRT_STORAGE_OK'
}

function Assert-ComposeBridgeEvidence {
    param(
        [Parameter(Mandatory)]
        [string]$NetworkName,

        [Parameter(Mandatory)]
        [string]$NetworkId,

        [Parameter(Mandatory)]
        [string]$Subnet,

        [Parameter(Mandatory)]
        [string]$Gateway,

        [Parameter(Mandatory)]
        [string]$Bridge,

        [Parameter(Mandatory)]
        [string]$BridgeSource,

        [Parameter(Mandatory)]
        [int]$LinkExitCode,

        [Parameter(Mandatory)]
        [AllowEmptyString()]
        [string]$LinkOutput,

        [Parameter(Mandatory)]
        [int]$AddressExitCode,

        [Parameter(Mandatory)]
        [AllowEmptyString()]
        [string]$AddressOutput,

        [Parameter(Mandatory)]
        [int]$RouteExitCode,

        [Parameter(Mandatory)]
        [AllowEmptyString()]
        [string]$RouteOutput
    )

    if ($Bridge -notmatch '^[A-Za-z0-9_.-]+$') {
        throw "COMPOSE_BRIDGE_INVALID: Docker bridge '$Bridge' contains unsupported characters."
    }
    if ($Subnet -notmatch '^\d{1,3}(?:\.\d{1,3}){3}/\d{1,2}$' -or $Gateway -notmatch '^\d{1,3}(?:\.\d{1,3}){3}$') {
        throw "COMPOSE_NETWORK_METADATA_INVALID: Docker network '$NetworkName' returned subnet='$Subnet' gateway='$Gateway'."
    }
    if ($Subnet -eq $LibvirtNetworkSubnet) {
        throw "COMPOSE_NETWORK_SUBNET_CONFLICT: Docker network '$NetworkName' uses the libvirt subnet '$LibvirtNetworkSubnet'."
    }
    if ($LinkExitCode -ne 0 -or [string]::IsNullOrWhiteSpace($LinkOutput)) {
        throw "COMPOSE_BRIDGE_NOT_FOUND: Docker network '$NetworkName' selected bridge '$Bridge', but 'ip -o link show dev $Bridge' found no interface."
    }
    if ($LinkOutput -notmatch '(?m)\bstate\s+UP\b') {
        throw "COMPOSE_BRIDGE_DOWN: Docker bridge '$Bridge' exists but is not up.`n$LinkOutput"
    }

    $networkIdPrefix = $NetworkId.Substring(0, [Math]::Min(12, $NetworkId.Length))
    if ($BridgeSource -eq 'derived' -and $Bridge -ne "br-$networkIdPrefix") {
        throw "COMPOSE_BRIDGE_MISMATCH: Docker network ID '$NetworkId' requires derived bridge 'br-$networkIdPrefix', but '$Bridge' was selected."
    }
    $prefixLength = $Subnet.Substring($Subnet.IndexOf('/') + 1)
    $gatewayCidr = "$Gateway/$prefixLength"
    if ($AddressExitCode -ne 0 -or $AddressOutput -notmatch ("(?m)\binet\s+" + [regex]::Escape($gatewayCidr) + "\b")) {
        throw "COMPOSE_BRIDGE_MISMATCH: Docker network '$NetworkName' reports gateway '$gatewayCidr', but bridge '$Bridge' has no matching address.`n$AddressOutput"
    }
    if ($RouteExitCode -ne 0 -or $RouteOutput -notmatch ("(?m)^" + [regex]::Escape($Subnet) + "\s+.*\bdev\s+" + [regex]::Escape($Bridge) + "\b")) {
        throw "COMPOSE_BRIDGE_MISMATCH: Docker network '$NetworkName' reports subnet '$Subnet', but WSL has no route for that subnet through bridge '$Bridge'.`n$RouteOutput"
    }
    [pscustomobject]@{
        NetworkName = $NetworkName
        NetworkId = $NetworkId
        Subnet = $Subnet
        Gateway = $Gateway
        Bridge = $Bridge
        BridgeSource = $BridgeSource
    }
}

function Get-ComposeNetworkEvidence {
    $composeScript = @'
set -eu
network='__COMPOSE_NETWORK__'
network_id=$(docker network inspect "$network" --format '{{.Id}}')
subnet=$(docker network inspect "$network" --format '{{(index .IPAM.Config 0).Subnet}}')
gateway=$(docker network inspect "$network" --format '{{(index .IPAM.Config 0).Gateway}}')
bridge=$(docker network inspect "$network" --format '{{index .Options "com.docker.network.bridge.name"}}' 2>/dev/null || true)
bridge_source='metadata'
if test -z "$bridge" || test "$bridge" = '<no value>'; then
    prefix=$(printf '%s' "$network_id" | cut -c1-12)
    bridge=$(ip -o link show | awk -F': ' -v wanted="br-$prefix" '$2 == wanted { print wanted; exit }')
    bridge_source='derived'
fi
test -n "$network_id"
test -n "$subnet"
test -n "$gateway"
if test -z "$bridge"; then
    echo "COMPOSE_BRIDGE_NOT_FOUND: Docker network '$network' has no bridge metadata and no matching br-<network-id> interface." >&2
    exit 41
fi
case "$bridge" in *[!A-Za-z0-9_.-]*) echo "invalid Docker bridge name: $bridge" >&2; exit 2;; esac
printf 'network=%s\nid=%s\nsubnet=%s\ngateway=%s\nbridge=%s\nbridge_source=%s\n' "$network" "$network_id" "$subnet" "$gateway" "$bridge" "$bridge_source"
'@
    $composeScript = $composeScript.Replace('__COMPOSE_NETWORK__', $ComposeNetworkName)
    $composeScript = (($composeScript -split [Environment]::NewLine |
        ForEach-Object { $_.TrimEnd() } |
        Where-Object { $_ }) -join ' ')
    $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($composeScript))
    $result = Invoke-WslCommand -Command "printf '%s' '$encoded' | base64 -d | sh"
    $values = @{}
    foreach ($line in ($result.Output -split [Environment]::NewLine)) {
        if ($line -match '^([^=]+)=(.*)$') {
            $values[$matches[1]] = $matches[2]
        }
    }
    foreach ($key in @('network', 'id', 'subnet', 'gateway', 'bridge', 'bridge_source')) {
        if ([string]::IsNullOrWhiteSpace([string]$values[$key])) {
            throw "COMPOSE_NETWORK_DISCOVERY_FAILED: missing '$key' in Docker network inspection output."
        }
    }
    $bridge = [string]$values['bridge']
    $subnet = [string]$values['subnet']
    $gateway = [string]$values['gateway']
    $networkId = [string]$values['id']
    $linkProbe = Invoke-WslCommand -Command "ip -o link show dev '$bridge'" -AllowFailure
    $addressProbe = Invoke-WslCommand -Command "ip -o -4 addr show dev '$bridge'" -AllowFailure
    $routeProbe = Invoke-WslCommand -Command "ip -o route show '$subnet'" -AllowFailure
    Assert-ComposeBridgeEvidence -NetworkName $ComposeNetworkName -NetworkId $networkId -Subnet $subnet -Gateway $gateway -Bridge $bridge -BridgeSource ([string]$values['bridge_source']) -LinkExitCode $linkProbe.ExitCode -LinkOutput $linkProbe.Output -AddressExitCode $addressProbe.ExitCode -AddressOutput $addressProbe.Output -RouteExitCode $routeProbe.ExitCode -RouteOutput $routeProbe.Output | Out-Null
    [pscustomobject]@{
        Name = [string]$values['network']
        Id = $networkId
        Subnet = $subnet
        Gateway = $gateway
        Bridge = $bridge
        BridgeSource = [string]$values['bridge_source']
        Link = $linkProbe.Output
        Address = $addressProbe.Output
        Route = $routeProbe.Output
    }
}

function Invoke-ConnectGuacamole {
    $networkInfo = Invoke-WslCommand -Command "virsh -c qemu:///system net-info '$LibvirtNetworkName'"
    if ($networkInfo.Output -notmatch '(?m)^Active:\s+yes\s*$') {
        throw "LIBVIRT_NETWORK_NOT_READY: '$LibvirtNetworkName' must be active before connecting Docker to the guest network. Run '.\libvirt.ps1 network'."
    }

    $compose = Get-ComposeNetworkEvidence
    Write-Host "Compose network: $($compose.Name) subnet=$($compose.Subnet) bridge=$($compose.Bridge)"

    $routeScript = @'
set -eu
docker_bridge='__DOCKER_BRIDGE__'
libvirt_bridge='__LIBVIRT_BRIDGE__'
libvirt_subnet='__LIBVIRT_SUBNET__'
sysctl -w net.ipv4.ip_forward=1
ensure_rule() {
    if ! iptables -C FORWARD "$@" >/dev/null 2>&1; then
        iptables -I FORWARD "$@"
    fi
}
ensure_rule -i "$docker_bridge" -o "$libvirt_bridge" -p tcp -d "$libvirt_subnet" --dport 3389 -j ACCEPT
ensure_rule -i "$libvirt_bridge" -o "$docker_bridge" -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -C FORWARD -i "$docker_bridge" -o "$libvirt_bridge" -p tcp -d "$libvirt_subnet" --dport 3389 -j ACCEPT
iptables -C FORWARD -i "$libvirt_bridge" -o "$docker_bridge" -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
printf 'docker_bridge=%s\nlibvirt_bridge=%s\nlibvirt_subnet=%s\n' "$docker_bridge" "$libvirt_bridge" "$libvirt_subnet"
'@
    foreach ($replacement in @{
        '__DOCKER_BRIDGE__' = $compose.Bridge
        '__LIBVIRT_BRIDGE__' = $LibvirtBridgeName
        '__LIBVIRT_SUBNET__' = $LibvirtNetworkSubnet
    }.GetEnumerator()) {
        $routeScript = $routeScript.Replace($replacement.Key, $replacement.Value)
    }
    $routeScript = (($routeScript -split [Environment]::NewLine |
        ForEach-Object { $_.TrimEnd() } |
        Where-Object { $_ }) -join ' ')
    $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($routeScript))
    Invoke-WslCommand -Command "printf '%s' '$encoded' | base64 -d | sh" | Out-Null

    Write-Host "Docker-to-$LibvirtBridgeName forwarding rules are active and idempotent."
    Write-Host 'DOCKER_TO_GUEST_RDP_TEST_READY: run the read-only Compose-network probe after Windows 11 is running.'
    Write-Host 'LIBVIRT_GUACAMOLE_ROUTE_OK'
}

function Invoke-LegacyOwnerGuard {
    $guardScript = @'
set -eu
for unit in guacamole-vm-windows11.service guacamole-vm-windows11-tpm.service; do
  state=$(systemctl show -p ActiveState --value "$unit" 2>/dev/null || true)
  case "$state" in
    active|activating|deactivating)
      printf 'legacy_unit=%s state=%s\n' "$unit" "$state"
      exit 1
      ;;
  esac
done
legacy_matches=$(pgrep -af '[q]emu-system-x86_64.*-name guacamole-vm-windows11' || true)
if test -n "$legacy_matches"; then
  printf 'legacy_qemu_matches=%s\n' "$legacy_matches"
  exit 1
fi
'@
    $guardScript = (($guardScript -split [Environment]::NewLine |
        ForEach-Object { $_.Trim() } |
        Where-Object { $_ }) -join ' ')
    $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($guardScript))
    $result = Invoke-WslCommand -Command "printf '%s' '$encoded' | base64 -d | sh" -AllowFailure
    if ($result.ExitCode -ne 0) {
        throw "LEGACY_QEMU_OWNER_BLOCKED: legacy QEMU/TPM ownership is still active. $($result.Output)"
    }
}

function Get-LibvirtDomainXml {
    param([Parameter(Mandatory)][string]$Name)

    $result = Invoke-WslCommand -Command "virsh -c qemu:///system dumpxml '$Name'" -AllowFailure
    if ($result.ExitCode -ne 0) {
        throw "LIBVIRT_DOMAIN_MISSING: '$Name' is not defined on qemu:///system."
    }
    return $result.Output
}

function Assert-LibvirtDomainReadyForStart {
    param([Parameter(Mandatory)][string]$Name)

    $info = Invoke-WslCommand -Command "virsh -c qemu:///system dominfo '$Name'"
    if ($info.Output -notmatch '(?m)^Persistent:\s+yes\s*$') {
        throw "LIBVIRT_DOMAIN_NOT_PERSISTENT: '$Name' must be a persistent libvirt domain."
    }

    $xmlText = Get-LibvirtDomainXml -Name $Name
    try {
        $xml = [xml]$xmlText
    } catch {
        throw "LIBVIRT_DOMAIN_XML_INVALID: could not parse '$Name': $($_.Exception.Message)"
    }
    $cdroms = @($xml.domain.devices.disk | Where-Object { [string]$_.device -eq 'cdrom' })
    if ($cdroms.Count -gt 0 -or $xmlText -match '(?i)<source\s+file=[^>]*\.iso(?:[''\"\s>])') {
        throw "LIBVIRT_DOMAIN_INSTALLER_MEDIA_PRESENT: '$Name' still has installer ISO media; refusing to start it through recovery."
    }
    return $xmlText
}

function Get-LibvirtDomainState {
    param([Parameter(Mandatory)][string]$Name)

    $result = Invoke-WslCommand -Command "virsh -c qemu:///system domstate '$Name'" -AllowFailure
    if ($result.ExitCode -ne 0) {
        throw "LIBVIRT_DOMAIN_MISSING: '$Name' is not defined on qemu:///system."
    }
    return $result.Output.Trim()
}

function Wait-ForLibvirtDomainState {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][string]$ExpectedState,
        [Parameter(Mandatory)][int]$Seconds
    )

    $deadline = [DateTime]::UtcNow.AddSeconds($Seconds)
    do {
        if ((Get-LibvirtDomainState -Name $Name) -eq $ExpectedState) {
            return
        }
        Start-Sleep -Milliseconds 500
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "LIBVIRT_DOMAIN_STATE_TIMEOUT: '$Name' did not reach '$ExpectedState' within $Seconds seconds."
}

function Start-LibvirtTpmOwner {
    Invoke-LegacyOwnerGuard
    Invoke-WslCommand -Command "systemctl start '$TpmUnitName'" | Out-Null
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        $socket = Invoke-WslCommand -Command "test -S '$TpmSocketPath'" -AllowFailure
        if ($socket.ExitCode -eq 0) {
            return
        }
        Start-Sleep -Milliseconds 500
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "LIBVIRT_TPM_SOCKET_TIMEOUT: '$TpmSocketPath' was not ready within $TimeoutSeconds seconds."
}

function Stop-LibvirtTpmOwner {
    Invoke-WslCommand -Command "systemctl stop '$TpmUnitName'" -AllowFailure | Out-Null
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        $socket = Invoke-WslCommand -Command "test -S '$TpmSocketPath'" -AllowFailure
        if ($socket.ExitCode -ne 0) {
            return
        }
        Start-Sleep -Milliseconds 500
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "LIBVIRT_TPM_SOCKET_TIMEOUT: '$TpmSocketPath' remained after stopping the dedicated TPM owner."
}

function Invoke-LibvirtStart {
    if (-not (Test-Path -LiteralPath $CutoverMarkerPath -PathType Leaf)) {
        throw "LIBVIRT_CUTOVER_MARKER_REQUIRED: '$CutoverMarkerPath' is absent; recovery will not start a second Windows owner."
    }

    Invoke-LibvirtNetwork
    Invoke-LibvirtStorage
    Invoke-LegacyOwnerGuard
    $domainXml = Assert-LibvirtDomainReadyForStart -Name $DomainName
    Start-LibvirtTpmOwner

    $state = Get-LibvirtDomainState -Name $DomainName
    if ($state -eq 'shut off') {
        Invoke-WslCommand -Command "virsh -c qemu:///system start '$DomainName'" | Out-Null
    } elseif ($state -notmatch '^(running|blocked|paused)$') {
        throw "LIBVIRT_DOMAIN_NOT_STARTABLE: '$DomainName' is in state '$state'."
    }

    $finalState = Get-LibvirtDomainState -Name $DomainName
    if ($finalState -notmatch '^(running|blocked|paused)$') {
        throw "LIBVIRT_DOMAIN_START_FAILED: '$DomainName' is in state '$finalState'."
    }
    Write-Host "Libvirt domain '$DomainName' is $finalState."
    Write-Host 'LIBVIRT_DOMAIN_WINDOWS11_ACTIVE'
    Write-Host 'LIBVIRT_START_OK'
}

function Invoke-LibvirtStop {
    $state = Get-LibvirtDomainState -Name $DomainName
    if ($state -match '^(running|blocked|paused|in shutdown|pmsuspended)$') {
        Invoke-WslCommand -Command "virsh -c qemu:///system shutdown '$DomainName'" | Out-Null
        try {
            Wait-ForLibvirtDomainState -Name $DomainName -ExpectedState 'shut off' -Seconds $TimeoutSeconds
        } catch {
            Write-Warning $_.Exception.Message
            Invoke-WslCommand -Command "virsh -c qemu:///system destroy '$DomainName'" | Out-Null
            Wait-ForLibvirtDomainState -Name $DomainName -ExpectedState 'shut off' -Seconds $TimeoutSeconds
        }
    } elseif ($state -ne 'shut off') {
        throw "LIBVIRT_DOMAIN_STOP_UNSAFE: '$DomainName' is in unexpected state '$state'."
    }

    Stop-LibvirtTpmOwner
    Invoke-LegacyOwnerGuard
    Write-Host "Libvirt domain '$DomainName' is shut off and the dedicated TPM owner is stopped."
    Write-Host 'LIBVIRT_STOP_OK'
}

function Show-Status {
    Write-Host "Repository: $RepoRoot"
    Write-Host "H-backed WSL VHDX: $(Test-Path -LiteralPath $VhdxPath -PathType Leaf) ($VhdxPath)"
    Write-Host "Cockpit local URL: $CockpitUrl"
    Write-Host "Libvirt cutover marker: $(Test-Path -LiteralPath $CutoverMarkerPath -PathType Leaf) ($CutoverMarkerPath)"
    Invoke-WslProbe -Label 'systemd state' -Command 'systemctl is-system-running' | Out-Null
    Invoke-WslProbe -Label 'Cockpit listener' -Command 'ss -ltnp' | Out-Null
    Invoke-WslProbe -Label 'libvirt system URI' -Command 'virsh -c qemu:///system uri' | Out-Null
    Invoke-WslProbe -Label 'libvirt networks' -Command 'virsh -c qemu:///system net-list --all' | Out-Null
    Invoke-WslProbe -Label 'libvirt storage pools' -Command 'virsh -c qemu:///system pool-list --all' | Out-Null

    $domain = Invoke-WslProbe -Label "libvirt domain ($DomainName)" -Command "virsh -c qemu:///system domstate '$DomainName'"
    if ($domain.ExitCode -eq 0 -and $domain.Output -match '^(running|blocked|paused|in shutdown|shut off|crashed|pmsuspended)') {
        if ($domain.Output -match '^(running|blocked|paused)') {
            Write-Host 'LIBVIRT_DOMAIN_WINDOWS11_ACTIVE'
        } else {
            Write-Host 'LIBVIRT_DOMAIN_WINDOWS11_INACTIVE'
        }
        Invoke-WslProbe -Label "domain disks ($DomainName)" -Command "virsh -c qemu:///system domblklist '$DomainName'" | Out-Null
        $domainXml = Invoke-WslProbe -Label "domain XML ($DomainName)" -Command "virsh -c qemu:///system dumpxml '$DomainName'"
        if ($domainXml.Output -match '(?i)<disk\s+[^>]*device=[''\"]cdrom[''\"]|<source\s+file=[^>]*\.iso(?:[''\"\s>])') {
            Write-Host 'LIBVIRT_DOMAIN_INSTALLER_MEDIA_PRESENT'
        } else {
            Write-Host 'LIBVIRT_DOMAIN_INSTALLER_MEDIA_ABSENT'
        }
    } else {
        Write-Host 'LIBVIRT_DOMAIN_WINDOWS11_MISSING'
    }

    Invoke-TpmOwnerStatus
    Invoke-LegacyOwnerStatus
    Invoke-ComposeStatus
}

function Protect-Checkpoint {
    param([Parameter(Mandatory)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        throw "CHECKPOINT_NOT_FOUND: cannot protect missing checkpoint '$Path'."
    }

    $currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $rules = @(
        ('{0}:(OI)(CI)(F)' -f $currentUser),
        '*S-1-5-18:(OI)(CI)(F)',
        '*S-1-5-32-544:(OI)(CI)(F)'
    )
    & icacls.exe $Path /inheritance:r /grant:r @rules /C | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "CHECKPOINT_ACL_FAILED: could not restrict '$Path'."
    }
    foreach ($item in @(Get-ChildItem -LiteralPath $Path -Recurse -Force)) {
        & icacls.exe $item.FullName /inheritance:e /C | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "CHECKPOINT_ACL_FAILED: could not enable inherited restricted ACL on '$($item.FullName)'."
        }
    }
}

function Write-CheckpointText {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][AllowEmptyString()][string]$Text
    )

    $parent = Split-Path -Parent $Path
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    $utf8NoBom = New-Object -TypeName System.Text.UTF8Encoding -ArgumentList $false
    [System.IO.File]::WriteAllText($Path, $Text, $utf8NoBom)
}

function Invoke-LegacyCheckpointStop {
    if (-not (Test-Path -LiteralPath $LegacyWindowsScriptPath -PathType Leaf)) {
        throw "LEGACY_STOP_WRAPPER_MISSING: expected '$LegacyWindowsScriptPath'."
    }

    Write-Host 'Stopping the legacy Windows 11 owner through the explicit -AllowLegacyQemuOwner wrapper.'
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $LegacyWindowsScriptPath -Action stop -AllowLegacyQemuOwner
    if ($LASTEXITCODE -ne 0) {
        throw "LEGACY_STOP_FAILED: windows11.ps1 stop returned exit code $LASTEXITCODE."
    }

    $ownerProbeScript = @'
set -eu
qemu_state=$(systemctl is-active guacamole-vm-windows11.service 2>/dev/null || true)
tpm_state=$(systemctl is-active guacamole-vm-windows11-tpm.service 2>/dev/null || true)
qemu_matches=$(pgrep -af '[q]emu-system-x86_64.*windows11' || true)
socket_exists=0
if test -S '/run/guacamole-vm-windows11/swtpm.sock'; then socket_exists=1; fi
printf 'qemu_unit=%s\ntpm_unit=%s\nsocket_exists=%s\nqemu_matches=%s\n' "$qemu_state" "$tpm_state" "$socket_exists" "$qemu_matches"
test "$qemu_state" != active
test "$qemu_state" != activating
test "$qemu_state" != deactivating
test "$tpm_state" != active
test "$tpm_state" != activating
test "$tpm_state" != deactivating
test "$socket_exists" = 0
test -z "$qemu_matches"
'@
    $ownerProbeScript = (($ownerProbeScript -split [Environment]::NewLine |
        ForEach-Object { $_.Trim() } |
        Where-Object { $_ }) -join ' ')
    $encodedOwnerProbe = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($ownerProbeScript))
    $probe = Invoke-WslCommand -Command "printf '%s' '$encodedOwnerProbe' | base64 -d | sh" -AllowFailure
    Write-Host $probe.Output
    if ($probe.ExitCode -ne 0) {
        throw ("LEGACY_OWNER_STILL_ACTIVE: both legacy units and the qemu process must be inactive before checkpointing. {0}" -f $probe.Output)
    }
    return $probe.Output
}

function Invoke-CheckpointDatabaseExport {
    param(
        [Parameter(Mandatory)][string]$CheckpointWslPath
    )

    $composeCommand = "docker compose --project-directory $WslDeployRoot --file $WslDeployRoot/compose.yaml"
    $safeTables = @(
        'guacamole_connection',
        'guacamole_connection_group',
        'guacamole_connection_group_permission',
        'guacamole_connection_permission',
        'guacamole_entity',
        'guacamole_system_permission',
        'guacamole_user_group',
        'guacamole_user_group_member',
        'guacamole_user_group_permission',
        'guacamole_user_permission'
    )
    $safeTableArguments = ($safeTables | ForEach-Object { "--table=$($_)" }) -join ' '
    $connectionQuery = @'
select c.connection_id, c.connection_name, c.protocol,
       coalesce(max(case when p.parameter_name = 'hostname' then p.parameter_value end), '') as hostname,
       coalesce(max(case when p.parameter_name = 'port' then p.parameter_value end), '') as port
from guacamole_connection c
left join guacamole_connection_parameter p on p.connection_id = c.connection_id
group by c.connection_id, c.connection_name, c.protocol
order by c.connection_id;
'@
    $permissionQuery = @'
select 'connection' as permission_scope, p.entity_id, e.name as entity_name,
       p.connection_id, c.connection_name, '' as affected_id, p.permission::text as permission
from guacamole_connection_permission p
join guacamole_entity e on e.entity_id = p.entity_id
join guacamole_connection c on c.connection_id = p.connection_id
union all
select 'user' as permission_scope, p.entity_id, e.name as entity_name,
       null, '', p.affected_user_id::text, p.permission::text
from guacamole_user_permission p
join guacamole_entity e on e.entity_id = p.entity_id
union all
select 'group' as permission_scope, p.entity_id, e.name as entity_name,
       null, '', p.affected_user_group_id::text, p.permission::text
from guacamole_user_group_permission p
join guacamole_entity e on e.entity_id = p.entity_id
order by permission_scope, entity_id, connection_id nulls first, affected_id;
'@
    $entityQuery = @'
select entity_id, name, type::text as type
from guacamole_entity
order by entity_id;
'@
    $connectionQueryB64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($connectionQuery))
    $permissionQueryB64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($permissionQuery))
    $entityQueryB64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($entityQuery))
    $dbScript = @'
set -eu
checkpoint='__CHECKPOINT__'
compose='__COMPOSE__'
mkdir -p "$checkpoint"
$compose exec -T postgres sh -lc 'PGPASSWORD="$(cat /run/secrets/postgres_password)" pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --schema-only --no-owner --no-privileges' > "$checkpoint/guacamole-schema.sql"
$compose exec -T postgres sh -lc 'PGPASSWORD="$(cat /run/secrets/postgres_password)" pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --data-only --no-owner --no-privileges __SAFE_TABLES__' > "$checkpoint/guacamole-config-redacted.sql"
cat "$checkpoint/guacamole-schema.sql" "$checkpoint/guacamole-config-redacted.sql" > "$checkpoint/guacamole-db-redacted.sql"
printf '%s' '__CONNECTION_QUERY__' | base64 -d | $compose exec -T postgres sh -lc 'PGPASSWORD="$(cat /run/secrets/postgres_password)" psql -X -q -U "$POSTGRES_USER" -d "$POSTGRES_DB" --csv --pset footer=off' > "$checkpoint/guacamole-connections.csv"
printf '%s' '__PERMISSION_QUERY__' | base64 -d | $compose exec -T postgres sh -lc 'PGPASSWORD="$(cat /run/secrets/postgres_password)" psql -X -q -U "$POSTGRES_USER" -d "$POSTGRES_DB" --csv --pset footer=off' > "$checkpoint/guacamole-permissions.csv"
printf '%s' '__ENTITY_QUERY__' | base64 -d | $compose exec -T postgres sh -lc 'PGPASSWORD="$(cat /run/secrets/postgres_password)" psql -X -q -U "$POSTGRES_USER" -d "$POSTGRES_DB" --csv --pset footer=off' > "$checkpoint/guacamole-entities.csv"
printf '%s\n' 'This checkpoint intentionally excludes guacamole_user password hashes/salts and every guacamole_connection_parameter value, including the managed Windows username/password. Re-enter authentication and secret connection parameters during restore; credential values are permitted only in that live parameter table.' > "$checkpoint/guacamole-redaction.txt"
'@
    $dbScript = $dbScript.Replace('__CHECKPOINT__', $CheckpointWslPath)
    $dbScript = $dbScript.Replace('__COMPOSE__', $composeCommand)
    $dbScript = $dbScript.Replace('__SAFE_TABLES__', $safeTableArguments)
    $dbScript = $dbScript.Replace('__CONNECTION_QUERY__', $connectionQueryB64)
    $dbScript = $dbScript.Replace('__PERMISSION_QUERY__', $permissionQueryB64)
    $dbScript = $dbScript.Replace('__ENTITY_QUERY__', $entityQueryB64)
    $dbScript = (($dbScript -split [Environment]::NewLine |
        ForEach-Object { $_.TrimEnd() } |
        Where-Object { $_ }) -join ' ')
    $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($dbScript))
    Invoke-WslCommand -Command "printf '%s' '$encoded' | base64 -d | sh" | Out-Null
    Write-Host 'PostgreSQL redacted checkpoint export completed.'
}

function Invoke-LibvirtCheckpointExport {
    param(
        [Parameter(Mandatory)][string]$CheckpointPath,
        [Parameter(Mandatory)][string]$DomainName,
        [Parameter(Mandatory)][string]$CheckpointName
    )

    $exportCommands = @(
        [pscustomobject]@{ Name = 'guac-nat.network.xml'; Command = "virsh -c qemu:///system net-dumpxml '$LibvirtNetworkName'" },
        [pscustomobject]@{ Name = 'guacamole-vms.pool.xml'; Command = "virsh -c qemu:///system pool-dumpxml '$LibvirtStoragePoolName'" },
        [pscustomobject]@{ Name = 'windows11.domain.xml'; Command = "virsh -c qemu:///system dumpxml '$DomainName'" },
        [pscustomobject]@{ Name = 'package-manifest.txt'; Command = "dpkg-query -W $($PackageNames -join ' ')" },
        [pscustomobject]@{ Name = 'legacy-qemu.service.txt'; Command = "systemctl cat 'guacamole-vm-windows11.service'" },
        [pscustomobject]@{ Name = 'legacy-tpm.service.txt'; Command = "systemctl cat 'guacamole-vm-windows11-tpm.service'" }
    )

    foreach ($item in $exportCommands) {
        $result = Invoke-WslCommand -Command $item.Command -AllowFailure
        $text = $result.Output
        if ($item.Name -like '*.service.txt') {
            $text = $text -replace '(?i)(password|secret|token|credential|private-key|passphrase)([=:\s][^\r\n\s]*)', '$1=<redacted>'
        }
        if ($result.ExitCode -ne 0) {
            $text = "EXPORT_EXIT_CODE=$($result.ExitCode)" + [Environment]::NewLine + "COMMAND=$($item.Command)" + [Environment]::NewLine + $text
        }
        Write-CheckpointText -Path (Join-Path $CheckpointPath $item.Name) -Text $text
    }

    $stateManifest = @(
        "checkpoint=$CheckpointName",
        "source_nvram=$WslRepoRoot/runtime/vm-windows11/OVMF_VARS_4M.ms.fd",
        "source_tpm=$WslRepoRoot/runtime/vm-windows11/tpm",
        "disk_path=$WslWindowsDiskPath",
        'disk_copy=not-created-by-default; only path, qemu-img info, and SHA-256 are recorded'
    )
    Write-CheckpointText -Path (Join-Path $CheckpointPath 'state-manifest.txt') -Text ($stateManifest -join [Environment]::NewLine)
}

function Write-BackupFailureEvidence {
    param(
        [Parameter(Mandatory)][string]$CheckpointName,
        [Parameter(Mandatory)][string]$ErrorMessage,
        [Parameter(Mandatory)][bool]$PartialRemoved,
        [Parameter()][AllowEmptyString()][string]$CleanupError = ''
    )

    $failureRoot = Join-Path $ScriptRoot 'libvirt\exports\backup-failures'
    $failureName = "windows11-backup-failure-{0}.txt" -f (Get-Date -Format 'yyyyMMdd-HHmmss')
    $failurePath = Join-Path $failureRoot $failureName
    New-Item -ItemType Directory -Force -Path $failureRoot | Out-Null
    Protect-Checkpoint -Path $failureRoot
    $lines = @(
        "checkpoint=$CheckpointName",
        "failed_utc=$([DateTime]::UtcNow.ToString('o'))",
        "partial_removed=$PartialRemoved",
        "error=$ErrorMessage"
    )
    if (-not [string]::IsNullOrWhiteSpace($CleanupError)) {
        $lines += "cleanup_error=$CleanupError"
    }
    Write-CheckpointText -Path $failurePath -Text ($lines -join [Environment]::NewLine)
    Protect-Checkpoint -Path $failureRoot
    return $failurePath
}

function Invoke-Backup {
    Assert-WslAndHStorage
    New-Item -ItemType Directory -Force -Path $BackupRoot | Out-Null
    $checkpointName = "windows11-pre-libvirt-{0}" -f (Get-Date -Format 'yyyyMMdd-HHmmss')
    $checkpointPath = Join-Path $BackupRoot $checkpointName
    $stagingName = ".$checkpointName.incomplete"
    $stagingPath = Join-Path $BackupRoot $stagingName
    $stagingWslPath = "$WslRepoRoot/runtime/backups/$stagingName"
    if (Test-Path -LiteralPath $checkpointPath -PathType Container) {
        throw "CHECKPOINT_ALREADY_EXISTS: '$checkpointPath'."
    }
    if (Test-Path -LiteralPath $stagingPath -PathType Container) {
        throw "CHECKPOINT_STAGING_ALREADY_EXISTS: '$stagingPath'."
    }

    try {
        New-Item -ItemType Directory -Force -Path $stagingPath | Out-Null
        Protect-Checkpoint -Path $stagingPath

        $stopEvidence = Invoke-LegacyCheckpointStop
        Write-CheckpointText -Path (Join-Path $stagingPath 'legacy-owner-stop.txt') -Text (@($stopEvidence) -join [Environment]::NewLine)

        $check = Invoke-WslCommand -Command "qemu-img check -f qcow2 '$WslWindowsDiskPath'"
        Write-CheckpointText -Path (Join-Path $stagingPath 'qemu-img-check.txt') -Text $check.Output
        $info = Invoke-WslCommand -Command "qemu-img info --output=json '$WslWindowsDiskPath'"
        Write-CheckpointText -Path (Join-Path $stagingPath 'qemu-img-info.json') -Text $info.Output
        $hash = Invoke-WslCommand -Command "sha256sum '$WslWindowsDiskPath'"
        $hashValue = (($hash.Output.Trim() -split '\s+')[0])
        Write-CheckpointText -Path (Join-Path $stagingPath 'windows11.qcow2.sha256') -Text $hash.Output
        Write-CheckpointText -Path (Join-Path $stagingPath 'windows11.qcow2.path.txt') -Text ("WSL path: {0}; H-backed source runtime is inside: {1}" -f $WslWindowsDiskPath, $VhdxPath)

        if (-not (Test-Path -LiteralPath $LegacyNVRAMPath -PathType Leaf)) {
            throw "CHECKPOINT_NVRAM_MISSING: '$LegacyNVRAMPath'."
        }
        if (-not (Test-Path -LiteralPath $LegacyTpmStatePath -PathType Container)) {
            throw "CHECKPOINT_TPM_MISSING: '$LegacyTpmStatePath'."
        }
        $checkpointWslScript = @'
set -eu
checkpoint='__CHECKPOINT__'
mkdir -p "$checkpoint/tpm"
cp --reflink=auto '__NVRAM__' "$checkpoint/OVMF_VARS_4M.ms.fd"
cp -a '__TPM__/.' "$checkpoint/tpm/"
(
  cd "$checkpoint"
  find tpm -type f -print0 | sort -z | xargs -0 sha256sum
  sha256sum OVMF_VARS_4M.ms.fd
) > "$checkpoint/state-sha256sums.txt"
'@
        $checkpointWslScript = $checkpointWslScript.Replace('__CHECKPOINT__', $stagingWslPath)
        $checkpointWslScript = $checkpointWslScript.Replace('__NVRAM__', "$WslRepoRoot/runtime/vm-windows11/OVMF_VARS_4M.ms.fd")
        $checkpointWslScript = $checkpointWslScript.Replace('__TPM__', "$WslRepoRoot/runtime/vm-windows11/tpm")
        $checkpointWslScript = (($checkpointWslScript -split [Environment]::NewLine |
            ForEach-Object { $_.Trim() } |
            Where-Object { $_ }) -join ' ')
        $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($checkpointWslScript))
        Invoke-WslCommand -Command "printf '%s' '$encoded' | base64 -d | sh" | Out-Null

        Invoke-CheckpointDatabaseExport -CheckpointWslPath $stagingWslPath
        Invoke-LibvirtCheckpointExport -CheckpointPath $stagingPath -DomainName $DomainName -CheckpointName $checkpointName

        $manifest = [ordered]@{
            checkpoint = $checkpointName
            created_utc = [DateTime]::UtcNow.ToString('o')
            repository = $RepoRoot
            h_backed_wsl_vhdx = $VhdxPath
            legacy_qcow2_wsl_path = $WslWindowsDiskPath
            legacy_qcow2_sha256 = $hashValue
            legacy_qcow2_copied = $false
            nvram_checkpoint = 'OVMF_VARS_4M.ms.fd'
            tpm_checkpoint = 'tpm/'
            disk_copy_policy = 'The qcow2 is intentionally not copied by default; use its path/hash and request a full checkpoint explicitly.'
            redacted_database = 'guacamole-db-redacted.sql (schema plus safe configuration tables); authentication hashes and parameter values excluded'
            legacy_owner_state = 'stopped; no qemu process matching windows11; no legacy TPM owner'
        }
        Write-CheckpointText -Path (Join-Path $stagingPath 'checkpoint-manifest.json') -Text (($manifest | ConvertTo-Json -Depth 4))
        Protect-Checkpoint -Path $stagingPath
        Move-Item -LiteralPath $stagingPath -Destination $checkpointPath
        $verify = Invoke-WslOutput -Command "cd '$WslRepoRoot/runtime/backups/$checkpointName' && sha256sum -c state-sha256sums.txt"
        if ($verify.Output -match '(?m)(FAILED|No such file|WARNING:.*mismatch)' -or
            $verify.Output -notmatch '(?m)OK$') {
            throw "CHECKPOINT_STATE_HASH_VERIFY_FAILED: $($verify.Output)"
        }
    } catch {
        $backupError = $_.Exception.Message
        $cleanupError = ''
        $partialRemoved = $true
        if (Test-Path -LiteralPath $stagingPath) {
            try {
                Remove-Item -LiteralPath $stagingPath -Recurse -Force
            } catch {
                $partialRemoved = $false
                $cleanupError = $_.Exception.Message
            }
        }
        try {
            $failurePath = Write-BackupFailureEvidence -CheckpointName $checkpointName -ErrorMessage $backupError -PartialRemoved $partialRemoved -CleanupError $cleanupError
            Write-Warning "Backup failed; failure evidence: $failurePath"
        } catch {
            Write-Warning ("Backup failure evidence could not be written: {0}" -f $_.Exception.Message)
        }
        throw ("LIBVIRT_BACKUP_FAILED: {0}" -f $backupError)
    }

    Write-Host "Checkpoint: $checkpointPath"
    Write-Host 'QCOW2_COPY_SKIPPED: path and SHA-256 recorded; no full disk copy was requested.'
    Write-Host 'LIBVIRT_BACKUP_OK'
}

function Invoke-Export {
    if (-not (Test-Path -LiteralPath $ExportMaintenanceScriptPath -PathType Leaf)) {
        throw "MAINTENANCE_EXPORT_SCRIPT_MISSING: '$ExportMaintenanceScriptPath'."
    }
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $ExportMaintenanceScriptPath
    if ($LASTEXITCODE -ne 0) {
        throw "MAINTENANCE_EXPORT_FAILED: export-maintenance-bundle.ps1 returned exit code $LASTEXITCODE."
    }
    Write-Host 'LIBVIRT_EXPORT_OK'
}

function Assert-FutureAction {
    param([Parameter(Mandatory)][string]$Name)
    throw "Action '$Name' is reserved for the later network/storage/migration tasks; no VM or network mutation was performed by Task 2."
}

switch ($Action) {
    'preflight'          { Invoke-Preflight }
    'install'            { Assert-WslAndHStorage; Install-Packages }
    'configure'          { Assert-WslAndHStorage; Configure-Cockpit }
    'cockpit-workspaces' { Assert-WslAndHStorage; Install-CockpitWorkspaces }
    'status'             { Assert-WslAndHStorage; Show-Status }
    'network'            { Assert-WslAndHStorage; Invoke-LibvirtNetwork }
    'storage'            { Assert-WslAndHStorage; Invoke-LibvirtStorage }
    'connect-guacamole'  { Assert-WslAndHStorage; Invoke-ConnectGuacamole }
    'start'              { Assert-WslAndHStorage; Invoke-LibvirtStart }
    'stop'               { Assert-WslAndHStorage; Invoke-LibvirtStop }
    'backup'             { Invoke-Backup }
    'export'             { Invoke-Export }
}
