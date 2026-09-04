"""
Synthetic marketplace data generator.

This is the source of truth for `ground_truth.json`. Payouts are built
*from* orders (never generated independently), so we always know exactly
which orders should explain each payout -- that known answer is what lets
`report.py` measure real accuracy later instead of a self-reported one.

Scenario mix (see config.SCENARIO_WEIGHTS), applied per payout batch:
  - exact_match      (~70%): payout_amount == sum(net_payable) of its orders.
  - close_match      (~15%): payout is short by a small unlogged processing
                              fee -- a real ops gap, not a random offset.
  - timing_mismatch  (~10%): one order in the batch is dated *after* the
                              payout date, simulating an order that hasn't
                              settled in the ledger yet. The payout amount
                              still reflects it, so the reconciliation
                              engine -- correctly -- won't see it as
                              eligible at reconciliation time.
  - unresolved        (~5%): a payout amount with no backing orders at all
                              (manual adjustment / chargeback reversal).

A handful of orders per seller are deliberately never placed in any
payout -- these become ORPHANED_ORDERs (money owed, not yet settled).

Uses a fixed random seed by default, so every run is byte-for-byte
reproducible unless --seed is overridden.
"""

import argparse
import csv
import json
import random
from datetime import date, timedelta

import config
from money import paise_to_rupees_str

START_DATE = date(2026, 8, 1)

SELLER_NAME_PREFIXES = [
    "Blue", "Northern", "Golden", "Silver", "Coastal", "Urban", "Metro",
    "Sunrise", "Prime", "Everest", "Rapid", "Trusted", "Fresh", "Royal",
    "Vibrant", "Classic", "Pioneer", "Summit", "Harbor", "Crown",
    "Zenith", "Falcon", "Maple", "Orbit",
]
SELLER_NAME_SUFFIXES = [
    "Traders", "Retail Co", "Marketplace", "Emporium", "Goods",
    "Bazaar", "Enterprises", "Mart", "Collective", "Supplies",
    "Ventures", "Commerce", "Outlet", "House", "Works",
]

COMMISSION_RATES = [0.05, 0.08, 0.10, 0.12, 0.15, 0.18, 0.20]


def weighted_choice(rng: random.Random, weights: dict):
    keys = list(weights.keys())
    probs = list(weights.values())
    return rng.choices(keys, weights=probs, k=1)[0]


def generate_seller_names(rng: random.Random, n: int):
    """Deterministic, non-repeating business names for the demo."""
    combos = [f"{p} {s}" for p in SELLER_NAME_PREFIXES for s in SELLER_NAME_SUFFIXES]
    rng.shuffle(combos)
    return combos[:n]


