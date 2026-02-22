<#
.SYNOPSIS
    Installs all prerequisites for the JJP Asset Decryptor.

.DESCRIPTION
    Checks for and installs:
    - WSL2 (Windows Subsystem for Linux)
    - Ubuntu distribution in WSL
    - gcc (C compiler in WSL)
    - partclone (partition imaging in WSL)
    - xorriso (ISO manipulation in WSL)
    - usbipd-win (USB device sharing with WSL)

    This script is safe to re-run — it checks before installing and skips
    anything that is already present.

.NOTES
    Must be run as Administrator (required for WSL and usbipd-win installation).
    May require a reboot if WSL2 was not previously enabled.
#>

# --- Require admin ---
$currentPrincipal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $currentPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "This script must be run as Administrator." -ForegroundColor Red
    Write-Host "Right-click and select 'Run as administrator', or run from an elevated PowerShell." -ForegroundColor Red
    Write-Host ""
    Read-Host "Press Enter to exit"
    exit 1
}

$ErrorActionPreference = "Continue"
$needsReboot = $false
$results = @()

function Write-Step($msg) {
    Write-Host "`n=== $msg ===" -ForegroundColor Cyan
}

function Write-OK($msg) {
    Write-Host "  [OK] $msg" -ForegroundColor Green
    $script:results += [PSCustomObject]@{ Name = $msg; Status = "OK" }
}

function Write-Installed($msg) {
    Write-Host "  [INSTALLED] $msg" -ForegroundColor Green
    $script:results += [PSCustomObject]@{ Name = $msg; Status = "Installed" }
}

function Write-FAIL($msg) {
    Write-Host "  [MISSING] $msg" -ForegroundColor Red
    $script:results += [PSCustomObject]@{ Name = $msg; Status = "Missing" }
}

function Write-SKIP($msg) {
    Write-Host "  [SKIP] $msg" -ForegroundColor Yellow
    $script:results += [PSCustomObject]@{ Name = $msg; Status = "Skipped" }
}

# ============================================================
# 1. WSL2
# ============================================================
Write-Step "Checking WSL2..."

$wslAvailable = $false
try {
    $wslStatus = wsl --status 2>&1 | Out-String
    if ($LASTEXITCODE -eq 0) {
        $wslAvailable = $true
        Write-OK "WSL2"
    }
} catch {}

if (-not $wslAvailable) {
    Write-Host "  WSL2 is not installed or not enabled." -ForegroundColor Yellow
    $install = Read-Host "  Install WSL2 now? (y/n)"
    if ($install -eq 'y') {
        Write-Host "  Installing WSL2 (this may take a few minutes)..." -ForegroundColor Cyan
        wsl --install --no-distribution 2>&1 | ForEach-Object { Write-Host "    $_" }
        $needsReboot = $true
        Write-Installed "WSL2 (reboot required)"
    } else {
        Write-SKIP "WSL2"
    }
}

# ============================================================
# 2. Ubuntu distribution
# ============================================================
Write-Step "Checking Ubuntu distribution..."

$ubuntuFound = $false
if ($wslAvailable) {
    try {
        $distros = wsl --list --quiet 2>&1 | Out-String
        if ($distros -match 'Ubuntu') {
            $ubuntuFound = $true
            Write-OK "Ubuntu"
        }
    } catch {}
}

if (-not $ubuntuFound -and -not $needsReboot) {
    if ($wslAvailable) {
        Write-Host "  No Ubuntu distribution found in WSL." -ForegroundColor Yellow
        $install = Read-Host "  Install Ubuntu now? (y/n)"
        if ($install -eq 'y') {
            Write-Host "  Installing Ubuntu (this may take several minutes)..." -ForegroundColor Cyan
            wsl --install -d Ubuntu 2>&1 | ForEach-Object { Write-Host "    $_" }
            if ($LASTEXITCODE -eq 0) {
                $ubuntuFound = $true
                Write-Installed "Ubuntu"
            } else {
                Write-FAIL "Ubuntu (installation failed)"
            }
        } else {
            Write-SKIP "Ubuntu"
        }
    } else {
        Write-SKIP "Ubuntu (WSL2 not available yet — install after reboot)"
    }
} elseif ($needsReboot -and -not $ubuntuFound) {
    Write-SKIP "Ubuntu (will install after WSL2 reboot)"
}

# ============================================================
# 3. gcc in WSL
# ============================================================
Write-Step "Checking gcc in WSL..."

if ($wslAvailable -and $ubuntuFound) {
    $gccFound = $false
    try {
        wsl -u root -- which gcc 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) {
            $gccFound = $true
            Write-OK "gcc"
        }
    } catch {}

    if (-not $gccFound) {
        Write-Host "  Installing gcc..." -ForegroundColor Cyan
        wsl -u root -- bash -c "apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq gcc" 2>&1 | ForEach-Object { Write-Host "    $_" }
        # Verify
        try {
            wsl -u root -- which gcc 2>&1 | Out-Null
            if ($LASTEXITCODE -eq 0) {
                Write-Installed "gcc"
            } else {
                Write-FAIL "gcc"
            }
        } catch {
            Write-FAIL "gcc"
        }
    }
} else {
    Write-SKIP "gcc (WSL/Ubuntu not available yet)"
}

# ============================================================
# 4. partclone in WSL
# ============================================================
Write-Step "Checking partclone in WSL..."

