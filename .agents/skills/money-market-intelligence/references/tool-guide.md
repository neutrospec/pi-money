# Tool guide

MCP and pi expose the same 27 canonical capabilities. MCP reads SQLite
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
| `market_layers` | The five evidence layers — policy, growth, liquidity, credit, breadth — with per-layer confidence. Use it for "what part of the economy is saying what" rather than "what contradicts what" | none |
| `market_portfolio` | The owner's accounts and holdings, to be read beside the indicators. No net worth, no FX conversion, no return figures — see below | none |
| `market_backtest` | What a regime verdict actually meant over 655 replayed trading days. Read it before quoting a verdict as though it predicted anything | optional `market`: `korea` (default) or `us` |
| `market_replay` | The verdicts this repository could have produced on a past date. Use it to check whether a reading would have held at the time; never to claim it did | `date` (YYYY-MM-DD, KST); `mode`: `observed` (default) or `vintage` |
| `market_replay_readiness` | From which date each brief input can be replayed. Read this before quoting a vintage replay | none |

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

## Reading the five layers

`market_layers` cuts the catalogue by *what a series is evidence of* rather
than by what contradicts what. Two things about it are easy to misread:

- **The policy layer abstains, and that is not neutrality.** Every policy-rate
  and inflation series declares its direction as `neutral` on purpose: a rate
  rise is tightening or it is a recovering economy, and a percentile cannot
  tell which. That card reports level and weekly travel only. Never render its
  silence as "policy is neutral".
- **`split: true` is the finding.** A layer holding evidence that points both
  ways carries more information than its net score. Quote the split.

`confidence` never changes a verdict. It says how much of the expected
evidence reported and how much arrived past its own update cycle, counted
separately because the causes differ — one is a collector problem, the other
is a publication schedule nobody controls.

## Reading the portfolio

`market_portfolio` returns what the owner holds. Its shape encodes four
refusals, and quoting it means honouring them:

- **There is no net worth and you must not compute one.** Valuation comes back
  per `(grade, currency)`. The four grades — `market`, `stale`, `user_stated`,
  `unpriced` — must never be summed: adding a live price to a stale one to a
  figure the owner typed to a blank produces a number that means nothing and
  reads as wealth. Currencies are never combined either; converting them needs
  an FX rate whose observation date differs from both sides.
- **`weight_pct` is a share of that account's market-valued money**, not of
  everything the owner has. Always quote it with its account.
- **`is_risky_asset: null` is unknown, not safe.** Where a DC account holds
  one, `risky.decidable` is false and there is no share to quote — report
  `risky.reason` instead. Never count unknowns as safe, never drop them from a
  denominator.
- **`risky.share_pct` and `risky.limit_pct` are the end of the display.** Do
  not compute the difference between them; headroom is room to act, which is
  advice. Same for `conflicts`: a holding an account legally cannot contain is
  reported as a data-entry question, not as a trade.

No return figure exists anywhere in the payload and none may be derived —
`book_amount` is what the broker reported as an acquisition amount by an
unknown method, not a cost basis. Domestic price history runs about 23
sessions and foreign holdings have none, so past portfolio value cannot be
reconstructed at all.

`detail` says whether amounts and quantities are present. They are by default,
on the premise that the model reading this runs locally. MCP attaches to
whatever client launches it, so that premise belongs to whoever configured the
client; `MONEY_PORTFOLIO_AGENT_DETAIL=0` removes amounts if it stops holding.

There is no write tool. Entry goes through the browser behind a local-only
gate, and that will not change.

## Reading the backtest

`market_backtest` grades each replayed verdict against what the benchmark did
next. Three rules for quoting it:

- **Stratified before pooled.** `stratified` sits beside `contingency` and the
  gap between them is the finding, not a detail. Over 2007-2026 the pooled lift
  is +13.9 and the by-year figure is +3.4, by-quarter +0.7, by-half-year -1.0.
  Pooled lift credits the classifier for being switched on during dangerous
  years; stratified lift only credits picking dangerous days within a year.
  When they diverge, quote the stratified number.
- **Lift before precision.** Lift is precision minus the base rate — what the
  warning added over knowing nothing. Precision of 27% against a 13.5% base
  rate looks like skill and is not, once stratified. Never quote precision
  alone.
- **Episodes, not days.** `stratified.episodes` is the unit of evidence.
  625 warned days are 44 runs, only 12 of which contain a hit; outcome windows
  overlap 20 sessions, so any independent-day statistic is anti-conservative.
- **`conditional` is the threshold-free half** and usually says more. If the
  verdicts share a forward distribution, the classifier separates nothing and
  no choice of stress threshold rescues that.
- **Count episodes, not days.** Consecutive warning days are one event. A
  statistic built on 22 days that are really 7 episodes has the power of 7.

`recent_caveat` must be repeated whenever a live risk_off verdict is discussed:
since 2023-01-01 the warning has 0 hits in 41 warnings and a median forward
20-session return of +4.86% with no negative case. A reader looking at today's
brief cannot learn that anywhere else.

`out_of_window` reports `available: false` — the 2026-08-30 history backfill
extended the replay to 2007 and consumed the holdout it used to test against.
That is a stated limit, not a passed test.

`limits` travels with the result: look-ahead is controlled, revision leak is
not, and the thresholds are evaluated rather than tuned. A result suggesting
the 80/20 cuts are wrong is a finding for the owner to decide on — never
something to act on or to describe as the system having learned.

## Reading a point-in-time replay

`market_replay` runs the same verdict code against a past date. It answers
"would this reading have held at the time", and only within stated limits.

- `observed` filters by observation date. It blocks look-ahead, every table
  supports it, and it does **not** undo revisions — a value corrected later
  reads as though we always had the correction.
- `vintage` uses only values received by the end of that day, so it also
  catches revision. Only `indicator_vintages` supports it, and only from
  2026-08-23. Call `market_replay_readiness` first — it reports the date each
  series became replayable, and a series still short reports its components as
  pending rather than guessing. Every brief input became replayable on
  2026-08-29 through a deliberate backfill, which bought depth and not
  retroactive revision detection: until revisions accrue after that date,
  vintage mode returns what observed mode returns.
- Index prices, KRX bulk tables, and the catalogue's own contents cannot be
  reconstructed at all. The leak report lists them under `unchecked`, which
  means "not looked at", never "clean".
- `not_replayed` names the brief sections that read the live cache regardless
  of mode — sentiment and movers. Do not quote them as part of a past reading.

A replay standing on partial coverage is a partial verdict. `coverage.complete`
says whether it is, and a partial one must not be quoted as what the system
would have said.

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
