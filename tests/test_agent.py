"""
Tests for the AI explanation layer: mock mode determinism, the FACT vs
POSSIBLE EXPLANATION contract, MATCHED payouts never triggering an AI call,
graceful Q&A behavior, and audit logging (redirected to tmp_path so test
runs never pollute the real output/audit_log.jsonl).
"""

from helpers import D0

import agent
import config


def _close_match_result():
    return {
        "payout_id": "PYT-00099",
        "seller_id": "SLR-01",
        "payout_date": D0.isoformat(),
        "payout_amount_paise": 100000,
        "payout_amount": 1000.0,
        "utr": "UTR1",
        "status": "CLOSE_MATCH",
        "matched_order_ids": ["ORD-1"],
        "matched_total_paise": 100500,
        "matched_total": 1005.0,
        "delta_paise": 500,
        "delta": 5.0,
        "tolerance_paise": 3000,
        "search_capped": False,
        "eligible_order_ids": ["ORD-1"],
        "matched_orders": [],
    }


def _unresolved_result(has_candidate=True):
    return {
        "payout_id": "PYT-00088",
        "seller_id": "SLR-02",
        "payout_date": D0.isoformat(),
        "payout_amount_paise": 500000,
        "payout_amount": 5000.0,
        "utr": "UTR2",
        "status": "UNRESOLVED",
        "matched_order_ids": ["ORD-2"] if has_candidate else [],
        "matched_total_paise": 100000 if has_candidate else 0,
        "matched_total": 1000.0 if has_candidate else 0.0,
        "delta_paise": -400000 if has_candidate else None,
        "delta": -4000.0 if has_candidate else None,
        "tolerance_paise": 3000,
        "search_capped": False,
        "eligible_order_ids": ["ORD-2"] if has_candidate else [],
        "matched_orders": [],
    }


def _matched_result():
    return {
        "payout_id": "PYT-00077",
        "seller_id": "SLR-03",
        "payout_date": D0.isoformat(),
        "payout_amount_paise": 100000,
        "payout_amount": 1000.0,
        "utr": "UTR3",
        "status": "MATCHED",
        "matched_order_ids": ["ORD-3"],
        "matched_total_paise": 100000,
        "matched_total": 1000.0,
        "delta_paise": 0,
        "delta": 0.0,
        "tolerance_paise": 3000,
        "search_capped": False,
        "eligible_order_ids": ["ORD-3"],
        "matched_orders": [],
    }


def _redirect_output(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(config, "AUDIT_LOG_JSONL", tmp_path / "audit_log.jsonl")
    monkeypatch.setattr(config, "LLM_MODE", "mock")


def test_matched_payout_never_calls_ai(monkeypatch, tmp_path):
    _redirect_output(monkeypatch, tmp_path)
    result = agent.explain_payout(_matched_result())

    assert result["explanation"] is None
    assert result["mode"] == "n/a"
    # No audit entry should be written for a payout with nothing to explain.
    assert not (tmp_path / "audit_log.jsonl").exists()


def test_close_match_explanation_has_fact_and_hypothesis(monkeypatch, tmp_path):
    _redirect_output(monkeypatch, tmp_path)
    result = agent.explain_payout(_close_match_result())

    assert result["mode"] == "mock"
    assert "1000" in result["fact"] or "1,000" in result["fact"]
    assert result["explanation"].startswith("POSSIBLE EXPLANATION:")
    assert (tmp_path / "audit_log.jsonl").exists()


def test_mock_explanation_is_deterministic(monkeypatch, tmp_path):
    _redirect_output(monkeypatch, tmp_path)
    r1 = agent.explain_payout(_unresolved_result())
    r2 = agent.explain_payout(_unresolved_result())
    assert r1["explanation"] == r2["explanation"]


def test_unresolved_without_candidate_still_explains(monkeypatch, tmp_path):
    _redirect_output(monkeypatch, tmp_path)
    result = agent.explain_payout(_unresolved_result(has_candidate=False))
    assert result["explanation"].startswith("POSSIBLE EXPLANATION:")
    assert "no combination" in result["fact"].lower() or "0 order" not in result["fact"]


def test_audit_log_never_contains_api_key(monkeypatch, tmp_path):
    _redirect_output(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-super-secret-value")
    agent.explain_payout(_close_match_result())

    log_text = (tmp_path / "audit_log.jsonl").read_text()
    assert "sk-super-secret-value" not in log_text


def test_answer_question_requires_payout_id(monkeypatch, tmp_path):
    _redirect_output(monkeypatch, tmp_path)
    answer = agent.answer_question("why is this broken", results_by_id={})
    assert answer["ok"] is False


def test_answer_question_unknown_payout(monkeypatch, tmp_path):
    _redirect_output(monkeypatch, tmp_path)
    answer = agent.answer_question("Why doesn't payout PYT-99999 match?", results_by_id={})
    assert answer["ok"] is False
    assert "PYT-99999" in answer["message"]


def test_answer_question_known_payout(monkeypatch, tmp_path):
    _redirect_output(monkeypatch, tmp_path)
    r = _close_match_result()
    answer = agent.answer_question(f"Why doesn't payout {r['payout_id']} fully match?", {r["payout_id"]: r})
    assert answer["ok"] is True
    assert answer["payout_id"] == r["payout_id"]
    assert answer["explanation"].startswith("POSSIBLE EXPLANATION:")


def test_live_mode_falls_back_to_mock_without_api_key(monkeypatch, tmp_path):
    _redirect_output(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "LLM_MODE", "live")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")

    result = agent.explain_payout(_close_match_result())

    assert result["mode"] == "mock_fallback"
    assert result["explanation"].startswith("POSSIBLE EXPLANATION:")
