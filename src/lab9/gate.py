"""CI gate: compare a PR eval report with the main-branch baseline using the metric spec.

    python -m lab9.gate --pr reports/eval_workflow.json --baseline reports/baseline_v1.json

exit 0 = merge, exit 1 = blocked. Safety metrics: max 0, no averaging away.
Capability metrics: meet the floor AND the paired CI of (PR - main) is not entirely below 0.
Thresholds may only get stricter on the same eval-set version (ratchet).
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from lab8.console import utf8_stdio
from lab9 import stats
from lab9.eval import SPEC, load_spec, per_item


def integrity_check(pr: dict, base: dict, spec: dict, base_spec: dict | None = None) -> list[str]:
    """M7 (Expert): refuse to compare reports that cannot be trusted. Return a list of problems
    (empty = trustworthy). A gate that passes a broken report is worse than no gate.
    Must catch at least:
      - any metric value in pr["metrics"] that is NaN or inf
      - len(pr["rows"]) != manifest items x len(manifest seeds)   (dropped / duplicated rows)
      - the (item_id, seed) set of PR differs from the baseline's (cherry-picked seeds/items)
      - pr["evaluator_failures"] non-empty, or a row whose value for a graded spec metric is
        None (a crashed grader silently scores as "missing" and drops out of the mean)
      - base_spec given and a threshold in spec is LOOSER than in base_spec for the same
        eval-set version (ratchet: max may not rise, min may not fall, a blocking metric may
        not become non-blocking, a metric may not disappear; the same for spec["slices"],
        including dropping no_significant_drop)
      - an item's slice tags in PR rows differ from the baseline's (relabelling an item out of a
        slice hides its regression from the slice gate)"""
    problems: list[str] = []
    pr_rows = pr.get("rows") or []
    base_rows = base.get("rows") or []
    manifest = pr.get("manifest") or {}
    items, seeds = manifest.get("items"), manifest.get("seeds")
    if not isinstance(items, int) or not isinstance(seeds, list):
        problems.append("invalid PR manifest item/seed counts")
    elif len(pr_rows) != items * len(seeds):
        problems.append(f"row count {len(pr_rows)} != manifest items×seeds {items * len(seeds)}")

    def row_keys(rows: list[dict]) -> list[tuple]:
        return [(row.get("item_id"), row.get("seed")) for row in rows if isinstance(row, dict)]

    pr_keys, base_keys = row_keys(pr_rows), row_keys(base_rows)
    if len(pr_keys) != len(set(pr_keys)):
        problems.append("duplicate (item_id, seed) rows in PR")
    if set(pr_keys) != set(base_keys):
        problems.append("PR (item_id, seed) set differs from baseline")

    if pr.get("evaluator_failures"):
        problems.append(f"evaluator_failures is non-empty ({len(pr['evaluator_failures'])})")
    pr_metrics = pr.get("metrics") or {}
    for name, value in pr_metrics.items():
        if isinstance(value, (int, float)) and not math.isfinite(value):
            problems.append(f"metric {name} is not finite")

    from lab9.graders import GRADERS
    graded = [m["id"] for m in spec.get("metrics", []) if m.get("id") in GRADERS]
    for index, row in enumerate(pr_rows):
        if not isinstance(row, dict):
            problems.append(f"row {index} is not an object")
            continue
        for metric in graded:
            if row.get(metric) is None:
                problems.append(f"row {row.get('item_id')}@{row.get('seed')} has missing {metric}")
    pr_tags = {row.get("item_id"): tuple(sorted(row.get("slices") or []))
               for row in pr_rows if isinstance(row, dict)}
    base_tags = {row.get("item_id"): tuple(sorted(row.get("slices") or []))
                 for row in base_rows if isinstance(row, dict)}
    if pr_tags != base_tags:
        problems.append("PR slice tags differ from baseline")

    if base_spec is not None and spec.get("eval_set_version") == base_spec.get("eval_set_version"):
        current = {m.get("id"): m for m in spec.get("metrics", [])}
        original = {m.get("id"): m for m in base_spec.get("metrics", [])}
        for mid, old in original.items():
            new = current.get(mid)
            if new is None:
                problems.append(f"ratchet: metric {mid} was removed")
                continue
            old_th, new_th = old.get("threshold") or {}, new.get("threshold") or {}
            for op, weakens in (("min", lambda n, o: n < o), ("max", lambda n, o: n > o)):
                if op in old_th and (op not in new_th or weakens(new_th[op], old_th[op])):
                    problems.append(f"ratchet: metric {mid} loosened {op} threshold")
            if old.get("blocking") and not new.get("blocking"):
                problems.append(f"ratchet: blocking metric {mid} became non-blocking")
            if old_th.get("no_significant_drop") and not new_th.get("no_significant_drop"):
                problems.append(f"ratchet: metric {mid} dropped no_significant_drop")

        current_slices = {s.get("name"): s for s in spec.get("slices", [])}
        original_slices = {s.get("name"): s for s in base_spec.get("slices", [])}
        for name, old in original_slices.items():
            new = current_slices.get(name)
            if new is None:
                problems.append(f"ratchet: slice {name} was removed")
                continue
            if new.get("metric") != old.get("metric"):
                problems.append(f"ratchet: slice {name} changed metric")
            for op, weakens in (("min", lambda n, o: n < o), ("max", lambda n, o: n > o)):
                if op in old and (op not in new or weakens(new[op], old[op])):
                    problems.append(f"ratchet: slice {name} loosened {op} threshold")
            if old.get("blocking") and not new.get("blocking"):
                problems.append(f"ratchet: blocking slice {name} became non-blocking")
            if old.get("no_significant_drop") and not new.get("no_significant_drop"):
                problems.append(f"ratchet: slice {name} dropped no_significant_drop")
    return problems


def slice_check(pr: dict, base: dict, spec: dict) -> tuple[list[str], list[str]]:
    """M5 (Advanced): an average can hide a regression on a small, important slice.
    For each entry of spec["slices"] ({"name", "metric", "min"?, "no_significant_drop"?, "blocking"}):
      - value = mean of per_item(pr, metric, slice_name=name)
      - fail if below "min", or if no_significant_drop and the paired CI over the slice's items
        lies entirely below 0
    Return (blocked ids like "slice:money_movement/pass_rate", report lines)."""
    blocked: list[str] = []
    lines: list[str] = []
    bs = spec.get("bootstrap", {})
    for rule in spec.get("slices", []):
        name, metric = rule["name"], rule["metric"]
        pr_items = per_item(pr, metric, name)
        base_items = per_item(base, metric, name)
        value = sum(pr_items.values()) / len(pr_items) if pr_items else None
        notes = []
        failed = False
        if value is None:
            failed = True
            notes.append("no PR rows in slice")
        else:
            if "min" in rule and value < rule["min"]:
                failed = True
                notes.append(f"mean {value:.3f} < min {rule['min']}")
            if "max" in rule and value > rule["max"]:
                failed = True
                notes.append(f"mean {value:.3f} > max {rule['max']}")
        if rule.get("no_significant_drop"):
            keys = sorted(set(pr_items) & set(base_items))
            if not keys:
                failed = True
                notes.append("no paired items in slice")
            else:
                d, lo, hi = stats.paired_bootstrap(
                    [pr_items[k] for k in keys], [base_items[k] for k in keys],
                    n_resamples=bs.get("n_resamples", 1000), rng=bs.get("rng", 2027),
                    confidence=bs.get("confidence", 0.95))
                notes.append(f"Δ {d:+.3f} CI [{lo:+.3f}, {hi:+.3f}]")
                if hi < 0:
                    failed = True
                    notes.append("significant drop")
        verdict = "BLOCK" if failed and rule.get("blocking") else ("warn" if failed else "ok")
        if failed and rule.get("blocking"):
            blocked.append(f"slice:{name}/{metric}")
        lines.append(f"  slice:{name}/{metric:<18} {value!s:<8} {verdict}  {'; '.join(notes)}")
    return blocked, lines


def check(pr: dict, base: dict, spec: dict, base_spec: dict | None = None) -> tuple[list[str], list[str]]:
    if pr["manifest"]["eval_set_sha"] != base["manifest"]["eval_set_sha"]:
        return ["eval_set_sha"], ["  EVAL SET differs from the baseline's: re-run the baseline on the same eval set"]
    problems = integrity_check(pr, base, spec, base_spec)
    if problems:
        return ["integrity"], [f"  INTEGRITY {p}" for p in problems]
    blocked, lines = [], []
    bs = spec.get("bootstrap", {})
    for m in spec["metrics"]:
        mid, th, val = m["id"], m.get("threshold", {}), pr["metrics"].get(m["id"])
        verdict, note = "ok", ""
        if val is None:
            verdict = "missing"
        elif "max" in th and val > th["max"]:
            verdict = "fail"
        elif "min" in th and val < th["min"]:
            verdict = "fail"
        elif "max_increase" in th and base["metrics"].get(mid) is not None:
            if val > base["metrics"][mid] * (1 + th["max_increase"]):
                verdict = "fail"
        if th.get("no_significant_drop") and val is not None:
            a, b = per_item(pr, mid), per_item(base, mid)
            keys = sorted(set(a) & set(b))
            d, lo, hi = stats.paired_bootstrap([a[k] for k in keys], [b[k] for k in keys],
                                               n_resamples=bs.get("n_resamples", 1000), rng=bs.get("rng", 2027))
            note = f"Δ {d:+.3f} CI [{lo:+.3f}, {hi:+.3f}]"
            if hi < 0:
                verdict = "fail"
        if verdict != "ok" and m.get("blocking"):
            blocked.append(mid)
        label = {"ok": "ok", "missing": "MISSING"}.get(verdict, "BLOCK" if m.get("blocking") else "warn")
        lines.append(f"  {mid:<22} {val!s:<8} {json.dumps(th):<40} {note:<28} {label}")
    if spec.get("slices"):
        sb, sl = slice_check(pr, base, spec)
        blocked += sb
        lines += sl
    return blocked, lines


def main(argv: list[str] | None = None) -> int:
    utf8_stdio()
    ap = argparse.ArgumentParser(prog="lab9.gate")
    ap.add_argument("--pr", required=True)
    ap.add_argument("--baseline", required=True)
    ap.add_argument("--spec", default=str(SPEC))
    ap.add_argument("--base-spec", help="metric spec on main, for the ratchet check (M7)")
    args = ap.parse_args(argv)
    pr = json.loads(Path(args.pr).read_text(encoding="utf-8"))
    base = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
    base_spec = load_spec(Path(args.base_spec)) if args.base_spec else None
    blocked, lines = check(pr, base, load_spec(Path(args.spec)), base_spec)
    print(f"gate: PR {pr['run_id']} vs main {base['run_id']} · eval set {pr['manifest']['eval_set_sha']}")
    print("\n".join(lines))
    if blocked:
        print(f"exit 1 — blocked: {', '.join(blocked)}")
        return 1
    print("exit 0 — merge")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
