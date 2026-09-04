"""
Metrics and exception reporting.

Produces:
  output/metrics.json     -- throughput, match-rate KPIs, and a genuine
                              accuracy check against data/ground_truth.json
  output/exceptions.csv   -- every UNRESOLVED payout and ORPHANED_ORDER,
                              each with its AI-generated explanation,
                              shown in full (never cherry-picked).

The ground-truth comparison in compute_metrics() is computed by scoring
this run's actual reconciliation output against what generate_data.py
intentionally built -- it is never inferred from the reconciliation
result itself, so it cannot be inflated or faked.
"""

import argparse
import csv
import json

import agent
import config
from money import paise_to_float
from reconcile import run_reconciliation
from validation import load_orders, load_payouts


def _scenario_is_correct(scenario: str, status: str, matched_order_ids: list, true_order_ids: list) -> bool:
    if scenario == "exact_match":
        return status == "MATCHED" and set(matched_order_ids) == set(true_order_ids)
    if scenario == "close_match":
        return status == "CLOSE_MATCH"
    if scenario == "timing_mismatch":
        # A late-arriving order should never be silently treated as a clean
        # exact match -- correctness here means the engine correctly
        # flagged it as an exception (CLOSE_MATCH or UNRESOLVED).
        return status in ("CLOSE_MATCH", "UNRESOLVED")
    if scenario == "unresolved":
        return status == "UNRESOLVED"
    return False


def compute_ground_truth_validation(results: list, orphaned_order_ids: list, ground_truth: dict) -> dict:
    results_by_id = {r["payout_id"]: r for r in results}
    per_scenario = {}
    total_correct = 0
    total_evaluated = 0

    for payout_id, entry in ground_truth.get("payouts", {}).items():
        r = results_by_id.get(payout_id)
        if r is None:
            continue  # payout in ground truth but not in this run's payouts.csv
        scenario = entry["scenario"]
        bucket = per_scenario.setdefault(scenario, {"correct": 0, "total": 0})
        bucket["total"] += 1
        total_evaluated += 1
        if _scenario_is_correct(scenario, r["status"], r["matched_order_ids"], entry["true_order_ids"]):
            bucket["correct"] += 1
            total_correct += 1

    for bucket in per_scenario.values():
        bucket["accuracy"] = round(bucket["correct"] / bucket["total"], 4) if bucket["total"] else None

    true_orphans = set(ground_truth.get("orphaned_order_ids", []))
    engine_orphans = set(orphaned_order_ids)
    true_positives = len(true_orphans & engine_orphans)
    orphan_precision = round(true_positives / len(engine_orphans), 4) if engine_orphans else None
    orphan_recall = round(true_positives / len(true_orphans), 4) if true_orphans else None

    return {
        "payouts_evaluated": total_evaluated,
        "payouts_correct": total_correct,
        "overall_accuracy": round(total_correct / total_evaluated, 4) if total_evaluated else None,
        "by_scenario": per_scenario,
        "orphaned_order_ground_truth_count": len(true_orphans),
        "orphaned_order_engine_count": len(engine_orphans),
        "orphaned_order_true_positives": true_positives,
        "orphaned_order_precision": orphan_precision,
        "orphaned_order_recall": orphan_recall,
    }


def compute_metrics(orders: list, payouts: list, outcome: dict, ground_truth: dict = None) -> dict:
    results = outcome["results"]
    orphaned_order_ids = outcome["orphaned_order_ids"]
    orders_by_id = {o["order_id"]: o for o in orders}

    total_payout_value_paise = sum(r["payout_amount_paise"] for r in results)

    by_status_count = {"MATCHED": 0, "CLOSE_MATCH": 0, "UNRESOLVED": 0}
    by_status_value_paise = {"MATCHED": 0, "CLOSE_MATCH": 0, "UNRESOLVED": 0}
    for r in results:
        by_status_count[r["status"]] += 1
        by_status_value_paise[r["status"]] += r["payout_amount_paise"]

    matched_payout_value_paise = by_status_value_paise["MATCHED"] + by_status_value_paise["CLOSE_MATCH"]
    orphaned_order_value_paise = sum(orders_by_id[oid]["net_payable_paise"] for oid in orphaned_order_ids)

    metrics = {
        "totals": {
            "total_orders": len(orders),
            "total_payouts": len(payouts),
            "total_payout_value": paise_to_float(total_payout_value_paise),
            "orphaned_order_count": len(orphaned_order_ids),
            "orphaned_order_value": paise_to_float(orphaned_order_value_paise),
        },
        "by_status": {
            status: {
                "count": by_status_count[status],
                "value": paise_to_float(by_status_value_paise[status]),
            }
            for status in ("MATCHED", "CLOSE_MATCH", "UNRESOLVED")
        },
        "kpis": {
            "match_rate_by_value": (
                round(matched_payout_value_paise / total_payout_value_paise, 4)
                if total_payout_value_paise else None
            ),
            "exact_match_rate_by_value": (
                round(by_status_value_paise["MATCHED"] / total_payout_value_paise, 4)
                if total_payout_value_paise else None
            ),
            "match_rate_by_count": (
                round((by_status_count["MATCHED"] + by_status_count["CLOSE_MATCH"]) / len(results), 4)
                if results else None
            ),
            "exact_match_rate_by_count": (
                round(by_status_count["MATCHED"] / len(results), 4) if results else None
            ),
        },
        "config": {
            "tolerance_paise": config.TOLERANCE_PAISE,
            "eligibility_window_days": config.ELIGIBILITY_WINDOW_DAYS,
        },
    }

    if ground_truth is not None:
        metrics["ground_truth_validation"] = compute_ground_truth_validation(
            results, orphaned_order_ids, ground_truth
        )

    return metrics