if ($wslAvailable -and $ubuntuFound) {
    $pcFound = $false
    try {
        wsl -u root -- which partclone.ext4 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) {
            $pcFound = $true
            Write-OK "partclone"
        }
    } catch {}

    if (-not $pcFound) {
        Write-Host "  Installing partclone..." -ForegroundColor Cyan
        wsl -u root -- bash -c "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq partclone" 2>&1 | ForEach-Object { Write-Host "    $_" }
        try {
            wsl -u root -- which partclone.ext4 2>&1 | Out-Null
            if ($LASTEXITCODE -eq 0) {
                Write-Installed "partclone"
            } else {
                Write-FAIL "partclone"
            }
        } catch {
            Write-FAIL "partclone"
        }
    }
} else {
    Write-SKIP "partclone (WSL/Ubuntu not available yet)"
}

# ============================================================
# 5. xorriso in WSL
# ============================================================
Write-Step "Checking xorriso in WSL..."

if ($wslAvailable -and $ubuntuFound) {
    $xrFound = $false
    try {
        wsl -u root -- which xorriso 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) {
            $xrFound = $true
            Write-OK "xorriso"
        }
    } catch {}

    if (-not $xrFound) {
        Write-Host "  Installing xorriso..." -ForegroundColor Cyan
        wsl -u root -- bash -c "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq xorriso" 2>&1 | ForEach-Object { Write-Host "    $_" }
        try {
            wsl -u root -- which xorriso 2>&1 | Out-Null
            if ($LASTEXITCODE -eq 0) {
                Write-Installed "xorriso"
            } else {
                Write-FAIL "xorriso"
            }
        } catch {
            Write-FAIL "xorriso"
        }
    }
} else {
    Write-SKIP "xorriso (WSL/Ubuntu not available yet)"
}

# ============================================================
# 6. usbipd-win
# ============================================================
Write-Step "Checking usbipd-win..."

$usbipdPath = "C:\Program Files\usbipd-win\usbipd.exe"
if (Test-Path $usbipdPath) {
    $version = & $usbipdPath --version 2>&1
    Write-OK "usbipd-win ($version)"
} else {
    Write-Host "  usbipd-win is not installed." -ForegroundColor Yellow

    # Try winget first
    $wingetAvailable = $false
    try {
        $null = Get-Command winget -ErrorAction Stop
        $wingetAvailable = $true
    } catch {}

    if ($wingetAvailable) {
        $install = Read-Host "  Install usbipd-win via winget? (y/n)"
        if ($install -eq 'y') {
            Write-Host "  Installing usbipd-win..." -ForegroundColor Cyan
            winget install --exact --interactive dorssel.usbipd-win --accept-source-agreements 2>&1 | ForEach-Object { Write-Host "    $_" }
            if (Test-Path $usbipdPath) {
                Write-Installed "usbipd-win"
            } else {
                Write-Host "  usbipd-win may require a reboot to appear." -ForegroundColor Yellow
                Write-Installed "usbipd-win (may need reboot)"
            }
        } else {
            Write-SKIP "usbipd-win"
        }
    } else {
        Write-Host "  winget is not available. Opening the download page..." -ForegroundColor Yellow
        $install = Read-Host "  Open the usbipd-win download page in your browser? (y/n)"
        if ($install -eq 'y') {
            Start-Process "https://github.com/dorssel/usbipd-win/releases"
            Write-SKIP "usbipd-win (download manually from browser)"
        } else {
            Write-SKIP "usbipd-win"
        }
    }
}

# ============================================================
# Summary
# ============================================================
Write-Host "`n"
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  Prerequisites Summary" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan

foreach ($r in $results) {
    $color = switch ($r.Status) {
        "OK"        { "Green" }
        "Installed" { "Green" }
        "Missing"   { "Red" }
        "Skipped"   { "Yellow" }
        default     { "White" }
    }
    Write-Host ("  {0,-20} {1}" -f $r.Name, $r.Status) -ForegroundColor $color
}

# ============================================================
# Reboot prompt
# ============================================================
if ($needsReboot) {
    Write-Host ""
    Write-Host "============================================" -ForegroundColor Yellow
    Write-Host "  A REBOOT IS REQUIRED to finish WSL2 setup." -ForegroundColor Yellow
    Write-Host "" -ForegroundColor Yellow
    Write-Host "  After rebooting, run this script again" -ForegroundColor Yellow
    Write-Host "  from the Start Menu to install the" -ForegroundColor Yellow
    Write-Host "  remaining WSL prerequisites (gcc," -ForegroundColor Yellow
    Write-Host "  partclone, xorriso)." -ForegroundColor Yellow
    Write-Host "============================================" -ForegroundColor Yellow
    Write-Host ""
    $reboot = Read-Host "  Reboot now? (y/n)"
    if ($reboot -eq 'y') {
        Restart-Computer -Force
    }
} else {
    $allOk = ($results | Where-Object { $_.Status -in @("Missing") }).Count -eq 0
    $skipped = ($results | Where-Object { $_.Status -eq "Skipped" }).Count
    if ($allOk -and $skipped -eq 0) {
        Write-Host ""
        Write-Host "  All prerequisites are installed!" -ForegroundColor Green
    } elseif ($skipped -gt 0) {
        Write-Host ""
        Write-Host "  Some prerequisites were skipped." -ForegroundColor Yellow
        Write-Host "  You can re-run this script at any time." -ForegroundColor Yellow
    }
}

Write-Host ""
Read-Host "Press Enter to exit"
