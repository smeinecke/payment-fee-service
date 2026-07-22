# Refactoring & Optimization Plan

Scope: the whole `payment-fee-service` polyglot workspace —
`packages/payment-fee` (Python core, canonical), `packages/payment-fee-php`,
`packages/payment-fee-typescript`, `services/payment-fee-service` (FastAPI wrapper),
`tools/paypal-sandbox-validation` (10,232-line standalone CLI), `tools/conformance`
(cross-language differential runner), and `contracts/` (shared schemas/fixtures).

Goal: fix a confirmed cross-language correctness bug, reduce complexity, remove dead
code, and optimize the request hot path — **without removing any currently used
feature**. All fixes must keep `make test-conformance` (the release-gate job) green
and preserve the public HTTP/CLI contracts of all three language ports.

---

## Current state (measured)

| Metric | Value |
|---|---|
| Python core (`packages/payment-fee`) | 4,184 lines; worst: `StripeProvider.compile_rules` (F), `PayPalProvider.compile_rules` (F) |
| PHP port (`packages/payment-fee-php`) | 2,139 lines |
| TypeScript port (`packages/payment-fee-typescript`) | 1,671 lines |
| Service (`services/payment-fee-service`) | 681 lines — clean, no hotspots |
| Sandbox tool (`tools/paypal-sandbox-validation`) | 10,232 lines — **bigger than the core library**; `cli.py` alone is 2,758 lines with 22 commands |
| Radon average (Python, all 3 components) | B (5.05) — good average, but 20 F/E/D-rated functions concentrated in the two providers and the sandbox tool |
| Conformance fixtures | `contracts/conformance/cases/*.json`, single source of truth read by all 3 language runners — confirmed no fixture drift |

---

## Phase 0 — Fix a confirmed cross-language correctness bug

- [x] **TypeScript's Stripe condition matcher checks 5 dimensions; Python and PHP check
      32 (+ transaction-amount range).** Verified directly:
      `packages/payment-fee-typescript/src/providers/stripe/provider.ts:267-293`
      (`normalizeConditions`) only promotes `account_country`, `payment_method`,
      `product_id`, `variant_id`, and a **hardcoded `null`** for
      `payment_method_variant`. `packages/payment-fee/src/payment_fee/providers/stripe/provider.py:126-160`
      and `packages/payment-fee-php/src/Providers/Stripe/StripeProvider.php:239-277`
      both promote the full 32-dimension list (`card_origin`, `card_region`,
      `settlement_timing`, `dispute_state`, `pricing_plan`, `fee_type`,
      `transaction_type`, …) plus `transaction_amount_min`/`transaction_amount_max`
      range conditions, which TS has **no handling for at all**.
      **Impact**: any Stripe rule that constrains on one of the missing 27 dimensions
      (or an amount threshold) as a top-level rule field will match unconditionally in
      TS while PHP/Python correctly gate it — the same transaction can get a different
      quote depending on which language port answers the request. Not caught by the 35
      existing conformance cases, meaning no fixture currently exercises those fields.
      **Fix**: port the full dimension list + amount-range handling from
      `provider.py:126-160`/`StripeProvider.php:239-277` into `provider.ts`; extend
      `apiFieldName` (provider.ts:570-606) with the two missing entries
      (`fee_type`, `transaction_type`) already present in PHP
      (StripeProvider.php:603-643); add a conformance case that pins a rule using one
      of the previously-ignored dimensions so a future regression is caught by CI.
- [x] **Error-contract divergence for "market not found."** Python raises
      `UnknownMarket`/`UNKNOWN_MARKET` (`providers/stripe/provider.py:598`,
      `providers/paypal/provider.py:633`). PHP (`StripeProvider.php:156-164`
      `findMarket`, `PayPalProvider.php:211-219` `findCountry`) and TypeScript
      (`provider.ts:92`) both throw `QuoteNotAvailable`/`QUOTE_NOT_AVAILABLE` instead —
      and both ports define an unused `UnknownMarket` exception class that's never
      thrown (see Phase 1). **Decision needed**: pick the canonical error code (Python's
      `UnknownMarket` is more specific and arguably correct) and align PHP/TS, or
      formally adopt `QuoteNotAvailable` everywhere and delete the dead classes. Either
      way, add an `unknown-market.json` conformance case to lock in the decision — none
      exists today.

