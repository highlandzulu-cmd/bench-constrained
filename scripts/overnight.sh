#!/usr/bin/env bash
# Run harnesses ONE AFTER ANOTHER (each with the usual 4 pods at a time) so the load stays steady and comparable.
# Keeps going if one harness fails. Saves a backup of all results after each one and writes a summary at the end.
#
#   cd ~/bench-constrained && nohup scripts/overnight.sh kilo > overnight.log 2>&1 &
#   (then in the morning:  cat ~/bench-constrained/night-*-summary.md)
cd "$(dirname "$0")/.." || exit 1
stamp=$(date +%m%d-%H%M)
index="results/night-$stamp.index"
: > "$index"
if pgrep -f "bench run" > /dev/null; then echo "another 'bench run' is already going; not starting a second one"; exit 1; fi
for h in "$@"; do
  echo "=== $(date '+%F %T') starting $h"
  .venv/bin/bench run --harness "$h" --profile tiny --repeats 1 --run-id "night-$stamp-$h" --code-from-configmap > "night-$stamp-$h.log" 2>&1
  echo "=== $(date '+%F %T') $h finished with exit code $?"
  tail -6 "night-$stamp-$h.log"
  echo "$h=results/night-$stamp-$h" >> "$index"
  tar czf "$HOME/bench-results-backup-$stamp.tgz" results && echo "(results backed up to ~/bench-results-backup-$stamp.tgz)"
done
# the earlier 13-task runs (edit this list if the run names differ), plus tonight's
earlier="loop=results/1105-211130 dsh=results/1106-004735 codex=results/1106-143638 pi=results/1106-145543 goose=results/1106-151807 cline=results/1106-153755"
out="night-$stamp-summary.md"
{ echo "# Summary written $(date)"; echo; .venv/bin/python scripts/compare_runs.py --compact $earlier $(cat "$index"); } > "$out" 2>&1
echo "=== all done $(date '+%F %T'). Summary in ~/bench-constrained/$out"
cat "$out"
