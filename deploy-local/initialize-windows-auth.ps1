[CmdletBinding()]
param(
    [switch]$FromStdin
)

$ErrorActionPreference = 'Stop'
$WslDistro = 'Ubuntu-24.04'
# The installed helper stores the value at this fixed POSIX path inside the
# H-backed Ubuntu ext4 VHDX. This initializer never accepts a path override.
# /var/lib/guacamole-workspace/secrets/windows11_guacadmin_password
# Use the same immutable helper release as Cockpit and persistent workers.
$WslHelper = '/usr/local/libexec/guacamole-workspace-helper'
$WslExecutable = if ($env:GUACAMOLE_WSL_EXECUTABLE) { $env:GUACAMOLE_WSL_EXECUTABLE } else { 'wsl.exe' }

$password = $null
$securePassword = $null
$pointer = [IntPtr]::Zero
try {
    if ($FromStdin) {
        $password = [Console]::In.ReadToEnd()
    }
    else {
        $securePassword = Read-Host 'Windows guacadmin password' -AsSecureString
        $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($securePassword)
        $password = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    }

    if ([string]::IsNullOrEmpty($password)) {
        throw 'A non-empty password is required.'
    }

    # The password is sent only through the child process stdin. It is never an
    # argument, environment variable, JSON value, or host output.
    $password | & $WslExecutable -d $WslDistro -u root -- python3 $WslHelper set-windows-credential --stdin
    if ($LASTEXITCODE -ne 0) {
        throw "Credential initialization failed with exit code $LASTEXITCODE."
    }
}
finally {
    if ($pointer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    }
    if ($securePassword -ne $null) {
        $securePassword.Dispose()
    }
    $password = $null
}
