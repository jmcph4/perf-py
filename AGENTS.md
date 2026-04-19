# AGENTS.md

Guidance for coding agents working on this repo. The user-facing docs live
in `README.md`; this file is about *modifying* the code.

## What this project is

A single-file Python CLI (`perf.py`) that computes annualised money-weighted
returns (XIRR) for an ASX portfolio. Inputs are `(ticker, date, units)`
triples; everything else (prices, distributions, CPI, bond yields) is
fetched from public sources at runtime. The project is managed by `uv` —
dependencies live in `pyproject.toml` and the lockfile is `uv.lock`.

## Files

- `perf.py` — the entire program.
- `pyproject.toml` / `uv.lock` — dependencies, managed by `uv`.
- `README.md` — user-facing docs.
- `AGENTS.md` — this file.

No tests, no CI. Keep it that way unless the user asks.

## Architecture

`perf.py` is laid out top-to-bottom in the order data flows:

1. **Purchase parsing** (`Purchase`, `parse_purchase_arg`, `load_purchases_csv`).
   Accepts `TICKER:DATE:UNITS` or `DATE:UNITS` (with `--ticker` fallback).
   `_normalize_ticker` uppercases and appends `.AX` to suffixless tickers.
2. **XIRR primitives** (`xnpv`, `xirr`). Uses `scipy.optimize.brentq`
   rooting on the NPV equation, actual/365 daycount.
3. **Data fetchers**:
   - `_fetch_rba_csv(url, series_id)` — generic RBA Statistical Tables CSV
     reader. Finds the `Series ID` header row, parses from there, pulls
     the named column.
   - `_fetch_fred_csv(series_id)` — FRED `fredgraph.csv` endpoint.
   - `fetch_cpi(series_id)` — wraps `_fetch_rba_csv` against Table G1.
   - `fetch_risk_free_yields(country, tenor)` — dispatches to RBA (AU) or
     FRED (US) using the `AU_BOND_SERIES` / `US_BOND_SERIES` maps.
   - `build_rf_index(yields)` — compounds a daily annualised-yield series
     into a numéraire index so it can deflate cashflows the same way CPI
     does.
   - `series_at(s, d)` — forward-fill lookup (last observation on or
     before `d`).
4. **Per-ticker cashflow construction** (`build_ticker_cashflows`).
   Pulls OHLC + dividends from yfinance with `auto_adjust=False`, walks
   buy/div events in date order, optionally reinvests at ex-date close.
5. **Deflation** (`deflate`). Numéraire-agnostic: scales each cashflow
   by `numeraire(target) / numeraire(d)`.
6. **`main`**. Parses CLI, groups purchases by ticker, builds per-ticker
   results, concatenates into a portfolio cashflow stream, computes two
   XIRRs (nominal + deflated), prints per-ticker and portfolio blocks.

## Invariants to preserve

- **Only `(ticker, date, units)` come from the user.** Prices,
  distributions, CPI, and yields are always fetched. Do not add flags
  that take price or distribution data directly — that's the whole point
  of the tool.
- **Cashflow sign convention**: purchases negative, distributions and
  terminal valuation positive. `xirr` assumes at least one sign change.
- **Deflation is a scalar transform per cashflow.** Do not re-implement
  it as a rate-subtraction hack; the numéraire-ratio form is correct for
  both CPI and risk-free and is what the `deflate` call sites expect.
- **All tickers are assumed AUD.** The docstring and README say so. If
  the user asks for FX support, this assumption has to be lifted
  deliberately — don't silently introduce a currency column.
- **No I/O outside the fetchers and `print`.** Don't write files, don't
  cache to disk, don't log. The script is meant to be stateless.

## Adding a new risk-free series

Add the tenor → series-id mapping to `AU_BOND_SERIES` or `US_BOND_SERIES`.
The help text for `--risk-free-tenor` auto-includes the keys of these
dicts, so no other code needs changing.

If you need a new *country*, extend `fetch_risk_free_yields` with another
branch and a corresponding `{country}_BOND_SERIES` dict. Update the
`--risk-free` `choices=` list and the README table.

## Adding a new deflator family

The `deflate` function takes any `pd.Series` indexed by `date` whose
values rise monotonically with time (a numéraire). To add e.g. a wage
index or a different inflation measure:

1. Write a `fetch_X` that returns `pd.Series[date -> float]`.
2. Extend the `if args.risk_free / else` block in `main` with a new
   branch selected by a new CLI flag; set `numeraire`, `deflator_label`,
   and `return_label` appropriately.

Keep `return_label` semantically honest: "Real return" for purchasing-
power deflators, "Excess return vs X" for investable-benchmark deflators.

## Modifying distribution handling

The reinvestment model uses the ex-date close as the DRP price. This is
a proxy — real DRPs use a VWAP window, sometimes at a discount. If the
user asks for fidelity here, the hook is inside `build_ticker_cashflows`
where `reinvest` is checked. Don't push this logic out to `main`.

Watch for:
- `units_held <= 0` at a dividend event (e.g. dividend before first buy
  when date ranges overlap) — already handled, preserve the guard.
- Same-day buy + dividend: the event sort puts buys first, so units
  bought that day do receive the dividend. If you change this, document
  it — it affects the output.

## Yahoo data gotchas

- `auto_adjust=False` is required. With `auto_adjust=True`, Close is
  back-adjusted for distributions and the `Dividends` column still
  carries the cash amount, which would double-count.
- `yf.Ticker(...).history` returns a DatetimeIndex with timezone; the
  code casts to `.date`. Preserve this — `series_at` and the event sort
  assume plain `date` objects.
- Yahoo's dividend series for ASX ETFs is imperfect (occasional missing
  or mis-dated quarters, EOFY top-up lag). The README flags this for
  users; don't paper over it in code by inventing distributions.

## RBA URL fragility

`RBA_G1_URL` and `RBA_F2_URL` are the single points of brittleness. The
RBA occasionally renames or splits these tables (e.g. F2 into F2.1, F2.2).
If a fetch fails:

- First check whether the URL has moved (browse
  `https://www.rba.gov.au/statistics/tables/`).
- Then check whether the expected series ID still lives in the new file.
- Update the constant and, if needed, the `AU_BOND_SERIES` mapping.

Don't silently fall back to a different series — surface the error.

## Dependencies

Manage via `uv`:

- `uv add <pkg>` — add a runtime dependency.
- `uv remove <pkg>` — drop one.
- `uv sync` — reconcile `.venv` with the lockfile.

Don't hand-edit `uv.lock`. Don't reintroduce a PEP 723 inline metadata
block in `perf.py` — `pyproject.toml` is authoritative now.

## Style

- Python 3.11+. `from __future__ import annotations` is in place; use
  PEP 604 (`str | None`), built-in generics (`list[...]`).
- Dataclasses for structured returns.
- Keep the file single-module. If it grows past ~500 lines, ask the user
  before splitting.
- No comments explaining *what* the code does; only non-obvious *why*
  comments. The user prefers terse code.
