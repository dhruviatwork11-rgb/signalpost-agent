"""Brønnøysund Register Centre API (Enhetsregisteret) client and response normalizer."""

from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any, Optional
import httpx

from app.config import BRREG_API_BASE_URL, REGNSKAP_API_BASE_URL, REQUEST_TIMEOUT_SECONDS, USER_AGENT
from app.models import Address, CompanyProfile, CompanyRole, FinancialAccounts, SourceEvidence, utc_now


class BrregError(Exception):
    """Base exception for Brønnøysund API client operations."""


class BrregInvalidOrgNrError(BrregError):
    """Raised when organization number does not conform to 9 digits."""


class BrregNotFoundError(BrregError):
    """Raised when organization is not found in Enhetsregisteret (HTTP 404)."""


class BrregOrgMismatchError(BrregError):
    """Raised when the organization number returned by the API does not match requested."""


class BrregTimeoutError(BrregError):
    """Raised when the request times out."""


class BrregNetworkError(BrregError):
    """Raised when network connectivity fails."""


class BrregMalformedDataError(BrregError):
    """Raised when response is not valid JSON or expected dictionary structure."""


class BrregHttpError(BrregError):
    """Raised on non-200 / non-404 HTTP errors."""

    def __init__(self, status_code: int, message: str):
        super().__init__(f"HTTP {status_code}: {message}")
        self.status_code = status_code


