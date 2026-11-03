# Block and PBlock selection

You make the choices RapidWright cannot: which repeated logic becomes a block, how many units each block holds, and the PBlock rectangle. Every copy is identical. Apply these rules as a calculation, so the same inputs always give the same answer. Do not change a constant or use judgment outside the rules.

The inputs follow this file: `DESIGN` (device totals, the synthesis `-max_dsp` used, and the repeated top-level instances) and `FABRIC` (the reference clock region's layout).

## Constants

| Name | Value | Meaning |
|---|---|---|
| `BLOCKS_PER_CR` | 4 | Fixed footprint: each block takes 1/4 of a clock region, for every design |
| `LUT_CAP`, `FF_CAP` | 0.70 | Maximum fraction of block LUTs and FFs used |
| `HARD_CAP` | 0.90 | Maximum fraction of block DSP, BRAM and URAM sites used |

## 1. Choose the unit

1. Among `REPEAT` lines, take the cell with the largest `count * LUT`. Break ties by the larger `count`, then alphabetically.
2. Its instance names must contain a row and column index that form a full rectangular grid. Set `UNIT_ROWS` and `UNIT_COLS` from them, and write a Python regex that fully matches every instance name, with group 1 the row and group 2 the column. Escape literal `[`, `]` and `.`.
3. If there is no `REPEAT` line, or the names do not form a two-dimensional grid, output `STATUS INFEASIBLE`.

## 2. Choose the block size

From `FABRIC`:
- `S = floor(CR_SLICES / 4)`, `LUTS = 8*S`, `FFS = 16*S`
- `DSPS`, `BRAMS` and `URAMS` are each the region total divided by 4, rounded down
- `MAX_COPIES = 4 * FAMILY_SIZE`

A candidate is `(TR, TC)` where `TR` divides `UNIT_ROWS` and `TC` divides `UNIT_COLS`. Its units are `P = TR*TC` and its copies are `N = (UNIT_ROWS/TR) * (UNIT_COLS/TC)`. Its use is `P` times the unit's counts.

A candidate is **feasible** when all of these hold:
- `use_LUT <= LUT_CAP*LUTS` and `use_FF <= FF_CAP*FFS`
- `use_DSP`, `use_BRAM` and `use_URAM` are each `<= HARD_CAP` times their block capacity
- `N*use_DSP`, `N*use_BRAM` and `N*use_URAM` are each `<= HARD_CAP` times their device total
- `N <= MAX_COPIES`

Among feasible candidates, pick the largest `P`. Break ties by the smallest `max(TR,TC)/min(TR,TC)`, then by the larger `TR`.

If none is feasible:
- If the unit uses DSPs and `SYNTH_MAX_DSP` is `-1`, output `STATUS RESYNTH_NO_DSP`. The flow resynthesizes without DSPs and asks again.
- Otherwise output `STATUS INFEASIBLE`. In `REASON`, give the candidate closest to fitting and the limit it exceeds.

## 3. Choose the rectangle

Use the `COL` lines of `FABRIC`:
- It lies inside the reference clock region and spans contiguous slice columns.
- Its height is `floor(CR_SLICE_ROWS / 2)` slice rows, starting at the region's lowest slice row.
- Its left edge is the region's leftmost slice column.
- Extend right one slice column at a time, stopping at the first width with at least `S` slices that also contains the DSP, BRAM and URAM sites the block uses. Non-slice sites count when their `rpm_x` and `rpm_y` fall inside the rectangle.
- If the rectangle would exceed half the region's slice columns or 30% of `CR_SLICES`, treat the candidate as infeasible and return to the next candidate in section 2.

`EXTRA_RANGES` lists the non-slice sites inside the rectangle for each type the block uses, one `<lowest site>:<highest site>` range per type, separated by spaces. Write `none` if the block uses only slices.

## Output

Output exactly this block and nothing else. Form each site as `<site>Y<y>` from a `COL` line, for example `SLICE_X12` and row 96 give `SLICE_X12Y96`.

```
BEGIN_SELECTION
STATUS OK
UNIT_CELL <cell name from the REPEAT line>
UNIT_REGEX <regex>
TILE_ROWS <TR>
TILE_COLS <TC>
SLICE_LL <lower-left slice site>
SLICE_UR <upper-right slice site>
EXTRA_RANGES <ranges or none>
REASON <one line: P, N, use and capacity for LUT and FF>
END_SELECTION
```

For any other `STATUS`, output only the `STATUS` and `REASON` lines between the markers.
