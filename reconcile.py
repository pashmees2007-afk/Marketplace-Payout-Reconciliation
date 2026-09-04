"""
Deterministic payout <-> order reconciliation engine.

THIS IS THE FINANCIAL CORE OF THE SYSTEM. It contains no AI/LLM calls.
Given a set of orders and a set of payouts, it decides -- with plain
arithmetic and search, in integer paise -- whether a payout is explained
by a subset of that seller's orders. That decision is never revisited or
second-guessed by the AI layer; agent.py only narrates results this
module already produced.

Algorithm (subset-sum via itertools.combinations)
---------------------------------------------------
For a given payout, "does some subset of these orders sum to the payout
amount?" is the classic subset-sum decision problem. Per the project plan,
each seller's order batches are small (a payout typically bundles a
handful of orders from a short date window), so brute-force enumeration
of subsets is the simplest approach that is still provably correct and
fully auditable -- there's no dynamic-programming reconstruction logic
for a judge to trust blindly; you can print every combination it tried.

We search combinations of increasing size (0, 1, 2, ...). For each
combination we compute `delta = candidate_total - payout_amount`:
  - `delta == 0`               -> exact match, stop immediately.
  - `0 < |delta| <= tolerance` -> no exact match, but this may still be
                                   the best CLOSE_MATCH candidate.
  - otherwise                  -> track it only if it's the closest
                                   candidate seen so far (for UNRESOLVED
                                   reporting, so the AI has a real "best
                                   candidate" to reason about instead of
                                   nothing).

Search space guard: brute force over all subsets is O(2^n). With eligible
sets kept small by seller isolation + date-window filtering, n rarely
exceeds ~10-12 in this dataset. As a safety net for pathological inputs,
if the eligible set exceeds FULL_SEARCH_MAX_ELIGIBLE we cap the maximum
subset size searched (CAPPED_SUBSET_SIZE) rather than exploring every
subset -- this trades a small chance of missing an exact match that
requires an unusually large number of orders for a hard runtime bound,
and is recorded on the result so it's never a silent trade-off.

Two-pass allocation per seller
-------------------------------
A single left-to-right pass has a subtle failure mode: payout X (which
only has a CLOSE_MATCH available) might get allocated a "best candidate"
subset that happens to include an order that payout Y -- processed right
after it -- actually needed for a genuine EXACT match. A fuzzy match
should never be allowed to consume an order out from under a later exact
match. So each seller's payouts are resolved in two passes:

  Pass A: scan all of this seller's payouts in chronological order and
          lock in only unambiguous EXACT matches, allocating their orders
          immediately.
  Pass B: scan whatever payouts remain (against the now-smaller pool left
          by Pass A) and resolve them as CLOSE_MATCH or UNRESOLVED.

This keeps the whole engine deterministic (no ordering ambiguity within a
pass -- ties are impossible for pass A since delta==0 is exact) while
guaranteeing certain matches are never sacrificed for uncertain ones.
"""

import argparse
import itertools
import json
from datetime import timedelta

import config
from money import paise_to_float
from validation import load_orders, load_payouts, DataValidationError

STATUS_MATCHED = "MATCHED"
STATUS_CLOSE_MATCH = "CLOSE_MATCH"
STATUS_UNRESOLVED = "UNRESOLVED"


def _max_subset_size(n_eligible: int) -> int:
    if n_eligible <= config.FULL_SEARCH_MAX_ELIGIBLE:
        return n_eligible
    return min(n_eligible, config.CAPPED_SUBSET_SIZE)


