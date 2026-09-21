[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('start', 'status', 'stop')]
    [string]$Action = 'start'
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$SecretPath = Join-Path $Root 'secrets\postgres_password.txt'
$InitSqlPath = Join-Path $Root 'data\postgres-init\001-guacamole.sql'
$WslDistro = 'Ubuntu-24.04'
$WslRoot = '/mnt/h/RemoteWorkspaces/guacamole-client/deploy-local'
$PostgresVolume = 'guacamole-local-postgres-data'
$GuacamoleVersion = '1.6.0'
$RepoRoot = (Resolve-Path (Join-Path $Root '..')).Path

function Invoke-WslCommand {
    param([Parameter(Mandatory)][string]$Command)

    & wsl.exe -d $WslDistro -u root -- sh -lc "cd '$WslRoot' && $Command"
    if ($LASTEXITCODE -ne 0) {
        throw "WSL command failed with exit code $LASTEXITCODE."
    }
}

function Assert-DockerReady {
    Invoke-WslCommand 'docker info >/dev/null'
}

function Ensure-Directories {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $SecretPath), (Split-Path -Parent $InitSqlPath) | Out-Null
    Invoke-WslCommand "mkdir -p '$WslRoot/secrets' '$WslRoot/data/postgres-init'"
}

function Ensure-Secret {
    $existingSecret = Get-Item -LiteralPath $SecretPath -Force -ErrorAction SilentlyContinue
    if ($null -ne $existingSecret -and $existingSecret.Length -gt 0) {
        return
    }
    if ($null -ne $existingSecret) {
        throw "The database secret at '$SecretPath' is empty. Restore it from the H: backup or remove the empty file before starting."
    }

    $volumeExists = & wsl.exe -d $WslDistro -u root -- sh -lc "docker volume inspect '$PostgresVolume' >/dev/null 2>&1 && printf exists || printf missing"
    if ($LASTEXITCODE -ne 0) {
        throw 'Could not inspect the PostgreSQL Docker volume in WSL.'
    }
    if (($volumeExists -join '').Trim() -eq 'exists') {
        throw "The PostgreSQL volume '$PostgresVolume' exists, but '$SecretPath' is missing. Restore the secret from the H: backup before starting; a new password would not match the existing database."
    }

    Invoke-WslCommand "umask 077; openssl rand -hex 32 | tr -d '\n' > '$WslRoot/secrets/postgres_password.txt'; test -s '$WslRoot/secrets/postgres_password.txt'; chmod 600 '$WslRoot/secrets/postgres_password.txt'"
    Write-Host "Created the local database secret at $SecretPath"
}

function Ensure-InitSql {
    $existingInitSql = Get-Item -LiteralPath $InitSqlPath -Force -ErrorAction SilentlyContinue
    if ($null -ne $existingInitSql -and $existingInitSql.Length -gt 0) {
        return
    }

    $tempSqlPath = "$WslRoot/data/postgres-init/001-guacamole.sql.tmp"
    Write-Host 'Generating the Guacamole PostgreSQL schema from the pinned image...'
    Invoke-WslCommand "set -eu; rm -f '$tempSqlPath'; docker run --rm 'guacamole/guacamole:$GuacamoleVersion' /opt/guacamole/bin/initdb.sh --postgresql > '$tempSqlPath'; test -s '$tempSqlPath'; mv '$tempSqlPath' '$WslRoot/data/postgres-init/001-guacamole.sql'; chmod 644 '$WslRoot/data/postgres-init/001-guacamole.sql'"
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

function Protect-SecretFiles {
    $secretDirectory = Join-Path $Root 'secrets'
    if (Test-Path -LiteralPath $secretDirectory -PathType Container) {
        Get-ChildItem -LiteralPath $secretDirectory -File -Filter '*.txt' |
            ForEach-Object { Protect-SecretFile -Path $_.FullName }
    }

    $vmPasswordPath = Join-Path $RepoRoot 'runtime\vm-demo\vm-password.txt'
    Protect-SecretFile -Path $vmPasswordPath
}

function Test-PostgresDataInitialized {
    $mountPoint = @(& wsl.exe -d $WslDistro -u root -- sh -lc "docker volume inspect --format '{{.Mountpoint}}' '$PostgresVolume' 2>/dev/null")
    if ($LASTEXITCODE -ne 0 -or $mountPoint.Count -eq 0) {
        return $false
    }

    $mountPoint = ($mountPoint | Select-Object -Last 1).ToString().Trim()
    if ([string]::IsNullOrWhiteSpace($mountPoint)) {
        return $false
    }

    & wsl.exe -d $WslDistro -u root -- sh -lc "test -s '$mountPoint/PG_VERSION'"
    return $LASTEXITCODE -eq 0
}

function Invoke-Compose {
    param([Parameter(Mandatory)][string]$Arguments)

    Invoke-WslCommand "docker compose --project-directory '$WslRoot' --file '$WslRoot/compose.yaml' $Arguments"
}

switch ($Action) {
    'start' {
        Assert-DockerReady
        Ensure-Directories
        Ensure-Secret
        Protect-SecretFiles
        $postgresDataWasInitialized = Test-PostgresDataInitialized
        Ensure-InitSql
        Invoke-Compose 'up --detach'
        Write-Host 'Guacamole is available at http://127.0.0.1:8080/guacamole/'
        if ($postgresDataWasInitialized) {
            Write-Host 'The existing PostgreSQL database was retained; use the credentials already configured.'
        } else {
            Write-Host 'A new PostgreSQL database was initialized. The initial login is guacadmin / guacadmin; change it immediately.'
        }
    }
    'status' {
        if (-not (Test-Path -LiteralPath $SecretPath -PathType Leaf)) {
            Write-Host 'Guacamole has not been initialized yet; no local database secret exists.'
            return
        }
        Invoke-Compose 'ps'
    }
    'stop' {
        if (-not (Test-Path -LiteralPath $SecretPath -PathType Leaf)) {
            Write-Host 'Guacamole has not been initialized yet; nothing to stop.'
            return
        }
        Invoke-Compose 'stop'
    }
}
