[CmdletBinding()]
param(
    [string]$RepoRoot
)

$ErrorActionPreference = 'Stop'
if ([string]::IsNullOrWhiteSpace($RepoRoot)) {
    $RepoRoot = $PSScriptRoot
}
$RepoRoot = (Resolve-Path -LiteralPath $RepoRoot).Path
$MaxContentBytes = 2MB
$Violations = [System.Collections.Generic.List[string]]::new()
$TextExtensions = @(
    '.bat', '.cmd', '.conf', '.cfg', '.css', '.csv', '.html', '.ini', '.java',
    '.js', '.json', '.md', '.properties', '.ps1', '.psm1', '.py', '.sh', '.sql',
    '.svg', '.ts', '.txt', '.xml', '.yaml', '.yml'
)
$TextFileNames = @('CONTRIBUTING', 'LICENSE', 'NOTICE', 'README')
$UpstreamSourceRoots = @(
    'doc', 'extensions', 'guacamole', 'guacamole-common', 'guacamole-common-js',
    'guacamole-docker', 'guacamole-ext', 'src'
)

function Add-Violation {
    param(
        [Parameter(Mandatory)][string]$File,
        [Parameter(Mandatory)][string]$Rule
    )

    $entry = '{0}:{1}' -f $File, $Rule
    if (-not $Violations.Contains($entry)) {
        [void]$Violations.Add($entry)
    }
}

function Invoke-GitNames {
    param([Parameter(Mandatory)][string[]]$Arguments)

    $result = @(& git -C $RepoRoot @Arguments 2>$null)
    if ($LASTEXITCODE -ne 0) {
        throw "git command failed: $($Arguments -join ' ')"
    }
    return @($result | ForEach-Object { $_.ToString().Trim() } | Where-Object { $_ })
}

function Normalize-RelativePath {
    param([Parameter(Mandatory)][string]$Path)

    return (($Path -replace '\\', '/') -replace '^\./', '').TrimStart('/')
}

function Get-SourceRoot {
    param([Parameter(Mandatory)][string]$Path)

    $slash = Normalize-RelativePath -Path $Path
    $first = $slash.Split('/')[0]
    return $first
}

function Test-UpstreamSourcePath {
    param([Parameter(Mandatory)][string]$Path)

    return $UpstreamSourceRoots -contains (Get-SourceRoot -Path $Path)
}

function Test-ArtifactPath {
    param([Parameter(Mandatory)][string]$Path)

    $normalized = Normalize-RelativePath -Path $Path
    $leaf = [System.IO.Path]::GetFileName($normalized)
    $localPath = $normalized -match '^(?i)(runtime|deploy-local/(data|secrets))(/|$)'

    if ($normalized -match '^(?i)runtime(/|$)') {
        Add-Violation -File $normalized -Rule 'PATH_RUNTIME'
    }
    if ($normalized -match '^(?i)deploy-local/data(/|$)') {
        Add-Violation -File $normalized -Rule 'PATH_DEPLOY_DATA'
    }
    if ($normalized -match '^(?i)deploy-local/secrets(/|$)') {
        Add-Violation -File $normalized -Rule 'PATH_DEPLOY_SECRETS'
    }
    if ($normalized -match '(?i)(^|/)(backup|backups|archive|archives|logs|__pycache__|\.cache)(/|$)') {
        Add-Violation -File $normalized -Rule 'PATH_GENERATED_STATE'
    }
    if ($normalized -match '(?i)(\.vhdx|\.qcow2?|\.vmdk|\.vdi|\.iso|\.img|\.nvram|\.fd|\.tpm2?|\.swtpm|\.permall)$') {
        Add-Violation -File $normalized -Rule 'VM_ARTIFACT'
    }
    if ($normalized -match '(?i)(\.log|\.bak|\.backup|\.zip|\.tar|\.tgz|\.gz|\.bz2|\.xz|\.7z|\.rar|\.archive)$' -or
        $leaf -match '(?i)\.generated\.' -or
        ($leaf -match '(?i)(^|[-_.])(pid|marker)([-_.]|$)' -and -not (Test-UpstreamSourcePath -Path $normalized))) {
        Add-Violation -File $normalized -Rule 'GENERATED_ARTIFACT'
    }
    if ($normalized -match '(?i)(\.dump|\.pgdump|\.sqlite3?|\.db)$' -or
        ($leaf -match '(?i)(dump|backup|export|snapshot).*\.sql$' -and -not (Test-UpstreamSourcePath -Path $normalized))) {
        Add-Violation -File $normalized -Rule 'DATABASE_DUMP'
    }
    if ($normalized -match '(?i)(\.pyc|\.pyo)$' -or $normalized -match '(?i)(^|/)(__pycache__|\.cache)(/|$)') {
        Add-Violation -File $normalized -Rule 'PYTHON_CACHE'
    }
    if ($normalized -match '(?i)(^|/)(tpm|swtpm)(/|$)' -or $leaf -match '(?i)^tpm[-_.]' ) {
        Add-Violation -File $normalized -Rule 'TPM_STATE'
    }
    if ($normalized -match '(?i)(\.pem|\.key|\.p12|\.pfx)$' -or $leaf -match '(?i)(^|[-_.])(id_rsa|id_ed25519|private[-_]?key)([-_.]|$)') {
        Add-Violation -File $normalized -Rule 'PRIVATE_KEY'
    }
    $sensitiveLeaf = $leaf -match '(?i)(password|passwd|secret|credential|private[-_]?key|privatekey|passphrase|token)'
    if ($normalized -match '(?i)(^|/)(\.env(?:\.|$)|[^/]+\.env)$' -or
        ($sensitiveLeaf -and -not (Test-UpstreamSourcePath -Path $normalized))) {
        Add-Violation -File $normalized -Rule 'CREDENTIAL_PATH'
    }
    if ($normalized -match '(?i)(^|/)[^/]*inventory[^/]*\.(json|csv|txt|yaml|yml|xml)$') {
        Add-Violation -File $normalized -Rule 'GENERATED_INVENTORY'
    }

    return $localPath
}