Gate: `make test-conformance` (existing 35 cases) + the 2 new cases above, run for all
three languages via `tools/conformance/run_differential.py`.

---

## Phase 1 — Delete confirmed-dead code (no behavior change)

- [x] `tools/paypal-sandbox-validation/src/paypal_sandbox_validation/accounts.py:259`
      `summarize_accounts` — zero references anywhere in the repo. **Decision needed**:
      looks like an intended-but-never-wired diagnostic helper (e.g. for
      `validate-config` output) — confirm before deleting vs. wiring it in.
- [x] `tools/paypal-sandbox-validation/src/paypal_sandbox_validation/diagnostics.py:796`
      `render_markdown = _render_markdown` — unused module-level alias; only the
      private `_render_markdown` is actually called (`diagnostics.py:611`).
- [x] `packages/payment-fee/src/payment_fee/registry.py:41-43`
      `ProviderRegistry.ready` — never referenced in `packages/`, `services/`,
      `tools/`, or `tests/`. Either remove or wire into the health-check endpoint
      (which currently re-derives readiness from `data_status()` independently).
- [x] **PHP**: `Exception/DatasetValidationException.php`, `Exception/ProviderDataUnavailable.php`
      never thrown anywhere in the PHP package.
- [x] **TypeScript**: `errors.ts` `ProviderDataUnavailable` (L31),
      `DatasetValidationException` (L83) never thrown.
      (`UnknownMarket` in both PHP and TS is covered by the Phase 0 decision above —
      don't delete it independently of that decision.)

**Decision needed (dead-but-maybe-incomplete feature):**

- `providers/stripe/provider.py`: the full `StripePaymentMethods` dataset is loaded,
  schema-validated, and stored as `self.payment_methods` (lines 489, 494, 529, 538,
  547, 572-578, 581, 590) but **never read** — `capabilities()` derives payment
  methods purely from individual rules' `payment_method` field instead of this
  richer dataset (with localized names, family groupings, `fee_rule_refs`).
  `StripePaymentMethodName`/`StripePaymentMethodEntry` in `providers/stripe/models.py:146-162`
  are consequently dead too. Either finish wiring this into
  `capabilities()`/`quote_schema()`, or drop the load entirely.
- `calculator.py:199-204`: the `_derive_status` branch handling
  `metadata.get("status") in ("range", "not_calculable", "included")` is unreachable —
  neither provider ever passes `metadata=` to `CompiledFeePlan` (only to
  `ExecutableFeeRule`, a different model). Delete the branch, or wire a provider to
  actually produce that status if a third provider is expected to need it.

Gate: full test suite (Python `pytest`, PHP `phpunit`, TS test runner) + conformance.

---

## Phase 2 — Extract the shared PayPal/Stripe provider template (the big one)

Both providers independently implement the **same five-stage pipeline** almost
line-for-line, and this duplication is the root cause of both F-rated
`compile_rules` functions and both D/E-rated `capabilities` functions. This is the
single highest-value refactor in the repo.

- [x] **Context building.** `providers/stripe/provider.py:53-120 _build_stripe_context`
      vs `providers/paypal/provider.py:179-226 _build_paypal_context` — both seed a
      dict from typed fields then merge free-form `transaction.context` with an
      **identical** contradictory-duplicate-value check
      (`QuoteNotAvailable("Contradictory duplicate value in transaction context...")`,
      copy-pasted with only variable names changed). Extract
      `_merge_context_overrides(context, extra)` into `providers/base.py`.
- [x] **Rule matching / specificity / selection** — the core of both F-rated
      `compile_rules`. Both do: bucket rules into full-match/missing-context/conflict
      → compute specificity → pick most-specific → detect ambiguity via a financial
      signature → raise one of `InsufficientTransactionContext`/`QuoteNotAvailable`/
      `AmbiguousFeeRules`. Concrete parallel line ranges:
      - bucketing: `stripe/provider.py:611-628` ≈ `paypal/provider.py:670-693`
      - ambiguity check: `stripe/provider.py:692-699` ≈ `paypal/provider.py:703-717`
      - tie-break: `stripe/provider.py:701` ≈ `paypal/provider.py:719`
      Extract a shared template in `providers/base.py`:
      `compile_generic(rules, context, *, is_evaluable, specificity, financial_signature, missing_dims, conflicts) -> selected_rule`,
      with each provider supplying only its 4 hook functions. Shrinks both
      `compile_rules` to ~40-60 lines of assembly.
      Concrete per-provider split (if the shared-template extraction is done
      incrementally instead of all at once):
      - Stripe `compile_rules` (601-735): `_match_candidates` (604-628),
        `_require_any_full_match` (630-642), `_require_evaluable_most_specific`
        (644-661), `_check_no_more_specific_missing` (663-676), `_select_base_rule`
        (678-699).
      - PayPal `compile_rules` (636-765): `_resolve_product_rules` (644-668),
        `_match_rules` (670-693), `_require_calculable` (695-701), `_select_rule`
        (703-719), `_check_surcharge_region_context` (721-734), `_resolve_source_url`
        (736-741).
- [x] **Shared `CapabilityAccumulator`.** Both `capabilities()` methods
      (`stripe/provider.py:795-868`, D-rated; `paypal/provider.py:787-877`, E-rated)
      build the *same seven collections* (products, variants, payment_methods,
      fee_shapes, currencies, dimensions, allowed_values) plus 5 classification
      buckets, in a triple-nested loop, differing only in per-rule classification and
      condition-value normalization. Extract a `CapabilityAccumulator` class in
      `base.py` with `.add_rule(...)`; each provider supplies only `_classify_rule` +
      `_normalize_conditions`. Reduces both to ~15-line loops.
- [x] **`_api_field_name` / `markets()` / `from_paths`/`from_documents`.** Same-shape
      pairs: `_api_field_name` (stripe 288-326, paypal 338-360, only the dicts
      differ); `markets()` (stripe 775-793, paypal 767-785); the loader classmethods
      (stripe 501-592, paypal 568-627, ~60 duplicated lines across 4 methods).
      Consolidate into shared helpers in `base.py`/`data.py`.
- [x] **Collapse `_condition_matches`/`_condition_status`** (stripe provider.py:178,
      207) — ~90% identical, same 6-operator dispatch written twice differing only in
      `bool` vs `"match"/"conflict"/"missing"` return. Keep one
      `_evaluate_condition(...) -> Literal[...]`, derive the boolean version from it.
- [x] **Split `_compile_stripe_components`** (332, D-rated, ~78 lines) into
      `_synthesize_legacy_components` (341-367), `_aggregate_components` (369-390),
      `_apply_behavior` (392-410).
- [x] **Split PayPal's `_compile_rule`** (439, D-rated, ~107 lines) into
      `_resolve_fixed_amount` (452-471), `_resolve_maximum_amount` (473-477),
      `_resolve_surcharge_rate` (479-488), `_build_executable_rules` (490-537). Note:
      `_rule_signature` (401-436, used only for ambiguity detection) currently
      re-derives the same fixed/max/surcharge resolution a second time for comparison
      purposes — after extraction, have it call the same three resolvers instead of
      duplicating their logic.
- [x] **Small dedups**: `_as_list` defined verbatim 3× (`stripe/provider.py:249-252`,
      `paypal/provider.py:262-265`, `audit.py:107-110`) → one shared helper.
      `_normalize_confidence` duplicated verbatim in `models.py:9-12` and
      `rules.py:9-12` → one definition. `SUPPORTED_SCHEMA_VERSIONS = {1}` defined
      independently in both providers with the same 4-call validation pattern → a
      shared `_check_schema_version(model, supported, provider_name)` in `base.py`.
      5+ near-identical `.upper()`-returning currency validators scattered across
      `models.py` → one `normalize_currency()` helper.

Gate: unit tests for `compile_rules`/`capabilities` per provider (existing coverage),
then full conformance suite (this phase touches the calculation core — highest-risk
phase in the repo, verify thoroughly).

---

## Phase 3 — Fix the audit.py coupling risk (correctness, not just cleanup)

- [x] `audit.py` hand-maintains **parallel copies** of the providers' dimension/
      operator knowledge instead of importing it. `_stripe_request_from_rule`
      (audit.py:265-398, E-rated) and `_paypal_request_from_rule` (audit.py:191-262,
      D-rated) reverse-engineer fake requests from rule conditions using hardcoded
      `transaction_fields`/`card_fields`/`STRIPE_KNOWN_DIMENSIONS` sets
      (audit.py:25-104, 296-322, 50-88) that duplicate — and can silently drift from —
      the providers' own `_api_field_name` mapping dicts and `_normalize_conditions`
      dimension lists. **This is the same class of bug as Phase 0**: a new dimension
      added to a provider won't be flagged as "known" in the audit tool until someone
      remembers to update the parallel constant by hand.
      **Fix**: make each provider's `_api_field_name` mapping (already a dict, just
      currently function-local — hoist to a module constant as part of Phase 2) the
      single source of truth; have `audit.py` build requests generically from it
      instead of re-declaring `transaction_fields`/`card_fields`/
      `STRIPE_KNOWN_DIMENSIONS`/`AUDIT_KNOWN_DIMENSIONS`/`KNOWN_OPERATORS` by hand.
