# The Vivado steps of the flow. run.py runs one command per Vivado process:
#   vivado -mode batch -source flow.tcl -tclargs <command> <params file> <result file>
# The params file is a Tcl dict. The result file gets one "key<TAB>value" line per result, and
# a failure writes an "error" line instead and exits 1.

set ::STARTED [clock seconds]
# Extra setup uncertainty while the block is placed and routed, to leave slack for the copies.
set ::BLOCK_MARGIN_NS 0.175

proc die {msg} {
    error $msg
}

proc write_file {path text} {
    set f [open $path w]
    puts -nonewline $f $text
    close $f
}

proc read_file {path} {
    set f [open $path r]
    set text [read $f]
    close $f
    return $text
}

proc read_sources {} {
    foreach src $::SOURCES {
        switch -- [file extension $src] {
            .sv             { read_verilog -sv $src }
            .vhd - .vhdl    { read_vhdl $src }
            default         { read_verilog $src }
        }
    }
}

# flatten "none" is for the pre-implemented flow: without a hierarchy, boundary optimization
# makes edge instances differ from the rest (constant inputs, unused outputs), and those can't
# be copies of one block. The flat baseline uses "rebuilt" and keeps normal optimization.
proc synth {flatten} {
    set args [list -top $::TOP -part $::PART -mode out_of_context -flatten_hierarchy $flatten -max_dsp -1]
    foreach g $::GENERICS { lappend args -generic $g }
    read_sources
    set keep_xdc [file normalize keep_hierarchy.xdc]
    if {$flatten eq "none"} {
        write_file $keep_xdc {set_property KEEP_HIERARCHY yes [get_cells -hierarchical -filter {!IS_PRIMITIVE}]}
        read_xdc $keep_xdc
    }
    synth_design {*}$args
}

# On Versal each DSP58 is a macro with internal primitives, so only the macro is counted. A carry
# LUT is one LUT6CY cell plus its LUTCY1 and LUTCY2 parts, so the parts are skipped.
proc cell_counts {{prefix ""}} {
    set counts {}
    foreach {name filter} {
        LUT  {REF_NAME =~ LUT* && REF_NAME !~ LUTCY*}
        FF   {REF_NAME =~ FD*}
        DSP  {REF_NAME =~ DSP58* && PRIMITIVE_LEVEL != INTERNAL}
        BRAM {REF_NAME =~ RAMB*}
        URAM {REF_NAME =~ URAM*}
    } {
        if {$prefix ne ""} { append filter " && NAME =~ $prefix/*" }
        dict set counts $name [llength [get_cells -quiet -hierarchical -filter $filter]]
    }
    return $counts
}

proc route_errors {} {
    if {![regexp {nets with routing errors[.\s]*:\s*(\d+)} [report_route_status -return_string] -> n]} {
        die "cannot read report_route_status"
    }
    return $n
}

proc worst_slack {type args} {
    set paths [get_timing_paths -quiet -delay_type $type -max_paths 1 {*}$args]
    if {[llength $paths] == 0} { return "none" }
    return [get_property SLACK $paths]
}

proc fmax_of {setup} {
    if {$setup eq "none" || $::PERIOD - $setup <= 0} { return "n/a" }
    return [format %.1f [expr {1000.0 / ($::PERIOD - $setup)}]]
}

# Measures the open routed design.
proc measure {} {
    set setup [worst_slack max]
    set result [dict create setup $setup hold [worst_slack min] fmax [fmax_of $setup] errors [route_errors]]
    return [dict merge $result [cell_counts]]
}

# Synthesizes with the hierarchy kept and describes the repeated cells, so run.py can find the
# unit grid: groups.txt has one "<cell> <count> <counts of one instance>" line per repeated
# cell, and units_<cell>.txt lists its instance names.
proc cmd_prepare {params} {
    synth none
    write_checkpoint -force synth.dcp
    write_edif -force synth.edf
    set result [dict create total [cell_counts]]
    set groups {}
    foreach cell [get_cells -quiet -filter {!IS_PRIMITIVE}] {
        # Synthesis uniquifies REF_NAME per instance (systolic_pe__1), so group by the RTL name.
        dict lappend groups [get_property ORIG_REF_NAME $cell] [get_property NAME $cell]
    }
    set lines {}
    dict for {ref names} $groups {
        if {[llength $names] < 2} { continue }
        set names [lsort -dictionary $names]
        write_file units_$ref.txt "[join $names \n]\n"
        lappend lines "$ref [llength $names] [cell_counts [lindex $names 0]]"
    }
    write_file groups.txt "[join $lines \n]\n"
    return $result
}

