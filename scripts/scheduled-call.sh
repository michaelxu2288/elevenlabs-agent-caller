#!/usr/bin/env bash
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"
task="$1"
day="$2"
slots="$3"
slack_min="$4"
mode="${5:-}"
marker="calls/.reached-$(basename "$task" .toml)-$day"
now=$(date +%s)
echo "=== $(date '+%F %T') scheduled call: $task (day $day, slots $slots)"
if [ "$(date +%F)" != "$day" ]; then
  echo "not $day, skipping"
  exit 0
fi
if [ -e "$marker" ]; then
  echo "already reached today, skipping"
  exit 0
fi
in_slot=""
for slot in $slots; do
  s=$(date -j -f "%F %H:%M:%S" "$day $slot:00" +%s)
  if [ "$now" -ge "$s" ] && [ "$now" -le $((s + slack_min * 60)) ]; then
    in_slot="$slot"
  fi
done
if [ -z "$in_slot" ] && [ "$mode" != "--dry" ]; then
  echo "not within $slack_min min of a slot (late wake?), skipping"
  exit 0
fi
lid=$(ioreg -r -k AppleClamshellState -d 4 | grep -o '"AppleClamshellState" = [A-Za-z]*' | awk '{print $3}')
held=$(pmset -g | awk '/SleepDisabled/ {print $2}')
echo "lid closed: ${lid:-unknown}, nosleep hold: ${held:-0}"
if [ "$lid" = "Yes" ] && [ "$held" != "1" ] && [ "$mode" != "--dry" ]; then
  echo "lid closed without nosleep, so this is only a brief dark wake; skipping"
  exit 0
fi
if [ "$mode" = "--dry" ]; then
  ./vc doctor --online
  ./vc call "$task" --no-redial ${CALL_FLAGS:-}
  exit 0
fi
caffeinate -i -s ./vc call "$task" --yes ${CALL_FLAGS:-}
.venv/bin/python - "$now" "$marker" <<'EOF'
import json
import sys
from pathlib import Path

start, marker = float(sys.argv[1]), sys.argv[2]
runs = [d for d in Path("calls").iterdir() if d.is_dir() and d.name[:1].isdigit() and d.stat().st_mtime >= start]
if not runs:
    print("no call record found")
    sys.exit(0)
reached = False
for d in sorted(runs, key=lambda p: p.stat().st_mtime):
    outcome = json.loads((d / "outcome.json").read_text() or "{}").get("outcome", "")
    call = json.loads((d / "call.json").read_text() or "{}")
    turns = (call.get("stats") or {}).get("turns", 0)
    print(f"record {d.name}: outcome={outcome!r} turns={turns} error={call.get('error')!r}")
    if not call.get("error") and (outcome in ("success", "partial", "failed") or (not outcome and turns >= 2)):
        reached = True
if reached:
    Path(marker).touch()
    print("reached; later slots will skip")
EOF
