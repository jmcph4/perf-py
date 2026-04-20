"""Annualised money-weighted performance of an ASX portfolio.

Fetches prices and distributions from Yahoo Finance, builds the series
of dated cashflows implied by the supplied purchases, and reports the
nominal XIRR plus a deflated XIRR. The deflator is either Australian
CPI from the RBA (producing a real return) or a sovereign bond yield —
Australian govt bonds (RBA Table F2) or US Treasuries (FRED DGS series) —
producing an excess return over that risk-free rate.

Every ticker is assumed to be ASX-listed and quoted in AUD, so no FX
conversion is performed. Supplying a foreign listing will mix currencies
and the output will be meaningless.
"""

from __future__ import annotations

import argparse
import csv
import io
import sys
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import yfinance as yf
from scipy.optimize import brentq


@dataclass
class Purchase:
    ticker: str
    on: date
    units: float


def _normalize_ticker(t: str) -> str:
    t = t.strip().upper()
    return t if "." in t else f"{t}.AX"


def _parse_purchase_fields(
    fields: list[str], default_ticker: str | None, origin: str
) -> Purchase:
    if len(fields) == 3:
        ticker, date_s, units_s = fields
    elif len(fields) == 2:
        if default_ticker is None:
            raise ValueError(
                f"{origin}: 2-field row needs --ticker as default, got {fields!r}"
            )
        ticker, date_s, units_s = default_ticker, fields[0], fields[1]
    else:
        raise ValueError(
            f"{origin}: expected 2 or 3 fields (TICKER,DATE,UNITS), got {fields!r}"
        )
    return Purchase(
        ticker=_normalize_ticker(ticker),
        on=datetime.strptime(date_s.strip(), "%Y-%m-%d").date(),
        units=float(units_s.strip()),
    )


def parse_purchase_arg(s: str, default_ticker: str | None) -> Purchase:
    sep = ":" if ":" in s else ","
    return _parse_purchase_fields(s.split(sep), default_ticker, f"--purchase {s!r}")


def load_purchases_csv(path: Path, default_ticker: str | None) -> list[Purchase]:
    out: list[Purchase] = []
    with path.open() as f:
        for row in csv.reader(f):
            if not row:
                continue
            first = row[0].strip().lower()
            if first in {"ticker", "date"}:
                continue
            out.append(_parse_purchase_fields(row, default_ticker, str(path)))
    return out


def xnpv(rate: float, cashflows: list[tuple[date, float]]) -> float:
    t0 = cashflows[0][0]
    return sum(cf / (1.0 + rate) ** ((d - t0).days / 365.0) for d, cf in cashflows)


def xirr(cashflows: list[tuple[date, float]]) -> float:
    if not cashflows:
        raise ValueError("xirr: no cashflows")
    cashflows = sorted(cashflows, key=lambda x: x[0])
    if cashflows[0][0] == cashflows[-1][0]:
        raise ValueError("xirr: all cashflows share a single date; return is undefined")
    lo, hi = -0.9999, 100.0
    f_lo, f_hi = xnpv(lo, cashflows), xnpv(hi, cashflows)
    if f_lo * f_hi > 0:
        raise ValueError(
            f"xirr: NPV does not change sign on [{lo}, {hi}] "
            f"(NPV(lo)={f_lo:.4g}, NPV(hi)={f_hi:.4g}); "
            "cashflows likely lack both an inflow and an outflow"
        )
    return brentq(lambda r: xnpv(r, cashflows), lo, hi, maxiter=500)


RBA_G1_URL = "https://www.rba.gov.au/statistics/tables/csv/g1-data.csv"
RBA_F2_URL = "https://www.rba.gov.au/statistics/tables/csv/f2-data.csv"
FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}"

AU_BOND_SERIES = {
    2: "FCMYGBAG2D",
    3: "FCMYGBAG3D",
    5: "FCMYGBAG5D",
    10: "FCMYGBAG10D",
}
US_BOND_SERIES = {
    1: "DGS1",
    2: "DGS2",
    3: "DGS3",
    5: "DGS5",
    7: "DGS7",
    10: "DGS10",
    20: "DGS20",
    30: "DGS30",
}


