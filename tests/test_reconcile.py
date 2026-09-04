"""
Tests 1-9 from the project plan, exercising reconcile.py directly against
hand-built orders/payouts (no CSV I/O involved).
"""

from datetime import date, timedelta

from helpers import D0, make_order, make_payout

import config
from reconcile import find_best_subset, reconcile


# --------------------------------------------------------------------------
# TEST 1 -- EXACT
# --------------------------------------------------------------------------
def test_exact_match():
    orders = [
        make_order("O1", "S1", 100, D0),
        make_order("O2", "S1", 200, D0),
        make_order("O3", "S1", 300, D0),
    ]
    payouts = [make_payout("P1", "S1", 300, D0)]

    outcome = reconcile(orders, payouts)
    result = outcome["results"][0]

    assert result["status"] == "MATCHED"
    assert result["delta_paise"] == 0
    assert result["matched_total"] == 300


# --------------------------------------------------------------------------
# TEST 2 -- MULTI-ORDER
# --------------------------------------------------------------------------
def test_multi_order_from_spec():
    """Literal numbers from the project plan: orders 100/150/250, payout 250.
    (Note: 250 is also a single order here, so either a 1- or 2-order subset
    legitimately explains it -- the assertion only requires an exact match,
    per the spec. test_multi_order_requires_combination below proves the
    engine can combine multiple orders when no single order will do.)
    """
    orders = [
        make_order("O1", "S1", 100, D0),
        make_order("O2", "S1", 150, D0),
        make_order("O3", "S1", 250, D0),
    ]
    payouts = [make_payout("P1", "S1", 250, D0)]

    outcome = reconcile(orders, payouts)
    result = outcome["results"][0]

    assert result["status"] == "MATCHED"
    assert result["matched_total"] == 250


def test_multi_order_requires_combination():
    """No single eligible order equals the target -- only combining two does."""
    orders = [
        make_order("O1", "S1", 100, D0),
        make_order("O2", "S1", 150, D0),
        make_order("O3", "S1", 80, D0),
    ]
    payouts = [make_payout("P1", "S1", 250, D0)]

    outcome = reconcile(orders, payouts)
    result = outcome["results"][0]

    assert result["status"] == "MATCHED"
    assert set(result["matched_order_ids"]) == {"O1", "O2"}
    assert result["matched_total"] == 250


# --------------------------------------------------------------------------
# TEST 3 -- CLOSE
# --------------------------------------------------------------------------
def test_close_match_within_tolerance():
    orders = [make_order("O1", "S1", 1000, D0)]
    payouts = [make_payout("P1", "S1", 978, D0)]

    outcome = reconcile(orders, payouts, tolerance_paise=config.TOLERANCE_PAISE)
    result = outcome["results"][0]

    assert result["status"] == "CLOSE_MATCH"
    assert round(abs(result["delta"]), 2) == 22.00
    assert result["matched_order_ids"] == ["O1"]


# --------------------------------------------------------------------------
# TEST 4 -- UNRESOLVED
# --------------------------------------------------------------------------
def test_unresolved_no_close_combination():
    orders = [
        make_order("O1", "S1", 100, D0),
        make_order("O2", "S1", 200, D0),
        make_order("O3", "S1", 300, D0),
    ]
    payouts = [make_payout("P1", "S1", 9999, D0)]

    outcome = reconcile(orders, payouts)
    result = outcome["results"][0]

    assert result["status"] == "UNRESOLVED"
    # Best candidate should be all three orders (600), still nowhere close.
    assert result["matched_total"] == 600


# --------------------------------------------------------------------------
# TEST 5 -- ORDER REUSE
# --------------------------------------------------------------------------
def test_order_cannot_fund_two_payouts():
    orders = [make_order("O1", "S1", 500, D0)]
    payouts = [
        make_payout("P1", "S1", 500, D0),
        make_payout("P2", "S1", 500, D0 + timedelta(days=1)),
    ]

    outcome = reconcile(orders, payouts)
    results_by_id = {r["payout_id"]: r for r in outcome["results"]}

    matched_statuses = [r["status"] for r in results_by_id.values()]
    assert matched_statuses.count("MATCHED") == 1
    assert results_by_id["P1"]["status"] == "MATCHED"
    assert results_by_id["P1"]["matched_order_ids"] == ["O1"]
    # P2 can no longer see O1 -- it's already allocated.
    assert "O1" not in results_by_id["P2"]["eligible_order_ids"]
    assert results_by_id["P2"]["status"] == "UNRESOLVED"