# Routes the placed block with CONTAIN_ROUTING on. A V80 flop's CE and reset pins are driven
# only from SLL tiles, and those columns are holes in a PBlock's contained tile set, so those
# nets can never route contained. They are routed first without it and locked, then everything
# else routes contained. rapidwright.py harness rejects any route that leaves the PBlock.
proc route_contained {pblock} {
    set ctrl [get_nets -quiet -of_objects \
        [get_pins -hierarchical -filter {REF_PIN_NAME == CE || REF_PIN_NAME == R || REF_PIN_NAME == S}] \
        -filter {TYPE == SIGNAL}]
    set_property CONTAIN_ROUTING 0 $pblock
    if {[llength $ctrl] > 0} {
        route_design -nets $ctrl -auto_delay
        set_property IS_ROUTE_FIXED 1 $ctrl
    }
    set_property CONTAIN_ROUTING 1 $pblock
    route_design
    if {[llength $ctrl] > 0} { set_property IS_ROUTE_FIXED 0 $ctrl }
}

# Places and routes one block inside its PBlock, with port flops from rapidwright.py harness.
proc cmd_impl {params} {
    set clock [dict get $params clock]
    open_checkpoint [dict get $params harness_dcp]
    create_clock -name $clock -period $::PERIOD [get_ports $clock]
    set pb [create_pblock pb_block]
    resize_pblock $pb -add [dict get $params ranges]
    add_cells_to_pblock $pb -top
    set_property IS_SOFT 0 $pb
    set_clock_uncertainty -setup $::BLOCK_MARGIN_NS [get_clocks $clock]
    lock_design -level placement
    opt_design
    # Fanout replicas give port nets several loads, which then miss timing between blocks.
    place_design -no_fanout_opt
    route_contained $pb
    set_clock_uncertainty -setup 0.0 [get_clocks $clock]
    report_utilization -pblocks pb_block -file block_util.rpt
    report_timing_summary -file block_timing.rpt
    set flops [get_cells -quiet -filter {NAME =~ *_rw_inline_flop}]
    set result [dict create wns [worst_slack max] whs [worst_slack min] errors [route_errors]]
    dict set result fmax [fmax_of [dict get $result wns]]
    # Paths through the port flops stand in for the routes to the neighboring copies.
    dict set result port_in_wns [worst_slack max -from $flops]
    dict set result port_out_wns [worst_slack max -to $flops]
    set internal [get_cells -quiet -hierarchical -filter {IS_PRIMITIVE && NAME !~ *_rw_inline_flop}]
    dict set result internal_wns [worst_slack max -from $internal -to $internal]
    if {[dict get $result errors] != 0} { die "block has [dict get $result errors] routing errors inside its PBlock" }
    write_checkpoint -force [dict get $params out_dcp]
    write_edif -force [file rootname [dict get $params out_dcp]].edf
    return $result
}