def _fetch_rba_csv(url: str, series_id: str, date_format: str) -> pd.Series:
    with urllib.request.urlopen(url, timeout=30) as resp:
        raw = resp.read().decode("utf-8-sig")
    lines = raw.splitlines()
    header_idx = next(
        i
        for i, line in enumerate(lines)
        if line.lstrip().lower().startswith("series id")
    )
    df = pd.read_csv(io.StringIO(raw), skiprows=header_idx, header=0)
    if series_id not in df.columns:
        raise RuntimeError(
            f"series {series_id!r} not present in {url} "
            f"(have {list(df.columns)[:6]}...)"
        )
    date_col = df.columns[0]
    df[date_col] = pd.to_datetime(df[date_col], errors="coerce", format=date_format)
    df = df.dropna(subset=[date_col]).set_index(date_col).sort_index()
    s = pd.to_numeric(df[series_id], errors="coerce").dropna()
    if s.empty:
        raise RuntimeError(
            f"series {series_id!r} from {url} parsed to empty "
            f"(date format {date_format!r} may not match)"
        )
    s.index = s.index.date
    return s


def _fetch_fred_csv(series_id: str) -> pd.Series:
    with urllib.request.urlopen(FRED_CSV_URL.format(sid=series_id), timeout=30) as resp:
        raw = resp.read().decode()
    df = pd.read_csv(io.StringIO(raw))
    date_col, val_col = df.columns[0], df.columns[1]
    df[date_col] = pd.to_datetime(df[date_col])
    df = df.set_index(date_col).sort_index()
    s = pd.to_numeric(df[val_col], errors="coerce").dropna()
    s.index = s.index.date
    return s


def fetch_cpi(series_id: str = "GCPIAG") -> pd.Series:
    """Fetch Australian quarterly CPI index from the RBA's Table G1."""
    return _fetch_rba_csv(RBA_G1_URL, series_id, "%d/%m/%Y")


def fetch_risk_free_yields(country: str, tenor: int) -> pd.Series:
    """Fetch daily annualised bond yields (in %) for the requested country/tenor."""
    country = country.lower()
    if country == "au":
        if tenor not in AU_BOND_SERIES:
            raise ValueError(
                f"AU tenor {tenor}y unsupported; choose from {sorted(AU_BOND_SERIES)}"
            )
        return _fetch_rba_csv(RBA_F2_URL, AU_BOND_SERIES[tenor], "%d-%b-%Y")
    if country == "us":
        if tenor not in US_BOND_SERIES:
            raise ValueError(
                f"US tenor {tenor}y unsupported; choose from {sorted(US_BOND_SERIES)}"
            )
        return _fetch_fred_csv(US_BOND_SERIES[tenor])
    raise ValueError(f"unknown country {country!r}; expected 'au' or 'us'")


def build_rf_index(yields: pd.Series) -> pd.Series:
    """Compound a daily annualised-yield series into a numéraire index.

    Between consecutive observations, the index accrues at the preceding
    yield using actual/365 daycount: idx(d_i) = idx(d_{i-1}) * (1 + y_{i-1} * Δ/365).
    """
    yields = yields.sort_index()
    dates = list(yields.index)
    if len(dates) < 2:
        raise RuntimeError("risk-free yield series has too few observations")
    vals = [1.0]
    for i in range(1, len(dates)):
        dt_days = (dates[i] - dates[i - 1]).days
        y = float(yields.iloc[i - 1]) / 100.0
        vals.append(vals[-1] * (1.0 + y * dt_days / 365.0))
    return pd.Series(vals, index=dates)


def series_at(s: pd.Series, d: date) -> float:
    earlier = s[[i for i in s.index if i <= d]]
    if earlier.empty:
        return float(s.iloc[0])
    if d > s.index[-1]:
        gap = (d - s.index[-1]).days
        print(
            f"warning: series_at({d}) extrapolates past last observation "
            f"{s.index[-1]} by {gap} day(s); using stale value",
            file=sys.stderr,
        )
    return float(earlier.iloc[-1])


