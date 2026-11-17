#!/usr/bin/env python3
"""One run of the pre-implemented flow against flat Vivado, into results/runN/. Start it with run.sh.

  Vivado       synthesizes the design with its hierarchy kept, and run.py finds the unit grid
  Claude       picks the block size and PBlock rectangle once, from agent_instructions.md
  RapidWright  checks the rectangle, then groups the units into one block (rapidwright.py)
  Vivado       places and routes one block inside its PBlock (flow.tcl impl)
  RapidWright  places the copies and routes the wires between them (rapidwright.py place, ArrayBuilder)
  Vivado       reroutes the SLR-crossing nets and times the whole array (flow.tcl stitch)
  Vivado       runs the same design flat, as the baseline (flow.tcl flat)

  run.py --top T --period NS [--generic K=V ...] [--block SPEC] [--flat fresh|none|results/runM] SOURCES

--block "8x2 SLICE_X84Y96:SLICE_X103Y111" skips Claude and uses that block, to reproduce a result.
--flat results/runM reuses that run's flat result, which is fair only for the same design, clock
and Vivado version. The exit code is 0 only if both flows route and the pre-implemented Fmax is higher.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(SCRIPTS)
PART = "xcv80-lsva4737-2MHP-e-S"
CLK_SRC = "BUFGCE_X2Y0"
CLAUDE_MODEL = "claude-opus-5-5"
CLAUDE_BUDGET_USD = 5
# RWRoute can stall on a congested stitch, so ArrayBuilder fails after this long.
ARRAYBUILDER_LIMIT_S = 90 * 60
# Rows a PBlock keeps from the top and bottom of its SLR (rapidwright.py EDGE_ROWS).
EDGE_ROWS = 12


class ToolError(Exception):
    pass


def step(msg):
    print("\n=== [%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def save(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=1)


def load(path):
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def numbers(raw):
    return dict((k, number(v) if number(v) is not None else v) for k, v in raw.items())


# ---- Running the tools ------------------------------------------------------------------------

def clean_env(extra=None):
    """Vivado's library paths break Java and Claude, so they are cleared for each call."""
    env = dict(os.environ)
    for key in ("LD_LIBRARY_PATH", "PYTHONPATH", "PYTHONHOME"):
        env.pop(key, None)
    env.update(extra or {})
    return env


def tail(path, pattern=r"ERROR|FAILED|Exception|Traceback|Error", lines=6):
    if not os.path.exists(path):
        return ""
    with open(path, errors="replace") as f:
        text = f.read().splitlines()
    hits = [l for l in text if re.search(pattern, l) and not l.startswith("#")]
    return " | ".join((hits or text)[-lines:])[-1500:]


