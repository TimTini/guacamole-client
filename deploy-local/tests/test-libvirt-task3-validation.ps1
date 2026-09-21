[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$scriptPath = (Resolve-Path (Join-Path $PSScriptRoot '..\libvirt.ps1')).Path
$source = Get-Content -Raw -LiteralPath $scriptPath
$switchMarker = 'switch ($Action)'
$markerIndex = $source.IndexOf($switchMarker, [StringComparison]::Ordinal)
if ($markerIndex -lt 0) {
    throw "Could not find the action switch in $scriptPath."
}

# Load only the definitions so focused tests do not run a real libvirt action.
$definitions = $source.Substring(0, $markerIndex).Replace('$PSScriptRoot', "'$($PSScriptRoot.Replace("'", "''"))'")
$testBody = @'
$passed = 0
$failed = 0

function Assert-TestEqual {
    param([string]$Name, [object]$Actual, [object]$Expected)
    if ($Actual -ne $Expected) {
        $script:failed++
        throw "FAIL: $Name expected '$Expected' but got '$Actual'."
    }
    $script:passed++
}

function Assert-TestThrowsToken {
    param([string]$Name, [scriptblock]$Script, [string]$Token)
    try {
        & $Script
        $script:failed++
        throw "FAIL: $Name did not throw."
    } catch {
        if ($_.Exception.Message -notmatch [regex]::Escape($Token)) {
            $script:failed++
            throw "FAIL: $Name threw '$($_.Exception.Message)' without token '$Token'."
        }
        $script:passed++
    }
}

$validLink = '4: br-abcdef123456: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 state UP group default'
$validAddress = '4: br-abcdef123456    inet 172.18.0.1/16 brd 172.18.255.255 scope global br-abcdef123456'
$validRoute = '172.18.0.0/16 dev br-abcdef123456 proto kernel scope link src 172.18.0.1'
$bridgeArguments = @{
    NetworkName = 'guacamole-local_default'
    NetworkId = 'abcdef1234567890abcdef1234567890'
    Subnet = '172.18.0.0/16'
    Gateway = '172.18.0.1'
    Bridge = 'br-abcdef123456'
    BridgeSource = 'derived'
    LinkExitCode = 0
    LinkOutput = $validLink
    AddressExitCode = 0
    AddressOutput = $validAddress
    RouteExitCode = 0
    RouteOutput = $validRoute
}

$evidence = Assert-ComposeBridgeEvidence @bridgeArguments
Assert-TestEqual -Name 'valid bridge evidence' -Actual $evidence.Bridge -Expected 'br-abcdef123456'

$missingBridge = $bridgeArguments.Clone()
$missingBridge.LinkExitCode = 1
$missingBridge.LinkOutput = ''
Assert-TestThrowsToken -Name 'missing bridge interface' -Script { Assert-ComposeBridgeEvidence @missingBridge } -Token 'COMPOSE_BRIDGE_NOT_FOUND'

$mismatchedBridge = $bridgeArguments.Clone()
$mismatchedBridge.RouteOutput = '172.18.0.0/16 dev br-other proto kernel scope link src 172.18.0.1'
Assert-TestThrowsToken -Name 'bridge route mismatch' -Script { Assert-ComposeBridgeEvidence @mismatchedBridge } -Token 'COMPOSE_BRIDGE_MISMATCH'

$dirPool = Get-LibvirtStoragePoolTarget -XmlText '<pool type="dir"><target><path>/var/lib/guacamole-vms</path></target></pool>'
Assert-LibvirtStoragePoolContract -PoolContract $dirPool | Out-Null
Assert-TestEqual -Name 'directory pool type' -Actual $dirPool.Type -Expected 'dir'

$nonDirPool = Get-LibvirtStoragePoolTarget -XmlText '<pool type="logical"><target><path>/var/lib/guacamole-vms</path></target></pool>'
Assert-TestThrowsToken -Name 'non-directory pool rejected' -Script { Assert-LibvirtStoragePoolContract -PoolContract $nonDirPool } -Token 'LIBVIRT_STORAGE_POOL_CONFLICT'

Write-Host "TASK3_FOCUSED_TESTS_OK: $script:passed assertions"
'@

$testScript = [scriptblock]::Create($definitions + [Environment]::NewLine + $testBody)
& $testScript
