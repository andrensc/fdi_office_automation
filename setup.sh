#!/bin/bash
# fdi_office_automation setup script
# Installs dependencies, configures paths, and installs crontab for any machine.
# Supports: macOS, Linux, WSL2 (Windows)
#
# Usage:
#   bash setup.sh               # interactive setup
#   bash setup.sh --no-crontab  # skip crontab installation

set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31d'
NC='\033[0m'

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
INSTALL_CRONTAB=true
if [ "$1" == "--no-crontab" ]; then INSTALL_CRONTAB=false; fi

echo "========================================"
echo " fdi_office_automation SETUP"
echo "========================================"

# ── Step 1: Detect OS / environment ──────────────────────────────────────────
OS="$(uname -s)"
IS_WSL=false
if grep -qi microsoft /proc/version 2>/dev/null; then
    IS_WSL=true
fi

if [ "$IS_WSL" = true ]; then
    echo -e "${GREEN}Step 1: Detected environment: WSL2 (Windows)${NC}"
else
    echo -e "${GREEN}Step 1: Detected OS: $OS${NC}"
fi

# ── Step 2: Detect sync.com root ─────────────────────────────────────────────
echo -e "${GREEN}Step 2: Locating sync.com folder${NC}"
SYNC_ROOT=""

if [ "$IS_WSL" = true ]; then
    # sync.com on Windows syncs to C:\Users\<user>\Sync\FdI
    # In WSL2 this is accessible at /mnt/c/Users/<user>/Sync/FdI
    WIN_USER=$(cmd.exe /c "echo %USERNAME%" 2>/dev/null | tr -d '\r\n' || true)
    if [ -n "$WIN_USER" ]; then
        WSL_CANDIDATE="/mnt/c/Users/$WIN_USER/Sync/FdI"
        if [ -d "$WSL_CANDIDATE" ]; then
            SYNC_ROOT="$WSL_CANDIDATE"
            echo "  Found sync root (via Windows user '$WIN_USER'): $SYNC_ROOT"
        fi
    fi
fi

if [ -z "$SYNC_ROOT" ]; then
    for candidate in \
        "$HOME/Sync/FdI" \
        "$HOME/sync.com/FdI" \
        "/home/office/Sync/FdI" \
        "/home/fdi/Sync/FdI" \
        "/mnt/c/Users/office/Sync/FdI" \
        "/mnt/c/Users/fdi/Sync/FdI"; do
        if [ -d "$candidate" ]; then
            SYNC_ROOT="$candidate"
            echo "  Found sync root: $SYNC_ROOT"
            break
        fi
    done
fi

if [ -z "$SYNC_ROOT" ]; then
    echo -e "${YELLOW}  Could not auto-detect sync.com folder.${NC}"
    if [ "$IS_WSL" = true ]; then
        echo "  Hint: sync.com on Windows syncs to C:\\Users\\<you>\\Sync\\FdI"
        echo "        which in WSL2 is: /mnt/c/Users/<you>/Sync/FdI"
    fi
    read -rp "  Enter full path to the FdI sync folder: " SYNC_ROOT
    if [ ! -d "$SYNC_ROOT" ]; then
        echo -e "${RED}  Path does not exist: $SYNC_ROOT${NC}"
        echo "  Check that sync.com is running and fully synced, then re-run setup."
        exit 1
    fi
fi

REPO_PATH="$SYNC_ROOT/fdi_office_automation"
SIG_BASE="$SYNC_ROOT/SIG"

# ── Step 3: Detect GRASS python ──────────────────────────────────────────────
echo -e "${GREEN}Step 3: Locating GRASS GIS Python${NC}"
GRASS_PYTHON=""
if [ "$OS" = "Darwin" ]; then
    for app in /Applications/GRASS-8.4.app /Applications/GRASS-8.3.app /Applications/GRASS-8.2.app; do
        if [ -f "$app/Contents/Resources/bin/python3" ]; then
            GRASS_PYTHON="$app/Contents/Resources/bin/python3"
            break
        fi
    done
else
    # Linux / WSL2: check grass --config, fall back to system python3
    if command -v grass &>/dev/null; then
        GRASS_PYTHON="$(grass --config python_path 2>/dev/null || true)"
    fi
    if [ -z "$GRASS_PYTHON" ]; then
        GRASS_PYTHON="$(command -v python3)"
    fi
fi

