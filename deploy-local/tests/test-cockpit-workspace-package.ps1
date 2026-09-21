[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$PackageRoot = Join-Path $RepoRoot 'deploy-local\cockpit\workspace_templates'
$ManifestPath = Join-Path $PackageRoot 'manifest.json'
$HtmlPath = Join-Path $PackageRoot 'index.html'
$JsPath = Join-Path $PackageRoot 'workspace-templates.js'
$CssPath = Join-Path $PackageRoot 'workspace-templates.css'
$LibvirtPath = Join-Path $RepoRoot 'deploy-local\libvirt.ps1'
$InitializerPath = Join-Path $RepoRoot 'deploy-local\initialize-windows-auth.ps1'

function Assert-Test {
    param(
        [Parameter(Mandatory)][bool]$Condition,
        [Parameter(Mandatory)][string]$Message
    )
    if (-not $Condition) {
        throw "TASK5_ASSERTION_FAILED: $Message"
    }
}

function Invoke-BoundedExternal {
    param(
        [Parameter(Mandatory)][string]$FilePath,
        [Parameter(Mandatory)][string[]]$Arguments,
        [int]$TimeoutSeconds = 15
    )
    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $FilePath
    $startInfo.UseShellExecute = $false
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    if ($null -ne $startInfo.PSObject.Properties['ArgumentList']) {
        foreach ($argument in $Arguments) {
            [void]$startInfo.ArgumentList.Add($argument)
        }
    } else {
        # Windows PowerShell/.NET Framework has no ArgumentList collection;
        # quote the bounded argv explicitly for ProcessStartInfo.Arguments.
        $startInfo.Arguments = (($Arguments | ForEach-Object {
            if ($_ -match '\s') { '"' + $_ + '"' } else { $_ }
        }) -join ' ')
    }
    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    if (-not $process.Start()) {
        throw "Could not start bounded process: $FilePath"
    }
    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
        try { $process.Kill($true) } catch { }
        [void]$process.WaitForExit(2000)
        return [pscustomobject]@{
            ExitCode = 124
            TimedOut = $true
            Output = (($stdoutTask.GetAwaiter().GetResult()) + "`n" + ($stderrTask.GetAwaiter().GetResult()))
        }
    }
    return [pscustomobject]@{
        ExitCode = $process.ExitCode
        TimedOut = $false
        Output = (($stdoutTask.GetAwaiter().GetResult()) + "`n" + ($stderrTask.GetAwaiter().GetResult()))
    }
}

foreach ($path in @($ManifestPath, $HtmlPath, $JsPath, $CssPath, $LibvirtPath, $InitializerPath)) {
    Assert-Test -Condition (Test-Path -LiteralPath $path -PathType Leaf) -Message "missing file '$path'"
}

$manifest = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
$menu = $manifest.menu.'workspace-templates'
Assert-Test -Condition ($manifest.version -eq 1) -Message 'manifest version must be 1'
Assert-Test -Condition ($manifest.requires.cockpit -eq '314') -Message 'Cockpit 314 is required'
Assert-Test -Condition ($menu.label -eq 'Workspace Templates') -Message 'workspace menu label is incorrect'
Assert-Test -Condition ($menu.path -eq 'index.html') -Message 'workspace menu path is incorrect'
Assert-Test -Condition ($menu.order -eq 55) -Message 'workspace menu order is incorrect'

$html = Get-Content -LiteralPath $HtmlPath -Raw
$js = Get-Content -LiteralPath $JsPath -Raw
$css = Get-Content -LiteralPath $CssPath -Raw
$libvirt = Get-Content -LiteralPath $LibvirtPath -Raw
$initializer = Get-Content -LiteralPath $InitializerPath -Raw

Assert-Test -Condition ($html -match '<script\s+src=["'']\.\.\/base1\/cockpit\.js["'']') -Message 'Cockpit base script is missing'
Assert-Test -Condition ($html -match 'id=["'']create-form["'']') -Message 'create form is missing'
Assert-Test -Condition ($html -match '<select[^>]+id=["'']template["'']') -Message 'template select is missing'
Assert-Test -Condition ($html -match '<select[^>]+id=["'']assignee["'']') -Message 'assignee select is missing'
Assert-Test -Condition ($html -match 'id=["'']stage["'']') -Message 'stage output is missing'
Assert-Test -Condition ($html -match 'id=["'']result-links["'']') -Message 'result links container is missing'
Assert-Test -Condition ($html -match 'id=["'']template-list["'']') -Message 'template detail list is missing'
Assert-Test -Condition ($html -match 'id=["'']recent-list["'']') -Message 'recent workspace list is missing'
Assert-Test -Condition ($css -match '\.form-grid|#create-form') -Message 'package CSS is empty'

