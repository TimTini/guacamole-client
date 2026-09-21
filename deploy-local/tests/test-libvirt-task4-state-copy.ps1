[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$testName = ".task4-state-copy-test-$PID"
$testRoot = Join-Path $RepoRoot "runtime\backups\$testName"
$sourceRoot = Join-Path $testRoot 'source'
$targetRoot = Join-Path $testRoot 'checkpoint'
$wslRoot = '/mnt/h/RemoteWorkspaces/guacamole-client'
$sourceWslRoot = "$wslRoot/runtime/backups/$testName/source"
$targetWslRoot = "$wslRoot/runtime/backups/$testName/checkpoint"

function Invoke-WslScript {
    param([Parameter(Mandatory)][string]$Script)

    $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Script))
    $output = @(& wsl.exe -d Ubuntu-24.04 -u root -- sh -lc "printf '%s' '$encoded' | base64 -d | sh" 2>&1)
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        throw ("WSL test script failed with exit code {0}: {1}" -f $exitCode, ($output -join [Environment]::NewLine))
    }
    return ($output -join [Environment]::NewLine)
}

try {
    New-Item -ItemType Directory -Force -Path (Join-Path $sourceRoot 'tpm') | Out-Null
    Set-Content -LiteralPath (Join-Path $sourceRoot 'OVMF_VARS_4M.ms.fd') -Value 'fixture nvram' -Encoding ASCII
    Set-Content -LiteralPath (Join-Path $sourceRoot 'tpm\state.bin') -Value 'fixture tpm state' -Encoding ASCII

    $stateCopyScript = @'
set -eu
checkpoint='__CHECKPOINT__'
mkdir -p "$checkpoint/tpm"
cp --reflink=auto '__SOURCE__/OVMF_VARS_4M.ms.fd' "$checkpoint/OVMF_VARS_4M.ms.fd"
cp -a '__SOURCE__/tpm/.' "$checkpoint/tpm/"
(
  cd "$checkpoint"
  find tpm -type f -print0 | sort -z | xargs -0 sha256sum
  sha256sum OVMF_VARS_4M.ms.fd
) > "$checkpoint/state-sha256sums.txt"
'@
    $stateCopyScript = $stateCopyScript.Replace('__CHECKPOINT__', $targetWslRoot)
    $stateCopyScript = $stateCopyScript.Replace('__SOURCE__', $sourceWslRoot)
    $stateCopyScript = (($stateCopyScript -split [Environment]::NewLine |
        ForEach-Object { $_.Trim() } |
        Where-Object { $_ }) -join ' ')

    $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($stateCopyScript))
    $syntaxOutput = @(& wsl.exe -d Ubuntu-24.04 -u root -- sh -lc "printf '%s' '$encoded' | base64 -d | sh -n" 2>&1)
    if ($LASTEXITCODE -ne 0) {
        throw "TASK4_STATE_COPY_SHELL_SYNTAX_FAILED: $($syntaxOutput -join [Environment]::NewLine)"
    }
    Invoke-WslScript -Script $stateCopyScript | Out-Null

    if (-not (Test-Path -LiteralPath (Join-Path $targetRoot 'OVMF_VARS_4M.ms.fd') -PathType Leaf)) {
        throw 'TASK4_STATE_COPY_NVRAM_MISSING'
    }
    if (-not (Test-Path -LiteralPath (Join-Path $targetRoot 'tpm\state.bin') -PathType Leaf)) {
        throw 'TASK4_STATE_COPY_TPM_MISSING'
    }
    $hashManifest = Join-Path $targetRoot 'state-sha256sums.txt'
    if (-not (Test-Path -LiteralPath $hashManifest -PathType Leaf) -or
        @(Get-Content -LiteralPath $hashManifest).Count -ne 2) {
        throw 'TASK4_STATE_COPY_HASH_MANIFEST_INVALID'
    }
    $manifestText = Get-Content -LiteralPath $hashManifest -Raw
    if ($manifestText -match [regex]::Escape($targetWslRoot) -or $manifestText -match '\.incomplete') {
        throw 'TASK4_STATE_COPY_MANIFEST_PATH_NOT_RELATIVE'
    }
    $verify = Invoke-WslScript -Script "cd '$targetWslRoot' && sha256sum -c state-sha256sums.txt"
    if ($verify -notmatch '(?m)^(tpm/|OVMF_VARS_4M\.ms\.fd: OK)') {
        throw "TASK4_STATE_COPY_HASH_VERIFY_FAILED: $verify"
    }

    Write-Host 'TASK4_STATE_COPY_OK'
} finally {
    if (Test-Path -LiteralPath $testRoot) {
        Remove-Item -LiteralPath $testRoot -Recurse -Force
    }
}
