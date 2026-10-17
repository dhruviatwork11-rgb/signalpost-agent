"""SQLite storage service with strict foreign-key enforcement, transactions, versioning, and change tracking."""

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Optional

from app.models import (
    Address,
    ChangeRecord,
    CompanyProfile,
    CompanySnapshot,
    SourceEvidence,
    utc_now,
)


def get_connection(db_path: Path | str) -> sqlite3.Connection:
    """Create a database connection with PRAGMA foreign_keys enabled for every connection."""
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Path | str) -> None:
    """Initialize database tables with foreign keys and index constraints."""
    if isinstance(db_path, str) and db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    conn = get_connection(db_path)
    try:
        with conn:
            conn.executescript(
                """
                -- Current verified state of company profiles
                CREATE TABLE IF NOT EXISTS companies (
                    org_nr TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    org_form TEXT,
                    org_form_description TEXT,
                    registration_date TEXT,
                    business_address TEXT,
                    postal_address TEXT,
                    nace_code TEXT,
                    nace_description TEXT,
                    is_bankrupt INTEGER,       -- NULL=unmentioned, 0=false, 1=true
                    is_in_liquidation INTEGER, -- NULL=unmentioned, 0=false, 1=true
                    is_deleted INTEGER,        -- NULL=unmentioned, 0=false, 1=true
                    is_forced_dissolution INTEGER, -- NULL=unmentioned, 0=false, 1=true
                    official_website TEXT,
                    employees INTEGER,
                    registered_in_vat INTEGER,
                    latest_submitted_accounts TEXT,
                    institutional_sector_code TEXT,
                    institutional_sector_description TEXT,
                    municipality TEXT,
                    municipality_code TEXT,
                    foundation_date TEXT,
                    current_version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                -- Audit log of verified data sources and retrieval timestamps
                CREATE TABLE IF NOT EXISTS source_evidence (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    org_nr TEXT NOT NULL,
                    source_name TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    retrieved_at TEXT NOT NULL,
                    response_hash TEXT NOT NULL,
                    status_code INTEGER DEFAULT 200,
                    FOREIGN KEY (org_nr) REFERENCES companies (org_nr) ON DELETE CASCADE
                );

                -- Complete historical point-in-time snapshots of verified profiles
                CREATE TABLE IF NOT EXISTS company_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    org_nr TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    snapshot_data TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    source_evidence_id INTEGER NOT NULL,
                    FOREIGN KEY (org_nr) REFERENCES companies (org_nr) ON DELETE CASCADE,
                    FOREIGN KEY (source_evidence_id) REFERENCES source_evidence (id) ON DELETE RESTRICT,
                    UNIQUE (org_nr, version)
                );

                -- Fine-grained field-level change history
                CREATE TABLE IF NOT EXISTS change_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    org_nr TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    field_name TEXT NOT NULL,
                    old_value TEXT,
                    new_value TEXT,
                    detected_at TEXT NOT NULL,
                    source_evidence_id INTEGER NOT NULL,
                    FOREIGN KEY (org_nr) REFERENCES companies (org_nr) ON DELETE CASCADE,
                    FOREIGN KEY (source_evidence_id) REFERENCES source_evidence (id) ON DELETE RESTRICT
                );

                CREATE INDEX IF NOT EXISTS idx_source_evidence_org ON source_evidence (org_nr);
                CREATE INDEX IF NOT EXISTS idx_snapshots_org ON company_snapshots (org_nr);
                CREATE INDEX IF NOT EXISTS idx_changes_org ON change_history (org_nr);
                """
            )
            # Automatic schema migration for existing databases
            cursor = conn.cursor()
            existing_cols = {row["name"] for row in cursor.execute("PRAGMA table_info(companies)").fetchall()}
            new_cols = {
                "is_forced_dissolution": "INTEGER",
                "official_website": "TEXT",
                "employees": "INTEGER",
                "registered_in_vat": "INTEGER",
                "latest_submitted_accounts": "TEXT",
                "institutional_sector_code": "TEXT",
                "institutional_sector_description": "TEXT",
                "municipality": "TEXT",
                "municipality_code": "TEXT",
                "foundation_date": "TEXT",
            }
            for col, col_type in new_cols.items():
                if col not in existing_cols:
                    cursor.execute(f"ALTER TABLE companies ADD COLUMN {col} {col_type}")
    finally:
        conn.close()


def _serialize_address(addr: Optional[Address]) -> Optional[str]:
    return json.dumps(addr.model_dump(), sort_keys=True) if addr else None


