#!/bin/bash
set -e

DEST=/opt/mediacleaner
OWNER=sabnzbd:sabnzbd

# Create destination if needed
sudo mkdir -p "$DEST"

# Sync code, excluding config/db/venv
sudo rsync -av --delete \
    --exclude config.yaml \
    --exclude '*.db' \
    --exclude .venv \
    --exclude __pycache__ \
    --exclude .git \
    --exclude '*.pyc' \
    /mirror/develop/mediacleaner/ "$DEST/"

# Set up venv if missing
if [ ! -d "$DEST/.venv" ]; then
    sudo -u sabnzbd python3 -m venv "$DEST/.venv"
fi

# Install/update deps
sudo -u sabnzbd "$DEST/.venv/bin/pip" install -e "$DEST" --quiet

# Copy example config if no live config exists
if [ ! -f "$DEST/config.yaml" ]; then
    sudo cp "$DEST/config.yaml.example" "$DEST/config.yaml"
    echo ">>> Created config.yaml from example — edit it with your credentials"
fi

# Fix ownership
sudo chown -R "$OWNER" "$DEST"

echo "Deployed to $DEST"
