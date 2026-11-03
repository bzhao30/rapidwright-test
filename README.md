# Accelerated place and route for the Alveo V80

Builds a design out of context by routing one block once and copying it, instead of placing and routing the whole design. It also runs plain Vivado on the same design and compares the two.

**Status: rough draft.** The build flow has never been run, so expect to fix things on the first run. The testbench passes on arrays up to 16x16, but has not finished a full 128x128 simulation.

## How it works

1. **Vivado** synthesizes the full design.
2. **Claude** picks the repeated unit, how many units go in each block, and the PBlock. It follows the fixed rules in `scripts/pblock_instructions.md`, and each block takes 1/4 of a clock region.
3. **RapidWright** finds the clock and groups the units into one shared block cell (`scripts/group_blocks.py`).
4. **Vivado** places and routes one block inside its hard-boundaried PBlock.
5. **RapidWright** (ArrayBuilder) places the copies and routes the connections between them.
6. **Vivado** routes the clock and checks routing and timing.
7. **Vivado** runs the same design flat, as the baseline.

The design needs repeated units that are direct children of the top module, arranged in a 2D grid, with a single clock. Otherwise only the flat result is produced.

## Setup

You need:

- Vivado 2024.2 with V80 support and a license
- A built copy of RapidWright (`build/libs/rapidwright.jar` and `jars/`)
- Java
- The `claude` CLI, logged in

Set your paths at the top of `scripts/run.sh`, or set the same variables in your shell.

## Run

From this directory:

```
scripts/run.sh --top systolic128 --period 2.0 --generic ROWS=64 --generic COLS=64 hdl/src/systolic128.v
```

| Argument | Meaning |
|---|---|
| `--top` | Top module |
| `--period` | Clock period in ns |
| `--generic NAME=VALUE` | Top-level parameter; repeat as needed |
| Remaining arguments | Source files (`.v`, `.sv`, `.vhd`) |

The default 128x128 will almost certainly not fit, because it needs 16,384 multipliers and the V80 has 10,848 DSPs. Start with 64x64.

A run can take hours on a large design. Each new Claude selection is limited to $5, and identical inputs reuse a cached answer.

## Output

| Path | Contents |
|---|---|
| `results/<run>_comparison.md` | Compile time, timing and resources for both flows |
| `results/checkpoints/<run>_final.dcp` | The placed and routed design |
| `build/<run>/` | Reports and logs, plus intermediate DCPs if the flow failed |

## Testbench

```
hdl/sim/run_xsim.sh [ROWS] [COLS] [ACC_W] [NUM_TESTS] [SEED]
hdl/sim/run_xsim.sh 16 16 32 200 1
```

It prints `*** TEST PASSED ***` or `*** TEST FAILED ***`. With no arguments it runs 128x128, which takes more than 36 minutes and prints nothing until the end.