def _deserialize_address(json_str: Optional[str]) -> Optional[Address]:
    if not json_str:
        return None
    try:
        return Address.model_validate(json.loads(json_str))
    except Exception:
        return None


def _bool_to_sqlite(val: Optional[bool]) -> Optional[int]:
    if val is None:
        return None
    return 1 if val else 0


def _sqlite_to_bool(val: Optional[int]) -> Optional[bool]:
    if val is None:
        return None
    return bool(val)


class DatabaseService:
    """Service handling transactional persistence, versioning, and change tracking."""

    def __init__(self, db_path: Path | str):
        self.db_path = db_path
        init_db(self.db_path)

    def get_connection(self) -> sqlite3.Connection:
        """Obtain a new connection with foreign-key enforcement enabled."""
        return get_connection(self.db_path)

    def upsert_company_profile(
        self, profile: CompanyProfile, evidence: SourceEvidence
    ) -> tuple[int, list[ChangeRecord]]:
        """
        Transactionally record company profile, source evidence, historical snapshot, and change history.
        Guarantees that a failed update will never leave a partial or inconsistent audit trail.
        """
        conn = self.get_connection()
        now_iso = utc_now().isoformat()
        retrieved_at_iso = evidence.retrieved_at.isoformat()

        try:
            with conn:
                cursor = conn.cursor()

                # Fetch current record if it exists
                cursor.execute(
                    "SELECT * FROM companies WHERE org_nr = ?",
                    (profile.org_nr,),
                )
                existing = cursor.fetchone()

                b_addr_json = _serialize_address(profile.business_address)
                p_addr_json = _serialize_address(profile.postal_address)
                bankrupt_val = _bool_to_sqlite(profile.is_bankrupt)
                liquidation_val = _bool_to_sqlite(profile.is_in_liquidation)
                deleted_val = _bool_to_sqlite(profile.is_deleted)
                forced_dissolution_val = _bool_to_sqlite(profile.is_forced_dissolution)
                vat_val = _bool_to_sqlite(profile.registered_in_vat)

                changes: list[ChangeRecord] = []

                if existing is None:
                    # Initial insertion: Version 1
                    target_version = 1
                    cursor.execute(
                        """
                        INSERT INTO companies (
                            org_nr, name, org_form, org_form_description,
                            registration_date, business_address, postal_address,
                            nace_code, nace_description, is_bankrupt,
                            is_in_liquidation, is_deleted, is_forced_dissolution,
                            official_website, employees, registered_in_vat,
                            latest_submitted_accounts, institutional_sector_code,
                            institutional_sector_description, municipality,
                            municipality_code, foundation_date, current_version,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            profile.org_nr,
                            profile.name,
                            profile.org_form,
                            profile.org_form_description,
                            profile.registration_date,
                            b_addr_json,
                            p_addr_json,
                            profile.nace_code,
                            profile.nace_description,
                            bankrupt_val,
                            liquidation_val,
                            deleted_val,
                            forced_dissolution_val,
                            profile.official_website,
                            profile.employees,
                            vat_val,
                            profile.latest_submitted_accounts,
                            profile.institutional_sector_code,
                            profile.institutional_sector_description,
                            profile.municipality,
                            profile.municipality_code,
                            profile.foundation_date,
                            target_version,
                            now_iso,
                            now_iso,
                        ),
                    )

                    # Insert source evidence
                    cursor.execute(
                        """
                        INSERT INTO source_evidence (
                            org_nr, source_name, source_url, retrieved_at,
                            response_hash, status_code
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            profile.org_nr,
                            evidence.source_name,
                            evidence.source_url,
                            retrieved_at_iso,
                            evidence.response_hash,
                            evidence.status_code,
                        ),
                    )
                    evidence_id = cursor.lastrowid

                    # Save full historical snapshot
                    snapshot_dict = profile.model_dump(mode="json")
                    cursor.execute(
                        """
                        INSERT INTO company_snapshots (
                            org_nr, version, snapshot_data, created_at, source_evidence_id
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            profile.org_nr,
                            target_version,
                            json.dumps(snapshot_dict, sort_keys=True),
                            now_iso,
                            evidence_id,
                        ),
                    )

                else:
                    # Company already exists: detect field diffs
                    old_version = existing["current_version"]
                    field_comparisons = [
                        ("name", existing["name"], profile.name),
                        ("org_form", existing["org_form"], profile.org_form),
                        ("org_form_description", existing["org_form_description"], profile.org_form_description),
                        ("registration_date", existing["registration_date"], profile.registration_date),
                        ("business_address", existing["business_address"], b_addr_json),
                        ("postal_address", existing["postal_address"], p_addr_json),
                        ("nace_code", existing["nace_code"], profile.nace_code),
                        ("nace_description", existing["nace_description"], profile.nace_description),
                        ("is_bankrupt", str(existing["is_bankrupt"]) if existing["is_bankrupt"] is not None else None, str(bankrupt_val) if bankrupt_val is not None else None),
                        ("is_in_liquidation", str(existing["is_in_liquidation"]) if existing["is_in_liquidation"] is not None else None, str(liquidation_val) if liquidation_val is not None else None),
                        ("is_deleted", str(existing["is_deleted"]) if existing["is_deleted"] is not None else None, str(deleted_val) if deleted_val is not None else None),
                        ("is_forced_dissolution", str(existing["is_forced_dissolution"]) if existing["is_forced_dissolution"] is not None else None, str(forced_dissolution_val) if forced_dissolution_val is not None else None),
                        ("official_website", existing["official_website"], profile.official_website),
                        ("employees", str(existing["employees"]) if existing["employees"] is not None else None, str(profile.employees) if profile.employees is not None else None),
                        ("registered_in_vat", str(existing["registered_in_vat"]) if existing["registered_in_vat"] is not None else None, str(vat_val) if vat_val is not None else None),
                        ("latest_submitted_accounts", existing["latest_submitted_accounts"], profile.latest_submitted_accounts),
                        ("institutional_sector_code", existing["institutional_sector_code"], profile.institutional_sector_code),
                        ("institutional_sector_description", existing["institutional_sector_description"], profile.institutional_sector_description),
                        ("municipality", existing["municipality"], profile.municipality),
                        ("municipality_code", existing["municipality_code"], profile.municipality_code),
                        ("foundation_date", existing["foundation_date"], profile.foundation_date),
                    ]

                    detected_diffs = []
                    for field_name, old_v, new_v in field_comparisons:
                        if old_v != new_v:
                            detected_diffs.append((field_name, old_v, new_v))

                    # Insert source evidence for audit tracking
                    cursor.execute(
                        """
                        INSERT INTO source_evidence (
                            org_nr, source_name, source_url, retrieved_at,
                            response_hash, status_code
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            profile.org_nr,
                            evidence.source_name,
                            evidence.source_url,
                            retrieved_at_iso,
                            evidence.response_hash,
                            evidence.status_code,
                        ),
                    )
                    evidence_id = cursor.lastrowid

                    if detected_diffs:
                        target_version = old_version + 1

                        # Update companies table
                        cursor.execute(
                            """
                            UPDATE companies SET
                                name = ?,
                                org_form = ?,
                                org_form_description = ?,
                                registration_date = ?,
                                business_address = ?,
                                postal_address = ?,
                                nace_code = ?,
                                nace_description = ?,
                                is_bankrupt = ?,
                                is_in_liquidation = ?,
                                is_deleted = ?,
                                is_forced_dissolution = ?,
                                official_website = ?,
                                employees = ?,
                                registered_in_vat = ?,
                                latest_submitted_accounts = ?,
                                institutional_sector_code = ?,
                                institutional_sector_description = ?,
                                municipality = ?,
                                municipality_code = ?,
                                foundation_date = ?,
                                current_version = ?,
                                updated_at = ?
                            WHERE org_nr = ?
                            """,
                            (
                                profile.name,
                                profile.org_form,
                                profile.org_form_description,
                                profile.registration_date,
                                b_addr_json,
                                p_addr_json,
                                profile.nace_code,
                                profile.nace_description,
                                bankrupt_val,
                                liquidation_val,
                                deleted_val,
                                forced_dissolution_val,
                                profile.official_website,
                                profile.employees,
                                vat_val,
                                profile.latest_submitted_accounts,
                                profile.institutional_sector_code,
                                profile.institutional_sector_description,
                                profile.municipality,
                                profile.municipality_code,
                                profile.foundation_date,
                                target_version,
                                now_iso,
                                profile.org_nr,
                            ),
                        )

                        # Record snapshot of this new version
                        snapshot_dict = profile.model_dump(mode="json")
                        cursor.execute(
                            """
                            INSERT INTO company_snapshots (
                                org_nr, version, snapshot_data, created_at, source_evidence_id
                            ) VALUES (?, ?, ?, ?, ?)
                            """,
                            (
                                profile.org_nr,
                                target_version,
                                json.dumps(snapshot_dict, sort_keys=True),
                                now_iso,
                                evidence_id,
                            ),
                        )

                        # Record each individual field change
                        for field_name, old_v, new_v in detected_diffs:
                            cursor.execute(
                                """
                                INSERT INTO change_history (
                                    org_nr, version, field_name, old_value, new_value,
                                    detected_at, source_evidence_id
                                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    profile.org_nr,
                                    target_version,
                                    field_name,
                                    old_v,
                                    new_v,
                                    now_iso,
                                    evidence_id,
                                ),
                            )
                            changes.append(
                                ChangeRecord(
                                    field_name=field_name,
                                    old_value=old_v,
                                    new_value=new_v,
                                    detected_at=datetime.fromisoformat(now_iso),
                                    version=target_version,
                                )
                            )
                    else:
                        # No field diffs: maintain version, but touch updated_at
                        target_version = old_version
                        cursor.execute(
                            "UPDATE companies SET updated_at = ? WHERE org_nr = ?",
                            (now_iso, profile.org_nr),
                        )

                return target_version, changes
        finally:
            conn.close()

    def get_company_profile(self, org_nr: str) -> Optional[CompanyProfile]:
        """Fetch the current verified company profile."""
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM companies WHERE org_nr = ?", (org_nr,))
            row = cursor.fetchone()
            if not row:
                return None

            return CompanyProfile(
                org_nr=row["org_nr"],
                name=row["name"],
                org_form=row["org_form"],
                org_form_description=row["org_form_description"],
                registration_date=row["registration_date"],
                business_address=_deserialize_address(row["business_address"]),
                postal_address=_deserialize_address(row["postal_address"]),
                nace_code=row["nace_code"],
                nace_description=row["nace_description"],
                is_bankrupt=_sqlite_to_bool(row["is_bankrupt"]),
                is_in_liquidation=_sqlite_to_bool(row["is_in_liquidation"]),
                is_deleted=_sqlite_to_bool(row["is_deleted"]),
                is_forced_dissolution=_sqlite_to_bool(row["is_forced_dissolution"]),
                official_website=row["official_website"],
                employees=row["employees"],
                registered_in_vat=_sqlite_to_bool(row["registered_in_vat"]),
                latest_submitted_accounts=row["latest_submitted_accounts"],
                institutional_sector_code=row["institutional_sector_code"],
                institutional_sector_description=row["institutional_sector_description"],
                municipality=row["municipality"],
                municipality_code=row["municipality_code"],
                foundation_date=row["foundation_date"],
            )
        finally:
            conn.close()

    def get_snapshots(self, org_nr: str) -> list[CompanySnapshot]:
        """Fetch all historical point-in-time snapshots for a company."""
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM company_snapshots WHERE org_nr = ? ORDER BY version ASC",
                (org_nr,),
            )
            rows = cursor.fetchall()
            return [
                CompanySnapshot(
                    org_nr=r["org_nr"],
                    version=r["version"],
                    snapshot_data=json.loads(r["snapshot_data"]),
                    created_at=datetime.fromisoformat(r["created_at"]),
                    source_evidence_id=r["source_evidence_id"],
                )
                for r in rows
            ]
        finally:
            conn.close()

    def get_change_history(self, org_nr: str) -> list[ChangeRecord]:
        """Fetch all field changes for a company across versions."""
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM change_history WHERE org_nr = ? ORDER BY id ASC",
                (org_nr,),
            )
            rows = cursor.fetchall()
            return [
                ChangeRecord(
                    field_name=r["field_name"],
                    old_value=r["old_value"],
                    new_value=r["new_value"],
                    detected_at=datetime.fromisoformat(r["detected_at"]),
                    version=r["version"],
                )
                for r in rows
            ]
        finally:
            conn.close()

    def get_source_evidence(self, org_nr: str) -> list[SourceEvidence]:
        """Fetch all source evidence records for a company."""
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM source_evidence WHERE org_nr = ? ORDER BY id ASC",
                (org_nr,),
            )
            rows = cursor.fetchall()
            return [
                SourceEvidence(
                    source_name=r["source_name"],
                    source_url=r["source_url"],
                    retrieved_at=datetime.fromisoformat(r["retrieved_at"]),
                    response_hash=r["response_hash"],
                    status_code=r["status_code"],
                )
                for r in rows
            ]
        finally:
            conn.close()
