# Changelog

## Unreleased — 2026-11-05

- Added owner-only local PKCS#12 certificate import, SDK certificate permission checks, and publication-audit coverage for ignored versus force-tracked credentials.
- Integrated free TWSE material disclosures and annual calendar candidates, the current TDCC OpenAPI ownership snapshot, and bounded GDELT GKG archive acquisition with explicit metadata-only coverage.
- Added a keyless acquisition plan, document/version exports and numeric ownership events; unchanged observations retain their first receipt while corrections and reversions survive.
- Added TDCC/GDELT web source cards and a dynamic provider count, plus current pricing, licensing and manual-setup guidance. Unverified annual calendars cannot be activated directly.
- Added repeatable, credential-safe `sources verify` live checks with immutable local evidence and partial entitlement reporting.
- Fixed FRED vintage-window limits and future-dated observations, Twinkle UTF-8 SSE/202 responses, official TAIFEX CSV, and non-JSON HTTP entitlement errors.
- Limited FMP financial snapshots to five records by default, enabled explicitly annual estimates, and paced Fugle requests to its historical-data quota.
- Fixed ETF suspensions in the market-session calendar, stock-label maturity and benchmark simulation; missing passive valuations withhold daily risk metrics.
- Validated real multi-source integration locally, prepared an isolated compatible Fubon SDK environment, and documented remaining data/manual setup and open-source options.

## 0.2.0 — 2026-11-05

- Added a responsive Traditional Chinese research workspace with overview, stock forecasts, backtests, data sources, workflow status and project gaps.
- Added server-allowlisted workspaces, price history, research-run history and bounded local workflow actions protected by a session nonce and same-origin checks.
- Added a synthetic-only static web export for hosted previews; private keys, real provider data, raw files and model binaries are excluded.
- Prepared GitHub test/security workflows, dependency-update PR configuration, contribution guidance and persistent update rules.
- Published the canonical GitHub repository and private synthetic preview; verified Python 3.11/3.14 CI, frontend checks and full-history secret scanning.

## 0.1.0 — 2026-11-04

- Implemented immutable data snapshots, point-in-time features, pooled baseline/boosting models, 1/5/20-session forecasts and temporal calibration.
- Added purged walk-forward research, sealed holdout evaluation, costs, valuation methods and immutable prediction/maturity tracking.
- Added FinMind, FRED, FMP, News API, Twinkle, Fugle and read-only Fubon adapters, plus public TWSE/TAIFEX snapshots.
- Added credential boundaries, private-file protections, output/log redaction, publication auditing and regression tests.
