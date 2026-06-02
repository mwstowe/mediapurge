#!/bin/bash
set -e

DEST=/opt/mediacleaner
OWNER=sabnzbd:sabnzbd

# Create destination if needed
sudo mkdir -p "$DEST"

# Sync code, excluding config/db/venv/dev files
sudo rsync -av --delete \
    --exclude config.yaml \
    --exclude '*.db' \
    --exclude .venv \
    --exclude __pycache__ \
    --exclude .git \
    --exclude '*.pyc' \
    /mirror/develop/mediacleaner/ "$DEST/"

# Copy example config if no live config exists
if [ ! -f "$DEST/config.yaml" ]; then
    sudo cp "$DEST/config.yaml.example" "$DEST/config.yaml"
    echo ">>> Created config.yaml from example — edit it with your credentials"
fi

# Fix ownership
sudo chown -R "$OWNER" "$DEST"

echo "Deployed to $DEST"
echo ""
echo "Required Gentoo packages:"
echo "  dev-python/flask"
echo "  dev-python/sqlalchemy"
echo "  dev-python/pyyaml"
echo "  dev-python/requests"
echo "  dev-python/bcrypt"
echo "  dev-python/PlexAPI"
echo ""
echo "Install with: sudo emerge -av flask sqlalchemy pyyaml requests bcrypt PlexAPI"
