[CmdletBinding()]
param(
    [string]$HostName = '127.0.0.1',
    [int]$Port = 3391
)

$ErrorActionPreference = 'Stop'
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$PasswordPath = Join-Path $RepoRoot 'deploy-local\secrets\windows11_password.txt'
$WslDistro = 'Ubuntu-24.04'
$WslPasswordPath = '/mnt/h/RemoteWorkspaces/guacamole-client/deploy-local/secrets/windows11_password.txt'

if (-not (Test-Path -LiteralPath $PasswordPath -PathType Leaf)) {
    Write-Host 'WIN11_RDP_CONNECTION_FAILED'
    exit 2
}

$pythonCode = @'
import os
import pty
import select
import signal
import socket
import sys
import time


def emit(result, code=1):
    print(result)
    raise SystemExit(code)


try:
    host, port, user, password_path = sys.argv[1:5]
    with open(password_path, 'r', encoding='ascii', newline='') as password_file:
        password = password_file.read().rstrip('\r\n')
    if not password or any(character in password for character in ('\x00', '\r', '\n')):
        emit('WIN11_RDP_CONNECTION_FAILED', 2)

    # xfreerdp3 can return success without a useful diagnostic when the
    # pre-connect phase fails. Verify the host forward is listening first so
    # a stopped VM can never be reported as an authentication success.
    try:
        with socket.create_connection((host, int(port)), timeout=3):
            pass
    except (ConnectionRefusedError, TimeoutError, OSError, ValueError):
        emit('WIN11_RDP_CONNECTION_FAILED', 2)

    child_pid, terminal_fd = pty.fork()
    if child_pid == 0:
        os.execvp('xfreerdp3', [
            'xfreerdp3',
            f'/v:{host}:{port}',
            f'/u:{user}',
            '/auth-only',
            '/from-stdin:force',
            '/cert:ignore',
            '/log-level:ERROR',
        ])

    os.set_blocking(terminal_fd, False)
    captured = bytearray()
    deadline = time.monotonic() + 30
    send_after = time.monotonic() + 0.3
    password_sent = False
    child_status = None

    while time.monotonic() < deadline:
        if not password_sent and time.monotonic() >= send_after:
            os.write(terminal_fd, (password + '\n').encode('utf-8'))
            password_sent = True

        ready, _, _ = select.select([terminal_fd], [], [], 0.2)
        if ready:
            try:
                chunk = os.read(terminal_fd, 4096)
            except OSError:
                chunk = b''
            if not chunk:
                break
            captured.extend(chunk)
            if len(captured) > 65536:
                del captured[:-65536]

        finished_pid, child_status = os.waitpid(child_pid, os.WNOHANG)
        if finished_pid == child_pid:
            break

    if child_status is None:
        try:
            os.kill(child_pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        # Some FreeRDP builds keep the PTY child alive after SIGTERM while the
        # transport is waiting for a guest response. Never let a health check
        # hang indefinitely; force the child down after the bounded probe.
        try:
            _, child_status = os.waitpid(child_pid, os.WNOHANG)
        except ChildProcessError:
            child_status = 0
        if child_status is None or child_status == 0:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            _, child_status = os.waitpid(child_pid, 0)

    if os.WIFEXITED(child_status):
        exit_code = os.WEXITSTATUS(child_status)
    else:
        exit_code = 1

    if exit_code == 0:
        emit('WIN11_RDP_AUTH_OK', 0)

    diagnostic = bytes(captured).decode('utf-8', 'replace').lower()
    if any(marker in diagnostic for marker in (
        'errconnect_logon_failure',
        'authentication failure',
        'authentication failed',
        'logon failure',
    )):
        emit('WIN11_RDP_AUTH_FAILED')
    if any(marker in diagnostic for marker in (
        'errconnect_connect_failed',
        'freerdp_tcp_connect',
        'connection refused',
        'connection failure',
        'timed out',
        'timeout',
    )):
        emit('WIN11_RDP_CONNECTION_FAILED')
    emit('WIN11_RDP_CONNECTION_FAILED')
except SystemExit:
    raise
except Exception:
    emit('WIN11_RDP_CONNECTION_FAILED', 2)
'@

$encodedPython = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($pythonCode))
$pythonRunner = "import base64;exec(base64.b64decode('$encodedPython'))"

try {
    $result = & wsl.exe -d $WslDistro -u root -- python3 -c $pythonRunner $HostName $Port guacadmin $WslPasswordPath 2>&1
    $exitCode = $LASTEXITCODE
} catch {
    Write-Host 'WIN11_RDP_CONNECTION_FAILED'
    exit 2
}

$resultToken = @($result |
    ForEach-Object { $_.ToString().Trim() } |
    Where-Object { $_ -match '^(WIN11_RDP_AUTH_OK|WIN11_RDP_AUTH_FAILED|WIN11_RDP_CONNECTION_FAILED)$' } |
    Select-Object -Last 1)

if ($resultToken.Count -eq 0) {
    Write-Host 'WIN11_RDP_CONNECTION_FAILED'
    exit 2
}

Write-Host $resultToken[0]
if ($resultToken[0] -eq 'WIN11_RDP_AUTH_OK' -and $exitCode -eq 0) {
    exit 0
}

exit 1
