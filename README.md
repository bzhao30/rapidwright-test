# Accelerated PAR for the Alveo V80

Routes one block once and copies it instead of placing and routing the whole design, then compares against flat Vivado. Claude picks the block and PBlock, Vivado implements one block, and RapidWright copies and stitches it.

A run passes if both flows route and the pre-implemented flow has the higher Fmax. Closing timing at the requested period is not required.

## Run

Set your paths at the top of `scripts/run.sh`, then:

```
scripts/run.sh --top systolic64 --period 2.0 --generic ROWS=64 --generic COLS=64 hdl/src/systolic64.v
```

Each run gets `results/runN/` with `report.md` and `final.dcp`. Logs go to `build/runN/`.

## How it works

1. Vivado synthesizes the full design, and Claude picks the block size and PBlock.
2. RapidWright groups the PEs into identical blocks and writes one block plus a top with every block black-boxed.
3. Vivado places and routes one block in a hard PBlock (`IS_SOFT 0`, `CONTAIN_ROUTING` except CE/reset nets).
4. ArrayBuilder stamps the routed block at each anchor (whole PBlock heights apart), and RWRoute routes the stitching.
5. Vivado finishes the design:
   - It unroutes the nets between blocks in different SLRs, which RWRoute routes slowly, along with any nets RWRoute left in conflict.
   - It routes those nets with `route_design -preserve -no_psir`, keeping every other route and cell in place.
   - It routes the VCC/GND pins that ArrayBuilder misses on Versal.

## Files

| File | What it does |
|---|---|
| `scripts/run.sh` | Entry point. Holds the machine paths, checks the tools, and starts `runtest.tcl` in Vivado. |
| `scripts/runtest.tcl` | The whole flow: synthesis, the Claude call, the block run, stitching, final routing, the flat baseline and `report.md`. |
| `scripts/pblock_instructions.md` | The rules Claude follows to choose the repeated unit, the block size and the PBlock rectangle. |
| `scripts/rapidwright.py` | The Jython RapidWright steps: `group` (builds the block and black-box top), `harness` (adds and removes port flops around the block run) and `place` (picks aligned anchors for ArrayBuilder). |
| `hdl/src/systolic64.v` | The test design: a parameterized systolic array of `systolic_pe` units. |
| `hdl/sim/systolic64_tb.sv` | Randomized testbench that checks the array's results. |
| `hdl/sim/run_xsim.sh` | Compiles and runs the testbench in XSim. |

## Results

`systolic64` at a 2.0 ns target with 8x4-PE blocks. Neither flow closes timing at 2.0 ns.

| Array | Flat Fmax | Pre-implemented Fmax | Flat time | Pre-implemented time |
|---|---|---|---|---|
| 8x8 (run20) | 456.8 MHz | 490.9 MHz (+7.5%) | 8.0 min | 14.3 min |
| 64x64 (run21) | 408.5 MHz | 456.8 MHz (+11.8%) | 103.6 min | 71.1 min (-31%) |

The machine was shared and loaded during run21. An earlier 64x64 run took 79.5 min flat. run21 predates a fix to the grouping step that saves about 7 more minutes. At 8x8 the fixed steps dominate, so the pre-implemented flow is slower there.

Vivado writes an EDIF next to each checkpoint that RapidWright reads. Without it, RapidWright starts another Vivado to decrypt the netlist, which takes minutes at 64x64.

## Test

```
hdl/sim/run_xsim.sh 16 16 32 200 1    # ROWS COLS ACC_W NUM_TESTS SEED
```