- [x] `audit.py:481-486, 557-560` imports the private `_executable_from_rule` from
      `providers/stripe/provider.py` and constructs throwaway `PayPalProvider`
      instances per-rule — reaching into provider internals with no type-checked
      contract. Expose an explicit audit hook (e.g.
      `provider._compile_single_rule_for_audit(rule, context)`) on
      `providers/base.py`'s `FeeProvider` contract instead.

Gate: `make audit-contract` (already a release-gate job per `.github/workflows/ci.yml`)
must still pass and — ideally — now catch dimensions it previously missed.

---

## Phase 4 — Split `tools/paypal-sandbox-validation/cli.py` into a package

`cli.py` is 2,758 lines with 22 distinct `@cli.command(...)` entries, all sharing
setup boilerplate (`_env_csv_default`, `parse_accounts_csv`/`validate_accounts`,
`QuoteAdapter` construction, `_execute_plan`/`generate_run_id`).

- [x] Split into `cli/` by command family:
      `__init__.py` (group + `main()`, was 1-107, 2753-2758) ·
      `probing.py` (validate-config/probe/probe-nvp, 109-280) ·
      `execution.py` (plan/run/surcharge-pilot/regional-pilot, 281-725) ·
      `qualify.py` (qualify/regional-validation + merchant filtering, 726-985,
      2556-2716) ·
      `runner.py` (the shared execution engine: `_execute_plan`, `_run_case`,
      `_merge_existing_case`, `_build_quote`, `_create_order`, `_approve_order`,
      `_capture`, `_reconcile_case`, `_case_dict`, 986-1553) ·
      `reconcile_report.py` (reconcile/report, 1554-1597) ·
      `diagnose.py` (diagnose + helpers, 1598-1903) ·
      `verify.py` (verify-merchant-association, 1904-1985) ·
      `manual_approval.py` (create-manual-approval-case, 1986-2225) ·
      `manual.py` (manual-plan/run/report/qualify, 2226-2374, 2487-2752) ·
      `profile_pricing.py` (record/inspect-profile-pricing, 2375-2486).