def find_best_subset(eligible: list, target_paise: int, tolerance_paise: int):
    """Search for a subset of `eligible` (list of (order_id, net_payable_paise))
    whose sum best explains `target_paise`.

    Returns a dict:
      {
        "match_kind": "exact" | "close" | "none",
        "order_ids": [...],       # the winning / best-candidate subset
        "total_paise": int,
        "delta_paise": int,       # candidate_total - target  (0 for exact)
        "search_capped": bool,    # True if we limited subset size for size
      }

    `eligible` with zero orders is valid input (returns "none", empty subset).
    """
    n = len(eligible)
    max_size = _max_subset_size(n)
    search_capped = max_size < n

    best_delta = None
    best_ids = []
    best_total = 0

    for size in range(0, max_size + 1):
        for combo in itertools.combinations(eligible, size):
            total = sum(amt for _, amt in combo)
            delta = total - target_paise

            if delta == 0:
                return {
                    "match_kind": "exact",
                    "order_ids": [oid for oid, _ in combo],
                    "total_paise": total,
                    "delta_paise": 0,
                    "search_capped": search_capped,
                }

            if best_delta is None or abs(delta) < abs(best_delta):
                best_delta = delta
                best_ids = [oid for oid, _ in combo]
                best_total = total

    if best_delta is None:
        # eligible was empty
        return {
            "match_kind": "none",
            "order_ids": [],
            "total_paise": 0,
            "delta_paise": None,
            "search_capped": search_capped,
        }

    match_kind = "close" if abs(best_delta) <= tolerance_paise else "none"
    return {
        "match_kind": match_kind,
        "order_ids": best_ids,
        "total_paise": best_total,
        "delta_paise": best_delta,
        "search_capped": search_capped,
    }


def _eligible_orders(orders_by_seller: dict, seller_id: str, payout_date, allocated: set):
    """Orders for this seller that are: unallocated, dated on/before the
    payout date, and within the eligibility lookback window. This is what
    enforces seller isolation and date-window filtering.
    """
    window_start = payout_date - timedelta(days=config.ELIGIBILITY_WINDOW_DAYS)
    result = []
    for o in orders_by_seller.get(seller_id, []):
        if o["order_id"] in allocated:
            continue
        if o["order_date"] > payout_date:
            continue
        if o["order_date"] < window_start:
            continue
        result.append(o)
    return result


