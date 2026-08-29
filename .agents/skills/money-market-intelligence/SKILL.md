---
name: money-market-intelligence
description: Query and interpret the Money Market Intelligence project's cached Korean and global financial/economic data through its MCP or pi tools. Use when answering about collected quotes, global indices, KRX instruments, economic indicators or events, correlations, spillovers, yield curves, technical/risk metrics, market regimes, data freshness, or the project's market-data coverage.
---

# Money Market Intelligence

Use this project's SQLite-backed tools as evidence. They are cache readers, not live market terminals.

## Choose the interface

- Prefer MCP tools when available. They read SQLite directly and do not require the web server.
- Use the pi tools when operating inside pi. They call the local REST server at `http://localhost:8077`, so the server must be running.
- Do not call ECOS, FRED, KRX, or Yahoo directly to answer a normal data question.
- Do not run a collector unless the user explicitly asks to refresh data.

See [references/tool-guide.md](references/tool-guide.md) for the canonical tool map and input conventions.

## Follow this workflow

1. If freshness matters, a result is empty, sources disagree, or the user asks for a broad market assessment, call `market_health` first.
   - Inspect `coverage.core` and `coverage.core_ready_pct`; database integrity alone does not mean the evidence set is ready.
   - Use `coverage.by_analysis_group` to name a weak or missing evidence layer instead of silently omitting it.
   - `completeness.status` is `incomplete` when observations are known to be missing. Call `market_coverage` before treating any absence as a market fact, and `market_coverage` with a `key` when one series looks wrong.
2. Discover identifiers instead of guessing:
   - Use `market_indicator_list` before `market_indicator` when the indicator key is uncertain.
   - Use `market_indices` to find an allowed index symbol or exact display name.
   - Use `market_universe` for provider-discovered KRX instruments; this is broader than the quote watchlist.
   - Use `market_datasets` before `market_daily` when the KRX table name is uncertain. `market_daily` needs an explicit dataset because the option table alone stores about 9,000 rows per session.
3. For "what is going on right now", call `market_situation` once instead of assembling the same picture from a dozen single-series calls. It returns both regime verdicts (`regime` for the US, `korea_regime` for Korea), core policy/funding/risk levels, derived spreads and liquidity, this week's high-impact releases, and a freshness line — each with its own observation date.
4. When the question is "what should I be looking at" rather than "what is X", call `market_brief`. It surfaces the contradictions the composite scores average away, the past week's distribution moves, and `flip_conditions` — which single component would have to change for the regime verdict to change. `flip_conditions` is arithmetic on the votes already cast, not a forecast and never a recommendation; quote it as "what would change the reading", never as "what to do". Report `unresolved` alongside it: evidence that did not vote must not be read as a neutral opinion.
5. For risk appetite specifically, `market_sentiment` scores it 0-100 from collected inputs and shows each component. Report where components disagree rather than only the composite.
6. Call the narrowest tool that answers the question. Do not combine every analysis by default.
   - For a broad market assessment, choose representative `core` series from the needed layers: `policy_rates`, `liquidity`, `credit_stress`, `growth_cycle`, `fx_external`, and `market_breadth`.
   - Do not fetch every indicator. Prefer one primary series plus at most one cross-check per layer unless the user asks for a deep dive.
   - Use `market_derived_metrics` for aligned transformations and cross-asset relative strength; do not duplicate its unit conversions ad hoc.
   - Use `market_breadth` for cached KRX advance/decline and concentration data. An unavailable result usually means dataset approval is still pending.
7. In the answer, state the market observation date, source, and whether the result is cached when those fields are relevant.
8. If data is missing, stale, partial, or errored, report that limitation. Do not interpolate, invent a current value, or silently replace it with web data.
   - Say which kind of absence it is: `confirmed` means the provider has it and we failed to collect it; `candidate` means the series' cadence implies it but it may never have been published. That distinction is itself evidence.

## Distinguish dates

