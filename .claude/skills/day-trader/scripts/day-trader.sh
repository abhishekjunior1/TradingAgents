#!/usr/bin/env bash
# The /day-trader skill's runner: one headless TradingAgents intraday (day-trader)
# run for an NSE or BSE stock, then the saved report's header, final decision and
# day-trader report. Research and paper trading only; nothing here places an order.
#
# Usage: day-trader.sh TICKER [nse|bse]    (the exchange defaults to nse)
#
# TradingAgents runs as "$TRADINGAGENTS_PYTHON -m cli.main" when that is set, else
# as the tradingagents command on PATH, else with the repo's .venv/bin/python or
# python3 as "python -m cli.main", always from the repo root: the CLI reads .env
# from its working directory. A Python it runs with must be 3.11 or later.

set -u
unset CDPATH   # a relative cd must not land elsewhere, or print where it went

usage="usage: day-trader.sh TICKER [nse|bse], e.g. day-trader.sh RELIANCE nse"
max_bytes=10000   # a report file is printed up to this size; the folder keeps all of it
tail_lines=40     # lines of the CLI's output shown when a run fails

die() {  # message [status]: stop before anything runs
    printf 'day-trader: %s\n' "$1" >&2
    exit "${2:-2}"
}

case ${1:-} in
    -h | --help) printf '%s\n' "$usage"; exit 0 ;;
esac
{ [ $# -ge 1 ] && [ $# -le 2 ]; } || die "$usage"

ticker=$1
exchange=${2:-nse}

# A ticker as Yahoo writes it: letters, digits and & . - ^ = (M&M, BAJAJ-AUTO.NS,
# ^NSEI), at most 20, starting with a letter, digit or ^ so it cannot read as an
# option. The letters are listed because in some locales a range such as A-Z
# matches other characters too.
alnum=ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789
ticker_re="^[$alnum^][$alnum&.^=-]{0,19}\$"
[[ $ticker =~ $ticker_re ]] || die "the ticker must be 1 to 20 letters, digits or & . - ^ =, starting with a letter, digit or ^ (e.g. RELIANCE, M&M, BAJAJ-AUTO.NS)"

case $exchange in
    [Nn][Ss][Ee]) exchange=nse ;;
    [Bb][Ss][Ee]) exchange=bse ;;
    *) die "the exchange must be nse or bse" ;;
esac

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P) || die "cannot find this script's folder" 1
repo_root=$(cd -- "$script_dir/../../../.." && pwd -P) || die "cannot find the repo root" 1
venv_hint="for example $repo_root/.venv/bin/python"

