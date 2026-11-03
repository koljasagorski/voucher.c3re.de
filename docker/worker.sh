#!/bin/sh
# Periodic jobs in a plain loop (one tick = 5 minutes):
#   fetch_mail         every tick   (5 min)
#   send_queued_mail   every tick   (5 min, retries mails that failed to send)
#   process_deadlines  every 2nd    (10 min)
#   purge_old_data     every 288th  (daily)
# A failing job is logged and retried on the next tick; the loop keeps running.
set -u
tick=0
while true; do
    python manage.py fetch_mail || echo "fetch_mail failed"
    python manage.py send_queued_mail || echo "send_queued_mail failed"
    if [ $((tick % 2)) -eq 0 ]; then
        python manage.py process_deadlines || echo "process_deadlines failed"
    fi
    if [ $((tick % 288)) -eq 0 ]; then
        python manage.py purge_old_data || echo "purge_old_data failed"
    fi
    tick=$((tick + 1))
    sleep "${WORKER_TICK_SECONDS:-300}"
done
