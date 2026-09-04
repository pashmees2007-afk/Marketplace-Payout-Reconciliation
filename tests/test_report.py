"""
Tests for report.py's metrics math and ground-truth scoring. Uses hand-built
reconciliation outcomes so the expected numbers can be checked by hand.
"""

import pytest
from helpers import D0, make_order

from report import compute_ground_truth_validation, compute_metrics


def _result(payout_id, seller_id, status, amount, matched_total, delta, matched_order_ids=None):
    return {
        "payout_id": payout_id,
        "seller_id": seller_id,
        "payout_date": D0.isoformat(),
        "payout_amount_paise": round(amount * 100),
        "payout_amount": amount,
        "utr": "UTR1",
        "status": status,
        "matched_order_ids": matched_order_ids or [],
        "matched_total_paise": round(matched_total * 100),
        "matched_total": matched_total,
        "delta_paise": None if delta is None else round(delta * 100),
        "delta": delta,
        "tolerance_paise": 3000,
        "search_capped": False,
        "eligible_order_ids": [],
        "matched_orders": [],
    }


def test_compute_metrics_basic_math():
    orders = [
        make_order("O1", "S1", 100, D0),
        make_order("O2", "S1", 900, D0),  # orphaned
    ]
    payouts = [{"payout_id": "P1"}, {"payout_id": "P2"}]  # only length matters here
    outcome = {
        "results": [
            _result("P1", "S1", "MATCHED", 100, 100, 0, ["O1"]),
            _result("P2", "S1", "UNRESOLVED", 50, 0, None),
        ],
        "orphaned_order_ids": ["O2"],
    }

    metrics = compute_metrics(orders, payouts, outcome)

    assert metrics["totals"]["total_payout_value"] == 150.0
    assert metrics["totals"]["orphaned_order_count"] == 1
    assert metrics["totals"]["orphaned_order_value"] == 900.0
    assert metrics["by_status"]["MATCHED"]["count"] == 1
    assert metrics["by_status"]["UNRESOLVED"]["count"] == 1
    # match_rate_by_value = matched (100) / total (150)
    assert metrics["kpis"]["match_rate_by_value"] == round(100 / 150, 4)
    assert metrics["kpis"]["match_rate_by_count"] == 0.5


def test_compute_metrics_exposes_exact_paise_alongside_float_display_values():
    """metrics.json carries integer-paise fields too, not just floats -- so
    a downstream consumer (the dashboard) never has to reverse a float back
    into paise via round(x * 100) to format an exact amount."""
    orders = [
        make_order("O1", "S1", 100, D0),
        make_order("O2", "S1", 900, D0),  # orphaned
    ]
    payouts = [{"payout_id": "P1"}, {"payout_id": "P2"}]
    outcome = {
        "results": [
            _result("P1", "S1", "MATCHED", 100, 100, 0, ["O1"]),
            _result("P2", "S1", "UNRESOLVED", 50, 0, None),
        ],
        "orphaned_order_ids": ["O2"],
    }

    metrics = compute_metrics(orders, payouts, outcome)

    assert metrics["totals"]["total_payout_value_paise"] == 15000  # 150.00
    assert metrics["totals"]["orphaned_order_value_paise"] == 90000  # 900.00
    assert metrics["by_status"]["MATCHED"]["value_paise"] == 10000
    assert metrics["by_status"]["UNRESOLVED"]["value_paise"] == 5000
    # Paise fields must always agree exactly with their float siblings.
    from money import paise_to_float
    assert paise_to_float(metrics["totals"]["total_payout_value_paise"]) == metrics["totals"]["total_payout_value"]


def test_compute_metrics_raises_on_orphaned_order_not_in_orders():
    """compute_metrics must fail loudly, not silently under-count, if given
    an `outcome` whose orphaned_order_ids don't match the `orders` passed in
    -- e.g. a caller accidentally mixing data from two different runs."""
    orders = [make_order("O1", "S1", 100, D0)]
    payouts = [{"payout_id": "P1"}]
    outcome = {
        "results": [_result("P1", "S1", "MATCHED", 100, 100, 0, ["O1"])],
        "orphaned_order_ids": ["O_DOES_NOT_EXIST"],
    }

    with pytest.raises(ValueError, match="O_DOES_NOT_EXIST"):
        compute_metrics(orders, payouts, outcome)


def test_ground_truth_validation_scores_each_scenario_type():
    results = [
        # exact_match, correctly matched with the right orders -> correct
        _result("P1", "S1", "MATCHED", 100, 100, 0, ["O1"]),
        # exact_match, but engine only found a partial/wrong set -> incorrect
        _result("P2", "S1", "MATCHED", 100, 100, 0, ["O2"]),
        # close_match, correctly flagged CLOSE_MATCH -> correct
        _result("P3", "S1", "CLOSE_MATCH", 100, 90, 10, ["O3"]),
        # unresolved, correctly flagged UNRESOLVED -> correct
        _result("P4", "S1", "UNRESOLVED", 500, 0, None),
        # timing_mismatch, engine flagged CLOSE_MATCH -> correct (any exception flag counts)
        _result("P5", "S1", "CLOSE_MATCH", 200, 190, 10, ["O5"]),
    ]
    ground_truth = {
        "payouts": {
            "P1": {"scenario": "exact_match", "true_order_ids": ["O1"]},
            "P2": {"scenario": "exact_match", "true_order_ids": ["O1"]},  # engine used wrong order
            "P3": {"scenario": "close_match", "true_order_ids": ["O3"]},
            "P4": {"scenario": "unresolved", "true_order_ids": []},
            "P5": {"scenario": "timing_mismatch", "true_order_ids": ["O5", "O6"]},
        },
        "orphaned_order_ids": ["O9"],
    }

    validation = compute_ground_truth_validation(results, orphaned_order_ids=["O9", "O10"], ground_truth=ground_truth)

    assert validation["payouts_evaluated"] == 5
    assert validation["payouts_correct"] == 4
    assert validation["by_scenario"]["exact_match"] == {"correct": 1, "total": 2, "accuracy": 0.5}
    assert validation["by_scenario"]["close_match"]["correct"] == 1
    assert validation["by_scenario"]["unresolved"]["correct"] == 1
    assert validation["by_scenario"]["timing_mismatch"]["correct"] == 1

    # orphan ground truth = {O9}; engine flagged {O9, O10} -> 1 true positive
    assert validation["orphaned_order_true_positives"] == 1
    assert validation["orphaned_order_precision"] == 0.5   # 1/2
    assert validation["orphaned_order_recall"] == 1.0       # 1/1
