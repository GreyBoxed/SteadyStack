#!/usr/bin/env bash
#
# Walk a trading day on a laptop.
#
# The box gets systemd timers in Phase 4; macOS has no systemd, and a launchd
# plist would be throwaway work for a schedule that is about to be written
# properly somewhere else. This is the stand-in: the same management commands,
# in the same order, with the same guards.
#
# It cannot compress a day. `ladder_tick` derives its round number from the
# wall clock -- which is deliberate, and is what lets a day resume where the
# time of day implies -- so firing sixty rounds in a row would claim the same
# round sixty times and the idempotency guard would reject fifty-nine of them.
#
#   ./ops/local/run_day.sh              plan the day and run one placing round
#   ./ops/local/run_day.sh --watch      stay up: a round every 30s until the
#                                       window closes, the close's rounds from
#                                       15:00, then settle
#   ./ops/local/run_day.sh --settle     close a day that already ran
#   ./ops/local/run_day.sh --date 2026-09-21 --settle
#
set -euo pipefail

cd "$(dirname "$0")/../.."

DATE=""
MODE="once"
DRY=0

while [ $# -gt 0 ]; do
    case "$1" in
        --watch)   MODE="watch"; shift ;;
        --settle)  MODE="settle"; shift ;;
        --date)    DATE="${2:-}"; shift 2 ;;
        --dry-run) DRY=1; shift ;;
        -h|--help) sed -n '3,21p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

DATE_ARG=""
if [ -n "$DATE" ]; then DATE_ARG="--date $DATE"; fi

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
note() { printf '   %s\n' "$*"; }
fail() { printf '\n\033[31m!! %s\033[0m\n' "$*" >&2; exit 1; }

run() {
    note "manage.py $*"
    if [ "$DRY" -eq 1 ]; then return 0; fi
    uv run python manage.py "$@"
}

# -- preflight ---------------------------------------------------------------
# Each of these has cost somebody an hour at least once. Checking takes a
# second and says which one it is.

say "Preflight"

if ! uv run python manage.py preflight; then
    fail "the problems above will stop the day; nothing has run"
fi

# -- settle only -------------------------------------------------------------

if [ "$MODE" = "settle" ]; then
    say "Settling"
    # One round, sized as the last: whatever is left is due now.
    # shellcheck disable=SC2086
    run market_fallback $DATE_ARG || note "fallback declined (it often should)"
    # shellcheck disable=SC2086
    run settle_day $DATE_ARG
    # shellcheck disable=SC2086
    run snapshot_valuation $DATE_ARG
    say "Done"
    exit 0
fi

# -- the morning -------------------------------------------------------------

say "09:00 — session check"
run check_kite_session

say "Instrument master"
run refresh_instruments

say "Tradesheet — rank, select, size"
# A day already placing keeps its plan: regenerating is refused once orders
# are out, and that refusal is the script resuming, not failing.
# shellcheck disable=SC2086
run generate_tradesheet $DATE_ARG --actor "$(whoami)" \
    || note "kept today's plan -- it is locked once orders are out"

say "Authorize — the plan is automatic, placing it is not (M15f)"
if [ -t 0 ]; then
    printf "   Authorize this plan to trade? [y/N] "
    read -r answer
    if [ "$answer" = "y" ] || [ "$answer" = "Y" ]; then
        run authorize_tradesheet --actor "$(whoami)" || note "already authorized, or refused (above)"
    else
        note "Not authorized: the ladder will place nothing. Authorize from Today when ready."
    fi
else
    note "Not a terminal: authorize from Today in the browser; until then nothing is placed."
fi

say "One placing round"
# A rebalance sale's round first: it takes at most one order, and the buy
# round's own count of the rate window then leaves it the rest.
# shellcheck disable=SC2086
run rebalance_tick $DATE_ARG || note "sell round declined; the day carries on"
# shellcheck disable=SC2086
run ladder_tick $DATE_ARG || note "round declined; the day carries on"

if [ "$MODE" = "once" ]; then
    cat <<'TEXT'

   That is the day planned and one round run. Each round places what every
   name's schedule owes, at most five orders per thirty seconds across all of
   them; a stale order is cancelled and replaced, never modified.

   To watch the rest of it:        ./ops/local/run_day.sh --watch
   To close it now:                ./ops/local/run_day.sh --settle
TEXT
    exit 0
fi

# -- the day ------------------------------------------------------------------

read -r INTERVAL CLOSES FALLBACK_AT CLOSE_END < <(uv run python -c "
import sys; sys.path.insert(0, 'src')
import os; os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.dev')
import django; django.setup()
from configuration.store import current
c = current()
print(c.placer_interval_seconds, c.placement_window_close.strftime('%H%M'),
      c.market_fallback_time.strftime('%H%M'), c.close_window_end.strftime('%H%M'))
")

# Sleep to just past the next round boundary rather than a fixed interval.
# The round number comes from the clock, so a fixed sleep drifts by however
# long each round takes and sooner or later steps over a boundary, and that
# round never runs. The boundaries fall on the interval (09:45:00, 09:45:30...).
next_boundary() {
    now=$(date +%s)
    sleep $(( INTERVAL - now % INTERVAL + 1 ))
}

say "Watching until $CLOSES, a round every ${INTERVAL}s"
note "Ctrl-C is safe: every round is claimed in the database before it acts."

while [ "$(date +%H%M)" -lt "$CLOSES" ]; do
    next_boundary
    # shellcheck disable=SC2086
    run rebalance_tick $DATE_ARG || note "sell round declined; the day carries on"
    # shellcheck disable=SC2086
    run ladder_tick $DATE_ARG || note "round declined; the day carries on"
done

say "Window closed — resting orders have until $FALLBACK_AT to fill"
while [ "$(date +%H%M)" -lt "$FALLBACK_AT" ]; do sleep 20; done

say "The close — market in rounds until $CLOSE_END"
while [ "$(date +%H%M)" -le "$CLOSE_END" ]; do
    # shellcheck disable=SC2086
    run rebalance_tick $DATE_ARG || note "sell close round declined"
    # shellcheck disable=SC2086
    run market_fallback $DATE_ARG || note "close round declined"
    next_boundary
done

# settle_day without --date refuses before the session closes (15:30).
if [ -z "$DATE" ]; then
    say "Waiting for the close at 15:30"
    while [ "$(date +%H%M)" -lt 1530 ]; do sleep 20; done
fi

say "Settling"
# shellcheck disable=SC2086
run settle_day $DATE_ARG
# shellcheck disable=SC2086
run snapshot_valuation $DATE_ARG

say "Done"
