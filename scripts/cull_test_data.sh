#!/usr/bin/env bash
# Cull synthetic test rows, identified by the reserved install UUID prefix.
#
# Safe by construction: only rows whose install_uuid starts with the reserved
# prefix are deleted. Real v4 UUIDs are random and never match, so this can only
# ever remove test traffic that opted in by using the marker. Set DRY_RUN=1 to
# report the match counts without deleting anything.
set -euo pipefail

PREFIX="${TESSERAE_TEST_PREFIX:-7e57c0de-}"
CONTAINER="${TESSERAE_PG_CONTAINER:-tesserae-postgres}"
DRY_RUN="${DRY_RUN:-0}"

psql() { docker exec -i "$CONTAINER" psql -U tesserae -d tesserae -v ON_ERROR_STOP=1 "$@"; }

echo "cull test data: prefix='${PREFIX}' dry_run=${DRY_RUN} at $(date -u +%FT%TZ)"

psql -c "
SELECT 'heartbeat_kinds' AS table, count(*) AS matched FROM heartbeat_kinds WHERE install_uuid LIKE '${PREFIX}%'
UNION ALL SELECT 'heartbeats',      count(*) FROM heartbeats      WHERE install_uuid LIKE '${PREFIX}%'
UNION ALL SELECT 'widget_installs', count(*) FROM widget_installs WHERE install_uuid LIKE '${PREFIX}%'
UNION ALL SELECT 'hits',            count(*) FROM hits            WHERE install_uuid LIKE '${PREFIX}%'
ORDER BY 1;"

if [ "$DRY_RUN" = "1" ]; then
  echo "dry run: no rows deleted"
  exit 0
fi

psql <<EOSQL
BEGIN;
DELETE FROM heartbeat_kinds WHERE install_uuid LIKE '${PREFIX}%';
DELETE FROM heartbeats      WHERE install_uuid LIKE '${PREFIX}%';
DELETE FROM widget_installs WHERE install_uuid LIKE '${PREFIX}%';
DELETE FROM hits            WHERE install_uuid LIKE '${PREFIX}%';
COMMIT;
EOSQL
echo "cull complete"
