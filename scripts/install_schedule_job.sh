#!/data/data/com.termux/files/usr/bin/bash
# Register the scheduler with Android's JobScheduler so tasks still fire when
# Termux has been killed.
#
# Why not cron: crond dies with Termux, and Termux:Boot plus crond does not
# reliably resume after a real reboot. A persisted JobScheduler job lives in
# Android under com.termux.api, not in a Termux session, so it survives both.
#
# Why 15 minutes: that is Android's floor for a periodic job. Doze also defers
# jobs to maintenance windows, and the gaps grow the longer the phone sits
# idle, so treat the period as "about every 15 minutes, probably longer when
# the phone is asleep" - not as a clock.
#
#   bash scripts/install_schedule_job.sh            # install or update
#   bash scripts/install_schedule_job.sh --cancel   # remove
#   termux-job-scheduler -p                         # list pending jobs

set -euo pipefail

JOB_ID=4242
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNNER="$HOME/.android-agent-schedule-tick.sh"
PERIOD_MS=900000

if ! command -v termux-job-scheduler >/dev/null 2>&1; then
    echo "termux-job-scheduler not found. Install it with:" >&2
    echo "  pkg install termux-api" >&2
    echo "and install the Termux:API app from F-Droid." >&2
    exit 1
fi

if [ "${1:-}" = "--cancel" ]; then
    termux-job-scheduler --cancel --job-id "$JOB_ID"
    rm -f "$RUNNER"
    echo "Scheduler job $JOB_ID cancelled."
    exit 0
fi

# A tiny wrapper, because JobScheduler takes one script path and no arguments.
# It loads the same .env the bot uses so the two cannot drift apart.
cat > "$RUNNER" <<EOF
#!/data/data/com.termux/files/usr/bin/bash
cd "$REPO_DIR" || exit 1
if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    . ./.env
    set +a
fi
exec python -m android_agent.schedule >> "\$HOME/telegram_agent_v2/schedule-job.log" 2>&1
EOF
chmod +x "$RUNNER"

termux-job-scheduler \
    --script "$RUNNER" \
    --job-id "$JOB_ID" \
    --period-ms "$PERIOD_MS" \
    --persisted true \
    --network any \
    --battery-not-low false

cat <<'NOTE'

Installed. Two things decide whether it actually fires:

  1. Settings > Apps > Termux:API > Battery > Unrestricted
     Settings > Apps > Termux     > Battery > Unrestricted
     The job is owned by Termux:API and executed by Termux. If either is
     battery-restricted, Android drops the job silently.

  2. Settings > System > Developer options > Disable child process
     restrictions (Android 14+), then reboot.

Check it is registered:   termux-job-scheduler -p
Watch it run:             tail -f ~/telegram_agent_v2/schedule-job.log

Timing is approximate by design. Android will not run a periodic job more
often than every 15 minutes and defers it further while the phone is dozing,
so a 07:00 daily task fires at the first tick at or after 07:00.
NOTE
