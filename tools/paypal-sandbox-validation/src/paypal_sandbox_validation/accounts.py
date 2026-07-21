from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path
from typing import Any

from paypal_sandbox_validation.models import Account, AccountType

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
    "nvp_user",
    "nvp_password",
    "nvp_signature",
]


def detect_delimiter(sample: str) -> str:
    delimiters = [",", "\t", ";"]
    counts = {d: sample.count(d) for d in delimiters}
    max_delim = max(counts.items(), key=lambda item: item[1])[0]
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
    with path.open(encoding=encoding, newline="") as f:
        text = f.read()
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
    if headers != EXPECTED_HEADERS:
        missing = [h for h in EXPECTED_HEADERS if h not in headers]
        extra = [h for h in headers if h not in EXPECTED_HEADERS]
        raise ValueError(
            f"Account CSV headers do not match expected schema. missing={missing}, extra={extra}, got={headers}"
        )

    rows: list[Account] = []
    for idx, row in enumerate(reader, start=2):
        if None in row:
            raise ValueError(f"Malformed CSV row {idx}: too many values for the header.")
        if not any(row.values()):
            continue
        if not row.get("country_code") or not row.get("account_type"):
            raise ValueError(f"Empty or malformed account row {idx}.")
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

    account = Account(
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
        nvp_user=row.get("nvp_user") or None,
        nvp_password=row.get("nvp_password") or None,
        nvp_signature=row.get("nvp_signature") or None,
    )

    _validate_account_credentials(account)
    return account


def _incomplete_nvp(account: Account) -> bool:
    """Return True when any NVP credential is present but the triple is incomplete."""
    fields = [account.nvp_user, account.nvp_password, account.nvp_signature]
    return any(fields) and not all(fields)


def _validate_account_credentials(account: Account) -> None:
    """Validate that credential rules are satisfied for a single account row."""
    if account.is_business():
        if not account.client_id or not account.secret:
            raise ValueError(f"Missing Business REST credentials for {account.country_code}")
        nvp_fields = [account.nvp_user, account.nvp_password, account.nvp_signature]
        if not all(nvp_fields):
            raise ValueError(f"Missing Business NVP credentials for {account.country_code}")
    else:
        if account.client_id or account.secret:
            raise ValueError(f"Business REST credentials on Personal row {account.country_code}")
        if account.nvp_user or account.nvp_password or account.nvp_signature:
            raise ValueError(f"Business NVP credentials on Personal row {account.country_code}")


def _duplicate_signals(
    accounts: list[Account], merchants: list[Account], buyers: list[Account]
) -> tuple[list[str], set[str], set[str], set[str]]:
    merchant_countries = Counter(a.country_code for a in merchants)
    buyer_countries = Counter(a.country_code for a in buyers)
    duplicate_merchants = {c for c, n in merchant_countries.items() if n > 1}
    duplicate_buyers = {c for c, n in buyer_countries.items() if n > 1}
    duplicates = sorted(duplicate_merchants | duplicate_buyers)

    emails = Counter(a.primary_email_alias for a in accounts)
    dup_emails = {e for e, n in emails.items() if n > 1}

    client_ids = [a.client_id for a in merchants if a.client_id]
    dup_client_ids = {cid for cid, n in Counter(client_ids).items() if n > 1}

    nvp_users = [a.nvp_user for a in merchants if a.nvp_user]
    dup_nvp_users = {u for u, n in Counter(nvp_users).items() if n > 1}

    return duplicates, dup_emails, dup_client_ids, dup_nvp_users


def _credential_signals(
    accounts: list[Account], merchants: list[Account], buyers: list[Account]
) -> tuple[list[str], list[str], list[str], list[str], list[str], list[str]]:
    missing_business = [a.country_code for a in merchants if not a.client_id or not a.secret]
    missing_nvp = [a.country_code for a in merchants if not a.nvp_user or not a.nvp_password or not a.nvp_signature]
    incomplete_nvp = [a.country_code for a in merchants if _incomplete_nvp(a)]
    invalid_business = [a.country_code for a in merchants if a.client_id and a.secret and a.client_id == a.secret]
    missing_personal = [a.country_code for a in buyers if not a.password]
    unsupported = [a.country_code for a in accounts if a.country_code not in EXPECTED_COUNTRIES]
    return missing_business, missing_nvp, incomplete_nvp, invalid_business, missing_personal, unsupported


