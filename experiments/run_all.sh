
#!/usr/bin/env bash

set -u

# Run from the project root, or invoke this script by its path.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-python3}"
DURATION="${DURATION:-45}"
FAILURE_AFTER="${FAILURE_AFTER:-15}"
FAILURE_DURATION="${FAILURE_DURATION:-8}"
RATE="${RATE:-50}"

SCRIPT="experiments/run_experiment.py"

echo "Adaptive UDP/SDN experiment suite"
echo "Project root: $ROOT"
echo "Duration: $DURATION seconds per run"
echo

run_experiment() {
    local recovery="$1"
    local failure="$2"

    echo "========================================"
    echo "Recovery: $recovery | Failure: $failure"
    echo "========================================"

    local args=(
        "$SCRIPT"
        --duration "$DURATION"
        --recovery "$recovery"
        --rate "$RATE"
    )

    if [[ "$failure" == "yes" ]]; then
        args+=(
            --failure-after "$FAILURE_AFTER"
            --failure-duration "$FAILURE_DURATION"
        )
    else
        args+=(--no-failure)
    fi

    if sudo -E "$PYTHON" "${args[@]}"; then
        echo "Run completed."
    else
        echo "Run FAILED. Check the error and the results directory."
        return 1
    fi

    echo
}

failures=0

run_experiment on no  || failures=$((failures + 1))
run_experiment off no || failures=$((failures + 1))
run_experiment on yes || failures=$((failures + 1))
run_experiment off yes || failures=$((failures + 1))

echo "========================================"
echo "Experiment suite finished."
echo "Failed runs: $failures"
echo "Inspect results/ for individual logs and summaries."
echo "========================================"

if [[ "$failures" -ne 0 ]]; then
    exit 1
fi