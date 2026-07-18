# Conformance

The cross-language conformance suite lives in `contracts/conformance/`.

## Case format

Each case is a JSON file:

```json
{
  "id": "stripe-de-online-domestic-card",
  "provider_documents": {},
  "request": {},
  "expected_result": {},
  "expected_error": null
}
```

* `provider_documents` — provider data needed for the case, or references to fixture paths.
* `request` — canonical `/v1` quote request.
* `expected_result` — normalized quote response.
* `expected_error` — canonical error envelope, or `null`.

## Running the suite

```bash
make test-conformance
```

This runs every case through the Python, PHP, and TypeScript implementations, normalizes outputs, and fails on any difference.

## Normalization

Only representation differences not part of the contract are normalized (e.g. map key ordering). The following must match exactly:

* status
* monetary strings
* components and component order
* matched rule IDs
* assumptions and warnings
* provenance
* structured error code and details

## Adding a case

When fixing a calculation bug, add or update a shared conformance case first, then update the specification if necessary, then fix Python, PHP, and TypeScript in that order.
