from datetime import date

from money import rupees_to_paise


def make_order(order_id, seller_id, net_payable, order_date, amount=None, commission=0, refund=0):
    """Build an internal order dict (the shape validation.load_orders() returns)
    directly from rupee amounts, for use in unit tests.
    """
    net_paise = rupees_to_paise(net_payable)
    amount_paise = rupees_to_paise(amount) if amount is not None else net_paise
    return {
        "order_id": order_id,
        "seller_id": seller_id,
        "amount_paise": amount_paise,
        "commission_rate": 0.0,
        "commission_paise": rupees_to_paise(commission),
        "refund_paise": rupees_to_paise(refund),
        "net_payable_paise": net_paise,
        "order_date": order_date,
    }


def make_payout(payout_id, seller_id, amount, payout_date, utr="UTR0000000000"):
    return {
        "payout_id": payout_id,
        "seller_id": seller_id,
        "amount_paise": rupees_to_paise(amount),
        "payout_date": payout_date,
        "utr": utr,
    }


D0 = date(2026, 8, 1)
