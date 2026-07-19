from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path
from typing import Any

from paypal_sandbox_validation.models import Account, AccountType
from paypal_sandbox_validation.redaction import mask_client_id, mask_email

EXPECTED_COUNTRIES = {
    "DE",
    "ES",
    "GB",
    "US",
    "JP",
    "CA",
    "AU",
    "CH",
    "BR",
    "HK",
    "CZ",
    "IL",
    "ZA",
}

COUNTRY_CURRENCY = {
    "DE": "EUR",
    "ES": "EUR",
    "GB": "GBP",
    "US": "USD",
    "JP": "JPY",
    "CA": "CAD",
    "AU": "AUD",
    "CH": "CHF",
    "BR": "BRL",
    "HK": "HKD",
    "CZ": "CZK",
    "IL": "ILS",
    "ZA": "ZAR",
}

EXPECTED_HEADERS = [
    "country_code",
    "account_type",
    "primary_email_alias",
    "password",
    "first_name",
    "last_name",
    "ppBalance",
    "addBank",
    "ccType",
    "payment_card",
    "client_id",
    "secret",
]


def detect_delimiter(sample: str) -> str:
    delimiters = [",", "\t", ";"]
    counts = {d: sample.count(d) for d in delimiters}
    max_delim = max(counts, key=counts.get)
    if counts[max_delim] == 0:
        raise ValueError("Could not detect CSV delimiter; the file appears to be empty.")
    # Reject ambiguous parsing: two delimiters have the same non-zero count.
    non_zero = {k: v for k, v in counts.items() if v > 0}
    if len(non_zero) > 1:
        values = sorted(non_zero.items(), key=lambda x: x[1], reverse=True)
        if len(values) > 1 and values[0][1] == values[1][1]:
            raise ValueError(f"Ambiguous delimiter detection: {values}")
    return max_delim


def _open_csv(path: Path) -> tuple[str, Any]:
    raw = path.read_bytes()
    encoding = "utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else "utf-8"
    text = path.read_text(encoding=encoding, newline="")
    if not text.strip():
        raise ValueError("Account CSV is empty.")
    delimiter = detect_delimiter(text[:2048])
    return delimiter, text


def parse_accounts_csv(csv_path: str | Path) -> list[Account]:
    path = Path(csv_path)
    if not path.is_file():
        raise FileNotFoundError(f"Account CSV not found: {path}")

    delimiter, text = _open_csv(path)
    reader = csv.DictReader(text.splitlines(), delimiter=delimiter)
    if reader.fieldnames is None:
        raise ValueError("Account CSV has no headers.")

    headers = [h.strip() for h in reader.fieldnames]
    missing = [h for h in EXPECTED_HEADERS if h not in headers]
    if missing:
        raise ValueError(f"Account CSV missing required headers: {missing}")
    extra = [h for h in headers if h not in EXPECTED_HEADERS]
    if extra:
        raise ValueError(f"Account CSV contains unexpected headers: {extra}")

    rows: list[Account] = []
    for row in reader:
        if not any(row.values()):
            continue
        if not row.get("country_code") or not row.get("account_type"):
            raise ValueError("Empty or malformed account row.")
        rows.append(_row_to_account(row))
    return rows


def _row_to_account(row: dict[str, str]) -> Account:
    country = row["country_code"].strip().upper()
    account_type = row["account_type"].strip().upper()
    if account_type not in {"BUSINESS", "PERSONAL"}:
        raise ValueError(f"Invalid account_type for {country}: {account_type!r}")
    if country not in EXPECTED_COUNTRIES:
        raise ValueError(f"Unsupported country_code: {country}")
    if not row.get("primary_email_alias"):
        raise ValueError(f"Missing primary_email_alias for {country} {account_type}")
    if not row.get("password"):
        raise ValueError(f"Missing password for {country} {account_type}")
    if account_type == "BUSINESS" and (not row.get("client_id") or not row.get("secret")):
        raise ValueError(f"Missing Business REST credentials for {country}")
    if account_type == "PERSONAL" and not row.get("password"):
        raise ValueError(f"Missing Personal login credentials for {country}")

    return Account(
        country_code=country,
        account_type=AccountType(account_type),
        primary_email_alias=row["primary_email_alias"].strip(),
        password=row["password"],
        first_name=row.get("first_name", "").strip(),
        last_name=row.get("last_name", "").strip(),
        pp_balance=row.get("ppBalance", "").strip(),
        add_bank=row.get("addBank", "").strip(),
        cc_type=row.get("ccType", "").strip(),
        payment_card=row.get("payment_card", "").strip(),
        client_id=row.get("client_id") or None,
        secret=row.get("secret") or None,
    )


def validate_accounts(accounts: list[Account]) -> dict[str, Any]:
    merchants = [a for a in accounts if a.is_business()]
    buyers = [a for a in accounts if a.is_personal()]

    merchant_countries = Counter(a.country_code for a in merchants)
    buyer_countries = Counter(a.country_code for a in buyers)
    duplicate_merchants = {c for c, n in merchant_countries.items() if n > 1}
    duplicate_buyers = {c for c, n in buyer_countries.items() if n > 1}
    duplicates = sorted(duplicate_merchants | duplicate_buyers)

    emails = Counter(a.primary_email_alias for a in accounts)
    dup_emails = {e for e, n in emails.items() if n > 1}

    client_ids = [a.client_id for a in merchants if a.client_id]
    dup_client_ids = {cid for cid, n in Counter(client_ids).items() if n > 1}

    missing_business = [a.country_code for a in merchants if not a.client_id or not a.secret]
    invalid_business = [a.country_code for a in merchants if a.client_id and a.secret and a.client_id == a.secret]
    missing_personal = [a.country_code for a in buyers if not a.password]
    unsupported = [a.country_code for a in accounts if a.country_code not in EXPECTED_COUNTRIES]

    merchant_set = set(merchant_countries)
    buyer_set = set(buyer_countries)
    missing_merchants = EXPECTED_COUNTRIES - merchant_set
    missing_buyers = EXPECTED_COUNTRIES - buyer_set

    return {
        "merchant_count": len(merchants),
        "buyer_count": len(buyers),
        "duplicate_accounts": duplicates,
        "duplicate_emails": sorted(dup_emails),
        "duplicate_client_ids": sorted(dup_client_ids),
        "missing_business_credentials": sorted(missing_business),
        "invalid_business_credentials": sorted(invalid_business),
        "missing_personal_credentials": sorted(missing_personal),
        "unsupported_countries": sorted(unsupported),
        "missing_merchants": sorted(missing_merchants),
        "missing_buyers": sorted(missing_buyers),
        "valid": not any(
            [
                duplicates,
                dup_emails,
                dup_client_ids,
                invalid_business,
                missing_business,
                missing_personal,
                unsupported,
                missing_merchants,
                missing_buyers,
            ]
        ),
    }


def summarize_accounts(accounts: list[Account]) -> dict[str, Any]:
    merchants = [a for a in accounts if a.is_business()]
    buyers = [a for a in accounts if a.is_personal()]
    return {
        "merchant_count": len(merchants),
        "buyer_count": len(buyers),
        "rest_credential_pairs": sum(1 for a in merchants if a.client_id and a.secret),
        "sample_merchants": [mask_client_id(a.client_id) if a.client_id else None for a in merchants[:3]],
        "sample_buyers": [mask_email(a.primary_email_alias) for a in buyers[:3]],
    }
