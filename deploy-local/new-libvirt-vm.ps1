[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)][ValidatePattern('^[a-zA-Z0-9][a-zA-Z0-9._-]{0,62}$')][string]$Name,
    [Parameter(Mandatory)][string]$IsoPath,
    [ValidateRange(2048, 262144)][int]$MemoryMiB = 4096,
    [ValidateRange(1, 64)][int]$Vcpus = 2,
    [ValidateRange(64, 4096)][int]$DiskGiB = 64,
    [string]$Mac,
    [string]$Ip
)

$ErrorActionPreference = 'Stop'
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$IsoRoot = (Resolve-Path (Join-Path $RepoRoot 'runtime\iso')).Path
$resolvedIso = (Resolve-Path -LiteralPath $IsoPath).Path
if (-not ($resolvedIso.StartsWith($IsoRoot + '\', [StringComparison]::OrdinalIgnoreCase))) { throw "ISO_PATH_OUTSIDE_REPO: $resolvedIso" }
if ([IO.Path]::GetExtension($resolvedIso) -ne '.iso') { throw 'ISO_FILE_REQUIRED' }

function Wsl([string]$Command, [switch]$AllowFailure) {
    $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try { $o = @(& wsl.exe -d Ubuntu-24.04 -u root -- sh -lc $Command 2>&1); $code = $LASTEXITCODE }
    finally { $ErrorActionPreference = $old }
    $text = ($o -join [Environment]::NewLine).Trim()
    if ($code -ne 0 -and -not $AllowFailure) { throw "WSL command failed ($code): $Command`n$text" }
    [pscustomobject]@{ Output=$text; ExitCode=$code }
}

if ((Wsl "virsh -c qemu:///system dominfo '$Name'" -AllowFailure).ExitCode -eq 0) { throw "DOMAIN_ALREADY_EXISTS: $Name" }
$disk = "/var/lib/guacamole-vms/$Name.qcow2"
if ((Wsl "test -e '$disk'" -AllowFailure).ExitCode -eq 0) { throw "DISK_ALREADY_EXISTS: $disk" }
$networkXml = (Wsl 'virsh -c qemu:///system net-dumpxml guac-nat').Output

$usedMacs = [regex]::Matches($networkXml, 'mac=["'']([^"'']+)["'']') | ForEach-Object { $_.Groups[1].Value.ToLowerInvariant() }
$usedIps = [regex]::Matches($networkXml, 'ip=["''](192\.168\.250\.\d+)["'']') | ForEach-Object { $_.Groups[1].Value }
if (-not $Mac) {
    foreach ($n in 2..254) { $candidate = '52:54:00:11:11:{0:x2}' -f $n; if ($candidate -notin $usedMacs) { $Mac = $candidate; break } }
}
if ($Mac -notmatch '^52:54:00:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}$' -or $Mac.ToLowerInvariant() -in $usedMacs) { throw "MAC_INVALID_OR_USED: $Mac" }
if (-not $Ip) { foreach ($n in 12..99) { $candidate = "192.168.250.$n"; if ($candidate -notin $usedIps) { $Ip = $candidate; break } } }
if ($Ip -notmatch '^192\.168\.250\.(1[2-9]|[2-9][0-9])$' -or $Ip -in $usedIps) { throw "IP_INVALID_OR_USED: $Ip" }

$isoWsl = '/mnt/h/' + ($resolvedIso.Substring(3).Replace('\','/'))
$xml = @"
<domain type='kvm'>
  <name>$Name</name><memory unit='MiB'>$MemoryMiB</memory><vcpu>$Vcpus</vcpu>
  <os><type arch='x86_64' machine='q35'>hvm</type><loader readonly='yes' secure='yes' type='pflash'>/usr/share/OVMF/OVMF_CODE_4M.ms.fd</loader><nvram template='/usr/share/OVMF/OVMF_VARS_4M.ms.fd'/></os>
  <features><acpi/><apic/><smm state='on'/></features><cpu mode='host-passthrough'/>
  <devices>
    <disk type='file' device='disk'><driver name='qemu' type='qcow2'/><source file='$disk'/><target dev='sda' bus='sata'/></disk>
    <disk type='file' device='cdrom'><driver name='qemu' type='raw'/><source file='$isoWsl'/><target dev='sdb' bus='sata'/><readonly/></disk>
    <interface type='network'><mac address='$Mac'/><source network='guac-nat'/><model type='e1000e'/></interface>
    <graphics type='spice' autoport='yes' listen='127.0.0.1'/><video><model type='qxl'/></video>
  </devices>
</domain>
"@
[xml]$null = $xml
$generated = Join-Path $RepoRoot "runtime\vm-definitions\$Name.generated.xml"
New-Item -ItemType Directory -Force -Path (Split-Path $generated) | Out-Null
[IO.File]::WriteAllText($generated, $xml, [Text.UTF8Encoding]::new($false))
$generatedWsl = '/mnt/h/' + ($generated.Substring(3).Replace('\','/'))
$reservation = Join-Path $RepoRoot "runtime\vm-definitions\$Name.dhcp-host.xml"
[IO.File]::WriteAllText($reservation, "<host mac='$Mac' name='$Name' ip='$Ip'/>", [Text.UTF8Encoding]::new($false))
$reservationWsl = '/mnt/h/' + ($reservation.Substring(3).Replace('\','/'))

if ($PSCmdlet.ShouldProcess($Name, "Create $DiskGiB GiB disk, DHCP reservation, and persistent libvirt domain")) {
    Wsl "qemu-img create -f qcow2 '$disk' '${DiskGiB}G'" | Out-Null
    try {
        Wsl "virsh -c qemu:///system net-update guac-nat add ip-dhcp-host '$reservationWsl' --live --config" | Out-Null
        Wsl "virsh -c qemu:///system define '$generatedWsl'" | Out-Null
        Wsl 'virsh -c qemu:///system pool-refresh guacamole-vms' | Out-Null
    } catch {
        Wsl "virsh -c qemu:///system undefine '$Name' --nvram" -AllowFailure | Out-Null
        Wsl "virsh -c qemu:///system net-update guac-nat delete ip-dhcp-host '$reservationWsl' --live --config" -AllowFailure | Out-Null
        Wsl "rm -f '$disk'" -AllowFailure | Out-Null
        throw
    }
}

$hash = (Get-FileHash -LiteralPath $resolvedIso -Algorithm SHA256).Hash
Write-Host "ISO_SHA256=$hash"
Write-Host "LIBVIRT_VM_CREATED name=$Name mac=$Mac ip=$Ip"
Write-Host 'The VM remains shut off. Verify capacity and ISO hash, then start it from Cockpit.'
