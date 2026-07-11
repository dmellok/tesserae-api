#!/usr/bin/env bash
# Nightly PostgreSQL backup for tesserae-api. Runs on the VPS (via cron or the
# systemd timer in systemd/). Dumps the tesserae database to a gzipped SQL file
# and prunes backups older than the retention window. pg_dump is read-only.
set -euo pipefail

# Default lives under the deploy-owned app dir (the /data volume is owned by the
# container uid 999 and is not writable by the deploy user).
BACKUP_DIR="${TESSERAE_BACKUP_DIR:-/opt/tesserae-api/backups}"
RETENTION_DAYS="${TESSERAE_BACKUP_RETENTION_DAYS:-7}"
CONTAINER="${TESSERAE_PG_CONTAINER:-tesserae-postgres}"

mkdir -p "$BACKUP_DIR"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
out="$BACKUP_DIR/tesserae-$stamp.sql.gz"

docker exec "$CONTAINER" pg_dump -U tesserae -d tesserae | gzip >"$out"
find "$BACKUP_DIR" -maxdepth 1 -name 'tesserae-*.sql.gz' -mtime +"$RETENTION_DAYS" -delete
echo "backup written: $out ($(du -h "$out" | cut -f1))"
