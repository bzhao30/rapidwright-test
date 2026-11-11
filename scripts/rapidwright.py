# The RapidWright steps of runtest.tcl. Each step runs as its own Jython call:
#
#   rapidwright.py group <in.dcp> <block.dcp> <top_blackbox.dcp> <info.txt> <side map> <unit cell> <regex>
#                        <block rows> <block cols> <clock period>
#   rapidwright.py harness add <in.dcp> <out.dcp> <clock> <pblock ranges> <side map>
#   rapidwright.py harness remove <in.dcp> <out.dcp> <clock> <pblock ranges>
#   rapidwright.py place <block_routed.dcp> <top_blackbox.dcp> <placement.txt> <pblock ranges> <side map>
#                        <copy_pblocks.txt>

import os
import re
import sys

from com.xilinx.rapidwright.design import ClockTools, Design, Module, NetTools
from com.xilinx.rapidwright.design.blocks import PBlock
from com.xilinx.rapidwright.design.tools import ArrayBuilder, ArrayNetlistGraph, InlineFlopTools
from com.xilinx.rapidwright.edif import EDIFCell, EDIFCellInst, EDIFDirection, EDIFTools


# A checkpoint Vivado writes has an encrypted netlist, and RapidWright decrypts it by running
# Vivado again, which takes several minutes on a large design. runtest.tcl writes the netlist
# next to those checkpoints, so this reads it from there when it exists.
def read_checkpoint(dcp):
    edf = dcp[:-len(".dcp")] + ".edf"
    if os.path.exists(edf):
        return Design.readCheckpoint(dcp, edf)
    return Design.readCheckpoint(dcp)


def die(msg):
    print("RAPIDWRIGHT FAILED: " + msg)
    sys.exit(1)


# Groups repeated sibling instances into identical blocks that all reference one shared
# cell. RegroupInstances can't do this: it makes a separate cell per group, with port names
# and port sets that vary by position, and ArrayBuilder needs identical interfaces. The regex
# must capture a unit's row and column from its instance name. Writes one block and the top
# with every block as a black box, which takes Vivado over 10 minutes on a large design.
def group(args):
    in_dcp, block_dcp, top_dcp, info_path, side_map_path, unit_cell, regex = args[0:7]
    block_rows, block_cols, period = int(args[7]), int(args[8]), args[9]

    design = read_checkpoint(in_dcp)
    netlist = design.getNetlist()
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
            for port_inst in top.getNet(signature[tile_index]).getPortInsts():
                cell_inst = port_inst.getCellInst()
                if cell_inst is None or not is_unit(cell_inst):
                    continue
                match = re.match(regex + "$", cell_inst.getName())
                dr = int(match.group(1)) // block_rows - tile[0]
                dc = int(match.group(2)) // block_cols - tile[1]
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
    info.write("CLOCK %s\nCOPIES %d\n" % (clock, len(tiles)))
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
        outside = 0
        routed = 0
        for net in design.getNets():
            if net.isClockNet() or net.isStaticNet() or not net.hasPIPs():
                continue
            routed += 1
            for pip in net.getPIPs():
                if not pblock.containsTile(pip.getTile()):
                    outside += 1
                    break
        print("Routed nets that leave the PBlock: %d of %d" % (outside, routed))
        if outside != 0:
            die("the block's routing is not contained")
    else:
        die("unknown harness mode " + mode)

    EDIFTools.ensurePreservedInterfaceVivado(netlist)
    design.writeCheckpoint(out_dcp)


