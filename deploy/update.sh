#!/usr/bin/env bash
# Pull the latest code for the deployed branch, rebuild and restart. Data volumes are kept.
#   sudo bash /opt/maw/deploy/update.sh            # BRANCH=... to switch branches
set -euo pipefail
APP_DIR="${APP_DIR:-/opt/maw}"
cd "$APP_DIR"
BRANCH="${BRANCH:-$(git rev-parse --abbrev-ref HEAD)}"
COMPOSE=(docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml)

PREVIOUS="$(git rev-parse --short HEAD)"
git fetch -q origin "$BRANCH"
git checkout -q "$BRANCH"
git reset -q --hard "origin/$BRANCH"
echo "code: $PREVIOUS -> $(git rev-parse --short HEAD) ($BRANCH)"
echo "roll back with: sudo bash deploy/rollback.sh $PREVIOUS"

"${COMPOSE[@]}" up -d --build --remove-orphans
docker image prune -f >/dev/null
"${COMPOSE[@]}" ps
