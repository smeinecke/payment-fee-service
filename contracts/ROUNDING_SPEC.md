# Rounding Specification

All implementations use arbitrary-precision decimal arithmetic for financial calculations. Binary floating-point must never be used for fee amounts, percentages, or monetary totals.

## Input representation

* All input decimal values are strings.
* Percentages are decimal strings (e.g. `"2.9"` means 2.9%).
* Monetary values serialize as fixed-point decimal strings.
* Currency codes are uppercase ISO-style strings (`EUR`, `JPY`, `USD`).

## Currency minor units

Currency precision is derived from `contracts/currencies.json`. Implementations generate native constants from this canonical file and must not maintain separate handwritten precision tables.

| Minor units | Currencies | Rounding quantum |
|-------------|------------|------------------|
| 0           | `BIF`, `CLP`, `DJF`, `GNF`, `ISK`, `JPY`, `KMF`, `KRW`, `PYG`, `RWF`, `UGX`, `VND`, `VUV`, `XAF`, `XOF`, `XPF` | `1` |
| 2           | Default for all other currencies | `0.01` |
| 3           | `BHD`, `JOD`, `KWD`, `OMR`, `TND` | `0.001` |

## Rounding mode

All intermediate monetary values are rounded with **round half up** (`ROUND_HALF_UP`) to the currency quantum.

## Canonical output

* No scientific notation.
* Negative zero must serialize as `0` (or `0.00`, `0.000` depending on minor units).
* Trailing zeros up to the currency minor units must be preserved in normalized JSON results.
* Values must not include a leading `+` sign.

## Calculation order

Fees are calculated in this order:

```text
base percentage
+ direct fixed amount
+ schedule fixed amount
+ additive percentage
+ additive fixed amount
apply minimum
apply maximum
round in fee currency
calculate net amount
```

The `processing_fee` is the sum of all component amounts after rounding. The `net_amount` is `amount - processing_fee`, rounded to the same currency quantum.

## Component rounding

Each calculable rule produces one `FeeComponent`. The component amount is rounded to the fee currency quantum before it is appended to the response. The total is the rounded sum of already-rounded component amounts.
