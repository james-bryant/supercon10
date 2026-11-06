@echo off
rem Double-click (or run from a terminal) to set up the project.
rem Creates .venv, installs mpremote/esptool, and pulls the code off the ESP32 on COM6.
rem Pass a different port like:  setup.cmd -Port COM7
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" %*
echo.
pause
