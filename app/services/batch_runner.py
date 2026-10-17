"""Competition batch runner implementing the official Signalpost evaluation contract."""

from concurrent.futures import ThreadPoolExecutor, as_completed
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
import time
from typing import Any, Callable, Optional

from app.config import DATABASE_PATH, REQUEST_TIMEOUT_SECONDS
from app.models import CompanyProfile, SourceEvidence, utc_now
from app.services.brreg_client import (
    BrregClient,
    BrregError,
    BrregHttpError,
    BrregInvalidOrgNrError,
    BrregMalformedDataError,
    BrregNetworkError,
    BrregNotFoundError,
    BrregOrgMismatchError,
    BrregTimeoutError,
)
from app.services.database import DatabaseService

# Allowed availability states according to the official contract
ALLOWED_AVAILABILITIES = {
    "available",
    "not_available",
    "blocked",
    "not_applicable",
    "ambiguous",
    "failed",
}


def format_utc_z(dt: datetime) -> str:
    """Format datetime as UTC ISO-8601 ending with Z."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


def read_organisation_inputs(path: str | Path) -> list[str]:
    """
    Parse evaluator-supplied company batch supporting JSON, JSONL, and plain text.
    Validates Norwegian 9-digit format and preserves input order.
    """
    source = Path(path)
    if not source.exists():
        raise FileNotFoundError(f"Input batch file not found: {source}")

    text = source.read_text(encoding="utf-8-sig").strip()
    if not text:
        return []

    raw_items: list[Any] = []
    if source.suffix == ".json":
        data = json.loads(text)
        if isinstance(data, list):
            raw_items = data
        elif isinstance(data, dict):
            raw_items = data.get("organisation_numbers") or data.get("organisations") or []
        else:
            raise ValueError(f"Unexpected JSON structure in {source}")
    elif source.suffix == ".jsonl":
        for line in text.splitlines():
            line_str = line.strip()
            if line_str:
                raw_items.append(json.loads(line_str))
    else:
        # Plain text: one org number per line
        raw_items = [line.strip() for line in text.splitlines() if line.strip()]

    org_list: list[str] = []
    seen = set()
    for item in raw_items:
        if isinstance(item, dict):
            val = (
                item.get("organisation_number")
                or item.get("organisasjonsnummer")
                or item.get("org_nr")
                or item.get("org")
            )
        else:
            val = item

        # Extract only digits
        digits = "".join(ch for ch in str(val or "") if ch.isdigit())
        if len(digits) != 9:
            raise ValueError(f"Invalid Norwegian organisation number: {val!r}")

        org_list.append(digits)

    return org_list


def build_terminal_envelope(
    org_nr: str,
    run_id: str,
    started_at: datetime,
    completed_at: datetime,
    profile: Optional[CompanyProfile] = None,
    evidence: Optional[SourceEvidence] = None,
    changes: Optional[list[Any]] = None,
    error_message: Optional[str] = None,
    requests_count: int = 1,
    runtime_ms: int = 0,
    status_override: Optional[str] = None,
) -> dict[str, Any]:
    """
    Construct exactly one terminal JSON envelope compliant with the official
    Signalpost evaluation contract (OUTPUT_CONTRACT.md).
    """
    # Determine terminal status
    if status_override:
        terminal_status = status_override
    elif profile is not None:
        terminal_status = "completed"
    elif error_message and "not found" in error_message.lower():
        terminal_status = "not_found"
    elif error_message and ("blocked" in error_message.lower() or "rate limit" in error_message.lower()):
        terminal_status = "blocked"
    else:
        terminal_status = "failed"

    claims: list[dict[str, Any]] = []
    evidence_list: list[dict[str, Any]] = []
    changes_list: list[dict[str, Any]] = []
    errors_list: list[str] = [error_message] if error_message else []

    if profile is not None and evidence is not None:
        # Structured claims mapped to explicit availability states and unhallucinated evidence spans
        field_specs = [
            ("legal_name", profile.name, f"navn: {profile.name}, organisasjonsnummer: {profile.org_nr}"),
            ("legal_form", profile.org_form, f"organisasjonsform.kode: {profile.org_form}"),
            ("legal_form_description", profile.org_form_description, f"organisasjonsform.beskrivelse: {profile.org_form_description}"),
            ("registration_date", profile.registration_date, f"registreringsdatoEnhetsregisteret: {profile.registration_date}"),
            ("foundation_date", profile.foundation_date, f"stiftelsesdato: {profile.foundation_date}"),
            ("nace_code", profile.nace_code, f"naeringskode1.kode: {profile.nace_code}"),
            ("nace_description", profile.nace_description, f"naeringskode1.beskrivelse: {profile.nace_description}"),
            ("business_address", profile.business_address.format_single_line() if profile.business_address else None, f"forretningsadresse: {profile.business_address.format_single_line()}" if profile.business_address else None),
            ("postal_address", profile.postal_address.format_single_line() if profile.postal_address else None, f"postadresse: {profile.postal_address.format_single_line()}" if profile.postal_address else None),
            ("municipality", profile.municipality, f"forretningsadresse.kommune: {profile.municipality}"),
            ("municipality_code", profile.municipality_code, f"forretningsadresse.kommunenummer: {profile.municipality_code}"),
            ("official_website", profile.official_website, f"hjemmeside: {profile.official_website}"),
            ("employees", profile.employees, f"antallAnsatte: {profile.employees}"),
            ("registered_in_vat_register", profile.registered_in_vat, f"registrertIMvaregisteret: {str(profile.registered_in_vat).lower()}"),
            ("latest_submitted_accounts", profile.latest_submitted_accounts, f"sisteInnsendteAarsregnskap: {profile.latest_submitted_accounts}"),
            ("institutional_sector_code", profile.institutional_sector_code, f"institusjonellSektorkode.kode: {profile.institutional_sector_code}"),
            ("institutional_sector_description", profile.institutional_sector_description, f"institusjonellSektorkode.beskrivelse: {profile.institutional_sector_description}"),
            ("bankrupt", profile.is_bankrupt, f"konkurs: {str(profile.is_bankrupt).lower()}"),
            ("in_liquidation", profile.is_in_liquidation, f"underAvvikling: {str(profile.is_in_liquidation).lower()}"),
            ("forced_dissolution", profile.is_forced_dissolution, f"underTvangsavviklingEllerTvangsopplosning: {str(profile.is_forced_dissolution).lower()}"),
            ("deleted", profile.is_deleted, f"slettedato: {profile.is_deleted}" if profile.is_deleted is not None else None),
            ("profile_summary", profile.generate_summary(), f"Enhetsregisteret official record for {profile.name} (organisasjonsnummer {profile.org_nr})"),
        ]

        for field_name, value, span in field_specs:
            if value is not None and span is not None:
                ev_id = f"ev-{len(evidence_list) + 1}"
                evidence_list.append({
                    "id": ev_id,
                    "source_url": evidence.source_url,
                    "source_class": "official_registry",
                    "retrieved_at": format_utc_z(evidence.retrieved_at),
                    "content_sha256": evidence.response_hash,
                    "claim_span": span,
                })
                claims.append({
                    "field": field_name,
                    "value": value,
                    "availability": "available",
                    "confidence": 1.0,
                    "evidence_ids": [ev_id],
                })
            else:
                claims.append({
                    "field": field_name,
                    "value": None,
                    "availability": "not_available",
                    "confidence": 0.0,
                    "evidence_ids": [],
                })

    elif terminal_status == "not_found":
        claims.append({
            "field": "entity",
            "value": None,
            "availability": "not_available",
            "confidence": 1.0,
            "evidence_ids": [],
        })
    elif terminal_status == "blocked":
        claims.append({
            "field": "entity",
            "value": None,
            "availability": "blocked",
            "confidence": 0.0,
            "evidence_ids": [],
        })
    else:
        claims.append({
            "field": "entity",
            "value": None,
            "availability": "failed",
            "confidence": 0.0,
            "evidence_ids": [],
        })

    # Track field changes if present (idempotent: identical rerun produces [])
    if changes:
        for c in changes:
            changes_list.append({
                "field": getattr(c, "field_name", str(c)),
                "old_value": getattr(c, "old_value", None),
                "new_value": getattr(c, "new_value", None),
            })

    return {
        "organisation_number": org_nr,
        "run": {
            "run_id": run_id,
            "started_at": format_utc_z(started_at),
            "completed_at": format_utc_z(completed_at),
            "terminal_status": terminal_status,
        },
        "claims": claims,
        "evidence": evidence_list,
        "changes": changes_list,
        "errors": errors_list,
        "operations": {
            "requests": requests_count,
            "runtime_ms": runtime_ms,
            "third_party_cost_usd": 0.0,
        },
    }


class BatchRunner:
    """Manages concurrent execution, rate limiting, persistence, and output generation."""

    def __init__(
        self,
        db_path: Path | str = DATABASE_PATH,
        client: Optional[BrregClient] = None,
        workers: int = 4,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
    ):
        self.db_path = db_path
        self.db = DatabaseService(db_path)
        self.client = client or BrregClient(timeout=timeout)
        self.workers = max(1, min(workers, 8))  # Bounded concurrency

    def process_company(self, org_nr: str, run_id: str) -> dict[str, Any]:
        """Process a single organization number and return its terminal envelope."""
        start_time = time.monotonic()
        started_at = utc_now()
        requests_count = 1
        profile = None
        evidence = None
        changes = None
        error_msg = None
        status_override = None

        try:
            profile, evidence = self.client.fetch_company(org_nr)
            # Transactionally persist and check for historical diffs
            version, changes = self.db.upsert_company_profile(profile, evidence)
        except BrregNotFoundError as e:
            error_msg = str(e)
            status_override = "not_found"
        except (BrregTimeoutError, BrregNetworkError) as e:
            # Safe single retry for transient connection issues
            try:
                time.sleep(0.5)
                requests_count += 1
                profile, evidence = self.client.fetch_company(org_nr)
                version, changes = self.db.upsert_company_profile(profile, evidence)
            except Exception as retry_exc:
                error_msg = f"Network failure: {retry_exc}"
                status_override = "failed"
        except (BrregOrgMismatchError, BrregInvalidOrgNrError, BrregMalformedDataError) as e:
            error_msg = str(e)
            status_override = "failed"
        except BrregHttpError as e:
            error_msg = str(e)
            status_override = "blocked" if e.status_code in (403, 429) else "failed"
        except Exception as e:
            error_msg = f"Unexpected lookup failure: {e}"
            status_override = "failed"

        elapsed_ms = int((time.monotonic() - start_time) * 1000)
        completed_at = utc_now()

        return build_terminal_envelope(
            org_nr=org_nr,
            run_id=run_id,
            started_at=started_at,
            completed_at=completed_at,
            profile=profile,
            evidence=evidence,
            changes=changes,
            error_message=error_msg,
            requests_count=requests_count,
            runtime_ms=elapsed_ms,
            status_override=status_override,
        )

    def run(
        self,
        input_path: Path | str,
        output_path: Path | str,
        report_path: Optional[Path | str] = None,
        run_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Execute batch processing across all input organizations and write
        one JSONL terminal envelope per input.
        """
        overall_start = time.monotonic()
        run_started_at = utc_now()
        run_id = run_id or f"run-{int(run_started_at.timestamp())}"

        orgs = read_organisation_inputs(input_path)
        expected_count = len(orgs)
        unique_orgs = list(dict.fromkeys(orgs))

        results_by_org: dict[str, dict[str, Any]] = {}
        total_requests = 0

        # Execute with bounded concurrency for unique orgs
        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            future_to_org = {
                executor.submit(self.process_company, org, run_id): org
                for org in unique_orgs
            }
            for future in as_completed(future_to_org):
                envelope = future.result()
                org = envelope["organisation_number"]
                results_by_org[org] = envelope
                total_requests += envelope["operations"]["requests"]

        # Preserve exact input order and duplicate rows (1 envelope per input entry)
        ordered_envelopes = [copy.deepcopy(results_by_org[org]) for org in orgs]

        # Verify zero silent drops
        if len(ordered_envelopes) != expected_count:
            raise RuntimeError(
                f"Evaluation Contract Violation: Expected {expected_count} envelopes, got {len(ordered_envelopes)}"
            )

        # Write output JSONL atomically
        out_file = Path(output_path)
        out_file.parent.mkdir(parents=True, exist_ok=True)
        tmp_out = out_file.with_suffix(".tmp")
        with tmp_out.open("w", encoding="utf-8") as f:
            for envelope in ordered_envelopes:
                f.write(json.dumps(envelope, ensure_ascii=False) + "\n")
        tmp_out.replace(out_file)

        run_completed_at = utc_now()
        overall_elapsed = time.monotonic() - overall_start

        # Compute summary metrics
        completed_count = sum(1 for e in ordered_envelopes if e["run"]["terminal_status"] == "completed")
        not_found_count = sum(1 for e in ordered_envelopes if e["run"]["terminal_status"] == "not_found")
        failed_count = sum(1 for e in ordered_envelopes if e["run"]["terminal_status"] in ("failed", "blocked"))

        report = {
            "run_id": run_id,
            "started_at": format_utc_z(run_started_at),
            "completed_at": format_utc_z(run_completed_at),
            "elapsed_seconds": round(overall_elapsed, 2),
            "input_file": str(input_path),
            "output_file": str(output_path),
            "expected_count": expected_count,
            "emitted_envelopes": len(ordered_envelopes),
            "zero_silent_drops": len(ordered_envelopes) == expected_count,
            "completed": completed_count,
            "not_found": not_found_count,
            "failed_or_blocked": failed_count,
            "total_requests": total_requests,
            "third_party_cost_usd": 0.0,
        }

        if report_path:
            rep_file = Path(report_path)
            rep_file.parent.mkdir(parents=True, exist_ok=True)
            rep_file.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

        return report
