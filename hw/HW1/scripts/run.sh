#!/usr/bin/env bash
# HW1 launcher: paths, prerequisites, locking, and terminal/master logging.
# Workflow functions live in scripts/workflow.py.

set -uo pipefail

HW1_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)" || exit 1
export HW1_ROOT

usage() {
    cat <<'USAGE'
Usage: bash scripts/run.sh DESIGN {pre|post|synth|tune|all} [Area|Delay|Between|all] [options]
       bash scripts/run.sh all all [Area|Delay|Between|all] [options]
       bash scripts/run.sh collect

Options:
  --period NS             Single synthesis period
  --jobs N                Global parallel task limit (default: 1)
  --resume                Resume tune/all with matching inputs
  --round N               Explicit post/select round
  --max-attempts N        Maximum synthesis attempts per search
  --tolerance NS          Nonnegative-slack convergence tolerance
  --resolution NS         Period adjustment resolution
  --max-period NS         Maximum allowed period
  --wave fsdb|vcd         Waveform format (default: fsdb)
  --verbose               Show full tool output in the terminal (always logged)
  --accept-unconverged    Explicitly inspect/select a passing fallback
  --input-delays-reviewed Acknowledge reviewed check_timing diagnostics

Manual selection:
  bash scripts/run.sh DESIGN select OPT --round N

Server root: /MasterClass/M143010027_ALU/Hws/HW1/
USAGE
}

