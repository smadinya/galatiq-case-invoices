# Design: Acme invoice-processing agents

**Status:** agreed in design review on 2026-11-14. This file is the build spec.
**Companion:** [ARCHITECTURE.md](ARCHITECTURE.md) covers how the pieces fit together. This file covers what the system does and why.

---

## 1. Goal and scope

Automate Acme's four-stage invoice workflow (ingest, validate, approve, pay) as a local multi-agent prototype. Code applies the hard rules. LLM agents handle unstructured documents and judgment calls. A person sees an invoice only for a short, closed list of reasons (§3.4).

### Build tiers

Each tier is complete and works without the tiers after it.

| Tier | Contents |
|---|---|
| **1** | LangGraph pipeline, CLI, SQLite state, Grok and replay providers, the recorder, deterministic and replay golden tests, JSONL logs |
| **2** | Streamlit dashboard (4 pages, §12.2) |
| **3** | Risk score, prompt-injection detection, extra test invoices, cost and metrics, Ollama provider |

The golden **outcomes** (§14.2) hold without tier 3. When tier 3 lands, the expected **flag lists** gain `high_risk_score` and `prompt_injection` where they apply.

### Cut on purpose

| Cut | Instead |
|---|---|
| Email inbox ingestion | Folder input (`--dir`) |
| OCR for scanned images | PDFs must have a text layer (all three samples do) |
| Login and multiple users | One local operator |
| Real banking | `mock_payment` wrapped with idempotency and retries |
| Live FX rates | Foreign-currency invoices go to a person, who enters the rate |

---

## 2. What the sample data contains

`data/invoices/` holds **20 files covering 16 invoice numbers**. The README's scenario table documents 8 of the 16 numbers. These are the facts that shaped this design:

| Invoice | Finding |
|---|---|
| 1011, 1012, 1013 | Each appears twice, as TXT/JSON plus a PDF with the same content. A naive batch would pay each one twice. |
| 1004 / 1004_revised | Same number. R1 adds 5 × GadgetX ("revised per PO amendment"). |
| 1014 | XML, **EUR**. Neither the format nor a foreign currency is mentioned in the README. |
| 1006 vs 1007/1015 | Two different CSV layouts: field/value rows vs a normal table. |
| 1007 | **Math error:** lines are $14,750, 6% tax is $885, so the total should be $15,635. The invoice says $15,525 ($110 short). |
| 1009 | Negative quantity, no vendor, no due date. The stated subtotal ($1,000) doesn't match the lines (−$250). |
| 1013 | Each line fits within stock on its own, but the per-item totals are WidgetA 22, WidgetB 18, GadgetX 9. A line-by-line check misses this. It also has a **$50 math error** (see `generate_pdfs.py` below). |
| 1010 | WidgetA appears on two lines, one a **rush line at $300** against a $250 catalog price. There is also a shipping line. |
| 1012 | OCR-style errors (`2O26`, `$3,500.O0`, `Widget A`, `INV 1012`). The vendor is **"QuickShip Distributers (formerly FastShip Ltd.)"**, a rename (classic vendor-impersonation pattern). Total $9,975, just under $10K. |
| 1008 | Email-style text. Unknown vendor, unknown items, $9,900 (just under $10K). |
| 1005 | Unknown vendor whose address is 1600 Pennsylvania Ave (the White House). Over $10K. |
| 1002 | Due date equals the invoice date despite Net 30. The number "1002" has no `INV-` prefix. |
| 1003 | Unknown vendor, FakeItem (0 stock), due date "yesterday", pressure wording ("URGENT… wire transfer"), $100K. |
| 1016 | Approved vendor billing WidgetC, which is not in inventory. |
| `generate_pdfs.py` | Adds $50 to the 1013 PDF grand total. The committed files **already contain it**: 1013.json and 1013.pdf both state $22,562.80, but lines $21,040 + 7% tax $1,472.80 = $22,512.80. So 1013 raises `math_mismatch` (+$50). |

Every invoice over $10K in the sample set fails at least one check. That is why §14.3 adds a clean invoice over $10K.

---

## 3. Decision model

### 3.1 The ceiling

Rules compute the best outcome an invoice can get. The LLM picks an outcome **at or below** that ceiling.

```
REJECT  <  HUMAN  <  APPROVE
```

- Code takes `final = min(llm_decision, ceiling)`. If the LLM proposed something above the ceiling, code logs a `ceiling_clamped` event.
- The LLM can never overturn a hard rule, and can never approve what the rules don't allow.
- The LLM *can* always make an outcome stricter.
- HUMAN is only available when the ceiling is HUMAN. A HUMAN proposal under an APPROVE ceiling becomes **REJECT**, because the human-review reasons are a closed list (§3.4).

