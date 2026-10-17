"""Pydantic data models for company profiles, source evidence, and change tracking."""

from datetime import datetime, timezone
import re
from typing import Any, Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator


def utc_now() -> datetime:
    """Return current timestamp in UTC timezone."""
    return datetime.now(timezone.utc)


class Address(BaseModel):
    """Structured postal or business address."""

    model_config = ConfigDict(frozen=True)

    address_lines: list[str] = Field(default_factory=list)
    postal_code: Optional[str] = None
    city: Optional[str] = None
    country: Optional[str] = "Norway"
    country_code: Optional[str] = "NO"

    def format_single_line(self) -> str:
        """Return a readable single-line representation of the address."""
        parts = list(self.address_lines)
        if self.postal_code or self.city:
            postal_city = " ".join(filter(None, [self.postal_code, self.city]))
            parts.append(postal_city)
        if self.country:
            parts.append(self.country)
        return ", ".join(parts)


class SourceEvidence(BaseModel):
    """Metadata documenting where and when data was retrieved."""

    model_config = ConfigDict(frozen=True)

    source_name: str
    source_url: str
    retrieved_at: datetime = Field(default_factory=utc_now)
    response_hash: str
    status_code: Optional[int] = 200

    @field_validator("retrieved_at")
    @classmethod
    def ensure_utc(cls, v: datetime) -> datetime:
        """Ensure timestamps are stored in UTC."""
        if v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v.astimezone(timezone.utc)


class CompanyProfile(BaseModel):
    """Structured company profile sourced from official public registries."""

    model_config = ConfigDict(populate_by_name=True)

    org_nr: str = Field(..., description="Norwegian 9-digit organization number")
    name: str = Field(..., description="Official registered company name")
    org_form: Optional[str] = Field(default=None, description="Legal form code (e.g., AS, ENK)")
    org_form_description: Optional[str] = Field(
        default=None, description="Legal form description (e.g., Aksjeselskap)"
    )
    registration_date: Optional[str] = Field(
        default=None, description="Registration date in Enhetsregisteret (YYYY-MM-DD)"
    )
    business_address: Optional[Address] = None
    postal_address: Optional[Address] = None
    nace_code: Optional[str] = Field(default=None, description="Primary NACE industry code")
    nace_description: Optional[str] = Field(
        default=None, description="Primary NACE industry description"
    )

    # Registration status: represented ONLY when supported by source data.
    # None indicates unknown / not specified in source; we do NOT infer inactivity.
    is_bankrupt: Optional[bool] = Field(
        default=None,
        description="True/False if explicitly stated in registry, None if unmentioned.",
    )
    is_in_liquidation: Optional[bool] = Field(
        default=None,
        description="True/False if liquidation status is explicitly stated, None if unmentioned.",
    )
    is_deleted: Optional[bool] = Field(
        default=None,
        description="True if officially deregistered/deleted, None if unmentioned.",
    )

    # Additional official registry fields
    official_website: Optional[str] = Field(
        default=None, description="Official company website URL registered in the registry"
    )
    employees: Optional[int] = Field(
        default=None, description="Number of registered employees (antallAnsatte)"
    )
    registered_in_vat: Optional[bool] = Field(
        default=None, description="True if registered in the VAT register (Merverdiavgiftsregisteret)"
    )
    latest_submitted_accounts: Optional[str] = Field(
        default=None, description="Year of latest submitted annual accounts (sisteInnsendteAarsregnskap)"
    )
    institutional_sector_code: Optional[str] = Field(
        default=None, description="Institutional sector code (e.g. 2100)"
    )
    institutional_sector_description: Optional[str] = Field(
        default=None, description="Institutional sector description"
    )
    municipality: Optional[str] = Field(
        default=None, description="Municipality name (kommune)"
    )
    municipality_code: Optional[str] = Field(
        default=None, description="Municipality code (kommunenummer)"
    )
    is_forced_dissolution: Optional[bool] = Field(
        default=None,
        description="True/False if under forced dissolution or liquidation, None if unmentioned.",
    )
    foundation_date: Optional[str] = Field(
        default=None, description="Foundation date (stiftelsesdato, YYYY-MM-DD)"
    )

    raw_data: Optional[dict[str, Any]] = Field(
        default=None, exclude=True, description="Unmodified response dictionary for auditing"
    )

    def generate_summary(self) -> str:
        """
        Deterministically synthesize verified company profile facts into a concise executive summary.
        Does not fabricate facts or speculate beyond official public registry evidence.
        """
        status_terms = []
        if self.is_deleted is True:
            status_terms.append("officially deleted/deregistered")
        if self.is_bankrupt is True:
            status_terms.append("in bankruptcy proceedings")
        if self.is_in_liquidation is True:
            status_terms.append("in liquidation")
        if self.is_forced_dissolution is True:
            status_terms.append("under forced dissolution")

        status_phrase = ", ".join(status_terms) if status_terms else "active and in normal standing"
        form = self.org_form_description or self.org_form or "entity"
        parts = [f"{self.name} (org nr: {self.org_nr}) is a registered Norwegian {form}"]

        if self.municipality:
            parts.append(f"based in {self.municipality}")

        parts.append(f"with status: {status_phrase}.")

        details = []
        if self.registration_date:
            details.append(f"registered on {self.registration_date}")
        if self.nace_description:
            details.append(f"industry: {self.nace_description} (NACE {self.nace_code})")
        if self.employees is not None:
            details.append(f"{self.employees} registered employee{'s' if self.employees != 1 else ''}")
        if self.latest_submitted_accounts:
            details.append(f"latest submitted annual accounts for {self.latest_submitted_accounts}")
        if self.registered_in_vat is True:
            details.append("registered in the VAT register")
        elif self.registered_in_vat is False:
            details.append("not registered in the VAT register")
        if self.official_website:
            details.append(f"website: {self.official_website}")

        if details:
            parts.append("Key verified details: " + "; ".join(details) + ".")

        return " ".join(parts)

    @field_validator("org_nr")
    @classmethod
    def validate_norwegian_org_nr(cls, v: str) -> str:
        """Validate that organization number is exactly 9 digits."""
        cleaned = re.sub(r"\s+", "", str(v))
        if not re.fullmatch(r"^\d{9}$", cleaned):
            raise ValueError(
                f"Invalid Norwegian organization number: '{v}'. Must be exactly 9 digits."
            )
        return cleaned


class ChangeRecord(BaseModel):
    """Field-level audit difference between company profile versions."""

    field_name: str
    old_value: Optional[str]
    new_value: Optional[str]
    detected_at: datetime = Field(default_factory=utc_now)
    version: int


class CompanySnapshot(BaseModel):
    """Historical point-in-time snapshot of a company profile."""

    org_nr: str
    version: int
    snapshot_data: dict[str, Any]
    created_at: datetime = Field(default_factory=utc_now)
    source_evidence_id: Optional[int] = None
