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

proc read_sources {sources} {
    foreach src $sources {
        switch -- [file extension $src] {
            .sv             { read_verilog -sv $src }
            .vhd - .vhdl    { read_vhdl $src }
            default         { read_verilog $src }
        }
    }
}

# On Versal each DSP58 is a macro with internal primitives, so only the macro is counted. A carry
# LUT is one LUT6CY cell plus its LUTCY1 and LUTCY2 parts, so the parts are skipped.
proc cell_counts {} {
    set counts {}
    foreach {name filter} {
        LUT  {REF_NAME =~ LUT* && REF_NAME !~ LUTCY*}
        FF   {REF_NAME =~ FD*}
        DSP  {REF_NAME =~ DSP58* && PRIMITIVE_LEVEL != INTERNAL}
        BRAM {REF_NAME =~ RAMB*}
        URAM {REF_NAME =~ URAM*}
    } {
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

# Synthesizes the repeated unit alone. Only the EDIF is written: RapidWright reads it directly,
# and a checkpoint would cost another minute.
proc cmd_synth_unit {params} {
    read_sources [dict get $params sources]
    synth_design -top [dict get $params unit] -part $::PART -mode out_of_context -max_dsp -1
    write_edif -force unit.edf
    return [dict create counts [cell_counts]]
}

# Synthesizes the top with the unit as a black box, so this is only the wiring between units.
# units.txt lists the unit instances, from which run.py reads the unit grid.
proc cmd_synth_top {params} {
    set args [list -top $::TOP -part $::PART -mode out_of_context -flatten_hierarchy none]
    foreach g $::GENERICS { lappend args -generic $g }
    read_sources [dict get $params sources]
    synth_design {*}$args
    set boxes [get_cells -quiet -filter {IS_BLACKBOX}]
    if {[llength $boxes] == 0} { die "the top has no black-box unit instances" }
    set boxes [lsort -dictionary $boxes]
    set first [get_property NAME [lindex $boxes 0]]
    write_file units.txt "[join [get_property NAME $boxes] \n]\n"
    # The wires between the first unit and each unit it connects to directly, so Claude knows
    # how wide the buses between neighbors are. Nets that reach more units (clock, reset) are skipped.
    set wires {}
    foreach net [get_nets -quiet -of_objects [get_pins -quiet -of_objects [lindex $boxes 0]]] {
        set others [lsearch -all -inline -not -exact [get_property NAME [get_cells -quiet -of_objects $net]] $first]
        if {[llength $others] == 1} { dict incr wires [lindex $others 0] }
    }
    set text ""
    dict for {name count} $wires { append text "$name $count\n" }
    write_file neighbors.txt $text
    write_edif -force top.edf
    return [dict create units [llength $boxes]]
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
# run.py starts this process while Claude chooses the block, so the device, which takes most of
# a minute to load, is loaded by the time the block exists. run.py then writes "ready" with the
# PBlock ranges.
proc cmd_impl {params} {
    link_design -part $::PART
    close_design
    while {![file exists ready]} {
        if {[file exists abort]} { die "the block was not built" }
        after 500
    }
    set ranges [string trim [read_file ready]]
    regexp -line {^CLOCK (\S+)} [read_file [dict get $params info]] -> clock
    open_checkpoint [dict get $params harness_dcp]
    create_clock -name $clock -period $::PERIOD [get_ports $clock]
    set pb [create_pblock pb_block]
    resize_pblock $pb -add $ranges
    add_cells_to_pblock $pb -top
    set_property IS_SOFT 0 $pb
    set_clock_uncertainty -setup $::BLOCK_MARGIN_NS [get_clocks $clock]
    lock_design -level placement
    opt_design
    # Fanout replicas give port nets several loads, which then miss timing between blocks.
    place_design -no_fanout_opt
    route_contained $pb
    set_clock_uncertainty -setup 0.0 [get_clocks $clock]
    set flops [get_cells -quiet -filter {NAME =~ *_rw_inline_flop}]
    set result [dict create wns [worst_slack max] whs [worst_slack min] errors [route_errors]]
    dict set result fmax [fmax_of [dict get $result wns]]
    # Paths through the port flops stand in for the routes to the neighboring copies.
    dict set result port_in_wns [worst_slack max -from $flops]
    dict set result port_out_wns [worst_slack max -to $flops]
    if {[dict get $result errors] != 0} { die "block has [dict get $result errors] routing errors inside its PBlock" }
    write_checkpoint -force [dict get $params out_dcp]
    write_edif -force [file rootname [dict get $params out_dcp]].edf
    dict set result clock $clock
    return $result
}

# Times the array RapidWright routed, without changing it. This is outside the compile time:
# Vivado only measures what the flow produced.
proc cmd_timing {params} {
    set clock [dict get $params clock]
    open_checkpoint [dict get $params array_dcp]
    if {[llength [get_clocks -quiet $clock]] == 0} {
        create_clock -name $clock -period $::PERIOD [get_ports $clock]
    }
    # The port flops only give the router real endpoints. The flat run has no such flops and
    # its ports are untimed, so excluding these paths compares the same logic.
    set port_flops [get_cells -hierarchical -filter {NAME =~ *_rw_inline_flop}]
    set_false_path -from $port_flops
    set_false_path -to $port_flops
    set result [measure]
    dict set result port_flops [llength $port_flops]
    report_route_status -file route_status.rpt
    report_timing_summary -file timing.rpt
    dump_paths paths.tsv 1000
    return $result
}

# A general Vivado run with default directives and no PBlocks. The clock comes from synthesis.
proc cmd_flat {params} {
    set args [list -top $::TOP -part $::PART -mode out_of_context -max_dsp -1]
    foreach g $::GENERICS { lappend args -generic $g }
    read_sources [dict get $params sources]
    synth_design {*}$args
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
    dump_paths flat_paths.tsv 200
    return [dict merge [measure] [dict create seconds $seconds]]
}

# Written whole and then renamed, because run.py goes on as soon as the file appears.
proc write_result {path result} {
    set text ""
    dict for {key value} $result { append text "$key\t[string map {\n " | " \t " "} $value]\n" }
    write_file $path.tmp $text
    file rename -force $path.tmp $path
}

lassign $argv command params_path result_path
set params [read_file $params_path]
foreach {global key} {PART part TOP top PERIOD period GENERICS generics CLK_SRC clk_src} {
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
