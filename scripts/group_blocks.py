# Groups repeated sibling instances into identical blocks that all reference one shared
# cell. RegroupInstances can't do this: it makes a separate cell per group, with port names
# and port sets that vary by position, and ArrayBuilder needs identical interfaces.
#
# Usage: group_blocks.py <in.dcp> <out.dcp> <info.txt> <unit cell> <regex> <block rows> <block cols>
# The regex must capture a unit's row and column from its instance name.

import re
import sys

from com.xilinx.rapidwright.design import ClockTools, Design
from com.xilinx.rapidwright.edif import EDIFCell, EDIFDirection


def die(msg):
    print("GROUPING FAILED: " + msg)
    sys.exit(1)


in_dcp, out_dcp, info_path, unit_cell, regex = sys.argv[1:6]
block_rows, block_cols = int(sys.argv[6]), int(sys.argv[7])

design = Design.readCheckpoint(in_dcp)
netlist = design.getNetlist()
top = netlist.getTopCell()

clock_net = ClockTools.getClockFromDesign(design)
if clock_net is None or "/" in clock_net.getHierarchicalNetName():
    die("no top-level clock found: %s" % clock_net)
clock = clock_net.getHierarchicalNetName()

units = {}
for inst in top.getCellInsts():
    if inst.getCellType().getName() != unit_cell:
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
# same in every block, or the blocks are not copies.
internal = {}
for pin, nets in pin_nets.items():
    for tile, net_name in nets.items():
        net = top.getNet(net_name)
        owners = set()
        for port_inst in net.getPortInsts():
            cell_inst = port_inst.getCellInst()
            if cell_inst is None or cell_inst.getCellType().getName() != unit_cell:
                owners.add(None)
            else:
                match = re.match(regex + "$", cell_inst.getName())
                owners.add((int(match.group(1)) // block_rows, int(match.group(2)) // block_cols))
        if owners == set([tile]):
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
design.writeCheckpoint(out_dcp)

info = open(info_path, "w")
info.write("CLOCK %s\nCOPIES %d\n" % (clock, len(tiles)))
info.close()
print("Grouped %d units into %d blocks with %d ports, clock %s" % (len(units), len(tiles), len(ports), clock))
