"""
Integer-paise money helpers.

Every financial computation in this project (generation, reconciliation,
reporting) is done in **integer paise**, never in floating point rupees.

Why: binary floating point cannot represent most decimal fractions exactly
(0.1 + 0.2 != 0.3 in IEEE-754 double precision). For a system whose entire
job is deciding whether a set of numbers sums *exactly* to another number,
that rounding noise is not a cosmetic bug -- it silently turns real exact
matches into false "CLOSE_MATCH"/"UNRESOLVED" results, or worse, turns a
mismatched payout into a false exact match. Integers under addition and
subtraction have no such error, so all comparisons (`==`, subset sums,
tolerance checks) are exact and reproducible.

Rupee amounts only exist at the I/O boundary: reading a CSV column or
formatting a number for a report/UI. `Decimal` is used for that boundary
conversion (never `float`) because Decimal parses the *textual* value of
a rupee amount ("1234.56") exactly, with no binary rounding step.
"""

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


class InvalidMoneyValue(ValueError):
    """Raised when a value cannot be safely interpreted as a rupee amount."""


def rupees_to_paise(value) -> int:
    """Convert a rupee amount (str, int, float, or Decimal) to integer paise.

    Uses Decimal on the *string* representation so values already read from
    CSV text convert exactly, with no intermediate binary-float rounding.
    """
    try:
        as_decimal = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise InvalidMoneyValue(f"Cannot parse monetary value: {value!r}") from exc

    paise = (as_decimal * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(paise)


def paise_to_rupees_str(paise: int) -> str:
    """Format integer paise as a fixed 2-decimal rupee string for CSV/reports."""
    if not isinstance(paise, int):
        raise InvalidMoneyValue(f"paise value must be int, got {type(paise)}")
    sign = "-" if paise < 0 else ""
    paise = abs(paise)
    rupees, remainder = divmod(paise, 100)
    return f"{sign}{rupees}.{remainder:02d}"


def paise_to_float(paise: int) -> float:
    """Convert paise to a float rupee value, for display/plotting ONLY.

    Never feed this back into a financial comparison -- use paise ints.
    """
    return float(Decimal(paise) / 100)


def format_inr(paise: int) -> str:
    """Human-readable signed INR string with thousands separators, e.g. '₹1,234.56'."""
    sign = "-" if paise < 0 else ""
    rupees_str = paise_to_rupees_str(abs(paise))
    whole, frac = rupees_str.split(".")
    # Indian digit grouping: last 3 digits, then groups of 2.
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return f"{sign}₹{whole}.{frac}"