def reconcile(orders: list, payouts: list, tolerance_paise: int = None):
    """Run the full deterministic reconciliation pass.

    Returns a dict:
      {
        "results": [ per-payout result dicts, in the order processed ],
        "orphaned_order_ids": [ order_ids never allocated to any payout ],
      }
    """
    tolerance_paise = config.TOLERANCE_PAISE if tolerance_paise is None else tolerance_paise

    orders_by_seller = {}
    all_order_ids = set()
    for o in orders:
        orders_by_seller.setdefault(o["seller_id"], []).append(o)
        all_order_ids.add(o["order_id"])

    # Process payouts chronologically *within each seller* so an earlier
    # payout gets first claim on orders over a later one (prevents a later
    # payout from "stealing" orders that should settle an earlier payout).
    payouts_by_seller = {}
    for p in payouts:
        payouts_by_seller.setdefault(p["seller_id"], []).append(p)
    for seller_payouts in payouts_by_seller.values():
        seller_payouts.sort(key=lambda p: (p["payout_date"], p["payout_id"]))

    allocated_by_seller = {sid: set() for sid in orders_by_seller}

    # Preserve overall payout_id order for stable, readable output.
    ordered_payouts = sorted(payouts, key=lambda p: p["payout_id"])
    result_by_id = {}

    for seller_id, seller_payouts in payouts_by_seller.items():
        allocated = allocated_by_seller.setdefault(seller_id, set())
        order_lookup = {o["order_id"]: o for o in orders_by_seller.get(seller_id, [])}
        resolved_ids = set()

        def _resolve(p, status, search, eligible_orders):
            matched_orders = [order_lookup[oid] for oid in search["order_ids"] if oid in order_lookup]
            result_by_id[p["payout_id"]] = {
                "payout_id": p["payout_id"],
                "seller_id": seller_id,
                "payout_date": p["payout_date"].isoformat(),
                "payout_amount_paise": p["amount_paise"],
                "payout_amount": paise_to_float(p["amount_paise"]),
                "utr": p["utr"],
                "status": status,
                "matched_order_ids": search["order_ids"],
                "matched_total_paise": search["total_paise"],
                "matched_total": paise_to_float(search["total_paise"]),
                "delta_paise": search["delta_paise"],
                "delta": None if search["delta_paise"] is None else paise_to_float(search["delta_paise"]),
                "tolerance_paise": tolerance_paise,
                "search_capped": search["search_capped"],
                "eligible_order_ids": [o["order_id"] for o in eligible_orders],
                "matched_orders": [
                    {
                        "order_id": o["order_id"],
                        "order_amount": paise_to_float(o["amount_paise"]),
                        "commission_amount": paise_to_float(o["commission_paise"]),
                        "refund_amount": paise_to_float(o["refund_paise"]),
                        "net_payable": paise_to_float(o["net_payable_paise"]),
                        "order_date": o["order_date"].isoformat(),
                    }
                    for o in matched_orders
                ],
            }
            resolved_ids.add(p["payout_id"])

        # Pass A: lock in unambiguous exact matches first (see module
        # docstring). A fuzzy CLOSE_MATCH must never be allowed to consume
        # an order that a later exact match genuinely needs.
        for p in seller_payouts:
            eligible_orders = _eligible_orders(orders_by_seller, seller_id, p["payout_date"], allocated)
            eligible_pairs = [(o["order_id"], o["net_payable_paise"]) for o in eligible_orders]
            search = find_best_subset(eligible_pairs, p["amount_paise"], tolerance_paise)
            if search["match_kind"] == "exact":
                allocated.update(search["order_ids"])
                _resolve(p, STATUS_MATCHED, search, eligible_orders)

        # Pass B: everything left over, searched against the pool as it
        # stands after every exact match has already claimed its orders.
        for p in seller_payouts:
            if p["payout_id"] in resolved_ids:
                continue
            eligible_orders = _eligible_orders(orders_by_seller, seller_id, p["payout_date"], allocated)
            eligible_pairs = [(o["order_id"], o["net_payable_paise"]) for o in eligible_orders]
            search = find_best_subset(eligible_pairs, p["amount_paise"], tolerance_paise)

            if search["match_kind"] == "exact":
                status = STATUS_MATCHED
            elif search["match_kind"] == "close":
                status = STATUS_CLOSE_MATCH
            else:
                status = STATUS_UNRESOLVED

            if status in (STATUS_MATCHED, STATUS_CLOSE_MATCH):
                allocated.update(search["order_ids"])

            _resolve(p, status, search, eligible_orders)

    # Re-order to match input payout_id ordering for stable, readable output.
    results = [result_by_id[p["payout_id"]] for p in ordered_payouts]

    all_allocated = set()
    for allocated in allocated_by_seller.values():
        all_allocated.update(allocated)
    orphaned_order_ids = sorted(all_order_ids - all_allocated)

    return {"results": results, "orphaned_order_ids": orphaned_order_ids}


def run_reconciliation(orders_path=None, payouts_path=None, tolerance_paise: int = None):
    """Load, validate, and reconcile. Raises DataValidationError on bad input."""
    orders_path = orders_path or config.ORDERS_CSV
    payouts_path = payouts_path or config.PAYOUTS_CSV

    orders = load_orders(orders_path)
    payouts = load_payouts(payouts_path)

    if not orders:
        raise DataValidationError(f"No orders found in {orders_path}; cannot reconcile.")
    if not payouts:
        raise DataValidationError(f"No payouts found in {payouts_path}; cannot reconcile.")

    return reconcile(orders, payouts, tolerance_paise=tolerance_paise)


def main():
    parser = argparse.ArgumentParser(description="Run deterministic payout reconciliation.")
    parser.add_argument("--orders", type=str, default=str(config.ORDERS_CSV))
    parser.add_argument("--payouts", type=str, default=str(config.PAYOUTS_CSV))
    parser.add_argument("--tolerance-paise", type=int, default=config.TOLERANCE_PAISE)
    parser.add_argument("--out", type=str, default=str(config.RECONCILIATION_RESULTS_JSON))
    args = parser.parse_args()

    outcome = run_reconciliation(args.orders, args.payouts, tolerance_paise=args.tolerance_paise)

    from pathlib import Path
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(outcome, f, indent=2)

    counts = {}
    for r in outcome["results"]:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print(f"Reconciled {len(outcome['results'])} payouts: {counts}")
    print(f"Orphaned orders: {len(outcome['orphaned_order_ids'])}")
    print(f"Wrote: {out_path}")


if __name__ == "__main__":
    main()
