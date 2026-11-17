# Accelerated PAR for the Alveo V80

Routes one block once and copies it instead of placing and routing the whole design, then compares against flat Vivado. The open choice is how to cut the design into blocks and where the block's PBlock sits. Claude makes that choice once, from a description of the design and the device, and the flow then implements, copies and stitches the block.

A run passes if both flows route and the pre-implemented flow has the higher Fmax. Closing timing at the requested period is not required.

## Run

Set your paths at the top of `scripts/run.sh`, then:

```
scripts/run.sh --top systolic64 --period 1.8 --generic ROWS=64 --generic COLS=64 hdl/src/systolic64.v
```

| Option | Meaning |
|---|---|
| `--block "8x2 SLICE_X84Y96:SLICE_X103Y111"` | Skip Claude and use this block: 8x2 units in that slice rectangle, to reproduce a result |
| `--flat fresh`, `--flat results/runM`, `--flat none` | Run the flat baseline, reuse an earlier run's flat result (only fair for the same RTL, clock and Vivado), or skip it |

Each run gets `results/runN/` with `report.md` (the comparison, the block and Claude's reasoning), `final.dcp`, `flat.dcp` and `flat.json`. Logs and intermediate files are in `build/runN/`, including Claude's prompt and answer in `build/runN/claude/`.

## The flow

1. Vivado synthesizes the full design with its hierarchy kept, and `run.py` finds the repeated unit and its row/column grid from the instance names.
2. Claude reads `scripts/agent_instructions.md`, the unit's resource use and the device's clock regions and site columns, and answers with one block size and rectangle. It has no tools and is asked once.
3. RapidWright checks the rectangle against the device before any place and route: the DSP/BRAM/URAM sites inside it, use against that capacity, one clock region and SLR, at least 12 rows from the SLR edges, and a legal copy position for every block of the grid. A rectangle that fails stops the run.
4. RapidWright groups the units into identical blocks and writes one block plus a top with every block black-boxed.
5. Vivado places and routes one block in a hard PBlock (`IS_SOFT 0`, `CONTAIN_ROUTING` except CE/reset nets), with temporary port flops standing in for the neighbors.
6. ArrayBuilder stamps the routed block at each copy position, and RWRoute routes the stitching.
7. Vivado finishes the design: it reroutes the nets between blocks in different SLRs and any nets RWRoute left in conflict with `route_design -preserve -no_psir`, routes the VCC/GND pins that ArrayBuilder misses on Versal, and times the array.
8. Vivado runs the same design flat, as the baseline.

## Files

| File | What it does |
|---|---|
| `scripts/run.sh` | Entry point. Holds the machine paths, checks the tools, and starts `run.py`. |
| `scripts/run.py` | The run: synthesis, Claude's choice, the check, block, stitch and flat steps, and `report.md`. |
| `scripts/agent_instructions.md` | What Claude is told: the flow, the rules a rectangle must meet, what sets Fmax on the V80, and the answer format. |
| `scripts/flow.tcl` | The Vivado steps (synthesis, block run, final route, flat baseline), one command per Vivado process. |
| `scripts/rapidwright.py` | The Jython RapidWright steps: `group`, `harness`, `place`, `check` and `fabric` (the device description Claude reads). |
| `hdl/src/systolic64.v` | The test design: a parameterized systolic array of `systolic_pe` units, each mapping its multiply-accumulate to one DSP58. |
| `hdl/sim/systolic64_tb.sv`, `hdl/sim/run_xsim.sh` | Randomized testbench and its XSim runner. |

## Results

The design is fixed: `systolic64` with `ROWS=64 COLS=64`, 4096 PEs, each mapping its multiply-accumulate to one DSP58 (all 4096 DSPs on the V80). What varies is the block, the PBlock-sized piece of the array that is implemented once and copied: `RxC` means R x C PEs per block. Flat Vivado on the same RTL reaches 369.5 MHz in 58.5 minutes at a 2.0 ns target (run27), and 397.3 MHz in 56.8 minutes at 1.8 ns (run39). Every pre-implemented result below routes with 0 errors.

### Block size

Same home clock region (X2Y2) for every size, chosen by hand with what is now `--block`; only the block changes (run38, 2.0 ns target).

| Block | PEs per block | Copies | PBlock | Block alone (MHz) | Stitched Fmax (MHz) | WNS (ns) | vs flat | Compile (min) | of which ArrayBuilder / final route |
|---|---|---|---|---|---|---|---|---|---|
| 8x1 | 8 | 512 | `SLICE_X84Y96:SLICE_X103Y103` (20x8) | 716.3 | 470.6 | -0.125 | +27.4% | 47.3 | 3.6 / 18.9 |
| 8x2 | 16 | 256 | `SLICE_X84Y96:SLICE_X103Y111` (20x16) | 716.3 | **508.9** | **+0.035** | **+37.7%** | 54.3 | 3.1 / 26.4 |
| 8x4 | 32 | 128 | `SLICE_X84Y92:SLICE_X103Y139` (20x48) | 713.3 | 467.1 | -0.141 | +26.4% | 51.5 | 2.9 / 23.4 |

The 8x2 result meets 2.0 ns, so it is clipped (see the notes). The same three candidates at a 1.8 ns target (run39), against a fresh flat run at 1.8 ns that reaches 397.3 MHz in 56.8 minutes:

| Block | PEs per block | Copies | Block alone (MHz) | Stitched Fmax (MHz) | WNS (ns) | vs flat | Compile (min) | of which ArrayBuilder / final route | SLR-crossing nets |
|---|---|---|---|---|---|---|---|---|---|
| 8x1 | 8 | 512 | 699.3 | 495.8 | -0.217 | +24.8% | 48.5 | 3.3 / 21.2 | 577 |
| 8x2 | 16 | 256 | 716.3 | **505.8** | -0.177 | **+27.3%** | 53.0 | 3.1 / 25.8 | 1153 |
| 8x4 | 32 | 128 | 709.7 | 476.9 | -0.297 | +20.0% | 51.3 | 2.9 / 24.3 | 1153 |

The ranking is the same at both targets: 8x2 first, 8x4 last. Every failing path, for every size, is a direct flop-to-flop wire between copies in different SLRs. With 8x4 blocks those wires are slower: across the 1000 worst paths the crossing datapath has a median of 1.75 ns, against 1.66 ns for 8x1 and 8x2. 8x1 has half as many crossing nets as 8x2, because its 64 rows of copies fit across one SLR boundary instead of two, but its worst crossing (1.85 ns) is still slower than 8x2's (1.82 ns). So the worst hop sets Fmax, not the number of hops.

Only these three sizes fit a 64x64 array on the V80 (the check in step 3):
- **Fewer than 8 PEs per block** (1x1 to 4x1, 2x2): each copy needs its own DSP column pair, so at most 12 columns of copies fit, and those grids need 16 or more.
- **64 PEs per block** (8x8, 4x16, 16x4): enough DSPs only in a 40-slice-wide rectangle, which allows 6 columns of copies and 48 positions in all.
- **128 PEs per block** (8x16, 16x8): the rectangle contains the clock-region center row, so a copy can only go to the same spot in another region, 12 positions in all.

### Claude's choice

Claude's one-shot answer is the method under test. Runs 29 to 34 used an earlier, multi-step agent with tool access and budgets, since removed; its best pick was `8x4 SLICE_X84Y92:SLICE_X103Y139` at 465.8 MHz (2.0 ns, run34), below the hand-chosen 8x2 block above.

Notes:
- Compile time is the flow from synthesis to the end of the final route. Several runs overlapped on the shared machine, so times carry some noise.
- Fmax is computed from WNS at the target period. Vivado stops improving a route once it meets the target, so a result that meets it (8x2 at 2.0 ns) is a lower bound. Use 1.8 ns for comparisons.

What the runs show:
- **The SLR crossings decide the result.** Every block alone runs at 699 to 716 MHz, and the worst stitched paths are always flop-to-flop wires between copies in different SLRs. SLL tiles on the V80 have no register sites, and such a hop takes 1.75 to 1.97 ns of route depending on how Vivado routes it. A 64x64 grid needs two SLR boundaries. The 8x2 grid met 2.0 ns with crossings of 1.75 to 1.81 ns; the 8x1 and 8x4 grids each had a few crossings near 1.85 to 1.97 ns. Pinning port registers to the PBlock edge did not shorten them.
- **Orientation matters.** The longer side of the copy grid runs up the device, which puts the 9-bit-per-PE bus across the SLR boundaries. With the 43-bit bus across them instead, the same rectangle reached 437.4 MHz against 462.3 MHz (run32).
- **Compile time.** ArrayBuilder stamps and stitches 128 to 512 copies in about 3 minutes. Most of the pre-implemented flow is synthesis (16 min), the block run (8 min) and Vivado's final route (19 to 26 min), against 57 to 59 minutes for the flat place and route.

Fixed: **PBlocks on an SLR edge broke every copy.** Runs 29 and 33 lost every stitch with `[Route 35-19] Pin mapping failure, cannot reach driver pin`. Their home PBlocks sat on the bottom row of an SLR (Y0, the device edge, and Y332, the bottom of SLR1). Long vertical wires turn back at an SLR edge: Vivado routed the block through `OUT_SS12_BEG3`, which at row 7 of the bottom edge returns as `IN_NN12_END3` at row 4 inside the block. Everywhere else that wire runs 12 rows down, out of the block, so RapidWright could not relocate those nets and dropped them from every copy (22 nets for run33's 8x1 block, 177 for run29's), and RWRoute could not reroute them between the packed copies. The earlier guess that short blocks were the problem was a coincidence of where those blocks sat. The check now rejects a rectangle within 12 rows of its SLR's top or bottom, and `rapidwright.py place` compares every routed node of the block with the same wire at an interior copy position and fails with the affected nets if any differ. Copies themselves may sit on the edge rows.

Open issues:
- **Uncontained control nets.** In narrow PBlocks, a CE or reset net routed before containment sometimes detours outside the PBlock. `rapidwright.py harness` now unroutes up to 64 such nets so ArrayBuilder routes them in each copy.
- **Stitch time.** Clean 64x64 stitches spend 1.5 to 3.5 minutes in RWRoute. The edge-bug stitches took 12 to 73 minutes, retrying the dropped nets. One clean candidate in run34 (4x8 blocks, the 43-bit bus across the SLR boundaries) was still congested after 90 minutes. A stitch that spends more than 90 minutes in ArrayBuilder now fails instead of stalling.

Vivado writes an EDIF next to each checkpoint that RapidWright reads. Without it, RapidWright starts another Vivado to decrypt the netlist, which takes minutes at 64x64.

## Test

```
hdl/sim/run_xsim.sh 16 16 32 200 1    # ROWS COLS ACC_W NUM_TESTS SEED
```
