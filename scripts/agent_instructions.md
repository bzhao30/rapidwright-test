# Choosing the block and PBlock for a pre-implemented array

A large array design is cut into identical blocks. The flow places and routes one block inside a PBlock, stamps copies of it across the device, and routes only the wires between copies. You choose the block size and the PBlock rectangle from the `DESIGN` and `FABRIC` sections below. The array must route, and quickly: compile time comes first, then the highest Fmax of the full array. Closing timing at the requested period is not required.

## The flow after your answer

1. A block is `rows x cols` units of the unit grid in `DESIGN`. The copies form a grid of `(unit rows / rows) x (unit cols / cols)` blocks, and the longer side of that grid runs up the device. So the block's shape decides which bus runs up the device and crosses the SLR boundaries:
   - more grid rows than grid columns (block rows < block cols, for a square unit grid): array rows run up the device, and every SLR boundary is crossed by `unit cols x wires to the next row`;
   - more grid columns than grid rows: the grid is turned, array columns run up the device, and every SLR boundary is crossed by `unit rows x wires to the next column`;
   - a square grid is turned so that the narrower bus runs up the device.
2. Vivado places and routes one block inside your rectangle, with `CONTAIN_ROUTING` on, so its wires never leave it.
3. RapidWright copies the routed block to an aligned grid of positions: copies in a grid column share one X, copies in a grid row share one Y, and neighbors in the array are neighbors on the device.
4. RWRoute routes every wire between copies, including the ones that cross SLRs. It does not know timing on this device, so it takes a short path for each wire but does not weigh one against another. Vivado then only times the result.

You give up to 3 candidates, best first. The flow checks them against the rules below in seconds and uses the first that passes. Whether copies fit depends on the exact tiles under the rectangle, which this description doesn't show, so if none passes as given, the flow moves each by up to 4 slice columns and 8 rows to the nearest position that passes. If that fails too, you are told why and asked again, at most 3 times in all. Each answer costs time, so check every candidate against every rule first.

## Rules a rectangle must meet

- It lies inside one clock region and one SLR, and every site in it is a slice: no holes, no device edge.
- It is at least 12 slice rows from the top and bottom of its SLR (the device edge, or an SLR boundary such as Y331/Y332). Long vertical wires turn back at those edges, so a block routed there uses wires no copy position has. Copies may still sit on the edge rows.
- A copy can sit only where the tile pattern under its whole rectangle matches the original's. A clock region is 96 slice rows tall with a clock spine (RCLK row) across its middle: a rectangle that contains the RCLK row can only be copied to the same rows in other regions, while one inside a half region can be copied to either half of any region with the same columns. Each copy stays inside one clock region.
- DSP, BRAM and URAM sites between its slice columns come with it. They must hold the block's use, and its LUT and FF use must stay under 90% of 8 LUTs and 16 flops per slice.
- DSP58 sites come in paired columns, 48 per column per clock region, one per two slice rows. Copies in different grid columns need different column pairs, so the number of DSP column pairs across the device bounds the number of grid columns when the unit uses DSPs. Regions with different hard-site counts have different columns, so a rectangle copies only to regions that match it.
- The whole copy grid must fit: enough legal columns for the grid's columns, each with enough legal rows for the grid's rows, without overlapping.
- Neighboring copies may be at most 16 slices apart. A block whose height or width does not divide the space between the places it can be copied to leaves empty slices between copies, and RWRoute cannot route SLR crossings across a wider gap. For example, a 32-row block fits once in each 48-row half region, which leaves 32-row gaps.

## What sets compile time and Fmax

The whole compile has a budget of about 10 minutes, and RWRoute is the part your choice moves most. It routes a 64x64 array in 1 to 3 minutes when the wires between copies have room, but congestion can stretch it past 15 minutes.

- A wire between SLRs uses an SLL, and the SLL tiles on this device have no register sites. A flop-to-flop crossing takes about 2.0 to 2.2 ns as RWRoute routes it, and the slowest crossing usually sets the array's Fmax. SLLs are scarce: thousands of wires across one boundary congest it, so pick the orientation that puts the narrower bus across the SLR boundaries.
- Every wire between two copies leaves one block edge and enters the next. A narrow edge carrying a wide bus congests the routing around it.
- Wires between copies in one SLR are short if neighbors meet edge to edge. Tall, thin blocks stacked up the device keep vertical neighbors close; gaps between grid columns lengthen horizontal wires.
- Inside a block, a tight or over-full rectangle hurts the block's own timing. Leave the placer some room, but a much bigger rectangle spreads the block and lengthens its own wires.
- Bigger blocks mean fewer copies and fewer wires between them, but a bigger rectangle, which may contain the RCLK row or not repeat enough times.

## Answer

Reason it through, then end your answer with one to three lines in this form, best first, each with the rectangle's lower-left and upper-right slices:

```
SELECTION <rows>x<cols> SLICE_X<a>Y<b>:SLICE_X<c>Y<d>
```
