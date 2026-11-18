# The RapidWright steps of the flow (run.py). Each step runs as its own Jython call:
#
#   rapidwright.py group <top.edf> <unit.edf> <block.dcp> <top_blackbox.dcp> <info.txt> <side map> <unit cell> <regex>
#                        <block rows> <block cols> <clock period>
#   rapidwright.py harness add <in.dcp> <out.dcp> <clock> <pblock ranges> <side map>
#                  (harness remove <in.dcp> <out.dcp> <clock> <pblock ranges> runs inside array)
#   rapidwright.py array <block_routed_flops.dcp> <block_routed.dcp> <clock> <pblock ranges> <top_blackbox.dcp>
#                        <side map> <placement.txt> <copy_pblocks.txt> <grid.json> <out.dcp> <clock period>
#   rapidwright.py check <candidates.json> <out.json>
#   rapidwright.py fabric <part> [clock region]

import json
import os
import re
import sys

from com.xilinx.rapidwright.design import ClockTools, Design, Module, NetTools
from com.xilinx.rapidwright.design.blocks import PBlock
from com.xilinx.rapidwright.design.tools import ArrayBuilder, ArrayNetlistGraph, InlineFlopTools
from com.xilinx.rapidwright.edif import EDIFCell, EDIFCellInst, EDIFDirection, EDIFTools
from com.xilinx.rapidwright.device import Node


# A checkpoint Vivado writes has an encrypted netlist, and RapidWright decrypts it by running
# Vivado again. flow.tcl writes the netlist next to its checkpoints, so this reads it from there.
def read_checkpoint(dcp):
    edf = dcp[:-len(".dcp")] + ".edf"
    if os.path.exists(edf):
        return Design.readCheckpoint(dcp, edf)
    return Design.readCheckpoint(dcp)


def die(msg):
    print("RAPIDWRIGHT FAILED: " + msg)
    sys.exit(1)


# Fills the empty unit instances of the top netlist (synthesized with the unit as a black box)
# with the unit's own netlist, so every unit references one synthesized cell.
def fill_units(top_edf, unit_edf, unit_cell):
    netlist = EDIFTools.readEdifFile(top_edf)
    unit = EDIFTools.readEdifFile(unit_edf).getTopCell()
    top = netlist.getTopCell()
    insts = [i for i in top.getCellInsts() if re.sub(r"__\d+$", "", i.getCellType().getName()) == unit_cell]
    if not insts:
        die("no black-box instances of %s in the top" % unit_cell)
    for name in set(i.getCellType().getName() for i in insts):
        netlist.getWorkLibrary().removeCell(name)
    netlist.migrateCellAndSubCells(unit)
    for inst in insts:
        inst.setCellType(unit)
        inst.removeProperty("black_box")
    netlist.resetParentNetMap()
    return netlist


