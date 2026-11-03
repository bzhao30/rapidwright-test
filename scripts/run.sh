#!/bin/bash
# Runs the pre-implemented flow and the flat Vivado baseline, then writes results/.
# Usage: scripts/run.sh --top <module> --period <ns> [--generic NAME=VALUE]... <source files>
#   Example: scripts/run.sh --top systolic128 --period 2.0 \
#                --generic ROWS=64 --generic COLS=64 hdl/src/systolic128.v
#
# ---- Edit these for your machine, or set them in the environment. ------------------
VIVADO_SETTINGS="${VIVADO_SETTINGS:-/opt/xilinx/Vivado/2024.2/settings64.sh}"
RAPIDWRIGHT_PATH="${RAPIDWRIGHT_PATH:-/work/shared/users/meng/bjz24/RapidWright}"
CLAUDE_BIN="${CLAUDE_BIN:-claude}"
JAVA_BIN="${JAVA_BIN:-java}"
JAVA_HEAP="${JAVA_HEAP:-128g}"
# License server, if your shell doesn't already set one (for example 2100@host).
XILINXD_LICENSE_FILE="${XILINXD_LICENSE_FILE:-}"
# -------------------------------------------------------------------------------------

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

fail() { echo "run.sh: $1" >&2; exit 1; }
[ -f "$VIVADO_SETTINGS" ] || fail "VIVADO_SETTINGS not found: $VIVADO_SETTINGS"
[ -f "$RAPIDWRIGHT_PATH/build/libs/rapidwright.jar" ] || fail "no rapidwright.jar under RAPIDWRIGHT_PATH: $RAPIDWRIGHT_PATH"
[ -d "$RAPIDWRIGHT_PATH/jars" ] || fail "no jars directory under RAPIDWRIGHT_PATH: $RAPIDWRIGHT_PATH"
command -v "$CLAUDE_BIN" > /dev/null || fail "CLAUDE_BIN not found: $CLAUDE_BIN"
command -v "$JAVA_BIN" > /dev/null || fail "JAVA_BIN not found: $JAVA_BIN"

set +u  # Vivado settings scripts read unset variables.
source "$VIVADO_SETTINGS"
set -u

export RAPIDWRIGHT_PATH CLAUDE_BIN JAVA_BIN JAVA_HEAP
if [ -n "$XILINXD_LICENSE_FILE" ]; then export XILINXD_LICENSE_FILE; fi

mkdir -p "$ROOT/build"
exec vivado -mode batch -nojournal -log "$ROOT/build/vivado_$(date +%Y%m%d_%H%M%S).log" \
    -source "$ROOT/scripts/runtest.tcl" -tclargs "$@"