# Writes an ArrayBuilder placement file. ArrayNetlistGraph, the graph ArrayBuilder itself uses,
# decides which copy sits next to which. This step only picks the anchors, because
# ArrayBuilder's automatic search does not know about the rule below.
#
# Every anchor stays a whole number of PBlock heights from where the block was implemented.
# Routes that cross a clock region's center spine (RCLK) are slower, and a copy shifted by
# anything else straddles it: the same routes measured 0.969 ns aligned and 1.315 ns
# straddling on the V80. Columns are one PBlock width apart, which is always clear of the
# next copy because the block's routing is contained.
def place(args):
    block_dcp, top_dcp, out_path, ranges, side_map_path, pblocks_path = args[0:6]
    match = re.match(r"SLICE_X(\d+)Y(\d+):SLICE_X(\d+)Y(\d+)", ranges)
    if match is None:
        die("cannot parse PBlock " + ranges)
    x0, y0, x1, y1 = [int(v) for v in match.groups()]
    width, height = x1 - x0 + 1, y1 - y0 + 1

    design = read_checkpoint(block_dcp)
    device = design.getDevice()
    module = Module(design, True)
    module.calculateAllValidPlacements(device)


    def slice_xy(site_insts):
        # Lowest slice column and row the block uses. Module's anchor can be a DSP or another
        # site type, so the rules below work in these slice coordinates instead, and each
        # position maps back to the anchor site that ArrayBuilder's placement file needs.
        xs, ys = [], []
        for si in site_insts:
            found = re.match(r"SLICE_X(\d+)Y(\d+)$", si.getSiteName())
            if found and "STATIC_SOURCE" not in si.getName():
                xs.append(int(found.group(1)))
                ys.append(int(found.group(2)))
        return min(xs), min(ys)


    def one_clock_region(inst):
        region = inst.getAnchor().getSite().getClockRegion()
        for si in inst.getSiteInsts():
            if si.getSiteName().startswith("SLICE") and "STATIC_SOURCE" not in si.getName():
                if si.getSite().getClockRegion() != region:
                    return False
        return True


    home_x, home_y = slice_xy(design.getSiteInsts())
    array = Design("array", design.getPartName())
    probe = array.createModuleInst("probe", module)
    # Maps the lowest slice (x, y) of each legal copy to its anchor site.
    legal = {}
    for site in module.getAllValidPlacements():
        if not probe.place(site, True, False):
            continue
        x, y = slice_xy(probe.getSiteInsts())
        if (y - home_y) % height == 0 and one_clock_region(probe):
            legal[(x, y)] = site
        probe.unplace()
    print("Implemented at slice X%dY%d (anchor %s), PBlock height %d, %d aligned anchors"
          % (home_x, home_y, module.getAnchor().getName(), height, len(legal)))

    top = read_checkpoint(top_dcp)
    EDIFTools.removeVivadoBusPreventionAnnotations(top.getNetlist())
    names = ArrayBuilder.getMatchingModuleInstanceNames(module, top)
    side_map = InlineFlopTools.parseSideMap(design.getNetlist(), side_map_path)
    grid = ArrayNetlistGraph(top, names, side_map).getGreedyPlacementGrid()
    grid = dict(((e.getKey().getFirst(), e.getKey().getSecond()), e.getValue()) for e in grid.entrySet())
    cols = max(p[0] for p in grid) + 1
    rows = max(p[1] for p in grid) + 1
    print("ArrayNetlistGraph grid: %d columns, %d rows, %d copies" % (cols, rows, len(grid)))

    def fits(x, y):
        return (x, y) in legal


    # Columns come from the row with the most anchors. Some are legal on only some rows, so the
    # column window is chosen together with its rows: the closest to the implementation that fits.
    ys = sorted(set(p[1] for p in legal))
    ref_y = max(ys, key=lambda y: len([p for p in legal if p[1] == y]))
    all_xs = []
    for x in sorted(p[0] for p in legal if p[1] == ref_y):
        if (not all_xs or x - all_xs[-1] >= width) and fits(x, ref_y):
            all_xs.append(x)
    print("Columns available: %d %s" % (len(all_xs), all_xs))
    if len(all_xs) < cols:
        die("%d columns do not fit, only %d are available" % (cols, len(all_xs)))


    def closest(values, count, center):
        # Returns the run of count values closest to center.
        starts = range(len(values) - count + 1)
        best = min(starts, key=lambda i: abs((values[i] + values[i + count - 1]) // 2 - center))
        return values[best:best + count]


    windows = [all_xs[i:i + cols] for i in range(len(all_xs) - cols + 1)]
    windows.sort(key=lambda w: abs((w[0] + w[-1]) // 2 - home_x))
    xs, good_ys = None, []
    for window in windows:
        window_ys = [y for y in ys if all(fits(x, y) for x in window)]
        print("Columns %d..%d: %d rows" % (window[0], window[-1], len(window_ys)))
        if len(window_ys) >= rows:
            xs, good_ys = window, closest(window_ys, rows, home_y)
            break
    if xs is None:
        die("no %d columns have %d rows in common" % (cols, rows))

    # Each copy's PBlock is the implemented one shifted by the anchor's offset. runtest.tcl draws
    # them on the final design for viewing only.
    out = open(out_path, "w")
    pblocks = open(pblocks_path, "w")
    for (col, row), name in sorted(grid.items()):
        # The grid counts rows downward, and device rows count upward.
        x, y = xs[col], good_ys[rows - 1 - row]
        out.write("%s %s\n" % (name, legal[(x, y)].getName()))
        dx, dy = x - home_x, y - home_y
        pblocks.write("%s SLICE_X%dY%d:SLICE_X%dY%d\n" % (name, x0 + dx, y0 + dy, x1 + dx, y1 + dy))
    out.close()
    pblocks.close()
    print("Grid X%s Y%s" % (xs, good_ys))


STEPS = {"group": group, "harness": harness, "place": place}
if len(sys.argv) < 2 or sys.argv[1] not in STEPS:
    die("usage: rapidwright.py group|harness|place <arguments>")
STEPS[sys.argv[1]](sys.argv[2:])
