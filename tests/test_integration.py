"""
Integration tests for the glue/I-O code that ties the pure functions
together: report.generate_report(), report.write_exceptions_csv(),
report.write_metrics_json(), reconcile.run_reconciliation(), and run.py's
end-to-end main(). These are exactly the functions Phase 15 of the project
plan says to "actually run" -- unlike the rest of the suite, which exercises
pure computation, these tests exercise real file I/O against tmp_path so a
regression in path handling, CSV writing, or wiring between modules is
caught by `pytest` and not just by manually running the pipeline.
"""

import csv
import json
import sys
from datetime import timedelta

import pytest
from helpers import D0, make_order, make_payout

import config
import generate_data
import report
import run as run_module
from reconcile import run_reconciliation
from validation import DataValidationError


def _write_dataset(data_dir, seed=7, num_sellers=4):
    orders, payouts, ground_truth = generate_data.generate(seed=seed, num_sellers=num_sellers)
    generate_data.write_orders_csv(orders, data_dir / "orders.csv")
    generate_data.write_payouts_csv(payouts, data_dir / "payouts.csv")
    generate_data.write_ground_truth_json(ground_truth, data_dir / "ground_truth.json")
    return orders, payouts, ground_truth


# --------------------------------------------------------------------------
# reconcile.run_reconciliation
# --------------------------------------------------------------------------

def test_run_reconciliation_loads_from_path_when_not_preloaded(tmp_path):
    _write_dataset(tmp_path)
    outcome = run_reconciliation(tmp_path / "orders.csv", tmp_path / "payouts.csv")
    assert len(outcome["results"]) > 0
    assert all(r["status"] in ("MATCHED", "CLOSE_MATCH", "UNRESOLVED") for r in outcome["results"])


def test_run_reconciliation_uses_preloaded_data_instead_of_reloading(tmp_path):
    """Proves the reload-skip path is actually exercised, not just present:
    the CSVs on disk are a decoy that would produce a different result if
    ever read; only the explicitly passed-in orders/payouts should matter.
    """
    decoy_orders = [make_order("DECOY1", "S1", 999, D0)]
    decoy_payouts = [make_payout("DECOY-P", "S1", 999, D0)]
    generate_data.write_orders_csv(
        [{"order_id": "DECOY1", "seller_id": "S1", "amount_paise": 99900,
          "commission_rate": 0.0, "commission_paise": 0, "refund_paise": 0,
          "net_payable_paise": 99900, "order_date": D0}],
        tmp_path / "orders.csv",
    )
    generate_data.write_payouts_csv(
        [{"payout_id": "DECOY-P", "seller_id": "S1", "amount_paise": 99900,
          "payout_date": D0, "utr": "UTR0"}],
        tmp_path / "payouts.csv",
    )

    real_orders = [make_order("REAL1", "S9", 500, D0)]
    real_payouts = [make_payout("REAL-P", "S9", 500, D0)]

    outcome = run_reconciliation(
        tmp_path / "orders.csv", tmp_path / "payouts.csv",
        orders=real_orders, payouts=real_payouts,
    )

    assert [r["payout_id"] for r in outcome["results"]] == ["REAL-P"]
    assert outcome["results"][0]["status"] == "MATCHED"


def test_run_reconciliation_rejects_empty_orders(tmp_path):
    (tmp_path / "orders.csv").write_text(
        "order_id,seller_id,order_amount,commission_rate,commission_amount,refund_amount,net_payable,order_date\n"
    )
    generate_data.write_payouts_csv(
        [{"payout_id": "P1", "seller_id": "S1", "amount_paise": 100,
          "payout_date": D0, "utr": "UTR0"}],
        tmp_path / "payouts.csv",
    )
    with pytest.raises(DataValidationError, match="No orders found"):
        run_reconciliation(tmp_path / "orders.csv", tmp_path / "payouts.csv")


# --------------------------------------------------------------------------
# report.generate_report (full integration: CSVs on disk -> metrics.json,
# exceptions.csv, reconciliation_results.json)
# --------------------------------------------------------------------------

