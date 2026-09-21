[CmdletBinding()]
param(
    [ValidateSet('preflight', 'migration', 'recovery', 'new-vm', 'templates', 'all')]
    [string]$Mode = 'all',
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$')]
    [string]$DomainName = 'windows11',
    [ValidatePattern('^windows11-v[0-9]+$')]
    [string]$TemplateVersion = 'windows11-v1'
)

$ErrorActionPreference = 'Stop'
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$VhdxPath = Join-Path $RepoRoot 'runtime\ubuntu\ext4.vhdx'
$MarkerPath = Join-Path $RepoRoot 'runtime\vm-windows11\libvirt-cutover.marker'
$HelperPath = '/usr/local/libexec/guacamole-workspace-helper'
$InventoryPath = '/var/lib/guacamole-templates/inventory.json'
$TemplateRoot = '/var/lib/guacamole-templates'
$CloneRoot = '/var/lib/guacamole-vms'
$GuacamoleComposeNetwork = 'guacamole-local_default'
$Wsl = @('-d', 'Ubuntu-24.04', '-u', 'root', '--')

function Invoke-WslReadOnly {
    param([Parameter(Mandatory)][string]$Command)
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { $output = @(& wsl.exe @Wsl sh -lc $Command 2>&1); $exitCode = $LASTEXITCODE }
    finally { $ErrorActionPreference = $previousPreference }
    if ($exitCode -ne 0) { throw "Read-only WSL check failed: $Command`n$($output -join [Environment]::NewLine)" }
    ($output -join [Environment]::NewLine).Trim()
}

function Assert-Text { param([bool]$Condition, [string]$Message) if (-not $Condition) { throw $Message } }

function Get-XmlTextValue {
    param([Parameter(Mandatory)]$Value)
    if ($Value -is [System.Xml.XmlElement]) { return [string]$Value.InnerText }
    return [string]$Value
}

function Get-LastJsonObject {
    param([Parameter(Mandatory)][string]$Text)
    $line = $Text -split "`r?`n" |
        Where-Object { $_ -match '^\s*\{' } |
        Select-Object -Last 1
    Assert-Text (-not [string]::IsNullOrWhiteSpace($line)) 'Workspace helper did not return a JSON object.'
    try { return ($line | ConvertFrom-Json) }
    catch { throw 'Workspace helper returned invalid JSON.' }
}

function Invoke-GuacamoleReadOnlyQuery {
    param([Parameter(Mandatory)][string]$Query)
    $query64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Query))
    $command = "cd /mnt/h/RemoteWorkspaces/guacamole-client/deploy-local && printf '%s' '$query64' | base64 -d | docker compose exec -T postgres psql -X -q -At -U guacamole_user -d guacamole_db"
    Invoke-WslReadOnly $command
}

function Get-DhcpHosts {
    param([Parameter(Mandatory)][string]$XmlText)
    try { [xml]$xml = $XmlText }
    catch { throw 'guac-nat DHCP XML is invalid.' }
    @($xml.network.ip.dhcp.host)
}

function Assert-DhcpReservation {
    param(
        [Parameter(Mandatory)][string]$XmlText,
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][string]$Mac,
        [Parameter(Mandatory)][string]$Ip,
        [Parameter(Mandatory)][string]$Scope
    )
    $match = Get-DhcpHosts -XmlText $XmlText |
        Where-Object { $_.name -eq $Name -and $_.mac.ToLowerInvariant() -eq $Mac.ToLowerInvariant() -and $_.ip -eq $Ip }
    Assert-Text (@($match).Count -eq 1) "DHCP reservation is missing or mismatched in $Scope for $Name ($Ip/$Mac)."
}