- [x] **Required in the same commit**: `tests/test_paypal_sandbox_validation_hardening.py`
      monkeypatches private helpers *by string path on the `cli` module*
      (`paypal_sandbox_validation.cli._create_order`, `._approve_order`, `._capture`,
      `._reconcile_case`, `.ensure_surcharge_case`, `.probe_credentials`). Once these
      move to `cli/runner.py`, a patch on the `cli` package's re-export will **not**
      affect calls made from inside `runner.py`'s own module scope — update all ~7
      monkeypatch targets to the new submodule paths (e.g.
      `paypal_sandbox_validation.cli.runner._create_order`) in the same change, or the
      tests will silently stop patching.

Gate: full `tests/test_paypal_sandbox_validation_*.py` suite, paying special attention
to the hardening tests above.

---

## Phase 5 — Decompose the F/E/D-rated sandbox-tool functions

All of these are pure/data-flow functions already covered by existing tests at their
public boundary — internal splitting is behavior-preserving and low-risk.

- [x] `accounts.py:173 validate_accounts` (F, worst after `decompose_case`) →
      `_duplicate_signals`, `_credential_signals`, `_aggregate_invalid_merchants`,
      `_aggregate_invalid_buyers` (line ranges 177-219).
- [x] `diagnostics.py:106 decompose_case` (F, worst in repo) →
      `_decompose_base`, `_decompose_surcharge`, `_decompose_components`,
      `_infer_rounding_point` (121-217).
- [x] `diagnostics.py:408 classify_root_cause` (D) →
      `_extract_observed_pct_fixed`, `_classify_from_stable_formula`,
      `_classify_from_single_observation` (436-543).