def generate_orders_for_seller(rng: random.Random, seller_id: str, order_counter: list):
    n_orders = rng.randint(config.MIN_ORDERS_PER_SELLER, config.MAX_ORDERS_PER_SELLER)
    orders = []
    for _ in range(n_orders):
        order_date = START_DATE + timedelta(days=rng.randint(0, config.ORDER_SPAN_DAYS - 1))
        amount_paise = rng.randint(10_000, 500_000)  # ₹100 - ₹5,000
        commission_rate = rng.choice(COMMISSION_RATES)
        commission_paise = round(amount_paise * commission_rate)

        refund_paise = 0
        if rng.random() < 0.12:
            max_refund = max(1000, amount_paise // 3)
            refund_paise = rng.randint(1000, max_refund)  # ₹10 - up to 1/3 of order

        net_payable_paise = amount_paise - commission_paise - refund_paise

        order_id = f"ORD-{order_counter[0]:05d}"
        order_counter[0] += 1
        orders.append({
            "order_id": order_id,
            "seller_id": seller_id,
            "amount_paise": amount_paise,
            "commission_rate": commission_rate,
            "commission_paise": commission_paise,
            "refund_paise": refund_paise,
            "net_payable_paise": net_payable_paise,
            "order_date": order_date,
        })
    return orders


def build_payouts_for_seller(rng: random.Random, seller_id: str, orders: list, payout_counter: list):
    """Bundle a seller's orders into payout batches per the scenario mix.

    Returns (payouts, ground_truth_entries, orphaned_order_ids).
    """
    # Reserve a handful of orders that will never be paid out.
    n_orphan = max(1, round(len(orders) * rng.uniform(0.05, 0.10)))
    n_orphan = min(n_orphan, max(0, len(orders) - 2))  # leave enough orders to batch
    orphan_orders = rng.sample(orders, n_orphan) if n_orphan else []
    orphan_ids = {o["order_id"] for o in orphan_orders}

    payable_orders = sorted(
        (o for o in orders if o["order_id"] not in orphan_ids),
        key=lambda o: o["order_date"],
    )

    payouts = []
    gt_entries = {}

    i = 0
    max_retries_at_index = 3
    retries = 0
    while i < len(payable_orders):
        remaining = len(payable_orders) - i
        batch_size = rng.randint(1, min(6, remaining))
        batch = payable_orders[i:i + batch_size]

        scenario = weighted_choice(rng, config.SCENARIO_WEIGHTS)

        net_sum_paise = sum(o["net_payable_paise"] for o in batch)
        last_order_date = max(o["order_date"] for o in batch)
        payout_date = last_order_date + timedelta(days=rng.choice([0, 1]))

        batch_ids = [o["order_id"] for o in batch]

        if scenario == "exact_match":
            payout_amount_paise = net_sum_paise
            note = "Payout equals the exact sum of net_payable for the listed orders."
            i += batch_size
            retries = 0

        elif scenario == "close_match":
            fee_paise = rng.randint(500, 2800)  # ₹5 - ₹28, inside default tolerance
            payout_amount_paise = net_sum_paise - fee_paise
            note = (
                f"Payout is short by an unlogged processing fee of "
                f"₹{fee_paise / 100:.2f} not present in the order ledger."
            )
            i += batch_size
            retries = 0

        elif scenario == "timing_mismatch":
            late_order = rng.choice(batch)
            # Push this order's ledger date to *after* the payout date, so
            # it will not be "visible" (eligible) at reconciliation time.
            late_order["order_date"] = payout_date + timedelta(days=rng.choice([1, 2]))
            payout_amount_paise = net_sum_paise
            note = (
                f"Payout includes order {late_order['order_id']}, which settles "
                f"in the order ledger after this payout's date (timing mismatch)."
            )
            i += batch_size
            retries = 0

        else:  # unresolved
            if retries >= max_retries_at_index:
                # Extremely unlikely, but guarantees termination: force this
                # batch through as an exact match instead of looping forever.
                payout_amount_paise = net_sum_paise
                scenario = "exact_match"
                note = "Payout equals the exact sum of net_payable for the listed orders."
                i += batch_size
                retries = 0
            else:
                payout_amount_paise = rng.randint(20_000, 300_000)
                note = (
                    "No order combination backs this payout -- simulates a manual "
                    "adjustment or chargeback reversal outside the order ledger."
                )
                batch_ids = []
                retries += 1
                # Do not advance i: these orders remain unconsumed and will be
                # re-batched (possibly under a different scenario) next loop.

        payout_id = f"PYT-{payout_counter[0]:05d}"
        payout_counter[0] += 1
        utr = f"UTR{rng.randint(10**11, 10**12 - 1)}"

        payouts.append({
            "payout_id": payout_id,
            "seller_id": seller_id,
            "amount_paise": payout_amount_paise,
            "payout_date": payout_date,
            "utr": utr,
        })
        gt_entries[payout_id] = {
            "seller_id": seller_id,
            "scenario": scenario,
            "true_order_ids": batch_ids,
            "note": note,
        }

    return payouts, gt_entries, sorted(orphan_ids)


def generate(seed: int = None, num_sellers: int = None):
    seed = config.RANDOM_SEED if seed is None else seed
    num_sellers = config.NUM_SELLERS if num_sellers is None else num_sellers
    rng = random.Random(seed)

    seller_ids = [f"SLR-{i:02d}" for i in range(1, num_sellers + 1)]
    seller_names = generate_seller_names(rng, num_sellers)

    order_counter = [1]
    payout_counter = [1]

    all_orders = []
    all_payouts = []
    ground_truth = {
        "seed": seed,
        "num_sellers": num_sellers,
        "sellers": dict(zip(seller_ids, seller_names)),
        "payouts": {},
        "orphaned_order_ids": [],
        "scenario_weights": config.SCENARIO_WEIGHTS,
    }

    for seller_id in seller_ids:
        orders = generate_orders_for_seller(rng, seller_id, order_counter)
        all_orders.extend(orders)

        payouts, gt_entries, orphan_ids = build_payouts_for_seller(
            rng, seller_id, orders, payout_counter
        )
        all_payouts.extend(payouts)
        ground_truth["payouts"].update(gt_entries)
        ground_truth["orphaned_order_ids"].extend(orphan_ids)

    all_orders.sort(key=lambda o: o["order_id"])
    all_payouts.sort(key=lambda p: p["payout_id"])
    ground_truth["orphaned_order_ids"].sort()

    return all_orders, all_payouts, ground_truth


def write_orders_csv(orders: list, path):
    fieldnames = [
        "order_id", "seller_id", "order_amount", "commission_rate",
        "commission_amount", "refund_amount", "net_payable", "order_date",
    ]
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for o in orders:
            writer.writerow({
                "order_id": o["order_id"],
                "seller_id": o["seller_id"],
                "order_amount": paise_to_rupees_str(o["amount_paise"]),
                "commission_rate": f"{o['commission_rate']:.4f}",
                "commission_amount": paise_to_rupees_str(o["commission_paise"]),
                "refund_amount": paise_to_rupees_str(o["refund_paise"]),
                "net_payable": paise_to_rupees_str(o["net_payable_paise"]),
                "order_date": o["order_date"].isoformat(),
            })


def write_payouts_csv(payouts: list, path):
    fieldnames = ["payout_id", "seller_id", "payout_amount", "payout_date", "utr"]
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for p in payouts:
            writer.writerow({
                "payout_id": p["payout_id"],
                "seller_id": p["seller_id"],
                "payout_amount": paise_to_rupees_str(p["amount_paise"]),
                "payout_date": p["payout_date"].isoformat(),
                "utr": p["utr"],
            })


def write_ground_truth_json(ground_truth: dict, path):
    with open(path, "w") as f:
        json.dump(ground_truth, f, indent=2, sort_keys=True)


def main():
    parser = argparse.ArgumentParser(description="Generate synthetic marketplace payout data.")
    parser.add_argument("--seed", type=int, default=config.RANDOM_SEED)
    parser.add_argument("--sellers", type=int, default=config.NUM_SELLERS)
    parser.add_argument("--data-dir", type=str, default=str(config.DATA_DIR))
    args = parser.parse_args()

    from pathlib import Path
    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    orders, payouts, ground_truth = generate(seed=args.seed, num_sellers=args.sellers)

    write_orders_csv(orders, data_dir / "orders.csv")
    write_payouts_csv(payouts, data_dir / "payouts.csv")
    write_ground_truth_json(ground_truth, data_dir / "ground_truth.json")

    scenario_counts = {}
    for entry in ground_truth["payouts"].values():
        scenario_counts[entry["scenario"]] = scenario_counts.get(entry["scenario"], 0) + 1

    print(f"Generated {len(orders)} orders, {len(payouts)} payouts, "
          f"{len(ground_truth['orphaned_order_ids'])} intentionally orphaned orders "
          f"across {args.sellers} sellers (seed={args.seed}).")
    print(f"Scenario mix: {scenario_counts}")
    print(f"Wrote: {data_dir / 'orders.csv'}")
    print(f"Wrote: {data_dir / 'payouts.csv'}")
    print(f"Wrote: {data_dir / 'ground_truth.json'}")


if __name__ == "__main__":
    main()
