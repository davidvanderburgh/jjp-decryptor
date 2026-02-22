<#
.SYNOPSIS
    Build script for the JJP Asset Decryptor Windows installer.

.DESCRIPTION
    Downloads a Python embeddable distribution, copies tkinter files from the
    local Python installation, assembles the bundle, and compiles the Inno Setup
    installer.

.NOTES
    Prerequisites:
    - Inno Setup 6 (https://jrsoftware.org/isinfo.php)
    - Python 3.10+ with tkinter (used to source tkinter files for the bundle)
    - Internet access (downloads Python embeddable zip)
    - PowerShell 5.1+
#>

param(
    [string]$InnoSetupPath = ""
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Split-Path -Parent $ScriptDir
$BuildDir = Join-Path $ScriptDir "build"

# --- Detect local Python and its version ---
Write-Host "Detecting local Python installation..." -ForegroundColor Cyan
try {
    $pyInfo = python -c "import sys, os, _tkinter, tkinter; base = os.path.dirname(os.path.dirname(_tkinter.__file__)); print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}'); print(base); print(_tkinter.__file__); print(os.path.dirname(tkinter.__file__))" 2>&1
    if ($LASTEXITCODE -ne 0) { throw "Python not found" }
    $pyLines = $pyInfo -split "`n" | ForEach-Object { $_.Trim() }
    $PythonVersion = $pyLines[0]
    $pyBase = $pyLines[1]
    $tkinterPydPath = $pyLines[2]
    $tkinterPkgDir = $pyLines[3]
} catch {
    Write-Error "Python with tkinter is required to build the installer. Install Python 3.10+ from python.org."
    exit 1
}

$pyMajorMinor = ($PythonVersion -split '\.')[0..1] -join ''  # e.g. "313"
Write-Host "  Python version: $PythonVersion (python$pyMajorMinor)" -ForegroundColor Green
Write-Host "  Python base: $pyBase" -ForegroundColor Green

# --- Locate Inno Setup compiler ---
if (-not $InnoSetupPath) {
    $candidates = @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
    )
    foreach ($c in $candidates) {
        if (Test-Path $c) { $InnoSetupPath = $c; break }
    }
}
if (-not $InnoSetupPath -or -not (Test-Path $InnoSetupPath)) {
    Write-Error "Inno Setup compiler (ISCC.exe) not found. Install Inno Setup 6 or pass -InnoSetupPath."
    exit 1
}
Write-Host "Using Inno Setup: $InnoSetupPath" -ForegroundColor Cyan

# --- Read version from __init__.py ---
$initFile = Join-Path $ProjectDir "jjp_decryptor\__init__.py"
$versionLine = Get-Content $initFile | Where-Object { $_ -match '__version__\s*=\s*"([^"]+)"' }
if ($versionLine -match '"([^"]+)"') {
    $AppVersion = $Matches[1]
} else {
    Write-Error "Could not read __version__ from $initFile"
    exit 1
}
Write-Host "Building version: $AppVersion" -ForegroundColor Cyan

# --- Clean and create build directory ---
if (Test-Path $BuildDir) {
    Write-Host "Cleaning previous build..." -ForegroundColor Yellow
    Remove-Item -Recurse -Force $BuildDir
}
New-Item -ItemType Directory -Path $BuildDir | Out-Null

$PythonDir = Join-Path $BuildDir "python"
New-Item -ItemType Directory -Path $PythonDir | Out-Null

# --- Download Python embeddable zip ---
$embedUrl = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip"
$embedZip = Join-Path $BuildDir "python-embed.zip"

Write-Host "`nDownloading Python $PythonVersion embeddable zip..." -ForegroundColor Cyan
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
Invoke-WebRequest -Uri $embedUrl -OutFile $embedZip -UseBasicParsing
Write-Host "  Saved to: $embedZip"

Write-Host "Extracting embeddable zip..." -ForegroundColor Cyan
Expand-Archive -Path $embedZip -DestinationPath $PythonDir -Force

# --- Copy tkinter files from local Python installation ---
Write-Host "`nCopying tkinter files from local Python..." -ForegroundColor Cyan

# Copy _tkinter.pyd and its DLL dependencies from local Python
$tkinterDllDir = Split-Path -Parent $tkinterPydPath
foreach ($file in @("_tkinter.pyd", "tcl86t.dll", "tk86t.dll", "zlib1.dll")) {
    $src = Join-Path $tkinterDllDir $file
    $dst = Join-Path $PythonDir $file
    if ((Test-Path $src) -and -not (Test-Path $dst)) {
        Copy-Item $src -Destination $dst -Force
        Write-Host "  Copied $file"
    } elseif (Test-Path $dst) {
        Write-Host "  $file already present"
    }
}

# Copy tkinter Python package
$libDir = Join-Path $PythonDir "Lib"
if (-not (Test-Path $libDir)) { New-Item -ItemType Directory -Path $libDir | Out-Null }
Copy-Item $tkinterPkgDir -Destination (Join-Path $libDir "tkinter") -Recurse -Force
Write-Host "  Copied Lib/tkinter/"

# Copy Tcl/Tk runtime scripts
$tclSrcDir = Join-Path $pyBase "tcl"
if (Test-Path $tclSrcDir) {
    Copy-Item $tclSrcDir -Destination (Join-Path $PythonDir "tcl") -Recurse -Force
    Write-Host "  Copied tcl/ directory"
} else {
    Write-Warning "tcl/ directory not found at $tclSrcDir - tkinter may fail to initialize"
}

# --- Patch python3XX._pth to add our paths ---
Write-Host "`nPatching python${pyMajorMinor}._pth..." -ForegroundColor Cyan
$pthFile = Join-Path $PythonDir "python${pyMajorMinor}._pth"
if (-not (Test-Path $pthFile)) {
    Write-Error "Could not find $pthFile"
    exit 1
}
$pthContent = @(
    "python${pyMajorMinor}.zip",
    ".",
    "./Lib",
    ".."
)
$pthContent -join "`r`n" | Set-Content -Path $pthFile -Encoding ASCII -NoNewline
Write-Host "  Updated: $pthFile"

# --- Smoke test: verify tkinter imports ---
Write-Host "`nRunning tkinter smoke test..." -ForegroundColor Cyan
$pythonExe = Join-Path $PythonDir "python.exe"

# Set TCL_LIBRARY and TK_LIBRARY for the test
$env:TCL_LIBRARY = Join-Path $PythonDir "tcl\tcl8.6"
$env:TK_LIBRARY = Join-Path $PythonDir "tcl\tk8.6"

$testResult = & $pythonExe -c "import tkinter; print('tkinter OK')" 2>&1
if ($LASTEXITCODE -eq 0) {
    Write-Host "  $testResult" -ForegroundColor Green
} else {
    Write-Warning "tkinter smoke test failed: $testResult"
    Write-Warning "The installer will still be built, but the bundled Python may not work correctly."
}

# Clean up env vars
Remove-Item Env:\TCL_LIBRARY -ErrorAction SilentlyContinue
Remove-Item Env:\TK_LIBRARY -ErrorAction SilentlyContinue

# --- Compile Inno Setup installer ---
Write-Host "`nCompiling installer..." -ForegroundColor Cyan
$issFile = Join-Path $ScriptDir "jjp_decryptor.iss"
& $InnoSetupPath /Qp "/DAppVersion=$AppVersion" "/DPythonDir=$PythonDir" "/DProjectDir=$ProjectDir" $issFile

if ($LASTEXITCODE -eq 0) {
    $outputDir = Join-Path $ScriptDir "Output"
    Write-Host "`n========================================" -ForegroundColor Green
    Write-Host "  Build successful!" -ForegroundColor Green
    Write-Host "  Output: $outputDir" -ForegroundColor Green
    Write-Host "========================================" -ForegroundColor Green
} else {
    Write-Error "Inno Setup compilation failed with exit code $LASTEXITCODE"
    exit 1
}
