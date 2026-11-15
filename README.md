# Acme Invoice Processing: a multi-agent prototype

Acme loses **$2M a year** on manual invoice processing: a 30% error rate, 5-day delays and approvals buried in email chains. This repo automates the whole workflow (**ingest → validate → approve → pay**) as a local, multi-agent system built on **LangGraph** and **Grok**.

The idea in one sentence: **code enforces the hard rules, LLM agents handle messy documents and judgement calls, and a person sees an invoice only for one of six named reasons.**

On the 20 provided sample files (16 distinct invoice numbers):

| Paid | Rejected | Sent to a person | Duplicates caught | Decided without a person |
|---|---|---|---|---|
| 5 ($19,140) | 9 | 3 | 3 | **14 of 17 documents (82%)** |

Every rejection comes with written reasons. Every decision is in an audit log. No invoice is paid twice, and nothing over $10K is approved without a second agent agreeing.

> **Docs map.** This README covers what the system does, how to run it and why it's built this way.
> [DESIGN.md](DESIGN.md) is the full spec (every rule, flag and threshold).
> [ARCHITECTURE.md](ARCHITECTURE.md) covers how the code fits together (nodes, state, tables, tools).
> The original case brief is kept at the [bottom of this file](#appendix-the-original-case-brief).

---

## Contents

1. [Quick start](#1-quick-start)
2. [The dashboard](#2-the-dashboard)
3. [The sample invoices and their expected outcomes](#3-the-sample-invoices-and-their-expected-outcomes)
4. [How it works](#4-how-it-works)
5. [Key decisions and why](#5-key-decisions-and-why)
6. [What we found in the sample data](#6-what-we-found-in-the-sample-data)
7. [CLI reference](#7-cli-reference)
8. [Testing](#8-testing)
9. [Observability and error handling](#9-observability-and-error-handling)
10. [Business impact](#10-business-impact)
11. [Cut on purpose](#11-cut-on-purpose)

---

## 1. Quick start

**Requirements:** [uv](https://docs.astral.sh/uv/) and Python 3.12 (uv installs it if needed). No API key is needed, because recorded Grok responses are committed (see [Replay](#replay-a-recorded-grok-run-instead-of-a-mock)).

```bash
uv sync                                                    # install dependencies
uv run python main.py --reset                              # create acme.db with the demo seed
uv run python main.py --dir data/invoices                  # run all 20 sample files
uv run python main.py --queue                              # see what's waiting for a person
uv run python main.py --resolve INV-1012 --approve --note "Called FastShip on the number on file"
```

The dashboard, over the same database:

```bash
uv run streamlit run ui/app.py                             # opens http://localhost:8501
```

You get four pages (Process, Review queue, Ledger, Batch) and a sidebar showing the model in use, the budget and the stock. No key is needed: without one it replays the recorded Grok run. See [§2](#2-the-dashboard).

Single invoice, in the form the brief asks for:

```bash
uv run python main.py --invoice_path=data/invoices/invoice_1001.txt
uv run python main.py --invoice_path=data/invoices/invoice_1001.txt --json   # one JSON object, nothing else
```

### Running offline

You don't need an API key or network access to see the system work. There are two ways:

- **Run the tests.** `uv run pytest` runs 844 tests offline in about 100 s. They include the golden batch (with a scripted VP and Critic), a replay of the real Grok run, and the dashboard walkthrough. Network sockets are blocked during tests, and the 6 live tests are skipped unless you ask for them with `-m live`. See [§8](#8-testing).
- **Use replay mode.** The real Grok responses from a full batch are committed under `fixtures/replay/`, and replay serves them instead of calling the API. It is chosen automatically when no key is set. To force it, pass `--provider replay` to the CLI, or pick Replay under the dashboard's Run settings:

  ```bash
  uv run python main.py --reset
  uv run python main.py --dir data/invoices --provider replay
  ```

  Replay covers the 20 sample files. It reproduces the recorded run exactly when they are processed as a batch from a fresh reset. A file with no recording, such as your own upload, needs live Grok.

### Live Grok: setting up `.env`

You don't need a `.env` file to run the samples. Without a key, the system replays the committed Grok recording. To call Grok live, copy the template and add your xAI API key:

```bash
cp .env.example .env
# then edit .env and set XAI_API_KEY
```

The template already sets the model names the recording was made with:

```dotenv
XAI_API_KEY=xai-...              # your key; leave empty to use the recording
GROK_EXTRACTION_MODEL=grok-4.7   # reads TXT and PDF invoices
GROK_REASONING_MODEL=grok-4.7    # shared by the VP and the Critic
```

| Variable | Needed for | Notes |
|---|---|---|
| `XAI_API_KEY` | Live Grok | Leave it empty to use the recording. |
| `GROK_EXTRACTION_MODEL` | Live Grok | No default in code: model names come only from the environment. |
| `GROK_REASONING_MODEL` | Live Grok | One model for both the VP and the Critic, with different prompts. |
| `ACME_UI_PROVIDER` | Dashboard, optional | `grok` or `replay` as the dashboard's starting provider. Unset means automatic. |
| `ACME_SETTINGS` | Dashboard, optional | Path to another `settings.toml`, for example to point the dashboard at a scratch database. |

**How it's used:**
- `main.py`, `scripts/record.py` and the dashboard read `.env` from the repo root when they start. A variable already set in your shell takes precedence over the file.
- Once a key is set, Grok is used automatically, and every run's banner shows which provider ran. `--provider replay` forces the recording, as does choosing Replay under the dashboard's Run settings.
- The test suite never loads `.env`. Only `uv run pytest -m live` reads the key from it.
- `.env` is git-ignored, so the key never reaches the repo. `.env.example` contains no secrets.

> **State carries over between runs.** Stock, budget and history live in `acme.db`, just as they would in production. Every run prints a banner with the provider, remaining stock and budget. Run `--reset` to start fresh.

---

## 2. The dashboard

`uv run streamlit run ui/app.py` starts a local web app over the same `service` functions as the CLI. It reads and writes the same `acme.db`, so don't run the dashboard and the CLI at the same time.

![Batch page after running the 20 sample files: a status bar, the results table and the measured figures](docs/screenshots/batch.png)

### The pages

- **Process.** Pick a sample or upload a file, then watch it move through Read, Check, Approve and Finish. The result is a case: the verdict, the checks that fired, the extracted fields and line items beside the source text (flagged lines highlighted), the VP ⇄ Critic exchange as a chat, and what happened next. Uploads are stored under `data/uploads/<hash>/`, which is git-ignored.
- **Review queue.** The invoices waiting for a person, each with its reason, the VP's recommendation and the evidence. Before you decide, it previews what approving would do: the amount paid, stock per item before and after, and the effect on the budget. Approving or rejecting needs a note, and a foreign-currency invoice also needs an exchange rate.
- **Ledger.** Spending against the monthly limit, stock after each payment, and the payments table with a CSV download.
- **Batch.** Run a folder or uploaded files and get the results table, with any row opening as a case. Under it are the measured figures (decided without a person, time, model calls and tokens per invoice) and the savings estimate, with each input labelled as an assumption or a measurement. Model cost shows as not priced yet until tier 3.

![Process page showing INV-1010 rejected for a price above catalog, with the line-items table and the source text](docs/screenshots/case-1010.png)

The **sidebar** is on every page. It shows which model is in use (live Grok, or replay of the recording and when it was made), the budget spent and left, and each item's stock against its starting level. It also holds **Reset demo data** (with a choice between the demo seed and the brief's starting data) and **Run settings**: model provider, bank failure rate, and a toggle that slows replay down on the Process page so the steps can be read.

A run keeps going in the background if you navigate to another page, and the page picks it up when you come back.

### A demo run

1. **Reset** from the sidebar, keeping the demo data.
2. **Batch:** run `data/invoices`. The result is 5 paid, 9 rejected, 3 for a person and 3 duplicates. 14 of 17 distinct documents (82%) were decided without a person, and $19,140.00 of the $50,000.00 budget is spent.
3. **Review queue:** open each of the three items, read the recommendation and approve with a note. INV-1014 needs an exchange rate, for example 1.08.
4. **Ledger:** spending now stands at $37,620.00 after the three approvals, and stock has dropped to match.

These are the numbers in [§3](#3-the-sample-invoices-and-their-expected-outcomes), and `tests/golden/test_ui_walkthrough.py` checks them through the UI.

![Review queue with INV-1014 selected: reason, recommendation, exchange-rate field and the approve and reject controls](docs/screenshots/queue-1014.png)

![Ledger after the walkthrough: spending against the $50,000 limit, stock per item and the payments table](docs/screenshots/ledger.png)

To regenerate the screenshots, run `uv run python scripts/ui_screenshots.py --help`. The tool needs Node with Playwright and a Chromium headless shell.

---

## 3. The sample invoices and their expected outcomes

Run from a fresh `--reset` (demo seed), as-of date **2026-01-31**, monthly budget **$50,000**, in filename order. These are the exact rows asserted by `tests/golden/test_sample_batch.py`, and the recorded Grok run reproduces all 20 of them.

**Who decided:**
- **rules:** an `error` flag fixed the outcome, and the VP only wrote the memo.
- **VP:** the fast path, with the VP alone.
- **VP + Critic:** the reflection loop.

**†** marks a real judgement call by the agents, as opposed to a rule.

| # | File | Vendor · amount | Flags raised | Expected outcome | Why |
|---|---|---|---|---|---|
| 1 | `invoice_1001.txt` | Widgets Inc. · $5,000 | — | **PAID** $5,000 (VP) | Clean and under $9K, so the VP decides alone (fast path). |
| 2 | `invoice_1002.txt` | Gadgets Co. · $15,000 | `insufficient_stock` · `over_threshold` · `past_due` · `terms_mismatch` · `value_normalized` | **REJECTED** (rules) | Asks for 20 GadgetX, but only 13 are in stock. The number "1002" is normalised to `INV-1002`. |
| 3 | `invoice_1003.txt` | Fraudster LLC · $100,000 | `zero_stock_item` · `invalid_due_date` · `unknown_vendor` · `over_threshold` | **REJECTED** (rules) | FakeItem has 0 stock, the due date is "yesterday", the vendor is unknown and the text says "URGENT… wire transfer". |
| 4 | `invoice_1004.json` | Precision Parts Ltd. · $1,890 | — | **PAID** $1,890 (VP) | Clean. The address matches the vendor master (yes, 742 Evergreen Terrace). |
| 5 | `invoice_1004_revised.json` | Precision Parts Ltd. · $5,940 | `revision_of_paid_invoice` | **PENDING_HUMAN** † | A revision of an invoice we already paid (it adds 5 GadgetX). Approving it pays only the **$4,050 difference**. |
| 6 | `invoice_1005.json` | Global Supply Chain Partners · $15,225 | `unknown_vendor` · `over_threshold` | **REJECTED** † (VP + Critic) | The rules allow approval, but this is an unknown vendor at 1600 Pennsylvania Ave billing over $10K. The VP rejects it (vendor not onboarded) and the Critic agrees. |
| 7 | `invoice_1006.csv` | Acme Industrial Supplies · $2,750 | — | **PAID** $2,750 (VP) | Clean. Uses the field/value CSV layout. |
| 8 | `invoice_1007.csv` | MegaWidgets Corp · $15,525 | `math_mismatch` (−$110) · `over_threshold` | **REJECTED** (rules) | Lines of $14,750 plus 6% tax come to $15,635, but the invoice says $15,525. |
| 9 | `invoice_1008.txt` | NoProd Industries · $9,900 | `unknown_item` ×2 · `unknown_vendor` · `near_threshold` · `past_due` | **REJECTED** (rules) | SuperGizmo and MegaSprocket aren't in inventory. |
| 10 | `invoice_1009.json` | *(none)* · −$250 | `non_positive_quantity` · `missing_vendor` · `invalid_due_date` · `math_mismatch` | **REJECTED** (rules) | Negative quantity, no vendor, no due date, and the stated subtotal ($1,000) doesn't match the lines. |
| 11 | `invoice_1010.txt` | Consolidated Materials Group · $7,185 | `price_above_catalog` | **REJECTED** † (VP + Critic) | A "rush" line bills WidgetA at $300 against a $250 catalog price (+20%), while another line bills the same item at $250. A note on the invoice isn't an authorised price exception. |
| 12 | `invoice_1011.pdf` | Summit Manufacturing Co. · $3,000 | — | **PAID** $3,000 (VP) | Clean. The PDF text is read with pdfplumber and extracted by the LLM. |
| 13 | `invoice_1011.txt` | Summit Manufacturing Co. · $3,000 | — | **DUPLICATE** of #12 | The same invoice in another format. It is never paid twice. |
| 14 | `invoice_1012.pdf` | QuickShip Distributers · $9,975 | `vendor_renamed` · `near_threshold` · `value_normalized` | **PENDING_HUMAN** † | "(formerly FastShip Ltd.)" is a classic impersonation pattern. Verifying it means calling the known contact, which only a person can do. OCR errors (`2O26`, `$3,500.O0`) are normalised. |
| 15 | `invoice_1012.txt` | QuickShip Distributers · $9,975 | — | **DUPLICATE** of #14 | |
| 16 | `invoice_1013.json` | Atlas Industrial Supply · $22,562.80 | `over_threshold` · `price_below_catalog` · `math_mismatch` (+$50) | **REJECTED** (rules) | Lines of $21,040 plus 7% tax come to $22,512.80, but the invoice says $50 more. `generate_pdfs.py` adds that $50. |
| 17 | `invoice_1013.pdf` | Atlas Industrial Supply · $22,562.80 | — | **DUPLICATE** of #16 | |
| 18 | `invoice_1014.xml` | TechParts International · €4,125 | `foreign_currency` | **PENDING_HUMAN** † | Invoiced in EUR. Exchange rates move, so a person enters the rate. |
| 19 | `invoice_1015.csv` | Reliable Components Inc. · $6,500 | — | **PAID** $6,500 (VP) | Clean. Uses the tabular CSV layout. |
| 20 | `invoice_1016.json` | Widgets Inc. · $3,233 | `unknown_item` (WidgetC) | **REJECTED** (rules) | WidgetC isn't in inventory. It is deliberately **not** fuzzy-matched to WidgetA (see [§5](#no-fuzzy-auto-correction-of-item-names)). |

Every scenario in the brief's own table behaves as the brief expects:
- **1001, 1004 and 1006** pass.
- **1002** is a stock mismatch.
- **1003** is out of stock.
- **1008 and 1016** have unknown items.
- **1009** is invalid data.

The other eight invoice numbers hold problems the brief didn't mention ([§6](#6-what-we-found-in-the-sample-data)).

### Stock and budget after each payment

| After | WidgetA | WidgetB | GadgetX | Budget spent |
|---|---|---|---|---|
| start | 90 | 50 | 13 | $0 |
| 1001 | 80 | 45 | 13 | $5,000 |
| 1004 | 77 | 43 | 13 | $6,890 |
| 1006 | 72 | 40 | 13 | $9,640 |
| 1011 | 66 | 37 | 13 | $12,640 |
| 1015 | 56 | 32 | 11 | $19,140 |

### Resolving the human queue

`--queue` then lists three items. Resolving them in this order is also a golden test (`tests/golden/test_queue_resolution.py`):

| Step | Command | Expected result |
|---|---|---|
| 1 | `--resolve INV-1012 --approve --note "..."` | Stock is rechecked (GadgetX 11 → 7). **PAID $9,975**, and "QuickShip Distributers" is stored as an alias of FastShip Ltd., so the next invoice under that name won't be flagged. Spent: $29,115. |
| 2 | `--resolve INV-1004 --approve --note "..."` | Only the 5 added GadgetX are checked and used (7 → 2). **PAID $4,050** (the difference). Spent: $33,165. |
| 3 | `--resolve INV-1014 --approve --note "..."` | **Refused** with *"An exchange rate to USD is required for EUR. Add --fx-rate RATE."* |
| 4 | `--resolve INV-1014 --approve --fx-rate 1.08 --note "..."` | **PAID €4,125**, which is $4,455 against the budget. Spent: $37,620. The queue is now empty. |

`--resolve REF` with no `--approve` or `--reject` is a **preview**. It shows what approving would pay, any stock shortfall, any budget overrun and any exchange rate needed, all before you commit.

---

## 4. How it works

### 4.1 The four stages

```mermaid
flowchart LR
    file(["Invoice file<br/>PDF, TXT, JSON, CSV, XML"]) --> ingest["1. Ingest<br/>code, plus an LLM for TXT and PDF"]
    ingest --> validate["2. Validate<br/>code only"]
    validate --> approve["3. Approve<br/>LLM agents, capped by code"]
    approve --> pay["4. Pay<br/>code only"]
    approve --> person[["Person<br/>six reasons only"]]
    person --> pay
    validate --> duplicate(["Duplicate"])
    approve --> rejected(["Rejected"])
    pay --> paid(["Paid"])
```

| Stage | Deterministic? | What it does |
|---|---|---|
| **1. Ingest** | Code for JSON, CSV and XML. For TXT and PDF, an LLM (Grok) extracts the fields and code checks the result. | Turns the file into a structured invoice: vendor, number, dates, line items and totals. For TXT and PDF, code confirms that every extracted value actually appears in the source text. A failure gets one retry, then goes to a person. Values are normalised (`INV-NNNN`, ISO dates, `Decimal` money). |
| **2. Validate** | Code | Catches duplicates and revisions. Checks every line against stock and catalog prices, recomputes the totals, and checks the vendor, its address, the due date and the budget. Each problem raises a flag, and the worst flag sets the **ceiling**: the best outcome the rules allow. |
| **3. Approve** | LLM agents (VP and Critic), capped by code | The VP decides using lookup tools and must answer every escalation flag in writing. Above $9,000, or with an escalation flag, the Critic reviews it with more tools and can send it back, for up to three rounds. Code then caps the decision at the ceiling. When the ceiling is Reject, the VP only writes the rejection memo. |
| **4. Pay** | Code | Calls the brief's `mock_payment` unchanged, with retries and idempotency, and updates stock and budget in one transaction. If payment keeps failing, the invoice goes to a person. |
| **Person** | A person decides; code rechecks | Only for six reasons: foreign currency, a revision or reused invoice number, over budget, a renamed vendor, a failed payment, or an unreadable document. The run pauses and resumes from the CLI or the dashboard, and stock and budget are checked again before paying. |

**Only three steps call a model:** extraction from TXT and PDF, the VP, and the Critic. Everything that decides whether money can move is code: the flags, the ceiling, the cap on the agents' decision, the stock and budget checks, and the payment. The agents can make an outcome stricter, never more generous.

**Around the stages:** the CLI and the dashboard are thin front ends over the same service. LangGraph runs the stages, and can pause an invoice for a person and resume it later. SQLite holds stock, budget, invoices, payments and paused runs, and every step is logged as JSON lines with a trace ID. [ARCHITECTURE.md](ARCHITECTURE.md) has the module-level detail.

### 4.2 The decision ceiling

Every flag has a **severity**, and the worst severity sets the **ceiling**: the best outcome the rules allow. The final decision is the agents' decision capped at the ceiling, in the order Reject < Person < Approve.

| Severity | Effect | Examples |
|---|---|---|
| `error` | Ceiling REJECT. The LLM has no say in the outcome. | `insufficient_stock`, `unknown_item`, `zero_stock_item`, `math_mismatch`, `non_positive_quantity`, `missing_vendor`, `invalid_due_date` |
| `human` | Ceiling HUMAN | `foreign_currency`, `vendor_renamed`, `revision_of_paid_invoice`, `conflicting_invoice_number`, `over_budget`, `payment_failed`, `unreadable_document` |
| `escalation` | No change, but the VP must answer it in writing, and it brings in the Critic | `unknown_vendor`, `price_above_catalog` |
| `info` | Context for the VP and the person | `over_threshold`, `near_threshold`, `past_due`, `terms_mismatch`, `value_normalized`, `price_below_catalog` |

The full catalogue (26 codes, with when each fires) is in [DESIGN.md §3.3](DESIGN.md).

### 4.3 The VP ⇄ Critic reflection loop

```mermaid
flowchart LR
    vp["VP decides<br/>looks up stock, budget,<br/>vendor history and policy"] --> check{"Over $9,000, an escalation<br/>flag, or a Person ceiling?"}
    check -->|no| cap["Code caps the decision<br/>at the ceiling"]
    check -->|yes| critic["Critic reviews<br/>also checks the source text,<br/>totals and split invoices"]
    critic -->|"objects, up to 3 rounds"| vp
    critic -->|agrees| cap
```

| Situation | Path |
|---|---|
| Ceiling REJECT | The VP writes the memo only. No Critic. |
| Ceiling APPROVE, **under $9,000**, no escalation flag | **Fast path:** the VP alone. It may approve or be stricter. |
| Everything else | **VP ⇄ Critic**, up to 3 Critic reviews |

An approval over $10,000 needs the Critic's agreement, and if the two still disagree after three rounds, the stricter decision wins. The Critic has **more tools than the VP**, so the reviewer is better grounded than the decider: it can check source lines, recompute totals, look for split invoices and read the vendor master.

### 4.4 Duplicates and revisions

| Case | Detected by | Result |
|---|---|---|
| Exact same file run again | SHA-256 of the bytes | The stored result is returned and nothing is re-run |
| Same invoice, different format (1011, 1012, 1013) | Economic fingerprint: vendor, number, lines, total, currency | `DUPLICATE`, never paid |
| Same vendor and number, different content, no revision marker | Same vendor and number, different fingerprint | `conflicting_invoice_number` → person |
| Marked revision of a **paid** invoice (1004 R1) | `revision` field or a "revised" note | `revision_of_paid_invoice` → person. Approving pays the difference; if that is ≤ 0, the status is `CREDIT_DUE`. |
| Marked revision of an **unpaid** invoice | Same | The revision replaces it, and the original becomes `SUPERSEDED` |

---

## 5. Key decisions and why

### Rules set a ceiling; the LLM can only be stricter

The LLM picks an outcome **at or below** what the rules allow, and code takes `min(llm_decision, ceiling)`. If the model proposes something above the ceiling, the clamp logs a `ceiling_clamped` event.

**Why:** in a payment system the worst failure is paying something you shouldn't. An LLM is useful for judgement, such as whether an unknown vendor over $10K is acceptable (1005) or whether a rush-line note justifies a 20% markup (1010). It shouldn't be able to overturn "this item has zero stock". This keeps the model's mistakes on the safe side: it can wrongly reject, but it can never approve what the rules forbid.

### A closed list of six reasons for human review

An invoice reaches a person **only** for these reasons:
1. Foreign currency
2. Revision or conflicting number
3. Over budget
4. Vendor renamed
5. Payment still failing after retries
6. Unreadable document

**Why:** the pain point is a 5-day delay. If "send it to a person" is the escape hatch for anything uncertain, automation quietly collapses back into manual review. Each of the six is something an AI genuinely can't do, like phoning a vendor, setting an FX rate or authorising an overspend. A VP/Critic **deadlock resolves to the stricter decision**, not to a person, and a HUMAN proposal under an APPROVE ceiling becomes REJECT.

### Any math mismatch is rejected

If line amounts, the subtotal or the total are off by more than $0.01, in either direction, `math_mismatch` is an `error` flag and the invoice is rejected.

**Why:** this was an owner's business decision, made during the build. An earlier version paid the lower of the stated and recomputed totals. But an invoice that doesn't add up is either careless or manipulated, and in both cases the vendor should reissue it. That way nobody has to decide which number is "right". This is why 1007 (−$110) and 1013 (+$50) are rejected.

### A price above catalog explained only by a note is rejected (1010)

**Why:** this was also an owner's decision. A line note like "rush" is the vendor's claim, not an authorised price exception, and the same invoice bills the same item at catalog price on another line. The VP and Critic reach this through `get_policy`, so it's a judgement row (†), not a hard rule.

### The $9K–$10K band always gets the Critic

Invoices from $9,000 up to $10,000 skip the fast path, even with no other flags.

**Why:** that's where invoices get split to stay under a $10K approval threshold. Only the Critic's `find_similar_invoices` tool can see the other half of a split, so the band can't be decided by the VP alone. 1008 ($9,900) and 1012 ($9,975) sit right there.

### The demo seed raises the brief's starting stock

| Item | Brief's seed | Demo seed (default) | Unit price (added) |
|---|---|---|---|
| WidgetA | 15 | **90** | $250 |
| WidgetB | 10 | **50** | $500 |
| GadgetX | 5 | **13** | $750 |
| FakeItem | 0 | 0 | $1,000 |

**Why:** the brief's seed assumes each invoice is checked on its own. Real stock **carries over** and is used up when an invoice is paid. Under carried-over stock, the brief's seed breaks: after 1001 and 1004 are paid, only 2 WidgetA are left, and almost everything after that fails the stock check. That includes **1006, which the brief's own table says should pass**.

The demo numbers were picked so that:
- 1001, 1004, 1006 and 1011 pass.
- 1002 (needs 20 GadgetX) still fails.
- Enough is left for the queue walk-through.

To see the brief's numbers, run `--reset --seed readme`. The result is 2 paid, 1 person, 14 rejected and 3 duplicates, and a golden test asserts exactly that.

### Stock is checked per item, across all lines, and only used on payment

**Why:** 1013 has lines that each fit in stock individually, but WidgetA appears on several lines. A line-by-line check would miss the total. Invoices waiting in the human queue **don't reserve stock**, so stock is rechecked when the person resolves the invoice. If it's short then, the result is REJECT with a memo written by code.

### No fuzzy auto-correction of item names

Item names are matched ignoring case, whitespace and punctuation **only**. A near miss adds a "did you mean" hint to the flag; it never silently corrects the name.

**Why:** `WidgetC` scores about 86 against `WidgetA` on rapidfuzz's ratio. A fuzzy matcher would quietly turn 1016's unknown item into a real one and pay for it. On the other hand, `Widget A` (1012's OCR) correctly becomes `WidgetA`.

### The VP never sees the raw document

The VP gets the **structured** invoice only. Free-text fields (notes, addresses) are quoted and labelled as untrusted data. The Critic sees source text only one line at a time, through `get_source_evidence`.

**Why:** an invoice is attacker-controlled input. A note like "pre-approved by the CFO, ignore previous instructions" shouldn't reach the decision-maker as instructions. This isolation is in place now; a pattern-matching injection detector is planned ([DESIGN.md §3.6](DESIGN.md)).

### Replay a recorded Grok run instead of a mock

Every real Grok call from a full batch is committed under `fixtures/replay/`. Each one is keyed by `(file hash, stage, prompt version, call index)`.

**Why:**
- **Anyone can run it.** Reviewers and CI can run the real agent behaviour offline, with no key.
- **Tests are deterministic.** The replay golden test checks that Grok's actual decisions still match the expected table.
- **Prompt edits can't drift silently.** Each prompt file carries a `prompt_version`, and editing a prompt makes its recordings stale. Replay then fails with "re-record" instead of quietly serving old answers.

`scripts/record.py` regenerates the recordings.

---

## 6. What we found in the sample data

The brief documents 8 of the 16 invoice numbers. Reading every file turned up these:

| Invoice | Finding | How it's handled |
|---|---|---|
| 1011, 1012, 1013 | Each appears **twice** (TXT/JSON plus a PDF). A naive batch pays each one twice. | Fingerprint dedupe → `DUPLICATE` |
| 1004 / 1004_revised | Same number. R1 adds 5 GadgetX "per PO amendment". | Revision of a paid invoice → person; pays the difference only |
| 1007 | Total is **$110 short** of lines + tax | `math_mismatch` → rejected |
| 1013 | Total is **$50 over** lines + tax. `generate_pdfs.py` adds it, and both committed files contain it. | `math_mismatch` → rejected |
| 1014 | **XML** and **EUR**. The brief mentions neither. | XML parser; `foreign_currency` → person enters the rate |
| 1006 vs 1007/1015 | Two different **CSV layouts** (field/value rows vs a table) | Two CSV parsers |
| 1012 | OCR damage (`2O26`, `$3,500.O0`, `Widget A`, `INV 1012`) and a vendor "formerly FastShip": an impersonation pattern, just under $10K | Normalised with `value_normalized`; `vendor_renamed` → person |
| 1010 | The same item on two lines, one a $300 "rush" line vs a $250 catalog price | `price_above_catalog` → VP + Critic reject |
| 1005 | Unknown vendor at 1600 Pennsylvania Ave, over $10K | VP + Critic reject |
| 1002 | Due date equals the invoice date despite Net 30. The number lacks the `INV-` prefix. | `terms_mismatch`, `value_normalized` (info) |
| 1008 | Email-style text, unknown vendor, $9,900, just under the threshold | Unknown items → rejected; the near-threshold band would bring in the Critic anyway |

**Every invoice over $10K in the sample set fails at least one check.** So the "approval over $10K needs the Critic's agreement" path is covered by unit tests, not by a sample.

---

## 7. CLI reference

```bash
python main.py --invoice_path=FILE          # one invoice (the brief's form; --invoice-path also works)
python main.py --dir DIR                    # batch, with a summary table and totals
python main.py --queue                      # list invoices waiting for a person
python main.py --resolve REF                # preview: what approving would do
python main.py --resolve REF --approve --note "..." [--fx-rate 1.08] [--confirm-overspend]
python main.py --resolve REF --reject  --note "..."
python main.py --reset [--seed demo|readme] # restore starting stock and budget, clear history
```

| Common flag | Purpose |
|---|---|
| `--provider grok\|replay` | Choose the model provider. By default: Grok if `XAI_API_KEY` is set, else replay if every file has a recording, else exit with a message saying how to get a model. |
| `--json` | Turn off Rich and print exactly one JSON object to stdout |
| `--as-of YYYY-MM-DD` | The date used for past-due checks (default 2026-01-31) |
| `--payment-fail-rate R` | Inject payment failures (0–1) to demonstrate retries and `payment_failed` |

**What you see:**
- **Run banner:** the provider and recording date, the seed, the as-of date, the budget used and stock per item.
- **Per invoice:** live stage-by-stage progress, then a coloured flags table and a decision panel with the VP's reasoning, the Critic's verdict and every tool each one called.
- **Batch table:** a **Reason** column for every rejection.
- **Queue:** the reason, the agents' recommendation and the amount.

**Exit codes:**
- `0`: ok
- `1`: error
- `2`: usage
- `3`: resolution refused (for example, a missing `--fx-rate` or overspend confirmation)

---

## 8. Testing

```bash
uv run pytest            # 844 tests, offline, about 100 s
uv run pytest -m live    # 6 tests against real Grok (needs .env)
```

---

## 9. Observability and error handling

**Structured logs:** `logs/YYYY-MM-DD.jsonl` holds one event per line. Each event carries a `trace_id`; the logger refuses events without one.

```json
{"ts": "2026-11-15T00:52:47.294734+00:00", "trace_id": "inv-invoice-1001-48ef7153",
 "invoice": "invoice_1001.txt", "node": "extraction", "event": "llm_call", "duration_ms": 5757,
 "tokens_in": 3298, "tokens_out": 181, "provider": "replay", "data": {"stage": "extraction", "...": "..."}}
```

The event types are `node_start`, `node_end`, `llm_call`, `tool_call`, `flag_raised`, `ceiling_clamped`, `retry`, `interrupt`, `resume`, `payment` and `error`. The CLI's live progress view is driven by the real graph event stream, not a separate animation.

**Audit trail:** every business decision (VP and Critic turns, clamps, human resolutions, payments) is written to the `audit_log` table, with an actor of `system`, `vp`, `critic` or `human`.

| Failure | Handling |
|---|---|
| File can't be parsed, or extraction fails grounding twice | `unreadable_document` → person (reject only; fix the file and re-run) |
| LLM returns an invalid structured output | One retry with the validation error, then the node raises |
| Unexpected exception in any node | That invoice is `ERROR` and the traceback is logged with its `trace_id`. Nothing is paid and the batch continues. |
| Replay recording missing or stale | `ERROR`, with a "re-record" hint |
| Payment keeps failing | 4 attempts with backoff, then `payment_failed` → person |

---

## 10. Business impact

| Pain point | What addresses it |
|---|---|
| **30% error rate** | Deterministic validation, per-item stock totals, independent math recomputation, grounding checks on every extracted value |
| **5-day delays** | Seconds per invoice. A person only sees six named kinds of exception, and 82% of the sample documents needed nobody. |
| **Email approval chains** | The VP ⇄ Critic loop produces written reasoning and a full audit trail, and `--resolve` gives a person a one-line decision with a required note |
| **Fraud risk** | Hard rules the LLM can't override, vendor-rename detection, an address check against the vendor master, split-invoice detection in the $9K–$10K band, and the VP isolated from raw document text |

**Estimating savings.** The formula is:

`annual volume × automation rate × baseline cost per invoice − annual volume × LLM cost per invoice`

The inputs come from two places:
- **Automation rate** is measured: 82% on the sample batch. The samples are deliberately problem-heavy, so a real mix of mostly clean invoices should do better.
- **Annual volume** (20,000 invoices, so $2M a year works out to $100 per invoice) is an **assumption**, set in `config/settings.toml`.

With those inputs the gross figure is about **$1.6M a year**, before LLM cost. Tokens and model calls per invoice are measured and shown in the dashboard; pricing them is tier 3.

---

## 11. Cut on purpose

| Cut | Instead |
|---|---|
| Email inbox ingestion | Folder input (`--dir`) |
| OCR for scanned images | PDFs need a text layer (all three samples have one) |
| Login and multiple users | One local operator |
| Real banking | `mock_payment` wrapped with idempotency and retries |
| Live FX rates | Foreign-currency invoices go to a person, who enters the rate |

---

## Appendix: the original case brief

<details>
<summary>The case brief as provided (click to expand)</summary>

### Background

Acme Corp is a PE-backed manufacturing firm losing **$2M/year** on manual invoice processing. Invoices arrive via email as PDFs in messy formats with frequent errors. Staff manually extract data, validate against a legacy inventory database (inconsistent), obtain VP approval (via email chains), and process payment (via a banking API).

**Current pain points:** 30% error rate · 5-day processing delays · frustrated stakeholders.

### Objective

Build a **multi-agent system** that automates the end-to-end invoice processing workflow. The system must run as a working prototype, not just designs or slides.

### Workflow

1. **Ingestion:** Extract structured data from invoice documents (PDFs, text files). Fields include Vendor, Amount, Items (with quantities) and Due Date. Expect unstructured text, typos, missing data and potentially fraudulent entries.
2. **Validation:** Verify extracted data against a mock inventory database (SQLite). Flag mismatches such as quantity exceeding available stock or items not found in inventory.
3. **Approval:** Simulate VP-level review with rule-based decision-making (e.g., invoices over $10K require additional scrutiny). The agent should reason through approval/rejection with a reflection or critique loop.
4. **Payment:** If approved, call a mock payment function. If rejected, log the rejection with reasoning.

### Technical requirements

- **LLM integration:** xAI's Grok as the core reasoning engine (other models are acceptable without an API key).
- **Multi-agent orchestration:** LangGraph, CrewAI, AutoGen, or a custom solution.
- **Agent capabilities:** function calling / tool use, structured outputs, self-correction loops.
- **Runtime:** assume no internet for external APIs; simulate everything locally.
- **Tech stack:** Python, run locally, no cloud deployment.

### Provided resources

**Mock invoice data** in `data/invoices/` (PDF, CSV, JSON, TXT), mixing clean and problematic entries.

**Mock inventory database (starter seed):**

```python
cursor.execute('CREATE TABLE IF NOT EXISTS inventory (item TEXT PRIMARY KEY, stock INTEGER)')
cursor.execute("""
    INSERT INTO inventory VALUES
    ('WidgetA', 15), ('WidgetB', 10), ('GadgetX', 5), ('FakeItem', 0)
""")
```

| Scenario | Invoice | What should happen |
|---|---|---|
| Normal order within stock | INV-1001, INV-1004, INV-1006 | Items found, quantities valid; passes validation |
| Quantity exceeds stock | INV-1002 (20× GadgetX, only 5 in stock) | Flagged as stock mismatch |
| Fraudulent / zero-stock item | INV-1003 (FakeItem, 0 stock) | Flagged as out of stock or suspicious |
| Item not in database at all | INV-1008 (SuperGizmo, MegaSprocket), INV-1016 (WidgetC) | Flagged as unknown item |
| Invalid data | INV-1009 (negative quantity) | Flagged as data integrity issue |

**Mock payment API:**

```python
def mock_payment(vendor, amount):
    print(f"Paid {amount} to {vendor}")
    return {"status": "success"}
```

**Grok API setup (as given):**

```python
from xai import Grok
client = Grok(api_key="your_key")
response = client.chat.completions.create(model="grok-3", messages=[...])
```

### Running the system

```bash
python main.py --invoice_path=data/invoices/invoice1.txt
```

### Evaluation criteria

Functionality · Code quality · Agentic sophistication · Shipping mindset · Presentation · Above/beyond · UI/UX

### Submission

A link to a public GitHub repository.

</details>