function Test-TemplateGate {
    Assert-Text ($DomainName -eq 'windows11') 'Template verification source must be exactly windows11.'
    Assert-Text (Test-Path -LiteralPath $VhdxPath -PathType Leaf) 'H-backed WSL disk is missing before template verification.'

    $packages = Invoke-WslReadOnly 'cockpit-bridge --packages'
    Assert-Text ($packages -match '(?m)(^|\s)workspace_templates(\s|$)') 'Cockpit Workspace Templates package was not discovered.'

    $payload = Get-LastJsonObject -Text (Invoke-WslReadOnly "$HelperPath list --json")
    Assert-Text ($payload.ok -eq $true) 'Workspace helper list did not report ok:true.'
    $templates = @($payload.templates | Where-Object { $_.version -eq $TemplateVersion })
    Assert-Text ($templates.Count -eq 1) "Template $TemplateVersion is missing or duplicated in inventory."
    $template = $templates[0]
    Assert-Text ($template.sourceDomain -eq 'windows11') 'Template sourceDomain is not windows11.'
    Assert-Text ($template.hashState -eq 'recorded') "Template $TemplateVersion does not have a recorded hash state."
    Assert-Text ($template.sha256 -match '^[0-9a-fA-F]{64}$') 'Template inventory hash is not a SHA-256 digest.'
    Assert-Text ($template.path -eq "$TemplateRoot/$TemplateVersion.qcow2") 'Template inventory path is not the versioned H-backed path.'

    $templateMode = Invoke-WslReadOnly "stat -c '%a' '$($template.path)'"
    Assert-Text ($templateMode -eq '444') "Template image mode must be 0444; observed $templateMode."
    $templateHash = (Invoke-WslReadOnly "sha256sum '$($template.path)' | cut -d ' ' -f 1").Trim()
    Assert-Text ($templateHash -eq $template.sha256.ToLowerInvariant()) 'Template SHA-256 does not match inventory.'
    Invoke-WslReadOnly "test -f '$($template.path)' && qemu-img check '$($template.path)'" | Out-Null
    $templateInfo = Invoke-WslReadOnly "qemu-img info --backing-chain '$($template.path)'"
    Assert-Text ($templateInfo -notmatch '(?m)^backing file:') 'Golden template must not depend on a writable backing file.'

    $sourceXmlText = Invoke-WslReadOnly "virsh -c qemu:///system dumpxml windows11"
    try { [xml]$sourceXml = $sourceXmlText }
    catch { throw 'Source domain XML is invalid during template verification.' }
    $sourceUuid = [string]$sourceXml.domain.uuid
    $sourceMac = [string]$sourceXml.domain.devices.interface.mac.address
    $sourceNvram = Get-XmlTextValue $sourceXml.domain.os.nvram
    Assert-Text ($sourceUuid -match '^[0-9a-fA-F-]{36}$' -and $sourceMac -match '^(?i)([0-9a-f]{2}:){5}[0-9a-f]{2}$') 'Source identity could not be read.'

    $clones = @($payload.clones)
    $seenUuids = @($sourceUuid.ToLowerInvariant())
    $seenMacs = @($sourceMac.ToLowerInvariant())
    $seenNvram = @($sourceNvram)
    $inventoryCloneCount = 0
    foreach ($clone in $clones) {
        Assert-Text ($null -ne $clone -and $clone.name -match '^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$') 'Inventory contains an invalid clone name.'
        Assert-Text ($clone.templateVersion -eq $TemplateVersion) "Clone $($clone.name) does not use $TemplateVersion."
        Assert-Text ($clone.mac -match '^(?i)([0-9a-f]{2}:){5}[0-9a-f]{2}$') "Clone $($clone.name) has an invalid MAC."
        Assert-Text ($clone.ip -match '^192\.168\.250\.(2[0-4][0-9]|1[0-9][0-9]|[2-9][0-9])$') "Clone $($clone.name) has an invalid workspace IP."
        $info = Invoke-WslReadOnly "virsh -c qemu:///system dominfo '$($clone.name)'"
        Assert-Text ($info -match '(?m)^Persistent:\s+yes\s*$') "Clone $($clone.name) is not persistent."
        $xmlText = Invoke-WslReadOnly "virsh -c qemu:///system dumpxml '$($clone.name)'"
        try { [xml]$xml = $xmlText }
        catch { throw "Clone $($clone.name) XML is invalid." }
        $uuid = [string]$xml.domain.uuid
        $mac = [string]$xml.domain.devices.interface.mac.address
        $nvram = Get-XmlTextValue $xml.domain.os.nvram
        $disk = [string]$xml.domain.devices.disk.source.file
        $network = [string]$xml.domain.devices.interface.source.network
        $tpmBackend = $xml.domain.devices.tpm.backend
        Assert-Text ($uuid -match '^[0-9a-fA-F-]{36}$' -and $uuid.ToLowerInvariant() -notin $seenUuids) "Clone $($clone.name) UUID is missing or reused."
        Assert-Text ($mac -match '^(?i)([0-9a-f]{2}:){5}[0-9a-f]{2}$' -and $mac.ToLowerInvariant() -notin $seenMacs) "Clone $($clone.name) MAC is missing or reused."
        Assert-Text ($nvram -and $nvram -notin $seenNvram -and $nvram -match "/var/lib/libvirt/qemu/nvram/$([regex]::Escape($clone.name))_VARS\.fd$") "Clone $($clone.name) NVRAM is missing or shared."
        Assert-Text ($disk -eq "$CloneRoot/$($clone.name).qcow2" -and $disk -ne [string]$sourceXml.domain.devices.disk.source.file) "Clone $($clone.name) disk path is not independent."
        Assert-Text ($network -eq 'guac-nat') "Clone $($clone.name) is not attached to guac-nat."
        Assert-Text ($null -ne $tpmBackend -and [string]$tpmBackend.type -eq 'emulator' -and [string]$tpmBackend.version -eq '2.0') "Clone $($clone.name) does not use a managed TPM 2.0."
        Assert-Text ($xmlText -notmatch [regex]::Escape('/run/guacamole-vm-windows11/swtpm.sock')) "Clone $($clone.name) reuses source TPM socket."
        Invoke-WslReadOnly "test -f '$disk' && test -d '/var/lib/libvirt/swtpm/$uuid/tpm2' && qemu-img check --force-share '$disk'" | Out-Null
        $chain = Invoke-WslReadOnly "qemu-img info --force-share --backing-chain '$disk'"
        Assert-Text ($chain -match [regex]::Escape($template.path)) "Clone $($clone.name) backing chain does not point to $TemplateVersion."
        $liveDhcp = Invoke-WslReadOnly 'virsh -c qemu:///system net-dumpxml guac-nat'
        $configDhcp = Invoke-WslReadOnly 'virsh -c qemu:///system net-dumpxml guac-nat --inactive'
        Assert-DhcpReservation -XmlText $liveDhcp -Name $clone.name -Mac $clone.mac -Ip $clone.ip -Scope 'live network'
        Assert-DhcpReservation -XmlText $configDhcp -Name $clone.name -Mac $clone.mac -Ip $clone.ip -Scope 'persistent network'
        Invoke-WslReadOnly "docker run --rm --network $GuacamoleComposeNetwork busybox:1.36 sh -c 'nc -zvw5 $($clone.ip) 3389'" | Out-Null

        $assigneeType = [string]$clone.assigneeType
        $assigneeName = [string]$clone.assigneeName
        Assert-Text ($assigneeType -in @('USER', 'USER_GROUP') -and $assigneeName -match '^[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}$' -and $assigneeName -ne 'guacadmin') "Clone $($clone.name) has no isolated non-admin assignee."
        $quotedName = $clone.name.Replace("'", "''")
        $quotedAssignee = $assigneeName.Replace("'", "''")
        $permissionQuery = @"
SELECT c.protocol || '|' ||
       COALESCE((SELECT parameter_value FROM guacamole_connection_parameter WHERE connection_id=c.connection_id AND parameter_name='hostname'), '') || '|' ||
       COALESCE((SELECT parameter_value FROM guacamole_connection_parameter WHERE connection_id=c.connection_id AND parameter_name='port'), '') || '|' ||
       (SELECT count(*) FROM guacamole_connection_parameter WHERE connection_id=c.connection_id AND parameter_name IN ('hostname','port','security','ignore-cert')) || '|' ||
       (SELECT count(*) FROM guacamole_connection_parameter WHERE connection_id=c.connection_id AND parameter_name NOT IN ('hostname','port','security','ignore-cert')) || '|' ||
       COALESCE((SELECT string_agg(cp.permission::text, ',' ORDER BY cp.permission) FROM guacamole_connection_permission cp JOIN guacamole_entity e ON e.entity_id=cp.entity_id WHERE cp.connection_id=c.connection_id AND e.type='$assigneeType'::guacamole_entity_type AND e.name='$quotedAssignee'), '') || '|' ||
       COALESCE((SELECT string_agg(cp.permission::text, ',' ORDER BY cp.permission) FROM guacamole_connection_permission cp JOIN guacamole_entity e ON e.entity_id=cp.entity_id WHERE cp.connection_id=c.connection_id AND e.type='USER'::guacamole_entity_type AND e.name='guacadmin'), '') || '|' ||
       (SELECT count(*) FROM guacamole_connection_parameter WHERE connection_id=c.connection_id AND parameter_name IN ('username','password','domain','gateway-username','gateway-password','private-key','passphrase')) || '|' ||
       (SELECT count(*) FROM guacamole_connection_permission cp JOIN guacamole_entity e ON e.entity_id=cp.entity_id WHERE cp.connection_id=c.connection_id AND NOT ((e.type='$assigneeType'::guacamole_entity_type AND e.name='$quotedAssignee') OR (e.type='USER'::guacamole_entity_type AND e.name='guacadmin')))
FROM guacamole_connection c
WHERE c.connection_name='$quotedName' AND c.parent_id IS NULL AND c.protocol='rdp';
"@
        $permission = Invoke-GuacamoleReadOnlyQuery -Query $permissionQuery
        $fields = $permission.Trim().Split('|')
        Assert-Text ($fields.Count -eq 9 -and $fields[0] -eq 'rdp' -and $fields[1] -eq $clone.ip -and $fields[2] -eq '3389' -and $fields[3] -eq '4' -and $fields[4] -eq '0') "Guacamole parameter allowlist failed for clone $($clone.name)."
        Assert-Text ($fields[5] -eq 'READ' -and $fields[6] -eq 'READ,UPDATE,DELETE,ADMINISTER' -and $fields[7] -eq '0' -and $fields[8] -eq '0') "Guacamole permission isolation failed for clone $($clone.name)."
        $seenUuids += $uuid.ToLowerInvariant()
        $seenMacs += $mac.ToLowerInvariant()
        $seenNvram += $nvram
        $inventoryCloneCount++
    }
    Assert-Text ([int]$template.dependentCloneCount -eq $inventoryCloneCount) 'Template dependent clone count does not match inventory.'

    # The retirement audit joins durable workspace markers with template
    # backing chains, domain metadata, the helper-owned DHCP/MAC pool,
    # NVRAM/TPM state, and Guacamole workflow rows independently of inventory.
    # A clean inventory must not hide an arbitrary-name orphan.
    $retirement = Get-LastJsonObject -Text (Invoke-WslReadOnly "$HelperPath check-template-delete --version '$TemplateVersion' || true")
    if ($inventoryCloneCount -eq 0) {
        Assert-Text ($retirement.ok -eq $true -and $retirement.deletable -eq $true) "Template retirement audit found blockers or could not complete: $($retirement | ConvertTo-Json -Compress)"
        Assert-Text (@($retirement.blockers).Count -eq 0) 'Template retirement audit returned unexpected blockers.'
        Write-Host 'TEMPLATES_NO_DISPOSABLE_CLONE_OK'
    } else {
        Assert-Text ($retirement.ok -eq $false -and $retirement.code -eq 'TEMPLATE_IN_USE' -and $retirement.deletable -eq $false) "Template retirement audit did not fail closed for active clones: $($retirement | ConvertTo-Json -Compress)"
        foreach ($clone in $clones) {
            $cloneBlockers = @($retirement.blockers | Where-Object { $_.name -eq $clone.name })
            Assert-Text ($cloneBlockers.Count -gt 0) "Template retirement audit omitted active clone $($clone.name)."
        }
        Write-Host "TEMPLATES_RETIREMENT_BLOCKED_OK clones=$inventoryCloneCount"
    }
    Write-Host "TEMPLATE_IMMUTABLE_OK version=$TemplateVersion"
    Write-Host "TEMPLATE_BACKING_CHAINS_OK clones=$inventoryCloneCount"
    Write-Host 'TEMPLATE_IDENTITY_DHCP_TPM_OK'
    Write-Host 'TEMPLATE_RDP_GUACAMOLE_ISOLATION_OK'
    Write-Host 'TEMPLATE_ORPHAN_RETIREMENT_AUDIT_OK'
    Write-Host 'COCKPIT_WORKSPACE_PACKAGE_OK'
}