- [x] `diagnostics.py:717 classify_de_checkout_outcome` (D) →
      `_classify_payload_variant_outcome`, `_classify_manual_vs_playwright`,
      `_classify_checkout_layer_failure` (736-787).
- [x] `qualification.py:410 classify_qualification` (E) →
      `_qualification_validation_failures`, `_qualification_incalculable`,
      `_build_qualification_fee_maps`, `_is_representative`,
      `_is_sandbox_specific_pricing` (432-537).
- [x] `qualification.py:756 validation_summary` (D) →
      `_count_public_rate_outcomes`, `_count_diagnostic_outcomes` (782-805).
- [x] `qualification.py:171 classify_manual_send_pricing` (D) →
      `_filter_fresh_valid_cases`, `_check_all_match_public`,
      `_verify_formula_stability` (182-235).
- [x] `reconciliation.py:10 reconcile` (E) — highest call-frequency function in the
      tool, so highest-leverage split → `_extract_evidence_amounts`,
      `_check_preconditions`, `_populate_match_result` (17-122).
- [x] `manual_flow.py:632 infer_formula` (D) →
      `_eligible_single_scenario_cases`, `_collect_gross_fee_pairs`,
      `_least_squares_fit` (641-684).
- [x] `reporting.py:74 build_summary` (E) →
      `_classify_case_bucket`, `_build_case_summary_row` (130-203).
- [x] `reporting.py:215 save_summary_markdown` (D) →
      `_render_totals_section`, `_render_cases_table`, `_render_schedule_table`,
      `_render_mismatch_table` (217-330).
- [x] `approval.py:11 approve_order` (D) →
      `_map_playwright_result_to_outcome`, `_map_callback_state_to_outcome` (44-78).

---

## Phase 6 — Cross-module dedup (sandbox tool + service)

- [x] **`quote_adapter.py` duplicates `payment_fee.calculator`, verified byte-for-byte
      identical**: `quote_adapter.py:360-380` `ZERO_DECIMAL_CURRENCIES` (16 entries)
      matches `calculator.py:16-31` exactly; `quote_adapter.currency_exponent`/
      `quantize_currency` (388-403) reimplement `calculator.currency_quantum`/
      `quantize_money` (37-46) with the same `ROUND_HALF_UP` rule and the same
      three-decimal-currency set `{BHD, JOD, KWD, OMR, TND}`. Replace with
      `from payment_fee.calculator import ZERO_DECIMAL_CURRENCIES, currency_quantum, quantize_money`;
      keep only `minor_units()` (no library equivalent) as a thin wrapper. Removes
      ~25 duplicated lines and — more importantly — removes the risk of the two
      currency tables silently drifting if a currency is added/reclassified upstream.
- [x] **`_decimal(value)` helper** (`try: Decimal(str(value)) except: None`) is
      reimplemented identically in `diagnostics.py:34`, `reconciliation.py:126`,
      `manual_flow.py:625` → one shared helper (e.g. in `quote_adapter.py` or a new
      `numeric.py`).
- [x] **PayPal Sandbox login-form automation** duplicated between
      `browser.py:64-90` (`PayPalBrowser.login`) and `manual_browser.py:165-199`
      (`ManualPaymentBrowser.login`) — same selectors, same flow. Extract
      `_fill_paypal_login_form(page, email, password)` into a shared
      `browser_common.py` so a PayPal UI change needs one fix, not two.
- [x] **Raw status strings instead of the `ReconciliationStatus` enum**:
      `reporting.py:129-166` (`build_summary`), `reporting.py:337-349` (`_JUNIT_*`
      sets), and `qualification.py:788-805` (`validation_summary`) all hardcode
      `"fee_mismatch"`, `"match"`, etc. even though `models.py:174-200` already
      defines `ReconciliationStatus` as a `StrEnum` with these exact values. Use the
      enum consistently; consider one shared "status → bucket" table used by all
      three call sites (they currently re-derive overlapping bucket groupings
      independently).
- [x] **Two independent fee-formula-inference implementations**:
      `manual_flow.py:632 infer_formula` (least-squares regression over `Case`
      objects) vs `diagnostics.py:234/293 _percentage_plus_fixed_candidates`/
      `_base_plus_surcharge_candidates` (two-point-slope inference over dict
      observations) solve overlapping problems with different math and input
      shapes. **Design decision needed**, not a mechanical fix: consolidate on one
      canonical `infer_formula(observations: list[dict]) -> Formula` and convert
      `Case` objects to the same shape before calling it.
