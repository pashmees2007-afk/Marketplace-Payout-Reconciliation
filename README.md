# AI Finance Controller — Marketplace Payout Reconciliation

An agent that reconciles marketplace seller payouts against the underlying orders
that funded them — where **one payout is usually built from many orders**, minus
commission and refunds — and honestly flags the payouts it can't fully explain.

The financial matching is **100% deterministic**. The AI is only responsible for
explaining exceptions and answering questions about them. It never decides whether
a payout matches.

---

## 1. The problem

In a real marketplace (think Razorpay Route, or any platform paying out multiple
sellers), a payout to a seller isn't one order = one payment. It's: *"seller got
₹18,430 today"* — and that ₹18,430 is actually the sum of 7 different orders from
the last two days, each with its own commission cut and possibly a refund
deducted. Finance ops needs to answer one question, over and over, at scale:

> **Which combination of this seller's orders explains this payout?**

## 2. Why naive row-by-row reconciliation fails

A naive reconciliation script tries to match `payout.amount == order.amount` row
by row. That assumption is false the moment a marketplace batches orders into
payouts, which every real payout rail does. The real matching problem is a small
**subset-sum search**: "does some subset of this seller's unpaid orders sum to
this payout, within a tolerance?" That's a genuinely harder problem than a CSV
diff, and it naturally produces *real* ambiguity — a missing refund, an unlogged
fee, a late-arriving order — instead of injected noise.

## 3. Solution

Two layers, strictly separated:

1. **Deterministic reconciliation engine** (`reconcile.py`) — plain arithmetic and
   combinatorial search over integer paise. Given orders and payouts, it decides
   `MATCHED` / `CLOSE_MATCH` / `UNRESOLVED` for every payout, and `ORPHANED_ORDER`
   for every order nobody claimed. No AI, no network calls, fully reproducible.