Assert-Test -Condition ($js -match 'cockpit\.spawn') -Message 'Cockpit spawn is missing'
Assert-Test -Condition ($js -match '["'']start["'']') -Message 'detached start command is missing'
Assert-Test -Condition ($js -match '["'']repair["'']') -Message 'repair command is missing'
Assert-Test -Condition ($js -match 'superuser:\s*["'']require["'']') -Message 'Cockpit superuser boundary is missing'
Assert-Test -Condition ($js -match 'err:\s*["'']message["'']') -Message 'Cockpit error mode is missing'
Assert-Test -Condition ($js -match 'cockpit\.spawn\(\s*\[HELPER,\s*["'']list["''],\s*["'']--json["'']\]') -Message 'list argv is not fixed and explicit'
Assert-Test -Condition ($js -match '["'']start["'']') -Message 'start command is missing'
foreach ($argument in @('--name', '--assign-user', '--template', '--memory-mib', '--vcpus', '--json')) {
    Assert-Test -Condition ($js -match [regex]::Escape($argument)) -Message "clone argument '$argument' is missing"
}
Assert-Test -Condition ($js -match 'JSON\.parse') -Message 'helper JSON is not parsed'
Assert-Test -Condition ($js -match 'createElement\(["'']option["'']\)|appendChild') -Message 'select options are not created from helper data'
Assert-Test -Condition ($js -match 'guacamoleAssignees|assignees') -Message 'assignee options are not sourced from helper JSON'
Assert-Test -Condition ($js -match 'templates') -Message 'template options are not sourced from helper JSON'
Assert-Test -Condition ($js -match 'groups') -Message 'group options are not supported'
Assert-Test -Condition ($js -match '--assign-group') -Message 'group argv mapping is missing'
Assert-Test -Condition ($js -match 'parseAssignee') -Message 'assignee type is not preserved through submit'
Assert-Test -Condition ($js -match 'textContent') -Message 'user-facing values are not rendered with textContent'
Assert-Test -Condition ($js -match '\.disabled\s*=\s*true') -Message 'submit is not disabled while running'
Assert-Test -Condition ($js -match 'status\s*===?\s*["'']ready["'']') -Message 'ready status gate is missing'
Assert-Test -Condition ($js -match 'progress|PROGRESS_STAGES') -Message 'stage progress rendering is missing'
Assert-Test -Condition ($js -match 'setTimeout|refreshWorkspaceList') -Message 'active job polling is missing'
Assert-Test -Condition ($js -match 'resultDetails|IP address|Assignee') -Message 'result details are missing'
Assert-Test -Condition ($js -match 'SAFE_ERROR_MESSAGES|SPAWN_FAILED') -Message 'spawn and durable error sanitization is missing'
Assert-Test -Condition ($js -match 'JOB_STALE|stale') -Message 'stale job repair state is missing'
Assert-Test -Condition ($js -notmatch 'reason\.message') -Message 'raw spawn rejection text must not be rendered'
Assert-Test -Condition ($js -match '/machines/?') -Message 'Machines result link is missing'
Assert-Test -Condition ($js -match '/guacamole/') -Message 'Guacamole result link is missing'
Assert-Test -Condition ($js -notmatch 'sh\s+-c|bash\s+-c|eval\(|innerHTML\s*=') -Message 'unsafe shell/eval/HTML sink found'
Assert-Test -Condition ($js -notmatch 'cockpit\.spawn\(\s*["'']') -Message 'spawn must receive argv arrays'

