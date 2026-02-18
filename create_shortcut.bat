@echo off
:: Creates a desktop shortcut for JJP Asset Decryptor with the custom icon.

set "PROJECT_DIR=%~dp0"
set "ICON=%PROJECT_DIR%jjp_decryptor\icon.ico"

:: Generate icon if it doesn't exist
if not exist "%ICON%" (
    echo Generating icon...
    python "%PROJECT_DIR%generate_icon.py"
)

echo Creating desktop shortcut...
powershell -NoProfile -ExecutionPolicy Bypass -File "%PROJECT_DIR%_make_shortcut.ps1"
echo.
pause