### 3.2 Severities

| Severity | Effect on the ceiling | What happens |
|---|---|---|
| `error` | Ceiling becomes **REJECT** | The VP writes the rejection memo. The LLM has no say in the outcome. |
| `human` | Ceiling becomes **HUMAN** (unless there is an error) | The VP and Critic write a recommendation. The VP may reject outright so the person doesn't have to look. |
| `escalation` | No change | A flag the **VP must answer in writing**. It is not a separate route. Its only routing effect is that the Critic also reviews (§5). |
| `info` | No change | Shown to the VP and the person. |

### 3.3 Flag catalogue

| Code | Severity | Fires when | Samples |
|---|---|---|---|
| `non_positive_quantity` | error | Any line quantity ≤ 0 | 1009 |
| `missing_vendor` | error | Vendor name empty or absent | 1009 |
| `invalid_due_date` | error | Due date missing or unparseable (e.g. "yesterday") | 1003, 1009 |
| `zero_stock_item` | error | Item exists with stock 0 | 1003 (FakeItem) |
| `unknown_item` | error | Item not in inventory after name clean-up. A near miss adds a "did you mean" hint. | 1008, 1016 |
| `insufficient_stock` | error | Per-item total across all lines exceeds **current** stock | 1002, 1013 |
| `missing_invoice_number` | error | No invoice number (§18) | — |
| `no_line_items` | error | No line items (§18) | — |
| `high_risk_score` *(tier 3)* | error | Risk score ≥ 60 (§3.5) | 1003 |
| `prompt_injection` *(tier 3)* | error | Injection pattern found in any free text (§3.6) | INV-2002 |
| `math_mismatch` | error | A line amount, the subtotal or the total is off by more than $0.01. Any math mismatch is rejected, whichever way it is off (§19). | 1007, 1009, 1013 |
| `unknown_vendor` | escalation | Vendor not in the vendor master | 1003, 1005, 1008 |
| `price_above_catalog` | escalation | Unit price more than 10% above catalog (USD only) | 1010 |
| `foreign_currency` | human | Currency ≠ USD. The catalog price check is skipped. | 1014 |
| `revision_of_paid_invoice` | human | A marked revision of an invoice already PAID | 1004 R1 |
| `conflicting_invoice_number` | human | Same vendor and number, different content, no revision marker | — |
| `over_budget` | human | Paying would exceed the remaining monthly budget. Only checked when no `error` flag fired: an invoice the hard rules reject is never paid. | INV-2006 |
| `vendor_renamed` | human | Invoice names a known vendor as "formerly", "previously" or "f/k/a" | 1012 |
| `payment_failed` | human | Payment still failing after retries (§9) | — |
| `unreadable_document` | human | File can't be parsed, or LLM extraction fails the grounding check twice (§4.1) | — |
| `over_threshold` | info | Amount > $10,000 USD. This brings in the Critic (§5). | 1002, 1003, 1005, 1007, 1013 |
| `near_threshold` | info | $9,000 ≤ amount < $10,000. Always brings in the Critic (§5), because this band is where invoices get split to dodge the threshold. | 1008, 1012 |
| `past_due` | info | Due date before the as-of date. A priority marker, never a reason to hold. | 1002, 1008 |
| `price_below_catalog` | info | Unit price below catalog (e.g. volume discounts) | 1013 |
| `value_normalized` | info | A value was cleaned up (`INV 1012`→`INV-1012`, `2O26`→`2026`) | 1002, 1012 |
| `terms_mismatch` | info | Due date differs from invoice date + payment terms by more than ±3 days | 1002 |

Duplicates aren't flags. They get the terminal status `DUPLICATE` (§7).

### 3.4 Human-review reasons (closed list)

An invoice reaches a person **only** for one of these six reasons:

1. **Foreign currency.** Exchange rates move, so a person sets the rate.
2. **Revision of a paid invoice, or the same number with different content.**
3. **Over budget.** Only a person authorizes overspending.
4. **Vendor renamed.** Verifying it means calling the vendor's known contact, which an AI can't do.
5. **Payment still failing after retries.**
6. **Unreadable document.** The file can't be parsed, or extraction failed the grounding check.

Nothing else creates human work. If the VP and Critic deadlock, the result is REJECT, not a person (§5).

### 3.5 Risk score *(tier 3)*

The score is additive and explainable. It is shown to the VP, the Critic and the UI.

