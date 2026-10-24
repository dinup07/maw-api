#!/usr/bin/env bash
# Restore a backup made by backup.sh. Stops MAW during the restore.
#   sudo bash /opt/maw/deploy/restore.sh /opt/maw/backups/maw-20261020T023000Z.sql.gz
set -euo pipefail
APP_DIR="${APP_DIR:-/opt/maw}"
FILE="${1:?usage: restore.sh <backup.sql.gz>}"
cd "$APP_DIR"
COMPOSE=(docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml)
read -r -p "This replaces the current database with $FILE. Type 'restore' to continue: " answer
[ "$answer" = "restore" ] || { echo "aborted"; exit 1; }
"${COMPOSE[@]}" stop maw
gunzip -c "$FILE" | "${COMPOSE[@]}" exec -T postgres psql -q -U maw -d maw -v ON_ERROR_STOP=1
"${COMPOSE[@]}" start maw
echo "restored $FILE"
