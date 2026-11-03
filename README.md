# Accelerated PnR for the Alveo V80

Routes one block once and copies it instead of placing and routing the whole design, then compares the result against flat Vivado. Claude picks the block and PBlock, Vivado implements one block, and RapidWright copies and stitches it.

**Rough draft: the build flow has never been run.**

## Run

Set your paths at the top of `scripts/run.sh`, then:

```
scripts/run.sh --top systolic128 --period 2.0 --generic ROWS=64 --generic COLS=64 hdl/src/systolic128.v
```

Each run gets `results/runN/` with `report.md` and `final.dcp`. Logs go to `build/runN/`.

## Test

```
hdl/sim/run_xsim.sh 16 16 32 200 1    # ROWS COLS ACC_W NUM_TESTS SEED
```
