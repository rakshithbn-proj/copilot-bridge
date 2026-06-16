@echo off
setlocal
cd /d "%~dp0"

echo Building copilot_bridge wheel...

set "USE_NUITKA=0"
if /i "%~1"=="--nuitka" set "USE_NUITKA=1"
if /i "%~1"=="/nuitka"  set "USE_NUITKA=1"

:: Clean previous build artifacts
if exist build          rmdir /s /q build
if exist dist           rmdir /s /q dist
for /d %%d in ("*.egg-info") do rmdir /s /q "%%d" 2>nul
if exist *.pyd          del /q *.pyd 2>nul
if exist copilot_bridge.build rmdir /s /q copilot_bridge.build 2>nul

if "%USE_NUITKA%"=="1" (
    :: Optional path: compile source to .pyd using Nuitka.
    :: This can take a long time if C toolchain components need to be installed.
    echo.
    echo [1/2] Compiling copilot_bridge.py with Nuitka...

    python -m pip show nuitka >nul 2>&1
    if errorlevel 1 (
        echo.
        echo ERROR: Nuitka is not installed in this Python environment.
        echo        Install it first: python -m pip install nuitka
        exit /b 1
    )

    :: Use Strawberry Perl's 64-bit MinGW gcc; auto-confirm any downloads
    set "PATH=C:\Strawberry\c\bin;C:\Strawberry\perl\bin;%PATH%"

    echo.
    python -m nuitka --module ..\rakshith\copilot_bridge.py --output-dir=. --remove-output --mingw64 --assume-yes-for-downloads --show-progress --jobs=1
    if errorlevel 1 (
        echo.
        echo ERROR: Nuitka compilation failed.
        exit /b 1
    )
) else (
    echo.
    echo [1/2] Skipping Nuitka ^(default mode: pure-Python wheel^).
)

echo.
echo       Ensuring packaging dependencies ^(setuptools, wheel^)...
python -m pip install --quiet setuptools wheel
if errorlevel 1 (
    echo.
    echo ERROR: Failed to install Python packaging dependencies.
    exit /b 1
)

:: Step 2: Package into wheel
echo.
echo [2/2] Packaging into wheel...
python setup.py bdist_wheel
if errorlevel 1 (
    echo.
    echo ERROR: Wheel build failed.
    exit /b 1
)

echo.
echo Done. Wheel is in dist\
dir /b dist\*.whl
