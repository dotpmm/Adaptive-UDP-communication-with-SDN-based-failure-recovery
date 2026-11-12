#!/usr/bin/env bash
# Run the full experiment matrix.  Usage (from repo root):
#   sudo env "PATH=$PATH" REPS=3 ./experiments/run_all.sh
set -u
cd "$(dirname "$0")/.."
REPS=${REPS:-3}

# scenario name -> "duration|event schedule"
declare -A SCEN=(
  [single]="30|8:down:s2-s4,20:up:s2-s4"
  [double]="36|8:down:s2-s4,14:down:s3-s5,20:up:s3-s5,26:up:s2-s4"
  [flap]="30|8:down:s2-s4,12:up:s2-s4,16:down:s2-s4,20:up:s2-s4"
  [none]="30|"
)
# config name -> "mode recovery"
declare -A CFG=(
  [static-noapp]="static off"
  [static-app]="static on"
  [dynamic-noapp]="dynamic off"
  [dynamic-app]="dynamic on"
)

for sc in single double flap none; do
  IFS='|' read -r DUR EVT <<< "${SCEN[$sc]}"
  for cfg in static-noapp static-app dynamic-noapp dynamic-app; do
    # the no-failure control only needs the static baseline and the full system
    if [ "$sc" = none ] && [ "$cfg" != static-noapp ] && [ "$cfg" != dynamic-app ]; then continue; fi
    read -r MODE REC <<< "${CFG[$cfg]}"
    for i in $(seq 1 "$REPS"); do
      echo "=== $sc / $cfg / run$i ==="
      mn -c >/dev/null 2>&1
      python3 experiments/run_experiment.py --mode "$MODE" --recovery "$REC" \
        --duration "$DUR" --events "$EVT" --out "results/$sc/$cfg/run$i" || echo "run failed"
    done
  done
done
mn -c >/dev/null 2>&1
echo "All runs finished. Now: python3 analysis/analyze.py"
