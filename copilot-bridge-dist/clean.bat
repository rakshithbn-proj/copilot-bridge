@echo off
setlocal

echo Cleaning build artifacts...

if exist build                rmdir /s /q build
if exist dist                 rmdir /s /q dist
if exist *.egg-info           rmdir /s /q *.egg-info
if exist *.pyd                del /q *.pyd
if exist copilot_bridge.build rmdir /s /q copilot_bridge.build

echo Done.