# --------------------------------------------------------------------------
# TEST 6 -- SELLER ISOLATION
# --------------------------------------------------------------------------
def test_seller_isolation():
    orders = [make_order("O1", "SELLER_A", 500, D0)]
    payouts = [make_payout("P1", "SELLER_B", 500, D0)]

    outcome = reconcile(orders, payouts)
    result = outcome["results"][0]

    assert result["status"] == "UNRESOLVED"
    assert result["matched_order_ids"] == []
    assert "O1" not in result["eligible_order_ids"]
    # And O1 (seller A's order) should show up as orphaned, not stolen by seller B.
    assert "O1" in outcome["orphaned_order_ids"]


# --------------------------------------------------------------------------
# TEST 7 -- ORPHANED ORDERS
# --------------------------------------------------------------------------
def test_orphaned_order_detected():
    orders = [
        make_order("O1", "S1", 100, D0),
        make_order("O2", "S1", 999, D0),  # never claimed by any payout
    ]
    payouts = [make_payout("P1", "S1", 100, D0)]

    outcome = reconcile(orders, payouts)

    assert outcome["orphaned_order_ids"] == ["O2"]


# --------------------------------------------------------------------------
# TEST 8 -- DATE WINDOW
# --------------------------------------------------------------------------
def test_order_after_payout_date_is_ineligible():
    orders = [make_order("O1", "S1", 100, D0 + timedelta(days=5))]
    payouts = [make_payout("P1", "S1", 100, D0)]  # payout predates the order

    outcome = reconcile(orders, payouts)
    result = outcome["results"][0]

    assert "O1" not in result["eligible_order_ids"]
    assert result["status"] == "UNRESOLVED"


def test_order_too_far_before_payout_date_is_ineligible():
    too_old = D0
    payout_date = D0 + timedelta(days=config.ELIGIBILITY_WINDOW_DAYS + 5)
    orders = [make_order("O1", "S1", 100, too_old)]
    payouts = [make_payout("P1", "S1", 100, payout_date)]

    outcome = reconcile(orders, payouts)
    result = outcome["results"][0]

    assert "O1" not in result["eligible_order_ids"]
    assert result["status"] == "UNRESOLVED"


def test_order_within_window_is_eligible():
    within = D0 + timedelta(days=3)
    payout_date = D0 + timedelta(days=5)
    orders = [make_order("O1", "S1", 100, within)]
    payouts = [make_payout("P1", "S1", 100, payout_date)]

    outcome = reconcile(orders, payouts)
    result = outcome["results"][0]

    assert result["status"] == "MATCHED"


# --------------------------------------------------------------------------
# TEST 9 -- PAISA ACCURACY
# --------------------------------------------------------------------------
def test_integer_paise_avoids_float_drift():
    """0.1 + 0.2 != 0.3 in IEEE-754 -- prove the engine is immune to this
    by using amounts that are classic float-drift triggers."""
    orders = [
        make_order("O1", "S1", "19.99", D0),
        make_order("O2", "S1", "0.01", D0),
        make_order("O3", "S1", "10.10", D0),
        make_order("O4", "S1", "0.20", D0),
    ]
    payouts = [make_payout("P1", "S1", "20.00", D0)]

    outcome = reconcile(orders, payouts)
    result = outcome["results"][0]

    assert result["status"] == "MATCHED"
    assert result["delta_paise"] == 0
    assert set(result["matched_order_ids"]) == {"O1", "O2"}


def test_find_best_subset_uses_integer_paise_internally():
    eligible = [("A", 1999), ("B", 1)]  # 19.99 + 0.01 rupees, in paise
    outcome = find_best_subset(eligible, target_paise=2000, tolerance_paise=0)
    assert outcome["match_kind"] == "exact"
    assert outcome["delta_paise"] == 0
