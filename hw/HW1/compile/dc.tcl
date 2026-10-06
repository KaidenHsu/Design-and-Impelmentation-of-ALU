# Based on ALU_HW1_TA_dc.tcl. Run one design/period per DC process.
# Copied into each round and invoked there: dcnxt_shell -f dc.tcl.

# metadata
set Company "NSYSU2026ALU"
set Designer "Kaiden Hsu"

# Pipeline pin patterns include significand, exponent and sign register banks.
# FLP_mul_3 has two internal register banks and a combinational output stage.
set stage_maps [dict create]
dict set stage_maps FLP_mul_3 [list \
    [dict create from INPUTS to {s1_*_reg*/D}] \
    [dict create from {s1_*_reg*/Q*} to {s2_*_reg*/D}] \
    [dict create from {s2_*_reg*/Q*} to OUTPUTS]]

# Adders use s<N>_ register banks for every data and sideband signal.
dict set stage_maps FLP_adder_4 [list \
    [dict create from INPUTS to {s1_*_reg*/D}] \
    [dict create from {s1_*_reg*/Q*} to {s2_*_reg*/D}] \
    [dict create from {s2_*_reg*/Q*} to {s3_*_reg*/D}] \
    [dict create from {s3_*_reg*/Q*} to OUTPUTS]]

dict set stage_maps FLP_adder_7 [list \
    [dict create from INPUTS to {s1_*_reg*/D}] \
    [dict create from {s1_*_reg*/Q*} to {s2_*_reg*/D}] \
    [dict create from {s2_*_reg*/Q*} to {s3_*_reg*/D}] \
    [dict create from {s3_*_reg*/Q*} to {s4_*_reg*/D}] \
    [dict create from {s4_*_reg*/Q*} to {s5_*_reg*/D}] \
    [dict create from {s5_*_reg*/Q*} to {s6_*_reg*/D}] \
    [dict create from {s6_*_reg*/Q*} to OUTPUTS]]

# Read an optional launcher setting, retaining the course default if absent.
proc env_or {name default} {
    if {[info exists ::env($name)]} {
        return $::env($name)
    }
    return $default
}

# Required settings identify this design, strategy and isolated round.
proc need_env {name} {
    if {![info exists ::env($name)]} {
        error "Missing environment variable $name; use scripts/run.sh"
    }
    return $::env($name)
}

# Some DC commands return 0 on failure instead of raising a Tcl exception.
proc checked {script} {
    set result [uplevel 1 $script]
    if {$result eq "0"} {
        error "Command returned failure: $script"
    }
    return $result
}

# Convert time to ns and power to W for machine-readable measurements.
proc unit_scale {number suffix kind} {
    set scales [dict create \
        s 1e9 \
        ms 1e6 \
        us 1e3 \
        ns 1 \
        ps 1e-3 \
        fs 1e-6 \
        W 1 \
        mW 1e-3 \
        uW 1e-6 \
        nW 1e-9 \
        pW 1e-12]

    if {![dict exists $scales $suffix]} {
        error "Unsupported $kind unit: $suffix"
    }
    return [expr {double($number) * [dict get $scales $suffix]}]
}

# Extract reported data arrival time and its path endpoints.
proc timing_arrival {filename} {
    # Read the positive arrival line, not the negative slack-subtraction line.
    # Reports can contain one worst path per group; retain the largest arrival.
    set fd [open $filename r]
    set text [read $fd]
    close $fd
    set result ""
    foreach block [split [string map [list "Startpoint:" "\u0001"] $text] "\u0001"] {
        if {[regexp -line \
            {^[ \t]*data arrival time[ \t]+([0-9]+\.?[0-9]*(?:[eE][+-]?[0-9]+)?)[ \t\r]*$} $block all number]} {
            if {$result eq "" || $number > [dict get $result delay]} {
                regexp {^[ \t]*([^ \t\n]+)} $block all start
                regexp -line {^[ \t]*Endpoint:[ \t]+([^ \t\n]+)} $block all end
                set result [dict create delay $number start $start end $end]
            }
        }
    }
    if {$result eq ""} {
        error "No positive data arrival time in $filename"
    }
    return $result
}

