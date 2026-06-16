@echo off
setlocal

echo ============================================================
echo  Copilot Bridge Extension - Build + Install Script
echo ============================================================
echo.

:: Configuration
set "EXT_DIR=%~dp0"
:: VSIX is found dynamically below - this line kept for reference only
set "VSIX=%EXT_DIR%copilot-bridge-5.1.0.vsix"

:: Find code CLI
set "CODE_CLI="
where code >nul 2>&1
if %errorlevel%==0 (
    set "CODE_CLI=code"
) else (
    :: Common install locations
    if exist "%LOCALAPPDATA%\Programs\Microsoft VS Code\bin\code.cmd" (
        set "CODE_CLI=%LOCALAPPDATA%\Programs\Microsoft VS Code\bin\code.cmd"
    ) else if exist "C:\Program Files\Microsoft VS Code\bin\code.cmd" (
        set "CODE_CLI=C:\Program Files\Microsoft VS Code\bin\code.cmd"
    ) else if exist "%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe" (
        set "CODE_CLI=%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe"
    )
)

if "%CODE_CLI%"=="" (
    echo [ERROR] Cannot find VS Code CLI ^(code.cmd^). 
    echo         Make sure VS Code is installed and 'code' is in PATH.
    echo         Or install manually: Extensions panel ^> ... ^> Install from VSIX
    pause
    exit /b 1
)
echo [OK] Found VS Code CLI: %CODE_CLI%
echo.

:: Step 1: Build
echo [1/4] Checking node_modules...
if not exist "%EXT_DIR%node_modules" (
    echo       Installing dependencies...
    cd /d "%EXT_DIR%"
    call npm install
    if %errorlevel% neq 0 (
        echo [ERROR] npm install failed.
        pause
        exit /b 1
    )
)

echo [2/4] Packaging VSIX...
cd /d "%EXT_DIR%"
call npx vsce package --allow-missing-repository
if %errorlevel% neq 0 (
    echo [ERROR] VSIX packaging failed.
    pause
    exit /b 1
)
echo       Packaged: %VSIX%
echo.

:: Step 2: Uninstall old version
echo [3/4] Removing old copilot-bridge versions...
call "%CODE_CLI%" --uninstall-extension local.copilot-bridge 2>nul
:: Also clean up any leftover folders
for /d %%d in ("%USERPROFILE%\.vscode\extensions\local.copilot-bridge-*") do (
    echo       Removing %%~nxd...
    rmdir /s /q "%%d" 2>nul
)
echo.

:: Step 3: Install VSIX
echo [4/4] Installing copilot-bridge-5.1.0.vsix...
call "%CODE_CLI%" --install-extension "%VSIX%" --force
if %errorlevel% neq 0 (
    echo.
    echo [WARNING] CLI install may have failed. Try manual install:
    echo           1. Open VS Code
    echo           2. Ctrl+Shift+P ^> "Extensions: Install from VSIX..."
    echo           3. Select: %VSIX%
    pause
    exit /b 1
)

echo.
echo ============================================================
echo  SUCCESS! Copilot Bridge v5.1.0 installed.
echo ============================================================
echo.
echo  Next steps:
echo    1. Open VS Code ^(or reload window: Ctrl+Shift+P ^> Reload^)
echo    2. Wait for "Copilot Bridge v5.0.0 started" notification
echo    3. Check status bar shows "Bridge v5.0.0: 5150"
echo    4. Run smoke test:
echo       python fm\rakshith\test_copilot_bridge_smoke.py
echo.
echo  If the extension doesn't appear, install manually:
echo    Ctrl+Shift+P ^> "Extensions: Install from VSIX..."
echo    Select: %VSIX%
echo.

pause