- `date`, `latest_date`, `observation_date`, and analysis `as_of` identify the market/economic observation.
- `updated`, `updated_at`, and `retrieved_at` identify collection or retrieval time.
- Never present a retrieval timestamp as the date when the market value occurred.
- Mixed-frequency economic indicators can have different valid latest dates. Compare aligned dates where the tool does so, and disclose mismatches otherwise.
- Observation dates are each market's own local business date, so two markets sharing a date traded on their own sessions rather than simultaneously. Daily cross-market lead-lag therefore carries a timing artifact of up to one session.

## Apply interpretation guardrails

- `us_rate` is the monthly effective federal funds rate, not the FOMC target range.
- A name containing `(proxy)` is a tradable proxy, not the underlying index. In particular, `TOPIX ETF (proxy)` is not TOPIX itself.
- Indicator discovery also returns `proxy`; preserve that label for RSP/SMH/HYG/TLT/LQD/KRE/XLY/XLP and never describe an ETF price as an official index, credit spread, or Treasury yield.
- Series whose `source` is `krx` — the three `kr_put_call_*` ratios, `kr_vkospi`, and the seven promoted from exchange tables in M6.3 (`kr_breakeven_10y`, `kr_treasury_20y`, `kr_bond_duration`, `kr_kospi200_futures_oi`, `kr_kospi200_basis`, `kr_etf_discount`, `kr_gold_price`) — are derived by the KRX collector from bulk tables rather than fetched per key. They are ordinary indicator series to read; only their provenance differs.
- `market_indicator` also returns `explanation`: written layers (`what`/`why`/`how`/`caveat`), a machine-followable `watch` list of related catalogue keys, and a generated `now` sentence placing today's value in the distribution. `fallback: true` means the series has no explanation of its own and you are seeing the category stand-in — say so rather than presenting it as this series' explanation. Never quote `now` as a recommendation; it states placement, not action.
- `source_url` identifies the provider series page. `latest_date` is the observation date and `retrieved_at` is collection time.
- `position` on `market_indicator` places the latest value in the series' own distribution. It is a description of where this value sits relative to what this series usually does — never a signal, a forecast, or a threshold breach. Quote `percentile` with its `window_label`, because the window is part of the claim. `risk_percentile` is oriented so 100 always reads as risk-on, whichever direction the series was declared with — `up_is_risk` inverts the percentile, `up_is_support` uses it directly. When it is null the series is declared `neutral`, meaning a rise is deliberately neither tightening nor easing; do not supply a risk reading of your own for those. An `available: false` position carries its reason — report the reason rather than treating the absence as a neutral value, and honour any `caveat`.
- Correlation and lead-lag results are descriptive sample statistics. They do not establish prediction or causality, and trading-session timing can affect lags.
- Spillover uses generalized FEVD / Diebold-Yilmaz connectedness. Directional shock contribution is not structural causality.
- Yield-curve inversion is one signal, not a standalone recession forecast or timing model.
- RSI, MACD, Bollinger bands, moving-average crosses, and regime classification are heuristic or lagging descriptions, not trade instructions.
- `market_regime` returns two verdicts. The top-level one reads US inputs only (VIX, US IG spread, S&P 200-day average); `korea_regime` reads Korean inputs and scores each against its own distribution rather than an absolute cut. Never quote the US verdict as the state of the Korean market. When the two disagree, report both and say which inputs drove each — the disagreement is evidence about decoupling, not a defect.
- `korea_regime` states `component_count` / `component_total` and lists `pending` components whose history is too short to normalise. Quote the count with the verdict, and never treat a pending component as neutral — it did not vote.
- Historical VaR is a loss quantile, not the maximum possible or maximum expected loss. Report expected shortfall separately when available.
- Sharpe, volatility, drawdown, VaR, regime, and technical results depend on the requested sample period.
- Yahoo-backed values can be delayed or unavailable; do not call them official exchange data.

## Compose the answer

Lead with the direct finding, then attach compact evidence:

```text
결론: …
근거: 값/분석 … (관측일 YYYY-MM-DD, 출처 …, 캐시)
한계: 표본·최신성·방법론상 주의 …
```

Do not add an investment recommendation unless the user explicitly asks for decision support. Even then, frame it as scenario analysis with uncertainty, not a guaranteed action.
