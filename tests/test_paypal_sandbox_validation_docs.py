from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_tool_readme_contains_profile_pricing_section() -> None:
    readme = (REPO_ROOT / "tools" / "paypal-sandbox-validation" / "README.md").read_text()
    assert "## Sandbox profile pricing is not production pricing" in readme
    assert "merchantapps/businesstools/acceptpayments/checkout" in readme
    assert "DE | 1.90% + EUR 0.35" in readme
    assert "AU | 2.40% + AUD 0.30" in readme


def test_root_readme_contains_sandbox_validation_section() -> None:
    readme = (REPO_ROOT / "README.md").read_text()
    assert "### PayPal Sandbox validation" in readme
    assert "sandbox_profile_pricing" in readme
    assert "tools/paypal-sandbox-validation/README.md" in readme
    assert "docs/PAYPAL_SANDBOX_VALIDATION.md" in readme
    assert "paypal-sandbox-validation/" in readme


def test_detailed_documentation_exists_and_has_required_sections() -> None:
    doc = REPO_ROOT / "docs" / "PAYPAL_SANDBOX_VALIDATION.md"
    assert doc.exists()
    text = doc.read_text()
    headings = [
        "## 1. Production public fee source",
        "## 2. Authenticated Sandbox profile pricing",
        "## 3. Transaction evidence",
        "## 4. Qualification decisions",
        "## 5. Why Sandbox results must not rewrite production datasets",
        "## 6. DE findings",
        "## 7. AU findings",
        "## 8. Orders-v2 compliance limitations",
        "## 9. Evidence retention and sanitization",
        "## 10. Reproduction commands",
    ]
    for heading in headings:
        assert heading in text, f"Missing heading: {heading}"
    assert "merchantapps/businesstools/acceptpayments/checkout" in text
    assert "sandbox_profile_pricing" in text
    assert "observed_transaction_pricing" in text
    assert "production_public_pricing" in text


def test_tool_readme_links_to_detailed_documentation() -> None:
    readme = (REPO_ROOT / "tools" / "paypal-sandbox-validation" / "README.md").read_text()
    assert re.search(r"\[.*\]\(\.\./\.\./docs/PAYPAL_SANDBOX_VALIDATION\.md\)", readme)


def test_root_readme_links_to_tool_and_detailed_documentation() -> None:
    readme = (REPO_ROOT / "README.md").read_text()
    assert "tools/paypal-sandbox-validation/README.md" in readme
    assert "docs/PAYPAL_SANDBOX_VALIDATION.md" in readme
