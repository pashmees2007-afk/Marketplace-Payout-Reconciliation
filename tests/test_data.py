"""
Sanity checks on the synthetic data generator: reproducibility and
referential integrity between orders.csv, payouts.csv, and ground_truth.json.
"""

import generate_data


def test_reproducible_with_fixed_seed():
    orders_a, payouts_a, gt_a = generate_data.generate(seed=42, num_sellers=6)
    orders_b, payouts_b, gt_b = generate_data.generate(seed=42, num_sellers=6)

    assert orders_a == orders_b
    assert payouts_a == payouts_b
    assert gt_a == gt_b


def test_different_seed_gives_different_data():
    orders_a, payouts_a, _ = generate_data.generate(seed=1, num_sellers=6)
    orders_b, payouts_b, _ = generate_data.generate(seed=2, num_sellers=6)

    assert orders_a != orders_b or payouts_a != payouts_b


def test_referential_integrity():
    orders, payouts, ground_truth = generate_data.generate(seed=42, num_sellers=10)

    order_ids = {o["order_id"] for o in orders}
    payout_ids = {p["payout_id"] for p in payouts}
    seller_ids = set(ground_truth["sellers"].keys())

    assert len(order_ids) == len(orders), "order_ids must be unique"
    assert len(payout_ids) == len(payouts), "payout_ids must be unique"

    for o in orders:
        assert o["seller_id"] in seller_ids
    for p in payouts:
        assert p["seller_id"] in seller_ids

    # Every payout in ground truth corresponds to a real generated payout.
    assert set(ground_truth["payouts"].keys()) == payout_ids

    # Every order referenced by ground truth actually exists.
    for entry in ground_truth["payouts"].values():
        for oid in entry["true_order_ids"]:
            assert oid in order_ids

    # Orphaned orders are real orders that were never assigned to a payout batch.
    orphan_ids = set(ground_truth["orphaned_order_ids"])
    assert orphan_ids.issubset(order_ids)
    used_in_batches = {
        oid
        for entry in ground_truth["payouts"].values()
        for oid in entry["true_order_ids"]
    }
    assert orphan_ids.isdisjoint(used_in_batches)


def test_net_payable_arithmetic_is_consistent():
    orders, _, _ = generate_data.generate(seed=42, num_sellers=10)
    for o in orders:
        assert o["net_payable_paise"] == o["amount_paise"] - o["commission_paise"] - o["refund_paise"]
        assert o["amount_paise"] > 0
        assert o["commission_paise"] >= 0
        assert o["refund_paise"] >= 0


def test_scenario_mix_roughly_matches_target_weights():
    _, _, ground_truth = generate_data.generate(seed=42, num_sellers=20)

    counts = {}
    for entry in ground_truth["payouts"].values():
        counts[entry["scenario"]] = counts.get(entry["scenario"], 0) + 1
    total = sum(counts.values())

    # Loose bounds -- this is a randomized generator, not a fixed ratio.
    # exact_match should clearly dominate, and every scenario should be present.
    assert counts.get("exact_match", 0) / total > 0.5
    assert counts.get("close_match", 0) > 0
    assert counts.get("timing_mismatch", 0) > 0
    assert counts.get("unresolved", 0) > 0


def test_orders_written_and_read_round_trip(tmp_path):
    orders, payouts, ground_truth = generate_data.generate(seed=42, num_sellers=5)

    generate_data.write_orders_csv(orders, tmp_path / "orders.csv")
    generate_data.write_payouts_csv(payouts, tmp_path / "payouts.csv")
    generate_data.write_ground_truth_json(ground_truth, tmp_path / "ground_truth.json")

    from validation import load_orders, load_payouts

    loaded_orders = load_orders(tmp_path / "orders.csv")
    loaded_payouts = load_payouts(tmp_path / "payouts.csv")

    assert len(loaded_orders) == len(orders)
    assert len(loaded_payouts) == len(payouts)

    # Paise amounts must round-trip exactly through the CSV text representation.
    orders_by_id = {o["order_id"]: o for o in orders}
    for lo in loaded_orders:
        original = orders_by_id[lo["order_id"]]
        assert lo["net_payable_paise"] == original["net_payable_paise"]
        assert lo["amount_paise"] == original["amount_paise"]
