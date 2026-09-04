"""
Input loading and validation for orders.csv / payouts.csv.

Financial software must never silently swallow bad input. Every function
here fails loudly (raises) on structurally broken data -- missing files,
missing columns, duplicate IDs, unparseable money/dates -- and returns
clean, paise-denominated, date-typed records on success.
"""

import csv
from datetime import datetime
from pathlib import Path

from money import rupees_to_paise, InvalidMoneyValue

ORDER_COLUMNS = [
    "order_id", "seller_id", "order_amount", "commission_rate",
    "commission_amount", "refund_amount", "net_payable", "order_date",
]
PAYOUT_COLUMNS = ["payout_id", "seller_id", "payout_amount", "payout_date", "utr"]


class DataValidationError(ValueError):
    """Raised when input data fails structural or financial validation."""


def _parse_date(value: str, row_context: str):
    value = (value or "").strip()
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise DataValidationError(
            f"{row_context}: invalid date {value!r}, expected YYYY-MM-DD"
        ) from exc


def _require_file(path) -> Path:
    p = Path(path)
    if not p.exists():
        raise DataValidationError(f"Required input file not found: {p}")
    if p.stat().st_size == 0:
        raise DataValidationError(f"Input file is empty: {p}")
    return p


def _require_columns(fieldnames, expected, path):
    if fieldnames is None:
        raise DataValidationError(f"{path}: file has no header row")
    missing = [c for c in expected if c not in fieldnames]
    if missing:
        raise DataValidationError(f"{path}: missing required column(s): {missing}")


def load_orders(path) -> list:
    """Load and validate orders.csv. Returns list of dicts with paise ints
    and date objects. Raises DataValidationError on any structural problem.
    """
    path = _require_file(path)
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        _require_columns(reader.fieldnames, ORDER_COLUMNS, path)
        rows = list(reader)

    if not rows:
        return []

    seen_ids = set()
    orders = []
    for i, row in enumerate(rows, start=2):  # header is line 1
        ctx = f"{path}:{i} (order_id={row.get('order_id')!r})"

        order_id = (row.get("order_id") or "").strip()
        seller_id = (row.get("seller_id") or "").strip()
        if not order_id:
            raise DataValidationError(f"{ctx}: empty order_id")
        if not seller_id:
            raise DataValidationError(f"{ctx}: empty seller_id")
        if order_id in seen_ids:
            raise DataValidationError(f"{ctx}: duplicate order_id {order_id!r}")
        seen_ids.add(order_id)

        try:
            amount_paise = rupees_to_paise(row["order_amount"])
            commission_paise = rupees_to_paise(row["commission_amount"])
            refund_paise = rupees_to_paise(row["refund_amount"])
            net_payable_paise = rupees_to_paise(row["net_payable"])
        except InvalidMoneyValue as exc:
            raise DataValidationError(f"{ctx}: {exc}") from exc

        try:
            commission_rate = float(row["commission_rate"])
        except (TypeError, ValueError) as exc:
            raise DataValidationError(
                f"{ctx}: invalid commission_rate {row.get('commission_rate')!r}"
            ) from exc

        expected_net = amount_paise - commission_paise - refund_paise
        if expected_net != net_payable_paise:
            raise DataValidationError(
                f"{ctx}: net_payable ({net_payable_paise}p) != order_amount - "
                f"commission_amount - refund_amount ({expected_net}p)"
            )

        order_date = _parse_date(row["order_date"], ctx)

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


def load_payouts(path) -> list:
    """Load and validate payouts.csv. Returns list of dicts with paise ints
    and date objects. Raises DataValidationError on any structural problem.
    """
    path = _require_file(path)
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        _require_columns(reader.fieldnames, PAYOUT_COLUMNS, path)
        rows = list(reader)

    if not rows:
        return []

    seen_ids = set()
    payouts = []
    for i, row in enumerate(rows, start=2):
        ctx = f"{path}:{i} (payout_id={row.get('payout_id')!r})"

        payout_id = (row.get("payout_id") or "").strip()
        seller_id = (row.get("seller_id") or "").strip()
        if not payout_id:
            raise DataValidationError(f"{ctx}: empty payout_id")
        if not seller_id:
            raise DataValidationError(f"{ctx}: empty seller_id")
        if payout_id in seen_ids:
            raise DataValidationError(f"{ctx}: duplicate payout_id {payout_id!r}")
        seen_ids.add(payout_id)

        try:
            amount_paise = rupees_to_paise(row["payout_amount"])
        except InvalidMoneyValue as exc:
            raise DataValidationError(f"{ctx}: {exc}") from exc

        if amount_paise <= 0:
            raise DataValidationError(f"{ctx}: payout_amount must be positive, got {amount_paise}p")

        payout_date = _parse_date(row["payout_date"], ctx)
        utr = (row.get("utr") or "").strip()

        payouts.append({
            "payout_id": payout_id,
            "seller_id": seller_id,
            "amount_paise": amount_paise,
            "payout_date": payout_date,
            "utr": utr,
        })

    return payouts