# Top-level nets between blocks in different SLRs. Each block sits in one SLR, so a net crosses
# when the blocks on its pins do.
proc slr_crossing_nets {} {
    set slrs_of {}
    foreach block [get_cells block_*] {
        set flop [lindex [get_cells -quiet -filter {IS_PRIMITIVE && PRIMITIVE_GROUP == REGISTER} $block/*/*] 0]
        if {$flop eq ""} { die "no register directly inside the units of $block" }
        set slr [get_slrs -of_objects $flop]
        foreach net [get_nets -quiet -filter {TYPE == SIGNAL} -of_objects [get_pins $block/*]] {
            dict lappend slrs_of $net $slr
        }
    }
    set crossing {}
    dict for {net slrs} $slrs_of {
        if {[llength [lsort -unique $slrs]] > 1} { lappend crossing $net }
    }
    return $crossing
}

# Writes the worst setup paths as tab-separated rows for run.py to group by cause.
proc dump_paths {path count} {
    set f [open $path w]
    foreach p [get_timing_paths -quiet -setup -max_paths $count -nworst 1 -slack_lesser_than 0.1] {
        set s [get_property STARTPOINT_PIN $p]
        set e [get_property ENDPOINT_PIN $p]
        set sc [get_cells -of_objects $s]
        set ec [get_cells -of_objects $e]
        puts $f [join [list [get_property SLACK $p] [get_property LOGIC_LEVELS $p] \
            [get_property DATAPATH_DELAY $p] [get_property SKEW $p] $s $e \
            [get_property SITE $sc] [get_property SITE $ec] \
            [get_slrs -quiet -of_objects $sc] [get_slrs -quiet -of_objects $ec]] \t]
    }
    close $f
}

# Finishes the stitched array from ArrayBuilder and measures it.
proc cmd_stitch {params} {
    set clock [dict get $params clock]
    open_checkpoint [dict get $params array_dcp]
    if {[llength [get_clocks -quiet $clock]] == 0} {
        create_clock -name $clock -period $::PERIOD [get_ports $clock]
    }
    set_property HD.CLK_SRC $::CLK_SRC [get_ports $clock]
    # The port flops only give the stitching router real endpoints. The flat run has no such
    # flops and its ports are untimed, so excluding these paths compares the same logic.
    set port_flops [get_cells -hierarchical -filter {NAME =~ *_rw_inline_flop}]
    set_false_path -from $port_flops
    set_false_path -to $port_flops
    # ArrayBuilder's static routing misses LUTCY inputs on Versal, so the VCC and GND nets are
    # routed by Vivado here and again after the full router below.
    route_design -physical_nets
    # RWRoute is not timing-driven, and its routes between blocks in different SLRs are slow
    # enough to set the Fmax. Those nets are unrouted so Vivado's timing-driven router takes
    # them, along with any pins RWRoute could not reach. RWRoute can also leave nets sharing a
    # node, and -preserve keeps those conflicts, so they are unrouted too. -preserve keeps every
    # other route, and -no_psir skips the router's physical synthesis, which would move and
    # replicate cells so that copies stop being identical.
    set crossing [slr_crossing_nets]
    set conflicts [get_nets -quiet -filter {ROUTE_STATUS == CONFLICTS}]
    set redo [lsort -unique [concat $crossing $conflicts]]
    if {[llength $redo] > 0} { route_design -unroute -nets [get_nets $redo] }
    route_design -preserve -no_psir
    # The full router still leaves a few LUTCY VCC pins open. -physical_nets only sees them
    # once the router's state is released.
    route_design -release_memory
    route_design -physical_nets
    # The flat run is timed to the end of routing too, so reports and the checkpoint are not counted.
    set route_seconds [expr {[clock seconds] - $::STARTED}]
    set result [measure]
    dict set result route_seconds $route_seconds
    dict set result slr_nets [llength $crossing]
    dict set result conflict_nets [llength $conflicts]
    dict set result port_flops [llength $port_flops]
    report_route_status -file final_route_status.rpt
    report_timing_summary -file final_timing.rpt
    report_utilization -file final_util.rpt
    dump_paths paths.tsv [dict get $params max_paths]
    # One PBlock per copy, for viewing the tiling in the GUI. The design is already placed and
    # routed, so they constrain nothing.
    foreach {name range} [read_file [dict get $params copy_pblocks]] {
        set pb [create_pblock pb_$name]
        resize_pblock $pb -add $range
        add_cells_to_pblock $pb [get_cells $name]
    }
    write_checkpoint -force [dict get $params final_dcp]
    return $result
}

# A general Vivado run with default directives and no PBlocks. The clock comes from synthesis.
proc cmd_flat {params} {
    synth rebuilt
    set clock [get_ports -of_objects [get_nets -of_objects [lindex [get_pins -hierarchical -filter {REF_PIN_NAME == C}] 0]]]
    if {[llength $clock] != 1} { die "could not trace a single clock port" }
    create_clock -name [get_property NAME $clock] -period $::PERIOD $clock
    set_property HD.CLK_SRC $::CLK_SRC $clock
    opt_design
    place_design
    route_design
    set seconds [expr {[clock seconds] - $::STARTED}]
    write_checkpoint -force [dict get $params flat_dcp]
    report_route_status -file flat_route_status.rpt
    report_timing_summary -file flat_timing.rpt
    report_utilization -file flat_util.rpt
    dump_paths flat_paths.tsv 200
    return [dict merge [measure] [dict create seconds $seconds]]
}

proc write_result {path result} {
    set text ""
    dict for {key value} $result { append text "$key\t[string map {\n " | " \t " "} $value]\n" }
    write_file $path $text
}

lassign $argv command params_path result_path
set params [read_file $params_path]
foreach {global key} {PART part TOP top PERIOD period GENERICS generics SOURCES sources CLK_SRC clk_src} {
    set ::$global [expr {[dict exists $params $key] ? [dict get $params $key] : ""}]
}
cd [dict get $params work_dir]

if {[catch {
    set result [cmd_$command $params]
    dict set result vivado_seconds [expr {[clock seconds] - $::STARTED}]
    write_result $result_path $result
} err]} {
    puts $::errorInfo
    write_result $result_path [dict create error $err]
    exit 1
}
exit 0