# Resolve stage boundaries to data ports or register pins, excluding clk/rst.
proc boundary {pattern} {
    if {$pattern eq "INPUTS"} {
        set ports [all_inputs]
        foreach name {clk rst} {
            set control [get_ports -quiet $name]
            if {[sizeof_collection $control]} {
                set ports [remove_from_collection $ports $control]
            }
        }
        return $ports
    }
    if {$pattern eq "OUTPUTS"} {
        return [all_outputs]
    }
    return [get_pins -hierarchical -quiet $pattern]
}

# Read one numeric area field from the original DC report text.
proc numeric_area {text label} {
    set pattern [format {%s\s*:\s*([0-9.eE+-]+)} $label]
    if {![regexp -- $pattern $text all number]} {
        error "Missing area field: $label"
    }
    return $number
}

proc main {} {
    global stage_maps

    # [Paths, Top Module and Clock Configuration]
    # Retain the TA names. Path_Top is this variant's RTL directory; Path_Syn
    # is this attempt's gate_level directory. Clk_period is in DC library units.
    set HW1_Root [file normalize [need_env HW1_ROOT]]
    set Design [need_env HW1_DESIGN]
    set Optimization [need_env HW1_OPT]
    set Path_Top [file join $HW1_Root RTL $Design]
    set Path_Syn [file normalize [need_env HW1_OUT]]
    set Clk_pin "clk"
    set Clk_period_ns [need_env HW1_PERIOD]

    # Use the TA basename variable for the Verilog export. Other artifact names
    # stay fixed because post-sim and result collection expect those filenames.
    set Dump_file_name "netlist"

    if {$Optimization ni {Area Delay Between}} {
        error "Unknown optimization $Optimization"
    }
    if {![string is double -strict $Clk_period_ns] || $Clk_period_ns <= 0} {
        error "Invalid period"
    }

    # Variants reuse top-module names but compile from separate RTL folders.
    set configurations [dict create \
        FXP_adder {FXP_adder 1} \
        FXP_mul {FXP_mul 1} \
        FLP_adder {FLP_adder 1} \
        FLP_adder_7 {FLP_adder 7} \
        FLP_adder_4 {FLP_adder 4} \
        FLP_mul {FLP_mul 1} \
        FLP_mul_3 {FLP_mul 3}]
    if {![dict exists $configurations $Design]} {
        error "Unknown design $Design"
    }
    lassign [dict get $configurations $Design] Top stages

    set expected [file normalize [file join \
        $HW1_Root gate_level $Design $Optimization round[need_env HW1_ROUND]]]
    if {$Path_Syn ne $expected} {
        error "Output path does not match the configured design/optimization/round"
    }
    file mkdir $Path_Syn [file join $Path_Syn work]
    define_design_lib WORK -path [file join $Path_Syn work]

    # [Library and Tool Setup: Original TA Settings]
    set library_root [env_or HW1_LIBRARY_ROOT \
        /cad/CBDK/ADFP/Executable_Package/Collaterals/IP/stdcell/N16ADFP_StdCell/CCS]
    set ::search_path [concat [list $library_root $Path_Top] $::search_path]
    set ::target_library [list \
        N16ADFP_StdCellss0p72vm40c_ccs.db \
        N16ADFP_StdCellff0p88v125c_ccs.db]
    foreach db $::target_library {
        if {![file exists [file join $library_root $db]]} {
            error "Missing library [file join $library_root $db]"
        }
    }

    set ::link_library [concat [list *] $::target_library [list dw_foundation.sldb]]
    set ::symbol_library {tsmc040.sdb generic.sdb}
    set ::synthetic_library dw_foundation.sldb
    set ::hdlin_translate_off_skip_text "TRUE"
    set ::edifout_netlist_only "TRUE"
    set ::verilogout_no_tri true
    set ::hdlin_enable_presto_for_vhdl "TRUE"

    # Original TA shell preferences; these do not constrain synthesis timing.
    set ::sh_enable_line_editing true
    set ::sh_line_editing_mode emacs
    history keep 100
    alias h history

    # [Read Design File]
    # Use the launcher's ordered source list instead of the TA's example paths.
    # Analyze/elaborate preserves parameter support and round-local WORK isolation.
    set fd [open [need_env HW1_RTL_LIST] r]
    set files [split [string trim [read $fd]] \n]
    close $fd
    foreach source $files {
        if {![file isfile $source]} {
            error "Missing RTL: $source"
        }
        set ::search_path [concat [list [file dirname $source]] $::search_path]
        set format [expr {[file extension $source] eq ".sv" ? "sverilog" : "verilog"}]
        checked [list analyze -format $format -library WORK $source]
    }
    checked [list elaborate $Top -library WORK]
    current_design $Top
    checked {link}
    checked {check_design}

    # [Time Units]
    # Save the original unit report, then convert the launcher's ns clock period.
    redirect -variable units {report_units}
    set fd [open [file join $Path_Syn units_report.txt] w]
    puts $fd $units
    close $fd

    # In "Time_unit : 1.0e-09 Second(ns)", the number multiplies Second.
    # The parenthesized ns is a label; do not convert the value twice.
    set unit_pattern {^\s*Time(?:_|\s+)units?\s*:\s*([0-9.eE+-]+)\s*(Seconds?|[munpf]?s)(?:\([^)]*\))?\s*$}
    if {![regexp -nocase -line $unit_pattern $units match un suffix]} {
        error "Cannot determine library time unit; inspect units_report.txt"
    }
    set suffix [string tolower $suffix]
    if {$suffix in {second seconds}} {
        set suffix s
    }
    set time_ns [unit_scale $un $suffix time]
    if {$time_ns <= 0} {
        error "Library time unit must be positive"
    }
    puts "HW1_DC_UNITS: one library time unit = $time_ns ns"
    set Clk_period [expr {double($Clk_period_ns) / $time_ns}]

    # [Setting Clock Constraints]
    # Combinational variants use the TA's virtual-clock/max-delay alternative.
    # Pipelines use the physical clk port and the original clock-network settings.
    if {$stages == 1} {
        set_max_delay $Clk_period -from [all_inputs] -to [all_outputs]
        create_clock -name $Clk_pin -period $Clk_period
        set_input_delay 0 -clock $Clk_pin [all_inputs]
    } else {
        set Clock_port [get_ports -quiet $Clk_pin]
        if {![sizeof_collection $Clock_port]} {
            error "Pipelined top has no $Clk_pin port"
        }
        create_clock -name $Clk_pin -period $Clk_period $Clock_port
        set_fix_hold [get_clocks $Clk_pin]
        set_dont_touch_network [get_clocks $Clk_pin]
        set_ideal_network $Clock_port
        set_input_delay 0 -clock $Clk_pin \
            [remove_from_collection [all_inputs] $Clock_port]
    }
    set_output_delay 0 -clock $Clk_pin [all_outputs]

    # [Setting Design Environment: Original TA Corners and Wire-Load Model]
    # Do not silently change library corners when refactoring the script.
    set_operating_conditions \
        -min_library N16ADFP_StdCellff0p88v125c_ccs -min ff0p88v125c \
        -max_library N16ADFP_StdCellss0p72vm40c_ccs -max ss0p72vm40c
    set_wire_load_model -name ZeroWireload -library N16ADFP_StdCellss0p72vm40c_ccs
    set_wire_load_mode top

    # [Area Optimization and Compile]
    # Strategies differ by period; retain the same TA compile/max-area commands.
    set_max_area 0
    checked {compile}

    # [Change Naming Rule: Original TA Rules]
    # Escaped brackets keep the literal TA allowed-character set in quoted Tcl.
    set ::bus_inference_style {%s[%d]}
    set ::bus_naming_style {%s[%d]}
    set ::hdlout_internal_busses true
    change_names -hierarchy -rule verilog
    define_name_rules name_rule -allowed "A-Z a-z 0-9 _" -max_length 255 -type cell
    define_name_rules name_rule -allowed "A-Z a-z 0-9 _\[\]" -max_length 255 -type net
    define_name_rules name_rule -map {{"\\*cell\\*" "cell"}} -case_insensitive
    change_names -hierarchy -rules name_rule
    remove_unconnected_ports -blast_buses [get_cells -hierarchical *]

    # [Timing Coverage]
    # Period tuning requires constrained timing paths. Save all diagnostics.
    redirect -variable coverage {check_timing}
    set fd [open [file join $Path_Syn check_timing.txt] w]
    puts $fd $coverage
    close $fd
    puts $coverage
    if {[regexp -nocase {(Warning:|Error:)[^\n]*(unconstrained|not constrained|no clock|no input delay|no output delay)} $coverage]} {
        error "Required timing coverage is missing; correct the constraints before tuning"
    }
    set coverage_status clean
    if {[regexp -nocase {Warning:|Error:} $coverage]} {
        set coverage_status reviewed_with_warnings
    }
    if {[regexp -nocase {Warning:|Error:} $coverage] && [env_or HW1_TIMING_REVIEWED 0] ne "1"} {
        error "check_timing diagnostics require review; see check_timing.txt. Use --input-delays-reviewed only after correcting/reviewing coverage."
    }

    # [Report Timing]
    # Restore the TA console report and keep the detailed report file.
    report_timing -significant_digits 6 -sort_by group
    redirect -file [file join $Path_Syn timing_report.txt] {
        report_timing -path full -delay max -significant_digits 6 -sort_by group
    }

    # Find the worst setup slack across all path groups, not only one group.
    set worst ""
    foreach_in_collection group [get_path_groups *] {
        set paths [get_timing_paths \
            -group [get_object_name $group] -delay max -max_paths 1 -nworst 1]
        foreach_in_collection path $paths {
            set slack [get_attribute $path slack]
            if {![string is double -strict $slack]} {
                error "Invalid timing attribute"
            }
            if {$worst eq "" || $slack < $worst} {
                set worst $slack
            }
        }
    }
    if {$worst eq ""} {
        error "No constrained max-delay timing paths"
    }

    # Reported delay follows the TA's data-arrival-time convention.
    # Slack remains separate and is used only for timing feasibility/tuning.
    set critical_delay [dict get \
        [timing_arrival [file join $Path_Syn timing_report.txt]] delay]

    # [Report Area]
    # Use the documented TA library um2 convention, without guessed scaling.
    redirect -variable area_text {report_area -hier -nosplit}
    set fd [open [file join $Path_Syn area_report.txt] w]
    puts $fd $area_text
    close $fd
    set comb [numeric_area $area_text {Combinational area}]
    set seq [numeric_area $area_text {Noncombinational area}]
    set total [numeric_area $area_text {Total cell area}]

    # [Report Power]
    # Keep the TA effort setting and convert explicit report suffixes to W.
    # Unavailable power fields stay blank rather than becoming invented zeros.
    set dynamic ""
    set leakage ""
    set power ""
    if {[catch {
        redirect -variable power_text {report_power -analysis_effort low}
    } issue]} {
        puts "WARN: power report unavailable: $issue"
    } else {
        set fd [open [file join $Path_Syn power_report.txt] w]
        puts $fd $power_text
        close $fd
        if {[regexp {Total Dynamic Power\s*=\s*([0-9.eE+-]+)\s*([munp]?W)} $power_text all number unit]} {
            set dynamic [unit_scale $number $unit power]
        }
        if {[regexp {Cell Leakage Power\s*=\s*([0-9.eE+-]+)\s*([munp]?W)} $power_text all number unit]} {
            set leakage [unit_scale $number $unit power]
        }
        if {$dynamic ne "" && $leakage ne ""} {
            set power [expr {$dynamic + $leakage}]
        } else {
            puts "WARN: power units/values not extracted; inspect power_report.txt"
        }
    }

    # [Each Pipeline Delay]
    # Actual register-bank boundaries replace the TA's example m1/* ... m7/*.
    # Full path reports provide evidence; stages.tsv feeds report collection.
    set sf [open [file join $Path_Syn stages.tsv] w]
    for {set i 1} {$i <= $stages && $stages > 1} {incr i} {
        set delay ""
        set start ""
        set end ""
        set status unmapped

        if {[dict exists $stage_maps $Design] && [llength [dict get $stage_maps $Design]] >= $i} {
            set mapping [lindex [dict get $stage_maps $Design] [expr {$i - 1}]]
            if {[catch {
                set from [boundary [dict get $mapping from]]
                set to [boundary [dict get $mapping to]]
                if {![sizeof_collection $from] || ![sizeof_collection $to]} {
                    error "Empty stage boundary"
                }

                redirect -file [file join $Path_Syn timing_report_stage${i}.txt] {
                    report_timing -from $from -to $to -path full -delay max \
                        -nworst 1 -max_paths 1 -significant_digits 6 -sort_by group
                }
                set measured [timing_arrival \
                    [file join $Path_Syn timing_report_stage${i}.txt]]
                set delay [expr {[dict get $measured delay] * $time_ns}]
                set start [dict get $measured start]
                set end [dict get $measured end]
                set status measured
            } issue]} {
                set status invalid
                puts "WARN: stage $i: $issue"
                set delay ""
            }
        } else {
            puts "WARN: $Design stage $i mapping is not configured"
        }
        puts $sf "$i\t$delay\t$start\t$end\t$status"
    }
    close $sf

    # [Write Out]
    # Keep TA commands/options; fixed filenames maintain the automation interface.
    checked [list write -hierarchy -format ddc \
        -output [file join $Path_Syn design.ddc]]
    checked [list write -format verilog -hierarchy \
        -output [file join $Path_Syn ${Dump_file_name}.v]]
    checked [list write_sdf -version 2.1 -context verilog \
        [file join $Path_Syn timing.sdf]]
    checked [list write_sdc [file join $Path_Syn constraints.sdc]]

    # [Automation Measurements]
    # Stable keys feed period tuning/CSV collection. Preserve raw numeric precision.
    set fd [open [file join $Path_Syn metrics.tsv] w]
    foreach {key value} [list \
        schema 1 \
        period_ns $Clk_period_ns \
        slack_ns [expr {$worst * $time_ns}] \
        delay_ns [expr {$critical_delay * $time_ns}] \
        delay_basis timing_report_data_arrival \
        time_unit_ns $time_ns \
        comb_um2 $comb \
        seq_um2 $seq \
        total_um2 $total \
        dynamic_w $dynamic \
        leakage_w $leakage \
        power_w $power \
        power_activity default_estimated \
        area_units TA_library_um2 \
        sdf_corner min_typ_max \
        coverage_status $coverage_status \
        complete 1] {
        puts $fd "$key\t$value"
    }
    close $fd

    # TA optional example remains disabled; do not introduce clock-gating analysis.
    # report_clock_gating -gating_elements
    puts "HW1_DC_COMPLETE: $Design $Optimization period=$Clk_period_ns ns"
}

# Give the launcher a reliable exit status and retain complete Tcl error details.
if {[catch {main} failure options]} {
    puts stderr "HW1_DC_ERROR: $failure"
    if {[dict exists $options -errorinfo]} {
        puts stderr [dict get $options -errorinfo]
    }
    exit 1
}

exit 0
