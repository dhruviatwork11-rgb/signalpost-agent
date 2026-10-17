# Signalpost Agent

**Signalpost Agent** is a production-grade company research agent built for the **Signalpost company research challenge hosted on [Builderr](https://builderr.ai/challenges/signalpost)**.

The agent accepts batches of Norwegian organization numbers (`organisasjonsnummer`), queries official public registers, validates and normalizes entity data, and emits verified company profiles with source URLs, UTC timestamps, SHA-256 payload hashes, and idempotent update tracking in strict compliance with the **Signalpost evaluation contract**.

---

## Evaluation & Competition Specifications

| Item | Specification |
|---|---|
| **Official Evaluator Contract** | [`OUTPUT_CONTRACT.md`](file:///d:/signalpost%20agent/OUTPUT_CONTRACT.md) terminal JSONL envelope |
| **Primary Data Source** | Official [Brønnøysund Register Centre API (Enhetsregisteret)](https://data.brreg.no/enhetsregisteret/api/dokumentasjon/en/index.html) |
| **Data Licence** | [Norwegian Licence for Open Government Data (NLOD 2.0)](https://data.norge.no/nlod/en/2.0) |
| **Software Licence** | MIT |
| **External AI Models** | None required (deterministic registry resolution; zero LLM hallucination risk) |
| **Expected External API Cost** | **$0.00 USD / 0 NOK** per run (official open public data) |
| **Outbound Requests** | Bounded concurrency (default 4 workers, max 8) with exponential backoff on transient errors |

---

## Clean-Environment Windows Setup

### Prerequisites
- Windows 10/11
- Python 3.11 or newer (Python 3.12 recommended)

### 1. Installation Command
Run the following commands in PowerShell or Command Prompt:

```powershell
# Create an isolated virtual environment
python -m venv .venv

# Activate the virtual environment (PowerShell)
.venv\Scripts\Activate.ps1

# (Optional: If script execution is restricted on Windows)
# Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope Process

# Install pinned dependencies
pip install -r requirements.txt
```

---

## One-Command Batch Execution

To run the agent on an evaluator-supplied batch of companies:

```powershell
python -m app.main batch --input data/smoke_test_100_companies.json --output out/envelopes.jsonl --report out/report.json
```

### CLI Arguments
- `--input`, `-i`: Path to batch file containing organization numbers (supports `.json`, `.jsonl`, or `.txt`).
- `--output`, `-o`: Output destination for the terminal envelope stream (`.jsonl`).
- `--report`, `-r`: Optional destination for the machine-readable run summary (`.json`).
- `--workers`, `-w`: Bounded worker threads (default `4`, max `8`).
- `--run-id`: Optional identifier for the evaluation run.

---

## Input & Output Contracts

### 1. Supported Input Batch Formats
The batch runner automatically parses and validates any of the following input formats:
- **JSON array of strings**: `["923609016", "984851006", ...]`
- **JSON array of objects**: `[{"organisation_number": "923609016"}, {"organisasjonsnummer": "984851006"}]`
- **JSON root mapping**: `{"organisation_numbers": ["923609016", "984851006"]}`
- **JSON Lines (`.jsonl`)**: `{"organisation_number": "923609016"}\n{"organisation_number": "984851006"}`
- **Plain text (`.txt`)**: One 9-digit organization number per line.

### 2. Output Terminal Envelope Format (`.jsonl`)
Every input company produces **exactly one** terminal JSON object adhering to Builderr's official `OUTPUT_CONTRACT.md`:

```json
{
  "organisation_number": "923609016",
  "run": {
    "run_id": "run-1792230803",
    "started_at": "2026-10-17T09:53:23.577591Z",
    "completed_at": "2026-10-17T09:53:25.358296Z",
    "terminal_status": "completed"
  },
  "claims": [
    {
      "field": "legal_name",
      "value": "EQUINOR ASA",
      "availability": "available",
      "confidence": 1.0,
      "evidence_ids": ["ev-1"]
    },
    {
      "field": "legal_form",
      "value": "ASA",
      "availability": "available",
      "confidence": 1.0,
      "evidence_ids": ["ev-1"]
    },
    {
      "field": "registration_date",
      "value": "1997-09-08",
      "availability": "available",
      "confidence": 1.0,
      "evidence_ids": ["ev-1"]
    },
    {
      "field": "nace_code",
      "value": "06.100",
      "availability": "available",
      "confidence": 1.0,
      "evidence_ids": ["ev-1"]
    },
    {
      "field": "business_address",
      "value": "Forusbeen 50, 4035 STAVANGER, Norge",
      "availability": "available",
      "confidence": 1.0,
      "evidence_ids": ["ev-1"]
    },
    {
      "field": "municipality",
      "value": "STAVANGER",
      "availability": "available",
      "confidence": 1.0,
      "evidence_ids": ["ev-1"]
    },
    {
      "field": "official_website",
      "value": "www.equinor.com",
      "availability": "available",
      "confidence": 1.0,
      "evidence_ids": ["ev-12"]
    },
    {
      "field": "employees",
      "value": 21231,
      "availability": "available",
      "confidence": 1.0,
      "evidence_ids": ["ev-13"]
    },
    {
      "field": "registered_in_vat_register",
      "value": true,
      "availability": "available",
      "confidence": 1.0,
      "evidence_ids": ["ev-14"]
    },
    {
      "field": "latest_submitted_accounts",
      "value": "2025",
      "availability": "available",
      "confidence": 1.0,
      "evidence_ids": ["ev-15"]
    },
    {
      "field": "deleted",
      "value": null,
      "availability": "not_available",
      "confidence": 0.0,
      "evidence_ids": []
    },
    {
      "field": "profile_summary",
      "value": "EQUINOR ASA (org nr: 923609016) is a registered Norwegian Allmennaksjeselskap based in STAVANGER with status: active and in normal standing. Key verified details: registered on 1995-03-12; industry: Utvinning av råolje (NACE 06.100); 21231 registered employees; latest submitted annual accounts for 2025; registered in the VAT register; website: www.equinor.com.",
      "availability": "available",
      "confidence": 1.0,
      "evidence_ids": ["ev-21"]
    }
  ],
  "evidence": [
    {
      "id": "ev-1",
      "source_url": "https://data.brreg.no/enhetsregisteret/api/enheter/923609016",
      "source_class": "official_registry",
      "retrieved_at": "2026-10-17T10:29:55.007784Z",
      "content_sha256": "185340bb068fa95a2d51729b1b94cda7d6daf7aabb5b2e02335ba876ec992226",
      "claim_span": "navn: EQUINOR ASA, organisasjonsnummer: 923609016"
    },
    {
      "id": "ev-12",
      "source_url": "https://data.brreg.no/enhetsregisteret/api/enheter/923609016",
      "source_class": "official_registry",
      "retrieved_at": "2026-10-17T10:29:55.007784Z",
      "content_sha256": "185340bb068fa95a2d51729b1b94cda7d6daf7aabb5b2e02335ba876ec992226",
      "claim_span": "hjemmeside: www.equinor.com"
    },
    {
      "id": "ev-13",
      "source_url": "https://data.brreg.no/enhetsregisteret/api/enheter/923609016",
      "source_class": "official_registry",
      "retrieved_at": "2026-10-17T10:29:55.007784Z",
      "content_sha256": "185340bb068fa95a2d51729b1b94cda7d6daf7aabb5b2e02335ba876ec992226",
      "claim_span": "antallAnsatte: 21231"
    },
    {
      "id": "ev-21",
      "source_url": "https://data.brreg.no/enhetsregisteret/api/enheter/923609016",
      "source_class": "official_registry",
      "retrieved_at": "2026-10-17T10:29:55.007784Z",
      "content_sha256": "185340bb068fa95a2d51729b1b94cda7d6daf7aabb5b2e02335ba876ec992226",
      "claim_span": "Enhetsregisteret official record for EQUINOR ASA (organisasjonsnummer 923609016)"
    }
  ],
  "changes": [],
  "errors": [],
  "operations": {
    "requests": 1,
    "runtime_ms": 2000,
    "third_party_cost_usd": 0.0
  }
}
```

### 3. Rules Enforced
- **Zero Silent Drops**: If 100 organizations are supplied, exactly 100 envelopes are emitted.
- **Closed Status Taxonomy**: Every claim carries an explicit availability status: `available`, `not_available`, `blocked`, `not_applicable`, `ambiguous`, or `failed`.
- **Claim-Specific Evidence Spans**: Each available claim is backed by an unhallucinated excerpt directly from the official registry payload.
- **No Hallucinations**: Missing attributes remain `null`, never replaced with `0` or false claims.
- **Preserve Valid Zeroes**: Legitimate zero counts (e.g. `employees: 0`) and boolean `false` values (e.g. `bankrupt: false`) are preserved with `availability: available`, distinct from missing data.
- **Deterministic Synthesis**: Generates a verifiable executive summary (`profile_summary`) without ungrounded LLMs.
- **Idempotent Refreshes**: Repeated runs on identical source bytes emit `changes: []`, preventing false change alerts.
- **Transactional Persistence**: Backed by SQLite with `PRAGMA foreign_keys = ON;`, versioned snapshots, and change audits.

---

## Single Company Live Lookup & View

Initialize local SQLite schema:
```powershell
python -m app.main init-db
```

Perform an interactive single-company live registry lookup:
```powershell
python -m app.main lookup 923609016
```

View cached profile and audit history offline:
```powershell
python -m app.main view 923609016
```

---

## Running the Automated Test Suite

All 37 automated tests run completely offline with mocked HTTP responses and isolated databases:

```powershell
pytest -v
```

Test coverage includes:
- **100-company smoke test**: Validates 100 inputs producing 100 compliant envelopes with zero silent drops.
- **Expanded claims & evidence test**: Validates website, employees, VAT, accounts, sector, municipality, and claim-specific evidence spans.
- **Deterministic synthesis test**: Verifies grounded `profile_summary` generation without external LLMs.
- **Idempotency tests**: Verifies rerunning identical snapshots produces zero false diffs.
- **Format tests**: Verifies `.json` (arrays, dicts), `.jsonl`, `.txt` (with/without UTF-8 BOM), and Norwegian key variants.
- **Error classification**: Verifies HTTP 404, timeouts, connection drops, and org number mismatches.
- **Database & Foreign Keys**: Verifies atomic rollback and constraint enforcement.

---

## Competition Submission Summary

According to the official Signalpost submission requirements:

| Submission Item | Declared Specification |
|---|---|
| **Run Command** | `python -m app.main batch --input <INPUT_PATH> --output <OUTPUT_PATH> --report <REPORT_PATH>` |
| **100-Company Smoke Dataset** | [`data/smoke_test_100_companies.json`](file:///d:/signalpost%20agent/data/smoke_test_100_companies.json) |
| **Output Contract** | Emits 1 terminal JSONL envelope per input following [`OUTPUT_CONTRACT.md`](file:///d:/signalpost%20agent/OUTPUT_CONTRACT.md) |
| **Declared Models** | None (100% deterministic rule-based extraction and synthesis) |
| **Declared APIs** | Official Norwegian Enhetsregisteret API (`data.brreg.no`) under NLOD 2.0 |
| **Third-Party Cost** | **$0.00 USD / 0 NOK** per 100-company run |
| **Submission Email** | `submit@builderr.ai` |