Assert-Test -Condition ($libvirt -match "ValidateSet\([^\)]*'cockpit-workspaces'") -Message 'cockpit-workspaces action is not exposed'
Assert-Test -Condition ($libvirt -match 'function Install-CockpitWorkspaces') -Message 'Cockpit package installer is missing'
Assert-Test -Condition ($libvirt -match '/usr/local/share/cockpit/workspace_templates') -Message 'Cockpit package target is missing'
Assert-Test -Condition ($libvirt -match '/usr/local/libexec/guacamole-workspace-helper') -Message 'helper target is missing'
Assert-Test -Condition ($libvirt -match '/usr/local/libexec/guacamole-workspace') -Message 'self-contained helper bundle target is missing'
Assert-Test -Condition ($libvirt -match 'compose.yaml') -Message 'Compose asset staging is missing'
Assert-Test -Condition ($libvirt -match 'windows-clone\.xml\.template') -Message 'clone XML asset staging is missing'
Assert-Test -Condition ($libvirt -match 'source-tpm-state\.path') -Message 'source TPM path contract is missing'
Assert-Test -Condition ($libvirt -match '/var/lib/guacamole-workspaces/jobs') -Message 'workspace job status path is missing'
Assert-Test -Condition ($libvirt -match 'stage\.\$\$|\.stage') -Message 'atomic staging path is missing'
Assert-Test -Condition ($libvirt -match 'mv -T|mv -Tf') -Message 'atomic swap is missing'
Assert-Test -Condition ($libvirt -match 'guacamole-workspace-release\.current') -Message 'single release pointer is missing'
Assert-Test -Condition ($libvirt -match 'sha256sum.*cache_key|cache_key=.*sha256sum') -Message 'content-derived cache key is missing'
Assert-Test -Condition ($libvirt -match 'release_stage.*package|release_stage.*bundle') -Message 'coherent release root staging is missing'
Assert-Test -Condition ($libvirt -match 'release_stage/RELEASE|release=.*release_id') -Message 'release manifest is missing'
Assert-Test -Condition ($libvirt -match 'find /usr/local/share/cockpit|workspace_templates\.v') -Message 'stale package cleanup is missing'
Assert-Test -Condition ($libvirt -match 'cockpit-bridge --packages') -Message 'Cockpit package discovery check is missing'
Assert-Test -Condition ($libvirt -match 'python3 -m py_compile') -Message 'staged helper validation is missing'
Assert-Test -Condition ($libvirt -match 'set-windows-credential') -Message 'installed helper credential command is missing'
Assert-Test -Condition ($libvirt -match '/var/lib/guacamole-workspace/secrets/windows11_guacadmin_password') -Message 'canonical credential path publication check is missing'
Assert-Test -Condition ($libvirt -notmatch '/mnt/h/RemoteWorkspaces/guacamole-client/runtime/secrets') -Message 'DrvFs credential path must not be published'
Assert-Test -Condition ($libvirt -match 'initializer_source|initialize-windows-auth\.ps1') -Message 'initializer asset is not validated or staged'
Assert-Test -Condition ($libvirt -match "secret_root='/var/lib/guacamole-workspace/secrets'") -Message 'canonical secret root install check is missing'
Assert-Test -Condition ($libvirt -match 'stat -c ''%U:%G:%a:%F'' "\$secret_root"') -Message 'canonical secret root ownership/mode verification is missing'
Assert-Test -Condition ($libvirt -match 'WORKSPACE_SECRET_ROOT_UNSAFE') -Message 'unsafe secret root must fail closed with a diagnostic'
Assert-Test -Condition ($initializer -match '/usr/local/libexec/guacamole-workspace-helper') -Message 'initializer must target installed worker release'
Assert-Test -Condition ($initializer -match '/var/lib/guacamole-workspace/secrets/windows11_guacadmin_password') -Message 'initializer must document the canonical ext4 secret path'
Assert-Test -Condition ($initializer -notmatch 'windows-helper-secret|GUACAMOLE_WINDOWS_CREDENTIAL_SECRET') -Message 'initializer exposes arbitrary secret path override'
Assert-Test -Condition ($libvirt -match "'cockpit-workspaces'\s*\{\s*Assert-WslAndHStorage;\s*Install-CockpitWorkspaces\s*\}") -Message 'cockpit-workspaces switch branch is missing'
Assert-Test -Condition ($libvirt -notmatch '/usr/share/cockpit/machines') -Message 'vendor Cockpit machines package must not be modified'
Assert-Test -Condition ($libvirt -match "'start'\s*\{\s*Assert-WslAndHStorage;\s*Invoke-LibvirtStart\s*\}") -Message 'existing start action was changed'
Assert-Test -Condition ($libvirt -match "'stop'\s*\{\s*Assert-WslAndHStorage;\s*Invoke-LibvirtStop\s*\}") -Message 'existing stop action was changed'

