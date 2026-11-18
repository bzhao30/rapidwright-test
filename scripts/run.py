#!/usr/bin/env python3
"""Pre-implemented compile of a repeated-unit design on the V80, against flat Vivado, into results/runN/.

  scripts/run.py --top systolic64 --period 1.8 --generic ROWS=64 --generic COLS=64 hdl/src/systolic64.v

Compile, timed from RTL to the routed checkpoint:
  1. Vivado synthesizes the repeated unit alone, and the top with the unit as a black box, in parallel.
  2. Claude ranks up to 3 block sizes and PBlock rectangles from agent_instructions.md, and the first
     that passes RapidWright's check against the device is used, moved by a few slices if that is
     what it takes. If none passes, Claude is asked again with the problems, up to 3 times.
  3. RapidWright fills the black boxes with the unit and groups the units into blocks.
  4. Vivado places and routes one block inside its PBlock.
  5. RapidWright stamps the block across the device and RWRoute routes everything between copies
     (ArrayBuilder). Its checkpoint is the result: the full design never goes back into Vivado.
Measured afterwards, outside the compile time:
  6. Vivado opens the result only to time it and check its routing.
  7. Vivado runs the same RTL flat, as the baseline.

Options: --block "8x2 SLICE_X84Y96:SLICE_X103Y111" skips Claude. --unit NAME names the repeated
module if the top instantiates several. --flat fresh|none|results/runM runs, skips or reuses the
flat baseline (reuse only for the same RTL, clock and Vivado). The exit code is 0 if the result
is fully routed within the 45-minute limit.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

# ---- Edit these for your machine, or set them in the environment. ----------------------------
VIVADO_SETTINGS = os.environ.get("VIVADO_SETTINGS", "/opt/xilinx/Vivado/2024.2/settings64.sh")
RAPIDWRIGHT_PATH = os.environ.get("RAPIDWRIGHT_PATH", "/work/shared/users/meng/bjz24/RapidWright")
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
JAVA_BIN = os.environ.get("JAVA_BIN", "java")
JAVA_HEAP = os.environ.get("JAVA_HEAP", "64g")
# ------------------------------------------------------------------------------------------------

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(SCRIPTS)
PART = "xcv80-lsva4737-2MHP-e-S"
CLK_SRC = "BUFGCE_X2Y0"
CLAUDE_MODEL = "claude-opus-5-5"
CLAUDE_EFFORT = "medium"
CLAUDE_TRIES = 3
TARGET_MIN, LIMIT_MIN = 10, 45
# RWRoute can stall on a congested array, so ArrayBuilder fails after this long.
ARRAYBUILDER_LIMIT_S = 30 * 60
# Rows a PBlock keeps from the top and bottom of its SLR (rapidwright.py EDGE_ROWS).
EDGE_ROWS = 12
# The most empty slices between neighboring copies. ArrayBuilder runs RWRoute with a fixed bounding
# box, which gives up on SLR crossings that also span a wider gap (32 rows in run45).
MAX_GAP = 16


class ToolError(Exception):
    pass


def step(msg):
    print("=== [%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


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
    """The environment for Java and Claude: Vivado's library paths break both."""
    env = dict(os.environ)
    for key in ("LD_LIBRARY_PATH", "PYTHONPATH", "PYTHONHOME"):
        env.pop(key, None)
    env.update(extra or {})
    return env


_vivado_env = None


def vivado_env():
    global _vivado_env
    if _vivado_env is None:
        if not os.path.exists(VIVADO_SETTINGS):
            raise ToolError("VIVADO_SETTINGS not found: %s" % VIVADO_SETTINGS)
        out = subprocess.check_output(["bash", "-c", 'source "$1" > /dev/null 2>&1; env -0', "_", VIVADO_SETTINGS])
        _vivado_env = dict(kv.split("=", 1) for kv in out.decode(errors="replace").split("\0") if "=" in kv)
        # Vivado writes large temporary files, which shared machines' /tmp often has no room for.
        _vivado_env["TMPDIR"] = os.path.join(ROOT, "build", "tmp")
        os.makedirs(_vivado_env["TMPDIR"], exist_ok=True)
    return _vivado_env


def tail(path, pattern=r"ERROR|FAILED|Exception|Traceback|Error", lines=6):
    if not os.path.exists(path):
        return ""
    with open(path, errors="replace") as f:
        text = f.read().splitlines()
    hits = [l for l in text if re.search(pattern, l) and not l.startswith("#")]
    return " | ".join((hits or text)[-lines:])[-1500:]