def test_generate_report_writes_all_output_files(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    output_dir = tmp_path / "output"
    data_dir.mkdir()
    monkeypatch.setattr(config, "OUTPUT_DIR", output_dir)
    monkeypatch.setattr(config, "RECONCILIATION_RESULTS_JSON", output_dir / "reconciliation_results.json")
    monkeypatch.setattr(config, "METRICS_JSON", output_dir / "metrics.json")
    monkeypatch.setattr(config, "EXCEPTIONS_CSV", output_dir / "exceptions.csv")
    monkeypatch.setattr(config, "AUDIT_LOG_JSONL", output_dir / "audit_log.jsonl")
    monkeypatch.setattr(config, "LLM_MODE", "mock")

    orders, payouts, ground_truth = _write_dataset(data_dir)

    metrics, outcome, explanations = report.generate_report(
        data_dir / "orders.csv", data_dir / "payouts.csv", data_dir / "ground_truth.json"
    )

    assert (output_dir / "reconciliation_results.json").exists()
    assert (output_dir / "metrics.json").exists()
    assert (output_dir / "exceptions.csv").exists()

    with open(output_dir / "metrics.json") as f:
        on_disk_metrics = json.load(f)
    assert on_disk_metrics == metrics
    assert metrics["totals"]["total_orders"] == len(orders)
    assert metrics["totals"]["total_payouts"] == len(payouts)
    assert "ground_truth_validation" in metrics  # ground_truth.json was present

    with open(output_dir / "reconciliation_results.json") as f:
        on_disk_outcome = json.load(f)
    assert [r["payout_id"] for r in on_disk_outcome["results"]] == [r["payout_id"] for r in outcome["results"]]

    # Every CLOSE_MATCH/UNRESOLVED result got an explanation.
    exception_ids = {r["payout_id"] for r in outcome["results"] if r["status"] in ("CLOSE_MATCH", "UNRESOLVED")}
    assert set(explanations.keys()) == exception_ids


def test_generate_report_accepts_preloaded_orders_and_payouts(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    output_dir = tmp_path / "output"
    data_dir.mkdir()
    monkeypatch.setattr(config, "OUTPUT_DIR", output_dir)
    monkeypatch.setattr(config, "RECONCILIATION_RESULTS_JSON", output_dir / "reconciliation_results.json")
    monkeypatch.setattr(config, "METRICS_JSON", output_dir / "metrics.json")
    monkeypatch.setattr(config, "EXCEPTIONS_CSV", output_dir / "exceptions.csv")
    monkeypatch.setattr(config, "AUDIT_LOG_JSONL", output_dir / "audit_log.jsonl")
    monkeypatch.setattr(config, "LLM_MODE", "mock")

    orders, payouts, ground_truth = _write_dataset(data_dir)

    metrics, outcome, explanations = report.generate_report(
        data_dir / "orders.csv", data_dir / "payouts.csv", data_dir / "ground_truth.json",
        orders=orders, payouts=payouts,
    )
    assert metrics["totals"]["total_orders"] == len(orders)
    assert len(outcome["results"]) == len(payouts)


def test_generate_report_without_ground_truth_omits_validation(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    output_dir = tmp_path / "output"
    data_dir.mkdir()
    monkeypatch.setattr(config, "OUTPUT_DIR", output_dir)
    monkeypatch.setattr(config, "RECONCILIATION_RESULTS_JSON", output_dir / "reconciliation_results.json")
    monkeypatch.setattr(config, "METRICS_JSON", output_dir / "metrics.json")
    monkeypatch.setattr(config, "EXCEPTIONS_CSV", output_dir / "exceptions.csv")
    monkeypatch.setattr(config, "AUDIT_LOG_JSONL", output_dir / "audit_log.jsonl")
    monkeypatch.setattr(config, "LLM_MODE", "mock")

    _write_dataset(data_dir)
    missing_gt_path = data_dir / "does_not_exist.json"

    metrics, outcome, explanations = report.generate_report(
        data_dir / "orders.csv", data_dir / "payouts.csv", missing_gt_path
    )
    assert "ground_truth_validation" not in metrics


# --------------------------------------------------------------------------
# report.write_exceptions_csv / write_metrics_json (direct, isolated)
# --------------------------------------------------------------------------

def test_write_exceptions_csv_covers_unresolved_and_orphaned(tmp_path):
    orders_by_id = {
        "O1": {"seller_id": "S1", "net_payable_paise": 50000, "order_date": D0},
    }
    outcome = {
        "results": [
            {
                "payout_id": "P1", "seller_id": "S1", "payout_amount": 500.0,
                "payout_date": D0.isoformat(), "delta": -12.5, "status": "UNRESOLVED",
                "matched_order_ids": ["O1"],
            },
            {
                "payout_id": "P2", "seller_id": "S1", "payout_amount": 200.0,
                "payout_date": D0.isoformat(), "delta": 0.0, "status": "MATCHED",
                "matched_order_ids": ["O1"],
            },
        ],
        "orphaned_order_ids": ["O1"],
    }
    explanations = {"P1": {"explanation": "POSSIBLE EXPLANATION: test."}}

    out_path = tmp_path / "exceptions.csv"
    report.write_exceptions_csv(outcome, orders_by_id, explanations, out_path)

    with open(out_path, newline="") as f:
        rows = list(csv.DictReader(f))

    assert len(rows) == 2  # the UNRESOLVED payout + the orphaned order; MATCHED is excluded
    types = {r["type"] for r in rows}
    assert types == {"UNRESOLVED_PAYOUT", "ORPHANED_ORDER"}

    unresolved_row = next(r for r in rows if r["type"] == "UNRESOLVED_PAYOUT")
    assert unresolved_row["id"] == "P1"
    assert unresolved_row["ai_explanation"] == "POSSIBLE EXPLANATION: test."

    orphan_row = next(r for r in rows if r["type"] == "ORPHANED_ORDER")
    assert orphan_row["id"] == "O1"
    assert orphan_row["ai_explanation"]  # auto-generated, non-empty


def test_write_metrics_json_round_trips(tmp_path):
    metrics = {"kpis": {"match_rate_by_value": 0.9}, "totals": {"total_orders": 5}}
    out_path = tmp_path / "metrics.json"
    report.write_metrics_json(metrics, out_path)
    with open(out_path) as f:
        assert json.load(f) == metrics


# --------------------------------------------------------------------------
# run.py end-to-end (the actual `python run.py` entry point)
# --------------------------------------------------------------------------

def _patch_all_config_paths(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    output_dir = tmp_path / "output"
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "OUTPUT_DIR", output_dir)
    monkeypatch.setattr(config, "ORDERS_CSV", data_dir / "orders.csv")
    monkeypatch.setattr(config, "PAYOUTS_CSV", data_dir / "payouts.csv")
    monkeypatch.setattr(config, "GROUND_TRUTH_JSON", data_dir / "ground_truth.json")
    monkeypatch.setattr(config, "RECONCILIATION_RESULTS_JSON", output_dir / "reconciliation_results.json")
    monkeypatch.setattr(config, "METRICS_JSON", output_dir / "metrics.json")
    monkeypatch.setattr(config, "EXCEPTIONS_CSV", output_dir / "exceptions.csv")
    monkeypatch.setattr(config, "AUDIT_LOG_JSONL", output_dir / "audit_log.jsonl")
    monkeypatch.setattr(config, "LLM_MODE", "mock")
    return data_dir, output_dir


def test_run_main_end_to_end(tmp_path, monkeypatch, capsys):
    data_dir, output_dir = _patch_all_config_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "argv", ["run.py", "--seed", "3", "--sellers", "5"])

    run_module.main()

    assert (data_dir / "orders.csv").exists()
    assert (data_dir / "payouts.csv").exists()
    assert (data_dir / "ground_truth.json").exists()
    assert (output_dir / "metrics.json").exists()
    assert (output_dir / "exceptions.csv").exists()
    assert (output_dir / "audit_log.jsonl").exists()

    captured = capsys.readouterr()
    assert "Match rate by value" in captured.out
    assert "Ground-truth accuracy" in captured.out


def test_run_main_skip_generate_reuses_existing_data(tmp_path, monkeypatch, capsys):
    data_dir, output_dir = _patch_all_config_paths(monkeypatch, tmp_path)
    data_dir.mkdir()
    orders, payouts, ground_truth = _write_dataset(data_dir, seed=11, num_sellers=5)

    monkeypatch.setattr(sys, "argv", ["run.py", "--skip-generate"])
    run_module.main()

    with open(output_dir / "metrics.json") as f:
        metrics = json.load(f)
    assert metrics["totals"]["total_orders"] == len(orders)
    assert metrics["totals"]["total_payouts"] == len(payouts)

    captured = capsys.readouterr()
    assert "Skipped data generation" in captured.out


def test_run_main_exits_nonzero_on_validation_failure(tmp_path, monkeypatch, capsys):
    data_dir, output_dir = _patch_all_config_paths(monkeypatch, tmp_path)
    data_dir.mkdir()
    # Malformed orders.csv: missing required columns entirely.
    (data_dir / "orders.csv").write_text("not,a,valid,header\n1,2,3,4\n")
    (data_dir / "payouts.csv").write_text(
        "payout_id,seller_id,payout_amount,payout_date,utr\nP1,S1,100.00,2026-08-01,UTR1\n"
    )

    monkeypatch.setattr(sys, "argv", ["run.py", "--skip-generate"])
    with pytest.raises(SystemExit) as exc_info:
        run_module.main()

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "DATA VALIDATION FAILED" in captured.err