class BrregClient:
    """Client for querying and normalizing data from the official Norwegian Enhetsregisteret API."""

    SOURCE_NAME = "Brønnøysund Register Centre (Enhetsregisteret)"

    def __init__(
        self,
        base_url: str = BRREG_API_BASE_URL,
        regnskap_base_url: str = REGNSKAP_API_BASE_URL,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
        user_agent: str = USER_AGENT,
        http_client: Optional[httpx.Client] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.regnskap_base_url = regnskap_base_url.rstrip("/")
        self.timeout = timeout
        self.user_agent = user_agent
        self._http_client = http_client

    def build_unit_url(self, org_nr: str) -> str:
        """Construct the official Enhetsregisteret API endpoint URL for an organization."""
        return f"{self.base_url}/enheter/{org_nr}"

    def build_roles_url(self, org_nr: str) -> str:
        """Construct the official Enhetsregisteret roller API endpoint URL for an organization."""
        return f"{self.base_url}/enheter/{org_nr}/roller"

    def build_accounts_url(self, org_nr: str) -> str:
        """Construct the official Regnskapsregisteret API endpoint URL for an organization."""
        return f"{self.regnskap_base_url}/{org_nr}"

    @staticmethod
    def compute_payload_hash(data: dict[str, Any]) -> str:
        """Calculate SHA-256 hash of sorted JSON payload for audit integrity."""
        serialized = json.dumps(data, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def _parse_address(self, addr_dict: Optional[dict[str, Any]]) -> Optional[Address]:
        """Normalize an address dict, preserving absence vs emptiness."""
        if addr_dict is None:
            return None

        # Extract address lines (API provides them as an array or strings)
        raw_lines = addr_dict.get("adresse", [])
        if isinstance(raw_lines, str):
            lines = [raw_lines.strip()] if raw_lines.strip() else []
        elif isinstance(raw_lines, list):
            lines = [str(line).strip() for line in raw_lines if str(line).strip()]
        else:
            lines = []

        return Address(
            address_lines=lines,
            postal_code=addr_dict.get("postnummer"),
            city=addr_dict.get("poststed"),
            country=addr_dict.get("land", "Norge"),
            country_code=addr_dict.get("landkode", "NO"),
        )

    def normalize_response(
        self,
        raw_data: dict[str, Any],
        url: Optional[str] = None,
        retrieved_at: Optional[datetime] = None,
        status_code: int = 200,
    ) -> tuple[CompanyProfile, SourceEvidence]:
        """
        Carefully normalize raw Enhetsregisteret JSON data into a structured CompanyProfile
        and SourceEvidence. Distinguishes missing values from empty values and represents
        status only when explicitly reported by the official registry.
        """
        org_nr = raw_data.get("organisasjonsnummer")
        if not org_nr:
            raise ValueError("Payload missing mandatory 'organisasjonsnummer' field.")

        url = url or self.build_unit_url(str(org_nr))
        retrieved_at = retrieved_at or utc_now()
        payload_hash = self.compute_payload_hash(raw_data)

        # Organization form
        org_form_dict = raw_data.get("organisasjonsform") or {}
        org_form = org_form_dict.get("kode")
        org_form_desc = org_form_dict.get("beskrivelse")

        # NACE industry activity
        nace_dict = raw_data.get("naeringskode1") or {}
        nace_code = nace_dict.get("kode")
        nace_desc = nace_dict.get("beskrivelse")

        # Business and postal addresses
        business_addr = self._parse_address(raw_data.get("forretningsadresse"))
        postal_addr = self._parse_address(raw_data.get("postadresse"))

        # Explicit status indicators (only set if present in the response)
        is_bankrupt = (
            bool(raw_data["konkurs"]) if "konkurs" in raw_data and raw_data["konkurs"] is not None else None
        )
        is_in_liquidation = (
            bool(raw_data["underAvvikling"]) if "underAvvikling" in raw_data and raw_data["underAvvikling"] is not None else None
        )
        is_deleted = (
            bool(raw_data["slettedato"]) if "slettedato" in raw_data and raw_data["slettedato"] is not None else None
        )
        is_forced_dissolution = (
            bool(raw_data["underTvangsavviklingEllerTvangsopplosning"])
            if "underTvangsavviklingEllerTvangsopplosning" in raw_data and raw_data["underTvangsavviklingEllerTvangsopplosning"] is not None
            else None
        )

        # Website and employees
        hjemmeside = raw_data.get("hjemmeside")
        official_website = hjemmeside.strip() if isinstance(hjemmeside, str) and hjemmeside.strip() else None

        antall_ansatte = raw_data.get("antallAnsatte")
        employees = int(antall_ansatte) if antall_ansatte is not None else None

        # VAT registration status
        registered_in_vat = (
            bool(raw_data["registrertIMvaregisteret"])
            if "registrertIMvaregisteret" in raw_data and raw_data["registrertIMvaregisteret"] is not None
            else None
        )

        # Annual accounts year flag
        latest_accounts = raw_data.get("sisteInnsendteAarsregnskap")
        latest_submitted_accounts = str(latest_accounts).strip() if latest_accounts else None

        # Institutional sector
        sector_dict = raw_data.get("institusjonellSektorkode") or {}
        sector_code = sector_dict.get("kode")
        sector_desc = sector_dict.get("beskrivelse")

        # Municipality details from business address (or postal address fallback)
        forr_dict = raw_data.get("forretningsadresse") or {}
        post_dict = raw_data.get("postadresse") or {}
        municipality = forr_dict.get("kommune") or post_dict.get("kommune")
        municipality_code = forr_dict.get("kommunenummer") or post_dict.get("kommunenummer")

        # Foundation date
        foundation_date = raw_data.get("stiftelsesdato")

        profile = CompanyProfile(
            org_nr=str(org_nr),
            name=raw_data.get("navn", ""),
            org_form=org_form,
            org_form_description=org_form_desc,
            registration_date=raw_data.get("registreringsdatoEnhetsregisteret"),
            business_address=business_addr,
            postal_address=postal_addr,
            nace_code=nace_code,
            nace_description=nace_desc,
            is_bankrupt=is_bankrupt,
            is_in_liquidation=is_in_liquidation,
            is_deleted=is_deleted,
            is_forced_dissolution=is_forced_dissolution,
            official_website=official_website,
            employees=employees,
            registered_in_vat=registered_in_vat,
            latest_submitted_accounts=latest_submitted_accounts,
            institutional_sector_code=sector_code,
            institutional_sector_description=sector_desc,
            municipality=municipality,
            municipality_code=municipality_code,
            foundation_date=foundation_date,
            raw_data=raw_data,
        )

        evidence = SourceEvidence(
            source_name=self.SOURCE_NAME,
            source_url=url,
            retrieved_at=retrieved_at,
            response_hash=payload_hash,
            status_code=status_code,
        )

        return profile, evidence

    def fetch_company(self, org_nr: str) -> tuple[CompanyProfile, SourceEvidence]:
        """
        Fetch company profile from the official Brønnøysund API or injected HTTP client.
        Performs strict validation of organization numbers, catches and classifies errors safely,
        and ensures the returned record matches the requested entity.
        """
        cleaned_org_nr = re.sub(r"\s+", "", str(org_nr))
        if not re.fullmatch(r"^\d{9}$", cleaned_org_nr):
            raise BrregInvalidOrgNrError(
                f"Invalid organization number '{org_nr}'. Norwegian organization numbers must be exactly 9 digits."
            )

        url = self.build_unit_url(cleaned_org_nr)
        headers = {
            "Accept": "application/json",
            "User-Agent": self.user_agent,
        }

        # Execute HTTP request with safe exception handling
        try:
            if self._http_client is not None:
                response = self._http_client.get(url, headers=headers)
            else:
                with httpx.Client(timeout=self.timeout) as client:
                    response = client.get(url, headers=headers)
        except httpx.TimeoutException as exc:
            raise BrregTimeoutError(
                f"Request to Brønnøysund Register Centre timed out after {self.timeout}s."
            ) from exc
        except (httpx.ConnectError, httpx.NetworkError) as exc:
            raise BrregNetworkError(
                f"Network error while connecting to Brønnøysund Register Centre: {exc}"
            ) from exc
        except httpx.RequestError as exc:
            raise BrregNetworkError(
                f"Request failed while communicating with registry API: {exc}"
            ) from exc

        # Check HTTP status codes
        if response.status_code == 404:
            raise BrregNotFoundError(
                f"Company with organization number {cleaned_org_nr} was not found in Enhetsregisteret."
            )
        elif response.is_error:
            raise BrregHttpError(
                response.status_code,
                f"Registry returned error status: {response.text[:200]}",
            )

        # Decode JSON safely
        try:
            raw_data = response.json()
        except Exception as exc:
            raise BrregMalformedDataError(
                f"Malformed JSON received from registry for organization {cleaned_org_nr}: {exc}"
            ) from exc

        if not isinstance(raw_data, dict):
            raise BrregMalformedDataError(
                f"Expected JSON object in registry response, got {type(raw_data).__name__}."
            )

        # Validate that returned organization number exactly matches requested number
        returned_org_nr = str(raw_data.get("organisasjonsnummer", ""))
        if returned_org_nr != cleaned_org_nr:
            raise BrregOrgMismatchError(
                f"Organization number mismatch! Requested '{cleaned_org_nr}', but registry returned '{returned_org_nr}'."
            )

        return self.normalize_response(
            raw_data,
            url=url,
            retrieved_at=utc_now(),
            status_code=response.status_code,
        )

    def fetch_roles(self, org_nr: str) -> tuple[list[CompanyRole], Optional[SourceEvidence], str]:
        """
        Fetch verified corporate management and board members from official Brønnøysund Roller API.
        Extracts daglig leder (CEO), styreleder (board chair), and board members.
        Returns (roles_list, evidence, status) where status is:
        - 'completed': Roles found and verified.
        - 'not_available': 404 or company has no registered roles.
        - 'blocked': Rate limited (HTTP 429) or forbidden.
        - 'failed': Network error, timeout, or server error.
        """
        cleaned_org_nr = re.sub(r"\s+", "", str(org_nr))
        if not re.fullmatch(r"^\d{9}$", cleaned_org_nr):
            return [], None, "failed"

        url = self.build_roles_url(cleaned_org_nr)
        headers = {
            "Accept": "application/json",
            "User-Agent": self.user_agent,
        }

        try:
            if self._http_client is not None:
                response = self._http_client.get(url, headers=headers)
            else:
                with httpx.Client(timeout=self.timeout) as client:
                    response = client.get(url, headers=headers)
        except httpx.TimeoutException:
            return [], None, "failed"
        except (httpx.ConnectError, httpx.NetworkError, httpx.RequestError):
            return [], None, "failed"
        except Exception:
            return [], None, "failed"

        if response.status_code == 404:
            return [], None, "not_available"
        elif response.status_code == 429:
            return [], None, "blocked"
        elif response.status_code in (401, 403):
            return [], None, "blocked"
        elif response.is_error:
            return [], None, "failed"

        try:
            raw_data = response.json()
        except Exception:
            return [], None, "failed"

        if not isinstance(raw_data, dict):
            return [], None, "failed"

        # Verify entity link matches requested organization number if present
        enhet_link = raw_data.get("_links", {}).get("enhet", {}).get("href", "")
        if enhet_link and not enhet_link.rstrip("/").endswith(cleaned_org_nr):
            return [], None, "failed"

        roles: list[CompanyRole] = []
        seen_roles: set[tuple[str, str]] = set()
        allowed_governance_codes = {"DAGL", "LEDE", "NEST", "MEDL", "VARA", "INNH"}

        for rgroup in raw_data.get("rollegrupper", []):
            for role in rgroup.get("roller", []):
                # Filter strictly to authorized corporate governance roles (exclude REVI, REGN, etc.)
                type_dict = role.get("type", {})
                role_code = (type_dict.get("kode") or "").upper().strip()
                if role_code not in allowed_governance_codes:
                    continue
                role_title = type_dict.get("beskrivelse", role_code)

                # Ignore deregistered or deceased role holders
                if role.get("avregistrert") is True:
                    continue
                person = role.get("person", {})
                if isinstance(person, dict) and person.get("erDoed") is True:
                    continue

                # Extract person name parts without collecting dates of birth (GDPR data minimization)
                full_name = ""
                if isinstance(person, dict) and person.get("navn"):
                    navn_dict = person["navn"]
                    fornavn = (navn_dict.get("fornavn") or "").strip()
                    mellomnavn = (navn_dict.get("mellomnavn") or "").strip()
                    etternavn = (navn_dict.get("etternavn") or "").strip()
                    full_name = " ".join([p for p in [fornavn, mellomnavn, etternavn] if p]).strip()

                # Fallback: check if role holder is a registered entity (foretak/enhet)
                if not full_name:
                    enhet = role.get("enhet", {})
                    if isinstance(enhet, dict):
                        e_navn = enhet.get("navn")
                        if isinstance(e_navn, list):
                            full_name = " ".join([str(p).strip() for p in e_navn if p]).strip()
                        elif e_navn:
                            full_name = str(e_navn).strip()

                if not full_name:
                    continue

                dedup_key = (role_code, full_name)
                if dedup_key not in seen_roles:
                    seen_roles.add(dedup_key)
                    roles.append(
                        CompanyRole(
                            role_code=role_code,
                            role_title=role_title,
                            name=full_name,
                            is_active=True,
                        )
                    )

        payload_hash = self.compute_payload_hash(raw_data)
        evidence = SourceEvidence(
            source_name="Brønnøysund Register Centre (Roller)",
            source_url=url,
            retrieved_at=utc_now(),
            response_hash=payload_hash,
            status_code=response.status_code,
        )

        if not roles:
            return [], evidence, "not_available"

        return roles, evidence, "completed"

    def fetch_financials(self, org_nr: str) -> tuple[Optional[FinancialAccounts], Optional[SourceEvidence], str]:
        """
        Fetch latest verified annual accounts from official Regnskapsregisteret API.
        Extracts turnover/revenue (driftsinntekter) and annual net result (årsresultat).
        Returns (financials, evidence, status) where status is:
        - 'completed': Verified accounting records found.
        - 'not_available': 404 or entity has no annual accounts in Regnskapsregisteret.
        - 'blocked': Rate limited (HTTP 429) or forbidden.
        - 'failed': Network error, timeout, or server error.
        """
        cleaned_org_nr = re.sub(r"\s+", "", str(org_nr))
        if not re.fullmatch(r"^\d{9}$", cleaned_org_nr):
            return None, None, "failed"

        url = self.build_accounts_url(cleaned_org_nr)
        headers = {
            "Accept": "application/json",
            "User-Agent": self.user_agent,
        }

        try:
            if self._http_client is not None:
                response = self._http_client.get(url, headers=headers)
            else:
                with httpx.Client(timeout=self.timeout) as client:
                    response = client.get(url, headers=headers)
        except httpx.TimeoutException:
            return None, None, "failed"
        except (httpx.ConnectError, httpx.NetworkError, httpx.RequestError):
            return None, None, "failed"
        except Exception:
            return None, None, "failed"

        if response.status_code == 404:
            return None, None, "not_available"
        elif response.status_code == 429:
            return None, None, "blocked"
        elif response.status_code in (401, 403):
            return None, None, "blocked"
        elif response.is_error:
            # Server errors (HTTP 5xx) or other client errors are upstream failures, not missing filings
            return None, None, "failed"

        try:
            raw_data = response.json()
        except Exception:
            return None, None, "failed"

        if not isinstance(raw_data, list):
            return None, None, "failed"

        # Filter strictly for requested organization number
        matching = [
            item for item in raw_data
            if isinstance(item, dict)
            and str(item.get("virksomhet", {}).get("organisasjonsnummer", "")) == cleaned_org_nr
        ]

        if not matching:
            return None, None, "not_available"

        # Sort descending by accounting period end date (tilDato)
        matching.sort(
            key=lambda x: (
                x.get("regnskapsperiode", {}).get("tilDato", ""),
                1 if x.get("regnskapstype") == "KONSERN" else 0,
            ),
            reverse=True,
        )

        chosen = matching[0]
        account_type = str(chosen.get("regnskapstype") or "SELSKAP").strip().upper()
        is_consolidated = (account_type == "KONSERN")
        period = chosen.get("regnskapsperiode") or {}
        til_dato = period.get("tilDato")
        year = til_dato[:4] if til_dato and len(til_dato) >= 4 else str(chosen.get("journalnr", ""))[:4]
        currency = chosen.get("valuta", "NOK")

        res = chosen.get("resultatregnskapResultat") or {}
        drifts = res.get("driftsresultat") or {}
        driftsinntekter = drifts.get("driftsinntekter") or {}
        revenue = driftsinntekter.get("sumDriftsinntekter")
        if revenue is None:
            revenue = driftsinntekter.get("salgsinntekter")

        annual_result = res.get("aarsresultat")
        operating_result = drifts.get("driftsresultat")

        eiendeler = chosen.get("eiendeler") or {}
        total_assets = eiendeler.get("sumEiendeler")

        egenkapital_gjeld = chosen.get("egenkapitalGjeld") or {}
        egenkapital = egenkapital_gjeld.get("egenkapital") or {}
        total_equity = egenkapital.get("sumEgenkapital")

        fin = FinancialAccounts(
            financial_year=str(year) if year else "Unknown",
            period_end=til_dato,
            account_type=account_type,
            is_consolidated=is_consolidated,
            currency=str(currency),
            revenue=float(revenue) if revenue is not None else None,
            annual_result=float(annual_result) if annual_result is not None else None,
            operating_result=float(operating_result) if operating_result is not None else None,
            total_assets=float(total_assets) if total_assets is not None else None,
            total_equity=float(total_equity) if total_equity is not None else None,
        )

        payload_hash = self.compute_payload_hash({"accounts": matching})
        evidence = SourceEvidence(
            source_name="Regnskapsregisteret (Annual Accounts)",
            source_url=url,
            retrieved_at=utc_now(),
            response_hash=payload_hash,
            status_code=response.status_code,
        )

        return fin, evidence, "completed"