def _aggregate_invalid_merchants(
    merchants: list[Account],
    duplicate_merchants: set[str],
    dup_emails: set[str],
    dup_client_ids: set[str],
    dup_nvp_users: set[str],
    missing_business: list[str],
    missing_nvp: list[str],
    incomplete_nvp: list[str],
    invalid_business: list[str],
) -> set[str]:
    invalid: set[str] = set()
    invalid.update(duplicate_merchants)
    invalid.update(missing_business)
    invalid.update(missing_nvp)
    invalid.update(incomplete_nvp)
    invalid.update(invalid_business)
    for a in merchants:
        if a.client_id in dup_client_ids or a.nvp_user in dup_nvp_users or a.primary_email_alias in dup_emails:
            invalid.add(a.country_code)
    return invalid


def _aggregate_invalid_buyers(
    buyers: list[Account], duplicate_buyers: set[str], dup_emails: set[str], missing_personal: list[str]
) -> set[str]:
    invalid: set[str] = set()
    invalid.update(duplicate_buyers)
    invalid.update(missing_personal)
    for a in buyers:
        if a.primary_email_alias in dup_emails:
            invalid.add(a.country_code)
    return invalid


def validate_accounts(accounts: list[Account], *, require_complete: bool = False) -> dict[str, Any]:
    merchants = [a for a in accounts if a.is_business()]
    buyers = [a for a in accounts if a.is_personal()]

    duplicates, dup_emails, dup_client_ids, dup_nvp_users = _duplicate_signals(accounts, merchants, buyers)
    credential_signals = _credential_signals(accounts, merchants, buyers)
    missing_business, missing_nvp, incomplete_nvp, invalid_business, missing_personal, unsupported = credential_signals

    merchant_countries = Counter(a.country_code for a in merchants)
    buyer_countries = Counter(a.country_code for a in buyers)
    duplicate_merchants = {c for c, n in merchant_countries.items() if n > 1}
    duplicate_buyers = {c for c, n in buyer_countries.items() if n > 1}
    missing_merchants = EXPECTED_COUNTRIES - set(merchant_countries)
    missing_buyers = EXPECTED_COUNTRIES - set(buyer_countries)

    invalid_merchant_countries = _aggregate_invalid_merchants(
        merchants,
        duplicate_merchants,
        dup_emails,
        dup_client_ids,
        dup_nvp_users,
        missing_business,
        missing_nvp,
        incomplete_nvp,
        invalid_business,
    )
    invalid_buyer_countries = _aggregate_invalid_buyers(buyers, duplicate_buyers, dup_emails, missing_personal)

    invalid_account_reasons = [
        duplicates,
        dup_emails,
        dup_client_ids,
        dup_nvp_users,
        invalid_business,
        missing_business,
        missing_nvp,
        incomplete_nvp,
        missing_personal,
        unsupported,
    ]
    if require_complete:
        invalid_account_reasons.extend([missing_merchants, missing_buyers])

    return {
        "merchant_count": len(merchants),
        "buyer_count": len(buyers),
        "merchants_valid": len(merchants) - len(invalid_merchant_countries),
        "buyers_valid": len(buyers) - len(invalid_buyer_countries),
        "invalid_merchant_countries": sorted(invalid_merchant_countries),
        "invalid_buyer_countries": sorted(invalid_buyer_countries),
        "duplicate_accounts": duplicates,
        "duplicate_emails": sorted(dup_emails),
        "duplicate_client_ids": sorted(dup_client_ids),
        "duplicate_nvp_users": sorted(dup_nvp_users),
        "missing_business_credentials": sorted(missing_business),
        "missing_nvp_credentials": sorted(missing_nvp),
        "incomplete_nvp_credentials": sorted(incomplete_nvp),
        "invalid_business_credentials": sorted(invalid_business),
        "missing_personal_credentials": sorted(missing_personal),
        "unsupported_countries": sorted(unsupported),
        "missing_merchants": sorted(missing_merchants),
        "missing_buyers": sorted(missing_buyers),
        "valid": not any(invalid_account_reasons),
    }