$listFixture = @'
{"ok":true,"templates":[{"version":"windows11-v1","createdAt":"2026-09-20T00:00:00Z","virtualSize":123}],"clones":[],"guacamoleAssignees":{"users":[{"type":"USER","name":"alice","label":"alice"}],"groups":[{"type":"USER_GROUP","name":"developers","label":"developers"}]}}
'@
$listPayload = $listFixture | ConvertFrom-Json
Assert-Test -Condition ($listPayload.templates[0].version -eq 'windows11-v1') -Message 'template fixture contract is invalid'
Assert-Test -Condition ($listPayload.guacamoleAssignees.users[0].type -eq 'USER') -Message 'user fixture contract is invalid'
Assert-Test -Condition ($listPayload.guacamoleAssignees.groups[0].type -eq 'USER_GROUP') -Message 'group fixture contract is invalid'

$readyFixture = @'
{"ok":true,"status":"ready","stage":"permissions","clone":{"name":"vm-new","ip":"192.168.250.21","assigneeType":"USER_GROUP","assigneeName":"developers"},"progress":[{"stage":"validation","status":"ready"},{"stage":"disk-overlay","status":"ready"},{"stage":"domain","status":"ready"},{"stage":"dhcp","status":"ready"},{"stage":"rdp","status":"ready"},{"stage":"guacamole","status":"ready"},{"stage":"permissions","status":"ready"}]}
'@
$readyPayload = $readyFixture | ConvertFrom-Json
Assert-Test -Condition ($readyPayload.ok -and $readyPayload.status -eq 'ready') -Message 'ready fixture contract is invalid'
Assert-Test -Condition ($readyPayload.clone.ip -eq '192.168.250.21') -Message 'ready result IP is missing'
Assert-Test -Condition ($readyPayload.progress.Count -eq 7) -Message 'ready progress fixture is incomplete'

Assert-Test -Condition (Test-Path -LiteralPath (Join-Path $RepoRoot 'deploy-local\compose.yaml') -PathType Leaf) -Message 'source Compose asset is missing'
Assert-Test -Condition (Test-Path -LiteralPath (Join-Path $RepoRoot 'deploy-local\libvirt\domains\windows-clone.xml.template') -PathType Leaf) -Message 'source clone XML asset is missing'
$node = Get-Command node.exe -ErrorAction SilentlyContinue
$behaviorTestPath = Join-Path $PSScriptRoot 'test-cockpit-workspace-package.js'
Assert-Test -Condition (Test-Path -LiteralPath $behaviorTestPath -PathType Leaf) -Message 'UI behavior test is missing'
if ($null -ne $node) {
    & $node.Source $behaviorTestPath
    Assert-Test -Condition ($LASTEXITCODE -eq 0) -Message 'UI behavior test failed'
}