function Test-HostGate {
    Assert-Text (Test-Path -LiteralPath $VhdxPath -PathType Leaf) "Missing H-backed VHDX: $VhdxPath"
    Invoke-WslReadOnly 'test -e /dev/kvm' | Out-Null
    Assert-Text ((Invoke-WslReadOnly 'virsh -c qemu:///system uri') -eq 'qemu:///system') 'qemu:///system is unavailable.'
    $cockpitDomains = Invoke-WslReadOnly 'busctl call org.libvirt /org/libvirt/QEMU org.libvirt.Connect ListDomains u 0'
    Assert-Text ($cockpitDomains -match '^ao\s') 'Cockpit cannot activate org.libvirt over the system D-Bus. Run: wsl.exe -d Ubuntu-24.04 -u root -- systemctl reload dbus'
    $listeners = Invoke-WslReadOnly "ss -ltnp | grep ':9090 '"
    Assert-Text ($listeners -match '127\.0\.0\.1:9090' -and $listeners -notmatch '0\.0\.0\.0:9090|\[::\]:9090') 'Cockpit is not local-only on 127.0.0.1:9090.'
    $cockpitCode = & curl.exe --insecure --silent --output NUL --write-out '%{http_code}' https://127.0.0.1:9090
    Assert-Text ($cockpitCode -eq '200') "Cockpit HTTP check returned $cockpitCode."
    $guac = Invoke-WebRequest -Uri 'http://127.0.0.1:8080/guacamole/' -UseBasicParsing -TimeoutSec 15
    Assert-Text ($guac.StatusCode -eq 200) "Guacamole HTTP check returned $($guac.StatusCode)."
    Write-Host 'HOST_STORAGE_OK'
    Write-Host 'COCKPIT_LOCAL_ONLY_OK'
    Write-Host 'COCKPIT_LIBVIRT_DBUS_OK'
    Write-Host 'GUACAMOLE_LOCAL_HTTP_OK'
}