if [[ $# == 0 || ${1:-} == --help || ${1:-} == -h ]]; then
    usage
    exit 0
fi

VERBOSE_OUTPUT=0
for argument in "$@"; do
    if [[ "$argument" == --verbose ]]; then
        VERBOSE_OUTPUT=1
    fi
done

for cmd in python3 tee flock; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        printf 'ERROR: missing %s on PATH\n' "$cmd" >&2
        exit 1
    fi
done

# Keep the EDA loader path for tool children, but exclude it from Python startup.
if [[ ${LD_LIBRARY_PATH+x} ]]; then
    export HW1_EDA_LD_LIBRARY_PATH_SET=1
    export HW1_EDA_LD_LIBRARY_PATH="$LD_LIBRARY_PATH"
else
    export HW1_EDA_LD_LIBRARY_PATH_SET=0
    export HW1_EDA_LD_LIBRARY_PATH=''
fi

python_clean() (
    unset LD_LIBRARY_PATH
    exec python3 "$@"
)

# Check without inline Python; older interpreters cannot parse the workflow.
PYTHON_VERSION="$(python_clean --version 2>&1)"

if [[ "$PYTHON_VERSION" =~ ^Python\ ([0-9]+)\.([0-9]+) ]]; then
    python_major="${BASH_REMATCH[1]}"
    python_minor="${BASH_REMATCH[2]}"

    if (( python_major < 3 || (python_major == 3 && python_minor < 6) )); then
        printf 'ERROR: Python 3.6+ required; found %s\n' "$PYTHON_VERSION" >&2
        exit 1
    fi
else
    printf 'ERROR: cannot determine python3 version: %s\n' "$PYTHON_VERSION" >&2
    exit 1
fi

WORKFLOW="$HW1_ROOT/scripts/workflow.py"

for file in "$WORKFLOW" "$HW1_ROOT/scripts/helper.py" "$HW1_ROOT/scripts/scheduler.py"; do
    if [[ ! -f "$file" ]]; then
        printf 'ERROR: required Python file missing: %s\n' "$file" >&2
        exit 1
    fi
done

mkdir -p -- "$HW1_ROOT/log" "$HW1_ROOT/result" || exit 1

# flock releases ownership after interruption; never delete another lock.
exec 9>"$HW1_ROOT/.run.lock" || exit 1

if ! flock -n 9; then
    printf 'ERROR: another HW1 command owns %s/.run.lock; retry after it stops.\n' "$HW1_ROOT" >&2
    exit 1
fi

MASTER_LOG="$HW1_ROOT/scripts/run.log"
WORKER_PID_FILE="$HW1_ROOT/log/.active-worker.pid"

if ! touch -- "$MASTER_LOG"; then
    printf 'ERROR: cannot append %s\n' "$MASTER_LOG" >&2
    exit 1
fi

: > "$WORKER_PID_FILE" || exit 1

stop_worker() {
    local target=''

    read -r target < "$WORKER_PID_FILE" || true

    if [[ "$target" =~ ^[0-9]+$ ]]; then
        kill -TERM "$target" 2>/dev/null || true
    fi
}

trap stop_worker INT TERM

worker() {
    local worker_pid status

    printf '$ bash %q' "${BASH_SOURCE[0]}"
    printf ' %q' "$@"
    printf '\n'

    printf 'INFO: Python starts without LD_LIBRARY_PATH; tool children retain the EDA setting.\n'

    python_clean -u "$WORKFLOW" "$@" &
    worker_pid=$!

    printf '%s\n' "$worker_pid" > "$WORKER_PID_FILE"

    wait "$worker_pid"
    status=$?

    if kill -0 "$worker_pid" 2>/dev/null; then
        wait "$worker_pid" || true
    fi

    : > "$WORKER_PID_FILE"

    return "$status"
}

terminal_output() {
    local line
    local heading_lines=0 warning_lines=0

    while IFS= read -r line || [[ -n "$line" ]]; do
        # Full worker history is logged; progress and diagnostics display once.
        if [[ "$line" == HW1_LOG_ONLY:* ]]; then
            continue
        fi

        # Coordinator-approved diagnostics, verbose lines and final summaries.
        if [[ "$line" == HW1_TERMINAL:* ]]; then
            printf '%s\n' "${line#HW1_TERMINAL:}"
            continue
        fi
        # tee has already captured commands; do not echo them to the terminal.
        if [[ "$line" == '$ '* ]]; then
            continue
        fi

        if [[ "$heading_lines" -gt 0 ]]; then
            printf '%s\n' "$line"
            heading_lines=$((heading_lines - 1))
            continue
        fi

        if [[ "$line" == '========================================' ]]; then
            printf '\n%s\n' "$line"
            heading_lines=2
            continue
        fi

        if [[ "$line" == '# Area' || "$line" == '# Delay' || "$line" == '# Between' ]]; then
            printf '\n%s\n' "$line"
            continue
        fi

        # DC warnings end at their diagnostic code, including unindented text.
        if [[ "$line" == Warning:* || "$line" == Error:* ]]; then
            warning_lines=1
        elif [[ -z "$line" || "$line" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T ||
                "$line" == Information:* || "$line" == INFO:* || "$line" == 'Using '* ]]; then
            warning_lines=0
        fi

        if [[ "$line" == *' [INFO] === '* ]]; then
            printf '\n'
        fi

        if [[ "$VERBOSE_OUTPUT" == 1 ||
              "$warning_lines" == 1 ||
              "$line" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T ||
              "$line" == INFO:* ||
              "$line" == usage:* ||
              "$line" == *': error: '* ||
              "$line" == HW1_DC_ERROR:* ||
              "$line" == Error:* ||
              "$line" == Warning:* ||
              "$line" == WARN:* ]]; then
            printf '%s\n' "$line"
        fi

        if [[ "$line" =~ \([A-Z]+-[0-9]+\)[[:space:]]*$ ]]; then
            warning_lines=0
        fi
    done
}

pipeline_runner() {
    worker "$@" 2>&1 | tee -a -- "$MASTER_LOG" | terminal_output
    local statuses=("${PIPESTATUS[@]}")

    if [[ ${statuses[1]} != 0 ]]; then
        printf 'ERROR: master log capture failed\n' >&2
        return 1
    fi

    if [[ ${statuses[2]} != 0 ]]; then
        printf 'ERROR: terminal output failed\n' >&2
        return 1
    fi

    return "${statuses[0]}"
}

pipeline_runner "$@" &
runner_pid=$!

wait "$runner_pid"
status=$?

if kill -0 "$runner_pid" 2>/dev/null; then
    wait "$runner_pid" || true
fi

exit "$status"