| Signal | Points |
|---|---|
| Unknown vendor | +30 |
| Pressure wording ("urgent", "immediately", "wire transfer", "avoid penalties", …) | +25 |
| Vendor renamed | +25 |
| Invalid due date | +15 |
| Amount $9,000–$9,999.99 | +15 |
| Total is a multiple of $1,000 | +10 |

A score **≥ 60** raises `high_risk_score` (error). This is a hard rule the LLM can't override.

Sample scores: 1003 = 80, 1008 = 45, 1012 = 40, 1005 = 30, 1009 = 15, and 1001/1002/1011 = 10.

### 3.6 Prompt injection *(tier 3)*

- **Detector:** pattern matching runs on every free-text field (notes, address, raw TXT/PDF text). Patterns include "ignore (all/previous/prior) instructions", "disregard…", "you are now", "system prompt", "pre-approved", "approve this invoice" and "do not flag". A match raises `prompt_injection` (error).
- **Isolation:** the **VP never sees the raw document**, only the structured invoice. Free-text fields are passed in quotes and labelled as untrusted data.
- **Critic access:** the Critic sees source text only line by line, through `get_source_evidence` (§4.4).

---

## 4. Agents

Only three steps call a model: **unstructured extraction**, the **VP** and the **Critic**. The VP and Critic use the same model with different system prompts.

### 4.1 Ingestion

- **Structured files (JSON, both CSV layouts, XML):** parsed by code, with no LLM.
- **Unstructured files (TXT, PDF via pdfplumber):** the LLM extracts into the `ExtractedInvoice` schema using structured output. The schema also carries a `corrections` list of `{field, raw, interpreted}`, e.g. `2O26 → 2026`.
- **Grounding check (self-correction):** code checks that every extracted item name, quantity and money amount appears in the source text after normalization.
  - On failure, the error is sent back to the LLM for **one** retry.
  - A second failure raises `unreadable_document` (human).
- **Normalization:**
  - Invoice numbers become `INV-NNNN`.
  - Dates become ISO. Formats handled: `Jan 30 2026`, `01/28/2026`, `26-Jan-2O26`.
  - Money becomes `Decimal`.
  - Item names are matched by case, whitespace and punctuation only. There is **no** fuzzy auto-correction, because `WidgetC` scores about 86 against `WidgetA` on rapidfuzz's ratio and would be silently "corrected".

### 4.2 Validation

Deterministic Python. It runs the dedupe check (§7), raises every flag in §3.3, computes the risk score (tier 3) and sets the ceiling.

It also runs the **address check**. Code compares the invoice's vendor address with the vendor master's address on file and records one of four results, checked in this order (`no_record` first, then `not_on_invoice`, then `verified` or `mismatch`):

| Result | Meaning |
|---|---|
| `verified` | The address matches the one on file |
| `mismatch` | Both addresses exist and differ |
| `not_on_invoice` | The invoice gives no address |
| `no_record` | The vendor has no address on file, or is unknown |

The result goes to the VP and Critic. It is not a flag and doesn't change the ceiling. In the samples, 1004, 1004 R1, 1013 and 1016 are `verified` and 1005 is `no_record`.

### 4.3 VP

- **Inputs:** the structured invoice, its flags, the risk score, the ceiling and the address check result. `policy.md` states that a `verified` address is **never** grounds for rejection, which protects 1004 and its Simpsons address (742 Evergreen Terrace). A `mismatch` must be mentioned in the VP's reasoning.
- **Tools:** everything else is fetched through tools.

| Tool (VP and Critic) | Returns |
|---|---|
| `get_vendor_history(vendor)` | Past invoices for this vendor from our records, with outcomes and totals |
| `get_stock_level(item)` | Current stock |
| `get_budget_status()` | Monthly limit, spent so far, remaining |
| `get_policy(topic)` | A section of `policy.md` (thresholds, rules, human-review reasons) |

- **Output:** `{decision, reasoning, flags_addressed[{code, response}], objections_answered[...]}`. Every `escalation` flag must appear in `flags_addressed`.
- **REJECT ceiling:** the VP only writes the memo. The decision is fixed.

### 4.4 Critic

The Critic has the VP's four tools plus **five of its own**, so its view is grounded better than the VP's:

| Critic-only tool | Catches |
|---|---|
| `get_source_evidence(field)` | The source line(s) an extracted value came from, so it can check extraction without the full raw document |
| `recompute_totals()` | An independent recalculation of lines, tax, shipping and total |
| `find_similar_invoices(vendor, amount, days)` | Near-duplicates, and invoices split to stay under $10K |
| `get_vendor_master(vendor)` | The official record: approval status, previous names, address on file, terms |
| `get_pipeline_trace()` | What earlier stages did: corrections, retries, every flag |