@dataclass
class TickerResult:
    ticker: str
    units: float
    last_date: date
    last_price: float
    invested: float
    distributions: float
    reinvested_units: float
    final_value: float
    cashflows: list[tuple[date, float]]


def build_ticker_cashflows(
    ticker: str, purchases: list[Purchase], as_of: date, reinvest: bool
) -> TickerResult:
    purchases = sorted(purchases, key=lambda p: p.on)
    first = purchases[0].on

    hist = yf.Ticker(ticker).history(
        start=first.isoformat(),
        end=(as_of + pd.Timedelta(days=1)).isoformat(),
        auto_adjust=False,
    )
    if hist.empty:
        raise RuntimeError(f"no price data for {ticker}")
    hist.index = pd.to_datetime(hist.index).date
    prices, dividends = hist["Close"], hist["Dividends"]

    def price_on_or_after(d: date) -> tuple[date, float]:
        for i in prices.index:
            if i >= d:
                return i, float(prices[i])
        raise RuntimeError(f"no price on or after {d} for {ticker}")

    events: list[tuple[date, str, float, float]] = []
    for p in purchases:
        trade_date, px = price_on_or_after(p.on)
        events.append((trade_date, "buy", p.units, px))
    for d, v in dividends[dividends > 0].items():
        events.append((d, "div", float(v), 0.0))
    events.sort(key=lambda x: (x[0], 0 if x[1] == "div" else 1))

    cashflows: list[tuple[date, float]] = []
    units_held = 0.0
    distributions = 0.0
    reinvested_units = 0.0
    for d, kind, amount, px in events:
        if kind == "buy":
            cashflows.append((d, -amount * px))
            units_held += amount
        else:
            if units_held <= 0:
                continue
            cash = units_held * amount
            distributions += cash
            if reinvest:
                _, drp_px = price_on_or_after(d)
                if drp_px <= 0:
                    raise RuntimeError(
                        f"{ticker}: cannot reinvest dividend on {d}; "
                        f"ex-date close is {drp_px}"
                    )
                new_units = cash / drp_px
                units_held += new_units
                reinvested_units += new_units
            else:
                cashflows.append((d, cash))

    last_date = prices.index[-1]
    last_price = float(prices.iloc[-1])
    final_value = units_held * last_price
    cashflows.append((last_date, final_value))

    invested = -sum(cf for _, cf in cashflows if cf < 0)

    return TickerResult(
        ticker=ticker,
        units=units_held,
        last_date=last_date,
        last_price=last_price,
        invested=invested,
        distributions=distributions,
        reinvested_units=reinvested_units,
        final_value=final_value,
        cashflows=cashflows,
    )


