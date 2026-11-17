# Choosing the block and PBlock for a pre-implemented array

A large array design is cut into identical blocks. The flow places and routes one block inside a PBlock, stamps copies of it across the device, and routes only the wires between copies. You choose the block size and the PBlock rectangle, once, from the `DESIGN` and `FABRIC` sections below. Aim for the highest Fmax of the full stitched array. Closing timing at the requested period is not required, but the array must place and route.

## The flow after your answer

1. A block is `rows x cols` units of the unit grid in `DESIGN`. The copies form a grid of `(unit rows / rows) x (unit cols / cols)` blocks. The longer side of that grid runs up the device; a square grid puts the wider bus across the SLR boundaries the least.
2. Vivado places and routes one block inside your rectangle, with `CONTAIN_ROUTING` on, so its wires never leave it.
3. RapidWright copies the routed block to an aligned grid of positions: copies in a grid column share one X, copies in a grid row share one Y, and neighbors in the array are neighbors on the device. RWRoute routes the wires between copies.
4. Vivado reroutes the nets that cross SLRs with its timing-driven router and times the whole array.

If your rectangle fails the check below, the run fails without any place and route.

## Rules a rectangle must meet

- It lies inside one clock region and one SLR, and every site in it is a slice: no holes, no device edge.
- It is at least 12 slice rows from the top and bottom of its SLR (the device edge, or an SLR boundary such as Y331/Y332). Long vertical wires turn back at those edges, so a block routed there uses wires no copy position has. Copies may still sit on the edge rows.
- A copy can sit only where the tile pattern under its whole rectangle matches the original's. A clock region is 96 slice rows tall with a clock spine (RCLK row) across its middle: a rectangle that contains the RCLK row can only be copied to the same rows in other regions, while one inside a half region can be copied to either half of any region with the same columns. Each copy stays inside one clock region.
- DSP, BRAM and URAM sites between its slice columns come with it. They must hold the block's use, and its LUT and FF use must stay under 90% of 8 LUTs and 16 flops per slice.
- DSP58 sites come in paired columns, 48 per column per clock region, one per two slice rows. Copies in different grid columns need different column pairs, so the number of DSP column pairs across the device bounds the number of grid columns when the unit uses DSPs. Regions with different hard-site counts have different columns, so a rectangle copies only to regions that match it.
- The whole copy grid must fit: enough legal columns for the grid's columns, each with enough legal rows for the grid's rows, without overlapping.

## What sets Fmax

- A wire between SLRs uses an SLL, and the SLL tiles on this device have no register sites. A flop-to-flop crossing takes about 1.75 to 1.97 ns of route, and the slowest crossing usually sets the array's Fmax. Fewer crossing wires give the router more room on each one.
- Wires between copies in one SLR are short if neighbors meet edge to edge. Tall, thin blocks stacked up the device keep vertical neighbors close; gaps between grid columns lengthen horizontal wires.
- Inside a block, a tight or over-full rectangle hurts the block's own timing. Leave the placer some room, but a much bigger rectangle spreads the block and lengthens its own wires.
- Bigger blocks mean fewer copies and fewer wires between them, but a bigger rectangle, which may contain the RCLK row or not repeat enough times.

## Answer

Reason it through, then end your answer with exactly one line in this form, with the rectangle's lower-left and upper-right slices:

```
SELECTION <rows>x<cols> SLICE_X<a>Y<b>:SLICE_X<c>Y<d>
```