$wsl = Get-Command wsl.exe -ErrorAction SilentlyContinue
$wslPath = if ($null -ne $wsl) { [string]$wsl.Source } else { '' }
if ([string]::IsNullOrWhiteSpace($wslPath) -and $null -ne $wsl) { $wslPath = [string]$wsl.Path }
if (-not [string]::IsNullOrWhiteSpace($wslPath)) {
    $installedBundleResult = Invoke-BoundedExternal -FilePath $wslPath -Arguments @('-d', 'Ubuntu-24.04', '-u', 'root', '--', 'sh', '-lc', 'test -s /usr/local/libexec/guacamole-workspace/compose.yaml && test -s /usr/local/libexec/guacamole-workspace/libvirt/domains/windows-clone.xml.template')
    if ($installedBundleResult.TimedOut) {
        throw "Installed bundle probe timed out after 15 seconds: $($installedBundleResult.Output)"
    }
    $installedBundleStale = $false
    if ($installedBundleResult.ExitCode -eq 0) {
        $sourceHelperHash = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $RepoRoot 'deploy-local\workspace-helper.py')).Hash.ToLowerInvariant()
        $installedHashResult = Invoke-BoundedExternal -FilePath $wslPath -Arguments @('-d', 'Ubuntu-24.04', '-u', 'root', '--', 'sha256sum', '/usr/local/libexec/guacamole-workspace-release.current/bundle/workspace-helper.py')
        $installedHash = @($installedHashResult.Output -split "`r?`n") | Where-Object { $_ -match '^([0-9a-fA-F]{64})\s+' } | Select-Object -First 1
        if ($installedHashResult.TimedOut -or $installedHashResult.ExitCode -ne 0 -or [string]::IsNullOrWhiteSpace($installedHash) -or $installedHash.Split(' ')[0].ToLowerInvariant() -ne $sourceHelperHash) {
            $installedBundleStale = $true
            Write-Host ('TASK5_LIVE_INSTALL_CHECK_SKIPPED: installed worker is not the current source release; rerun libvirt.ps1 cockpit-workspaces. ' + ($installedHashResult.Output -join ' '))
        }
    }
    if ($installedBundleResult.ExitCode -eq 0 -and -not $installedBundleStale) {
        $listOutput = @()
        $listResult = $null
        for ($attempt = 1; $attempt -le 3; $attempt++) {
            $listResult = Invoke-BoundedExternal -FilePath $wslPath -Arguments @('-d', 'Ubuntu-24.04', '-u', 'root', '--', '/usr/local/libexec/guacamole-workspace-helper', 'list', '--json')
            $listOutput = @($listResult.Output -split "`r?`n")
            if ($listResult.ExitCode -eq 0 -and -not $listResult.TimedOut) { break }
            Start-Sleep -Seconds 1
        }
        Assert-Test -Condition ($null -ne $listResult -and $listResult.ExitCode -eq 0 -and -not $listResult.TimedOut) -Message ('installed helper list failed or timed out: ' + ($listOutput -join ' '))
        $listJsonLine = $listOutput | Where-Object { $_ -match '^\s*\{' } | Select-Object -Last 1
        Assert-Test -Condition (-not [string]::IsNullOrWhiteSpace($listJsonLine)) -Message 'installed helper did not return JSON'
        $installedList = $listJsonLine | ConvertFrom-Json
        Assert-Test -Condition ($null -ne $installedList.guacamoleAssignees.users) -Message 'installed list omitted users'
        Assert-Test -Condition ($null -ne $installedList.guacamoleAssignees.groups) -Message 'installed list omitted groups'
        $credentialHelpResult = Invoke-BoundedExternal -FilePath $wslPath -Arguments @('-d', 'Ubuntu-24.04', '-u', 'root', '--', '/usr/local/libexec/guacamole-workspace-helper', '--help')
        $credentialHelp = $credentialHelpResult.Output -join "`n"
        Assert-Test -Condition (-not $credentialHelpResult.TimedOut -and $credentialHelpResult.ExitCode -eq 0 -and $credentialHelp -match 'set-windows-credential' -and $credentialHelp -match 'adopt-guacamole-connection') -Message 'installed helper credential/adoption commands are missing'
        $initializerResult = Invoke-BoundedExternal -FilePath $wslPath -Arguments @('-d', 'Ubuntu-24.04', '-u', 'root', '--', 'test', '-s', '/usr/local/libexec/guacamole-workspace-release.current/bundle/initialize-windows-auth.ps1')
        Assert-Test -Condition (-not $initializerResult.TimedOut -and $initializerResult.ExitCode -eq 0) -Message 'installed credential initializer is missing'
        $secretDirResult = Invoke-BoundedExternal -FilePath $wslPath -Arguments @('-d', 'Ubuntu-24.04', '-u', 'root', '--', 'stat', '-c', '%U:%G:%a:%F', '/var/lib/guacamole-workspace/secrets')
        $secretDir = @($secretDirResult.Output -split "`r?`n")
        Assert-Test -Condition (-not $secretDirResult.TimedOut -and $secretDirResult.ExitCode -eq 0) -Message ('installed credential secret directory probe failed: ' + ($secretDir -join ' '))
        Assert-Test -Condition (($secretDir -join "`n") -match 'root:root:700:directory') -Message 'installed credential secret directory ownership or mode is unsafe'
        $statusDirResult = Invoke-BoundedExternal -FilePath $wslPath -Arguments @('-d', 'Ubuntu-24.04', '-u', 'root', '--', 'stat', '-c', '%U:%G:%a', '/var/lib/guacamole-workspaces/jobs')
        $statusDir = @($statusDirResult.Output -split "`r?`n")
        Assert-Test -Condition (-not $statusDirResult.TimedOut -and $statusDirResult.ExitCode -eq 0) -Message ('installed job status probe failed: ' + ($statusDir -join ' '))
        Assert-Test -Condition (($statusDir -join "`n") -match 'root:root:750') -Message 'installed job status directory ownership or mode is unsafe'

        # Exercise the installed Cockpit package bridge/helper boundary with a
        # read-only clone preflight. This is deterministic and never submits a
        # VM, DHCP, NVRAM, TPM, or Guacamole mutation.
        $bridgeResult = Invoke-BoundedExternal -FilePath $wslPath -Arguments @('-d', 'Ubuntu-24.04', '-u', 'root', '--', 'cockpit-bridge', '--packages')
        $bridgePackages = @($bridgeResult.Output -split "`r?`n")
        Assert-Test -Condition (-not $bridgeResult.TimedOut -and $bridgeResult.ExitCode -eq 0) -Message ('installed Cockpit bridge probe failed: ' + ($bridgePackages -join ' '))
        Assert-Test -Condition (($bridgePackages -join "`n") -match '(?m)(^|\s)workspace_templates(\s|$)') -Message 'installed Cockpit bridge did not discover workspace_templates'
        # The read-only preflight verifies the immutable 30 GiB template hash
        # before querying libvirt and PostgreSQL. Keep a bounded 300 second
        # budget so a slow VHDX fails with a diagnosis instead of hanging.
        $whatIfResult = Invoke-BoundedExternal -TimeoutSeconds 300 -FilePath $wslPath -Arguments @('-d', 'Ubuntu-24.04', '-u', 'root', '--', '/usr/local/libexec/guacamole-workspace-helper', 'clone', '--name', 'task6-cockpit-what-if', '--assign-user', 'demo', '--template', 'windows11-v1', '--what-if', '--json')
        $whatIfOutput = @($whatIfResult.Output -split "`r?`n")
        $whatIfJsonLine = $whatIfOutput | Where-Object { $_ -match '^\s*\{' } | Select-Object -Last 1
        Assert-Test -Condition (-not $whatIfResult.TimedOut -and -not [string]::IsNullOrWhiteSpace($whatIfJsonLine)) -Message ('installed Cockpit helper did not return what-if JSON within bounded template-preflight budget: ' + ($whatIfResult.Output -join ' '))
        $whatIfPayload = $whatIfJsonLine | ConvertFrom-Json
        Assert-Test -Condition (-not $whatIfResult.TimedOut -and $whatIfResult.ExitCode -eq 0 -and $whatIfPayload.ok -eq $true -and $whatIfPayload.whatIf -eq $true -and $whatIfPayload.status -eq 'what-if') -Message 'installed Cockpit helper what-if boundary failed or timed out'
        Write-Host 'TASK6_COCKPIT_INSTALLED_WHATIF_OK'

        $cockpitAdminResult = Invoke-BoundedExternal -FilePath $wslPath -Arguments @('-d', 'Ubuntu-24.04', '-u', 'root', '--', 'sh', '-lc', 'id -u cockpitadmin >/dev/null 2>&1')
        if (-not $cockpitAdminResult.TimedOut -and $cockpitAdminResult.ExitCode -eq 0) {
            $unprivilegedResult = Invoke-BoundedExternal -FilePath $wslPath -Arguments @('-d', 'Ubuntu-24.04', '-u', 'cockpitadmin', '--', '/usr/local/libexec/guacamole-workspace-helper', 'clone', '--name', 'task5-privilege-check', '--assign-user', 'demo', '--template', 'windows11-v1', '--json')
            $unprivilegedOutput = @($unprivilegedResult.Output -split "`r?`n")
            $unprivilegedJson = $unprivilegedOutput | Where-Object { $_ -match '^\s*\{' } | Select-Object -Last 1
            Assert-Test -Condition (-not $unprivilegedResult.TimedOut -and $unprivilegedResult.ExitCode -eq 2 -and $unprivilegedJson -match 'PRIVILEGE_REQUIRED') -Message 'unprivileged clone was not rejected before mutation'
        }
    } else {
        if (-not $installedBundleStale) {
            Write-Host 'TASK5_LIVE_INSTALL_CHECK_SKIPPED: installed helper bundle is not present'
        }
    }
}

Write-Host 'TASK6_COCKPIT_PERSISTENT_JOB_PACKAGE_TESTS_OK'