- **Output:** `{verdict: AGREE | OBJECT, proposed_decision, objections[{about, evidence, requested_change}]}`.
- **Feedback to the VP:** the review goes back to the VP, and the VP must answer every objection.

### 4.5 Payment

Wraps the README's `mock_payment(vendor, amount)`. See §9.

---

## 5. The approval loop

| Situation | Path |
|---|---|
| Ceiling REJECT | The VP writes the memo only. No Critic. |
| Ceiling APPROVE, amount **under $9,000**, no `escalation` flags | **Fast path:** the VP alone. It may approve or make the outcome stricter. |
| Everything else (any `escalation` flag, amount $9,000 or more or unknown in USD, or ceiling HUMAN) | **VP ⇄ Critic** |

The $9K–$10K band skips the fast path on purpose. Invoices split to stay under $10K land there, and only the Critic's `find_similar_invoices` can see the other half of a split.

Rules for the VP ⇄ Critic loop:

- **Round limit:** at most **3** Critic reviews.
- **Approvals over $10K:** need the Critic's explicit `AGREE`.
- **Deadlock:** if the Critic still objects after round 3, the **stricter** of the VP's decision and the Critic's `proposed_decision` wins. A deadlock never creates human work.
- **Ceiling:** code caps the final decision at the ceiling (§3.1).

---

## 6. Stock, budget and time

### 6.1 Stock

**Definition:** the quantity of an item currently available to sell.

**Rules:**
- **Per-item totals:** the check uses each item's total across all lines.
- **Carries over:** stock state persists across runs and is used up **only when an invoice is paid**.
- **Human queue holds nothing:** invoices waiting for a person don't reserve stock. Stock is checked again when the person resolves the invoice, and a shortfall then means REJECT.
- **Revisions:** for a revision of a paid invoice, only the **added** quantities are checked and used up.

**Starting stock** (`--seed demo`, the default):

| Item | Stock | Unit price | README seed |
|---|---|---|---|
| WidgetA | 90 | $250 | 15 |
| WidgetB | 50 | $500 | 10 |
| GadgetX | 13 | $750 | 5 |
| FakeItem | 0 | $1,000 | 0 |

**Why the starting stock was raised** (this note also goes in the README):

- **The README seed fails under carried-over stock.** The README's seed assumes each invoice is checked on its own. Under carried-over stock it breaks: a batch pays 1001 and 1004, which leaves WidgetA at 2, and from then on almost everything fails the stock check.
- **Result with the README seed:** 2 paid, 1 sent to a person, 14 rejected and 3 duplicates. One of the 14 is **1006**, which the README's own table says should pass. `--reset --seed readme` reproduces this.
- **WidgetA 90 and WidgetB 50** keep 1001, 1004, 1006 and 1011 passing however the VP rules on 1005.
- **GadgetX 13:**
  - 1002 (needs 20) still fails.
  - 1012 (needs 4) fits.
  - 1013 needs 9 in total and 13 are left when it runs, so it passes the stock check. It is still rejected for its `math_mismatch` (§19).
- **After the batch** 56 WidgetA, 32 WidgetB and 11 GadgetX are left, enough for the queue walk-through in §14.2: 1012 uses 4 GadgetX and 1004 R1 then uses 5, leaving 2. In the other order 1004 R1 leaves 6, enough for 1012.

### 6.2 Budget

- **Scope:** one company-wide monthly limit on payments to vendors, for the month of the as-of date. The default is **$50,000**.
- **When it's used:** on payment, in USD.
- **Over budget:** an invoice that would exceed it raises `over_budget` (human).
- **On resolve:** if approving would exceed the budget, the person sees by how much and must **explicitly confirm** the overspend. It is not rejected automatically.

### 6.3 Ordering and the clock

- **Order:** batch runs process files in **filename order**, which is invoice-number order. 1004 comes before 1004 R1, and each PDF before its TXT.
- **As-of date:** `--as-of` defaults to **2026-01-31**, so past-due checks are meaningful for January 2026 invoices.
- **Reset:** `--reset` restores the starting stock and budget and clears all history. `--seed readme` selects the README's numbers.
- **Banner:** because each run depends on earlier runs, the run banner always shows remaining stock and budget and suggests `--reset`.

---

## 7. Duplicates and revisions

