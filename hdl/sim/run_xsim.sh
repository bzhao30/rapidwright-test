#!/bin/bash
# Usage: run_xsim.sh [ROWS] [COLS] [ACC_W] [NUM_TESTS] [SEED]
# Set DRAIN_SLACK to change the spare cycles before each drain (default 16).
# Set BUILD_DIR to change where build outputs go, and RTL to simulate another source file.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROWS="${1:-128}"; COLS="${2:-128}"; ACC_W="${3:-32}"; NUM_TESTS="${4:-200}"; SEED="${5:-1}"
RTL="${RTL:-$HERE/../src/systolic128.v}"
BUILD_DIR="${BUILD_DIR:-$HERE/build_${ROWS}x${COLS}}"
set +u  # Vivado settings scripts read unset variables.
source "${VIVADO_SETTINGS:-/opt/xilinx/Vivado/2024.2/settings64.sh}"
set -u
mkdir -p "$BUILD_DIR"
cd "$BUILD_DIR"
xvlog "$RTL"
xvlog -sv "$HERE/systolic128_tb.sv"
xelab -debug off -timescale 1ns/1ps systolic128_tb -s tb_snap \
    -generic_top "ROWS=$ROWS" -generic_top "COLS=$COLS" \
    -generic_top "ACC_W=$ACC_W" -generic_top "NUM_TESTS=$NUM_TESTS" -generic_top "DRAIN_SLACK=${DRAIN_SLACK:-16}"
xsim tb_snap -runall -sv_seed "$SEED" | tee sim.log
grep -q '\*\*\* TEST PASSED \*\*\*' sim.log
