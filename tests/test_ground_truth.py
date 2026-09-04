"""
TEST 10 -- GROUND TRUTH: run a full synthetically generated dataset through
the real reconciliation engine and score it against generate_data.py's own
ground_truth.json. This is the end-to-end honesty check: accuracy here is
computed from actual engine output, never asserted or hard-coded.
"""

import generate_data
from reconcile import reconcile
from report import compute_ground_truth_validation


def test_full_pipeline_matches_ground_truth_with_high_accuracy():
    orders, payouts, ground_truth = generate_data.generate(seed=42, num_sellers=20)

    outcome = reconcile(orders, payouts)
    validation = compute_ground_truth_validation(
        outcome["results"], outcome["orphaned_order_ids"], ground_truth
    )

    assert validation["payouts_evaluated"] == len(payouts)
    # The deterministic engine should correctly classify the overwhelming
    # majority of intentionally-generated scenarios. Not asserting 100% here
    # on purpose -- this must reflect genuine engine behavior, not a
    # self-fulfilling target within report.py.
    assert validation["overall_accuracy"] >= 0.90

    # Every scenario category should be individually well handled, not just
    # the average -- a high overall score could otherwise hide one broken
    # category.
    for scenario, bucket in validation["by_scenario"].items():
        assert bucket["accuracy"] >= 0.75, f"{scenario} accuracy too low: {bucket}"

    # Orphan detection should be meaningfully better than chance.
    assert validation["orphaned_order_recall"] >= 0.5


def test_ground_truth_reproducible_across_multiple_seeds():
    """The accuracy property should hold generally, not just for seed 42."""
    for seed in (1, 7, 100):
        orders, payouts, ground_truth = generate_data.generate(seed=seed, num_sellers=15)
        outcome = reconcile(orders, payouts)
        validation = compute_ground_truth_validation(
            outcome["results"], outcome["orphaned_order_ids"], ground_truth
        )
        assert validation["overall_accuracy"] >= 0.85, f"seed={seed}: {validation['overall_accuracy']}"