- [x] **Service-layer minor dedup** (low priority, `services/payment-fee-service` has
      no structural issues otherwise): `engine_holder.py:71-74 _error_message()` and
      `app.py:45-56 _payment_fee_error_status()` both independently pattern-match on
      `isinstance(exc, PaymentFeeError)` — extract one `errors.py` helper
      (`status_for(exc)`/`message_for(exc)`).
- [x] `cli.py:1744 _default_currency` (sandbox tool) duplicates
      `configuration.currency_for_country` (used elsewhere at `cli.py:729-736`,
      `qualification.py:546-548`) — confirm identical country coverage, then delete
      `_default_currency` and call `currency_for_country` directly.

---

## Phase 7 — PHP/TypeScript structural parity (beyond the Phase 0 bug fix)

- [x] **Extract shared condition-matching logic per language**, mirroring Python's
      `rules.py` pattern. PHP's `StripeProvider.php` (239-643) and TS's `provider.ts`
      (267-606) each embed a full generic condition-matching engine
      (`normalizeConditions`, `conditionStatus`, `valuesEqual`, `numericCompare`,
      `isEvaluable`, `selectAdditiveRules`, component compilation, `apiFieldName`) as
      private methods inside the 600+ line provider class. Extracting this into a
      dedicated `RuleMatcher`/`ConditionEvaluator` module per language (PHP:
      standalone class; TS: `condition-matcher.ts`) is exactly what would have made
      the Phase 0 bug easier to catch by code review, and reduces the risk of it
      recurring when either port's provider file is next touched.
- [x] **Add a PHP equivalent of the shared `ExecutableFeeRule` shape.** Python
      (`rules.py`) and TS (the `ExecutableRule` type referenced from `calculator.ts`)
      both have a typed shared IR; PHP's `StripeProvider::executableFromRule`/
      `PayPalProvider::compileRule` return loose `array<string,mixed>` instead. Add
      `Model/ExecutableFeeRule.php` for type-safety symmetry.
