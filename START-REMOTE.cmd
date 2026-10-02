@echo off
setlocal EnableExtensions DisableDelayedExpansion

title Guacamole - whole local stack
set "REPO_DIR=%~dp0"
if "%REPO_DIR:~-1%"=="\" set "REPO_DIR=%REPO_DIR:~0,-1%"
set "RECOVERY_SCRIPT=%REPO_DIR%\deploy-local\recover-after-rollback.ps1"

if not "%~2"=="" goto :invalid_command
if /I "%~1"=="start" goto :command_start
if /I "%~1"=="stop" goto :command_stop
if /I "%~1"=="status" goto :command_status
if not "%~1"=="" goto :invalid_command
goto :menu

:command_start
call :run_action start
exit /b %ERRORLEVEL%

:command_stop
call :run_action stop
exit /b %ERRORLEVEL%

:command_status
call :run_action status
exit /b %ERRORLEVEL%

:menu
:menu_loop
echo.
echo ================================================
echo   Guacamole: quan ly toan bo stack local
echo ================================================
echo   1. Khoi dong stack (khong tu dong bat VM)
echo   2. Dung tat ca
echo   3. Xem trang thai
echo   4. Thoat
echo.
choice /C 1234 /N /M "Chon thao tac [1-4]: "
set "MENU_CHOICE=%ERRORLEVEL%"
if "%MENU_CHOICE%"=="0" goto :menu_cancel
if "%MENU_CHOICE%"=="1" goto :menu_start
if "%MENU_CHOICE%"=="2" goto :menu_stop
if "%MENU_CHOICE%"=="3" goto :menu_status
if "%MENU_CHOICE%"=="4" goto :menu_exit
echo [LOI] Khong doc duoc lua chon menu (ma %MENU_CHOICE%).
exit /b 1

:menu_start
call :menu_action start
goto :menu_loop

:menu_stop
call :menu_action stop
goto :menu_loop

:menu_status
call :menu_action status
goto :menu_loop

:menu_action
call :run_action %~1
set "MENU_EXIT=%ERRORLEVEL%"
echo.
if "%MENU_EXIT%"=="0" (
    echo [OK] Thao tac %~1 da hoan tat.
) else (
    echo [LOI] Thao tac %~1 that bai, ma loi %MENU_EXIT%.
)
pause
exit /b 0

:run_action
if not exist "%RECOVERY_SCRIPT%" (
    echo [LOI] Khong tim thay entrypoint: "%RECOVERY_SCRIPT%"
    exit /b 2
)

echo.
if /I "%~1"=="start" echo Dang khoi dong WSL, Docker, Guacamole, libvirt, Cockpit va Quick Tunnel (khong tu dong bat VM)...
if /I "%~1"=="stop" echo Dang dung WSL, Docker, Guacamole, libvirt, Windows 11 va Quick Tunnel...
if /I "%~1"=="status" echo Dang doc trang thai toan bo stack...
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%RECOVERY_SCRIPT%" -Action "%~1"
set "ACTION_EXIT=%ERRORLEVEL%"
if not "%ACTION_EXIT%"=="0" echo [LOI] recover-after-rollback.ps1 tra ve ma %ACTION_EXIT%.
if /I "%~1"=="start" if "%ACTION_EXIT%"=="0" (
    echo.
    echo Guacamole local: http://127.0.0.1:8080/guacamole/
    echo Cockpit local: https://127.0.0.1:9090
    echo Quick Tunnel URL: xem output khoi dong o tren hoac chay lenh status.
)
exit /b %ACTION_EXIT%

:invalid_command
echo [LOI] Tham so khong hop le: "%~1"
echo Su dung dung mot tham so: %~nx0 start ^| stop ^| status
exit /b 2

:menu_cancel
echo [HUY] Da huy thao tac; khong thay doi lifecycle.
exit /b 0

:menu_exit
echo Tam biet.
exit /b 0