if [ -z "$GRASS_PYTHON" ]; then
    echo -e "${YELLOW}  GRASS Python not found. OSM update script may not work.${NC}"
    GRASS_PYTHON="$(command -v python3)"
fi
echo "  GRASS Python: $GRASS_PYTHON"

# ── Step 4: Write .env ───────────────────────────────────────────────────────
echo -e "${GREEN}Step 4: Configuring .env${NC}"
ENV_FILE="$SCRIPT_DIR/.env"
if [ ! -f "$ENV_FILE" ]; then
    cp "$SCRIPT_DIR/.env.example" "$ENV_FILE"
    echo "  Created .env from .env.example"
fi
# Set / update SYNC_ROOT in .env
if grep -q "^SYNC_ROOT=" "$ENV_FILE"; then
    sed -i.bak "s|^SYNC_ROOT=.*|SYNC_ROOT=$SYNC_ROOT|" "$ENV_FILE" && rm -f "$ENV_FILE.bak"
    echo "  Updated SYNC_ROOT in .env"
else
    echo "SYNC_ROOT=$SYNC_ROOT" >> "$ENV_FILE"
    echo "  Added SYNC_ROOT to .env"
fi
echo "  SYNC_ROOT=$SYNC_ROOT"

# ── Step 5: Python virtual environment ───────────────────────────────────────
echo -e "${GREEN}Step 5: Python virtual environment${NC}"
if [ ! -d "$SCRIPT_DIR/venv" ]; then
    python3 -m venv "$SCRIPT_DIR/venv"
fi
source "$SCRIPT_DIR/venv/bin/activate"
pip install --quiet --upgrade pip
pip install --quiet -r "$SCRIPT_DIR/requirements.txt"
pip install --quiet watchdog pyyaml
echo "  Dependencies installed"

# ── Step 6: Directories ───────────────────────────────────────────────────────
echo -e "${GREEN}Step 6: Creating runtime directories${NC}"
mkdir -p "$SCRIPT_DIR/logs"
mkdir -p "$SCRIPT_DIR/outputs_admin"
echo "  logs/ and outputs_admin/ ready"

# ── Step 7: Install crontab ──────────────────────────────────────────────────
if [ "$INSTALL_CRONTAB" = true ]; then
    echo -e "${GREEN}Step 7: Installing crontab${NC}"
    PYTHON3="$(command -v python3)"

    if [ "$IS_WSL" = true ]; then
        echo -e "${YELLOW}  WSL2 detected: cron is not running by default.${NC}"
        echo "  Installing crontab — but you must also enable cron in WSL2:"
        echo "    sudo service cron start"
        echo "  To auto-start cron on WSL2 boot, see README.md - Windows section."
    fi

    # Substitute this machine's paths into the crontab template
    CRON_CONTENT=$(sed \
        -e "s|/Users/g/Sync/FdI/fdi_office_automation|$REPO_PATH|g" \
        -e "s|/Users/g/Sync/FdI|$SYNC_ROOT|g" \
        -e "s|/Applications/GRASS-8.4.app/Contents/Resources/bin/python3|$GRASS_PYTHON|g" \
        -e "s|/usr/bin/python3|$PYTHON3|g" \
        "$SCRIPT_DIR/scripts/crontab.export")

    # Merge: remove existing FdI jobs, add fresh ones
    (crontab -l 2>/dev/null | grep -v "fdi_office_automation"; echo "$CRON_CONTENT") | crontab -
    echo "  Crontab installed. Active FdI jobs:"
    crontab -l | grep "fdi_office_automation" | sed 's/^/    /'
else
    echo -e "${YELLOW}Step 7: Skipping crontab (--no-crontab)${NC}"
fi

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN} Setup Complete!${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
echo "  Repo:      $REPO_PATH"
echo "  SIG base:  $SIG_BASE"
echo "  GRASS py:  $GRASS_PYTHON"
if [ "$IS_WSL" = true ]; then
    echo ""
    echo -e "${YELLOW}  WSL2 reminder: run 'sudo service cron start' to activate the scheduler.${NC}"
    echo "  See README.md - Windows section for auto-start instructions."
fi
echo ""
echo "Verify with a dry run:"
echo "  python3 $REPO_PATH/scripts/overnight_predios_processor.py --dry-run"
echo ""
echo "See README.md - 'Office Fixed Computer Setup' for full instructions."