def run(cmd, log, cwd, env=None, timeout=None):
    with open(log, "w") as out:
        try:
            return subprocess.call(cmd, stdout=out, stderr=subprocess.STDOUT, cwd=cwd, env=env, timeout=timeout)
        except subprocess.TimeoutExpired:
            out.write("\nTIMEOUT after %d s\n" % timeout)
            raise ToolError("%s ran past its %d-minute limit (log %s)" % (os.path.basename(cmd[0]), timeout // 60, log))


def java_cmd(main, args, cwd):
    rw = os.environ["RAPIDWRIGHT_PATH"]
    return [os.environ.get("JAVA_BIN", "java"), "-Xmx" + os.environ.get("JAVA_HEAP", "64g"), "-Djava.io.tmpdir=" + cwd,
            "-cp", "%s/build/libs/rapidwright.jar:%s/jars/*" % (rw, rw), main] + [str(a) for a in args]


def jython(step_name, args, log, cwd):
    cmd = java_cmd("org.python.util.jython", [os.path.join(SCRIPTS, "rapidwright.py"), step_name] + list(args), cwd)
    if run(cmd, log, cwd, clean_env()) != 0:
        raise ToolError("rapidwright.py %s failed: %s (log %s)" % (step_name, tail(log, r"RAPIDWRIGHT FAILED|Error|Exception"), log))


def tcl_value(value):
    if isinstance(value, (list, tuple)):
        return "{%s}" % " ".join(tcl_value(v) for v in value)
    return "{%s}" % value


def vivado(cfg, command, params, work_dir):
    """Runs one flow.tcl command in its own Vivado process and returns its result lines."""
    os.makedirs(work_dir, exist_ok=True)
    params = dict(params, part=PART, top=cfg["top"], period=cfg["period"], generics=cfg["generics"],
                  sources=cfg["sources"], clk_src=CLK_SRC, work_dir=work_dir)
    params_path = os.path.join(work_dir, command + "_params.tcl")
    result_path = os.path.join(work_dir, command + "_result.txt")
    log = os.path.join(work_dir, command + "_vivado.log")
    with open(params_path, "w") as f:
        for key, value in params.items():
            f.write("%s %s\n" % (key, tcl_value(value)))
    if os.path.exists(result_path):
        os.remove(result_path)
    code = run(["vivado", "-mode", "batch", "-nojournal", "-log", log, "-source", os.path.join(SCRIPTS, "flow.tcl"),
                "-tclargs", command, params_path, result_path], log + ".out", work_dir)
    result = {}
    if os.path.exists(result_path):
        with open(result_path) as f:
            for line in f:
                if "\t" in line:
                    key, value = line.rstrip("\n").split("\t", 1)
                    result[key] = value
    if code != 0 or "error" in result or not result:
        # The first errors name the cause; the last ones only say that a command failed.
        first = []
        if os.path.exists(log):
            with open(log, errors="replace") as f:
                first = [l.strip() for l in f if l.startswith("ERROR:")][:3]
        cause = " | ".join(first)[:1500] or result.get("error") or tail(log)
        raise ToolError("Vivado %s failed: %s (log %s)" % (command, cause, log))
    return result


# ---- Synthesis and the unit grid --------------------------------------------------------------

def infer_grid(names):
    """Finds the two numbers in the instance names that form a full row/column grid."""
    pieces = re.split(r"(\d+)", names[0])
    numeric = list(range(1, len(pieces), 2))
    for a in range(len(numeric)):
        for b in range(a + 1, len(numeric)):
            parts = []
            for i, piece in enumerate(pieces):
                if i in (numeric[a], numeric[b]):
                    parts.append(r"(\d+)")
                elif i % 2 == 1:
                    parts.append(r"\d+")
                else:
                    parts.append(re.escape(piece))
            regex = "".join(parts)
            coords = set()
            for name in names:
                found = re.fullmatch(regex, name)
                if found is None:
                    break
                coords.add((int(found.group(1)), int(found.group(2))))
            else:
                rows = max(r for r, c in coords) + 1
                cols = max(c for r, c in coords) + 1
                if len(coords) == len(names) == rows * cols and rows > 1 and cols > 1:
                    return regex, rows, cols
    return None, 0, 0


def synthesize(cfg, run_dir):
    start = time.time()
    work = os.path.join(run_dir, "synth")
    result = vivado(cfg, "prepare", {}, work)
    groups = []
    with open(os.path.join(work, "groups.txt")) as f:
        for line in f:
            fields = line.split()
            if len(fields) >= 2:
                groups.append((fields[0], int(fields[1]), dict(zip(fields[2::2], [int(v) for v in fields[3::2]]))))
    if not groups:
        raise ToolError("synthesis found no repeated cells")
    # The unit is the repeated cell holding the most logic.
    groups.sort(key=lambda g: (-g[1] * (g[2]["LUT"] + g[2]["FF"]), -g[1], g[0]))
    cell, count, counts = groups[0]
    with open(os.path.join(work, "units_%s.txt" % cell)) as f:
        regex, rows, cols = infer_grid([line.strip() for line in f if line.strip()])
    if regex is None:
        raise ToolError("instances of %s do not form a row/column grid" % cell)
    total = result["total"].split()
    design = {"unit_cell": cell, "unit_regex": regex, "unit_rows": rows, "unit_cols": cols, "unit": counts,
              "total": dict(zip(total[::2], [int(v) for v in total[1::2]])), "synth_seconds": round(time.time() - start, 1),
              "synth_dcp": os.path.join(work, "synth.dcp")}
    save(os.path.join(run_dir, "design.json"), design)
    return design


def fabric(run_dir, region=None):
    log = os.path.join(run_dir, "fabric_%s.log" % (region or "all"))
    jython("fabric", [PART] + ([region] if region else []), log, run_dir)
    with open(log) as f:
        return "\n".join(l for l in f.read().splitlines() if l.startswith(("  ", "Clock", "Site"))) + "\n"


# ---- Claude's choice --------------------------------------------------------------------------

def design_text(cfg, d):
    return ("unit cell %s, %d rows x %d columns of units, instance regex %s\n"
            "one unit uses: %s\nwhole design: %s\nclock period %.3f ns\n" % (
                d["unit_cell"], d["unit_rows"], d["unit_cols"], d["unit_regex"],
                " ".join("%s %d" % kv for kv in sorted(d["unit"].items())),
                " ".join("%s %d" % kv for kv in sorted(d["total"].items())), cfg["period"]))


def ask_claude(cfg, d, run_dir):
    """Asks Claude once for a block size and rectangle. Returns (spec, notes, cost)."""
    regions = fabric(run_dir)
    # Regions with the same counts repeat the same columns, so one full region shows the layout.
    full = [l.split() for l in regions.splitlines() if re.match(r"\s+X\d+Y\d+\s+SLR", l)]
    typical = max(full, key=lambda f: (int(f[5]), int(f[4])))[0]
    with open(os.path.join(SCRIPTS, "agent_instructions.md")) as f:
        prompt = f.read()
    prompt += "\n## DESIGN\n\n```\n%s```\n\n## FABRIC\n\n```\n%s\n%s```\n" % (
        design_text(cfg, d), regions, fabric(run_dir, typical))
    work = os.path.join(run_dir, "claude")
    os.makedirs(work, exist_ok=True)
    with open(os.path.join(work, "prompt.md"), "w") as f:
        f.write(prompt)
    # No tools and no setting sources: the prompt is Claude's only input.
    cmd = [os.environ.get("CLAUDE_BIN", "claude"), "-p", "--model", CLAUDE_MODEL, "--effort", "high", "--tools", "",
           "--setting-sources", "", "--no-session-persistence", "--output-format", "json",
           "--max-budget-usd", str(CLAUDE_BUDGET_USD)]
    with open(os.path.join(work, "prompt.md")) as stdin, open(os.path.join(work, "output.json"), "w") as out, \
            open(os.path.join(work, "stderr.txt"), "w") as err:
        code = subprocess.call(cmd, stdin=stdin, stdout=out, stderr=err, cwd=work,
                               env=clean_env({"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}))
    reply = load(os.path.join(work, "output.json")) or {}
    text = reply.get("result", "")
    with open(os.path.join(work, "answer.md"), "w") as f:
        f.write(text + "\n")
    found = re.findall(r"^SELECTION\s+(\d+x\d+\s+SLICE_X\d+Y\d+:SLICE_X\d+Y\d+)\s*$", text, re.M)
    if code != 0 or not found:
        raise ToolError("Claude gave no SELECTION line (exit %d, see %s)" % (code, work))
    return found[-1], text, reply.get("total_cost_usd")


# ---- The pre-implemented flow ----------------------------------------------------------------

def parse_spec(text):
    found = re.fullmatch(r"\s*(\d+)x(\d+)\s+(SLICE_X(\d+)Y(\d+):SLICE_X(\d+)Y(\d+))\s*", text)
    if not found:
        raise ToolError("a block is '<rows>x<cols> SLICE_X<a>Y<b>:SLICE_X<c>Y<d>', not '%s'" % text)
    g = found.groups()
    return {"tr": int(g[0]), "tc": int(g[1]), "rect": g[2], "xy": [int(v) for v in g[3:]]}


def check(d, block, run_dir):
    """Geometry, resources and the copy grid, from the device itself, before any P&R."""
    rows, cols = d["unit_rows"] // block["tr"], d["unit_cols"] // block["tc"]
    if cols > rows:  # The longer side of the copy grid runs up the device (rapidwright.py group).
        rows, cols = cols, rows
    save(os.path.join(run_dir, "check_in.json"), [{"id": "block", "part": PART, "rect": block["rect"], "rows": rows, "cols": cols}])
    jython("check", [os.path.join(run_dir, "check_in.json"), os.path.join(run_dir, "check_out.json")],
           os.path.join(run_dir, "check.log"), run_dir)
    g = load(os.path.join(run_dir, "check_out.json"))["block"]
    x0, y0, x1, y1 = block["xy"]
    units = block["tr"] * block["tc"]
    use = dict((k, units * d["unit"].get(k, 0)) for k in ("LUT", "FF", "DSP", "BRAM", "URAM"))
    cap = {"LUT": 8 * g["slices"], "FF": 16 * g["slices"], "DSP": g["dsp"], "BRAM": g["bram"], "URAM": g["uram"]}
    problems = []
    if d["unit_rows"] % block["tr"] or d["unit_cols"] % block["tc"]:
        problems.append("%dx%d does not divide the %dx%d unit grid" % (block["tr"], block["tc"], d["unit_rows"], d["unit_cols"]))
    if g["missing"]:
        problems.append("%d sites of the rectangle are not slices (hole or device edge)" % g["missing"])
    if len(g["clock_regions"]) != 1:
        problems.append("spans clock regions %s; each copy must sit inside one" % " ".join(g["clock_regions"]))
    if len(g["slrs"]) != 1:
        problems.append("crosses an SLR boundary")
    elif g.get("edge_rows") is not None and g["edge_rows"] < EDGE_ROWS:
        problems.append("%d slice rows from the top or bottom of its SLR; it needs %d" % (g["edge_rows"], EDGE_ROWS))
    for k in ("LUT", "FF"):
        if use[k] > 0.9 * cap[k]:
            problems.append("%s use %d is over 90%% of %d" % (k, use[k], cap[k]))
    for k in ("DSP", "BRAM", "URAM"):
        if use[k] > cap[k]:
            problems.append("needs %d %s, the rectangle has %d" % (use[k], k, cap[k]))
        elif use[k] and "not a full" in g["ranges"].get(k, ""):
            problems.append("the %s sites inside the rectangle are not one Vivado range: %s" % (k, g["ranges"][k]))
    if not problems and g["grid"] is None:
        lim = g["grid_limit"]
        problems.append("no %d-row x %d-column copy grid fits: at most %d columns or %d rows of copies, and only "
                        "%d rows across %d columns" % (rows, cols, lim["columns"], lim["rows"], lim["rows_for_cols"], cols))
    hard = " ".join(r for k, r in sorted(g["ranges"].items()) if use[k] > 0)
    result = {"spec": "%dx%d %s" % (block["tr"], block["tc"], block["rect"]), "problems": problems,
              "copies": (d["unit_rows"] // block["tr"]) * (d["unit_cols"] // block["tc"]),
              "util": dict((k, round(use[k] / float(cap[k]), 3)) for k in use if cap[k]),
              "positions": g["positions"], "grid": g["grid"], "pblock": (block["rect"] + " " + hard).strip()}
    save(os.path.join(run_dir, "check.json"), result)
    return result


def implement(cfg, d, block, checked, run_dir):
    """Groups the units into blocks, places and routes one block, and picks the copy anchors."""
    work = os.path.join(run_dir, "block")
    os.makedirs(work, exist_ok=True)
    seconds = {}
    t0 = time.time()
    jython("group", [d["synth_dcp"], os.path.join(work, "block_synth.dcp"), os.path.join(work, "top_blackbox.dcp"),
                     os.path.join(work, "info.txt"), os.path.join(work, "side_map.txt"), d["unit_cell"], d["unit_regex"],
                     block["tr"], block["tc"], cfg["period"]], os.path.join(work, "group.log"), work)
    with open(os.path.join(work, "info.txt")) as f:
        info = dict(line.split() for line in f if line.strip())
    seconds["group"] = round(time.time() - t0, 1)
    t0 = time.time()
    clock, ranges, side_map = info["CLOCK"], checked["pblock"], os.path.join(work, "side_map.txt")
    jython("harness", ["add", os.path.join(work, "block_synth.dcp"), os.path.join(work, "block_flops.dcp"), clock, ranges,
                       side_map], os.path.join(work, "harness_add.log"), work)
    raw = vivado(cfg, "impl", {"harness_dcp": os.path.join(work, "block_flops.dcp"), "ranges": ranges, "clock": clock,
                               "out_dcp": os.path.join(work, "block_routed_flops.dcp")}, work)
    jython("harness", ["remove", os.path.join(work, "block_routed_flops.dcp"), os.path.join(work, "block_routed.dcp"),
                       clock, ranges], os.path.join(work, "harness_remove.log"), work)
    for name in ("block_flops", "block_routed_flops"):
        for ext in (".dcp", ".edf"):
            if os.path.exists(os.path.join(work, name + ext)):
                os.remove(os.path.join(work, name + ext))
    seconds["impl"] = round(time.time() - t0, 1)
    t0 = time.time()
    jython("place", [os.path.join(work, "block_routed.dcp"), os.path.join(work, "top_blackbox.dcp"),
                     os.path.join(work, "placement.txt"), ranges, side_map, os.path.join(work, "copy_pblocks.txt"),
                     os.path.join(work, "place.json")], os.path.join(work, "place.log"), work)
    seconds["place"] = round(time.time() - t0, 1)
    return {"dir": work, "clock": clock, "pblock": ranges, "block": numbers(raw), "grid": load(os.path.join(work, "place.json")),
            "ports": dict((k[6:], int(v)) for k, v in info.items() if k.startswith("PORTS_")), "seconds": seconds}


def block_of(pin):
    found = re.match(r"(block_\d+_\d+)/", pin)
    return found.group(1) if found else None


def classify(paths_tsv):
    """Counts the worst paths by where they run: inside a block, or between blocks in one or two SLRs."""
    counts = {}
    if os.path.exists(paths_tsv):
        with open(paths_tsv) as f:
            for line in f:
                fields = line.rstrip("\n").split("\t")
                if len(fields) < 10:
                    continue
                slack, a, b = float(fields[0]), block_of(fields[4]), block_of(fields[5])
                if a is None or b is None:
                    kind = "top level"
                elif a == b:
                    kind = "inside a block"
                else:
                    kind = "between blocks, %s" % ("across SLRs" if fields[8] != fields[9] else "same SLR")
                c = counts.setdefault(kind, {"paths": 0, "failing": 0, "worst": slack, "datapath_ns": float(fields[2])})
                c["paths"] += 1
                c["failing"] += slack < 0
                if slack < c["worst"]:
                    c["worst"], c["datapath_ns"] = slack, float(fields[2])
    return counts


def stitch(cfg, impl, run_dir):
    """Places every copy and routes the stitching (ArrayBuilder), then finishes and times it in Vivado."""
    work = os.path.join(run_dir, "stitch")
    os.makedirs(work, exist_ok=True)
    t0 = time.time()
    array = os.path.join(work, "array.dcp")
    log = os.path.join(work, "arraybuilder.log")
    cmd = java_cmd("com.xilinx.rapidwright.design.tools.ArrayBuilder",
                   ["--input", os.path.join(impl["dir"], "block_routed.dcp"),
                    "--top-design", os.path.join(impl["dir"], "top_blackbox.dcp"), "--output", array,
                    "--pblock", impl["pblock"], "--clk-period", cfg["period"],
                    "--kernel-clk-name", impl["clock"], "--top-clk-name", impl["clock"],
                    "--read-placement", os.path.join(impl["dir"], "placement.txt"), "--skip-impl", "--out-of-context",
                    "--unroute-static-nets", "--route"], work)
    if run(cmd, log, work, clean_env(), ARRAYBUILDER_LIMIT_S) != 0 or not os.path.exists(array):
        raise ToolError("ArrayBuilder failed: %s (log %s)" % (tail(log), log))
    arraybuilder_s = round(time.time() - t0, 1)
    t0 = time.time()
    raw = vivado(cfg, "stitch", {"array_dcp": array, "clock": impl["clock"], "max_paths": 1000,
                                 "copy_pblocks": os.path.join(impl["dir"], "copy_pblocks.txt"),
                                 "final_dcp": os.path.join(work, "final.dcp")}, work)
    for ext in (".dcp", ".edf"):
        if os.path.exists(array[:-4] + ext):
            os.remove(array[:-4] + ext)
    # The final step counts to the end of routing, like the flat run: Vivado's time to its last
    # route plus the process start, not the reports and checkpoint after it.
    startup = max(0.0, (time.time() - t0) - (number(raw.get("vivado_seconds")) or 0.0))
    return {"final": numbers(raw), "paths": classify(os.path.join(work, "paths.tsv")), "final_dcp": os.path.join(work, "final.dcp"),
            "seconds": {"arraybuilder": arraybuilder_s, "final": round(number(raw["route_seconds"]) + startup, 1)}}


# ---- Flat baseline and report -----------------------------------------------------------------

def flat_run(cfg, run_dir, result_dir, source):
    if source == "none":
        return {}
    if source != "fresh":
        flat = load(os.path.join(source if os.path.isabs(source) else os.path.join(ROOT, source), "flat.json"))
        if flat is None:
            raise ToolError("no flat.json in %s" % source)
        flat["source"] = "reused from %s" % source
        return flat
    flat = numbers(vivado(cfg, "flat", {"flat_dcp": os.path.join(result_dir, "flat.dcp")}, os.path.join(run_dir, "flat")))
    flat["minutes"] = round(flat["seconds"] / 60.0, 1)
    flat["source"] = "this run"
    return flat


def show(v):
    if v is None:
        return "n/a"
    if isinstance(v, float) and v.is_integer() and abs(v) >= 1:
        return str(int(v))
    return str(v)


def diff(a, b, kind):
    if a is None or b is None or kind == "none":
        return "n/a"
    if kind == "ns":
        return "%+.3f ns" % (b - a)
    return "%+.1f%%" % (100.0 * (b - a) / a) if a else "n/a"


def write_report(path, cfg, run, flat):
    pre = run.get("stitch", {}).get("final", {})
    lines = ["# %s %s, %s, %s ns clock" % (cfg["top"], " ".join(cfg["generics"]), PART, cfg["period"]), ""]
    if run.get("spec"):
        lines.append("Block: `%s`, chosen by %s." % (run["spec"], run["chosen_by"]))
    if run.get("error"):
        lines.append("Pre-implemented flow FAILED: %s" % run["error"])
    lines += ["Flat baseline: %s." % flat.get("source", "not run"), "",
              "| Metric | Flat Vivado | Pre-implemented | Pre-implemented vs flat |", "|---|---|---|---|"]
    for label, key, kind in (("Compile time (min)", "minutes", "percent"), ("Setup WNS (ns)", "setup", "ns"),
                             ("Hold WNS (ns)", "hold", "ns"), ("Fmax (MHz)", "fmax", "percent"),
                             ("Route errors", "errors", "none"), ("LUT", "LUT", "percent"), ("FF", "FF", "percent"),
                             ("DSP", "DSP", "percent")):
        a, b = flat.get(key), (run.get("minutes") if key == "minutes" else pre.get(key))
        if key == "errors":
            a, b = (None if a is None else int(a)), (None if b is None else int(b))
        lines.append("| %s | %s | %s | %s |" % (label, show(a), show(b), diff(a, b, kind)))
    lines += ["", "Pre-implemented compile time is synthesis, grouping, the block run, anchors, ArrayBuilder and the final "
              "route. Its FF count includes the port flops, whose paths are excluded from timing because the flat run's "
              "ports are untimed."]
    s = run.get("seconds")
    if s:
        lines += ["", "Minutes: " + ", ".join("%s %.1f" % (k, v / 60.0) for k, v in s.items()) + "."]
    c = run.get("check")
    if c:
        lines += ["", "Check: %d copies, %d legal positions, utilization %s." % (
            c["copies"], c["positions"], " ".join("%s %.0f%%" % (k, 100 * v) for k, v in sorted(c["util"].items()) if v))]
    b = run.get("impl", {}).get("block")
    if b:
        lines.append("Block alone: WNS %s ns (%s MHz), through port flops in %s / out %s ns." % (
            b.get("wns"), b.get("fmax"), b.get("port_in_wns"), b.get("port_out_wns")))
    if run.get("stitch"):
        lines += ["", "## Worst paths", ""]
        for kind, c in sorted(run["stitch"]["paths"].items()):
            lines.append("- %s: %d paths under 0.1 ns slack, %d failing, worst %.3f ns (datapath %.3f ns)" % (
                kind, c["paths"], c["failing"], c["worst"], c["datapath_ns"]))
        if not run["stitch"]["paths"]:
            lines.append("No path has less than 0.1 ns of slack.")
        lines.append("\nSLR-crossing nets rerouted by Vivado: %s." % show(pre.get("slr_nets")))
    if run.get("notes"):
        lines += ["", "## Claude's answer", "", "API cost: %s." % ("$%.2f" % run["cost_usd"] if run.get("cost_usd") else "n/a"),
                  "", run["notes"].strip()]
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines))


def next_run(results):
    taken = [int(m.group(1)) for m in (re.fullmatch(r"run(\d+)", n) for n in os.listdir(results)) if m]
    return "run%d" % (max(taken + [0]) + 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", required=True)
    ap.add_argument("--period", required=True, type=float)
    ap.add_argument("--generic", action="append", default=[])
    ap.add_argument("--block", help='"<rows>x<cols> SLICE_X..Y..:SLICE_X..Y.." to skip Claude')
    ap.add_argument("--flat", default="fresh", help="fresh, none, or a results/runN directory to reuse")
    ap.add_argument("sources", nargs="+")
    a = ap.parse_args()

    results = os.path.join(ROOT, "results")
    os.makedirs(results, exist_ok=True)
    name = next_run(results)
    result_dir, run_dir = os.path.join(results, name), os.path.join(ROOT, "build", name)
    os.makedirs(result_dir)
    os.makedirs(run_dir)
    cfg = {"top": a.top, "period": a.period, "generics": a.generic, "sources": [os.path.abspath(s) for s in a.sources]}
    save(os.path.join(run_dir, "config.json"), cfg)
    print("Run %s: %s" % (name, run_dir))

    run = {"chosen_by": "hand (--block)" if a.block else "Claude (%s)" % CLAUDE_MODEL}
    try:
        step("Synthesizing the full design")
        d = synthesize(cfg, run_dir)
        print("Unit %s on a %dx%d grid: %s" % (d["unit_cell"], d["unit_rows"], d["unit_cols"], d["unit"]))
        if a.block:
            run["spec"] = a.block
        else:
            step("Asking Claude for the block size and PBlock")
            run["spec"], run["notes"], run["cost_usd"] = ask_claude(cfg, d, run_dir)
        print("Block: %s" % run["spec"])
        block = parse_spec(run["spec"])
        step("Checking the PBlock")
        run["check"] = check(d, block, run_dir)
        if run["check"]["problems"]:
            raise ToolError("the PBlock fails its check: " + "; ".join(run["check"]["problems"]))
        print("%d copies, %d legal positions, pblock %s" % (run["check"]["copies"], run["check"]["positions"], run["check"]["pblock"]))
        step("Implementing one block")
        run["impl"] = implement(cfg, d, block, run["check"], run_dir)
        print("Block WNS %s ns (%s MHz alone)" % (run["impl"]["block"].get("wns"), run["impl"]["block"].get("fmax")))
        step("Stitching the array")
        run["stitch"] = stitch(cfg, run["impl"], run_dir)
        run["seconds"] = dict([("synthesis", d["synth_seconds"])] + list(run["impl"]["seconds"].items()) +
                              list(run["stitch"]["seconds"].items()))
        run["minutes"] = round(sum(run["seconds"].values()) / 60.0, 1)
        shutil.copy(run["stitch"]["final_dcp"], os.path.join(result_dir, "final.dcp"))
    except ToolError as e:
        run["error"] = str(e)
        print("PRE-IMPLEMENTED FLOW FAILED: %s" % e)
    save(os.path.join(run_dir, "run.json"), run)

    if a.flat == "fresh":
        step("Flat Vivado baseline")
    try:
        flat = flat_run(cfg, run_dir, result_dir, a.flat)
    except ToolError as e:
        flat = {"source": "FAILED: %s" % e}
        print("FLAT RUN FAILED: %s" % e)
    save(os.path.join(result_dir, "flat.json"), flat)

    report = os.path.join(result_dir, "report.md")
    write_report(report, cfg, run, flat)
    step("Done: %s" % report)
    pre_fmax, flat_fmax = number(run.get("stitch", {}).get("final", {}).get("fmax")), number(flat.get("fmax"))
    if pre_fmax is None or flat_fmax is None or run["stitch"]["final"].get("errors") != 0:
        print("RUNTEST FAILED: see %s" % report)
        return 1
    if pre_fmax <= flat_fmax:
        print("RUNTEST FAILED: pre-implemented Fmax %s MHz does not beat flat %s MHz" % (pre_fmax, flat_fmax))
        return 1
    print("RUNTEST PASSED: %s" % os.path.join(result_dir, "final.dcp"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
