# One-shot out-of-context build for the Alveo V80. Start it with scripts/run.sh.
#
#   Vivado:      synthesizes the full design, keeping its hierarchy
#   Claude:      picks the repeated unit, block size and PBlock (pblock_instructions.md)
#   RapidWright: finds the clock and groups the units into one shared block (rapidwright.py)
#   Vivado:      places and routes one block with a hard boundary
#   RapidWright: chooses aligned anchors for ArrayBuilder's own grid (rapidwright.py), then
#                places the copies and routes the stitching (ArrayBuilder)
#   Vivado:      finishes the clock and confirms the result
#   Vivado:      runs the same design flat, as the baseline for comparison
#
# Results:  results/runN/report.md, results/runN/final.dcp and results/runN/flat.dcp
# Details:  build/runN/ holds reports and logs. Intermediate DCPs are kept there only if
#           the pre-implemented flow fails, for debugging.
# A failed check stops that flow, the other flow still runs, and the table records the
# failure. Missing timing is a valid measurement, not a failure. The exit code is 0 only if
# both flows route and the pre-implemented flow has the higher Fmax.

foreach required {RAPIDWRIGHT_PATH CLAUDE_BIN JAVA_BIN JAVA_HEAP} {
    if {![info exists ::env($required)]} {
        puts "RUNTEST FAILED: $required is not set. Start this flow with scripts/run.sh."
        exit 1
    }
}

set SCRIPTS      [file normalize [file dirname [info script]]]
set ROOT         [file dirname $SCRIPTS]
set PART         xcv80-lsva4737-2MHP-e-S
set CLAUDE_MODEL claude-opus-5-5
set CLAUDE_BUDGET_USD 5
set CLK_SRC      BUFGCE_X2Y0
# Must match BLOCKS_PER_CR in pblock_instructions.md.
set BLOCKS_PER_CR 4
# Extra setup margin while the block is placed and routed. The final design is timed with an
# estimated clock, which has more uncertainty than the block's routed clock.
set BLOCK_OVERCONSTRAINT_NS 0.175

# Vivado sets library paths that break external tools, so clear them for each call.
set CLEAN_ENV [list /usr/bin/env -u LD_LIBRARY_PATH -u PYTHONPATH -u PYTHONHOME]

# Stops the current flow. main records the message in the comparison table.
proc die {msg} {
    error $msg
}

