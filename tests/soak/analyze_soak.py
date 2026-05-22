#!/usr/bin/env python3
"""Score a genie-ai-runtime soak run against the issue #113 exit criteria.

Reads the `telemetry.jsonl` written by `soak_driver.py` (plus optional
tegrastats / journal / systemd-restart inputs gathered by `genie-soak.sh`) and
emits a human-readable `report.md` and machine-readable `summary.json`. Each of
the six M1 acceptance criteria becomes a verdict: PASS, FAIL, or N/A (input not
captured). The process exits non-zero if any *evaluated* criterion fails, so it
drops straight into CI or a shell `if`.

Stdlib only. Run `--self-check` to score the committed example fixture and
assert the expected verdicts — that exercises every code path without a Jetson.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# Markers that indicate the kernel OOM killer fired. Matched case-insensitively
# against the captured dmesg + journal text.
OOM_MARKERS = (
    "out of memory",
    "oom-kill",
    "oom_reaper",
    "oom_kill_process",
    "killed process",
    "out_of_memory",
)

# Hour marks the alpha latency budget is checked at, and the half-width of the
# window (seconds) collected around each mark.
LATENCY_CHECK_HOURS = (1, 12, 24)
LATENCY_WINDOW_S = 1800.0

PASS = "PASS"
FAIL = "FAIL"
NA = "N/A"


def percentile(values: list[float], pct: float) -> float | None:
    """Linear-interpolated percentile (pct in 0..100). None for empty input."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (pct / 100.0) * (len(ordered) - 1)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    frac = rank - low
    return ordered[low] + (ordered[high] - ordered[low]) * frac


def load_telemetry(path: str) -> list[dict]:
    records = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def linregress_slope(xs: list[float], ys: list[float]) -> float | None:
    """Least-squares slope of ys over xs (units: y per x). None if degenerate."""
    n = len(xs)
    if n < 2:
        return None
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    denom = sum((x - mean_x) ** 2 for x in xs)
    if denom == 0:
        return None
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    return num / denom


def verdict_uptime(records: list[dict], restarts: int | None, target_h: float) -> dict:
    span_s = 0.0
    if records:
        span_s = max(r.get("elapsed_s", 0.0) for r in records)
    span_h = span_s / 3600.0
    long_enough = span_h >= target_h * 0.999  # tolerate sub-second rounding
    if restarts is None:
        status = NA
        detail = f"observed span {span_h:.2f}h; restart count not captured"
    elif restarts == 0 and long_enough:
        status = PASS
        detail = f"{span_h:.2f}h continuous, 0 runtime restarts"
    elif restarts != 0:
        status = FAIL
        detail = f"{restarts} runtime restart(s) during the run"
    else:
        status = FAIL
        detail = f"only {span_h:.2f}h of telemetry, target {target_h:.0f}h"
    return {
        "name": "24h continuous uptime, zero runtime restarts",
        "status": status,
        "detail": detail,
        "span_hours": round(span_h, 3),
        "restarts": restarts,
    }


def verdict_oom(journal_path: str | None) -> dict:
    if not journal_path or not os.path.exists(journal_path):
        return {
            "name": "Zero OOM kills (dmesg/journal clean)",
            "status": NA,
            "detail": "no dmesg/journal capture provided",
            "hits": [],
        }
    hits = []
    with open(journal_path, encoding="utf-8", errors="replace") as handle:
        for lineno, line in enumerate(handle, 1):
            low = line.lower()
            if any(marker in low for marker in OOM_MARKERS):
                hits.append(f"{lineno}: {line.strip()[:200]}")
    status = PASS if not hits else FAIL
    detail = "no OOM markers found" if not hits else f"{len(hits)} OOM marker(s) found"
    return {
        "name": "Zero OOM kills (dmesg/journal clean)",
        "status": status,
        "detail": detail,
        "hits": hits[:20],
    }


def verdict_health(records: list[dict], threshold: float) -> dict:
    samples = [r.get("health", {}) for r in records if isinstance(r.get("health"), dict)]
    total = len(samples)
    ok = sum(1 for h in samples if h.get("ok"))
    ratio = (ok / total) if total else 0.0
    if total == 0:
        status, detail = NA, "no health samples"
    elif ratio >= threshold:
        status = PASS
        detail = f"{ok}/{total} reachable ({ratio * 100:.2f}% >= {threshold * 100:.0f}%)"
    else:
        status = FAIL
        detail = f"{ok}/{total} reachable ({ratio * 100:.2f}% < {threshold * 100:.0f}%)"
    return {
        "name": "/api/health reports genie-ai-runtime reachable >= 99% of samples",
        "status": status,
        "detail": detail,
        "samples": total,
        "reachable": ok,
        "ratio": round(ratio, 5),
    }


