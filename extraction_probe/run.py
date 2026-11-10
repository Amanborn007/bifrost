"""CLI entry point for the Bifrost Extraction Probe.

Usage:
    python -m extraction_probe.run --target /path/to/python/project
    python extraction_probe/run.py --target /path/to/python/project
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter

from extraction_probe.agent import ExtractionResult, run_extraction


def print_summary(result: ExtractionResult) -> None:
    """Print a human-readable extraction summary."""
    print("\n" + "=" * 60)
    print("  BIFROST EXTRACTION PROBE — RESULTS")
    print("=" * 60)

    # Entity counts
    print(f"\n  Components extracted: {len(result.components)}")
    print(f"  Flows extracted:      {len(result.flows)}")
    print(f"  Files processed:      {result.files_processed}")
    print(f"  Files skipped:        {result.files_skipped}")

    # Component breakdown
    if result.components:
        print("\n  --- Components ---")
        type_counts = Counter(c.type for c in result.components)
        for comp_type, count in type_counts.most_common():
            print(f"    {comp_type}: {count}")

        print()
        for comp in result.components:
            conf_marker = "+" if comp.confidence == "confirmed" else "~"
            gaps_note = f" [{len(comp.known_gaps)} gaps]" if comp.known_gaps else ""
            print(f"    [{conf_marker}] {comp.name} ({comp.type}) — {comp.purpose[:60]}{gaps_note}")

    # Flow breakdown
    if result.flows:
        print("\n  --- Flows ---")
        for flow in result.flows:
            conf_marker = "+" if flow.confidence == "confirmed" else "~"
            print(f"    [{conf_marker}] {flow.name} ({flow.flow_type})")
            print(f"         entry: {flow.entry_point}")
            print(f"         segments: {len(flow.segments)}")
            for seg in flow.segments:
                print(f"           {seg.order}. {seg.component_name} [{seg.segment_type}]")

    # Confidence distribution
    all_confidences = [c.confidence for c in result.components] + [
        f.confidence for f in result.flows
    ]
    if all_confidences:
        conf_counts = Counter(all_confidences)
        total = len(all_confidences)
        confirmed_pct = conf_counts.get("confirmed", 0) / total * 100
        inferred_pct = conf_counts.get("inferred", 0) / total * 100

        print(f"\n  --- Confidence Distribution ---")
        print(f"    confirmed: {conf_counts.get('confirmed', 0)} ({confirmed_pct:.0f}%)")
        print(f"    inferred:  {conf_counts.get('inferred', 0)} ({inferred_pct:.0f}%)")
        confirmed_or_inferred_pct = confirmed_pct + inferred_pct
        print(f"    confirmed+inferred: {confirmed_or_inferred_pct:.0f}%")

    # Validation stats
    total_attempts = result.total_attempts
    failure_rate = (
        result.validation_failures / max(total_attempts, 1) * 100
    )
    print(f"\n  --- Validation ---")
    print(f"    Total LLM calls:        {total_attempts}")
    print(f"    Validation failures:     {result.validation_failures}")
    print(f"    Failure rate:            {failure_rate:.1f}%")
    print(f"    Canary mismatches:       {result.canary_mismatches}")

    # Cost
    cost = result.cost
    print(f"\n  --- Cost ---")
    print(f"    Input tokens:   {cost.total_input_tokens:,}")
    print(f"    Output tokens:  {cost.total_output_tokens:,}")
    print(f"    Total cost:     ${cost.total_cost_usd:.4f}")
    print(f"    LLM calls:      {cost.call_count}")

    # Duration
    print(f"\n  --- Duration ---")
    print(f"    Total time:     {result.duration_seconds:.1f}s")
    print("=" * 60)

    # Phase 0 gate check
    print("\n  PHASE 0 GATE CHECK:")
    gates = [
        (len(result.components) >= 5, f"  Components >= 5: {len(result.components)}"),
        (len(result.flows) >= 2, f"  Flows >= 2: {len(result.flows)}"),
        (failure_rate < 10, f"  Pydantic failure rate < 10%: {failure_rate:.1f}%"),
    ]

    # Check confidence: >50% confirmed or inferred (both count)
    if all_confidences:
        conf_counts_check = Counter(all_confidences)
        total_check = len(all_confidences)
        ci_pct = (conf_counts_check.get("confirmed", 0) + conf_counts_check.get("inferred", 0)) / total_check * 100
    else:
        ci_pct = 0.0
    gates.append((ci_pct > 50, f"  Confirmed+inferred > 50%: {ci_pct:.0f}%"))
    gates.append((cost.total_cost_usd < 2.0, f"  Cost < $2.00: ${cost.total_cost_usd:.4f}"))
    gates.append((result.duration_seconds < 300, f"  Runtime < 5 min: {result.duration_seconds:.1f}s"))

    all_pass = True
    for passed, msg in gates:
        status = "PASS" if passed else "FAIL"
        print(f"    [{status}] {msg}")
        if not passed:
            all_pass = False

    print()
    if all_pass:
        print("  >>> PHASE 0 GATE: PASS — proceed to Phase 1 <<<")
    else:
        print("  >>> PHASE 0 GATE: FAIL — iterate prompts or change test repo <<<")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="extraction_probe",
        description="Bifrost Phase 0 — validate extraction quality against a Python repo",
    )
    parser.add_argument(
        "--target",
        required=True,
        help="Path to the Python project directory to analyze",
    )
    parser.add_argument(
        "--repo-name",
        default=None,
        help="Human-readable repository name (defaults to directory name)",
    )
    parser.add_argument(
        "--max-files",
        type=int,
        default=50,
        help="Maximum number of Python files to process (default: 50)",
    )

    args = parser.parse_args()

    print(f"  Bifrost Extraction Probe")
    print(f"  Target: {args.target}")
    print(f"  Max files: {args.max_files}")
    print()

    try:
        result = run_extraction(
            target_dir=args.target,
            repo_name=args.repo_name,
        )
        print_summary(result)
        return 0 if len(result.components) >= 5 and len(result.flows) >= 2 else 1
    except FileNotFoundError as e:
        print(f"  Error: {e}", file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"  Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
