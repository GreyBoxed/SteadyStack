#!/usr/bin/env bash
#
# Write the services' environment into tmpfs, from Secrets Manager.
#
# /run is tmpfs, so the files are gone after every reboot -- which is the
# point: no secret sits on the volume that is snapshotted nightly (M48). This
# script puts them back. steadystack-env.service runs it at boot, before every
# other unit; bootstrap runs it on first boot. Idempotent.
#
# Two files:
#   /run/steadystack/env        every unit's environment, readable by the service
#                           user. The runtime role only.
#   /run/steadystack/owner.env  the schema owner's URL, root's alone. systemd reads
#                           it for the backup unit; an operator sources it to
#                           migrate. The web process never has it, so a bug
#                           there cannot reach UPDATE on the append-only tables.
#
# What it needs that is not secret -- region, secret prefix, domain, bucket,
# topic -- bootstrap wrote once to /etc/steadystack/bootstrap.env.
#
set -euo pipefail

# shellcheck source=/dev/null
. /etc/steadystack/bootstrap.env

APP_USER=steadystack
ENV_FILE=/run/steadystack/env
OWNER_FILE=/run/steadystack/owner.env

# At boot the network, the instance role's credentials or Secrets Manager can
# be a minute from answering. Every unit requires this one and none retries a
# failed dependency, so it waits here rather than failing them all.
fetch() {
    local attempt
    for attempt in $(seq 1 30); do
        if aws secretsmanager get-secret-value \
            --region "$REGION" --secret-id "$SECRET_PREFIX/$1" \
            --query SecretString --output text 2>/dev/null; then
            return 0
        fi
        sleep 5
    done
    echo "write-env: could not fetch $SECRET_PREFIX/$1 after $attempt attempts" >&2
    return 1
}

install -d -o root -g "$APP_USER" -m 0750 /run/steadystack

umask 077

# Whole or not at all: a service starting mid-write would read half a file.
write() {  # write <path> <group> <mode>, contents on stdin
    local tmp
    tmp=$(mktemp "$1.XXXXXX")
    cat > "$tmp"
    chown root:"$2" "$tmp"
    chmod "$3" "$tmp"
    mv -f "$tmp" "$1"
}

# Fetched into variables first: a failure inside a heredoc's $(...) would not
# stop the script, and would write an empty secret where a real one belongs.
DJANGO_SECRET_KEY=$(fetch django_secret_key)
DATABASE_URL=$(fetch database_url)
DATABASE_MIGRATION_URL=$(fetch database_owner_url)
KITE_API_KEY=$(fetch kite_api_key)
KITE_API_SECRET=$(fetch kite_api_secret)
KITE_TOKEN_KEY=$(fetch kite_token_key)
KITE_USER_ID=$(fetch kite_user_id)

write "$ENV_FILE" "$APP_USER" 0640 <<EOF
DJANGO_SETTINGS_MODULE=config.settings.prod
DJANGO_ALLOWED_HOSTS=$DOMAIN,127.0.0.1,localhost
DJANGO_SECRET_KEY=$DJANGO_SECRET_KEY
DATABASE_URL=$DATABASE_URL
KITE_API_KEY=$KITE_API_KEY
KITE_API_SECRET=$KITE_API_SECRET
KITE_TOKEN_KEY=$KITE_TOKEN_KEY
KITE_USER_ID=$KITE_USER_ID
STEADYSTACK_BACKUP_BUCKET=$BACKUP_BUCKET
STEADYSTACK_ALERTS_TOPIC=$ALERTS_TOPIC
STEADYSTACK_REGION=$REGION
ORDER_LOCK_PATH=/var/lib/steadystack/orders.lock
EOF

write "$OWNER_FILE" root 0600 <<EOF
DATABASE_MIGRATION_URL=$DATABASE_MIGRATION_URL
EOF