def _memory_series(records: list[dict]) -> tuple[str, list[float], list[float]]:
    """Pick the best available memory series for the plateau check.

    Prefers runtime RSS (MemoryCurrent). Falls back to declining
    mem_available_mb (inverted so "growth" is always an increasing series).
    Returns (source_label, elapsed_hours, values).
    """
    xs_rss, ys_rss = [], []
    xs_avail, ys_avail = [], []
    for r in records:
        t = r.get("elapsed_s")
        if t is None:
            continue
        hours = t / 3600.0
        rss = r.get("runtime_rss_bytes")
        if isinstance(rss, (int, float)):
            xs_rss.append(hours)
            ys_rss.append(float(rss))
        avail = (r.get("health") or {}).get("mem_available_mb")
        if isinstance(avail, (int, float)):
            xs_avail.append(hours)
            ys_avail.append(float(avail))
    if len(ys_rss) >= 2:
        return "runtime_rss_bytes", xs_rss, ys_rss
    if len(ys_avail) >= 2:
        # Invert: shrinking available memory == growth pressure.
        return "mem_available_mb_inverted", xs_avail, [-v for v in ys_avail]
    return "none", [], []


def verdict_memory(records: list[dict], max_growth_frac: float) -> dict:
    source, xs, ys = _memory_series(records)
    name = "No memory monotonic growth (RSS plateaus, not creeps)"
    if source == "none" or len(ys) < 4:
        return {"name": name, "status": NA, "detail": "no usable memory series", "source": source}

    window = max(1, len(ys) // 10)
    base = sorted(ys[:window])[window // 2]
    tail = sorted(ys[-window:])[window // 2]
    slope = linregress_slope(xs, ys)  # value units per hour
    growth_frac = ((tail - base) / abs(base)) if base else 0.0

    detail = (
        f"median first-decile={base:.0f}, last-decile={tail:.0f} "
        f"({growth_frac * 100:+.1f}%, slope {slope:.1f}/h) via {source}"
    )
    # A plateau allows small noise; sustained creep fails. Inverted available
    # memory uses the same fractional bound on its (negative-of) magnitude.
    status = PASS if growth_frac <= max_growth_frac else FAIL
    return {
        "name": name,
        "status": status,
        "detail": detail,
        "source": source,
        "growth_frac": round(growth_frac, 4),
        "slope_per_hour": round(slope, 3) if slope is not None else None,
    }


def verdict_latency(records: list[dict], p50_budget: float, p99_budget: float) -> dict:
    name = "First-token latency p50/p99 within budget at hours 1, 12, 24"
    windows = {}
    for hour in LATENCY_CHECK_HOURS:
        center = hour * 3600.0
        vals = []
        for r in records:
            t = r.get("elapsed_s")
            req = r.get("request") or {}
            ftt = req.get("first_token_ms")
            if t is None or ftt is None or not req.get("ok"):
                continue
            if abs(t - center) <= LATENCY_WINDOW_S:
                vals.append(float(ftt))
        if vals:
            windows[hour] = {
                "n": len(vals),
                "p50_ms": round(percentile(vals, 50) or 0.0, 1),
                "p99_ms": round(percentile(vals, 99) or 0.0, 1),
            }

    if not windows:
        return {
            "name": name,
            "status": NA,
            "detail": "no successful first-token samples near hours 1/12/24",
            "budget_p50_ms": p50_budget,
            "budget_p99_ms": p99_budget,
            "windows": {},
        }

    breaches = []
    for hour, w in windows.items():
        if w["p50_ms"] > p50_budget:
            breaches.append(f"h{hour} p50 {w['p50_ms']}ms > {p50_budget}ms")
        if w["p99_ms"] > p99_budget:
            breaches.append(f"h{hour} p99 {w['p99_ms']}ms > {p99_budget}ms")
    status = PASS if not breaches else FAIL
    detail = (
        "within budget at " + ", ".join(f"h{h}" for h in sorted(windows))
        if not breaches
        else "; ".join(breaches)
    )
    return {
        "name": name,
        "status": status,
        "detail": detail,
        "budget_p50_ms": p50_budget,
        "budget_p99_ms": p99_budget,
        "windows": {str(h): w for h, w in sorted(windows.items())},
    }


def verdict_artifact(records: list[dict], telemetry_path: str) -> dict:
    name = "Soak telemetry artifact present under tests/soak/"
    n = len(records)
    status = PASS if n > 0 else FAIL
    detail = f"{n} telemetry records parsed from {os.path.basename(telemetry_path)}"
    return {"name": name, "status": status, "detail": detail, "records": n}


def render_markdown(summary: dict) -> str:
    icon = {PASS: "✅", FAIL: "❌", NA: "⚠️"}
    lines = [
        "# genie-ai-runtime 24h soak report",
        "",
        f"- Telemetry: `{summary['telemetry']}` ({summary['records']} records)",
        f"- Observed span: {summary['span_hours']} h",
        f"- Overall: **{summary['overall']}**",
        "",
        "| Criterion | Verdict | Detail |",
        "| --- | --- | --- |",
    ]
    for v in summary["criteria"]:
        detail = v["detail"].replace("|", "\\|")
        lines.append(f"| {v['name']} | {icon.get(v['status'], '')} {v['status']} | {detail} |")
    lines.append("")
    lines.append(
        "Verdicts marked N/A had no captured input (e.g. no journal, no restart "
        "count, or RSS unaccounted on this host) and were excluded from the "
        "pass/fail gate. See README.md for how each criterion is computed."
    )
    lines.append("")
    return "\n".join(lines)


def analyze(args) -> dict:
    records = load_telemetry(args.telemetry)
    criteria = [
        verdict_uptime(records, args.restarts, args.duration_target_h),
        verdict_oom(args.journal),
        verdict_health(records, args.health_threshold),
        verdict_memory(records, args.max_growth_frac),
        verdict_latency(records, args.budget_p50_ms, args.budget_p99_ms),
        verdict_artifact(records, args.telemetry),
    ]
    span_h = round(max((r.get("elapsed_s", 0.0) for r in records), default=0.0) / 3600.0, 3)
    failed = [c for c in criteria if c["status"] == FAIL]
    overall = FAIL if failed else PASS
    return {
        "telemetry": os.path.basename(args.telemetry),
        "records": len(records),
        "span_hours": span_h,
        "overall": overall,
        "criteria": criteria,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--telemetry", default="telemetry.jsonl", help="telemetry JSONL path")
    parser.add_argument("--journal", default=None, help="captured dmesg/journal text for OOM scan")
    parser.add_argument(
        "--restarts",
        type=int,
        default=None,
        help="genie-ai-runtime NRestarts delta over the run (0 = none); omit if uncaptured",
    )
    parser.add_argument("--duration-target-h", type=float, default=24.0)
    parser.add_argument("--health-threshold", type=float, default=0.99)
    parser.add_argument(
        "--max-growth-frac",
        type=float,
        default=0.05,
        help="max tolerated first->last decile memory growth (default: 0.05 = 5%%)",
    )
    # Placeholder alpha budget — confirm against the M1 voice-latency budget
    # before relying on a PASS here. See README.md.
    parser.add_argument("--budget-p50-ms", type=float, default=1500.0)
    parser.add_argument("--budget-p99-ms", type=float, default=4000.0)
    parser.add_argument("--out-json", default=None, help="write summary JSON here")
    parser.add_argument("--out-md", default=None, help="write Markdown report here")
    parser.add_argument(
        "--self-check",
        action="store_true",
        help="score the committed example fixture and assert expected verdicts",
    )
    return parser


def run_self_check() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    example = os.path.join(here, "example")
    args = build_parser().parse_args(
        [
            "--telemetry",
            os.path.join(example, "telemetry.jsonl"),
            "--journal",
            os.path.join(example, "journal.txt"),
            "--restarts",
            "0",
            # The example fixture is a compressed 60-tick stand-in, not a real
            # 24h run, so relax the duration gate for the self-check only.
            "--duration-target-h",
            "0",
        ]
    )
    summary = analyze(args)
    by_name = {c["name"]: c["status"] for c in summary["criteria"]}
    expected = {
        "24h continuous uptime, zero runtime restarts": PASS,
        "Zero OOM kills (dmesg/journal clean)": PASS,
        "/api/health reports genie-ai-runtime reachable >= 99% of samples": PASS,
        "No memory monotonic growth (RSS plateaus, not creeps)": PASS,
        "First-token latency p50/p99 within budget at hours 1, 12, 24": PASS,
        "Soak telemetry artifact present under tests/soak/": PASS,
    }
    ok = True
    for name, want in expected.items():
        got = by_name.get(name)
        flag = "ok" if got == want else "MISMATCH"
        if got != want:
            ok = False
        print(f"[{flag}] {name}: expected {want}, got {got}")
    print(f"self-check: {'PASS' if ok else 'FAIL'} (overall={summary['overall']})")
    return 0 if ok else 1


def main() -> int:
    # The report uses verdict emoji; keep stdout from dying on a non-UTF-8
    # console (e.g. Windows cp1252) while leaving Linux/CI behaviour unchanged.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    args = build_parser().parse_args()
    if args.self_check:
        return run_self_check()

    if not os.path.exists(args.telemetry):
        print(f"error: telemetry not found: {args.telemetry}", file=sys.stderr)
        return 2

    summary = analyze(args)
    report = render_markdown(summary)

    if args.out_json:
        with open(args.out_json, "w", encoding="utf-8") as handle:
            json.dump(summary, handle, indent=2)
            handle.write("\n")
    if args.out_md:
        with open(args.out_md, "w", encoding="utf-8") as handle:
            handle.write(report)

    print(report)
    return 0 if summary["overall"] == PASS else 1


if __name__ == "__main__":
    sys.exit(main())