function Test-LibvirtGate {
    Assert-Text (Test-Path -LiteralPath $MarkerPath -PathType Leaf) 'Libvirt cutover marker is missing.'
    $net = Invoke-WslReadOnly "virsh -c qemu:///system net-info guac-nat"
    Assert-Text ($net -match '(?m)^Active:\s+yes\s*$' -and $net -match '(?m)^Autostart:\s+yes\s*$') 'guac-nat is not active/autostart.'
    $pool = Invoke-WslReadOnly "virsh -c qemu:///system pool-info guacamole-vms"
    Assert-Text ($pool -match '(?m)^State:\s+running\s*$' -and $pool -match '(?m)^Autostart:\s+yes\s*$') 'guacamole-vms is not active/autostart.'
    $info = Invoke-WslReadOnly "virsh -c qemu:///system dominfo '$DomainName'"
    Assert-Text ($info -match '(?m)^Persistent:\s+yes\s*$' -and $info -match '(?m)^State:\s+running\s*$') "$DomainName is not persistent/running."
    $xml = Invoke-WslReadOnly "virsh -c qemu:///system dumpxml '$DomainName'"
    foreach ($required in @('OVMF_CODE_4M.ms.fd', "type='external'", '/run/guacamole-vm-windows11/swtpm.sock', "model type='e1000e'", '52:54:00:11:11:01', "network='guac-nat'")) {
        Assert-Text ($xml.Contains($required)) "Domain XML missing contract: $required"
    }
    Assert-Text ($xml -notmatch '(?i)\.iso|device=.cdrom.|password|trycloudflare|0\.0\.0\.0') 'Domain XML contains installer media, secret, tunnel, or public listener.'
    $blocks = Invoke-WslReadOnly "virsh -c qemu:///system domblklist '$DomainName' --details"
    Assert-Text ($blocks -match '/var/lib/guacamole-vm-windows11/windows11\.qcow2' -and $blocks -notmatch '(?i)\.iso') 'Domain disk path or media is invalid.'
    Assert-Text ((Invoke-WslReadOnly "systemctl is-active guacamole-vm-windows11-libvirt-tpm.service") -eq 'active') 'Dedicated TPM unit is inactive.'
    Invoke-WslReadOnly "test -S /run/guacamole-vm-windows11/swtpm.sock" | Out-Null
    $lease = Invoke-WslReadOnly 'virsh -c qemu:///system net-dhcp-leases guac-nat'
    Assert-Text ($lease -match '192\.168\.250\.11/24') 'Windows 11 DHCP reservation is absent.'
    Invoke-WslReadOnly "docker run --rm --network guacamole-local_default busybox:1.36 sh -c 'nc -zvw5 192.168.250.11 3389'" | Out-Null
    Write-Host 'LIBVIRT_STATE_OK'
    Write-Host 'LIBVIRT_TPM_OK'
    Write-Host 'LIBVIRT_NVRAM_OK'
    Write-Host 'WINDOWS11_RDP_ROUTE_OK'
}

