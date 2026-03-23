#!/usr/bin/env bash
#
# JJP Asset Decryptor — Linux Installer
#
# Installs all prerequisites and sets up the app on a native Linux system.
# Supports Ubuntu/Debian, Fedora/RHEL, and Arch Linux.
#
# Usage:
#   chmod +x install_linux.sh
#   sudo ./install_linux.sh
#
# After installation, run the app with:
#   python3 -m jjp_decryptor
#
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

echo -e "${CYAN}========================================${NC}"
echo -e "${CYAN}  JJP Asset Decryptor — Linux Installer${NC}"
echo -e "${CYAN}========================================${NC}"
echo ""

# Check root
if [ "$(id -u)" -ne 0 ]; then
    echo -e "${RED}This script must be run as root (sudo).${NC}"
    echo "  Usage: sudo $0"
    exit 1
fi

# Detect package manager
PM=""
if command -v apt-get &>/dev/null; then
    PM="apt"
elif command -v dnf &>/dev/null; then
    PM="dnf"
elif command -v pacman &>/dev/null; then
    PM="pacman"
else
    echo -e "${RED}Unsupported package manager.${NC}"
    echo "Please install manually: python3 python3-tk partclone xorriso e2fsprogs pigz ffmpeg"
    exit 1
fi

echo -e "${CYAN}Detected package manager: ${PM}${NC}"
echo ""

install_pkg() {
    local name="$1"
    shift
    echo -e "${CYAN}Installing ${name}...${NC}"
    case $PM in
        apt)    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$@" ;;
        dnf)    dnf install -y -q "$@" ;;
        pacman) pacman -S --noconfirm --needed "$@" ;;
    esac
}

check_cmd() {
    if command -v "$1" &>/dev/null; then
        echo -e "  ${GREEN}[OK]${NC} $1"
        return 0
    else
        echo -e "  ${RED}[MISSING]${NC} $1"
        return 1
    fi
}

# Update package lists
echo -e "${CYAN}Updating package lists...${NC}"
case $PM in
    apt)    apt-get update -qq ;;
    dnf)    dnf check-update -q 2>/dev/null || true ;;
    pacman) pacman -Sy --noconfirm ;;
esac
echo ""

# Python 3
echo -e "${CYAN}=== Python 3 ===${NC}"
if ! command -v python3 &>/dev/null; then
    case $PM in
        apt)    install_pkg "Python 3" python3 python3-tk python3-venv ;;
        dnf)    install_pkg "Python 3" python3 python3-tkinter ;;
        pacman) install_pkg "Python 3" python tk ;;
    esac
else
    echo -e "  ${GREEN}[OK]${NC} python3 ($(python3 --version 2>&1))"
fi

# Check for tkinter
if ! python3 -c "import tkinter" 2>/dev/null; then
    echo -e "  ${YELLOW}tkinter not found, installing...${NC}"
    case $PM in
        apt)    install_pkg "tkinter" python3-tk ;;
        dnf)    install_pkg "tkinter" python3-tkinter ;;
        pacman) install_pkg "tkinter" tk ;;
    esac
fi
echo ""

# partclone
echo -e "${CYAN}=== partclone ===${NC}"
if ! check_cmd partclone.ext4; then
    install_pkg "partclone" partclone
    check_cmd partclone.ext4
fi
echo ""

# xorriso
echo -e "${CYAN}=== xorriso ===${NC}"
if ! check_cmd xorriso; then
    install_pkg "xorriso" xorriso
    check_cmd xorriso
fi
echo ""

# debugfs (e2fsprogs)
echo -e "${CYAN}=== debugfs (e2fsprogs) ===${NC}"
if ! check_cmd debugfs; then
    case $PM in
        apt)    install_pkg "e2fsprogs" e2fsprogs ;;
        dnf)    install_pkg "e2fsprogs" e2fsprogs ;;
        pacman) install_pkg "e2fsprogs" e2fsprogs ;;
    esac
    check_cmd debugfs
fi
echo ""

# pigz
echo -e "${CYAN}=== pigz ===${NC}"
if ! check_cmd pigz; then
    install_pkg "pigz" pigz
    check_cmd pigz
fi
echo ""

# ffmpeg
echo -e "${CYAN}=== ffmpeg ===${NC}"
if ! check_cmd ffmpeg; then
    install_pkg "ffmpeg" ffmpeg
    check_cmd ffmpeg
fi
echo ""

# Summary
echo -e "${CYAN}========================================${NC}"
echo -e "${CYAN}  Prerequisites Summary${NC}"
echo -e "${CYAN}========================================${NC}"
MISSING=0
for cmd in python3 partclone.ext4 xorriso debugfs pigz ffmpeg; do
    if ! check_cmd "$cmd"; then
        MISSING=$((MISSING + 1))
    fi
done
echo ""

if [ "$MISSING" -eq 0 ]; then
    echo -e "${GREEN}All prerequisites installed successfully!${NC}"
    echo ""
    echo -e "To run the app:"
    echo -e "  ${CYAN}cd /path/to/jjp-decryptor${NC}"
    echo -e "  ${CYAN}python3 -m jjp_decryptor${NC}"
    echo ""
    echo -e "Or install from the repo:"
    echo -e "  ${CYAN}git clone https://github.com/davidvanderburgh/jjp-decryptor.git${NC}"
    echo -e "  ${CYAN}cd jjp-decryptor${NC}"
    echo -e "  ${CYAN}python3 -m jjp_decryptor${NC}"
else
    echo -e "${RED}${MISSING} prerequisite(s) could not be installed.${NC}"
    echo "Check the output above for errors."
fi
