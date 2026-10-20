#!/usr/bin/env bash
# Dump the MAW database (runs, checkpoints, registered workflows, pgvector data) to a gzip file.
#   sudo APP_DIR=/opt/maw deploy/backup.sh          # installed as /usr/local/bin/maw-backup (cron)
set -euo pipefail
APP_DIR="${APP_DIR:-/opt/maw}"
BACKUP_DIR="${BACKUP_DIR:-$APP_DIR/backups}"
KEEP_DAYS="${KEEP_DAYS:-14}"
cd "$APP_DIR"
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"
FILE="$BACKUP_DIR/maw-$(date -u +%Y%m%dT%H%M%SZ).sql.gz"
docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml exec -T postgres \
  pg_dump -U maw -d maw --clean --if-exists | gzip > "$FILE"
find "$BACKUP_DIR" -name 'maw-*.sql.gz' -mtime +"$KEEP_DAYS" -delete
echo "$(date -u +%FT%TZ) backup written: $FILE ($(du -h "$FILE" | cut -f1))"