proc step {msg} {
    puts "\n=== \[[clock format [clock seconds] -format %T]\] $msg"
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

proc parse_args {} {
    global argv TOP PERIOD GENERICS SOURCES
    set TOP ""
    set PERIOD ""
    set GENERICS {}
    set SOURCES {}
    for {set i 0} {$i < [llength $argv]} {incr i} {
        set arg [lindex $argv $i]
        switch -- $arg {
            --top     { set TOP [lindex $argv [incr i]] }
            --period  { set PERIOD [lindex $argv [incr i]] }
            --generic { lappend GENERICS [lindex $argv [incr i]] }
            default   { lappend SOURCES [file normalize $arg] }
        }
    }
    if {$TOP eq "" || $PERIOD eq "" || [llength $SOURCES] == 0} {
        die "usage: run.sh --top <module> --period <ns> \[--generic NAME=VALUE\]... <source files>"
    }
}

proc read_sources {} {
    global SOURCES
    foreach src $SOURCES {
        switch -- [file extension $src] {
            .sv             { read_verilog -sv $src }
            .vhd - .vhdl    { read_vhdl $src }
            default         { read_verilog $src }
        }
    }
}

proc synth {max_dsp flatten} {
    global PART TOP GENERICS
    set args [list -top $TOP -part $PART -mode out_of_context -flatten_hierarchy $flatten -max_dsp $max_dsp]
    foreach g $GENERICS { lappend args -generic $g }
    read_sources
    # Without a hierarchy, boundary optimization makes edge instances differ from the rest
    # (constant inputs, unused outputs), and those can't be copies of one block. Only the
    # pre-implemented flow needs this, so the flat baseline keeps normal optimization.
    set keep_xdc [file normalize keep_hierarchy.xdc]
    if {$flatten eq "none"} {
        write_file $keep_xdc {set_property KEEP_HIERARCHY yes [get_cells -hierarchical -filter {!IS_PRIMITIVE}]}
        read_xdc $keep_xdc
    } else {
        remove_files -quiet [get_files -quiet $keep_xdc]
    }
    synth_design {*}$args
}

proc cell_counts {{prefix ""}} {
    set counts {}
    # A carry LUT is one LUT6CY cell plus its LUTCY1 and LUTCY2 parts, so skip the parts.
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

# Splits a site name such as SLICE_X12Y96 into {prefix x y}.
proc parse_site {name} {
    if {![regexp {^(.+)_X(\d+)Y(\d+)$} $name -> prefix x y]} {
        die "cannot parse site name '$name'"
    }
    return [list $prefix $x $y]
}

# Returns one field from each row. Vivado's Tcl version may not have lmap.
proc column {rows index} {
    set out {}
    foreach r $rows { lappend out [lindex $r $index] }
    return $out
}

# Returns rows of {name type rpm_x rpm_y} for the fabric sites of a clock region.
proc cr_sites {cr} {
    set sites [get_sites -of_objects [get_clock_regions $cr] \
        -filter {SITE_TYPE =~ SLICE* || SITE_TYPE =~ DSP* || SITE_TYPE =~ RAMB* || SITE_TYPE =~ URAM*}]
    # Some Versal clock regions have no fabric sites, and get_property rejects an empty list.
    if {[llength $sites] == 0} { return {} }
    set rows {}
    foreach s $sites t [get_property SITE_TYPE $sites] x [get_property RPM_X $sites] y [get_property RPM_Y $sites] {
        lappend rows [list $s $t $x $y]
    }
    return $rows
}

# Two clock regions share a family only if every site type sits at the same relative
# position, which is what lets a block relocate between them unchanged. Returns the
# largest family, lowest region first. FAMILY_SIZE only sets the copy limit, and a block
# needs matching sites only under its own footprint, so it is the device's fabric area in
# reference regions. rapidwright.py place makes the exact check once the block exists.
proc find_reference_family {} {
    set families {}
    foreach cr [lsort -dictionary [get_clock_regions]] {
        set rows [cr_sites $cr]
        if {[llength $rows] == 0} { continue }
        set min_x [lindex [lsort -integer [column $rows 2]] 0]
        set min_y [lindex [lsort -integer [column $rows 3]] 0]
        set items {}
        foreach r $rows {
            lappend items "[lindex $r 1]@[expr {[lindex $r 2]-$min_x}],[expr {[lindex $r 3]-$min_y}]"
        }
        dict lappend families [lsort -unique $items] $cr
    }
    set best {}
    dict for {signature crs} $families {
        if {[llength $crs] > [llength $best]} { set best $crs }
    }
    set total_slices 0
    foreach cr [get_clock_regions] { incr total_slices [slice_count [cr_sites $cr]] }
    set ::family_size [expr {$total_slices / [slice_count [cr_sites [lindex $best 0]]]}]
    return $best
}

proc slice_count {rows} {
    set n 0
    foreach r $rows {
        if {[string match SLICE* [lindex $r 1]]} { incr n }
    }
    return $n
}

proc fabric_text {family} {
    global BLOCKS_PER_CR
    set columns {}
    set slices 0
    set slice_rows {}
    set totals [dict create DSP 0 BRAM 0 URAM 0]
    foreach r [cr_sites [lindex $family 0]] {
        lassign $r name type rx ry
        lassign [parse_site $name] prefix x y
        dict lappend columns [list $rx $type $prefix $x] [list $y $ry]
        if {[string match SLICE* $type]} {
            incr slices
            lappend slice_rows $y
        } elseif {[string match DSP58* $type] && ![string match *CPLX* $type] && ![string match *FP* $type]} {
            # Each DSP58 is also listed under CPLX and FP site types; count it once.
            dict incr totals DSP
        } elseif {[string match RAMB36* $type]} {
            dict incr totals BRAM
        } elseif {[string match URAM* $type]} {
            dict incr totals URAM
        }
    }
    set text "REFERENCE_CR [lindex $family 0]\nFAMILY_SIZE $::family_size\nBLOCKS_PER_CR $BLOCKS_PER_CR\n"
    append text "CR_SLICES $slices\nCR_SLICE_ROWS [llength [lsort -unique -integer $slice_rows]]\n"
    append text "CR_DSP [dict get $totals DSP]\nCR_BRAM [dict get $totals BRAM]\nCR_URAM [dict get $totals URAM]\n"
    foreach key [lsort -integer -index 0 [dict keys $columns]] {
        lassign $key rx type prefix x
        set ys [lsort -integer [column [dict get $columns $key] 0]]
        set rys [lsort -integer [column [dict get $columns $key] 1]]
        append text "COL rpm_x=$rx type=$type site=${prefix}_X$x y=[lindex $ys 0]..[lindex $ys end] rpm_y=[lindex $rys 0]..[lindex $rys end] count=[llength $ys]\n"
    }
    return $text
}

# Describes the open synthesized design. Only one instance of each repeated cell is
# measured, because the full per-instance report would be too large for the prompt.
proc design_text {max_dsp} {
    global PART GENERICS
    set p [get_parts $PART]
    set text "PART $PART\nGENERICS $GENERICS\nSYNTH_MAX_DSP $max_dsp\n"
    append text "DEVICE_DSP [get_property DSP $p]\nDEVICE_BRAM [get_property BLOCK_RAMS $p]\nDEVICE_URAM [get_property ULTRA_RAMS $p]\n"
    append text "TOTAL [cell_counts]\n"
    set groups {}
    foreach cell [get_cells -quiet -filter {!IS_PRIMITIVE}] {
        # Synthesis uniquifies REF_NAME per instance (systolic_pe__1), so group by the RTL name.
        dict lappend groups [get_property ORIG_REF_NAME $cell] [get_property NAME $cell]
    }
    dict for {ref names} $groups {
        if {[llength $names] < 2} { continue }
        set names [lsort -dictionary $names]
        append text "REPEAT cell=$ref count=[llength $names] [cell_counts [lindex $names 0]]\n"
        append text "  first [lrange $names 0 3]\n  last [lrange $names end-3 end]\n"
    }
    return $text
}

# Runs Claude once per unique prompt. The answer is cached by prompt hash, so the same
# design, instructions and model always reuse the same selection.
proc ask_claude {prompt run_dir} {
    global ROOT CLEAN_ENV CLAUDE_MODEL CLAUDE_BUDGET_USD
    set prompt_path $run_dir/prompt.md
    write_file $prompt_path $prompt
    set hash [lindex [exec md5sum $prompt_path] 0]
    set cache_dir $ROOT/build/pblock_cache
    file mkdir $cache_dir
    set cached $cache_dir/$hash.txt
    if {[file exists $cached]} {
        puts "Reusing cached selection $cached"
        set ::selection_source "reused from cache, so no Claude time or cost"
    } else {
        set ::selection_source "new Claude call"
        # A neutral directory and no setting sources keep CLAUDE.md files out of the
        # prompt. With no tools, the prompt is Claude's only input.
        set neutral [exec mktemp -d]
        set here [pwd]
        cd $neutral
        set failed [catch {
            exec {*}$CLEAN_ENV $::env(CLAUDE_BIN) -p --model $CLAUDE_MODEL --effort high --tools "" \
                --setting-sources "" --no-session-persistence --output-format text \
                --max-budget-usd $CLAUDE_BUDGET_USD \
                --system-prompt "Follow the instructions in the user message exactly. Output only the requested block." \
                < $prompt_path > $run_dir/claude_output.txt 2> $run_dir/claude_stderr.txt
        } err]
        cd $here
        file delete -force $neutral
        if {$failed} { die "claude call failed: $err (see $run_dir/claude_stderr.txt)" }
        file copy -force $run_dir/claude_output.txt $cached
    }
    return [parse_selection [read_file $cached]]
}

proc parse_selection {text} {
    if {![regexp {BEGIN_SELECTION\s*\n(.*?)\n\s*END_SELECTION} $text -> body]} {
        die "Claude output has no selection block"
    }
    set selection {}
    foreach line [split $body \n] {
        if {[regexp {^\s*(\w+)\s+(.*?)\s*$} $line -> key value]} {
            dict set selection $key $value
        }
    }
    if {![dict exists $selection STATUS]} { die "selection has no STATUS" }
    return $selection
}

# Runs Claude, resynthesizing without DSPs once if it asks. Leaves the final synthesized
# design open and returns the selection.
proc select_block {run_dir} {
    global INSTRUCTIONS CLAUDE_MODEL
    set max_dsp -1
    while {1} {
        synth $max_dsp none
        set family [find_reference_family]
        set fabric [fabric_text $family]
        set design [design_text $max_dsp]
        write_file $run_dir/fabric.txt $fabric
        write_file $run_dir/design.txt $design
        set prompt "[read_file $::SCRIPTS/pblock_instructions.md]\n\n<!-- model: $CLAUDE_MODEL -->\n\n## DESIGN\n\n```\n$design```\n\n## FABRIC\n\n```\n$fabric```\n"
        set selection [ask_claude $prompt $run_dir]
        puts "Selection: $selection"
        set status [dict get $selection STATUS]
        if {$status eq "RESYNTH_NO_DSP" && $max_dsp == -1} {
            close_design
            set max_dsp 0
            continue
        }
        if {$status ne "OK"} {
            set reason [expr {[dict exists $selection REASON] ? [dict get $selection REASON] : "no reason given"}]
            die "no block selected ($status): $reason"
        }
        foreach key {UNIT_CELL UNIT_REGEX TILE_ROWS TILE_COLS SLICE_LL SLICE_UR EXTRA_RANGES} {
            if {![dict exists $selection $key]} { die "selection is missing $key" }
        }
        check_rectangle $selection $family
        set ::max_dsp $max_dsp
        return $selection
    }
}

# Catches a bad rectangle before any P&R time is spent. Later steps are the ground truth:
# Vivado rejects a block that doesn't fit, and ArrayBuilder rejects illegal copies.
proc check_rectangle {selection family} {
    set cr [lindex $family 0]
    lassign [parse_site [dict get $selection SLICE_LL]] p0 x0 y0
    lassign [parse_site [dict get $selection SLICE_UR]] p1 x1 y1
    if {$p0 ne "SLICE" || $p1 ne "SLICE" || $x0 > $x1 || $y0 > $y1} {
        die "rectangle [dict get $selection SLICE_LL]:[dict get $selection SLICE_UR] is not valid"
    }
    set cr_slices 0
    set inside 0
    foreach r [cr_sites $cr] {
        if {![string match SLICE* [lindex $r 1]]} { continue }
        incr cr_slices
        lassign [parse_site [lindex $r 0]] p x y
        if {$x >= $x0 && $x <= $x1 && $y >= $y0 && $y <= $y1} { incr inside }
    }
    if {$inside != ($x1 - $x0 + 1) * ($y1 - $y0 + 1)} {
        die "rectangle is not fully inside $cr, or has gaps ($inside slices)"
    }
    set share [expr {double($inside) / $cr_slices}]
    if {$share < 0.20 || $share > 0.30} {
        die "rectangle holds [format %.0f [expr {$share*100}]]% of $cr; the fixed footprint needs about 25%"
    }
}

proc run_java {log main_class args} {
    global CLEAN_ENV RAPIDWRIGHT_HOME
    set classpath $RAPIDWRIGHT_HOME/build/libs/rapidwright.jar:$RAPIDWRIGHT_HOME/jars/*
    set here [pwd]
    cd [file dirname $log]
    set failed [catch {
        exec {*}$CLEAN_ENV RAPIDWRIGHT_PATH=$RAPIDWRIGHT_HOME $::env(JAVA_BIN) -Xmx$::env(JAVA_HEAP) \
            -Djava.io.tmpdir=[file dirname $log] \
            -cp $classpath $main_class {*}$args > $log 2>@1
    } err]
    cd $here
    if {$failed} { die "$main_class failed: $err (see $log)" }
}

proc route_errors {} {
    if {![regexp {nets with routing errors[.\s]*:\s*(\d+)} [report_route_status -return_string] -> n]} {
        die "cannot read report_route_status"
    }
    return $n
}

proc worst_slack {type} {
    set paths [get_timing_paths -quiet -delay_type $type -max_paths 1]
    if {[llength $paths] == 0} { return "none" }
    return [get_property SLACK $paths]
}

# Routes the placed block with CONTAIN_ROUTING on. A V80 flop's CE and reset pins are driven
# only from SLL tiles, and those columns are holes in a PBlock's contained tile set, so those
# nets can never route contained. They are routed first without it and locked, then everything
# else routes contained. rapidwright.py harness rejects any route that leaves the PBlock.
proc route_contained {pblock} {
    set ctrl [get_nets -of_objects \
        [get_pins -hierarchical -filter {REF_PIN_NAME == CE || REF_PIN_NAME == R || REF_PIN_NAME == S}] \
        -filter {TYPE == SIGNAL}]
    set_property CONTAIN_ROUTING 0 $pblock
    route_design -nets $ctrl -auto_delay
    set_property IS_ROUTE_FIXED 1 $ctrl
    set_property CONTAIN_ROUTING 1 $pblock
    route_design
    set_property IS_ROUTE_FIXED 0 $ctrl
}

# Top-level nets between blocks in different SLRs. Each block sits in one SLR, so a net
# crosses when the blocks on its pins do.
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

proc implement_block {block_dcp pblock_ranges clock out_dcp run_dir} {
    global PERIOD SCRIPTS BLOCK_OVERCONSTRAINT_NS
    set harness_dcp $run_dir/block_flops.dcp
    set routed_dcp $run_dir/block_routed_flops.dcp
    run_java $run_dir/harness_add.log org.python.util.jython $SCRIPTS/rapidwright.py harness \
        add $block_dcp $harness_dcp $clock $pblock_ranges $run_dir/side_map.txt
    open_checkpoint $harness_dcp
    create_clock -name $clock -period $PERIOD [get_ports $clock]
    create_pblock pb_block
    resize_pblock pb_block -add $pblock_ranges
    add_cells_to_pblock pb_block -top
    set_property IS_SOFT 0 [get_pblocks pb_block]
    set_clock_uncertainty -setup $BLOCK_OVERCONSTRAINT_NS [get_clocks $clock]
    lock_design -level placement
    opt_design
    # Fanout replicas give port nets several loads, which then miss timing between blocks.
    place_design -no_fanout_opt
    route_contained [get_pblocks pb_block]
    set_clock_uncertainty -setup 0.0 [get_clocks $clock]
    report_utilization -pblocks pb_block -file $run_dir/block_util.rpt
    report_timing_summary -file $run_dir/block_timing.rpt
    set errors [route_errors]
    if {$errors != 0} { die "block has $errors routing errors inside its PBlock" }
    append ::block_note ", block WNS [worst_slack max] ns"
    write_checkpoint -force $routed_dcp
    write_edif -force [file rootname $routed_dcp].edf
    close_design
    run_java $run_dir/harness_remove.log org.python.util.jython $SCRIPTS/rapidwright.py harness \
        remove $routed_dcp $out_dcp $clock $pblock_ranges
}

# Reads the open routed design. elapsed is the flow's time up to the end of routing, so
# neither flow is charged for reports or checks.
proc measure {elapsed} {
    global PERIOD
    set setup [worst_slack max]
    set fmax n/a
    if {$setup ne "none" && $PERIOD - $setup > 0} {
        set fmax [format %.1f [expr {1000.0 / ($PERIOD - $setup)}]]
    }
    set result [dict create minutes [format %.1f [expr {$elapsed / 60.0}]] setup $setup \
        hold [worst_slack min] fmax $fmax errors [route_errors]]
    return [dict merge $result [cell_counts]]
}

proc close_open_designs {} {
    catch {
        foreach d [get_designs -quiet] {
            current_design $d
            close_design
        }
    }
}

proc run_preimplemented {run_dir final_dcp} {
    global SCRIPTS PERIOD CLK_SRC BLOCKS_PER_CR
    set started [clock seconds]

    step "Synthesizing the full design, then Claude selects the block and PBlock"
    set selection [select_block $run_dir]
    write_checkpoint -force $run_dir/synth.dcp
    write_edif -force $run_dir/synth.edf
    close_design
    set tr [dict get $selection TILE_ROWS]
    set tc [dict get $selection TILE_COLS]
    set pblock_ranges "[dict get $selection SLICE_LL]:[dict get $selection SLICE_UR]"
    if {[dict get $selection EXTRA_RANGES] ne "none"} {
        append pblock_ranges " [dict get $selection EXTRA_RANGES]"
    }
    write_file $run_dir/pblock.txt $pblock_ranges\n

    step "RapidWright groups units into ${tr}x$tc blocks and splits off one block"
    run_java $run_dir/group.log org.python.util.jython $SCRIPTS/rapidwright.py group \
        $run_dir/synth.dcp $run_dir/block_synth.dcp $run_dir/top_blackbox.dcp \
        $run_dir/group_info.txt $run_dir/side_map.txt \
        [dict get $selection UNIT_CELL] [dict get $selection UNIT_REGEX] $tr $tc $PERIOD
    set info [read_file $run_dir/group_info.txt]
    set clock [dict get $info CLOCK]
    set copies [dict get $info COPIES]
    regexp {X(\d+)Y(\d+):SLICE_X(\d+)Y(\d+)} $pblock_ranges -> x0 y0 x1 y1
    set ::block_note "${tr}x$tc units, PBlock $pblock_ranges ([expr {$x1 - $x0 + 1}]x[expr {$y1 - $y0 + 1}] slices), $copies copies"
    set max_copies [expr {$BLOCKS_PER_CR * $::family_size}]
    if {$copies > $max_copies} { die "$copies copies exceed the $max_copies legal positions" }

    step "Vivado implements one block with a hard boundary"
    implement_block $run_dir/block_synth.dcp $pblock_ranges $clock $run_dir/block_routed.dcp $run_dir

    step "RapidWright chooses aligned anchors for $copies copies"
    run_java $run_dir/place.log org.python.util.jython $SCRIPTS/rapidwright.py place \
        $run_dir/block_routed.dcp $run_dir/top_blackbox.dcp $run_dir/placement.txt $pblock_ranges \
        $run_dir/side_map.txt $run_dir/copy_pblocks.txt

    step "RapidWright places $copies copies and routes the stitching"
    # --skip-impl keeps the block routed above. Without it, ArrayBuilder would run its own
    # multi-job PerformanceExplorer sweep instead.
    run_java $run_dir/arraybuilder.log com.xilinx.rapidwright.design.tools.ArrayBuilder \
        --input $run_dir/block_routed.dcp --top-design $run_dir/top_blackbox.dcp \
        --output $run_dir/array.dcp --pblock $pblock_ranges --clk-period $PERIOD \
        --kernel-clk-name $clock --top-clk-name $clock \
        --read-placement $run_dir/placement.txt --skip-impl --out-of-context \
        --unroute-static-nets --route
    if {![file exists $run_dir/array.dcp]} { die "ArrayBuilder wrote no DCP (see $run_dir/arraybuilder.log)" }

    step "Vivado finishes the clock and confirms the result"
    open_checkpoint $run_dir/array.dcp
    if {[llength [get_clocks -quiet $clock]] == 0} {
        create_clock -name $clock -period $PERIOD [get_ports $clock]
    }
    set_property HD.CLK_SRC $CLK_SRC [get_ports $clock]
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
    puts "Nets left to Vivado: [llength $crossing] SLR-crossing, [llength $conflicts] in conflict"
    set redo [lsort -unique [concat $crossing $conflicts]]
    if {[llength $redo] > 0} { route_design -unroute -nets [get_nets $redo] }
    route_design -preserve -no_psir
    # The full router still leaves a few LUTCY VCC pins open. -physical_nets only sees them
    # once the router's state is released.
    route_design -release_memory
    route_design -physical_nets
    set elapsed [expr {[clock seconds] - $started}]
    report_route_status -file $run_dir/final_route_status.rpt
    report_timing_summary -file $run_dir/final_timing.rpt
    report_drc -file $run_dir/final_drc.rpt
    report_utilization -file $run_dir/final_util.rpt
    # One PBlock per copy, for viewing the tiling in the GUI. The design is already placed and
    # routed, so they constrain nothing.
    foreach {name range} [read_file $run_dir/copy_pblocks.txt] {
        set pb [create_pblock pb_$name]
        resize_pblock $pb -add $range
        add_cells_to_pblock $pb [get_cells $name]
    }
    write_checkpoint -force $final_dcp

    # Reported only after the DCP exists, so a failure can be inspected.
    set ::PRE [measure $elapsed]
    set ::port_flops [llength [get_cells -quiet -hierarchical -filter {NAME =~ *_rw_inline_flop*}]]
    set errors [dict get $::PRE errors]
    if {$errors != 0} { die "final design has $errors routing errors" }
    foreach dcp {synth block_synth block_flops block_routed_flops block_routed top_blackbox array} {
        file delete $run_dir/$dcp.dcp $run_dir/$dcp.edf
    }
}

# A general Vivado run with default directives and no PBlocks. The clock comes from
# synthesis, because no block selection may have been made.
proc run_flat {run_dir flat_dcp} {
    global PERIOD CLK_SRC
    set started [clock seconds]
    synth $::max_dsp rebuilt
    set clock [get_ports -of_objects [get_nets -of_objects [lindex [get_pins -hierarchical -filter {REF_PIN_NAME == C}] 0]]]
    if {[llength $clock] != 1} { die "could not trace a single clock port" }
    create_clock -name [get_property NAME $clock] -period $PERIOD $clock
    set_property HD.CLK_SRC $CLK_SRC $clock
    opt_design
    place_design
    route_design
    set elapsed [expr {[clock seconds] - $started}]
    # Written after the time is taken, so the DCP doesn't count toward the flat compile time.
    write_checkpoint -force $flat_dcp
    report_route_status -file $run_dir/flat_route_status.rpt
    report_timing_summary -file $run_dir/flat_timing.rpt
    report_utilization -file $run_dir/flat_util.rpt
    set ::FLAT [measure $elapsed]
}

proc timing_status {result} {
    set setup [dict get $result setup]
    set hold [dict get $result hold]
    if {$setup eq "none" || $setup < 0 || ($hold ne "none" && $hold < 0)} {
        return "routed, timing missed"
    }
    return "routed, timing met"
}

proc one_line {text} {
    return [string map {\n " " | /} $text]
}

proc table_row {label flat pre diff_kind} {
    set diff "n/a"
    if {[string is double -strict $flat] && [string is double -strict $pre]} {
        if {$diff_kind eq "percent" && $flat != 0} {
            set diff [format %+.1f%% [expr {100.0 * ($pre - $flat) / $flat}]]
        } elseif {$diff_kind eq "ns"} {
            set diff [format "%+.3f ns" [expr {$pre - $flat}]]
        }
    }
    return "| $label | $flat | $pre | $diff |\n"
}

proc write_report {path flat_status pre_status} {
    global PART PERIOD TOP GENERICS
    set text "$TOP $GENERICS, $PART, $PERIOD ns clock, synthesis `-max_dsp $::max_dsp`.\n"
    append text "Block: $::block_note. Selection: $::selection_source.\n"
    append text "Pre-implemented time runs from full synthesis to the final clock route, and its FF count includes $::port_flops port flops. Paths to and from those flops are excluded from its timing, because the flat run's ports are untimed.\n\n"
    append text "| Metric | Flat Vivado | Pre-implemented | Pre-implemented vs flat |\n|---|---|---|---|\n"
    append text "| Status | [one_line $flat_status] | [one_line $pre_status] | |\n"
    foreach {label key kind} {
        "Compile time (min)" minutes percent
        "Setup WNS (ns)" setup ns
        "Hold WNS (ns)" hold ns
        "Fmax (MHz)" fmax percent
        "Route errors" errors none
        "LUT" LUT percent
        "FF" FF percent
        "DSP" DSP percent
        "BRAM" BRAM percent
        "URAM" URAM percent
    } {
        set flat [expr {[dict exists $::FLAT $key] ? [dict get $::FLAT $key] : "n/a"}]
        set pre  [expr {[dict exists $::PRE $key] ? [dict get $::PRE $key] : "n/a"}]
        append text [table_row $label $flat $pre $kind]
    }
    write_file $path $text
    puts "\n$text"
}

# Returns the next unused run number, so results/run1, run2 and so on never collide.
proc next_run_name {results_dir} {
    set highest 0
    foreach dir [glob -nocomplain -directory $results_dir -tails run*] {
        if {[regexp {^run(\d+)$} $dir -> n] && $n > $highest} { set highest $n }
    }
    return run[expr {$highest + 1}]
}

proc main {} {
    global ROOT RAPIDWRIGHT_HOME

    set RAPIDWRIGHT_HOME [file normalize $::env(RAPIDWRIGHT_PATH)]
    parse_args
    set run_name [next_run_name $ROOT/results]
    set run_dir $ROOT/build/$run_name
    set result_dir $ROOT/results/$run_name
    set final_dcp $result_dir/final.dcp
    file mkdir $run_dir
    file mkdir $result_dir
    # Vivado writes scratch files to its working directory.
    cd $run_dir

    # Defaults for the table if the pre-implemented flow stops before setting them.
    set ::PRE {}
    set ::FLAT {}
    set ::max_dsp -1
    set ::block_note "none selected"
    set ::selection_source "not reached"
    set ::port_flops "n/a"

    if {[catch {run_preimplemented $run_dir $final_dcp} err]} {
        puts $::errorInfo
        puts "PRE-IMPLEMENTED FLOW FAILED: $err"
        set pre_status "FAILED: $err"
    } else {
        set pre_status [timing_status $::PRE]
    }
    close_open_designs

    step "Flat Vivado baseline"
    if {[catch {run_flat $run_dir $result_dir/flat.dcp} err]} {
        puts $::errorInfo
        puts "FLAT RUN FAILED: $err"
        set flat_status "FAILED: $err"
    } elseif {[dict get $::FLAT errors] != 0} {
        set flat_status "FAILED: [dict get $::FLAT errors] routing errors"
    } else {
        set flat_status [timing_status $::FLAT]
    }
    close_open_designs

    set report $result_dir/report.md
    write_report $report $flat_status $pre_status
    step "Done: $report"

    if {[string match FAILED* $pre_status] || [string match FAILED* $flat_status]} {
        puts "RUNTEST FAILED: see $report"
        return 1
    }
    set pre_fmax [dict get $::PRE fmax]
    set flat_fmax [dict get $::FLAT fmax]
    if {$pre_fmax <= $flat_fmax} {
        puts "RUNTEST FAILED: pre-implemented Fmax $pre_fmax MHz does not beat flat $flat_fmax MHz"
        return 1
    }
    puts "RUNTEST PASSED: $final_dcp"
    return 0
}

if {[catch {main} result]} {
    puts $::errorInfo
    puts "RUNTEST FAILED: $result"
    exit 1
}
exit $result
