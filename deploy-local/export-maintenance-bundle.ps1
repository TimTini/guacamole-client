[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$ScriptRoot = $PSScriptRoot
$RepoRoot = (Resolve-Path (Join-Path $ScriptRoot '..')).Path
$RuntimeRoot = Join-Path $RepoRoot 'runtime'
$BackupRoot = Join-Path $RuntimeRoot 'backups'
$StagingRoot = Join-Path $BackupRoot '.maintenance-bundle-staging'
$ArchivePath = Join-Path $BackupRoot ("guacamole-maintenance-{0}.zip" -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
$SourceRoot = Join-Path $RepoRoot 'deploy-local'
# The Windows credential lives in the WSL ext4 VHDX at
# /var/lib/guacamole-workspace/secrets and never enters this export.

if (-not (Test-Path -LiteralPath $SourceRoot -PathType Container)) {
    throw "The local deployment toolkit was not found: $SourceRoot"
}

New-Item -ItemType Directory -Force -Path $BackupRoot | Out-Null
if (Test-Path -LiteralPath $StagingRoot) {
    Remove-Item -LiteralPath $StagingRoot -Recurse -Force
}
$StagedDeployRoot = Join-Path $StagingRoot 'deploy-local'
New-Item -ItemType Directory -Force -Path $StagedDeployRoot | Out-Null

try {
    $files = Get-ChildItem -LiteralPath $SourceRoot -File -Recurse |
        Where-Object {
            $relative = $_.FullName.Substring($SourceRoot.Length).TrimStart('\')
            $excludedDirectory = $relative -match '^(?i)(data|secrets|runtime)(\\|$)' -or
                $relative -match '^(?i)libvirt[\\]exports(\\|$)' -or
                $relative -match '(?i)(^|[\\])runtime([\\]|$)' -or
                $relative -match '(?i)(^|[\\])(__pycache__|\.cache|logs?|backups?|archives?)([\\]|$)'
            $excludedName = $_.Name -match '(?i)(password|secret|token|credential|private[-_]?key|passphrase|\.env|inventory|generated)'
            $excludedPayload = $_.Extension -match '(?i)^\.(qcow2|vhdx|vmdk|vdi|raw|img|fd|nvram|tpm2?|swtpm|sqlite|db|dump|pgdump|pyc|pyo|log|pid|marker|bak|backup|zip|tar|tgz|gz|bz2|xz|7z|rar|archive)$'
            $excludedGeneratedLibvirt = $relative -match '^(?i)libvirt[\\][^\\]+\.generated\.xml$'
            (-not $excludedDirectory) -and (-not $excludedName) -and (-not $excludedPayload) -and (-not $excludedGeneratedLibvirt)
        }

    if (@($files).Count -eq 0) {
        throw "No deployment toolkit files were found under $SourceRoot"
    }

    foreach ($file in $files) {
        $relative = $file.FullName.Substring($SourceRoot.Length).TrimStart('\')
        $destination = Join-Path $StagedDeployRoot $relative
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $destination) | Out-Null
        Copy-Item -LiteralPath $file.FullName -Destination $destination
    }

    Compress-Archive -Path $StagedDeployRoot -DestinationPath $ArchivePath -CompressionLevel Optimal
    if (-not (Test-Path -LiteralPath $ArchivePath -PathType Leaf)) {
        throw "The maintenance archive was not created: $ArchivePath"
    }

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [System.IO.Compression.ZipFile]::OpenRead($ArchivePath)
    try {
        $entries = @($archive.Entries | ForEach-Object { $_.FullName })
    } finally {
        $archive.Dispose()
    }

    $forbidden = @($entries | Where-Object {
        $_ -match '(?i)(^|/)(data|secrets|runtime|__pycache__|\.cache|logs?|backups?|archives?)(/|$)' -or
            $_ -match '(?i)(password|secret|token|credential|private[-_]?key|passphrase|inventory|\.env|generated)' -or
            $_ -match '(?i)\.(qcow2|vhdx|vmdk|vdi|raw|img|fd|nvram|tpm2?|swtpm|sqlite|db|dump|pgdump|pyc|pyo|log|pid|marker|bak|backup|zip|tar|tgz|gz|bz2|xz|7z|rar|archive)$' -or
            $_ -match '(?i)(^|/)libvirt/exports(/|$)' -or
            $_ -match '(?i)(^|/)libvirt/[^/]+\.generated\.xml$'
    })
    if ($forbidden.Count -gt 0) {
        throw "The maintenance archive contains forbidden paths: $($forbidden -join ', ')"
    }

    Write-Host "Created maintenance bundle: $ArchivePath"
    Write-Host "Files archived: $($entries.Count)"
    Write-Host 'Secrets, generated database data, and runtime state were excluded.'
} finally {
    if (Test-Path -LiteralPath $StagingRoot) {
        Remove-Item -LiteralPath $StagingRoot -Recurse -Force
    }
}
