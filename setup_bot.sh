#!/bin/bash

# This script automates the environment setup for the Plex Torrent Telegram Bot
# on a Debian-based Linux system (like Ubuntu).
#
# IT ASSUMES:
# 1. You are running this script from the root of the project directory.
# 2. Python 3.11 is already installed.
# 3. 'restart_plex.sh' is present in the project directory.

# --- Configuration ---
PROJECT_DIR=$(pwd) # Get the current working directory.
CURRENT_USER=$(whoami) # Get the current user for the sudoers rule.

# --- Colors for better output ---
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# Exit immediately if a command exits with a non-zero status.
set -e

echo -e "${GREEN}--- Starting Bot Environment Setup ---${NC}"

# --- Step 1: Install System Dependencies ---
echo -e "\n${YELLOW}Step 1: Installing required system packages (libtorrent)...${NC}"
sudo apt-get update
sudo apt-get install -y libtorrent-rasterbar-dev

echo -e "${GREEN}System packages installed successfully.${NC}"


# --- Step 2: Set Permissions for the Plex Restart Script ---
echo -e "\n${YELLOW}Step 2: Locating and setting permissions for 'restart_plex.sh'...${NC}"
WRAPPER_SCRIPT_PATH="$PROJECT_DIR/restart_plex.sh"

if [ -f "$WRAPPER_SCRIPT_PATH" ]; then
    chmod +x "$WRAPPER_SCRIPT_PATH"
    echo "Made 'restart_plex.sh' executable."
else
    echo -e "${RED}ERROR: The 'restart_plex.sh' script was not found in this directory.${NC}"
    echo "Please ensure you are running this from the root of the project directory."
    exit 1
fi


# --- Step 3: Configure Sudoers for Passwordless Restart ---
echo -e "\n${YELLOW}Step 3: Configuring sudoers for passwordless Plex restart...${NC}"
SUDOERS_FILE_PATH="/etc/sudoers.d/99-plex-restart"
SUDOERS_RULE="$CURRENT_USER ALL=(ALL) NOPASSWD: $WRAPPER_SCRIPT_PATH"

echo "This will create a sudoers rule for user '$CURRENT_USER' to run the script."
echo "$SUDOERS_RULE" | sudo tee "$SUDOERS_FILE_PATH" > /dev/null
sudo chmod 0440 "$SUDOERS_FILE_PATH"

echo -e "${GREEN}Sudoers rule created successfully at ${SUDOERS_FILE_PATH}.${NC}"


# --- Step 4: Create Template config.ini (if it doesn't exist) ---
echo -e "\n${YELLOW}Step 4: Checking for 'config.ini'...${NC}"
if [ ! -f "config.ini" ]; then
    echo "No 'config.ini' found. Creating a template..."
    cat << 'EOF' > config.ini
[telegram]
bot_token = PLACE_YOUR_TELEGRAM_BOT_TOKEN_HERE
allowed_user_ids = PLACE_YOUR_NUMERIC_TELEGRAM_USER_ID_HERE

[plex]
plex_url = http://127.0.0.1:32400
plex_token = PLACE_YOUR_PLEX_TOKEN_HERE

[host]
default_save_path = /path/to/your/telegram_downloads
movies_save_path = /path/to/your/plex_movies
tv_shows_save_path = /path/to/your/plex_tv_shows
EOF
    echo -e "${GREEN}'config.ini' template created.${NC}"
else
    echo -e "${GREEN}'config.ini' already exists. Skipping creation.${NC}"
fi


# --- Final Instructions ---
echo -e "\n\n${GREEN}--- Environment Setup Complete! ---${NC}"
echo -e "\n${YELLOW}IMPORTANT: You must now manually complete the following steps:${NC}"
echo "1. Edit the configuration file with your details:"
echo -e "   ${YELLOW}nano config.ini${NC}"
echo ""
echo "2. Create a Python virtual environment and install dependencies:"
echo -e "   ${YELLOW}python3.11 -m venv venv${NC}"
echo -e "   ${YELLOW}source venv/bin/activate${NC}"
echo -e "   ${YELLOW}pip install -r requirements.txt${NC}"
echo ""
echo "3. You can then run the bot with:"
echo -e "   ${YELLOW}python3.11 telegram_bot.py${NC}"
