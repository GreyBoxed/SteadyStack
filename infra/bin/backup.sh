#!/usr/bin/env bash
#
# The nightly dump (M49).
#
# Two things make this a backup rather than a file: it fails loudly, and the
# restore is tested. A dump that errors and leaves yesterday's object in place
# looks exactly like a dump that worked, right up until the day it matters --
# so this refuses to upload anything it could not verify, and raises an alert
# when it cannot.
#
# The instance can PutObject and nothing else (see iam.tf). It cannot read its
# own backups and cannot delete them; retention is the bucket's lifecycle
# rule. A box that can delete its own backups is a box whose backups a
# compromise takes with it.
#
set -euo pipefail

: "${STEADYSTACK_BACKUP_BUCKET:?the backup bucket is not configured}"
: "${STEADYSTACK_REGION:?the region is not configured}"
: "${DATABASE_MIGRATION_URL:?the owner connection is not configured}"

STAMP="$(date -u +%Y/%m/%d/steadystack-%Y%m%dT%H%M%SZ)"
DUMP="$(mktemp -t steadystack-dump.XXXXXX)"
trap 'rm -f "$DUMP"' EXIT

alert() {
    echo "{\"level\":\"ERROR\",\"logger\":\"steadystack.backup\",\"message\":\"$1\"}" >&2
    if [ -n "${STEADYSTACK_ALERTS_TOPIC:-}" ]; then
        aws sns publish --region "$STEADYSTACK_REGION" \
            --topic-arn "$STEADYSTACK_ALERTS_TOPIC" \
            --subject "steadystack backup failed" \
            --message "$1" >/dev/null || true
    fi
    exit 1
}

# Custom format: it compresses, and pg_restore can read one table out of it
# without replaying the whole thing. Privileges are kept: the REVOKEs are what
# make the ledger, the lots and the audit trail append-only, and a restore
# without them would be a database the runtime role can rewrite.
pg_dump --format=custom --no-owner \
        --file="$DUMP" "$DATABASE_MIGRATION_URL" \
    || alert "pg_dump failed; no object was uploaded"

# An empty or trivially small dump is a failed dump that exited zero.
SIZE=$(stat -c %s "$DUMP" 2>/dev/null || stat -f %z "$DUMP")
[ "$SIZE" -gt 4096 ] || alert "the dump is only $SIZE bytes; refusing to upload it"

# Read it back before trusting it. pg_restore --list parses the archive's own
# table of contents, so a truncated or corrupt file fails here rather than in
# six months.
pg_restore --list "$DUMP" >/dev/null \
    || alert "the dump does not parse; refusing to upload it"

aws s3 cp "$DUMP" "s3://$STEADYSTACK_BACKUP_BUCKET/$STAMP.dump" \
        --region "$STEADYSTACK_REGION" --only-show-errors \
    || alert "the dump could not be uploaded to s3://$STEADYSTACK_BACKUP_BUCKET"

echo "{\"level\":\"INFO\",\"logger\":\"steadystack.backup\",\"message\":\"backup uploaded\",\"key\":\"$STAMP.dump\",\"bytes\":$SIZE}"