# Groups repeated sibling instances into identical blocks that all reference one shared
# cell. RegroupInstances can't do this: it makes a separate cell per group, with port names
# and port sets that vary by position, and ArrayBuilder needs identical interfaces. The regex
# must capture a unit's row and column from its instance name. Writes one block and the top
# with every block as a black box.
def group(args):
    top_edf, unit_edf, block_dcp, top_dcp, info_path, side_map_path, unit_cell, regex = args[0:8]
    block_rows, block_cols, period = int(args[8]), int(args[9]), args[10]

    netlist = fill_units(top_edf, unit_edf, unit_cell)
    design = Design(netlist)
    # Synthesized out of context: the top's ports connect to other logic, not to I/O pads.
    design.setDesignOutOfContext(True)
    design.setAutoIOBuffers(False)
    top = netlist.getTopCell()

    clock_net = ClockTools.getClockFromDesign(design)
    if clock_net is None or "/" in clock_net.getHierarchicalNetName():
        die("no top-level clock found: %s" % clock_net)
    clock = clock_net.getHierarchicalNetName()


    def is_unit(inst):
        # Synthesis uniquifies each instance's cell (systolic_pe__1), so compare the RTL name.
        return re.sub(r"__\d+$", "", inst.getCellType().getName()) == unit_cell


    units = {}
    for inst in top.getCellInsts():
        if not is_unit(inst):
            continue
        match = re.match(regex + "$", inst.getName())
        if match is None:
            die("instance %s does not match %s" % (inst.getName(), regex))
        units[(int(match.group(1)), int(match.group(2)))] = inst

    if not units:
        die("no top-level instances of %s" % unit_cell)
    rows = max(r for r, c in units) + 1
    cols = max(c for r, c in units) + 1
    if len(units) != rows * cols:
        die("%d units do not fill a %dx%d grid" % (len(units), rows, cols))
    if rows % block_rows or cols % block_cols:
        die("block %dx%d does not divide grid %dx%d" % (block_rows, block_cols, rows, cols))


    def signature_of(cell):
        # Compares contents, not names, because each instance has its own uniquified cell.
        insts = sorted((i.getName(), i.getCellType().getName()) for i in cell.getCellInsts())
        nets = sorted(
            sorted((pi.getFullName() for pi in net.getPortInsts()))
            for net in cell.getNets()
        )
        return (insts, nets)


    # Every copy is stamped from one implementation, so a unit that differs would silently
    # change the design's function.
    reference = signature_of(units[(0, 0)].getCellType())
    for key, inst in sorted(units.items()):
        if signature_of(inst.getCellType()) != reference:
            die("unit %s (%s) differs from unit (0, 0)" % (inst.getName(), inst.getCellType().getName()))

    # A pin is (local row, local column, pin name). Record each block's use of every pin.
    tiles = [(r, c) for r in range(rows // block_rows) for c in range(cols // block_cols)]
    pin_info = {}
    pin_nets = {}
    for (r, c), inst in units.items():
        tile = (r // block_rows, c // block_cols)
        for port_inst in list(inst.getPortInsts()):
            net = port_inst.getNet()
            if net is None:
                continue
            pin = (r % block_rows, c % block_cols, port_inst.getName())
            pin_info[pin] = (port_inst.getPort(), port_inst.getIndex())
            pin_nets.setdefault(pin, {})[tile] = net.getName()

    # A net is internal if every connection is a pin of one block. Internal wiring must be the
    # same in every block, or the blocks are not copies. Owners are cached per net, because the
    # clock and reset reach every unit and recomputing them per pin is quadratic in array size.
    owners_of = {}
    def owners_of_net(net_name):
        if net_name not in owners_of:
            owners = set()
            for port_inst in top.getNet(net_name).getPortInsts():
                cell_inst = port_inst.getCellInst()
                if cell_inst is None or not is_unit(cell_inst):
                    owners.add(None)
                else:
                    match = re.match(regex + "$", cell_inst.getName())
                    owners.add((int(match.group(1)) // block_rows, int(match.group(2)) // block_cols))
            owners_of[net_name] = owners
        return owners_of[net_name]

    internal = {}
    for pin, nets in pin_nets.items():
        for tile, net_name in nets.items():
            if owners_of_net(net_name) == set([tile]):
                internal.setdefault(tile, {}).setdefault(net_name, []).append(pin)

    shapes = set()
    for tile in tiles:
        shapes.add(frozenset(frozenset(pins) for pins in internal.get(tile, {}).values()))
    if len(shapes) != 1:
        die("blocks have %d different internal wiring patterns" % len(shapes))
    internal_pins = set(pin for pins in shapes.pop() for pin in pins)

    # Every other pin goes to a port. Pins share a port only if they are on the same net in
    # every block, so a constant shared in one edge block can't merge ports for all of them.
    port_groups = {}
    for pin, nets in pin_nets.items():
        if pin in internal_pins:
            if len(nets) != len(tiles):
                die("pin %s is internal in only some blocks" % str(pin))
            continue
        signature = tuple(nets.get(tile) for tile in tiles)
        port_groups.setdefault(signature, []).append(pin)

    block = EDIFCell(netlist.getWorkLibrary(), "pblock_unit_block")
    children = {}
    for r in range(block_rows):
        for c in range(block_cols):
            children[(r, c)] = block.createChildCellInst("u_%d_%d" % (r, c), units[(r, c)].getCellType())


    def connect(net, pin):
        port, index = pin_info[pin]
        net.createPortInst(port, index, children[pin[:2]])


    # Wiring is taken from the first block, and the check above makes it the same for all.
    for index, pins in enumerate(sorted(internal.get(tiles[0], {}).values())):
        net = block.createNet("internal_%d" % index)
        for pin in pins:
            connect(net, pin)

    ports = []
    for signature, pins in sorted(port_groups.items(), key=lambda item: sorted(item[1])):
        pins = sorted(pins)
        if all(name == clock for name in signature):
            name = clock
        else:
            name = "u%d_%d_%s" % (pins[0][0], pins[0][1], re.sub(r"\W", "_", pins[0][2]))
        is_output = any(pin_info[pin][0].isOutput() for pin in pins)
        port = block.createPort(name, EDIFDirection.OUTPUT if is_output else EDIFDirection.INPUT, 1)
        net = block.createNet(name)
        net.createPortInst(port)
        for pin in pins:
            connect(net, pin)
        ports.append((port, signature))


    def side_of(signature):
        # The PBlock side facing the neighbor this port connects to. Ports shared by many blocks,
        # such as a reset, have no one neighbor and use TOP.
        sides = set()
        for tile_index, tile in enumerate(tiles):
            if signature[tile_index] is None:
                continue
            for other in owners_of_net(signature[tile_index]):
                if other is None:
                    continue
                dr, dc = other[0] - tile[0], other[1] - tile[1]
                if dr == 0 and dc > 0:
                    sides.add("RIGHT")
                elif dr == 0 and dc < 0:
                    sides.add("LEFT")
                elif dc == 0 and dr > 0:
                    sides.add("BOTTOM")
                elif dc == 0 and dr < 0:
                    sides.add("TOP")
        return sides.pop() if len(sides) == 1 else "TOP"


    # Tells the port flops and ArrayNetlistGraph where each port's neighbor is, so a port that
    # feeds the next block sits on the edge facing it. The V80 fits more copies of a PBlock
    # vertically than horizontally (18 rows against 15 columns for a quarter-region block), so
    # the longer side of the block grid runs along Y. A square grid puts the larger bus along X
    # instead: SLRs are stacked vertically, so that leaves fewer nets crossing one.
    sides = dict((port.getName(), side_of(signature)) for port, signature in ports if port.getName() != clock)
    vertical = len([s for s in sides.values() if s in ("TOP", "BOTTOM")])
    grid_rows, grid_cols = rows // block_rows, cols // block_cols
    if grid_rows != grid_cols:
        transpose = grid_cols > grid_rows
    else:
        transpose = vertical > len(sides) - vertical
    if transpose:
        swap = {"TOP": "LEFT", "BOTTOM": "RIGHT", "LEFT": "TOP", "RIGHT": "BOTTOM"}
        sides = dict((name, swap[side]) for name, side in sides.items())
    side_map = open(side_map_path, "w")
    for name, side in sorted(sides.items()):
        side_map.write("%s %s\n" % (name, side))
    side_map.close()

    # Replace the units in the top with block instances.
    for inst in units.values():
        for port_inst in list(inst.getPortInsts()):
            if port_inst.getNet() is not None:
                port_inst.getNet().removePortInst(port_inst)
        top.removeCellInst(inst)
    for tile_nets in internal.values():
        for net_name in tile_nets:
            top.removeNet(net_name)

    for tile_index, tile in enumerate(tiles):
        inst = top.createChildCellInst("block_%d_%d" % tile, block)
        for port, signature in ports:
            if signature[tile_index] is not None:
                top.getNet(signature[tile_index]).createPortInst(port, inst)

    netlist.resetParentNetMap()

    block_netlist = EDIFTools.createNewNetlist(top.getCellInst("block_0_0"))
    EDIFTools.ensureCorrectPartInEDIF(block_netlist, design.getPartName())
    block_design = Design(block_netlist)
    block_design.setDesignOutOfContext(True)
    block_design.setAutoIOBuffers(False)
    block_design.writeCheckpoint(block_dcp)

    # Every block becomes an instance of one empty cell with the same ports, as Vivado's
    # update_design -black_box leaves it.
    library = block.getLibrary()
    library.removeCell(block)
    black_box = EDIFCell(library, block.getName())
    for port in block.getPorts():
        black_box.addPort(port)
    for inst in top.getCellInsts():
        if inst.getCellType() == block:
            inst.setCellType(black_box)
            inst.addProperty(EDIFCellInst.BLACK_BOX_PROP, True)
    netlist.removeUnusedCellsFromAllWorkLibraries()
    netlist.resetParentNetMap()
    design.addXDCConstraint("create_clock -name %s -period %s [get_ports %s]" % (clock, period, clock))
    design.writeCheckpoint(top_dcp)

    info = open(info_path, "w")
    info.write("CLOCK %s\nCOPIES %d\nTRANSPOSE %d\n" % (clock, len(tiles), int(transpose)))
    for side in ("TOP", "BOTTOM", "LEFT", "RIGHT"):
        info.write("PORTS_%s %d\n" % (side, len([s for s in sides.values() if s == side])))
    info.close()
    print("Grouped %d units into %d blocks with %d ports, clock %s" % (len(units), len(tiles), len(ports), clock))


# Adds or removes temporary flops on a block's ports, the same way PerformanceExplorer does
# with its ensureExternalRoutability option and a side file. Out of context, a port has no
# driver or load for Vivado to route to, so without the flops those nets stay open. They also
# pull each port's logic toward the PBlock side facing its neighbor. The flops sit outside
# the PBlock and are removed after routing, along with any port route that left the PBlock.
# PBlock.containsTile counts the SLL columns inside the rectangle, unlike CONTAIN_ROUTING.
def harness(args):
    mode, in_dcp, out_dcp, clock, ranges = args[0:5]
    design = read_checkpoint(in_dcp)
    pblock = PBlock(design.getDevice(), ranges)
    netlist = design.getNetlist()

    if mode == "add":
        EDIFTools.removeVivadoBusPreventionAnnotations(netlist)
        netlist.resetParentNetMap()
        side_map = InlineFlopTools.parseSideMap(netlist, args[5])
        InlineFlopTools.createAndPlacePortFlopsOnSide(design, clock, pblock, side_map)
    elif mode == "remove":
        InlineFlopTools.removeInlineFlops(design)
        NetTools.unrouteTopLevelNetsThatLeavePBlock(design, pblock)
        outside = []
        routed = 0
        for net in design.getNets():
            if net.isClockNet() or net.isStaticNet() or not net.hasPIPs():
                continue
            routed += 1
            for pip in net.getPIPs():
                if not pblock.containsTile(pip.getTile()):
                    outside.append("%s (via %s)" % (net.getName(), pip.getTile().getName()))
                    break
        print("Routed nets that leave the PBlock: %d of %d" % (len(outside), routed))
        # Nets on CE and reset pins route without containment (only SLL tiles drive them), and in a
        # narrow PBlock one of them sometimes detours outside. A few such nets are unrouted here, so
        # ArrayBuilder routes them inside each copy along with the stitching; final timing covers
        # them. Many leaving nets mean the PBlock is wrong, and that still fails.
        if len(outside) > MAX_UNCONTAINED:
            die("the block's routing is not contained: %d nets leave the PBlock, e.g. %s" % (len(outside), ", ".join(outside[:3])))
        for entry in outside:
            design.getNet(entry.split(" (via")[0]).unroute()
            print("WARNING: unrouted %s, which left the PBlock; ArrayBuilder routes it in each copy" % entry)
    else:
        die("unknown harness mode " + mode)

    EDIFTools.ensurePreservedInterfaceVivado(netlist)
    design.writeCheckpoint(out_dcp)




# ---- Geometry -------------------------------------------------------------------------------
# A PBlock is a rectangle of slices, written SLICE_X<a>Y<b>:SLICE_X<c>Y<d>. Its footprint is the
# box of device tiles that holds those slices, which also covers the DSP, BRAM and URAM tiles and
# the routing between them. A copy can go wherever the same box of tile types appears, because
# RapidWright relocates a module by a tile offset. Comparing tile types, not just site types,
# keeps clock-row (RCLK) and SLR-boundary rows in the comparison, so a copy never straddles one
# that the original does not. ArrayBuilder also rejects a copy that spans two clock regions.

HARD_SITES = (("DSP", "DSP_X"), ("BRAM", "RAMB36_X"), ("URAM", "URAM288_X"))
MAX_UNCONTAINED = 64
# The longest vertical wire in a Versal INT tile spans 12 rows (NN12/SS12).
EDGE_ROWS = 12


def parse_rect(text):
    found = re.match(r"\s*SLICE_X(\d+)Y(\d+):SLICE_X(\d+)Y(\d+)", text)
    if found is None:
        die("cannot parse PBlock " + text)
    x0, y0, x1, y1 = [int(v) for v in found.groups()]
    if x0 > x1 or y0 > y1:
        die("PBlock %s is not lower-left:upper-right" % text)
    return x0, y0, x1, y1


def site_xy(name):
    found = re.match(r"^.+_X(\d+)Y(\d+)$", name)
    return int(found.group(1)), int(found.group(2))


class Geometry(object):
    def __init__(self, device):
        self.device = device
        self.nrows = device.getRows()
        self.ncols = device.getColumns()
        # One string per tile column, one character per tile type, so comparing a column's
        # stretch of tiles is a string comparison. The top and bottom halves of a Versal clock
        # region use mirrored names for the same tiles (REBUF_CLE_LAG_B_CORE and _T_CORE), and
        # RapidWright relocates between them, so the half is dropped from the name.
        codes = {}
        by_ordinal = {}
        self.cols = []
        for c in range(self.ncols):
            chars = []
            for r in range(self.nrows):
                tile = device.getTile(r, c)
                if tile is None:
                    chars.append(u"\x01")
                    continue
                kind = tile.getTileTypeEnum()
                char = by_ordinal.get(kind.ordinal())
                if char is None:
                    name = re.sub(r"_(B|T|BOT|TOP)(?=_|$)", "_V", str(kind))
                    name = re.sub(r"_(BL|TL)(?=_|$)", "_VL", name)
                    name = re.sub(r"_(BR|TR)(?=_|$)", "_VR", name)
                    char = codes.setdefault(name, unichr(0x100 + len(codes)))
                    by_ordinal[kind.ordinal()] = char
                chars.append(char)
            self.cols.append(u"".join(chars))
        self.windows = {}

    def columns_with(self, top, height):
        key = (top, height)
        if key not in self.windows:
            groups = {}
            for c in range(self.ncols):
                groups.setdefault(self.cols[c][top:top + height], []).append(c)
            self.windows[key] = groups
        return self.windows[key]

    def describe(self, rect):
        x0, y0, x1, y1 = rect
        info = {"slices": 0, "slicem": 0, "missing": 0}
        rows, cols, regions, slrs = set(), set(), set(), set()
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                site = self.device.getSite("SLICE_X%dY%d" % (x, y))
                if site is None:
                    info["missing"] += 1
                    continue
                tile = site.getTile()
                rows.add(tile.getRow())
                cols.add(tile.getColumn())
                regions.add(str(site.getClockRegion()))
                slrs.add(tile.getSLR().getId())
                info["slices"] += 1
                if "SLICEM" in str(site.getSiteTypeEnum()):
                    info["slicem"] += 1
        if not rows:
            die("PBlock has no slices")
        info["edge_rows"] = self.rows_to_slr_edge(rect)
        r0, r1, c0, c1 = min(rows), max(rows), min(cols), max(cols)
        info["bbox"] = [r0, r1, c0, c1]
        info["clock_regions"] = sorted(regions)
        info["slrs"] = sorted(slrs)
        info["has_rclk"] = False
        hard = dict((kind, []) for kind, prefix in HARD_SITES)
        for r in range(r0, r1 + 1):
            for c in range(c0, c1 + 1):
                tile = self.device.getTile(r, c)
                if tile is None:
                    continue
                if "RCLK" in str(tile.getTileTypeEnum()):
                    info["has_rclk"] = True
                for site in tile.getSites():
                    for kind, prefix in HARD_SITES:
                        if site.getName().startswith(prefix):
                            hard[kind].append(site_xy(site.getName()))
        info["ranges"] = {}
        for kind, prefix in HARD_SITES:
            info[kind.lower()] = len(hard[kind])
            if hard[kind]:
                xs = [p[0] for p in hard[kind]]
                ys = [p[1] for p in hard[kind]]
                # A Vivado range is a rectangle of sites, so it must hold exactly these sites.
                full = (max(xs) - min(xs) + 1) * (max(ys) - min(ys) + 1) == len(hard[kind])
                info["ranges"][kind] = "%s%dY%d:%s%dY%d%s" % (prefix, min(xs), min(ys), prefix, max(xs), max(ys),
                                                              "" if full else " (not a full rectangle)")
        return info

    # Every lower-left slice (x, y) where a copy of rect fits, mapped to its tile offset. Rows are
    # also kept a whole number of PBlock heights from the original, the rule the flow used before
    # this check existed, so the rows of copies tile the clock regions the same way the original
    # sits in its own.
    def placements(self, rect, info):
        x0, y0, x1, y1 = rect
        r0, r1, c0, c1 = info["bbox"]
        height, width = r1 - r0 + 1, c1 - c0 + 1
        signature = [self.cols[c][r0:r1 + 1] for c in range(c0, c1 + 1)]
        ll = self.device.getSite("SLICE_X%dY%d" % (x0, y0))
        ur = self.device.getSite("SLICE_X%dY%d" % (x1, y1))
        found = {}
        for top in range(0, self.nrows - height + 1):
            for start in self.columns_with(top, height).get(signature[0], []):
                if start + width > self.ncols:
                    continue
                if any(self.cols[start + i][top:top + height] != signature[i] for i in range(1, width)):
                    continue
                dr, dc = top - r0, start - c0
                corners = [self.moved(site, dr, dc) for site in (ll, ur)]
                if None in corners:
                    continue
                if corners[0].getClockRegion() != corners[1].getClockRegion():
                    continue
                if corners[0].getTile().getSLR() != corners[1].getTile().getSLR():
                    continue
                xy = site_xy(corners[0].getName())
                if (xy[1] - y0) % (y1 - y0 + 1) != 0:
                    continue
                found[xy] = (dc, dr)
        return found

    def moved(self, site, dr, dc):
        tile = self.device.getTile(site.getTile().getRow() + dr, site.getTile().getColumn() + dc)
        if tile is None:
            return None
        sites = tile.getSites()
        index = site.getSiteIndexInTile()
        return sites[index] if index < len(sites) else None

    # Slice rows between the rectangle and the bottom and top of its SLR (the device edge or an
    # SLR boundary), the smaller of the two. A long vertical wire that would leave the SLR turns
    # back at its edge, so a block routed within EDGE_ROWS of the edge can use wires that do not
    # exist anywhere else, and RapidWright drops every net that uses one from the copies.
    def rows_to_slr_edge(self, rect):
        x0, y0, x1, y1 = rect
        best = None
        for x in (x0, x1):
            low, high = self.device.getSite("SLICE_X%dY%d" % (x, y0)), self.device.getSite("SLICE_X%dY%d" % (x, y1))
            if low is None or high is None:
                continue
            slr = low.getTile().getSLR()
            below = above = 0
            while below <= EDGE_ROWS:
                s = self.device.getSite("SLICE_X%dY%d" % (x, y0 - below - 1))
                if s is None or s.getTile().getSLR() != slr:
                    break
                below += 1
            while above <= EDGE_ROWS:
                s = self.device.getSite("SLICE_X%dY%d" % (x, y1 + above + 1))
                if s is None or s.getTile().getSLR() != slr:
                    break
                above += 1
            near = min(below, above)
            best = near if best is None else min(best, near)
        return best

    def slr_of(self, x, y):
        return self.device.getSite("SLICE_X%dY%d" % (x, y)).getTile().getSLR().getId()


# Picks a rows x cols grid of copy positions from the legal lower-left slices. Columns are at
# least a PBlock width plus gapx apart and rows a height plus gapy apart, so copies never overlap.
# Every wire between copies in different SLRs pays a direct SLL hop, so the grid with the fewest
# SLR boundaries wins, then the most compact one. Returns None and how far the grid gets in each
# direction when nothing fits.
def choose_grid(geo, positions, width, height, gapx, gapy, rows, cols):
    ys_at = {}
    for x, y in positions:
        ys_at.setdefault(x, set()).add(y)
    picked = []
    for x in sorted(ys_at):
        if len(ys_at[x]) >= rows and (not picked or x - picked[-1] >= width + gapx):
            picked.append(x)
    best, best_rows = None, 0
    for i in range(len(picked) - cols + 1):
        xs = picked[i:i + cols]
        common = sorted(set.intersection(*[ys_at[x] for x in xs]))
        packed = []
        for y in common:
            if not packed or y - packed[-1] >= height + gapy:
                packed.append(y)
        best_rows = max(best_rows, len(packed))
        for j in range(len(packed) - rows + 1):
            ys = packed[j:j + rows]
            slrs = [geo.slr_of(xs[0], y) for y in ys]
            crossings = len([k for k in range(1, rows) if slrs[k] != slrs[k - 1]])
            score = (crossings, ys[-1] - ys[0], xs[-1] - xs[0])
            if best is None or score < best[0]:
                best = (score, xs, ys)
    if best is None:
        # How far the grid gets on its own in each direction, for the error message.
        spaced = []
        for x in sorted(ys_at):
            if not spaced or x - spaced[-1] >= width + gapx:
                spaced.append(x)
        tallest = 0
        for x in ys_at:
            packed = []
            for y in sorted(ys_at[x]):
                if not packed or y - packed[-1] >= height + gapy:
                    packed.append(y)
            tallest = max(tallest, len(packed))
        return None, {"columns": len(spaced), "rows": tallest, "rows_for_cols": best_rows}
    xs, ys = best[1], best[2]
    slr = [geo.slr_of(xs[0], y) for y in ys]
    boundaries = len([k for k in range(1, len(ys)) if slr[k] != slr[k - 1]])
    return {"xs": xs, "ys": ys, "slr_boundaries": boundaries, "slr_adjacent_pairs": boundaries * len(xs),
            "row_gaps": sorted(set(ys[k] - ys[k - 1] - height for k in range(1, len(ys)))),
            "col_gaps": sorted(set(xs[k] - xs[k - 1] - width for k in range(1, len(xs))))}, best_rows


# Geometry for each candidate, in one JVM.
#   rapidwright.py check <candidates.json> <out.json>
# Each candidate is {"id", "part", "rect", "rows", "cols"}, where rows and cols are the
# copy grid on the device.
def check(args):
    candidates = json.load(open(args[0]))
    from com.xilinx.rapidwright.device import Device
    device = Device.getDevice(candidates[0]["part"])
    geo = Geometry(device)
    results = {}
    for cand in candidates:
        rect = parse_rect(cand["rect"])
        info = geo.describe(rect)
        if info["missing"] or len(info["clock_regions"]) != 1 or len(info["slrs"]) != 1:
            info["positions"] = 0
            info["grid"] = None
            info["grid_limit"] = None
            results[cand["id"]] = info
            continue
        positions = geo.placements(rect, info)
        info["positions"] = len(positions)
        grid, limit = choose_grid(geo, positions, rect[2] - rect[0] + 1, rect[3] - rect[1] + 1,
                                  0, 0, cand["rows"], cand["cols"])
        info["grid"] = grid
        info["grid_limit"] = None if grid else limit
        results[cand["id"]] = info
    json.dump(results, open(args[1], "w"), indent=1)


# Describes the device for whoever chooses PBlocks.
#   rapidwright.py fabric <part>            one line per clock region
#   rapidwright.py fabric <part> <region>   the site columns of one clock region
def fabric(args):
    from com.xilinx.rapidwright.device import Device
    device = Device.getDevice(args[0])
    columns = {}
    for tile in device.getAllTiles():
        for site in tile.getSites():
            name = site.getName()
            kind = None
            if name.startswith("SLICE_X"):
                kind = "SLICEM" if "SLICEM" in str(site.getSiteTypeEnum()) else "SLICEL"
            else:
                for k, prefix in HARD_SITES:
                    if name.startswith(prefix):
                        kind = k
            if kind is None:
                continue
            region = str(site.getClockRegion())
            x, y = site_xy(name)
            key = (region, tile.getColumn(), kind, name.split("_X")[0], x)
            columns.setdefault(key, []).append(y)
    regions = {}
    for (region, col, kind, prefix, x), ys in columns.items():
        regions.setdefault(region, []).append((col, kind, prefix, x, min(ys), max(ys), len(ys)))
    if len(args) > 1:
        print("Site columns of %s, left to right. A PBlock spans slice columns; the DSP, BRAM and URAM"
              " columns between its slice columns come with it." % args[1])
        for col, kind, prefix, x, lo, hi, n in sorted(regions.get(args[1], [])):
            print("  %-6s %s_X%-4d Y%d..%d (%d sites)" % (kind, prefix, x, lo, hi, n))
        return
    print("Clock regions: name, SLR, slice X range, slice Y range, slices, DSP58, RAMB36, URAM288")
    order = []
    for region, cols in regions.items():
        slices = [c for c in cols if c[1].startswith("SLICE")]
        if not slices:
            continue
        xs = [c[3] for c in slices]
        y_lo = min(c[4] for c in slices)
        y_hi = max(c[5] for c in slices)
        site = device.getSite("SLICE_X%dY%d" % (xs[0], y_lo))
        counts = dict((k, sum(c[6] for c in cols if c[1] == k)) for k in ("DSP", "BRAM", "URAM"))
        order.append((y_lo, min(xs), "  %-7s SLR%d  X%d..%d  Y%d..%d  %5d  %4d  %3d  %3d" % (
            region, site.getTile().getSLR().getId(), min(xs), max(xs), y_lo, y_hi,
            sum(c[6] for c in slices), counts["DSP"], counts["BRAM"], counts["URAM"])))
    for line in sorted(order):
        print(line[2])


# Writes an ArrayBuilder placement file for the implemented block.
#   place(block_routed.dcp, top_blackbox.dcp, placement.txt, pblock ranges, side map, copy_pblocks.txt, grid.json)
# ArrayNetlistGraph, the graph ArrayBuilder itself uses, decides which copy sits next to which.
# The anchors are the module's own valid placements, kept only where the tile pattern of the
# whole footprint matches (see Geometry), so every copy sees the same clock rows as the original.
def node_shape(node, tile):
    return sorted((w.getTile().getRow() - tile.getRow(), w.getTile().getColumn() - tile.getColumn(), w.getWireName())
                  for w in node.getAllWiresInNode())


# The nets whose routed nodes change shape when moved from the original position to the legal
# anchor farthest from every edge. RapidWright relocates PIPs tile by tile, so a node that turns
# back at an edge in the original (OUT_SS12_BEG -> IN_NN12_END) runs off somewhere else in a copy,
# and ModuleInst/RWRoute leave that net unrouted in every copy.
def edge_only_nets(design, device, anchor, anchors):
    anchors = list(anchors)
    if not anchors:
        return []
    rows = [a.getTile().getRow() for a in anchors]
    cols = [a.getTile().getColumn() for a in anchors]
    mid_r, mid_c = (min(rows) + max(rows)) / 2.0, (min(cols) + max(cols)) / 2.0
    ref = min(anchors, key=lambda a: abs(a.getTile().getRow() - mid_r) + abs(a.getTile().getColumn() - mid_c))
    dr, dc = ref.getTile().getRow() - anchor.getRow(), ref.getTile().getColumn() - anchor.getColumn()
    if dr == 0 and dc == 0:
        return []
    broken = set()
    seen = set()
    for net in design.getNets():
        if net.isStaticNet() or net.isClockNet():
            continue
        for pip in net.getPIPs():
            tile = pip.getTile()
            moved = device.getTile(tile.getRow() + dr, tile.getColumn() + dc)
            for wire, node in ((pip.getStartWireIndex(), pip.getStartNode()), (pip.getEndWireIndex(), pip.getEndNode())):
                key = (tile.getName(), wire)
                if key in seen or node is None:
                    continue
                seen.add(key)
                other = Node.getNode(moved, wire) if moved is not None else None
                if other is None or node_shape(node, tile) != node_shape(other, moved):
                    broken.add(net.getName())
    print("Edge check against %s: %d routed nodes, %d nets that would not relocate" % (ref.getName(), len(seen), len(broken)))
    return broken


def place(args):
    block_dcp, top_dcp, out_path, ranges, side_map_path, pblocks_path, result_path = args[0:7]
    rect = parse_rect(ranges)
    x0, y0, x1, y1 = rect
    width, height = x1 - x0 + 1, y1 - y0 + 1

    design = read_checkpoint(block_dcp)
    device = design.getDevice()
    module = Module(design, True)
    module.calculateAllValidPlacements(device)
    geo = Geometry(device)
    pattern = geo.placements(rect, geo.describe(rect))
    by_offset = dict((offset, xy) for xy, offset in pattern.items())
    anchor = module.getAnchor().getTile()
    legal = {}
    for site in module.getAllValidPlacements():
        offset = (site.getTile().getColumn() - anchor.getColumn(), site.getTile().getRow() - anchor.getRow())
        if offset in by_offset:
            legal[by_offset[offset]] = site
    print("Anchor %s: %d valid placements, %d with a matching footprint" % (
        module.getAnchor().getName(), len(module.getAllValidPlacements()), len(legal)))

    broken = edge_only_nets(design, device, anchor, legal.values())
    if broken:
        die("the block's routing uses wires that exist only where it was implemented, so RapidWright would "
            "drop %d nets (e.g. %s) from every copy and the final route fails. Long wires turn back at an "
            "SLR or device edge: keep the PBlock at least %d slice rows from the top and bottom of its SLR."
            % (len(broken), ", ".join(sorted(broken)[:3]), EDGE_ROWS))

    top = read_checkpoint(top_dcp)
    EDIFTools.removeVivadoBusPreventionAnnotations(top.getNetlist())
    names = ArrayBuilder.getMatchingModuleInstanceNames(module, top)
    side_map = InlineFlopTools.parseSideMap(design.getNetlist(), side_map_path)
    grid = ArrayNetlistGraph(top, names, side_map).getGreedyPlacementGrid()
    grid = dict(((e.getKey().getFirst(), e.getKey().getSecond()), e.getValue()) for e in grid.entrySet())
    cols = max(p[0] for p in grid) + 1
    rows = max(p[1] for p in grid) + 1
    print("ArrayNetlistGraph grid: %d columns, %d rows, %d copies" % (cols, rows, len(grid)))

    chosen, limit = choose_grid(geo, set(legal), width, height, 0, 0, rows, cols)
    if chosen is None:
        die("no %d-row x %d-column grid of copies fits: at most %d columns, %d rows, or %d rows across %d columns"
            % (rows, cols, limit["columns"], limit["rows"], limit["rows_for_cols"], cols))
    xs, ys = chosen["xs"], chosen["ys"]
    out = open(out_path, "w")
    pblocks = open(pblocks_path, "w")
    for (col, row), name in sorted(grid.items()):
        # The grid counts rows downward, and device rows count upward.
        x, y = xs[col], ys[rows - 1 - row]
        out.write("%s %s\n" % (name, legal[(x, y)].getName()))
        dx, dy = x - x0, y - y0
        pblocks.write("%s SLICE_X%dY%d:SLICE_X%dY%d\n" % (name, x0 + dx, y0 + dy, x1 + dx, y1 + dy))
    out.close()
    pblocks.close()
    chosen.update({"rows": rows, "cols": cols, "copies": len(grid), "legal": len(legal)})
    json.dump(chosen, open(result_path, "w"), indent=1)
    print("Grid X%s Y%s" % (xs, ys))


# Everything after the block run, in one JVM so the device loads once: removes the port flops,
# writes the copy placement, then has ArrayBuilder stamp the copies and RWRoute route them.
def array(args):
    flops_dcp, block_dcp, clock, ranges, top_dcp, side_map, placement, pblocks, grid, out_dcp, period = args
    harness(["remove", flops_dcp, block_dcp, clock, ranges])
    place([block_dcp, top_dcp, placement, ranges, side_map, pblocks, grid])
    ArrayBuilder.main(["--input", block_dcp, "--top-design", top_dcp, "--output", out_dcp, "--pblock", ranges,
                       "--clk-period", period, "--kernel-clk-name", clock, "--top-clk-name", clock,
                       "--read-placement", placement, "--skip-impl", "--out-of-context", "--unroute-static-nets", "--route"])


STEPS = {"group": group, "harness": harness, "array": array, "check": check, "fabric": fabric}
if len(sys.argv) < 2 or sys.argv[1] not in STEPS:
    die("usage: rapidwright.py group|harness|array|check|fabric <arguments>")
STEPS[sys.argv[1]](sys.argv[2:])
