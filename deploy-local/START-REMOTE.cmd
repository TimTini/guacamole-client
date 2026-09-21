@echo off
setlocal EnableExtensions DisableDelayedExpansion

set "ROOT_SCRIPT=%~dp0..\START-REMOTE.cmd"
call "%ROOT_SCRIPT%" %*
set "FORWARD_EXIT=%ERRORLEVEL%"
endlocal & exit /b %FORWARD_EXIT%
