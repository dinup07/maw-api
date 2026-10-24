#!/usr/bin/env bash
# Redeploy an earlier commit:  sudo bash /opt/maw/deploy/rollback.sh <commit>
set -euo pipefail
APP_DIR="${APP_DIR:-/opt/maw}"
TARGET="${1:?usage: rollback.sh <commit>}"
cd "$APP_DIR"
git checkout -q --detach "$TARGET"
docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml up -d --build --remove-orphans
echo "now running $(git rev-parse --short HEAD); return to a branch with: BRANCH=<branch> sudo bash deploy/update.sh"