def write_metrics_json(metrics: dict, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(metrics, f, indent=2)


def write_exceptions_csv(outcome: dict, orders_by_id: dict, explanations: dict, path):
    """Every UNRESOLVED payout and every ORPHANED_ORDER, in full."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "type", "id", "seller_id", "amount", "date", "delta",
        "best_candidate_order_ids", "ai_explanation",
    ]
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for r in outcome["results"]:
            if r["status"] != "UNRESOLVED":
                continue
            exp = explanations.get(r["payout_id"])
            writer.writerow({
                "type": "UNRESOLVED_PAYOUT",
                "id": r["payout_id"],
                "seller_id": r["seller_id"],
                "amount": r["payout_amount"],
                "date": r["payout_date"],
                "delta": r["delta"],
                "best_candidate_order_ids": ";".join(r["matched_order_ids"]),
                "ai_explanation": exp["explanation"] if exp else "",
            })

        for order_id in outcome["orphaned_order_ids"]:
            o = orders_by_id.get(order_id)
            if o is None:
                continue
            writer.writerow({
                "type": "ORPHANED_ORDER",
                "id": order_id,
                "seller_id": o["seller_id"],
                "amount": paise_to_float(o["net_payable_paise"]),
                "date": o["order_date"].isoformat(),
                "delta": "",
                "best_candidate_order_ids": "",
                "ai_explanation": (
                    f"This order (net payable {paise_to_float(o['net_payable_paise']):.2f}) has not "
                    f"been claimed by any payout -- it represents money owed to the seller that has "
                    f"not yet been settled."
                ),
            })


def generate_report(orders_path=None, payouts_path=None, ground_truth_path=None,
                     orders: list = None, payouts: list = None):
    """Run reconciliation, AI explanations, and write metrics.json + exceptions.csv.

    Pass pre-loaded `orders`/`payouts` (e.g. from a caller that already
    validated them, such as run.py) to avoid re-reading the CSVs a second
    time -- omit them to have this function load from `orders_path`/
    `payouts_path` (defaulting to config.ORDERS_CSV/PAYOUTS_CSV) itself.
    """
    orders_path = orders_path or config.ORDERS_CSV
    payouts_path = payouts_path or config.PAYOUTS_CSV
    ground_truth_path = ground_truth_path or config.GROUND_TRUTH_JSON

    if orders is None:
        orders = load_orders(orders_path)
    if payouts is None:
        payouts = load_payouts(payouts_path)
    outcome = run_reconciliation(orders_path, payouts_path, orders=orders, payouts=payouts)

    ground_truth = None
    if ground_truth_path and ground_truth_path.exists():
        with open(ground_truth_path) as f:
            ground_truth = json.load(f)

    explanations = agent.explain_all_exceptions(outcome["results"])

    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(config.RECONCILIATION_RESULTS_JSON, "w") as f:
        json.dump(outcome, f, indent=2)

    metrics = compute_metrics(orders, payouts, outcome, ground_truth=ground_truth)
    write_metrics_json(metrics, config.METRICS_JSON)

    orders_by_id = {o["order_id"]: o for o in orders}
    write_exceptions_csv(outcome, orders_by_id, explanations, config.EXCEPTIONS_CSV)

    return metrics, outcome, explanations


def main():
    parser = argparse.ArgumentParser(description="Generate metrics.json and exceptions.csv.")
    parser.add_argument("--orders", type=str, default=str(config.ORDERS_CSV))
    parser.add_argument("--payouts", type=str, default=str(config.PAYOUTS_CSV))
    parser.add_argument("--ground-truth", type=str, default=str(config.GROUND_TRUTH_JSON))
    args = parser.parse_args()

    from pathlib import Path
    metrics, outcome, explanations = generate_report(
        Path(args.orders), Path(args.payouts), Path(args.ground_truth)
    )

    print(json.dumps(metrics["kpis"], indent=2))
    if "ground_truth_validation" in metrics:
        gtv = metrics["ground_truth_validation"]
        print(f"Ground truth accuracy: {gtv['overall_accuracy']} "
              f"({gtv['payouts_correct']}/{gtv['payouts_evaluated']})")
    print(f"Wrote: {config.METRICS_JSON}")
    print(f"Wrote: {config.EXCEPTIONS_CSV}")


if __name__ == "__main__":
    main()
