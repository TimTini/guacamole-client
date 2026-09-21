[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$IgnorePath = Join-Path $RepoRoot '.gitignore'
$AuditPath = Join-Path $RepoRoot 'public-release-audit.ps1'
$ExporterPath = Join-Path $RepoRoot 'deploy-local\export-maintenance-bundle.ps1'

function Assert-Test {
    param(
        [Parameter(Mandatory)][bool]$Condition,
        [Parameter(Mandatory)][string]$Message
    )
    if (-not $Condition) {
        throw "PUBLIC_RELEASE_ASSERTION_FAILED: $Message"
    }
}

function Invoke-Audit {
    param([Parameter(Mandatory)][string]$Path)

    $output = @(& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $AuditPath -RepoRoot $Path 2>&1)
    [pscustomobject]@{
        ExitCode = $LASTEXITCODE
        Output = ($output -join "`n")
    }
}

Assert-Test -Condition (Test-Path -LiteralPath $AuditPath -PathType Leaf) -Message 'public-release-audit.ps1 is missing'
Assert-Test -Condition (Test-Path -LiteralPath $ExporterPath -PathType Leaf) -Message 'export-maintenance-bundle.ps1 is missing'

Push-Location -LiteralPath $RepoRoot
try {
    $previousErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $defaultOutput = @(& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $AuditPath 2>&1)
    $defaultExitCode = $LASTEXITCODE
    $ErrorActionPreference = $previousErrorActionPreference
} finally {
    Pop-Location
}
Assert-Test -Condition ($defaultExitCode -in @(0, 1)) -Message "no-argument audit invocation failed unexpectedly: $($defaultOutput -join ' ')"
Assert-Test -Condition (($defaultOutput -join "`n") -notmatch '(?i)Cannot bind argument|empty string|ParameterArgumentValidationException') -Message "no-argument audit invocation has a binding error: $($defaultOutput -join ' ')"

$ignore = Get-Content -LiteralPath $IgnorePath -Raw
foreach ($pattern in @(
    '(?m)^runtime/$',
    '(?m)^deploy-local/data/$',
    '(?m)^deploy-local/secrets/$',
    '(?m)^\*\.vhdx$',
    '(?m)^\*\.qcow2$',
    '(?m)^\*\.vmdk$',
    '(?m)^\*\.iso$',
    '(?m)^\*\.img$',
    '(?m)^\*\.nvram$',
    '(?m)^\*\.log$',
    '(?m)^\*\.bak$',
    '(?m)^\*\.zip$',
    '(?m)^\*\.pem$',
    '(?m)^\*\.key$',
    '(?m)^\.env$',
    '(?m)^\.env\.\*$',
    '(?m)^\*\.pyc$'
)) {
    Assert-Test -Condition ($ignore -match $pattern) -Message "gitignore pattern missing: $pattern"
}
foreach ($pattern in @('**/*secret*', '**/*token*', '**/*privatekey*')) {
    Assert-Test -Condition $ignore.Contains($pattern) -Message "gitignore sensitive-name pattern missing: $pattern"
}

foreach ($path in @(
    'runtime/ubuntu/ext4.vhdx',
    'runtime/vm-windows11/Autounattend.xml',
    'runtime/vm-demo/vm-password.txt',
    'runtime/backups/maintenance.zip',
    'runtime/cloudflared/quick-tunnel.stderr.log',
    'deploy-local/data/postgres-init/001-guacamole.sql',
    'deploy-local/secrets/example.env',
    'deploy-local/secrets/example.key'
)) {
    & git -C $RepoRoot check-ignore --no-index -q -- $path
    Assert-Test -Condition ($LASTEXITCODE -eq 0) -Message "path is not ignored: $path"
}

$auditSource = Get-Content -LiteralPath $AuditPath -Raw
foreach ($required in @(
    "'ls-files'",
    "'--cached'",
    "'--exclude-standard'",
    'trycloudflare',
    'UPSTREAM_ORIGIN_FORBIDDEN',
    'ReadAllBytes'
)) {
    Assert-Test -Condition ($auditSource.Contains($required)) -Message "audit contract is missing: $required"
}

$exporterSource = Get-Content -LiteralPath $ExporterPath -Raw
foreach ($required in @('__pycache__', 'pyc', 'generated', 'inventory')) {
    Assert-Test -Condition ($exporterSource -match $required) -Message "maintenance export exclusion is missing: $required"
}

$fixture = Join-Path ([System.IO.Path]::GetTempPath()) ('guacamole-public-audit-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $fixture | Out-Null
try {
    & git -C $fixture init --quiet
    & git -C $fixture remote add origin https://example.invalid/public.git
    Set-Content -LiteralPath (Join-Path $fixture 'README.md') -Value 'safe source'
    & git -C $fixture add -- README.md

    $result = Invoke-Audit -Path $fixture
    Assert-Test -Condition ($result.ExitCode -eq 0) -Message "safe fixture did not pass: $($result.Output)"

    Set-Content -LiteralPath (Join-Path $fixture 'service.log') -Value 'log marker'
    Set-Content -LiteralPath (Join-Path $fixture 'database.dump') -Value 'dump marker'
    Set-Content -LiteralPath (Join-Path $fixture 'workspace-inventory.json') -Value '{}'
    Set-Content -LiteralPath (Join-Path $fixture 'compiled.pyc') -Value 'cache marker'
    $privateKeyMarker = ('-----BEGIN ' + 'PRIVATE KEY-----') + "`nplaceholder`n" + ('-----END ' + 'PRIVATE KEY-----')
    Set-Content -LiteralPath (Join-Path $fixture 'notes.txt') -Value $privateKeyMarker
    Set-Content -LiteralPath (Join-Path $fixture 'service-password.txt') -Value 'placeholder'
    $result = Invoke-Audit -Path $fixture
    Assert-Test -Condition ($result.ExitCode -ne 0) -Message 'generated and key artifacts were not rejected'
    foreach ($rule in @(
        'service\.log:GENERATED_ARTIFACT',
        'database\.dump:DATABASE_DUMP',
        'workspace-inventory\.json:GENERATED_INVENTORY',
        'compiled\.pyc:PYTHON_CACHE',
        'notes\.txt:CONTENT_PRIVATE_KEY',
        'service-password\.txt:CREDENTIAL_PATH'
    )) {
        Assert-Test -Condition ($result.Output -match $rule) -Message "artifact rule missing: $rule"
    }
    foreach ($file in @('service.log', 'database.dump', 'workspace-inventory.json', 'compiled.pyc', 'notes.txt', 'service-password.txt')) {
        Remove-Item -LiteralPath (Join-Path $fixture $file) -Force
    }

    New-Item -ItemType Directory -Path (Join-Path $fixture 'runtime') | Out-Null
    Set-Content -LiteralPath (Join-Path $fixture 'runtime/state.txt') -Value 'runtime marker'
    $result = Invoke-Audit -Path $fixture
    Assert-Test -Condition ($result.ExitCode -ne 0) -Message 'runtime path was not rejected'
    Assert-Test -Condition ($result.Output -match 'runtime/state\.txt:PATH_RUNTIME') -Message "runtime rule missing: $($result.Output)"

    Set-Content -LiteralPath (Join-Path $fixture 'forced.vhdx') -Value 'disk marker'
    & git -C $fixture add -- forced.vhdx
    $result = Invoke-Audit -Path $fixture
    Assert-Test -Condition ($result.ExitCode -ne 0) -Message 'staged VM disk was not rejected'
    Assert-Test -Condition ($result.Output -match 'forced\.vhdx:VM_ARTIFACT') -Message "staged VM rule missing: $($result.Output)"

    Remove-Item -LiteralPath (Join-Path $fixture 'runtime') -Recurse -Force
    & git -C $fixture reset --quiet -- forced.vhdx
    Remove-Item -LiteralPath (Join-Path $fixture 'forced.vhdx') -Force
    $dynamicTunnelUrl = 'https://' + 'example123' + '.trycloudflare.com/'
    Set-Content -LiteralPath (Join-Path $fixture 'tunnel.txt') -Value $dynamicTunnelUrl
    $result = Invoke-Audit -Path $fixture
    Assert-Test -Condition ($result.ExitCode -ne 0) -Message 'dynamic tunnel URL was not rejected'
    Assert-Test -Condition ($result.Output -match 'tunnel\.txt:CONTENT_TUNNEL_URL') -Message "tunnel rule missing: $($result.Output)"

    Remove-Item -LiteralPath (Join-Path $fixture 'tunnel.txt') -Force
    Set-Content -LiteralPath (Join-Path $fixture 'README.md') -Value 'custom change'
    & git -C $fixture add -- README.md
    & git -C $fixture remote set-url origin https://github.com/apache/guacamole-client.git
    $result = Invoke-Audit -Path $fixture
    Assert-Test -Condition ($result.ExitCode -ne 0) -Message 'upstream custom change was not rejected'
    Assert-Test -Condition ($result.Output -match 'origin:UPSTREAM_ORIGIN_FORBIDDEN') -Message "upstream rule missing: $($result.Output)"

    $cleanFixture = Join-Path ([System.IO.Path]::GetTempPath()) ('guacamole-public-audit-clean-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $cleanFixture | Out-Null
    try {
        & git -C $cleanFixture init --quiet
        & git -C $cleanFixture remote add origin https://github.com/apache/guacamole-client.git
        $cleanResult = Invoke-Audit -Path $cleanFixture
        Assert-Test -Condition ($cleanResult.ExitCode -ne 0) -Message 'clean upstream origin was not rejected'
        Assert-Test -Condition ($cleanResult.Output -match '^origin:UPSTREAM_ORIGIN_FORBIDDEN$') -Message "clean upstream rule missing: $($cleanResult.Output)"
    }
    finally {
        if (Test-Path -LiteralPath $cleanFixture) {
            Remove-Item -LiteralPath $cleanFixture -Recurse -Force
        }
    }
}
finally {
    if (Test-Path -LiteralPath $fixture) {
        Remove-Item -LiteralPath $fixture -Recurse -Force
    }
}

Write-Host 'PUBLIC_RELEASE_AUDIT_STATIC_TEST_OK'
