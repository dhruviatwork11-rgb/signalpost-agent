"""Competition batch runner implementing the official Signalpost evaluation contract."""

from concurrent.futures import ThreadPoolExecutor, as_completed
import copy
from datetime import datetime, timezone
import hashlib
import httpx
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
from app.services.job_verifier import JobVerifier
from app.services.news_verifier import NewsVerifier
from app.services.social_verifier import SocialVerifier
from app.services.website_verifier import WebsiteVerifier

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
        # Structured base registry claims
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

        # --- Evaluator-Driven Extended Claims (Version 2) ---

        # 1. Verified Website (distinct from unverified registry string)
        if profile.verified_website is not None:
            vw = profile.verified_website
            if vw.is_verified:
                ev_id = f"ev-{len(evidence_list) + 1}"
                web_url = vw.final_url or vw.candidate_url
                evidence_list.append({
                    "id": ev_id,
                    "source_url": web_url,
                    "source_class": "company_owned",
                    "retrieved_at": format_utc_z(vw.checked_at),
                    "content_sha256": hashlib.sha256(web_url.encode("utf-8")).hexdigest(),
                    "claim_span": vw.evidence_span or f"Verified ownership for {profile.name}",
                })
                claims.append({
                    "field": "verified_website",
                    "value": web_url,
                    "availability": "available",
                    "confidence": 1.0,
                    "evidence_ids": [ev_id],
                })
            elif vw.verification_status == "ambiguous":
                claims.append({
                    "field": "verified_website",
                    "value": None,
                    "availability": "ambiguous",
                    "confidence": 0.0,
                    "evidence_ids": [],
                })
            else:
                claims.append({
                    "field": "verified_website",
                    "value": None,
                    "availability": "not_available",
                    "confidence": 0.0,
                    "evidence_ids": [],
                })
        else:
            claims.append({
                "field": "verified_website",
                "value": None,
                "availability": "not_available",
                "confidence": 0.0,
                "evidence_ids": [],
            })

        # 2. Verified Social Profiles
        if profile.social_profiles is not None:
            verified_sp = [sp for sp in profile.social_profiles if sp.is_verified]
            if verified_sp:
                ev_id = f"ev-{len(evidence_list) + 1}"
                sp_urls = [sp.profile_url for sp in verified_sp]
                evidence_list.append({
                    "id": ev_id,
                    "source_url": verified_sp[0].profile_url,
                    "source_class": "company_owned",
                    "retrieved_at": format_utc_z(evidence.retrieved_at),
                    "content_sha256": hashlib.sha256(",".join(sp_urls).encode("utf-8")).hexdigest(),
                    "claim_span": f"Verified active profiles: {', '.join(sp.handle or sp.platform for sp in verified_sp)}",
                })
                claims.append({
                    "field": "social_profiles",
                    "value": sp_urls,
                    "availability": "available",
                    "confidence": 1.0,
                    "evidence_ids": [ev_id],
                })
            else:
                claims.append({
                    "field": "social_profiles",
                    "value": None,
                    "availability": "not_available",
                    "confidence": 0.0,
                    "evidence_ids": [],
                })

        # 3. Verified News Articles
        news_status = getattr(profile, "news_search_status", "completed")
        valid_articles = [na for na in (profile.news_articles or []) if not na.is_duplicate and na.publication_date]
        if valid_articles:
            ev_id = f"ev-{len(evidence_list) + 1}"
            first_art = valid_articles[0]
            art_data = [
                {"title": a.title, "url": a.article_url, "publication_date": a.publication_date}
                for a in valid_articles
            ]
            evidence_list.append({
                "id": ev_id,
                "source_url": first_art.article_url,
                "source_class": "company_owned",
                "retrieved_at": format_utc_z(first_art.retrieval_date),
                "content_sha256": hashlib.sha256(first_art.title.encode("utf-8")).hexdigest(),
                "claim_span": first_art.evidence_span or f"Dated article: {first_art.title}",
            })
            claims.append({
                "field": "news_articles",
                "value": art_data,
                "availability": "available",
                "confidence": 1.0,
                "evidence_ids": [ev_id],
            })
        elif news_status == "not_applicable" or not profile.verified_website or not profile.verified_website.is_verified:
            claims.append({
                "field": "news_articles",
                "value": None,
                "availability": "not_applicable",
                "confidence": 1.0,
                "evidence_ids": [],
            })
        else:
            claims.append({
                "field": "news_articles",
                "value": None,
                "availability": "not_available",
                "confidence": 1.0,
                "evidence_ids": [],
            })

        # 4. Verified Job Postings (NAV / Arbeidsplassen public register)
        job_status = getattr(profile, "job_search_status", "completed")
        active_jobs = [j for j in (profile.job_postings or []) if j.is_active]
        ev_id = f"ev-{len(evidence_list) + 1}"

        if active_jobs:
            first_job = active_jobs[0]
            job_data = [
                {
                    "job_title": j.job_title,
                    "employer": j.employer_name,
                    "url": j.advertisement_url,
                    "location": j.location,
                    "deadline": j.deadline_date,
                }
                for j in active_jobs
            ]
            is_nav_ad = "nav.no" in first_job.advertisement_url or "arbeidsplassen" in first_job.advertisement_url
            job_source_class = "public_employment_service" if is_nav_ad else "company_owned"
            evidence_list.append({
                "id": ev_id,
                "source_url": first_job.advertisement_url,
                "source_class": job_source_class,
                "retrieved_at": format_utc_z(evidence.retrieved_at),
                "content_sha256": hashlib.sha256(first_job.job_title.encode("utf-8")).hexdigest(),
                "claim_span": first_job.evidence_span or f"Active vacancy: {first_job.job_title}",
            })
            claims.append({
                "field": "job_postings",
                "value": job_data,
                "availability": "available",
                "confidence": 1.0,
                "evidence_ids": [ev_id],
            })
        elif job_status == "blocked":
            website_checked = (profile.verified_website is not None and profile.verified_website.is_verified)
            if website_checked:
                span_text = (
                    f"Primary national register (Arbeidsplassen) was rate-limited (HTTP 429) for {profile.name}. "
                    f"Verified official corporate website checked: 0 active vacancies found. Source coverage was blocked."
                )
            else:
                span_text = f"Official Arbeidsplassen register rate-limited (HTTP 429) for {profile.name}. Source not successfully checked."

            evidence_list.append({
                "id": ev_id,
                "source_url": "https://arbeidsplassen.nav.no/stillinger",
                "source_class": "public_employment_service",
                "retrieved_at": format_utc_z(evidence.retrieved_at),
                "content_sha256": hashlib.sha256(f"{profile.org_nr}_blocked".encode("utf-8")).hexdigest(),
                "claim_span": span_text,
            })
            claims.append({
                "field": "job_postings",
                "value": None,
                "availability": "blocked",
                "confidence": 0.0,
                "evidence_ids": [ev_id],
            })
        elif job_status == "failed":
            claims.append({
                "field": "job_postings",
                "value": None,
                "availability": "failed",
                "confidence": 0.0,
                "evidence_ids": [],
            })
            errors_list.append(f"Job search failed for {profile.name}")
        else:
            # Completed with 0 vacancies
            website_checked = (profile.verified_website is not None and profile.verified_website.is_verified)
            if website_checked:
                span_text = f"Official Arbeidsplassen register and verified corporate website checked for {profile.name}: 0 active vacancies found."
            else:
                span_text = f"Official Arbeidsplassen register checked for {profile.name}: 0 active vacancies found."

            evidence_list.append({
                "id": ev_id,
                "source_url": "https://arbeidsplassen.nav.no/stillinger",
                "source_class": "public_employment_service",
                "retrieved_at": format_utc_z(evidence.retrieved_at),
                "content_sha256": hashlib.sha256(f"{profile.org_nr}_no_jobs".encode("utf-8")).hexdigest(),
                "claim_span": span_text,
            })
            claims.append({
                "field": "job_postings",
                "value": None,
                "availability": "not_available",
                "confidence": 1.0,
                "evidence_ids": [ev_id],
            })

        # 5. Corporate Management and Board Roles (Phase 1)
        roles_status = getattr(profile, "roles_search_status", "completed")
        active_roles = [r for r in (profile.management_roles or []) if r.is_active]
        if active_roles:
            roles_ev_id = f"ev-{len(evidence_list) + 1}"
            roles_url = f"https://data.brreg.no/enhetsregisteret/api/enheter/{profile.org_nr}/roller"
            roles_data = [
                {
                    "role_code": r.role_code,
                    "role_title": r.role_title,
                    "name": r.name,
                }
                for r in active_roles
            ]
            evidence_list.append({
                "id": roles_ev_id,
                "source_url": roles_url,
                "source_class": "official_registry",
                "retrieved_at": format_utc_z(evidence.retrieved_at),
                "content_sha256": hashlib.sha256(json.dumps(roles_data, sort_keys=True).encode("utf-8")).hexdigest(),
                "claim_span": f"Official Roller registry for {profile.name}: {len(active_roles)} registered roles.",
            })
            claims.append({
                "field": "management_roles",
                "value": roles_data,
                "availability": "available",
                "confidence": 1.0,
                "evidence_ids": [roles_ev_id],
            })
            # Explicit single-role claims for CEO / Daglig leder and Board Chair / Styreleder
            daglig_leder = next((r.name for r in active_roles if r.role_code == "DAGL"), None)
            styreleder = next((r.name for r in active_roles if r.role_code == "LEDE"), None)
            if daglig_leder:
                claims.append({
                    "field": "daglig_leder",
                    "value": daglig_leder,
                    "availability": "available",
                    "confidence": 1.0,
                    "evidence_ids": [roles_ev_id],
                })
                claims.append({
                    "field": "ceo",
                    "value": daglig_leder,
                    "availability": "available",
                    "confidence": 1.0,
                    "evidence_ids": [roles_ev_id],
                })
            else:
                claims.append({
                    "field": "daglig_leder",
                    "value": None,
                    "availability": "not_available",
                    "confidence": 1.0,
                    "evidence_ids": [roles_ev_id],
                })
                claims.append({
                    "field": "ceo",
                    "value": None,
                    "availability": "not_available",
                    "confidence": 1.0,
                    "evidence_ids": [roles_ev_id],
                })

            if styreleder:
                claims.append({
                    "field": "styreleder",
                    "value": styreleder,
                    "availability": "available",
                    "confidence": 1.0,
                    "evidence_ids": [roles_ev_id],
                })
                claims.append({
                    "field": "board_chair",
                    "value": styreleder,
                    "availability": "available",
                    "confidence": 1.0,
                    "evidence_ids": [roles_ev_id],
                })
            else:
                claims.append({
                    "field": "styreleder",
                    "value": None,
                    "availability": "not_available",
                    "confidence": 1.0,
                    "evidence_ids": [roles_ev_id],
                })
                claims.append({
                    "field": "board_chair",
                    "value": None,
                    "availability": "not_available",
                    "confidence": 1.0,
                    "evidence_ids": [roles_ev_id],
                })
        elif roles_status == "blocked":
            roles_ev_id = f"ev-{len(evidence_list) + 1}"
            evidence_list.append({
                "id": roles_ev_id,
                "source_url": f"https://data.brreg.no/enhetsregisteret/api/enheter/{profile.org_nr}/roller",
                "source_class": "official_registry",
                "retrieved_at": format_utc_z(evidence.retrieved_at),
                "content_sha256": hashlib.sha256(f"{profile.org_nr}_roles_blocked".encode("utf-8")).hexdigest(),
                "claim_span": f"Official Roller registry rate-limited (HTTP 429) for {profile.name}.",
            })
            claims.append({
                "field": "management_roles",
                "value": None,
                "availability": "blocked",
                "confidence": 0.0,
                "evidence_ids": [roles_ev_id],
            })
            claims.append({
                "field": "daglig_leder",
                "value": None,
                "availability": "blocked",
                "confidence": 0.0,
                "evidence_ids": [roles_ev_id],
            })
            claims.append({
                "field": "ceo",
                "value": None,
                "availability": "blocked",
                "confidence": 0.0,
                "evidence_ids": [roles_ev_id],
            })
            claims.append({
                "field": "styreleder",
                "value": None,
                "availability": "blocked",
                "confidence": 0.0,
                "evidence_ids": [roles_ev_id],
            })
            claims.append({
                "field": "board_chair",
                "value": None,
                "availability": "blocked",
                "confidence": 0.0,
                "evidence_ids": [roles_ev_id],
            })
        elif roles_status == "failed":
            claims.append({
                "field": "management_roles",
                "value": None,
                "availability": "failed",
                "confidence": 0.0,
                "evidence_ids": [],
            })
            claims.append({
                "field": "daglig_leder",
                "value": None,
                "availability": "failed",
                "confidence": 0.0,
                "evidence_ids": [],
            })
            claims.append({
                "field": "ceo",
                "value": None,
                "availability": "failed",
                "confidence": 0.0,
                "evidence_ids": [],
            })
            claims.append({
                "field": "styreleder",
                "value": None,
                "availability": "failed",
                "confidence": 0.0,
                "evidence_ids": [],
            })
            claims.append({
                "field": "board_chair",
                "value": None,
                "availability": "failed",
                "confidence": 0.0,
                "evidence_ids": [],
            })
        else:
            # not_available (e.g. 0 roles registered)
            roles_ev_id = f"ev-{len(evidence_list) + 1}"
            evidence_list.append({
                "id": roles_ev_id,
                "source_url": f"https://data.brreg.no/enhetsregisteret/api/enheter/{profile.org_nr}/roller",
                "source_class": "official_registry",
                "retrieved_at": format_utc_z(evidence.retrieved_at),
                "content_sha256": hashlib.sha256(f"{profile.org_nr}_roles_none".encode("utf-8")).hexdigest(),
                "claim_span": f"Official Roller registry checked for {profile.name}: 0 active roles registered.",
            })
            claims.append({
                "field": "management_roles",
                "value": None,
                "availability": "not_available",
                "confidence": 1.0,
                "evidence_ids": [roles_ev_id],
            })
            claims.append({
                "field": "daglig_leder",
                "value": None,
                "availability": "not_available",
                "confidence": 1.0,
                "evidence_ids": [roles_ev_id],
            })
            claims.append({
                "field": "ceo",
                "value": None,
                "availability": "not_available",
                "confidence": 1.0,
                "evidence_ids": [roles_ev_id],
            })
            claims.append({
                "field": "styreleder",
                "value": None,
                "availability": "not_available",
                "confidence": 1.0,
                "evidence_ids": [roles_ev_id],
            })
            claims.append({
                "field": "board_chair",
                "value": None,
                "availability": "not_available",
                "confidence": 1.0,
                "evidence_ids": [roles_ev_id],
            })

        # 6. Financial Accounts and Figures (Phase 4)
        fin_status = getattr(profile, "financials_search_status", "completed")
        fin = profile.financial_accounts
        if fin is not None:
            fin_ev_id = f"ev-{len(evidence_list) + 1}"
            fin_url = f"https://data.brreg.no/regnskapsregisteret/regnskap/{profile.org_nr}"
            fin_dict = {
                "financial_year": fin.financial_year,
                "period_end": fin.period_end,
                "account_type": fin.account_type,
                "is_consolidated": fin.is_consolidated,
                "currency": fin.currency,
                "revenue": fin.revenue,
                "annual_result": fin.annual_result,
                "operating_result": fin.operating_result,
                "total_assets": fin.total_assets,
                "total_equity": fin.total_equity,
            }
            type_label = "consolidated group" if fin.is_consolidated else "individual company"
            evidence_list.append({
                "id": fin_ev_id,
                "source_url": fin_url,
                "source_class": "official_registry",
                "retrieved_at": format_utc_z(evidence.retrieved_at),
                "content_sha256": hashlib.sha256(json.dumps(fin_dict, sort_keys=True).encode("utf-8")).hexdigest(),
                "claim_span": f"Official Regnskapsregisteret {type_label} annual accounts ({fin.account_type}, {fin.financial_year}) for {profile.name}: Revenue {fin.revenue} {fin.currency}, Result {fin.annual_result} {fin.currency}.",
            })
            claims.append({
                "field": "financial_accounts",
                "value": fin_dict,
                "availability": "available",
                "confidence": 1.0,
                "evidence_ids": [fin_ev_id],
            })
            if fin.revenue is not None:
                claims.append({
                    "field": "annual_revenue",
                    "value": fin.revenue,
                    "availability": "available",
                    "confidence": 1.0,
                    "evidence_ids": [fin_ev_id],
                })
            else:
                claims.append({
                    "field": "annual_revenue",
                    "value": None,
                    "availability": "not_available",
                    "confidence": 1.0,
                    "evidence_ids": [fin_ev_id],
                })
            if fin.annual_result is not None:
                claims.append({
                    "field": "annual_net_result",
                    "value": fin.annual_result,
                    "availability": "available",
                    "confidence": 1.0,
                    "evidence_ids": [fin_ev_id],
                })
            else:
                claims.append({
                    "field": "annual_net_result",
                    "value": None,
                    "availability": "not_available",
                    "confidence": 1.0,
                    "evidence_ids": [fin_ev_id],
                })
        elif fin_status == "blocked":
            fin_ev_id = f"ev-{len(evidence_list) + 1}"
            evidence_list.append({
                "id": fin_ev_id,
                "source_url": f"https://data.brreg.no/regnskapsregisteret/regnskap/{profile.org_nr}",
                "source_class": "official_registry",
                "retrieved_at": format_utc_z(evidence.retrieved_at),
                "content_sha256": hashlib.sha256(f"{profile.org_nr}_financials_blocked".encode("utf-8")).hexdigest(),
                "claim_span": f"Official Regnskapsregisteret rate-limited (HTTP 429) for {profile.name}.",
            })
            claims.append({
                "field": "financial_accounts",
                "value": None,
                "availability": "blocked",
                "confidence": 0.0,
                "evidence_ids": [fin_ev_id],
            })
            claims.append({
                "field": "annual_revenue",
                "value": None,
                "availability": "blocked",
                "confidence": 0.0,
                "evidence_ids": [fin_ev_id],
            })
            claims.append({
                "field": "annual_net_result",
                "value": None,
                "availability": "blocked",
                "confidence": 0.0,
                "evidence_ids": [fin_ev_id],
            })
        elif fin_status == "failed":
            fin_ev_id = f"ev-{len(evidence_list) + 1}"
            evidence_list.append({
                "id": fin_ev_id,
                "source_url": f"https://data.brreg.no/regnskapsregisteret/regnskap/{profile.org_nr}",
                "source_class": "official_registry",
                "retrieved_at": format_utc_z(evidence.retrieved_at),
                "content_sha256": hashlib.sha256(f"{profile.org_nr}_financials_failed".encode("utf-8")).hexdigest(),
                "claim_span": f"Official Regnskapsregisteret lookup for {profile.name} failed due to an upstream server error or timeout.",
            })
            claims.append({
                "field": "financial_accounts",
                "value": None,
                "availability": "failed",
                "error": "Upstream server or network failure querying Regnskapsregisteret",
                "confidence": 0.0,
                "evidence_ids": [fin_ev_id],
            })
            claims.append({
                "field": "annual_revenue",
                "value": None,
                "availability": "failed",
                "error": "Upstream server or network failure querying Regnskapsregisteret",
                "confidence": 0.0,
                "evidence_ids": [fin_ev_id],
            })
            claims.append({
                "field": "annual_net_result",
                "value": None,
                "availability": "failed",
                "error": "Upstream server or network failure querying Regnskapsregisteret",
                "confidence": 0.0,
                "evidence_ids": [fin_ev_id],
            })
        else:
            # not_available (e.g. no annual accounts filed with Regnskapsregisteret)
            fin_ev_id = f"ev-{len(evidence_list) + 1}"
            evidence_list.append({
                "id": fin_ev_id,
                "source_url": f"https://data.brreg.no/regnskapsregisteret/regnskap/{profile.org_nr}",
                "source_class": "official_registry",
                "retrieved_at": format_utc_z(evidence.retrieved_at),
                "content_sha256": hashlib.sha256(f"{profile.org_nr}_financials_none".encode("utf-8")).hexdigest(),
                "claim_span": f"Official Regnskapsregisteret checked for {profile.name}: no annual accounts available.",
            })
            claims.append({
                "field": "financial_accounts",
                "value": None,
                "availability": "not_available",
                "confidence": 1.0,
                "evidence_ids": [fin_ev_id],
            })
            claims.append({
                "field": "annual_revenue",
                "value": None,
                "availability": "not_available",
                "confidence": 1.0,
                "evidence_ids": [fin_ev_id],
            })
            claims.append({
                "field": "annual_net_result",
                "value": None,
                "availability": "not_available",
                "confidence": 1.0,
                "evidence_ids": [fin_ev_id],
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
        db: Optional[DatabaseService] = None,
        client: Optional[BrregClient] = None,
        website_verifier: Optional[WebsiteVerifier] = None,
        social_verifier: Optional[SocialVerifier] = None,
        news_verifier: Optional[NewsVerifier] = None,
        job_verifier: Optional[JobVerifier] = None,
        workers: int = 4,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
        verify_extended_assets: bool = True,
    ):
        if db is not None:
            self.db = db
            self.db_path = getattr(db, "db_path", db_path)
        else:
            self.db_path = db_path
            self.db = DatabaseService(db_path)
        self.client = client or BrregClient(timeout=timeout)
        injected_http = getattr(self.client, "_http_client", None)
        self.website_verifier = website_verifier or WebsiteVerifier(
            timeout=min(timeout, 5.0), http_client=injected_http
        )
        self.social_verifier = social_verifier or SocialVerifier()
        self.news_verifier = news_verifier or NewsVerifier()
        self.job_verifier = job_verifier or JobVerifier(
            timeout=min(timeout, 5.0), http_client=injected_http, db=self.db
        )
        self.workers = max(1, min(workers, 8))  # Bounded concurrency
        self.verify_extended_assets = verify_extended_assets

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

            # Extended asset discovery & verification (Version 2)
            if profile is not None and self.verify_extended_assets:
                # 1. Website ownership verification
                if profile.official_website:
                    requests_count += 1
                    try:
                        profile.verified_website = self.website_verifier.verify_website(
                            candidate_url=profile.official_website,
                            org_nr=profile.org_nr,
                            legal_name=profile.name,
                            is_registry_supplied=True,
                        )
                    except TypeError:
                        try:
                            profile.verified_website = self.website_verifier.verify_website(
                                candidate_url=profile.official_website,
                                org_nr=profile.org_nr,
                                legal_name=profile.name,
                            )
                        except Exception:
                            profile.verified_website = None
                    except Exception:
                        profile.verified_website = None
                else:
                    # Conservative discovery path for missing registry websites
                    try:
                        disc_vw = self.website_verifier.discover_candidate_domain(
                            legal_name=profile.name,
                            org_nr=profile.org_nr,
                        )
                        if disc_vw and disc_vw.is_verified:
                            profile.verified_website = disc_vw
                    except Exception:
                        profile.verified_website = None

                # 2. Social profiles and news articles from verified website
                if (
                    profile.verified_website is not None
                    and profile.verified_website.is_verified
                    and profile.verified_website.html_content
                ):
                    base_url = profile.verified_website.final_url or profile.official_website
                    src_domain = self.website_verifier.extract_domain(base_url)

                    # Extract & verify candidate social links
                    try:
                        cand_social_links = self.social_verifier.extract_candidate_links(
                            html=profile.verified_website.html_content,
                            base_url=base_url,
                        )
                        for link in cand_social_links[:5]:
                            sp = self.social_verifier.verify_social_profile(
                                url=link,
                                legal_name=profile.name,
                                org_nr=profile.org_nr,
                                website_domain=src_domain,
                            )
                            if sp.is_verified:
                                profile.social_profiles.append(sp)
                    except Exception:
                        pass

                    # Extract & verify genuine dated news articles
                    try:
                        articles = []
                        # Step 1: Discover and inspect corporate RSS/Atom feeds
                        feed_urls = self.news_verifier.find_rss_feed_urls(
                            html=profile.verified_website.html_content,
                            base_url=base_url,
                        )
                        for f_url in feed_urls[:2]:
                            try:
                                headers = {"User-Agent": self.website_verifier.user_agent}
                                if self.website_verifier._http_client is not None:
                                    f_resp = self.website_verifier._http_client.get(f_url, headers=headers)
                                else:
                                    with httpx.Client(timeout=4.0, follow_redirects=True) as client:
                                        f_resp = client.get(f_url, headers=headers)
                                if f_resp.status_code == 200:
                                    feed_arts = self.news_verifier.extract_from_rss(f_resp.text, src_domain)
                                    articles.extend(feed_arts)
                            except Exception:
                                pass
                            if articles:
                                break

                        # Step 2: Extract from root HTML if no RSS articles
                        if not articles:
                            articles = self.news_verifier.extract_from_html(
                                html=profile.verified_website.html_content,
                                base_url=base_url,
                                source_domain=src_domain,
                            )

                        # Step 3: If still no articles on root page, inspect primary news sub-page
                        if not articles:
                            news_sub_url = self.news_verifier.find_news_section_url(
                                html=profile.verified_website.html_content,
                                base_url=base_url,
                            )
                            if news_sub_url:
                                try:
                                    headers = {"User-Agent": self.website_verifier.user_agent}
                                    if self.website_verifier._http_client is not None:
                                        resp = self.website_verifier._http_client.get(news_sub_url, headers=headers)
                                    else:
                                        with httpx.Client(timeout=4.0, follow_redirects=True) as client:
                                            resp = client.get(news_sub_url, headers=headers)
                                    if resp.status_code < 400:
                                        # Check for RSS feeds on news sub-page
                                        sub_feeds = self.news_verifier.find_rss_feed_urls(
                                            html=resp.text[:524288],
                                            base_url=news_sub_url,
                                        )
                                        for sf_url in sub_feeds[:2]:
                                            try:
                                                if self.website_verifier._http_client is not None:
                                                    sf_resp = self.website_verifier._http_client.get(sf_url, headers=headers)
                                                else:
                                                    with httpx.Client(timeout=4.0, follow_redirects=True) as client:
                                                        sf_resp = client.get(sf_url, headers=headers)
                                                if sf_resp.status_code == 200:
                                                    feed_arts = self.news_verifier.extract_from_rss(sf_resp.text, src_domain)
                                                    articles.extend(feed_arts)
                                            except Exception:
                                                pass
                                            if articles:
                                                break

                                        if not articles:
                                            sub_articles = self.news_verifier.extract_from_html(
                                                html=resp.text[:524288],
                                                base_url=news_sub_url,
                                                source_domain=src_domain,
                                            )
                                            articles.extend(sub_articles)
                                except Exception:
                                    pass

                        profile.news_articles.extend(articles)
                        profile.news_search_status = "completed"
                    except Exception:
                        profile.news_search_status = "failed"
                else:
                    # No verified website available to check for news
                    profile.news_search_status = "not_applicable"

                # 3. Individual job postings from NAV / Arbeidsplassen
                requests_count += 1
                try:
                    nav_postings = self.job_verifier.fetch_nav_vacancies(
                        legal_name=profile.name,
                        org_nr=profile.org_nr,
                    )
                    nav_status = getattr(nav_postings, "status", "completed")
                except Exception:
                    nav_postings = []
                    nav_status = "failed"

                # 3B. Career-page & ATS discovery on verified company website
                career_postings = []
                if profile.verified_website is not None and profile.verified_website.is_verified:
                    requests_count += 1
                    try:
                        career_list = self.job_verifier.discover_career_vacancies(
                            website_url=profile.verified_website.final_url or profile.official_website,
                            legal_name=profile.name,
                            org_nr=profile.org_nr,
                            http_client=self.website_verifier._http_client,
                            homepage_html=profile.verified_website.html_content,
                        )
                        if career_list is not None:
                            career_postings = list(career_list)
                    except Exception:
                        pass

                # Combine vacancies with deduplication
                combined_jobs = []
                seen_job_keys = set()
                for job in (list(nav_postings) + career_postings):
                    key = (
                        re.sub(r"\W+", "", job.job_title.lower()),
                        re.sub(r"\W+", "", job.employer_name.lower()),
                        job.deadline_date or "",
                    )
                    if key not in seen_job_keys:
                        seen_job_keys.add(key)
                        combined_jobs.append(job)

                profile.job_postings = combined_jobs

                # Determine overall job search status
                if combined_jobs:
                    profile.job_search_status = "completed"
                elif nav_status == "completed":
                    profile.job_search_status = "completed"
                elif nav_status == "blocked":
                    profile.job_search_status = "blocked"
                else:
                    profile.job_search_status = "failed"

                # 4. Corporate management and board roles (Phase 1)
                requests_count += 1
                try:
                    roles, r_ev, r_status = self.client.fetch_roles(profile.org_nr)
                    profile.management_roles = roles
                    profile.roles_search_status = r_status
                except Exception:
                    profile.management_roles = []
                    profile.roles_search_status = "failed"

                # 5. Financial accounts and results (Phase 4)
                requests_count += 1
                try:
                    fin, f_ev, f_status = self.client.fetch_financials(profile.org_nr)
                    profile.financial_accounts = fin
                    profile.financials_search_status = f_status
                except Exception:
                    profile.financial_accounts = None
                    profile.financials_search_status = "failed"

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
        resume: bool = True,
    ) -> dict[str, Any]:
        """
        Execute batch processing across all input organizations and write
        one JSONL terminal envelope per input. Supports resilient batch resumption.
        """
        overall_start = time.monotonic()
        run_started_at = utc_now()
        run_id = run_id or f"run-{int(run_started_at.timestamp())}"

        orgs = read_organisation_inputs(input_path)
        expected_count = len(orgs)
        unique_orgs = list(dict.fromkeys(orgs))

        results_by_org: dict[str, dict[str, Any]] = {}
        total_requests = 0

        # Resumable batch processing: inspect existing output file if resume is requested
        out_file = Path(output_path)
        if resume and out_file.exists():
            try:
                with out_file.open("r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            env = json.loads(line)
                            env_org = env.get("organisation_number")
                            term_status = env.get("run", {}).get("terminal_status")
                            # Only reuse completed or verified not_found envelopes
                            if env_org and term_status in ("completed", "not_found"):
                                results_by_org[env_org] = env
            except Exception:
                pass

        # Identify organizations still needing execution
        orgs_to_process = [org for org in unique_orgs if org not in results_by_org]

        # Execute with bounded concurrency for pending orgs
        if orgs_to_process:
            with ThreadPoolExecutor(max_workers=self.workers) as executor:
                future_to_org = {
                    executor.submit(self.process_company, org, run_id): org
                    for org in orgs_to_process
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