function Get-TextContent {
    param([Parameter(Mandatory)][string]$RelativePath)

    $fullPath = Join-Path $RepoRoot (($RelativePath -replace '/', '\'))
    if (-not (Test-Path -LiteralPath $fullPath -PathType Leaf)) {
        return $null
    }
    $item = Get-Item -LiteralPath $fullPath -Force
    if ($item.Length -gt $MaxContentBytes) {
        return $null
    }
    $extension = [System.IO.Path]::GetExtension($item.Name).ToLowerInvariant()
    if ($TextExtensions -notcontains $extension -and $TextFileNames -notcontains $item.Name) {
        return $null
    }

    try {
        $bytes = [System.IO.File]::ReadAllBytes($fullPath)
        if ([Array]::IndexOf($bytes, [byte]0) -ge 0) {
            return $null
        }
        return [System.Text.Encoding]::UTF8.GetString($bytes)
    } catch {
        return $null
    }
}

$tracked = Invoke-GitNames -Arguments @('ls-files')
$staged = Invoke-GitNames -Arguments @('diff', '--cached', '--name-only', '--diff-filter=ACMR')
$unignored = Invoke-GitNames -Arguments @('ls-files', '--others', '--exclude-standard')
$allNames = @()
$allNames += @($tracked)
$allNames += @($staged)
$allNames += @($unignored)
$candidatePaths = @($allNames | ForEach-Object { Normalize-RelativePath -Path $_ } | Sort-Object -Unique)

foreach ($path in $candidatePaths) {
    $localPath = Test-ArtifactPath -Path $path
    if ($localPath) {
        continue
    }

    $content = Get-TextContent -RelativePath $path
    if ($null -eq $content) {
        continue
    }
    if ($content -match '(?i)https?://[a-z0-9][a-z0-9-]{2,}\.(trycloudflare\.com|ngrok(?:-free)?\.app|ngrok\.io)(?::\d+)?(?:[/\?#][^\s"''<>)]*)?') {
        Add-Violation -File $path -Rule 'CONTENT_TUNNEL_URL'
    }
    if ($content -match '(?i)-----BEGIN (?:RSA |OPENSSH |EC |DSA |PGP )?PRIVATE KEY-----') {
        Add-Violation -File $path -Rule 'CONTENT_PRIVATE_KEY'
    }
    if ($content -match '(?i)\b(?:postgres(?:ql)?|mysql|mssql|mongodb(?:\+srv)?)://[^\s"''<>]+') {
        Add-Violation -File $path -Rule 'CONTENT_DATABASE_URL'
    }
    $extension = [System.IO.Path]::GetExtension($path).ToLowerInvariant()
    $configurationExtension = @('.conf', '.cfg', '.ini', '.json', '.properties', '.txt', '.xml', '.yaml', '.yml') -contains $extension
    if ($configurationExtension -and -not (Test-UpstreamSourcePath -Path $path) -and
        $content -match '(?im)^\s*(?:password|passwd|secret|token|api[_-]?key)\s*[:=]\s*["'']?[A-Za-z0-9+/=_-]{16,}') {
        Add-Violation -File $path -Rule 'CONTENT_CREDENTIAL_ASSIGNMENT'
    }
}

$originResult = @()
$originExitCode = 0
$previousErrorActionPreference = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
$originResult = @(& git -C $RepoRoot remote get-url origin 2>$null)
$originExitCode = $LASTEXITCODE
$ErrorActionPreference = $previousErrorActionPreference
if ($originExitCode -ne 0 -or @($originResult).Count -eq 0) {
    Add-Violation -File 'origin' -Rule 'ORIGIN_UNAVAILABLE'
} else {
    $origin = ($originResult -join '').Trim()
    $upstream = $origin -match '(?i)(?:github\.com[:/]apache/guacamole-client)(?:\.git)?/?$'
    if ($upstream) {
        Add-Violation -File 'origin' -Rule 'UPSTREAM_ORIGIN_FORBIDDEN'
    }
}

foreach ($violation in @($Violations | Sort-Object -Unique)) {
    Write-Output $violation
}
if ($Violations.Count -gt 0) {
    exit 1
}
exit 0