| Case | Detected by | Result |
|---|---|---|
| Exact same file run again | Same file bytes (SHA-256) | Return the stored result. No new processing, no duplicate flag. |
| Same invoice in a different format | Same economic fingerprint: vendor, number, lines, total, currency | `DUPLICATE`. Never paid again. |
| Same number, different content, no revision marker | Same vendor and number, different fingerprint | `conflicting_invoice_number` (human) |
| Marked revision, original **paid** | `revision` field or "revised" note, original status PAID | `revision_of_paid_invoice` (human). Approving pays the **difference** only. If the difference is ≤ 0, nothing is paid and the status is `CREDIT_DUE`. |
| Marked revision, original **not paid** | Same, original status not PAID | The revision replaces the original and is processed normally. The original's status becomes `SUPERSEDED`, and an open queue item for it is closed as superseded. |

An `ERROR` result is never short-circuited: running the same bytes again re-processes the file, because `ERROR` is a bug, not a business result.

---

## 8. Currency

Rates change, so a non-USD invoice raises `foreign_currency` (human). The catalog price check is skipped, because unit prices can't be compared without a rate.

- **The person's rate:** when approving, the person **enters the exchange rate**. It is recorded with the payment and used to count the invoice against the budget.
- **Payment currency:** payment is made in the invoice's currency.

---

## 9. Payment

- **Wrapper:** `mock_payment(vendor, amount)` is kept as written in the README and wrapped.
- **Idempotency key:** `vendor + invoice number + revision`, stored in `payments`. A repeat call returns the existing receipt.
- **Retries:** 3 after the first attempt (4 attempts in all), with exponential backoff (0.5 s, 1 s, 2 s; zero in tests). If they all fail, the invoice raises `payment_failed` (human). Approving it in the queue retries the payment.
- **Failure injection:** `--payment-fail-rate R` makes the retry path demonstrable.
- **Amount paid:**
  - Normally, the stated total.
  - An approved revision: the difference.
