#!/bin/sh
# Daily pg_dump of the database into /backups (mounted from ./backups on the host).
# Connection comes from PGHOST/PGUSER/PGPASSWORD/PGDATABASE. Dumps older than
# BACKUP_KEEP_DAYS (default 14) are deleted. Dumps contain voucher codes: keep them private.
set -u
mkdir -p /backups
while true; do
    file="/backups/voucher-$(date +%Y%m%d-%H%M).dump"
    if pg_dump --format=custom --file="$file.tmp"; then
        mv "$file.tmp" "$file"
        echo "backup written: $file"
    else
        rm -f "$file.tmp"
        echo "backup FAILED"
    fi
    find /backups -name 'voucher-*.dump' -mtime +"${BACKUP_KEEP_DAYS:-14}" -delete
    sleep "${BACKUP_INTERVAL_SECONDS:-86400}"
done