py=
if [ -n "${TRADINGAGENTS_PYTHON:-}" ]; then
    py=$TRADINGAGENTS_PYTHON
    # A relative path names a file here, not in the repo root the CLI runs from.
    case $py in
        /*) ;;
        */*) py=$PWD/${py#./} ;;
    esac
    command -v -- "$py" > /dev/null 2>&1 \
        || die "TRADINGAGENTS_PYTHON does not name a program that can run; set it to the Python of the environment TradingAgents is installed in ($venv_hint)" 1
elif command -v tradingagents > /dev/null 2>&1; then
    run=(tradingagents)
else
    if [ -x "$repo_root/.venv/bin/python" ]; then
        py=$repo_root/.venv/bin/python
    elif command -v python3 > /dev/null 2>&1; then
        py=python3
    else
        die "found no way to run TradingAgents: set TRADINGAGENTS_PYTHON to the Python of its environment ($venv_hint) or put the tradingagents command on PATH" 1
    fi
fi
if [ -n "$py" ]; then
    # Whichever Python was chosen may be too old (macOS's python3 is 3.9, and TRADINGAGENTS_PYTHON
    # may name it), and the run would end in an ImportError. The repo's .venv is not suggested
    # when it is the one refused, by whatever name or path (python3, a linked folder): the
    # folders are compared once resolved.
    hint=" ($venv_hint)"
    venv_dir=$(cd -P -- "$repo_root/.venv/bin" 2> /dev/null && pwd -P)
    if [ -n "$venv_dir" ] && py_path=$(command -v -- "$py") \
            && [ "$(cd -P -- "$(dirname -- "$py_path")" 2> /dev/null && pwd -P)" = "$venv_dir" ]; then
        hint=
    fi
    "$py" -c 'import sys; sys.exit(sys.version_info < (3, 11))' < /dev/null > /dev/null 2>&1 \
        || die "$py is not Python 3.11 or later, which TradingAgents needs; set TRADINGAGENTS_PYTHON to the Python of its environment$hint" 1
    run=("$py" -m cli.main)
fi

cd -- "$repo_root" || die "cannot enter $repo_root" 1
tmp=${TMPDIR:-/tmp}
log=$(mktemp "${tmp%/}/day-trader.XXXXXX") || die "cannot create a log file in $tmp" 1

printf 'Running TradingAgents (%s) for %s on %s, intraday; this takes several minutes.\n' \
    "${run[*]}" "$ticker" "$exchange"

# The CLI writes to the log as plain, unwrapped text, since the saved folder is read
# back from it: COLUMNS keeps rich from wrapping a long path, the unset variables
# would make rich and typer draw for a terminal, and typer's standard traceback
# reads well in the tail and never lists local variables.
(
    unset FORCE_COLOR PY_COLORS TTY_COMPATIBLE
    export TRADINGAGENTS_NO_UPDATE_CHECK=1 COLUMNS=1000 TYPER_STANDARD_TRACEBACK=1
    exec "${run[@]}" --horizon intraday --exchange "$exchange" --ticker "$ticker" \
        --analysts day_trader,market,news --save --no-show --html
) < /dev/null > "$log" 2>&1
status=$?

esc=$'\033'
strip_controls() {  # terminal escape sequences and control characters other than tab and newline
    LC_ALL=C sed -e "s|$esc\\[[0-9;?]*[ -/]*[@-~]||g" | LC_ALL=C tr -d '\000-\010\013-\037\177'
}

fail() {  # message: the run failed; show the end of its output and keep the log
    {
        printf 'day-trader: %s\n' "$1"
        printf 'The last %s lines of its output follow; all of it is in %s\n' "$tail_lines" "$log"
        # rich pads its panels to the console width: shorten long runs of spaces (keeping
        # a traceback's indentation) and cut long lines.
        tail -n "$tail_lines" "$log" | strip_controls | LC_ALL=C sed -E 's/ {5,}/    /g' | LC_ALL=C cut -c 1-300
    } >&2
    exit 1
}

[ "$status" -eq 0 ] || fail "TradingAgents exited with status $status."

# cli/run.py announces the folder as "✓ Report saved to: <absolute path>".
folder=$(strip_controls < "$log" | LC_ALL=C sed -n 's/^.*Report saved to:[[:space:]]*//p' \
    | tail -n 1 | LC_ALL=C sed 's/[[:space:]]*$//')
{ [ -n "$folder" ] && [ -f "$folder/complete_report.md" ]; } \
    || fail "TradingAgents finished, but its output names no saved report."

show() {  # heading, file in the report folder: printed up to max_bytes
    local file="$folder/$2" size
    printf '\n===== %s (%s) =====\n' "$1" "$2"
    if [ ! -s "$file" ]; then
        printf '(not in this report)\n'
        return
    fi
    head -c "$max_bytes" "$file" | strip_controls
    size=$(($(wc -c < "$file")))
    if [ "$size" -gt "$max_bytes" ]; then
        printf '\n[cut at %s of %s bytes; the whole file is %s]\n' "$max_bytes" "$size" "$file"
    elif [ -n "$(tail -c 1 "$file")" ]; then
        printf '\n'   # end the file's last line
    fi
}

printf '\nTradingAgents finished.\nReport folder: %s\n' "$folder"
printf 'Complete report: %s\n' "$folder/complete_report.md"
if [ -f "$folder/complete_report.html" ]; then
    printf 'HTML report: %s\n' "$folder/complete_report.html"
fi
# The title and the run's facts (analysis date, market, rating, models), up to the first section.
printf '\n===== Report header (complete_report.md) =====\n'
LC_ALL=C awk 'NR > 25 || /^## / { exit } NF { print }' "$folder/complete_report.md" | strip_controls
show "Final decision" 5_portfolio/decision.md
show "Day-trader report" 1_analysts/day_trader.md
rm -f -- "$log"
exit 0
