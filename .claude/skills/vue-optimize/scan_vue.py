#!/usr/bin/env python3
"""
scan_vue.py — heuristic structure/optimization scanner for Vue 3 SFCs.

Stdlib only (no deps), so it runs anywhere with `python3` or `uv run python`.
It does NOT parse Vue properly — it uses line/regex heuristics tuned to THIS
codebase's conventions (Options API `setup()` in views, `<script setup>` in
modals, module-level composables `useFilters` / `useI18n`). Treat the output as
leads for a human/Claude to confirm, not as ground truth.

Two layers of findings:
  1. Per-file metrics + flags (API style, reactivity counts, v-for key safety,
     expensive computeds, inline template work, unvirtualized tables).
  2. Cross-file duplication (same helper defined in N files) -> reuse candidates.

Usage:
    python3 scan_vue.py [ROOT] [--json] [--min-dup 2]

    ROOT        directory to scan (default: client/src)
    --json      emit machine-readable JSON instead of the text report
    --min-dup   minimum files a helper must appear in to be reported (default 2)

Exit code is always 0 — this is an advisory tool, not a gate.
"""

import argparse
import json
import os
import re
import sys
from collections import defaultdict

# --- block extraction --------------------------------------------------------

BLOCK_RE = {
    "template": re.compile(r"<template[^>]*>(.*?)</template>", re.S | re.I),
    "script": re.compile(r"<script[^>]*>(.*?)</script>", re.S | re.I),
    "style": re.compile(r"<style[^>]*>(.*?)</style>", re.S | re.I),
}
SCRIPT_OPEN_RE = re.compile(r"<script([^>]*)>", re.I)

# Named helpers we know get copy-pasted across views in this repo. If a helper
# with one of these names is *defined* (const NAME = / function NAME) in 2+
# files, it's a strong extract-to-composable candidate. Keep this list aligned
# with SKILL.md's "known duplication" section.
KNOWN_HELPERS = [
    "currencySymbol",
    "formatDate",
    "formatDateShort",
    "translateCategory",
    "translatePriority",
    "translateStockLevel",
    "translateStatus",
    "getStockStatus",
    "getStockStatusKey",
    "getStockBadge",
    "calculatePercentage",
]

# Iteration helpers that make a computed "expensive" (worth reviewing for
# memoization / moving work server-side). Presence inside a computed body is a
# heuristic, not a verdict.
LOOP_TOKENS = (".map(", ".filter(", ".reduce(", ".forEach(", ".sort(", " for ", "for(")


def split_blocks(text):
    out = {}
    for name, rgx in BLOCK_RE.items():
        m = rgx.search(text)
        out[name] = m.group(1) if m else ""
    m = SCRIPT_OPEN_RE.search(text)
    out["_script_attrs"] = m.group(1) if m else ""
    return out


# --- per-file analysis -------------------------------------------------------

def detect_api_style(blocks):
    if "setup" in blocks["_script_attrs"]:
        return "script setup"
    if re.search(r"\bsetup\s*\(", blocks["script"]):
        return "options + setup()"
    if re.search(r"export default\s*\{", blocks["script"]):
        return "options"
    return "unknown"


def find_vfor_key_issues(template):
    """Return list of dicts for v-for usages whose :key looks unsafe/missing."""
    issues = []
    # Match an element span that carries a v-for; we scan tag-by-tag.
    for tag in re.finditer(r"<[a-zA-Z][^>]*\bv-for\s*=\s*\"([^\"]*)\"[^>]*>", template):
        whole = tag.group(0)
        expr = tag.group(1)
        # Extract the index var if present: "(item, idx) in list"
        idx_var = None
        m = re.search(r"\(\s*[\w$]+\s*,\s*([\w$]+)\s*\)", expr)
        if m:
            idx_var = m.group(1)
        key_m = re.search(r":key\s*=\s*\"([^\"]*)\"", whole)
        line = template[: tag.start()].count("\n") + 1
        if not key_m:
            issues.append({"line": line, "vfor": expr.strip(), "key": None, "why": "missing :key"})
        else:
            key = key_m.group(1).strip()
            bad = key in ("index", "i", "idx") or (idx_var and key == idx_var)
            if bad:
                issues.append({"line": line, "vfor": expr.strip(), "key": key,
                               "why": "key is the loop index (breaks list diffing on reorder/insert)"})
    return issues


def find_expensive_computeds(script):
    """Heuristic: computed(() => { ... }) bodies that contain loop tokens."""
    flagged = []
    for m in re.finditer(r"(?:const|let)\s+([\w$]+)\s*=\s*computed\s*\(", script):
        name = m.group(1)
        # Walk braces/parens from the computed( to find a rough body end.
        start = m.end()
        depth = 1
        i = start
        while i < len(script) and depth > 0:
            c = script[i]
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
            i += 1
        body = script[start:i]
        hits = [t.strip() for t in LOOP_TOKENS if t in body]
        if hits:
            line = script[:m.start()].count("\n") + 1
            flagged.append({"name": name, "line": line,
                            "ops": sorted(set(h for h in hits))})
    return flagged


def find_inline_template_work(template):
    """Inline arrow handlers and inline numeric/format work in bindings."""
    inline_handlers = len(re.findall(r"@[\w.:-]+\s*=\s*\"[^\"]*=>", template))
    # .toLocaleString() / arithmetic inside :style / :title / {{ }} bindings.
    inline_calc = len(re.findall(r"\.toLocaleString\(", template))
    inline_math = len(re.findall(r"\{\{[^}]*[-+*/][^}]*\}\}", template))
    return {"inline_arrow_handlers": inline_handlers,
            "inline_tolocalestring": inline_calc,
            "inline_arithmetic_interp": inline_math}


