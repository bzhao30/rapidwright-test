# Accelerated PAR for the Alveo V80

Routes one block once and copies it instead of placing and routing the whole design, then compares against flat Vivado. Claude picks the block and PBlock, Vivado implements one block, and RapidWright copies and stitches it.

A run passes if both flows route and the pre-implemented flow has the higher Fmax. Closing timing at the requested period is not required.

## Run

Set your paths at the top of `scripts/run.sh`, then:

```
scripts/run.sh --top systolic64 --period 2.0 --generic ROWS=64 --generic COLS=64 hdl/src/systolic64.v
```

Each run gets `results/runN/` with `report.md`, `final.dcp` (pre-implemented) and `flat.dcp` (flat baseline). Logs go to `build/runN/`.

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
| `hdl/src/systolic64.v` | The test design: a parameterized systolic array of `systolic_pe` units, each mapping its multiply-accumulate to one DSP58. |
| `hdl/sim/systolic64_tb.sv` | Randomized testbench that checks the array's results. |
| `hdl/sim/run_xsim.sh` | Compiles and runs the testbench in XSim. |

## Results

`systolic64` 64x64 (run26) at a 2.0 ns target, one DSP58 per PE, 8x4-PE blocks (128 copies) in PBlock `SLICE_X84Y92:SLICE_X103Y139 DSP_X0Y46:DSP_X1Y69`. Neither flow closes timing at 2.0 ns.

| Metric | Flat Vivado | Pre-implemented | Pre-implemented vs flat |
|---|---|---|---|
| Fmax | 369.5 MHz | 467.1 MHz | +26.4% |
| Compile time | 57.4 min | 52.0 min | -9.4% |
| LUT / FF / DSP | 143296 / 302337 / 4096 | 143360 / 306433 / 4096 | |

The block alone reaches 713.3 MHz. Both flows route with 0 errors.

Vivado writes an EDIF next to each checkpoint that RapidWright reads. Without it, RapidWright starts another Vivado to decrypt the netlist, which takes minutes at 64x64.

## Test

```
hdl/sim/run_xsim.sh 16 16 32 200 1    # ROWS COLS ACC_W NUM_TESTS SEED
```
