# Tool guide

MCP and pi expose the same 22 canonical capabilities. MCP reads SQLite
directly; pi calls the matching cache-only REST endpoint.

| Tool | Use it for | Important inputs |
|------|------------|------------------|
| `market_health` | Database integrity, last collection, partial/error collectors, overall/core analysis coverage, completeness verdict | none |
| `market_coverage` | Which observations are missing and whether the collector can still recover them | optional `key`: an indicator key or index symbol |
| `market_brief` | What moved in the past week, what disagrees, and which single component would have to change for the regime verdict to change. Use it for "what should I be looking at"; `flip_conditions` is arithmetic on votes already cast, never advice | none |
| `market_situation` | The whole front-page state in one call: regime, core levels, derived risk, this week's high-impact releases, freshness | none |
| `market_sentiment` | Korean risk-appetite score 0-100 with per-component detail and what could not be measured | none |
| `market_events` | Upcoming official economic events in KST | `days` 1–365, optional ISO 2-letter `country` |
| `market_quotes` | Curated watchlist's last collected quotes | optional `category` |
| `market_indices` | Allowed global indices, symbols, latest observations | none |
| `market_indicator_list` | Discover indicator keys, analysis group, core priority, proxy/source metadata, and latest/retrieval dates | optional exact category |
| `market_indicator` | One indicator's cached time series, plus `position` (where the latest value sits in its own distribution) and `explanation` (written layers, related keys, and a generated reading of today's value) | exact `key`; `limit` 1–1000 |
| `market_universe` | Search provider-discovered KRX instruments and datasets | `query` (pi: `q`), `source`, `dataset`, `asset_type` (pi: `assetType`), `limit` |
| `market_datasets` | Which KRX daily tables are cached, and how much of each | none |
| `market_daily` | Cached daily rows for one KRX table: options, futures, ETFs, bonds | `dataset` required; `symbol`, `date`, `limit` 1-500 |
| `market_correlation` | Rolling and lead-lag correlation of two allowed indices | exact display names `a`, `b`; `window` 20–252; `max_lag` 0–20 (pi: `maxLag`) |
| `market_spillover` | Generalized-FEVD connectedness across cached indices | optional exact `region`; `maxlags` 1–10; `horizon` 1–50 |
| `market_yield_curve` | Aligned US or Korean term spread | `country`: `us` or `kr` |
| `market_index_analysis` | Trend, realized volatility, and maximum drawdown | allowed index `symbol`; `years` 1–20 |
| `market_technical` | RSI, MACD, Bollinger, and trend description | allowed index `symbol`; `years` 1–20 |
| `market_risk` | Sharpe, historical VaR/ES, and maximum drawdown | allowed index `symbol`; `years` 1–20 |
| `market_regime` | Two rule-based regime readings. Top level is the US one (VIX/credit/S&P); `korea_regime` is the Korean one, scored by percentile against each input's own distribution | none |
| `market_derived_metrics` | Aligned macro transformations and 20/60-day cross-asset relative strength | none |
| `market_breadth` | KOSPI/KOSDAQ advance-decline, turnover, concentration, and bounded 20-day breadth | none |

## Identifier rules

- Indicator tools use catalog keys such as `kr_base_rate`, `us_cpi`, or `us_10y`.
- Broad analysis should discover `priority=core` representatives by `analysis_group`; do not request all catalog entries by default.
- `proxy=true` means the series is a tradable ETF price used only as a directional cross-check.
- Correlation tools use exact display names such as `코스피` or `S&P 500`.
- Index analysis tools use Yahoo symbols from `market_indices`, such as `^KS11` or `^GSPC`.
- The KRX universe can contain multiple datasets for a symbol. Preserve `source`, `dataset`, and `asset_type` when identifying a row.

## Reaching the KRX tables

Three tools, narrowing in order:

1. `market_datasets` — what tables exist in cache and how much of each. Start
   here when you do not know the dataset name.
2. `market_universe` — find an instrument by name or code across tables.
3. `market_daily` — read prices for one table. `dataset` is required because
   the option table alone stores about 9,000 rows per session; an unfiltered
   read is neither useful nor affordable. Narrow with `symbol` or `date`.

Option and futures rows carry `metadata.right` (CALL/PUT),
`metadata.implied_volatility`, and `metadata.open_interest`. The market-wide
put/call ratios derived from them are indicator series
(`kr_put_call_volume`, `_value`, `_open_interest`) reachable through
`market_indicator` — prefer those over re-aggregating the contract table.

## Reading the sentiment gauge

The gauge is this project's own composite, not a vendor index. Its value is
the disagreement between components, not the headline number:

- Quote `components` individually when they diverge. Breadth reading greed
  while credit reads fear means the rally has not been confirmed by the credit
  market — that is the finding, and the average hides it.
- `pending` lists components that could not be measured and why. A gauge
  standing on four of seven components deserves a stated caveat.
- Do not compare the score to CNN's Fear & Greed. Different inputs, different
  thresholds, different market.

## Reading a coverage result

`market_coverage` classifies every gap by what is known about it, and the
three cases carry different obligations:

- `confirmed` — the provider has the observation and the cache does not. This
  is a real local deficit; the collector will retry it. Lower confidence and
  say so.
- `candidate` — the series' own publication cadence implies a date that is
  absent. It may never have been published (a suspended statistical release
  looks exactly like this). Do not assert the value exists.
- `unverifiable` — a traded series whose provider session list has not been
  captured yet. Absence of evidence here is not evidence of a gap.

`tail` describes the end of the series rather than its interior:
`fresh`/`stale` for indicators against their publication allowance, and
`current`/`behind_provider` for indices against the provider's last settled
session.

## Empty and error results

- Call `market_health` and report the relevant collector issue.
- A healthy database does not imply every series is fresh or populated.
- Report `coverage.core` gaps when they weaken a broad market conclusion; a low `core_ready_pct` lowers confidence even if collectors themselves are healthy.
- If KRX universe results are empty, check the `krx_market` collector: KRX requires per-dataset approval even though access is free.
- If an analysis reports insufficient observations, reduce scope only within the documented input range and only when that still answers the user's question.
