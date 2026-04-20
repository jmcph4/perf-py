# perf.py

Compute the annualised money-weighted return of an ASX ETF / share portfolio
from the list of dated unit purchases. Reports a nominal XIRR plus a
deflated XIRR using either Australian CPI (→ real return) or a sovereign
bond yield (→ excess return over the risk-free rate).

## Install & run

Dependencies are declared in `pyproject.toml` and managed by
[`uv`](https://docs.astral.sh/uv/):

```sh
uv run perf.py --help
```

`uv` syncs `pandas`, `scipy`, and `yfinance` into `.venv` on the first run.

## Inputs

You supply only **dates and unit counts**. Prices, distributions, CPI, and
bond yields are all fetched from public sources.

A purchase is `TICKER:DATE:UNITS`, e.g. `VGS.AX:2021-03-15:120`. If the
ticker is omitted, `--ticker` is used (default `VGS.AX`). Bare tickers
with no exchange suffix (e.g. `VGS`) are assumed ASX-listed and
normalised to `.AX`; supply an explicit suffix to override.

Either repeat `--purchase`, or pass a CSV via `--purchases`:

```csv
ticker,date,units
VGS.AX,2021-03-15,120
VGS.AX,2022-08-02,80
VAS.AX,2022-01-10,50
```

Two-column rows (`date,units`) are also accepted and inherit `--ticker`.
The header is optional.

## Examples

Single holding, real return via CPI:

```sh
uv run perf.py --purchase 2021-03-15:120 --purchase 2022-08-02:80
```

Multi-ticker portfolio:

```sh
uv run perf.py \
  --purchase VGS.AX:2021-03-15:120 \
  --purchase VAS.AX:2022-01-10:50
```

Assume DRP-style reinvestment at the ex-date close:

```sh
uv run perf.py --purchases holdings.csv --reinvest
```

Excess return vs 3-year Australian govt bond:

```sh
uv run perf.py --purchases holdings.csv --risk-free au --risk-free-tenor 3
```

Excess return vs 10-year US Treasury:

```sh
uv run perf.py --purchases holdings.csv --risk-free us
```

## CLI reference

| Flag | Default | Purpose |
|---|---|---|
| `--ticker` | `VGS.AX` | Fallback ticker for purchases that omit one |
| `--purchase [TICKER:]DATE:UNITS` | — | Repeatable purchase spec |
| `--purchases FILE` | — | CSV of purchases |
| `--as-of YYYY-MM-DD` | today | Valuation date |
| `--reinvest` | off | Convert distributions into units at ex-date close |
| `--cpi-series` | `GCPIAG` | RBA Table G1 series id (All Groups CPI) |
| `--risk-free {au,us}` | off (use CPI) | Use a sovereign bond yield as deflator |
| `--risk-free-tenor YEARS` | `10` | Tenor of the bond series |

AU tenors: 2, 3, 5, 10 (RBA series `FCMYGBAG{tenor}`).
US tenors: 1, 2, 3, 5, 7, 10, 20, 30 (FRED series `DGS{tenor}`).

## Data sources

- **Prices & distributions**: Yahoo Finance via `yfinance`. Close price on
  the purchase date is used as the execution price. Distributions are the
  per-unit cash amounts on the ex-date.
- **Australian CPI**: RBA Statistical Table G1, series `GCPIAG` (All
  Groups, original — sourced from the ABS).
- **Australian bond yields**: RBA Statistical Table F2 (daily).
- **US Treasury yields**: FRED `DGS{tenor}` series (daily).

## Method

1. Each purchase emits a dated cashflow of `-units × close(date)`.
2. Each ex-date dividend emits `+units_held × dist_per_unit`, unless
   `--reinvest` is set, in which case the cash is divided by that day's
   close and added to units held.
3. A terminal cashflow of `+total_units × latest_close` is added on the
   last available trading day for each ticker.
4. All per-ticker cashflows are concatenated; `XIRR` (Brent root-find on
   the NPV equation, actual/365 daycount) is computed on the combined
   series.
5. For the deflated XIRR, every cashflow is scaled by
   `numéraire(valuation_date) / numéraire(cashflow_date)`. The numéraire
   is either the CPI index directly or a daily-compounded index built
   from the bond yield (actual/365, held constant between observations).

## Caveats

- All tickers are assumed AUD-denominated (ASX-listed). With `--risk-free us`,
  cashflows are converted to USD via daily AUD/USD spot (Yahoo Finance
  `AUDUSD=X`) before computing the excess return, so the reported excess
  is a USD figure and an additional USD nominal return is shown. All other
  modes stay entirely in AUD.
- Yahoo's distribution history for ASX ETFs is occasionally incomplete
  or mis-dated (especially the EOFY top-up declared in July). Sanity-check
  `Total distributions` against the issuer's published history before
  trusting the output.
- Franking credits, foreign income tax offsets, and AMIT attribution are
  not modelled — the figure is a pre-tax cash-distribution return.
- The DRP model uses the ex-date close as the reinvestment price, which
  is a rough proxy for real DRP pricing (typically a VWAP, sometimes at
  a small discount).
- RBA table URLs occasionally change; if `f2-data.csv` or `g1-data.csv`
  starts 404ing, update `RBA_F2_URL` / `RBA_G1_URL` at the top of the
  file.