- [x] **`QuoteRequestFactory.php`** has no equivalent name/structure in Python or TS
      (the same request-building logic lives inline in Python's `models.py`/engine).
      Not wrong, but worth a short doc note in the PHP package README explaining the
      intentional structural divergence, so it isn't mistaken for drift during review.
- [x] Low priority, likely intentional: `run_python.py`/`run_php.php`/
      `run_typescript.mjs` (the conformance runners) are near-verbatim
      transliterations of the same harness algorithm in each language. Deliberately
      keeping them independent (rather than sharing code) avoids a shared bug masking
      a real cross-language divergence — which is exactly the property that caught
      nothing in Phase 0's TS gap because no fixture exercised it, not because the
      harness shares code. If kept duplicated, add a one-line comment in each file
      stating that duplication here is intentional.

Gate: `make test-conformance` (all 3 languages) + the differential runner, after every
change to a provider file in either language.

---

## Phase 8 — Performance optimizations

Hot path: `POST /v1/quotes` → `engine.quote()` → `provider.compile_rules()` →
`calculator._calculate_rule()`. The dataset itself is loaded/cached correctly
(confirmed: `EngineHolder` only rebuilds on startup or the periodic/admin refresh,
guarded by an `asyncio.Lock` — no per-request disk I/O). The problem is entirely
inside `compile_rules()`, which re-does full-dataset work on every request.

**High impact**

1. **`compile_rules()` does an O(n) linear scan over every rule in the market on
   every request** — confirmed not cached. PayPal
   (`providers/paypal/provider.py:655`): `[r for r in derived.transaction_fee_rules if r.id.lower() == product_id]`
   scans all rules with a `.lower()` compare per rule, per request. Stripe
   (`providers/stripe/provider.py:611-628`): iterates every rule in the market and
   calls `_normalize_conditions` (rebuilding a ~30-tuple list per rule) — **and then
   `_select_additive_rules` (737-773) re-iterates `market.rules` a second time and
   calls `_normalize_conditions` again for every rule**, so every rule is normalized
   twice per request.
   - [x] **Fix**: build per-`account_country` indexes once in each provider's `__init__`
         (alongside the existing `self._countries`/`self._markets`) —
         `dict[product_id][variant_id] -> list[rule]` for PayPal, and pre-split
         `market.rules` into base vs. additive lists with pre-normalized conditions cached
         at load time for Stripe. Eliminate the duplicate `market.rules` pass in
         `_select_additive_rules` by bucketing base/additive candidates in the same pass as
         the main matching loop.
2. - [x] **Duplicate Decimal computation for the selected rule**: Stripe's
      `_rule_financial_signature` (277-285) calls `_compile_stripe_components(rule, currency)`
      for the ambiguity check, then `_executable_from_rule` (442) calls
      `_compile_stripe_components` **a second time** for the same rule. Cache the result
      per `(rule_id, currency)` within one request (or via `functools.lru_cache` on the
      pure function).

**Medium impact**

3. **Repeated Decimal string-parsing of static rule values.** `Decimal(str(value))`
   for the *same static* rule-condition values (fixed amounts, percentage thresholds)
   happens on every request that touches that rule, in `calculator.py:37-57`
   (`currency_quantum`/`to_decimal`) and in provider condition-matching
   (`_amount_condition_matches`, `_value_matches`, `_values_equal`,
   `_numeric_compare`). Pre-convert static rule fields to `Decimal` once at
   dataset-load time (e.g. via a Pydantic `field_validator`) instead of re-parsing
   per request. For `currency_quantum`, precompute a `dict[str, Decimal]` lookup
   instead of set-membership + `Decimal(...)` construction on every call.
4. - [x] **Pydantic model reconstruction per request.** `ExecutableFeeRule`/
      `CompiledFeePlan` (`rules.py`) are rebuilt from scratch (with full validation) on
      every request even though most fields (label, currency, percentage, metadata) are
      static per `(provider, market, rule_id)` — only the amount-dependent arithmetic in
      `calculator._calculate_rule` (131-183) actually varies per transaction. Precompute
      and cache the static template objects once per `(provider, market, rule_id)` at
      provider-load time; reconstruct only the per-transaction `FeeComponent` at request
      time.  See `BENCHMARK.md` for the before/after hot-path results.

**Low impact / confirmed non-issues (no fix needed, noted so they aren't re-flagged)**

5. Dataset loading (`data.py`, `services/.../data/source.py`) is correctly cached via
   `EngineHolder` — not re-read per request.
6. FastAPI's `calculate_quote` route (`api/routes.py:30-35`) is intentionally a sync
   `def`, so FastAPI runs it in a thread-pool executor and the CPU-bound Decimal/
   rule-matching work does not block the event loop. **Do not** convert to
   `async def` without wrapping the engine call in `run_in_executor` — otherwise
   findings #1/#2 above would start blocking the loop directly.
7. `JsonDataSource.read_bytes` (`data/source.py:26-35`) opens a new `httpx.Client`
   per call for remote datasets — only relevant at refresh time (not per-request), so
   low priority; reuse a shared `httpx.Client` if refresh intervals ever become
   frequent.

Benchmark before/after: completed in `BENCHMARK.md`.  The optimized checkout shows
~+2.5% throughput and ~2% lower median latency on the Stripe US hot path (149 rules,
5,000 timed requests after a 500-request warm-up).

---

## Phase 9 — Workspace-level structural issues

- [x] **Dataset pinning doesn't match documented policy — reproducibility gap.**
      `docs/RELEASING.md` states: "Provider data revisions should be pinned in the
      service configuration and in conformance cases." Verified: neither happens.
      `services/payment-fee-service/src/payment_fee_service/settings.py:41,44` points
      at the floating `main` branch of both data repos
      (`https://raw.githubusercontent.com/smeinecke/paypal-fee-data/main`), and no
      revision field exists in `contracts/conformance/manifest.json` or the case
      files. `.github/workflows/ci.yml`'s `resolve-data-revisions` job (21-42) only
      resolves `main` HEAD *at run time* — it does thread that one resolved SHA via
      `needs:` outputs into every downstream job (48, 137, 251, 332), which correctly
      keeps a single CI run internally consistent, but nothing persists that SHA
      between runs. Since both `paypal-fee-data` and `stripe-fee-data` are crawled and
      pushed to **daily** (confirmed via their own `daily-crawl.yml` workflows), the
      same `payment-fee-service` commit can produce different conformance/quote
      results on different days — silently, since nothing here would catch it.
      **Fix**: add an actual pin — e.g. a `contracts/data-revisions.json` (or extend
      `contracts/dataset-support.json`) recording the exact commit SHA of each data
      repo the current contract/conformance cases were validated against; have
      `resolve-data-revisions` read and check out *that* pin instead of `main`, and
      have `settings.py`'s default `data_url` support a pinned-ref form (falling back
      to `main` only for local dev, not CI/production). Bump the pin explicitly as
      part of the release process already described in `RELEASING.md`.
- [x] **Stray empty directories.** `config/` (repo root) is empty, untracked, and not
      referenced by any code, `Makefile` target, or Docker file — looks like dead
      scaffolding; remove it or document its intended purpose.
      `artifacts/paypal-sandbox-provisioning/` is also empty but, unlike its four
      sibling `artifacts/paypal-sandbox-{,-diagnostics,-manual,-observations,-qualification}/`
      directories, is **not** covered by any `.gitignore` pattern (`.gitignore:29-33`
      lists the other four but not this one) — if the sandbox tool ever writes into
      it, that output will get committed by accident. Add the missing `.gitignore`
      line, or remove the directory if it's unused.
- [x] **Version policy check — confirmed healthy, no action needed.** Per
      `docs/RELEASING.md`, all four artifacts share one major.minor: Python core
      (`packages/payment-fee/pyproject.toml`), the service
      (`services/payment-fee-service/pyproject.toml`), and the TS package
      (`packages/payment-fee-typescript/package.json`) are all at `0.4.0`. PHP's
      `composer.json` has no `version` field, which is normal Composer convention
      (versions are sourced from git tags) — not drift. Noted here only so a future
      pass doesn't re-flag it as an inconsistency.

---

## Execution order & safety net

| Phase | Risk | Gate |
|---|---|---|
| 0 cross-language bug fix | **High priority, correctness** | new conformance cases + full differential run |
| 1 dead code | Low | full test suite (3 languages) |
| 6 (quote_adapter/calculator dedup, small helpers) | Low | pytest |
| 8.3–8.7 perf (static Decimal/template caching) | Low–Med | conformance + benchmark |
| 4 cli.py package split | Medium | sandbox-tool test suite incl. hardening monkeypatches |
| 5 sandbox hotspot decomposition | Low (pure functions, tested at boundary) | sandbox-tool test suite |
| 2 shared provider template | **Medium-high — touches calculation core** | provider unit tests + full conformance suite |
| 3 audit.py coupling fix | Medium | `make audit-contract` |
| 7 PHP/TS structural parity | Medium | `make test-conformance` after every provider-file change |
| 8.1–8.2 perf (rule indexing, dedup compilation) | Medium | conformance + benchmark |
| 9 dataset pin + stray directories | Low (process/hygiene) | CI reproducibility check — same commit, two runs, same result |

Non-negotiable invariants:
- `POST /v1/quotes`, `/v1/providers`, capabilities/quote-schema endpoints, and the
  sandbox-validation CLI's 22 commands keep accepting all current inputs/flags.
- All three language ports keep producing byte-identical quote results for every case
  in `contracts/conformance/cases/` — verified via `make test-conformance` after every
  phase.
- `make audit-contract` (release-gate job) keeps passing, and Phase 3 should make it
  strictly more thorough, not less.
- No published API/CLI contract changes without also updating conformance fixtures.

---

## Resolved cross-language correctness notes

- Boolean equality is now strict in all three ports (`_values_equal`/`valuesEqual`): a boolean is only equal to another boolean with the same value. Numeric `1`/`0` and strings such as `"true"`/`"false"` no longer accidentally match booleans, eliminating language-specific truthiness coercion bugs.
  New unit tests in Python, PHP, and TypeScript cover all mismatch pairs, and the
  conformance suite gained `stripe-us-boolean-strict-match`,
  `stripe-us-boolean-strict-string-fallback`, and
  `stripe-us-boolean-strict-numeric-fallback` to lock in the behavior across languages.
- Additive rule selection in PHP and TypeScript now raises `InsufficientTransactionContext` when `payment_method` is missing, matching the Python implementation.
- The Stripe "assumed a successful transaction" assumption is now emitted
  consistently across all three ports only when `success` is `true` or absent from the
  request context; string/integer `success` values no longer trigger the assumption.
