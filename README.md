# Accelerated PAR for the Alveo V80

Compiles a design built from one repeated unit, such as a systolic array, in minutes instead of an hour. Instead of placing and routing the whole design, it places and routes one block of units once, copies it across the device with RapidWright, and lets RWRoute route the wires between copies. The full design never goes back into Vivado. Claude chooses how big the block is and where its PBlock sits.

The target is about 10 minutes from RTL to a routed checkpoint, and never more than 45. Speed comes first: Fmax is measured and reported against flat Vivado, but beating it is not required.

## Run

Set your machine's paths at the top of `scripts/run.py` (or in the environment), then:

```
python3 scripts/run.py --top systolic64 --period 1.8 --generic ROWS=64 --generic COLS=64 hdl/src/systolic64.v
```

| Option | Meaning |
|---|---|
| `--block "8x2 SLICE_X84Y96:SLICE_X103Y111"` | Skip Claude and use this block: 8x2 units in that slice rectangle |
| `--unit NAME` | The repeated module, if the top instantiates more than one |
| `--flat fresh`, `--flat results/runM`, `--flat none` | Run the flat Vivado baseline, reuse an earlier run's (only fair for the same RTL, clock and Vivado), or skip it |

Each run writes `results/runN/` with `report.md` (compile time by step, Fmax against flat, the block and Claude's answer), `final.dcp` (the routed design) and `flat.json`. Logs and intermediate files are in `build/runN/`, with Claude's prompts and answers in `build/runN/claude/`. The exit code is 0 if the result is fully routed within 45 minutes.

## The flow

Timed from RTL to the routed checkpoint:

| Step | What happens | Minutes (runs 46-49) |
|---|---|---|
| 1. Synthesis | Vivado synthesizes the repeated unit alone, and the top with the unit as a black box, in parallel. Only EDIF is written. The top's instance names give the unit grid, and its nets give the bus widths between neighboring units. | 2.6-4.0 |
| 2. Choice | Claude reads `scripts/agent_instructions.md` with the unit's resources, the bus widths and the device's clock regions and site columns, and ranks up to 3 candidates. RapidWright checks them against the device in seconds; the first that passes is used, moved by a few slices if its copy grid only fits there. If none passes, Claude is asked again with the problems, up to 3 times. Meanwhile Vivado starts for step 4 and loads the device. | 1.6-2.2 |
| 3. Group | RapidWright fills the black boxes with the unit's netlist and groups the units into identical blocks: one block netlist, and the top with every block as a black box. | 0.4-0.5 |
| 4. Block | Vivado places and routes one block in a hard PBlock (`CONTAIN_ROUTING` on, except CE/reset nets), with temporary port flops standing in for the neighbors. | 3.5-4.3 |
| 5. Array | RapidWright removes the port flops and picks the copy positions; ArrayBuilder stamps the block at each and RWRoute routes everything between copies, including the SLR crossings. | 2.9-3.6 |

Measured afterwards, outside the compile time: Vivado opens the result only to time it and check its routing (about 10 minutes at 64x64), and runs the same RTL flat as the baseline (about an hour).

## Files

| File | What it does |
|---|---|
| `scripts/run.py` | The run: machine paths, the steps above, the flat baseline and `report.md`. |
| `scripts/agent_instructions.md` | What Claude is told: how the flow uses its answer, the rules a rectangle must meet, and what sets compile time and Fmax on the V80. |
| `scripts/flow.tcl` | The Vivado steps: `synth_unit`, `synth_top`, `impl` (the block), `timing` and `flat`, one per Vivado process. |
| `scripts/rapidwright.py` | The Jython RapidWright steps: `fabric` (the device description), `check`, `group`, `harness` and `array`. |
| `hdl/src/systolic64.v` | The test design: a parameterized systolic array of `systolic_pe` units, each mapping its multiply-accumulate to one DSP58. |
| `hdl/sim/systolic64_tb.sv`, `hdl/sim/run_xsim.sh` | Randomized testbench and its XSim runner. |

## Results

`systolic64` with `ROWS=64 COLS=64`: 4096 PEs, each one DSP58 (every DSP on the V80), at a 1.8 ns target. Flat Vivado reaches 397.3 MHz in 56.8 minutes (run39). `RxC` is a block of R x C PEs.

| Run | Block (chosen by Claude) | Compile (min) | Fmax (MHz) | vs flat | Route errors |
|---|---|---|---|---|---|
| run49 | 8x2 `SLICE_X92Y108:SLICE_X99Y123` | 13.5 | 392.8 | -1.1% | 0 |
| run48 | 8x2 `SLICE_X91Y100:SLICE_X100Y115` | 14.8 | 400.6 | +0.8% | 0 |
| run47 | 8x2 `SLICE_X90Y94:SLICE_X101Y109`, moved up 2 rows | 12.5 | 384.9 | -3.1% | 0 |
| run46 | 8x2 `SLICE_X92Y108:SLICE_X99Y123` | 12.3 | 392.8 | -1.1% | 0 |
| run45 | 8x4 `SLICE_X92Y92:SLICE_X99Y123`, moved down 8 rows | 12.5 | 404.9 | +1.9% | 48 |
| run43 | 16x2 `SLICE_X94Y96:SLICE_X117Y111` | 14.2 | 384.0 | -3.3% | 0 |
| run42 | 8x2 `SLICE_X92Y92:SLICE_X99Y107` | 13.3 | 399.8 | +0.6% | 3330 |
| run41 | 2x8 `SLICE_X170Y444:SLICE_X181Y459` | stopped | | | |

Run41 was stopped after 17 minutes of RWRoute congestion, run42's errors were the top's ports, and run45's were SLR crossings across 32-row gaps between copies; all three causes are fixed or rejected by the check now (below). Run48's synthesis took 5.9 minutes because the block's Vivado started alongside it; it now starts after synthesis. The machine is shared (load average 16 to 24), so the same step varies by a minute between runs.

- **Compile time is 3.8 to 4.6x shorter than flat.** Synthesis went from 16-20 minutes (the whole design with its hierarchy kept) to 2.6 to 4, by synthesizing one unit and the wiring between units separately. Dropping Vivado's final route of the full design saved another 25 to 35 minutes: loading the 64x64 design into Vivado alone takes almost 5.
- **Fmax is about flat's.** Every failing path is a flop-to-flop wire between copies in different SLRs. RWRoute does not model timing on Versal and routes such a hop in 2.0 to 2.25 ns, where Vivado's timing-driven reroute took 1.75 to 1.82 ns. With that reroute, the same 8x2 block reached 505.8 MHz, but the flow took 53 minutes (run39).
- **The block's orientation decides whether the array routes in minutes.** The `a` bus between columns is 9 bits per PE; the `b`, `res` and `drain` buses between rows are 43. The copy grid's longer side runs up the device, so a block with fewer rows than columns sends the 43-bit buses across each SLR boundary (64 x 43 = 2752 wires), and RWRoute was still congested after 17 minutes (run41, 2x8 blocks; stopped). With the 9-bit bus across (576 wires), it routes in 1 to 1.5 minutes. Claude now gets the bus widths and picks the right orientation.
- **Claude's choice.** At medium effort an answer takes 1 to 2 minutes and about $0.20, and since it has the bus widths it has put the narrow bus across the SLR boundaries every time (8x2, 16x2 and 8x4 blocks). Whether copies fit depends on the exact tiles under a rectangle, which Claude cannot see, so candidates a few rows off used to cost a retry of 2 to 5 minutes (run43); the flow now moves them to the nearest legal position in seconds instead (run45, run47).
- **Where the remaining time goes.** About 10 of the 12 to 14 minutes are fixed tool costs: Vivado elaborating 4096 instances (about 2 minutes), placing one block on a device this large (about 1.8), loading the device (about 40 s per process), and RWRoute (1 to 1.5). Reaching 10 minutes would take overlapping Claude with synthesis, which needs the unit grid and bus widths before the top is synthesized.

The block-size sweep with the earlier flow, which synthesized the whole design and had Vivado reroute the SLR crossings (about 50 minutes per compile, 1.8 ns, run39): 8x1 blocks 495.8 MHz, 8x2 505.8 MHz, 8x4 476.9 MHz. Only 8, 16 and 32 PEs per block fit a 64x64 array on the V80: smaller blocks need more columns of copies than there are DSP column pairs (12), 64-PE blocks need 40-slice-wide rectangles with room for only 6 columns, and 128-PE blocks contain the clock-region center row, which leaves 12 positions.

Fixed along the way:
- **PBlocks on an SLR edge broke every copy.** Long vertical wires turn back at an SLR or device edge, so a block routed within 12 rows of one used wires no copy position has, and RapidWright dropped those nets from every copy. The check rejects such rectangles, and the array step compares every routed node of the block with the same wire at an interior copy position before copying.
- **Top ports.** A top built from EDIF is not marked out of context, so Vivado expected its 3330 ports to reach I/O pads (run42). The group step now marks it.
- **Gaps between copies.** A 32-row block fits once per 48-row half region, so its copies sit 32 rows apart, and RWRoute (run by ArrayBuilder with a fixed bounding box) abandoned 80 SLR crossings across those gaps (run45). The check now rejects copy grids with gaps over 16 slices, and the report counts the connections RWRoute abandoned.
- **Grouping time.** Finding which side of the block each port faces re-scanned every net for each block; it now reuses the net owners computed just before, which cut grouping from 75 to 30 seconds.

Open issues:
- **Fmax below the old flow's.** RWRoute is not timing-driven on Versal. Pipelining the buses that cross SLRs in the RTL, or rerouting only the SLR-crossing nets with a timing-aware router, would recover most of the 25% the old flow had.
- **Uncontained control nets.** In narrow PBlocks a CE or reset net routed before containment sometimes leaves the PBlock; up to 64 such nets are unrouted so RWRoute routes them in each copy.

## Test

```
hdl/sim/run_xsim.sh 16 16 32 200 1    # ROWS COLS ACC_W NUM_TESTS SEED
```
