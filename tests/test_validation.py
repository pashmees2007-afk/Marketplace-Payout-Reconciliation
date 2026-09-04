"""
Phase 13 error handling: missing files, malformed CSV, missing columns,
invalid monetary values, duplicate IDs, invalid dates, empty data.
"""

import pytest

from validation import DataValidationError, load_orders, load_payouts

ORDER_HEADER = "order_id,seller_id,order_amount,commission_rate,commission_amount,refund_amount,net_payable,order_date\n"
PAYOUT_HEADER = "payout_id,seller_id,payout_amount,payout_date,utr\n"


def _write(path, text):
    path.write_text(text)
    return path


def test_missing_orders_file_raises(tmp_path):
    with pytest.raises(DataValidationError):
        load_orders(tmp_path / "does_not_exist.csv")


def test_empty_file_raises(tmp_path):
    path = _write(tmp_path / "orders.csv", "")
    with pytest.raises(DataValidationError):
        load_orders(path)


def test_missing_required_column_raises(tmp_path):
    path = _write(tmp_path / "orders.csv", "order_id,seller_id\nO1,S1\n")
    with pytest.raises(DataValidationError, match="missing required column"):
        load_orders(path)


def test_duplicate_order_id_raises(tmp_path):
    text = ORDER_HEADER + (
        "O1,S1,100.00,0.10,10.00,0.00,90.00,2026-08-01\n"
        "O1,S1,50.00,0.10,5.00,0.00,45.00,2026-08-02\n"
    )
    path = _write(tmp_path / "orders.csv", text)
    with pytest.raises(DataValidationError, match="duplicate order_id"):
        load_orders(path)


def test_invalid_monetary_value_raises(tmp_path):
    text = ORDER_HEADER + "O1,S1,not_a_number,0.10,10.00,0.00,90.00,2026-08-01\n"
    path = _write(tmp_path / "orders.csv", text)
    with pytest.raises(DataValidationError):
        load_orders(path)


def test_invalid_date_raises(tmp_path):
    text = ORDER_HEADER + "O1,S1,100.00,0.10,10.00,0.00,90.00,not-a-date\n"
    path = _write(tmp_path / "orders.csv", text)
    with pytest.raises(DataValidationError, match="invalid date"):
        load_orders(path)


def test_net_payable_arithmetic_mismatch_raises(tmp_path):
    # order_amount - commission - refund = 90.00, but net_payable claims 80.00
    text = ORDER_HEADER + "O1,S1,100.00,0.10,10.00,0.00,80.00,2026-08-01\n"
    path = _write(tmp_path / "orders.csv", text)
    with pytest.raises(DataValidationError, match="net_payable"):
        load_orders(path)


def test_empty_seller_id_raises(tmp_path):
    text = ORDER_HEADER + "O1,,100.00,0.10,10.00,0.00,90.00,2026-08-01\n"
    path = _write(tmp_path / "orders.csv", text)
    with pytest.raises(DataValidationError):
        load_orders(path)


def test_valid_orders_load_cleanly(tmp_path):
    text = ORDER_HEADER + "O1,S1,100.00,0.10,10.00,0.00,90.00,2026-08-01\n"
    path = _write(tmp_path / "orders.csv", text)
    orders = load_orders(path)
    assert len(orders) == 1
    assert orders[0]["net_payable_paise"] == 9000


def test_orders_csv_with_only_header_returns_empty_list(tmp_path):
    path = _write(tmp_path / "orders.csv", ORDER_HEADER)
    assert load_orders(path) == []


def test_duplicate_payout_id_raises(tmp_path):
    text = PAYOUT_HEADER + (
        "P1,S1,100.00,2026-08-01,UTR1\n"
        "P1,S1,200.00,2026-08-02,UTR2\n"
    )
    path = _write(tmp_path / "payouts.csv", text)
    with pytest.raises(DataValidationError, match="duplicate payout_id"):
        load_payouts(path)


def test_non_positive_payout_amount_raises(tmp_path):
    text = PAYOUT_HEADER + "P1,S1,0.00,2026-08-01,UTR1\n"
    path = _write(tmp_path / "payouts.csv", text)
    with pytest.raises(DataValidationError, match="positive"):
        load_payouts(path)


def test_valid_payouts_load_cleanly(tmp_path):
    text = PAYOUT_HEADER + "P1,S1,100.00,2026-08-01,UTR1\n"
    path = _write(tmp_path / "payouts.csv", text)
    payouts = load_payouts(path)
    assert len(payouts) == 1
    assert payouts[0]["amount_paise"] == 10000