- **Which amount counts where:** the threshold flags (`over_threshold`, `near_threshold`) use the stated total. `over_budget` and the payment use the amount actually payable (the stated total, or a revision's difference).

---

## 10. Human review

Pause and resume both use LangGraph's `interrupt()`. Progress is checkpointed to `checkpoints.db`, keyed by trace ID.

- **What the person sees:** the reason, the VP/Critic recommendation and the evidence.
- **Their choices:** approve or reject, with a **required note**. When it applies, they also enter an exchange rate or confirm an overspend.
- **Rechecks on resume:**
  - A stock shortfall means REJECT, with a memo.
  - A budget overrun needs confirmation (§6.2).
- **Vendor renames:** once a person approves a `vendor_renamed` invoice, the new name is stored as an alias, so the next invoice under that name doesn't raise the flag again.
- **Unreadable documents** can only be rejected: there is no reliable data to pay from. The operator fixes the file and runs it again.
- **Stock-short memo:** the REJECT memo written on resume is generated by code, so `--resolve` and `--queue` never need a model.
- **Preview:** `--resolve REF` without `--approve` or `--reject` shows what approving would do (overspend, stock shortfalls, exchange rate needed).
- **Where it runs:** the CLI (`--queue`, `--resolve`) and the Streamlit queue call the same `resolve()` function, so tier 1 is complete without Streamlit.

---

## 11. Models and providers

| Step | Model (configurable; names to be confirmed, §17) |
|---|---|
| Unstructured extraction | A fast non-reasoning Grok model, temperature 0 |
| VP and Critic | One reasoning-capable Grok model, two system prompts |

**Provider order:** `--provider` → **Grok** (if `XAI_API_KEY` is set) → **replay** (if this file has a recording) → **Ollama** (if running on `localhost:11434`). If none is available, the program **exits with a clear message** naming the three ways to get a model. Tier 1 has no Ollama, and replay is auto-selected only when **every** file in the run has a recording. There is no rules-only runtime mode. Offline correctness is covered by the test suite.

**Client:** Grok and Ollama share **one OpenAI-compatible client**, pointed at `https://api.x.ai/v1` or at Ollama's `/v1` endpoint. We don't use `xai_sdk`, so one code path serves both, including structured outputs and tool calling.

**Replay:**
- **Recording:** `scripts/record.py` runs the sample and extra invoices through Grok and saves every model call under `fixtures/replay/`.
- **Key:** each recording is keyed by file hash, stage, prompt version and call index.
- **Staleness:** changing a prompt makes its recordings stale, and the replay tests fail with "re-record".
- **Banner:** the run banner always names the active provider. In replay mode it also states the recording date.

The README's `from xai import Grok` snippet doesn't match a real SDK. The README write-up will point this out.

---

## 12. Interfaces

### 12.1 CLI

```bash
python main.py --invoice_path=data/invoices/invoice_1001.txt   # one invoice (README form)
python main.py --dir data/invoices                             # batch, summary table
python main.py --queue                                         # list human-review items
python main.py --resolve INV-1012 --approve --note "Called vendor, rename confirmed"
python main.py --resolve INV-1014 --approve --fx-rate 1.08 --note "..."
python main.py --resolve INV-1012 --approve --confirm-overspend --note "..."
python main.py --resolve INV-1004                              # preview only: overspend, shortfalls
python main.py --reset [--seed demo|readme]
# common flags: --provider grok|replay|ollama  --json  --as-of 2026-01-31  --payment-fail-rate 0.3
```

- **Display:** Rich shows stage-by-stage progress, a coloured flags table and a decision panel with the VP/Critic exchange.
- **`--json`:** turns Rich off and prints only the final result object to stdout.

### 12.2 Streamlit (tier 2)

1. **Process.** Upload a file or pick a sample, then watch the stages run. Extracted fields sit next to the source, with flagged fields highlighted. The VP ⇄ Critic exchange shows as a chat.
2. **Human queue.** The six reasons only. Each item shows its reason, the recommendation and the evidence. Approve and reject need a note, plus an exchange rate or overspend confirmation when needed.
3. **Ledger.** Stock and budget over time, and payments.
4. **Batch and metrics.** Run a folder, see the results table, automation rate, average time and LLM cost per invoice.

---

## 13. Observability and errors

- **Logs:** JSON lines go to `logs/YYYY-MM-DD.jsonl`, one event per graph node, with `trace_id`, node, event, duration, tokens and cost.
- **Audit trail:** every business decision is also written to the `audit_log` table.
- **Crashes:** an unexpected exception in any node gives that invoice status **`ERROR`**. It is logged with the full trace and nothing is paid. The batch continues, and the row shows red in the summary and on the batch page. It does **not** enter the human queue, because it's a bug, not a business decision.
- **Parse failures** are `unreadable_document` (human), not `ERROR`.

---

## 14. Testing

### 14.1 Three layers

| Layer | Runs | Asserts |
|---|---|---|
| **Deterministic** | Always, offline | Parsing, normalization, flags, severities, ceilings, and the stock and budget ledger after every invoice. VP decisions come from a **scripted fake** using the golden decisions below. |
| **Replay** | Always, offline | Final decisions for every sample and extra invoice match the recorded Grok run exactly |
| **Live** (`pytest -m live`) | With a key | Invariants only: final ≤ ceiling, every `escalation` flag answered in the VP memo, the Critic agrees on any approval over $10K |

Unit tests cover every tool and parser. There is one end-to-end test per path: paid, rejected, human, duplicate, error.

### 14.2 Golden table: sample batch

Starting point: a fresh `--reset` (demo seed), as-of 2026-01-31, budget $50,000. Rows marked † are the VP's judgment calls, and they are the scripted answers in the deterministic layer.

| # | File | Flags | Ceiling | Outcome |
|---|---|---|---|---|
| 1 | invoice_1001.txt | — | APPROVE | **PAID** $5,000 (fast path) |
| 2 | invoice_1002.txt | `insufficient_stock` GadgetX 20 > 13 · `over_threshold` · `past_due` · `terms_mismatch` · `value_normalized` | REJECT | **REJECTED** |
| 3 | invoice_1003.txt | `zero_stock_item` · `invalid_due_date` · `high_risk_score` (80) · `unknown_vendor` · `over_threshold` | REJECT | **REJECTED** |
| 4 | invoice_1004.json | — | APPROVE | **PAID** $1,890 (fast path) |
| 5 | invoice_1004_revised.json | `revision_of_paid_invoice` | HUMAN | **PENDING_HUMAN**† (approve → pay $4,050) |
| 6 | invoice_1005.json | `unknown_vendor` · `over_threshold` | APPROVE | **REJECTED**† by VP (vendor not onboarded, over $10K) |
| 7 | invoice_1006.csv | — | APPROVE | **PAID** $2,750 (fast path) |
| 8 | invoice_1007.csv | `math_mismatch` (−$110) · `over_threshold` | REJECT | **REJECTED** (error flag; VP memo) |
| 9 | invoice_1008.txt | `unknown_item` ×2 · `unknown_vendor` · `near_threshold` · `past_due` | REJECT | **REJECTED** |
| 10 | invoice_1009.json | `non_positive_quantity` · `missing_vendor` · `invalid_due_date` · `math_mismatch` | REJECT | **REJECTED** |
| 11 | invoice_1010.txt | `price_above_catalog` (rush line +20%) | APPROVE | **REJECTED**† by VP (rush line +20%, after the Critic's objection) |
| 12 | invoice_1011.pdf | — | APPROVE | **PAID** $3,000 (fast path) |
| 13 | invoice_1011.txt | — | — | **DUPLICATE** of #12 |
| 14 | invoice_1012.pdf | `vendor_renamed` · `near_threshold` · `value_normalized` | HUMAN | **PENDING_HUMAN**† |
| 15 | invoice_1012.txt | — | — | **DUPLICATE** of #14 |
| 16 | invoice_1013.json | `over_threshold` · `price_below_catalog` · `math_mismatch` (+$50) | REJECT | **REJECTED** |
| 17 | invoice_1013.pdf | — | — | **DUPLICATE** of #16 |
| 18 | invoice_1014.xml | `foreign_currency` (EUR) | HUMAN | **PENDING_HUMAN**† |
| 19 | invoice_1015.csv | — | APPROVE | **PAID** $6,500 (fast path) |
| 20 | invoice_1016.json | `unknown_item` WidgetC | REJECT | **REJECTED** |

**Totals:**
- **Outcomes:** 5 paid, 9 rejected, 3 for a person, 3 duplicates.
- **Automation:** 14 of the 17 distinct documents (82%) were decided without a person.
- **Budget:** $19,140 of $50,000 spent.

**Ledger after each payment:**

| After | WidgetA | WidgetB | GadgetX | Budget spent |
|---|---|---|---|---|
| start | 90 | 50 | 13 | $0 |
| 1001 | 80 | 45 | 13 | $5,000 |
| 1004 | 77 | 43 | 13 | $6,890 |
| 1006 | 72 | 40 | 13 | $9,640 |
| 1011 | 66 | 37 | 13 | $12,640 |
| 1015 | 56 | 32 | 11 | $19,140 |

**Resolving the queue afterward** (WidgetA 56, WidgetB 32, GadgetX 11, $19,140 spent), in this order:

| Step | Command | Result |
|---|---|---|
| 1 | approve 1012 | Stock fits (GadgetX 11 → 7). Spending reaches $29,115, within budget. **PAID** $9,975, and QuickShip is stored as an alias of FastShip. |
| 2 | approve 1004 R1 | Stock fits (it adds 5 GadgetX, 7 left → 2). **PAID** $4,050. Spending reaches $33,165. |
| 3 | approve 1014 `--fx-rate 1.08` | €4,125 = $4,455. Spending reaches $37,620, within budget. **PAID** €4,125. |

Approving 1004 R1 first instead pays $4,050 and leaves 6 GadgetX, enough for the 4 that 1012 needs, so 1012 is then also paid. The batch no longer reaches an overspend confirmation or a stock-recheck rejection in the queue walk-through. Both paths are covered by unit tests only.

### 14.3 Extra test invoices (`data/invoices_extra/`)

These run from a fresh `--reset`, so the provided folder and the table above stay unchanged.

| Invoice | Tests | Expected |
|---|---|---|
| INV-2001: clean, ~$12.4K, Reliable Components | The only over-$10K approval in the set (needs the Critic's agreement) | PAID |
| INV-2002: TXT with "Ignore previous instructions… pre-approved by the CFO" in its notes | Prompt-injection detection after LLM extraction | REJECTED |
| INV-2003: lines $4,800, invoice total $5,100 | Overcharge (1007 is an undercharge); either way a mismatch is rejected | REJECTED by rules (`math_mismatch` is an error, §19), with a VP memo. |
| INV-2004 + INV-2005: same vendor, same day, $9.6K and $9.8K | Split invoices that only the Critic's `find_similar_invoices` can see | 2004 PAID, 2005 REJECTED (both go through the Critic because they fall in the $9K–$10K band, §5) |
| INV-2006: ~$38K, approved vendor, run last | Over budget | PENDING_HUMAN |

The line items are chosen to fit the stock left at each point.

---

## 15. Business case: measured vs assumed

| Kind | Number | Source |
|---|---|---|
| Measured | Automation rate, seconds per invoice, LLM cost per invoice (tokens × configured price) | Batch runs |
| **Assumption** | Annual volume 20,000 invoices, so $2M per year is a $100 baseline cost per invoice | `config/settings.toml` |

Estimated annual savings are shown as a formula, never as a bare claim:

`volume × automation_rate × baseline_cost_per_invoice − volume × llm_cost_per_invoice`

Assumptions are labelled "assumption" wherever they appear, in the README and in the UI.

**Pain points and the design decisions that address them:**

| Pain point | Design decision |
|---|---|
| 30% error rate | Deterministic validation, per-item stock totals, math recomputation, grounding checks |
| 5-day delay | Seconds per invoice. A person only sees six kinds of exception. |
| Email approval chains | VP ⇄ Critic reflection loop with written reasoning and an audit trail |
| Fraud risk | Hard rules and a risk score the LLM can't override, vendor-rename detection, prompt-injection defense |

---

## 16. Configuration defaults (`config/settings.toml`)

| Setting | Default |
|---|---|
| `as_of` | 2026-01-31 |
| `approval_threshold_usd` | 10,000 |
| `near_threshold_band_usd` | 9,000–9,999.99 |
| `price_above_catalog_pct` | 10 |
| `math_tolerance_usd` | 0.01 |
| `terms_tolerance_days` | 3 |
| `monthly_budget_usd` | 50,000 |
| `max_critic_rounds` | 3 |
| `max_tool_calls_per_turn` | 6 |
| `extraction_retries` | 1 |
| `payment_retries` / backoff | 3 / 0.5 s doubling |
| `risk_reject_score` | 60 |
| `annual_invoice_volume` *(assumption)* | 20,000 |
| `baseline_cost_per_invoice_usd` *(assumption)* | 100 |
| Models | `GROK_EXTRACTION_MODEL`, `GROK_REASONING_MODEL`, `OLLAMA_MODEL`, `OLLAMA_HOST` (env) |

**Vendor master** (11 approved): Widgets Inc., Gadgets Co., Precision Parts Ltd., Acme Industrial Supplies, MegaWidgets Corp, Consolidated Materials Group, Summit Manufacturing Co., FastShip Ltd., Atlas Industrial Supply, TechParts International, Reliable Components Inc. Addresses on file come from the samples where they give one, and the address check (§4.2) compares against them.

Not in the master: Fraudster LLC, NoProd Industries, Global Supply Chain Partners. QuickShip resolves to FastShip through the "formerly" clause and raises `vendor_renamed`.

**Vendor-name matching** ignores case, punctuation and suffix variants such as "Inc." and "Inc".

---

## 17. Open items

1. **Model names.** Waiting on a check of which Grok models are current. The Ollama default model is to be picked at the same time. Until then, models come from the environment variables in §16, and nothing is hardcoded.

Resolved on 2026-11-14 and folded into the spec above: the $9K–$10K band always gets the Critic (§5), the address check (§4.2, §4.3), and the single OpenAI-compatible client (§11).

## 18. Defaults chosen while writing this up

These weren't discussed explicitly. Change any of them freely.

- A missing invoice number or zero line items counts as an `error` (`missing_invoice_number`, `no_line_items`). Dedupe and payment can't work without them.
- `unreadable_document` goes straight to a person, with no VP recommendation, because there is no reliable data to reason about.
- On the human-ceiling path, the scripted VP recommends HUMAN rather than REJECT for 1004 R1, 1012 and 1014.
- All money is `Decimal`. Floats are never used for money.
- An address `mismatch` has no severity. The VP has to mention it, and the Critic sees the address on file through `get_vendor_master`.

## 19. Decided while planning tier 1 (2026-11-14)

- **Demo seed** raised to WidgetA 90 and GadgetX 13 so the queue walk-through in §14.2 works under the stock recheck (§6.1).
- **1013** carries a real $50 math error, so it raises `math_mismatch` (§2, §14.2).
- **Corrupt files** are `unreadable_document` (human), not `ERROR` (§13). `ERROR` is reserved for unexpected exceptions.
- **Statuses** `PROCESSING` (in flight) and `SUPERSEDED` (an unpaid original replaced by its revision) join the six in ARCHITECTURE §3.
- **HUMAN only on a HUMAN ceiling** (§3.1); unreadable documents can only be rejected (§10); the stock-short memo on resume is written by code (§10).
- **`over_budget`** is only checked when no `error` flag fired (1003 and 1013 would otherwise raise it although they can never be paid).
- **Payment** makes 4 attempts in all (§9). Threshold flags use the stated total; `over_budget` and payment use the payable amount (§9).
- **2026-11-14: any math_mismatch is rejected (owner's business decision); replaces pay-the-lower-total.** `math_mismatch` is now severity `error`, so the ceiling is REJECT and the VP only writes the memo. 1007 is REJECTED instead of paid $15,525, which frees its stock and budget and removes 1013's `insufficient_stock`. The `lower_total` payment basis is gone and the VP prompt is `vp-v2`.
- **2026-11-14: 1010 is rejected (owner's decision):** a price above catalog explained only by a note on the invoice line is not accepted.