def deflate(
    cashflows: list[tuple[date, float]],
    numeraire: pd.Series,
    target: date,
) -> list[tuple[date, float]]:
    if numeraire.empty:
        raise RuntimeError("deflate: numéraire series is empty")
    first = numeraire.index[0]
    early = [d for d, _ in cashflows if d < first]
    if early:
        raise RuntimeError(
            f"deflate: {len(early)} cashflow(s) predate deflator series start "
            f"{first} (earliest: {min(early)}); cannot compute adjustment"
        )
    target_val = series_at(numeraire, target)
    return [(d, cf * target_val / series_at(numeraire, d)) for d, cf in cashflows]


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--ticker",
        default="VGS.AX",
        help="default ticker for --purchase / CSV rows that omit one (default: VGS.AX)",
    )
    ap.add_argument(
        "--purchase",
        action="append",
        default=[],
        metavar="[TICKER:]DATE:UNITS",
        help="a purchase, e.g. VGS.AX:2021-03-15:120 (repeatable). "
        "If TICKER is omitted, --ticker is used.",
    )
    ap.add_argument(
        "--purchases",
        type=Path,
        metavar="FILE",
        help="CSV with rows 'ticker,date,units' (or 'date,units' if --ticker set). "
        "Header optional.",
    )
    ap.add_argument(
        "--as-of",
        type=lambda s: datetime.strptime(s, "%Y-%m-%d").date(),
        default=date.today(),
        help="valuation date (default: today)",
    )
    ap.add_argument(
        "--cpi-series",
        default="GCPIAG",
        help="RBA Table G1 series id (default: GCPIAG — All Groups CPI). "
        "Ignored when --risk-free is set.",
    )
    ap.add_argument(
        "--risk-free",
        choices=["au", "us"],
        default=None,
        help="deflate using a sovereign bond yield instead of CPI. "
        "'au' = RBA Table F2, 'us' = FRED DGS series. "
        "Output becomes excess return over the chosen rate rather than real return.",
    )
    ap.add_argument(
        "--risk-free-tenor",
        type=int,
        default=10,
        metavar="YEARS",
        help=(
            "tenor in years for --risk-free (default: 10). "
            f"AU supports {sorted(AU_BOND_SERIES)}, US supports {sorted(US_BOND_SERIES)}."
        ),
    )
    ap.add_argument(
        "--reinvest",
        action="store_true",
        help="treat distributions as reinvested at the ex-date close, "
        "adding units rather than generating a cash inflow (applies to all tickers)",
    )
    args = ap.parse_args()

    purchases: list[Purchase] = []
    for s in args.purchase:
        try:
            purchases.append(parse_purchase_arg(s, args.ticker))
        except ValueError as e:
            ap.error(str(e))
    if args.purchases:
        try:
            purchases.extend(load_purchases_csv(args.purchases, args.ticker))
        except ValueError as e:
            ap.error(str(e))
    if not purchases:
        ap.error("no purchases provided; use --purchase or --purchases")

    grouped: dict[str, list[Purchase]] = defaultdict(list)
    for p in purchases:
        grouped[p.ticker].append(p)

    results = [
        build_ticker_cashflows(ticker, ps, args.as_of, args.reinvest)
        for ticker, ps in sorted(grouped.items())
    ]

    portfolio_cashflows: list[tuple[date, float]] = []
    for r in results:
        portfolio_cashflows.extend(r.cashflows)

    total_invested = sum(r.invested for r in results)
    total_distributions = sum(r.distributions for r in results)
    total_value = sum(r.final_value for r in results)
    valuation_date = max(r.last_date for r in results)

    nominal = xirr(portfolio_cashflows)

    if args.risk_free:
        yields = fetch_risk_free_yields(args.risk_free, args.risk_free_tenor)
        numeraire = build_rf_index(yields)
        country = {"au": "AU", "us": "US"}[args.risk_free]
        deflator_label = f"{country} {args.risk_free_tenor}y govt bond"
        return_label = f"Excess return vs {deflator_label}"
    else:
        numeraire = fetch_cpi(series_id=args.cpi_series)
        deflator_label = f"AU CPI ({args.cpi_series})"
        return_label = "Real return"

    adjusted = xirr(deflate(portfolio_cashflows, numeraire, valuation_date))

    for r in results:
        print(f"[{r.ticker}]")
        print(f"  Units:             {r.units:,.4f}")
        print(f"  Last price:        {r.last_price:,.4f}  ({r.last_date})")
        print(f"  Invested:          {r.invested:,.2f}")
        print(f"  Distributions:     {r.distributions:,.2f}")
        if args.reinvest:
            print(f"  Units from DRP:    {r.reinvested_units:,.4f}")
        print(f"  Current value:     {r.final_value:,.2f}")

    print("[portfolio]")
    print(f"  Valuation date:    {valuation_date}")
    print(f"  Distributions:     {'reinvested' if args.reinvest else 'as cash'}")
    print(f"  Deflator:          {deflator_label}")
    print(f"  Total invested:    {total_invested:,.2f}")
    print(f"  Distributions:     {total_distributions:,.2f}")
    print(f"  Current value:     {total_value:,.2f}")
    print(f"  Nominal return:    {nominal * 100:.2f}% p.a.")
    print(f"  {return_label + ':':<18} {adjusted * 100:.2f}% p.a.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