2. **AI explanation layer** (`agent.py`) — runs *only* on `CLOSE_MATCH` and
   `UNRESOLVED` payouts (the ~30% the engine couldn't cleanly resolve) and narrates
   *why*, clearly separating **FACT** (what the engine found) from **POSSIBLE
   EXPLANATION** (the AI's hedged hypothesis). It cannot override the engine's
   decision and cannot invent orders or numbers that aren't in the evidence.

## 4. Architecture

```
                          generate_data.py
                                 |
                 +---------------+----------------+
                 v                                 v
          data/orders.csv                  data/payouts.csv
        (Order Ledger)                     (Payout File)
                 |                                 |
                 +----------------+----------------+
                                  v
                         validation.py
              (schema, duplicate IDs, dates, integer-paise
               parsing, net_payable arithmetic checks)
                                  |
                                  v
                           reconcile.py
              per-seller, date-windowed subset-sum matcher
              (itertools.combinations over integer paise)
                                  |
              +-------------------+-------------------+
              v                   v                   v
          MATCHED           CLOSE_MATCH           UNRESOLVED
       (exact subset)      (within tolerance)    (nothing close)
              |                   |                   |
              |                   +---------+---------+
              |                             v
              |                        agent.py
              |             AI narrates the FACT the engine
              |             produced; hedged POSSIBLE EXPLANATION;
              |             every call written to audit_log.jsonl
              |                             |
              +---------------+-------------+
                              v
                          report.py
             metrics.json (match rate by value, ground-truth
             accuracy)  +  exceptions.csv (every unresolved
             payout & orphaned order, in full)
                              |
                              v
                           app.py
              Streamlit dashboard: KPI cards, color-coded
              payout table, drill-down, Q&A box
```

`data/ground_truth.json` is produced by `generate_data.py` alongside the CSVs —
it records what the generator *intentionally* built, never what reconciliation
later found, so `report.py` can measure real accuracy instead of a self-reported
one.

## 5. Data model

**Order Ledger** (`data/orders.csv`) — one row per order:

| column | meaning |
|---|---|
| `order_id` | unique order identifier |
| `seller_id` | which seller this order belongs to |
| `order_amount` | gross order value (₹) |
| `commission_rate` | marketplace commission rate |
| `commission_amount` | commission deducted (₹) |
| `refund_amount` | refund deducted, if any (₹) |
| `net_payable` | `order_amount - commission_amount - refund_amount` |
| `order_date` | date the order was recorded in the ledger |

**Payout File** (`data/payouts.csv`) — one row per payout batch, from the bank/payout rail:

| column | meaning |
|---|---|
| `payout_id` | unique payout identifier |
| `seller_id` | which seller was paid |
| `payout_amount` | total amount paid out (₹) |
| `payout_date` | date of the payout |
| `utr` | bank UTR reference |

## 6. Synthetic data design

`generate_data.py` builds sellers → orders → payouts, then derives payouts
**from** the orders — never independently — so the ground truth is known by
construction, not inferred after the fact. Default scale: 20 sellers, 10–30
orders each, spread over a 16-day window.

Each payout batch is deliberately built as one of four scenarios:

| scenario | weight | what happens |
|---|---|---|
| `exact_match` | ~70% | payout = exact sum of a clean subset of `net_payable` |
| `close_match` | ~15% | payout is short by a small unlogged processing fee (₹5–₹28) |
| `timing_mismatch` | ~10% | one order in the batch is dated *after* the payout — it hasn't "arrived" in the ledger at reconciliation time |
| `unresolved` | ~5% | a payout amount with no backing orders at all (manual adjustment / chargeback) |

A handful of orders per seller are also deliberately never placed in any payout —
these become `ORPHANED_ORDER` (money owed, not yet settled). A fixed random seed
(`RANDOM_SEED=42` by default) makes every run byte-for-byte reproducible.

## 7. The subset-sum algorithm

For each payout, `reconcile.py` asks: *does some subset of this seller's eligible
orders sum to the payout amount?* That's the classic subset-sum decision problem.
Per-payout eligible sets are small (a handful of orders from a short date
window), so the implementation is deliberately the simplest correct approach:
`itertools.combinations`, tried at increasing subset size, stopping the instant
an exact sum is found.

```python
for size in range(0, max_size + 1):
    for combo in itertools.combinations(eligible, size):
        total = sum(amt for _, amt in combo)
        delta = total - target_paise
        if delta == 0:
            return exact_match(combo)          # stop immediately
        track_as_best_candidate_if_closer(combo, delta)
```

This is intentionally auditable — there's no DP reconstruction table a judge has
to trust blindly; every combination it tried is a real, printable combination of
real order IDs. Brute force over all subsets is `O(2^n)`, but seller isolation
and date-window filtering keep `n` small in practice (rarely more than 10–12); as
a safety net, `FULL_SEARCH_MAX_ELIGIBLE` / `CAPPED_SUBSET_SIZE` in `config.py`
bound the search size explicitly rather than letting it blow up silently, and
every result records whether the cap was applied (`search_capped`).

## 8. Deterministic reconciliation

All money math runs in **integer paise** (`money.py`), never floats — `0.1 + 0.2
!= 0.3` in IEEE-754 binary floating point, and a system whose entire job is
deciding whether numbers sum *exactly* to another number cannot tolerate that
kind of rounding noise. Rupee amounts only exist at the CSV/report boundary,
converted with `Decimal` (never `float`) so the text `"19.99"` becomes exactly
`1999` paise, no binary drift. This carries through to `output/metrics.json`
too: every rupee figure (`total_payout_value`, per-status `value`, etc.) has an
exact `_paise` integer sibling (`total_payout_value_paise`, `value_paise`, ...),
so a downstream consumer like the dashboard never has to reverse a float back
into paise (`round(x * 100)`) to format an exact amount — it reads the paise
field directly.

Per payout, the engine enforces:
- **Seller isolation** — only that payout's own seller's orders are ever considered.
- **Date-window filtering** — only orders dated on/before the payout date, and no
  more than `ELIGIBILITY_WINDOW_DAYS` (default 30) earlier, are eligible.
- **No duplicate allocation** — once an order is used by a payout, it's removed
  from the pool for every subsequent payout.

**Two-pass allocation per seller.** A single left-to-right pass has a subtle
failure mode: a `CLOSE_MATCH` payout might get allocated a "best candidate" that
happens to include an order a *later* payout genuinely needed for an *exact*
match — a fuzzy match stealing from a certain one. So each seller's payouts are
resolved in two passes: **Pass A** locks in every unambiguous exact match first
(across all of that seller's payouts); **Pass B** then resolves whatever's left
as `CLOSE_MATCH` or `UNRESOLVED` against the smaller remaining pool. This is
still fully deterministic (there's no ordering ambiguity in either pass) and
measurably more correct — see §11 for what this actually changes.

**Exact-match tie-breaking.** Two-pass allocation fixes fuzzy matches stealing
from exact ones, but it doesn't address a different, rarer coincidence: a
payout's target amount can occasionally be reached by *more than one* distinct
subset of eligible orders — not because the business situation is ambiguous,
but by sheer arithmetic coincidence (e.g. two small orders happen to sum to
exactly one other order's value). Picking whichever one the search happens to
try first can still be financially correct (delta is 0 either way) but
*attribute* the payout to the wrong orders — and since a wrong attribution
frees up (or holds back) the wrong orders, it can cascade into breaking a
completely different, genuinely ambiguous payout. `find_best_subset()` handles
this by scanning every exact match in the bounded search space and preferring
the one that is most *contiguous* in the seller's date-sorted order list (fewest
"gaps" between its earliest and latest order), tie-broken by fewest orders —
because a real payout batch is, by construction, a run of consecutive unpaid
orders, not some skipped and others picked up. This is a heuristic, not a proof
of uniqueness; see §11/§12 for the measured, residual failure rate.

## 9. AI explanation layer

`agent.py` runs only on `CLOSE_MATCH` and `UNRESOLVED` results — the reconciliation
engine has already produced everything before the AI is ever called. Every
explanation is two labeled sections:

- **FACT** — verbatim numbers/IDs from the reconciliation engine. Never generated by the AI.
- **POSSIBLE EXPLANATION** — a hedged hypothesis ("could indicate", "may reflect"),
  never presented as confirmed.

Works with zero configuration:

- `LLM_MODE=mock` (default) — deterministic, rule-based explanations, no network
  call, no API key. Phrasing varies by payout (via a stable hash of the payout ID)
  but is 100% reproducible run to run.
- `LLM_MODE=live` — calls the real Anthropic API (model set via `LLM_MODEL`). If
  the call fails for *any* reason — missing key, network error, timeout,
  malformed response — the system automatically falls back to the mock
  explanation, tagged `mode: mock_fallback`, and keeps going. The pipeline and
  dashboard never crash because of the AI layer.

The Q&A entry point (`agent.answer_question`) extracts a payout ID from a free-text
question ("Why doesn't payout PYT-00042 fully match?"), looks up that payout's
*actual* reconciliation result, and explains only that — it cannot invent a payout,
order, or number that isn't already in the evidence. Unknown or missing payout IDs
get a graceful message, not a crash or a hallucinated answer. ID matching is
lenient about how it's typed — "PYT-00042", "PYT-42", "PYT42", and "pyt 42" all
resolve the same way: `extract_payout_id()` checks candidates against the
dataset's actual payout IDs first, falling back to the dataset's zero-padded
format only when nothing in the dataset matches, so it never guesses wrong for
a payout that genuinely exists.

**Dashboard explanations are cached, not re-requested on every render.**
Streamlit reruns the whole script on *any* widget interaction, not just when a
new payout is selected — so `app.py` wraps the per-payout explanation call in
`st.cache_data`, keyed by the payout's result. Without this, changing an
unrelated filter while an exception payout is selected would silently re-log a
duplicate audit entry every time, and in `LLM_MODE=live` would fire a real,
billable duplicate API call for an explanation already generated this session.
The cache is cleared by the sidebar's "Regenerate data & re-run pipeline"
button alongside the rest of the pipeline output.

## 10. Auditability

Every AI explanation request is appended to `output/audit_log.jsonl`: timestamp,
payout ID, reconciliation status, the FACT evidence given to the model, model
name, mode (`mock` / `live` / `mock_fallback`), and the output text. API keys and
secrets are explicitly filtered out of every entry before it's written — the
audit trail is safe to share with a judge. This makes the whole chain traceable:

```
ORDER DATA -> DETERMINISTIC MATCHING -> FINANCIAL RESULT -> AI EXPLANATION
```

## 11. Metrics — actual output of this repo

From a clean `python run.py` on the committed seed-42 dataset (20 sellers, 410
orders, 134 payouts):

```
Reconciliation: {'MATCHED': 96, 'CLOSE_MATCH': 19, 'UNRESOLVED': 19}
Orphaned orders: 54

Match rate by value        : 93.26%   <- primary KPI
Exact match rate by value  : 81.19%
Match rate by count        : 85.82%

Ground-truth accuracy      : 100.00% (134/134)
Orphan detection precision : 46.3%
Orphan detection recall    : 83.3%
```

**Primary KPI — match rate by value**:
`(MATCHED value + CLOSE_MATCH value) / total payout value`. Reported by ₹ value,
not row count, because a handful of large unresolved payouts matter more to a
finance controller than a long tail of small ones.

**Ground-truth validation is real, not self-reported.** `report.py` scores this
run's actual reconciliation output against what `generate_data.py` intentionally
built (§6) — never the reverse. On the seed-42 dataset the engine currently
classifies all 134 payouts correctly against their intended scenario. That
number is a genuine consequence of the two-pass allocation design and
exact-match tie-breaking (§8): a single left-to-right pass with no tie-breaking
on this same dataset scores only ~92.5%, because a handful of `CLOSE_MATCH`
searches would grab orders that a later `exact_match` payout actually needed.

**Seed 42 is not cherry-picked, but it also isn't fully representative — here's
the honest, wider picture.** A single seed is a single sample; presenting only
its (excellent) number without more context would overstate how clean this
problem is. Running the full pipeline across 100 different random seeds
(12,639 payouts total) instead of just one:

```
Overall payout misclassification rate : 0.673%  (85 / 12,639)
  by scenario:
    exact_match      0 / 8,849   (100.000% correct)
    timing_mismatch  1 / 1,273   ( 99.921% correct)
    close_match     45 / 1,901   ( 97.633% correct)
    unresolved      39 /   616   ( 93.669% correct)
```

In other words: across 100 seeds, the deterministic engine gets `exact_match`
exactly right on every single payout — both the status *and* the specific
orders attributed to it. That's the direct effect of the exact-match
tie-breaking heuristic in §8: before it, this same sweep produced 6 payouts
that were financially correct (delta 0) but credited to the wrong orders, plus
knock-on damage to whichever other payout those orders actually belonged to.
The remaining ~0.67% is concentrated in `close_match` and `unresolved` — the
two categories §12 explains are governed by tolerance-band coincidence, not
attribution ambiguity, and aren't fixable by a tie-break rule.

**Orphan detection is deliberately reported honestly, not flattered.** Recall
(83.3%) is solid — most orders the generator intentionally left unpaid are
correctly flagged. Precision (46.3%) is lower: many orders end up unclaimed not
because they were "intentionally orphaned" by the generator, but as a side effect
of a `timing_mismatch` or `unresolved` payout leaving its real orders unallocated
(correctly — the engine shouldn't guess) — see §12 for why that's a genuine
property of the problem, not a bug being hidden.

Run `pytest` for exact per-scenario accuracy breakdowns and the money-arithmetic
proofs.

## 12. Limitations

- **Exact-match tie-breaking is a heuristic, not a uniqueness proof, and a small
  residual failure mode remains in the tolerance-band scenarios.** When a
  `CLOSE_MATCH` payout's true orders get drawn into an *unrelated* exact match
  elsewhere, or a `timing_mismatch` payout's remaining orders coincidentally
  form a different close/no match than intended, it can surface as the wrong
  status. Measured directly: 45 of 1,901 `close_match` payouts (2.4%) and 1 of
  1,273 `timing_mismatch` payouts (0.08%) across a 100-seed sweep — down from
  50 and 1 respectively before the tie-break fix. `exact_match` itself is now
  100.000% correct (status and order attribution both) across the same sweep,
  versus 99.93% before — the 6 payouts per 100 seeds that used to be financially
  correct but attributed to the wrong orders (with knock-on damage to whatever
  payout those orders actually belonged to) are gone. The residual is not
  proven to be zero in general — contiguity is a strong proxy for "how this
  generator builds a real batch," not a guarantee against every possible
  numeric coincidence.
- **Tolerance-based closest-subset search can occasionally produce a coincidental
  near match.** An `UNRESOLVED` scenario's payout amount is generated
  independently of any real order combination, but with enough eligible orders in
  play, a combination can coincidentally land within tolerance by chance. Measured:
  39 of 616 `unresolved` payouts (6.3%) across the same sweep. This is a genuine
  property of tolerance-based reconciliation on real data — no tie-break rule
  fixes it, since there's no "true" alternative being displaced, just one random
  amount landing close to one real combination — and it's exactly why every `CLOSE_MATCH`
  gets an AI explanation flagged for human review rather than being silently
  accepted.
- **Orphan-detection precision is inherently coupled to exception handling.**
  Orders left over from a `timing_mismatch` or `unresolved` payout are correctly
  *not* allocated (the engine shouldn't guess), but that means they show up as
  "orphaned" alongside genuinely never-paid orders. Distinguishing the two would
  require carrying forward *why* a payout was an exception, which the current
  data model doesn't yet track per-order.
- **Brute-force subset search doesn't scale unbounded to extremely large
  batches** — see `FULL_SEARCH_MAX_ELIGIBLE` / `CAPPED_SUBSET_SIZE` in
  `config.py`. Fine for this dataset's scale (per-payout eligible sets stay in
  the single digits to low teens); a production system with far larger
  per-seller batches would need a smarter bound (e.g. meet-in-the-middle) or a
  hard cap with monitoring on `search_capped` (covered by
  `tests/test_reconcile.py`'s `test_search_capped_*` tests, which force the cap
  with a tiny configuration to prove it degrades to a reported, non-silent
  trade-off rather than a wrong answer).
- **The live-LLM request/response code path (`agent._call_live_llm`) is unit
  tested against a mocked Anthropic client** (`tests/test_agent.py`'s
  `test_call_live_llm_*` / `test_explain_payout_live_mode_*` tests cover
  request construction, response parsing, and the malformed-response fallback)
  **but has not been exercised against the real Anthropic API** in this
  environment. A mock can't catch a real SDK version mismatch or an actual API
  behavior change, so spot-check `LLM_MODE=live` with a real key before a demo
  that depends on it.
- **Mock AI explanations are template-based**, not generative — they're
  deterministic by design (see §9) so demos and tests are reproducible, but they
  won't produce genuinely novel phrasing the way `LLM_MODE=live` can.
- **Single currency, single marketplace rail.** No multi-currency handling, no
  partial-refund timing beyond what §6 models.

## 13. Future improvements

- Real Razorpay Route payout data instead of synthetic generation.
- Multi-currency support (paise arithmetic generalizes to any minor-unit currency).
- Track per-order exception provenance to separate "genuinely orphaned" from
  "left over from an exception" in the orphan report.
- A meet-in-the-middle or DP-based search for sellers with very large eligible
  batches, without giving up the current approach's auditability.
- Persist reconciliation state across runs (currently every run reconciles the
  full dataset from scratch).

---

## Installation

Requires Python 3.9+.

```bash
git clone <this-repo>
cd Marketplace-Payout-Reconciliation
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Configuration

Copy `.env.example` to `.env` and edit as needed — **this step is optional**. The
entire application works with zero configuration (`LLM_MODE=mock`, no API key).

```bash
cp .env.example .env
```

Key variables (see `.env.example` for the full list with explanations):

| variable | default | purpose |
|---|---|---|
| `LLM_MODE` | `mock` | `mock` (no API key needed) or `live` (calls Anthropic) |
| `ANTHROPIC_API_KEY` | *(empty)* | only used when `LLM_MODE=live` |
| `TOLERANCE_PAISE` | `3000` (₹30) | max delta still considered `CLOSE_MATCH` |
| `ELIGIBILITY_WINDOW_DAYS` | `30` | how far back an order can be dated and still be eligible |
| `RANDOM_SEED` | `42` | synthetic data generation seed |
| `NUM_SELLERS` | `20` | how many sellers to generate |

## Running the pipeline

```bash
python run.py
```

This generates synthetic data, validates it, runs deterministic reconciliation,
generates AI explanations for every exception, and writes `output/metrics.json`,
`output/exceptions.csv`, and `output/audit_log.jsonl`. Each stage prints its own
summary. Re-run with `--skip-generate` to reconcile against the existing
`data/*.csv` without regenerating it, or `--seed N --sellers N` for a different
(still reproducible) dataset.

Individual stages can also be run standalone: `python generate_data.py`,
`python reconcile.py`, `python agent.py`, `python report.py`.

## Launching the dashboard

```bash
streamlit run app.py
```

Opens at `http://localhost:8501`. If `python run.py` hasn't been run yet, the
dashboard shows a clear message and a **Regenerate data & re-run pipeline**
button in the sidebar — it never crashes on missing output.

## Running tests

```bash
pytest tests/ -v
```

70 tests covering the ten required scenarios (exact match, multi-order match,
close match, unresolved, order-reuse prevention, seller isolation, orphaned
orders, date-window filtering, integer-paise accuracy, and full-pipeline
ground-truth scoring — `tests/test_reconcile.py`, `tests/test_ground_truth.py`),
plus generator integrity (`tests/test_data.py`), input validation
(`tests/test_validation.py`), metrics math (`tests/test_report.py`), the AI
layer's mock-mode contract, audit logging, and mocked live-LLM request/response
handling (`tests/test_agent.py`), the subset-sum search-size cap
(`test_search_capped_*` in `tests/test_reconcile.py`), and the file I/O glue
code that `run.py` and the dashboard actually depend on -- `generate_report()`,
`write_exceptions_csv()`, `run_reconciliation()`, and `run.py`'s `main()` itself,
run end-to-end against real temp files (`tests/test_integration.py`).

## Example output

```
$ python run.py
======================================================================
STEP 1/4 -- Generating synthetic marketplace data
======================================================================
  410 orders, 134 payouts, 30 intentionally orphaned orders, seed=42

======================================================================
STEP 2/4 -- Validating input data
======================================================================
  410 orders and 134 payouts passed validation (integer-paise, unique IDs,
  valid dates, net_payable arithmetic checks).

======================================================================
STEP 3/4 -- Running deterministic reconciliation + AI exception explanations
======================================================================
  Reconciliation: {'MATCHED': 96, 'UNRESOLVED': 19, 'CLOSE_MATCH': 19}
  Orphaned orders: 54
  AI explanations generated: 38 (LLM_MODE=mock)

======================================================================
STEP 4/4 -- Report
======================================================================
  Match rate by value        : 93.26%
  Exact match rate by value  : 81.19%
  Match rate by count        : 85.82%
  Ground-truth accuracy      : 100.00% (134/134)
  Orphan detection precision : 0.463
  Orphan detection recall    : 0.8333

Pipeline complete in 0.06s.
```

## Project structure

```
finance-controller/
├── app.py              # Streamlit dashboard
├── generate_data.py    # synthetic data generator + ground truth
├── reconcile.py         # deterministic subset-sum reconciliation engine
├── agent.py             # AI explanation layer (mock + live modes)
├── report.py             # metrics.json + exceptions.csv + ground-truth scoring
├── run.py                 # end-to-end pipeline runner
├── validation.py           # CSV schema/data validation
├── money.py                 # integer-paise money helpers
├── config.py                 # central configuration (env-overridable)
├── data/
│   ├── orders.csv
│   ├── payouts.csv
│   └── ground_truth.json
├── output/
│   ├── reconciliation_results.json
│   ├── metrics.json
│   ├── exceptions.csv
│   └── audit_log.jsonl
├── tests/
│   ├── test_reconcile.py
│   ├── test_data.py
│   ├── test_report.py
│   ├── test_ground_truth.py
│   ├── test_validation.py
│   ├── test_agent.py
│   └── test_integration.py
├── .env.example
├── .gitignore
├── requirements.txt
└── README.md
```