function Test-OwnershipGate {
    foreach ($unit in @('guacamole-vm-windows11.service', 'guacamole-vm-windows11-tpm.service')) {
        $state = Invoke-WslReadOnly "systemctl show -p ActiveState --value '$unit' 2>/dev/null || true"
        Assert-Text ($state -ne 'active') "Legacy unit is active: $unit"
    }
    $legacy = Invoke-WslReadOnly "pgrep -af 'qemu-system-x86_64.*-name guacamole-vm-windows11([[:space:]]|$)' || true"
    Assert-Text ([string]::IsNullOrWhiteSpace($legacy)) 'Legacy Windows QEMU process is active.'
    $query = "SELECT c.connection_id||'|'||c.connection_name||'|'||max(CASE WHEN p.parameter_name='hostname' THEN p.parameter_value END)||'|'||max(CASE WHEN p.parameter_name='port' THEN p.parameter_value END) FROM guacamole_connection c JOIN guacamole_connection_parameter p USING(connection_id) WHERE c.connection_name='Windows 11' GROUP BY c.connection_id,c.connection_name;"
    $query64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($query))
    $inventory = Invoke-WslReadOnly "cd /mnt/h/RemoteWorkspaces/guacamole-client/deploy-local && printf '%s' '$query64' | base64 -d | docker compose exec -T postgres psql -X -q -At -U guacamole_user -d guacamole_db"
    Assert-Text ($inventory -eq '2|Windows 11|192.168.250.11|3389') "Unexpected Guacamole inventory: $inventory"
    Write-Host 'GUAC_PERMISSION_INVENTORY_OK'
    Write-Host 'RECOVERY_OWNER_OK'
}

Test-HostGate
if ($Mode -ne 'preflight') { Test-LibvirtGate; Test-OwnershipGate }
if ($Mode -eq 'new-vm') { Write-Host 'NEW_VM_PREREQUISITES_OK' }
if ($Mode -eq 'templates') { Test-TemplateGate }
Write-Host "LIBVIRT_TEST_OK mode=$Mode"