def run(cmd, log, cwd, env, timeout=None):
    with open(log, "w") as out:
        try:
            return subprocess.call(cmd, stdout=out, stderr=subprocess.STDOUT, cwd=cwd, env=env, timeout=timeout)
        except subprocess.TimeoutExpired:
            out.write("\nTIMEOUT after %d s\n" % timeout)
            raise ToolError("%s ran past its %d-minute limit (log %s)" % (os.path.basename(cmd[0]), timeout // 60, log))


def java(main, args, log, cwd, timeout=None):
    cmd = [JAVA_BIN, "-Xmx" + JAVA_HEAP, "-Djava.io.tmpdir=" + cwd, "-cp",
           "%s/build/libs/rapidwright.jar:%s/jars/*" % (RAPIDWRIGHT_PATH, RAPIDWRIGHT_PATH), main] + [str(a) for a in args]
    if run(cmd, log, cwd, clean_env(), timeout) != 0:
        raise ToolError("%s failed: %s (log %s)" % (main.split(".")[-1], tail(log, r"RAPIDWRIGHT FAILED|Error|Exception"), log))


def jython(step_name, args, log, cwd, timeout=None):
    java("org.python.util.jython", [os.path.join(SCRIPTS, "rapidwright.py"), step_name] + list(args), log, cwd, timeout)


def tcl_value(value):
    if isinstance(value, (list, tuple)):
        return "{%s}" % " ".join(tcl_value(v) for v in value)
    return "{%s}" % value


def vivado(cfg, command, params, work_dir):
    """Runs one flow.tcl command in its own Vivado process and returns its result lines."""
    os.makedirs(work_dir, exist_ok=True)
    params = dict(params, part=PART, top=cfg["top"], period=cfg["period"], generics=cfg["generics"],
                  clk_src=CLK_SRC, work_dir=work_dir)
    params_path = os.path.join(work_dir, command + "_params.tcl")
    result_path = os.path.join(work_dir, command + "_result.txt")
    log = os.path.join(work_dir, command + "_vivado.log")
    with open(params_path, "w") as f:
        for key, value in params.items():
            f.write("%s %s\n" % (key, tcl_value(value)))
    if os.path.exists(result_path):
        os.remove(result_path)
    with open(log + ".out", "w") as out:
        proc = subprocess.Popen(["vivado", "-mode", "batch", "-nojournal", "-log", log, "-source", os.path.join(SCRIPTS, "flow.tcl"),
                                 "-tclargs", command, params_path, result_path], stdout=out, stderr=subprocess.STDOUT,
                                cwd=work_dir, env=vivado_env())
        # Vivado takes about 10 s to exit after it writes its result, so the flow goes on without it.
        while proc.poll() is None and not os.path.exists(result_path):
            time.sleep(0.5)
    code = proc.returncode or 0
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


# ---- 1. Synthesis -----------------------------------------------------------------------------

def find_unit(sources, top, unit=None):
    """The repeated module: the one the top instantiates. Returns it and the black-box sources."""
    text = dict((s, open(s).read()) for s in sources)
    modules = {}
    for path, src in text.items():
        for m in re.finditer(r"\bmodule\s+(\w+)", src):
            modules[m.group(1)] = (path, m.start())
    if top not in modules:
        raise ToolError("no module %s in the sources (only Verilog sources are supported)" % top)
    path, start = modules[top]
    body = text[path][start:text[path].index("endmodule", start)]
    used = sorted(set(m.group(1) for m in re.finditer(r"^\s*(\w+)\s*(?:#\s*\(|\w+\s*\()", body, re.M)
                      if m.group(1) in modules and m.group(1) != top))
    if unit is None:
        if len(used) != 1:
            raise ToolError("the top instantiates %s; name the repeated one with --unit" % (", ".join(used) or "no module"))
        unit = used[0]
    # The black box keeps the unit's ANSI header, which ends at the first ");", and drops its body.
    path, start = modules[unit]
    src = text[path]
    header_end = src.index(");", start) + 2
    stub = src[:start] + "(* black_box *) " + src[start:header_end] + "\n" + src[src.index("endmodule", start):]
    return unit, path, stub


def synthesize(cfg, run_dir, unit_name=None):
    work = os.path.join(run_dir, "synth")
    os.makedirs(work, exist_ok=True)
    unit, unit_file, stub = find_unit(cfg["sources"], cfg["top"], unit_name)
    stub_path = os.path.join(work, "blackbox_" + os.path.basename(unit_file))
    with open(stub_path, "w") as f:
        f.write(stub)
    top_sources = [stub_path if s == unit_file else s for s in cfg["sources"]]
    with ThreadPoolExecutor(3) as pool:
        unit_job = pool.submit(vivado, cfg, "synth_unit", {"unit": unit, "sources": cfg["sources"]}, os.path.join(work, "unit"))
        top_job = pool.submit(vivado, cfg, "synth_top", {"sources": top_sources}, os.path.join(work, "top"))
        fabric_job = pool.submit(fabric_text, run_dir)
        unit_result, _, fabric = unit_job.result(), top_job.result(), fabric_job.result()
    with open(os.path.join(work, "top", "units.txt")) as f:
        regex, rows, cols = infer_grid([line.strip() for line in f if line.strip()])
    if regex is None:
        raise ToolError("instances of %s do not form a row/column grid" % unit)
    # Wires from the first unit to its neighbors, by direction.
    wires = {}
    with open(os.path.join(work, "top", "neighbors.txt")) as f:
        lines = [line.split() for line in f if line.strip()]
    with open(os.path.join(work, "top", "units.txt")) as f:
        r0, c0 = [int(v) for v in re.fullmatch(regex, f.readline().strip()).groups()]
    for name, count in lines:
        r, c = [int(v) for v in re.fullmatch(regex, name).groups()]
        direction = {(0, 1): "next column", (1, 0): "next row"}.get((r - r0, c - c0), "other")
        wires[direction] = wires.get(direction, 0) + int(count)
    counts = unit_result["counts"].split()
    design = {"unit_cell": unit, "unit_regex": regex, "unit_rows": rows, "unit_cols": cols, "wires": wires,
              "unit": dict(zip(counts[::2], [int(v) for v in counts[1::2]])),
              "unit_edf": os.path.join(work, "unit", "unit.edf"), "top_edf": os.path.join(work, "top", "top.edf")}
    save(os.path.join(run_dir, "design.json"), design)
    return design, fabric


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


def fabric_text(run_dir):
    """The device for Claude: every clock region, and the site columns of one full region."""
    def fabric(region=None):
        log = os.path.join(run_dir, "fabric_%s.log" % (region or "all"))
        jython("fabric", [PART] + ([region] if region else []), log, run_dir)
        with open(log) as f:
            return "\n".join(l for l in f.read().splitlines() if l.startswith(("  ", "Clock", "Site"))) + "\n"
    regions = fabric()
    # Regions with the same counts repeat the same columns, so one full region shows the layout.
    full = [l.split() for l in regions.splitlines() if re.match(r"\s+X\d+Y\d+\s+SLR", l)]
    typical = max(full, key=lambda f: (int(f[5]), int(f[4])))[0]
    return regions + "\n" + fabric(typical)


# ---- 2. Claude's choice and the check ---------------------------------------------------------

def ask_claude(cfg, d, fabric, work, failures, attempt):
    """Asks Claude once for up to 3 ranked blocks. Returns (specs, answer, cost)."""
    with open(os.path.join(SCRIPTS, "agent_instructions.md")) as f:
        prompt = f.read()
    prompt += ("\n## DESIGN\n\n```\nunit %s, %d rows x %d columns of units\none unit uses: %s\n"
               "wires from a unit to its neighbor: %s\nclock period %.3f ns\n```\n") % (
        d["unit_cell"], d["unit_rows"], d["unit_cols"], " ".join("%s %d" % kv for kv in sorted(d["unit"].items())),
        ", ".join("%d to the %s" % (n, k) for k, n in sorted(d["wires"].items())), cfg["period"])
    prompt += "\n## FABRIC\n\n```\n%s```\n" % fabric
    if failures:
        prompt += "\n## Earlier answers that failed the check\n\n" + "".join(
            "- `%s`: %s\n" % (spec, "; ".join(problems)) for spec, problems in failures)
    tag = "try%d" % attempt
    with open(os.path.join(work, tag + "_prompt.md"), "w") as f:
        f.write(prompt)
    # No tools and no setting sources: the prompt is Claude's only input.
    cmd = [CLAUDE_BIN, "-p", "--model", CLAUDE_MODEL, "--effort", CLAUDE_EFFORT, "--tools", "", "--setting-sources", "",
           "--no-session-persistence", "--output-format", "json", "--max-budget-usd", "5"]
    with open(os.path.join(work, tag + "_prompt.md")) as stdin, open(os.path.join(work, tag + "_output.json"), "w") as out, \
            open(os.path.join(work, tag + "_stderr.txt"), "w") as err:
        code = subprocess.call(cmd, stdin=stdin, stdout=out, stderr=err, cwd=work,
                               env=clean_env({"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}))
    reply = load(os.path.join(work, tag + "_output.json")) or {}
    text = reply.get("result", "")
    with open(os.path.join(work, tag + "_answer.md"), "w") as f:
        f.write(text + "\n")
    found = re.findall(r"^SELECTION\s+(\d+x\d+\s+SLICE_X\d+Y\d+:SLICE_X\d+Y\d+)\s*$", text, re.M)
    if code != 0 or not found:
        raise ToolError("Claude gave no SELECTION line (exit %d, see %s)" % (code, work))
    return found[:3], text, reply.get("total_cost_usd") or 0.0


def parse_spec(text):
    found = re.fullmatch(r"\s*(\d+)x(\d+)\s+(SLICE_X(\d+)Y(\d+):SLICE_X(\d+)Y(\d+))\s*", text)
    if not found:
        raise ToolError("a block is '<rows>x<cols> SLICE_X<a>Y<b>:SLICE_X<c>Y<d>', not '%s'" % text)
    g = found.groups()
    return {"tr": int(g[0]), "tc": int(g[1]), "rect": g[2], "xy": [int(v) for v in g[3:]]}


def check(d, blocks, work):
    """Geometry, resources and the copy grid of each block, from the device itself, in one JVM."""
    jobs = []
    for i, block in enumerate(blocks):
        rows, cols = d["unit_rows"] // block["tr"], d["unit_cols"] // block["tc"]
        if cols > rows:  # The longer side of the copy grid runs up the device (rapidwright.py group).
            rows, cols = cols, rows
        jobs.append({"id": str(i), "part": PART, "rect": block["rect"], "rows": rows, "cols": cols})
    save(os.path.join(work, "check_in.json"), jobs)
    jython("check", [os.path.join(work, "check_in.json"), os.path.join(work, "check_out.json")], os.path.join(work, "check.log"), work)
    geometry = load(os.path.join(work, "check_out.json"))
    return [judge(d, block, job, geometry[job["id"]]) for block, job in zip(blocks, jobs)]


def judge(d, block, job, g):
    """The problems that rule a block out, from its geometry, and what the report shows of it."""
    rows, cols = job["rows"], job["cols"]
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
    elif not problems and max(g["grid"]["row_gaps"] + g["grid"]["col_gaps"]) > MAX_GAP:
        problems.append("neighboring copies would sit up to %d slices apart (gaps between rows %s, between columns %s); "
                        "RWRoute cannot route SLR crossings across gaps over %d" % (
                            max(g["grid"]["row_gaps"] + g["grid"]["col_gaps"]), g["grid"]["row_gaps"], g["grid"]["col_gaps"], MAX_GAP))
    hard = " ".join(r for k, r in sorted(g["ranges"].items()) if use[k] > 0)
    return {"spec": "%dx%d %s" % (block["tr"], block["tc"], block["rect"]), "problems": problems,
            "copies": (d["unit_rows"] // block["tr"]) * (d["unit_cols"] // block["tc"]),
            "util": dict((k, round(use[k] / float(cap[k]), 3)) for k in use if cap[k]),
            "positions": g["positions"], "pblock": (block["rect"] + " " + hard).strip()}


# Where a candidate's exact tile pattern allows no copy grid, a shift of a few slices often does.
# Shifts are tried nearest first, rows in steps of two to keep the DSP pairing.
SHIFTS = sorted(((dx, dy) for dy in range(-8, 9, 2) for dx in range(-4, 5) if (dx, dy) != (0, 0)),
                key=lambda s: abs(s[0]) + abs(s[1]))


def shifted(spec, dx, dy):
    b = parse_spec(spec)
    x0, y0, x1, y1 = b["xy"]
    if min(x0 + dx, y0 + dy) < 0:
        return None
    return "%dx%d SLICE_X%dY%d:SLICE_X%dY%d" % (b["tr"], b["tc"], x0 + dx, y0 + dy, x1 + dx, y1 + dy)


def choose_block(cfg, d, fabric, run_dir, given):
    """The first of Claude's ranked candidates that passes the check, else the first that passes
    after the smallest shift, else Claude is asked again with the problems. --block skips Claude."""
    work = os.path.join(run_dir, "claude")
    os.makedirs(work, exist_ok=True)
    failures, answers, cost = [], [], 0.0
    for attempt in range(1, (1 if given else CLAUDE_TRIES) + 1):
        if given:
            specs = [given]
        else:
            specs, answer, spent = ask_claude(cfg, d, fabric, work, failures, attempt)
            answers.append(answer)
            cost += spent
        results = check(d, [parse_spec(s) for s in specs], work)
        for spec, checked in zip(specs, results):
            print("Candidate %s: %s" % (spec, "; ".join(checked["problems"]) or "passes"))
            if not checked["problems"]:
                return checked, answers, cost, failures
        variants = [(spec, v) for spec in specs for v in (shifted(spec, dx, dy) for dx, dy in SHIFTS) if v]
        for (spec, v), checked in zip(variants, check(d, [parse_spec(v) for _, v in variants], work)):
            if not checked["problems"]:
                print("Moved %s to %s, the nearest position that passes" % (spec, v))
                checked["moved_from"] = spec
                return checked, answers, cost, failures
        failures += [(spec, checked["problems"]) for spec, checked in zip(specs, results)]
    raise ToolError("no block passed the check: " + " / ".join("%s: %s" % (s, "; ".join(p)) for s, p in failures))


# ---- 3-5. Group, block, array -----------------------------------------------------------------

def start_block_vivado(cfg, work):
    """Starts the block's Vivado run early, so the device has loaded by the time the block
    exists. It waits for the file "ready", which carries the PBlock ranges, or "abort"."""
    os.makedirs(work, exist_ok=True)
    p = lambda name: os.path.join(work, name)
    pool = ThreadPoolExecutor(1)
    job = pool.submit(vivado, cfg, "impl", {"harness_dcp": p("block_flops.dcp"), "info": p("info.txt"),
                                            "out_dcp": p("block_routed_flops.dcp")}, work)
    pool.shutdown(wait=False)
    return job


def build(cfg, d, checked, work, final_dcp, times, block_vivado):
    block = parse_spec(checked["spec"])
    p = lambda name: os.path.join(work, name)
    ranges = checked["pblock"]
    t0 = time.time()
    step("Grouping the units into %dx%d blocks" % (block["tr"], block["tc"]))
    jython("group", [d["top_edf"], d["unit_edf"], p("block_synth.dcp"), p("top_blackbox.dcp"), p("info.txt"),
                     p("side_map.txt"), d["unit_cell"], d["unit_regex"], block["tr"], block["tc"], cfg["period"]],
           p("group.log"), work)
    with open(p("info.txt")) as f:
        clock = dict(line.split() for line in f if line.strip())["CLOCK"]
    times["group"] = time.time() - t0
    t0 = time.time()
    step("Placing and routing one block in %s" % ranges)
    jython("harness", ["add", p("block_synth.dcp"), p("block_flops.dcp"), clock, ranges, p("side_map.txt")],
           p("harness_add.log"), work)
    with open(p("ready.tmp"), "w") as f:
        f.write(ranges)
    os.rename(p("ready.tmp"), p("ready"))
    block_result = numbers(block_vivado.result())
    times["block"] = time.time() - t0
    print("Block alone: WNS %s ns (%s MHz)" % (block_result.get("wns"), block_result.get("fmax")))

    t0 = time.time()
    step("Stamping %d copies and routing the array (ArrayBuilder, RWRoute)" % checked["copies"])
    jython("array", [p("block_routed_flops.dcp"), p("block_routed.dcp"), clock, ranges, p("top_blackbox.dcp"), p("side_map.txt"),
                     p("placement.txt"), p("copy_pblocks.txt"), p("grid.json"), final_dcp, cfg["period"]],
           p("array.log"), work, ARRAYBUILDER_LIMIT_S)
    times["array"] = time.time() - t0
    with open(p("array.log"), errors="replace") as f:
        abandoned = sum("Unroutable connection" in line for line in f)
    if abandoned:
        print("RWRoute abandoned %d connections" % abandoned)
    return {"clock": clock, "block": block_result, "grid": load(p("grid.json")), "abandoned": abandoned}


# ---- 6-7. Measurements outside the compile time -----------------------------------------------

def classify(paths_tsv):
    """Counts the worst paths by where they run: inside a block, or between blocks in one or two SLRs."""
    counts = {}
    if os.path.exists(paths_tsv):
        with open(paths_tsv) as f:
            for line in f:
                fields = line.rstrip("\n").split("\t")
                if len(fields) < 10:
                    continue
                slack = float(fields[0])
                a, b = [re.match(r"(block_\d+_\d+)/", pin) for pin in fields[4:6]]
                if a is None or b is None:
                    kind = "top level"
                elif a.group(1) == b.group(1):
                    kind = "inside a block"
                else:
                    kind = "between blocks, %s" % ("across SLRs" if fields[8] != fields[9] else "same SLR")
                c = counts.setdefault(kind, {"paths": 0, "failing": 0, "worst": slack, "datapath_ns": float(fields[2])})
                c["paths"] += 1
                c["failing"] += slack < 0
                if slack < c["worst"]:
                    c["worst"], c["datapath_ns"] = slack, float(fields[2])
    return counts


def flat_baseline(cfg, run_dir, result_dir, source):
    if source == "none":
        return {"source": "not run"}
    if source != "fresh":
        flat = load(os.path.join(source if os.path.isabs(source) else os.path.join(ROOT, source), "flat.json"))
        if flat is None:
            raise ToolError("no flat.json in %s" % source)
        return dict(flat, source="reused from %s" % source)
    flat = numbers(vivado(cfg, "flat", {"flat_dcp": os.path.join(result_dir, "flat.dcp"), "sources": cfg["sources"]},
                          os.path.join(run_dir, "flat")))
    return dict(flat, minutes=round(flat["seconds"] / 60.0, 1), source="this run")


# ---- Report -----------------------------------------------------------------------------------

def show(v):
    if v is None:
        return "n/a"
    if isinstance(v, float) and v.is_integer() and abs(v) >= 1:
        return str(int(v))
    return str(v)


def write_report(path, cfg, r, flat):
    t = r.get("timing", {})
    lines = ["# %s %s, %s ns clock" % (cfg["top"], " ".join(cfg["generics"]), cfg["period"]), ""]
    if r.get("spec"):
        moved = r["check"].get("moved_from")
        lines.append("Block `%s`, chosen by %s%s." % (r["spec"], r["chosen_by"], ", moved from `%s` to the nearest position that passes the check" % moved if moved else ""))
    if r.get("error"):
        lines.append("Pre-implemented flow FAILED: %s" % r["error"])
    lines += ["Flat baseline: %s." % flat.get("source"), "",
              "| | Flat Vivado | Pre-implemented |", "|---|---|---|",
              "| Compile time (min) | %s | %s |" % (show(flat.get("minutes")), show(r.get("minutes"))),
              "| Fmax (MHz) | %s | %s |" % (show(flat.get("fmax")), show(t.get("fmax"))),
              "| Setup WNS (ns) | %s | %s |" % (show(flat.get("setup")), show(t.get("setup"))),
              "| Hold WNS (ns) | %s | %s |" % (show(flat.get("hold")), show(t.get("hold"))),
              "| Route errors | %s | %s |" % (show(flat.get("errors")), show(t.get("errors"))), ""]
    if r.get("times"):
        lines.append("Compile time, RTL to the routed checkpoint (target %d min, limit %d): %s." % (TARGET_MIN, LIMIT_MIN, ", ".join(
            "%s %.1f" % (k, v / 60.0) for k, v in r["times"].items())))
        lines.append("Vivado then took %.1f min to open and time the result; that is not part of the compile time." % (t.get("vivado_seconds", 0) / 60.0))
    c = r.get("check")
    if c:
        lines += ["", "Check: %d copies, %d legal positions, use %s." % (
            c["copies"], c["positions"], " ".join("%s %.0f%%" % (k, 100 * v) for k, v in sorted(c["util"].items()) if v))]
    b = r.get("build", {}).get("block")
    if b:
        lines.append("Block alone: WNS %s ns (%s MHz), through port flops in %s / out %s ns." % (
            b.get("wns"), b.get("fmax"), b.get("port_in_wns"), b.get("port_out_wns")))
        lines.append("RWRoute abandoned %d connections." % r["build"]["abandoned"])
    if r.get("paths"):
        lines += ["", "## Worst paths", ""] + ["- %s: %d paths under 0.1 ns slack, %d failing, worst %.3f ns (datapath %.3f ns)" % (
            kind, c["paths"], c["failing"], c["worst"], c["datapath_ns"]) for kind, c in sorted(r["paths"].items())]
    if r.get("answers"):
        lines += ["", "## Claude", "", "%d answer(s), $%.2f." % (len(r["answers"]), r["cost_usd"])]
        for spec, problems in r.get("failures", []):
            lines.append("- `%s` failed the check: %s" % (spec, "; ".join(problems)))
        lines += ["", "Final answer:", "", r["answers"][-1].strip()]
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines))


def next_run(results):
    taken = [int(m.group(1)) for m in (re.fullmatch(r"run(\d+)", n) for n in os.listdir(results)) if m]
    return "run%d" % (max(taken + [0]) + 1)


def main():
    sys.stdout = open(sys.stdout.fileno(), "w", buffering=1, closefd=False)  # line-buffered, for logs
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", required=True)
    ap.add_argument("--period", required=True, type=float)
    ap.add_argument("--generic", action="append", default=[])
    ap.add_argument("--block", help='"<rows>x<cols> SLICE_X..Y..:SLICE_X..Y.." to skip Claude')
    ap.add_argument("--unit", help="the repeated module, if the top instantiates several")
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
    final_dcp = os.path.join(result_dir, "final.dcp")
    print("Run %s: %s" % (name, run_dir))

    r = {"chosen_by": "hand (--block)" if a.block else "Claude (%s)" % CLAUDE_MODEL, "times": {}}
    start = time.time()
    block_dir = os.path.join(run_dir, "block")
    try:
        step("Synthesizing the unit and the top in parallel")
        t0 = time.time()
        d, fabric = synthesize(cfg, run_dir, a.unit)
        r["times"]["synthesis"] = time.time() - t0
        # Started after synthesis: three Vivado processes loading the device at once slowed
        # synthesis by minutes (run48), and Claude's answer still hides this one's load.
        block_vivado = start_block_vivado(cfg, block_dir)
        print("Unit %s on a %dx%d grid: %s" % (d["unit_cell"], d["unit_rows"], d["unit_cols"], d["unit"]))
        step("Choosing the block" + ("" if a.block else " (Claude)"))
        t0 = time.time()
        r["check"], r["answers"], r["cost_usd"], r["failures"] = choose_block(cfg, d, fabric, run_dir, a.block)
        r["spec"] = r["check"]["spec"]
        r["times"]["choice"] = time.time() - t0
        r["build"] = build(cfg, d, r["check"], block_dir, final_dcp, r["times"], block_vivado)
        r["minutes"] = round((time.time() - start) / 60.0, 1)
        r["times"]["total"] = time.time() - start
        step("Compiled in %.1f min: %s" % (r["minutes"], final_dcp))
        step("Timing the result in Vivado (not part of the compile time)")
        r["timing"] = numbers(vivado(cfg, "timing", {"array_dcp": final_dcp, "clock": r["build"]["clock"]}, os.path.join(run_dir, "timing")))
        r["paths"] = classify(os.path.join(run_dir, "timing", "paths.tsv"))
    except ToolError as e:
        r["error"] = str(e)
        print("PRE-IMPLEMENTED FLOW FAILED: %s" % e)
        if os.path.isdir(block_dir) and not os.path.exists(os.path.join(block_dir, "ready")):
            open(os.path.join(block_dir, "abort"), "w").close()
    save(os.path.join(run_dir, "run.json"), r)

    if a.flat == "fresh":
        step("Flat Vivado baseline")
    try:
        flat = flat_baseline(cfg, run_dir, result_dir, a.flat)
    except ToolError as e:
        flat = {"source": "FAILED: %s" % e}
    save(os.path.join(result_dir, "flat.json"), flat)

    report = os.path.join(result_dir, "report.md")
    write_report(report, cfg, r, flat)
    step("Done: %s" % report)
    if r.get("timing", {}).get("errors") != 0 or r.get("minutes", LIMIT_MIN + 1) > LIMIT_MIN:
        print("RUN FAILED: see %s" % report)
        return 1
    print("RUN PASSED: routed in %.1f min, Fmax %s MHz (flat %s)" % (r["minutes"], r["timing"].get("fmax"), flat.get("fmax")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