def find_tables(template):
    tables = len(re.findall(r"<table\b", template, re.I))
    row_loops = len(re.findall(r"<tr[^>]*\bv-for", template, re.I))
    return {"tables": tables, "row_vfor": row_loops}


def has_loading_boilerplate(script):
    has_loading = bool(re.search(r"\bloading\s*=\s*ref\(", script))
    has_error = bool(re.search(r"\berror\s*=\s*ref\(", script))
    has_trycatch = "try {" in script and "finally" in script
    return has_loading and has_error and has_trycatch


def defined_helpers(script):
    """Names from KNOWN_HELPERS that are *defined* in this file."""
    found = set()
    for name in KNOWN_HELPERS:
        if re.search(rf"(?:const|let|function)\s+{re.escape(name)}\b", script):
            found.add(name)
    return found


def count(pattern, text):
    return len(re.findall(pattern, text))


def analyze_file(path, root):
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    blocks = split_blocks(text)
    script = blocks["script"]
    template = blocks["template"]
    rel = os.path.relpath(path, root)

    return {
        "file": rel,
        "loc": text.count("\n") + 1,
        "api_style": detect_api_style(blocks),
        "reactivity": {
            "ref": count(r"\bref\(", script),
            "computed": count(r"\bcomputed\(", script),
            "reactive": count(r"\breactive\(", script),
            "watch": count(r"\bwatch\(", script),
            "watchEffect": count(r"\bwatchEffect\(", script),
        },
        "vfor_key_issues": find_vfor_key_issues(template),
        "expensive_computeds": find_expensive_computeds(script),
        "inline_template_work": find_inline_template_work(template),
        "tables": find_tables(template),
        "loading_boilerplate": has_loading_boilerplate(script),
        "defines_helpers": sorted(defined_helpers(script)),
        "has_debounce": bool(re.search(r"debounce|watchDebounced", script)),
    }


# --- reporting ---------------------------------------------------------------

def gather(root):
    files = []
    for dirpath, _dirs, names in os.walk(root):
        if "node_modules" in dirpath:
            continue
        for n in names:
            if n.endswith(".vue"):
                files.append(os.path.join(dirpath, n))
    return sorted(files)


def build_report(root, min_dup):
    files = gather(root)
    results = [analyze_file(p, root) for p in files]

    # Cross-file duplication: helper name -> files that define it.
    dup = defaultdict(list)
    for r in results:
        for h in r["defines_helpers"]:
            dup[h].append(r["file"])
    reuse = {h: fs for h, fs in dup.items() if len(fs) >= min_dup}

    return {"root": root, "file_count": len(results), "files": results,
            "reuse_candidates": reuse}


def text_report(rep):
    out = []
    w = out.append
    w(f"Vue optimization scan — {rep['file_count']} .vue files under {rep['root']}")
    w("=" * 72)

    # --- cross-file reuse first: it's the highest-leverage section ---
    w("\n## Cross-file reuse candidates (same helper defined in multiple files)")
    if not rep["reuse_candidates"]:
        w("  (none detected above the threshold)")
    else:
        for helper, fs in sorted(rep["reuse_candidates"].items(),
                                 key=lambda kv: -len(kv[1])):
            w(f"  • {helper}  — defined in {len(fs)} files:")
            for f in fs:
                w(f"      {f}")
            w(f"    → extract to a composable/util and import it once.")

    # --- per-file flags ---
    w("\n## Per-file findings")
    for r in rep["files"]:
        flags = []
        if r["vfor_key_issues"]:
            flags.append(f"{len(r['vfor_key_issues'])} v-for key issue(s)")
        if r["expensive_computeds"]:
            flags.append(f"{len(r['expensive_computeds'])} loop-heavy computed(s)")
        if r["tables"]["row_vfor"]:
            flags.append(f"{r['tables']['row_vfor']} table row-loop(s) (no virtualization)")
        itw = r["inline_template_work"]
        if itw["inline_arrow_handlers"]:
            flags.append(f"{itw['inline_arrow_handlers']} inline arrow handler(s)")
        if r["loading_boilerplate"]:
            flags.append("loading/error/try-finally boilerplate")

        head = f"\n  {r['file']}  [{r['api_style']}, {r['loc']} loc, " \
               f"{r['reactivity']['ref']} ref / {r['reactivity']['computed']} computed]"
        w(head)
        if not flags:
            w("    ✓ no flags")
            continue
        for f in flags:
            w(f"    - {f}")
        for issue in r["vfor_key_issues"]:
            w(f"      · v-for line {issue['line']}: {issue['why']}"
              + (f" (key=\"{issue['key']}\")" if issue["key"] else ""))
        for ec in r["expensive_computeds"]:
            w(f"      · computed '{ec['name']}' (line {ec['line']}): {', '.join(ec['ops'])}")

    w("\n" + "=" * 72)
    w("Heuristic output — confirm each finding in context before acting.")
    w("See SKILL.md for how to turn these flags into concrete refactors.")
    return "\n".join(out)


def main(argv):
    ap = argparse.ArgumentParser(description="Heuristic Vue 3 SFC optimization scanner.")
    ap.add_argument("root", nargs="?", default="client/src", help="directory to scan")
    ap.add_argument("--json", action="store_true", help="emit JSON")
    ap.add_argument("--min-dup", type=int, default=2,
                    help="min files a helper must appear in to flag as reuse candidate")
    args = ap.parse_args(argv)

    if not os.path.isdir(args.root):
        print(f"error: {args.root} is not a directory", file=sys.stderr)
        return 0  # advisory tool — never hard-fail callers

    rep = build_report(args.root, args.min_dup)
    if args.json:
        print(json.dumps(rep, indent=2))
    else:
        print(text_report(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
