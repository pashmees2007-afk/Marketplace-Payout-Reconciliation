"""
End-to-end pipeline runner.

    python run.py

runs the full flow described in the architecture diagram:

    generate_data.py
          |
          v
   orders.csv          payouts.csv
          |                  |
          +--------+---------+
                   v
             reconcile.py  (deterministic subset-sum matcher)
                   |
        +----------+-----------+
        v          v           v
     MATCHED  CLOSE_MATCH  UNRESOLVED
                   |           |
                   +-----+-----+
                         v
                    agent.py  (AI explains exceptions only)
                         |
                         v
                    report.py
                         |
                         v
        output/metrics.json + output/exceptions.csv + audit_log.jsonl

Then `streamlit run app.py` launches the interactive dashboard over the
same output files.
"""

import argparse
import sys
import time

import config
import generate_data
import report
from validation import DataValidationError


def _step(title):
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


def main():
    parser = argparse.ArgumentParser(description="Run the full AI Finance Controller pipeline.")
    parser.add_argument("--seed", type=int, default=config.RANDOM_SEED)
    parser.add_argument("--sellers", type=int, default=config.NUM_SELLERS)
    parser.add_argument("--skip-generate", action="store_true",
                         help="Reuse existing data/*.csv instead of regenerating.")
    args = parser.parse_args()

    started = time.time()

    if not args.skip_generate:
        _step("STEP 1/4 -- Generating synthetic marketplace data")
        orders, payouts, ground_truth = generate_data.generate(seed=args.seed, num_sellers=args.sellers)
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        generate_data.write_orders_csv(orders, config.ORDERS_CSV)
        generate_data.write_payouts_csv(payouts, config.PAYOUTS_CSV)
        generate_data.write_ground_truth_json(ground_truth, config.GROUND_TRUTH_JSON)
        print(f"  {len(orders)} orders, {len(payouts)} payouts, "
              f"{len(ground_truth['orphaned_order_ids'])} intentionally orphaned orders, "
              f"seed={args.seed}")
    else:
        _step("STEP 1/4 -- Skipped data generation (--skip-generate); using existing data/")

    _step("STEP 2/4 -- Validating input data")
    from validation import load_orders, load_payouts
    try:
        orders = load_orders(config.ORDERS_CSV)
        payouts = load_payouts(config.PAYOUTS_CSV)
    except DataValidationError as exc:
        print(f"DATA VALIDATION FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
    print(f"  {len(orders)} orders and {len(payouts)} payouts passed validation "
          f"(integer-paise, unique IDs, valid dates, net_payable arithmetic checks).")

    _step("STEP 3/4 -- Running deterministic reconciliation + AI exception explanations")
    try:
        metrics, outcome, explanations = report.generate_report(orders=orders, payouts=payouts)
    except DataValidationError as exc:
        print(f"RECONCILIATION FAILED: {exc}", file=sys.stderr)
        sys.exit(1)

    status_counts = {}
    for r in outcome["results"]:
        status_counts[r["status"]] = status_counts.get(r["status"], 0) + 1
    print(f"  Reconciliation: {status_counts}")
    print(f"  Orphaned orders: {len(outcome['orphaned_order_ids'])}")
    print(f"  AI explanations generated: {len(explanations)} (LLM_MODE={config.LLM_MODE})")

    _step("STEP 4/4 -- Report")
    kpis = metrics["kpis"]
    print(f"  Match rate by value        : {kpis['match_rate_by_value']:.2%}")
    print(f"  Exact match rate by value  : {kpis['exact_match_rate_by_value']:.2%}")
    print(f"  Match rate by count        : {kpis['match_rate_by_count']:.2%}")
    if "ground_truth_validation" in metrics:
        gtv = metrics["ground_truth_validation"]
        print(f"  Ground-truth accuracy      : {gtv['overall_accuracy']:.2%} "
              f"({gtv['payouts_correct']}/{gtv['payouts_evaluated']})")
        print(f"  Orphan detection precision : {gtv['orphaned_order_precision']}")
        print(f"  Orphan detection recall    : {gtv['orphaned_order_recall']}")

    elapsed = time.time() - started
    print(f"\nPipeline complete in {elapsed:.2f}s.")
    print(f"  {config.METRICS_JSON}")
    print(f"  {config.EXCEPTIONS_CSV}")
    print(f"  {config.AUDIT_LOG_JSONL}")
    print("\nNext: streamlit run app.py")


if __name__ == "__main__":
    main()
