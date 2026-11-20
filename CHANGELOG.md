# Changelog

## Unreleased — 2026-11-20 — Opening research dashboard and forecast attribution

- Made the opening-direction task the main workbench. Added daily and weakest-five scores, source-to-validation flow, a separate capture-week view, and searchable results with expandable per-prediction factors. Kept the existing daily research workflow on its own page.
- Persisted ordered source contributions and observed inputs alongside every historical prediction. Added exact probability decomposition and source/receipt times to the scheduled weekly evaluation, plus a strict pre-cutoff auction-event export for later multi-source training.
- Replayed the long historical comparison with explanations. Retained the same 4,428/6,300 final constituent hits. The hosted snapshot generates its own fictional opening results and the local page reads private study and capture outputs.

## Unreleased — 2026-11-20 — Bounded VM opening-auction capture

- Stopped the legacy 10:00 VM pilot and scheduled read-only trial-auction acquisition for November 23–27. Preserve opening observations in private per-day SQLite archives with distinct exchange and receipt clocks, plus separately identified end-of-session recovery.
- Fit the fixed ten on the first four sessions and score the complete constituent grid on November 27. The closing job checks each available archive, keeps missing inputs and answers in the output, and writes a private result without publishing live forecasts.

## Unreleased — 2026-11-20 — Fixed preopen direction study

- Added an 08:59 opening-gap task with a fixed prior-close reference, strict event and receipt cutoffs, a full missing-aware constituent grid, maturity-purged training, and calibration on the immutable ten. Compare overnight, price and volume, multi-source linear and tree models. Score all five transfer instruments with the same selected model.
- Trained and verified eight retrospective folds and a final 126-session check. The selected model hit 4,428 of 6,300 constituent forecasts, with 57.30% for the weakest five. Public TAIFEX tick replay scored 76.11% overall across 19 overlapping sessions and 42.11% for the weakest five. Documented the independent live evidence and the trial-auction, news and morning data still needed for the 75% goal.
- Verified that the existing read-only Fugle account returned trial-auction samples before the exchange cutoff and opening prices for all 50 constituents in the November 20 same-day replay. The trial direction hit 47/50 and the constant not-up baseline hit 49/50. Original receipt times still require live capture.

## Unreleased — 2026-11-20 — Daily scoring and private paper-forecast results

- Assessed the five completed live sessions with full coverage penalties. The frozen rules scored 30/250 at 60 seconds and 36/250 at 300 seconds. November 20 sealed all 50 constituents about 0.53 seconds after the decision and scored 25/50 and 29/50. All 15 fallbacks had an empty side in the last causal book. Source freshness and shared-host stability still failed the operational gate.
- Score the sealed daily working database before history merge and save a private result with the original directions, fallback flags and separate horizon counts. Use the existing private Telegram watchdog to send results, retry unsuccessful deliveries and indicate pending evaluation after the deadline. Keep the immutable acceptance and frozen scoring protocol.
- The isolated real-stream rehearsal verified 105 subscriptions and published its 53 forecasts 6.286984 seconds after the test decision with shorter warm-up. A follow-up with the production warm-up and priority was deferred due to the shared service health check. Both outcomes remain separate from the official 10:00 scores.

## Unreleased — 2026-11-19 — Source freshness, bounded tail capture and lighter ingestion

- Preserve November 19's failed acceptance: 1,114,162 received/persisted events and 53 timely forecasts do not establish usable inputs. Source events were about five minutes old at 10:00 and all 53 forecasts used fallback; gaps, reminder health failures and 50-member denominators remain. Maintenance finished at 12:08 after three aggregate-timeout interruptions; no forecast or score is rewritten.
- Cache unchanged receipt-directory/watermark observations, including rotation invalidation on coarse timestamps. Encode only already validated receipt envelopes with pinned native JSON, retaining frozen payload/digest validation and exact large-integer fallback. All 1,114,162 actual event frames compared byte-identically on the VM. Four 20,000-event trials with 38 processed historical segments preserved identical projections; average CPU fell from 4.50455 to 2.9756 seconds and wall time from 5.3907 to 3.80605 seconds. These are offline VM results, not opening validation.
- Track event-time age separately from persistence backlog, sampled callback/transport timing and process CPU. Notify through the existing private channel on delayed source, degraded forecast inputs or stale source closeout; never discard pending frames or republish forecasts. Date-gate stronger operational acceptance and a maximum 90-second source-tail drain from November 20, retaining actual receipt times and only original-window event times. Preserve all frozen forecasting/scoring source and existing immutable acceptance files.
- Give serialized maintenance finite per-stage systemd notification deadlines, a lightweight schedule guard and interrupted-stage recovery. Observe reminder health during maintenance. Verify merged fields in one pass without weakening publication checks; restoring today's 766,017,536-byte archive and comparing the canonical 2,423,313-row store took 159.588 seconds for verification, preserving publications and read-only canonical access. Actual notification positive/control jobs confirmed extension and timeout behavior.
- Verified 809 Python tests, 10 frontend tests and the secrets audit. Deployed four helpers and five service definitions; the actual maintenance service exited successfully, all nine timers are active and the original Telegram container is healthy. Preserved 159 formal forecasts, three release seals and the frozen source hash. Confirmed delivery of the new degraded-input incident through the existing Telegram channel. Released only the verified restore fixture, leaving about 13.1 GiB free; original data and archives remain. No new paid resources, subscriptions or Supabase project were enabled. November 20 opening validation remains pending.

## Unreleased — 2026-11-18 — Serialized maintenance and recoverable storage

- Serialize cleanup, receipt verification/compression, daily indexing, daily backup/archive, atomic historical merge, sequential field/publication verification, frozen reporting, explicit WAL checkpoint and derived-workspace release under one resumable coordinator. Existing triggers share a lock; retain per-stage progress, three-attempt budgets and120second retry delays without restart loops. Protect08:38through post-capture drain, including the08:40probe and unchanged10:00decision.
- Defer full-day digest uniqueness and secondary indexes until sealing, preserving the first receipt ID. Reuse gzip streams across batches; retain every logical receipt and original timestamp. Verify raw, compressed and decompressed identities before replacing sealed raw files with replayable gzip links.
- Back up the complete daily store before historical merge, retain its verified archive, merge rows/indexes/publications atomically and checkpoint only after reporting. Cache reports by committed generation; release only reproducible daily databases after every gate passes. Preserve original failed/missing days, immutable acceptance and all frozen prediction source.
- Clean orphaned snapshot temporaries under the backup lock. Removed14abandoned snapshots totaling6464561152bytes from an earlier timed-out diagnostic, preserving latest backups and original data. Add separate live-core/maintenance fields and precise private delay notifications without relaxing total daily acceptance. Wait for every maintenance stage before final retirement, including recovery if marker creation is interrupted. Supabase remains disabled; no new resources, orders or subscriptions.
- Verified791Python and10frontend tests plus the secrets audit. On the existing640MiB VM slice, resumed the interrupted933087-receipt fixture and verified1309151canonical rows, original publications and frozen60/300second evaluation exactly against the formal store; restored the933193-row daily archive. Daily build/backup took331.991/290.056seconds, sequential verification298.884seconds and explicit checkpoint4.000seconds; cached merge timing is not a fresh full-merge benchmark. A fresh88154-receipt fixture also preserved88260rows and all53actual forecasts/seals, with equal evaluation and verified restore. Diagnostics earn no formal forecast credit; opening validation remains pending. Deployed four helpers and six units with matching source fingerprints, preserving the106formal forecasts, two publication seals and healthy original Telegram service.

## Unreleased — 2026-11-18 — Small live projections and deferred historical indexes

- Added date-gated private forecast projections while retaining every valid receipt in the CRC journal. Kept the latest eligible event-time ties, original receipt times, first-digest identities and stable IDs; the frozen shared forecast still emits all targets with the same cutoffs, fallback and publication seal.
- Removed scientific imports from the projection and deferred-history consumers. Provider construction/handshakes run separately from receipt synchronization, with bounded readiness/reconnect attempts. Kept atomic cursor/state commits and WAL/FULL; small projections checkpoint at128pages, historical writes/merges at16384pages outside acquisition.
- Rebuild full history in8192-record/8MiB batches, build secondary indexes after ingestion, copy actual forecasts/seals with conflict rejection and merge without reforecasting. Extended bounded replay service time to1800seconds and backup service time to1800seconds after the full snapshot check took about14minutes; retain failed/missing-day evidence and the existing daily acceptance deadline.
- Preload the cloud forecaster at09:57 while retaining the fixed10:00decision and raise preflight memory from256to384MiB after an observed diagnostic OOM; keep the shared640MiB research limit.
- Run backups inside the existing640MiB research slice, stop an existing report first, defer subsequent reports on the backup lock/service order and avoid spending report recovery attempts while a backup is active. Recovered the old large WAL and933087-receipt historical merge; the905695232-byte canonical backup passed its integrity check, archival completed in81.961seconds and the106prior publications/seals remained unchanged.
- Track post-decision receipt freshness independently of the small forecast view, fix inspection-time races in receipt-progress checks and keep reports/backups/archives pending until historical merge and the matching backup complete. Supabase remains disabled.
- Replayed933087actual November18receipts with106original context rows in private local fixtures:105projection rows, identical53forecast vectors,933193identical restored historical rows, identical60/300second labels and reports, preserved publication records and successful integrity checks. This is offline equivalence evidence, not a timely formal forecast or a VM performance claim. A three-times-paced VM replay completed with the same105-row hash,815792-byte peak WAL and25872KiB peak process RSS. The first live candidate published60.110623seconds late; after three-minute preloading and forecast CPUWeight1000, a separate88154-receipt live session published all53rows0.293697seconds after its fixed decision, with both60/300second windows covered without gaps; full history, backup, archive decompression checks and immutable publication comparison passed. Original Telegram health failures and the formal missing/failed day remain visible; the next opening is unverified.

## Unreleased — 2026-11-17 — Durable receipts, daily working stores and local archives

- Persisted frozen, allowlisted market messages to private32MiB receipt segments before indexed writes, with original timestamps, CRC checks, synchronized watermarks and conservative recovery of unclean tails. Removed the large RAM message queue; batches and receipt cursors commit together under WAL/FULL so replay remains idempotent.
- Separated each day's acquisition/forecast database from the historical store. Retained causal background and equal-timestamp revisions, used the unchanged shared model and actual release seals, and merged after capture with conflict rejection for existing predictions/seals. Fixed historical seeding scans using the event-time index; no training universe, target, denominator or deadline changed.
- Bounded indexed-consumer cache to64MiB within the existing640MiB slice, reserved publication writer time, limited consumer restarts and distinguished persisted backlog from RAM overflow while retaining the five-second lag gate. Deferred reports/backups until merge and retried due backups before10:30acceptance.
- Added bounded10:18replay and11:00local gzip archives with32MiB parts, source/part SHA256 checks and restore verification. Existing backup/protocol archives totaled67985431compressed bytes and verified exactly. Supabase remains disabled per the user's choice; no provider data was uploaded or deleted.
- Verified744Python tests,10frontend tests and secrets checks. Final isolated real-stream validation retained92294receipts/events with zero drops or remaining records,105subscriptions,53forecasts sealed0.049313seconds after the declared diagnostic decision, and separate complete60/300second observation windows. Earlier failed diagnostics and both failed formal sessions remain unchanged; midday validation does not guarantee opening-peak performance or75pctaccuracy.
- Deployed four helpers and eight changed/new units, verified nine active timers, disabled intraday checkpoints, preserved formal records/frozen source and confirmed the original Telegram service remains healthy. The bounded November18–27schedule continues; no paid resources or orders were enabled.

## Unreleased — 2026-11-17 — Live failure diagnosis and checkpoint suspension

- Inspected the actual session's journals, private acceptance/closeout receipts, storage and cgroup evidence. Found about20.96GiBfree disk, a44.339351second late publication, a100000message queue peak and775143dropped messages in the last saved worker heartbeat; these are incomplete operational counts, not exchange fill totals. Retained failed60/300second observation gates and all formal records.
- Confirmed repeated45second checkpoint timeouts and one150second collector stop timeout followed by systemd SIGKILL. Cumulative cgroup pressure/limit counters support resource contention but do not isolate today's contributions or prove OOM;oom_killremained zero. Disabled only the repeatedly timed-out checkpoint timer/service as containment, keeping the seven existing research timers and original Telegram service active.
- Verified376064events, identical full forecast hashes and both publication seals before/after suspension, with successful database quick checks. Existing backup and Telegram incident/daily-failure delivery receipts were confirmed. No WAL deletion, threshold relaxation, source/model changes or retroactive score repair occurred; live throughput remains unresolved.
- Evaluated official Supabase free quotas,50MBfile limits and private storage. Documented asynchronous compressed archives as a possible future integration, with local hot data, retry/integrity/restore checks and quota planning. No Supabase account changes, credential collection, uploads or paid resources were introduced.
- Re-ran68cloud operations Python tests,10frontend tests and the working-tree secrets audit. Updated operational documentation to distinguish prior preparation from the failed live session and the currently suspended eighth timer.

## Unreleased — 2026-11-17 — Market-session preparation and retained daily acceptance

- Cached reports by observed storage/protocol fingerprints, matured session and final-report status, checking that inputs remain unchanged during evaluation. Aligned supervision with this cache while preserving full capture/drain report deferral and required new-session/final evaluations.
- Pinned the Linux research backend to pysqlite3 0.6.2 and verified SQLite3.54.1 before enabling concurrent WAL maintenance. Added a64MiB-triggered PASSIVE checkpoint with a45second service limit, Nice19, the existing640MiB slice and09:57–10:06 connection exclusion. Retained WAL/FULL durability and explicit partial/interrupted states;128MiB journal reuse is not a hard WAL cap.
- Added read-only health observation of the original Telegram container and retained incidents until health is explicitly confirmed. From November17, persist immutable10:30daily operational checks covering publication, both target windows, gaps, closeout, backup/report, unresolved incidents and sampled flow/health/monitoring coverage; retry failed Telegram delivery without rewriting failed days.
- Deployed operations helpers and the eighth timer with private rollback copies, verified unchanged frozen source/protocol/formal forecasts/seals/events, and confirmed the existing reminder service remains healthy. Reused a successful actual sandbox notification receipt. Whole-VM failure monitoring remains excluded at the user's request; no paid resources, orders or provider-data publication were added.
- Verified713Python tests,10frontend tests and the working-tree secrets audit. Isolated cloud tests produced identical53forecast/evaluation fingerprints across old/new SQLite and retained8001synthetic rows through an82.7MB WAL checkpoint. Software and synthetic checks do not establish live one-second publication, shared-host stability or75pctaccuracy; November16failures remain in the audit.

## Unreleased — 2026-11-16 — Bounded collector closeout and missing operational alerts

- Reproduced accepted messages being left in RAM at capture end. Added bounded draining on natural completion and SIGTERM, original receipt timestamps, explicit incomplete counts/gaps and private append-only closeout receipts. The supervisor waits before terminating the cloud child, and systemd permits150seconds for stop; hard crashes and exhausted budgets remain failures.
- Deferred reports across the whole existing-host capture and drain window09:28–10:17:30, retaining the frozen model, publication limit, target horizons and declared dates. Future isolated rehearsals stop reports before capture and exclude report services/timers from final evaluation.
- Added fixed backlog, daily-gap, missing/incomplete-closeout and large-WAL alerts without discarding pending data through automatic restarts. Distinguished SQLite recovery/busy status and prevented unreadable or untrusted snapshots from falsely resolving existing incidents.
- Deployed only the changed operations helpers and units to the existing VM, verified unchanged formal protocol/predictions/seals/events, and preserved Telegram configuration. A real SIGTERM under the service account/sandbox drained954pending messages and durably retained all1000synthetic events with no predictions or broker authentication.
- Verified701Python tests,10frontend tests, secret publication checks and systemd syntax. Documented remaining live deadline/throughput validation, WAL maintenance, volatile queues, external supervision, off-disk recovery and provider/statistical evidence limits. Today's failures remain unchanged; synthetic checks do not establish live acceptance.

## Unreleased — 2026-11-16 — Full-session live operational validation

- Added a bounded, isolated real-stream rehearsal with45minutes of capture, the frozen53query model, separate60/300second checks, deliberate collector interruption, private incident/recovery notifications, backup verification and unconditional cleanup. Formal ten-session evidence remains separate.
- Captured428523events and53forecasts, but measured the11:25publication seal at1.125641seconds after the decision, beyond the unchanged1second limit. Retained the late result and delivered alert; recurring Telegram health-check timeouts and end-of-window backlog prevent a full-operation acceptance claim.
- Reproduced and fixed the validation tool's missing private writable path under its32MiB sandbox. Retained original authenticated systemd timing evidence, the failed receipt and retrieval timestamps; did not backdate observations or rewrite forecasts. Failed validation has its own retried notification and retires only its isolated monitor after delivery.
- Confirmed a286580736byte consistent backup after multi-minute WAL recovery. Missing or corrupt backups fail the validation gate without creating a replacement database. Reverted the unsuccessful memory-soft-limit experiment; left the original Telegram service, VM size, frozen source and formal publication records intact.
- Verified690Python tests,10frontend tests and the working-tree secrets audit. Operational timing and shared-host stability remain failed despite passing software tests; private data, runtime receipts and logs are excluded from Git.

## Unreleased — 2026-11-16 — Live-feed collection, scheduling and durability repairs

- Investigated first-session feed-not-ready and stalled-collector alerts with sanitized diagnostics and stack samples without locals. Confirmed pure-Python UTF-8 validation cost, the512MiBgroup repeatedly hitting its limit, commit/I/O stalls and an oversized20000message drain starving heartbeat updates. The SDK busy-wait hypothesis was not supported by the sampled stacks; its connection code was left unchanged.
- Enabled pinned wsaccel0.6.7 in a service-owned private directory and a single explicit loader file. Verified262synthetic byte cases against the original validator, preserving validation semantics, credentials and frozen prediction source; future Linux CPython deployments use a cloud extra.
- Added an explicitly versioned cloud acquisition runtime that yields after512messages or0.25seconds while retaining the original feed, allowlisted persistence, conservative gaps and bounded retries. Added research-venv SQLite tuning with an8MiBcache and deferred commit-time automatic checkpoints, preserving WAL/FULL durability and verifying committed recovery after abrupt process exit.
- Added a09:58timer that stops an already-running report and remains bounded to the declared quiet window. Raised the existing research group's limit to640MiB and lowered only collection/report CPU priority, preserving forecast priority, VM size and Telegram configuration. No paid resources, orders or provider-data publication were introduced.
- Retained the first day's53query forecasts, actual10:03:50publication seal, late penalties, gaps and exhausted retry history. Validated the repairs in a separate100second real-stream diagnostic with105subscriptions, timely heartbeats and persisted events; it emitted no predictions and was not merged into pilot scores. Original Telegram health recovered. Added regressions for deployment, quiet-window conditions, bounded memory, collector fairness and SQLite durability.
- Verified680Python tests,10frontend tests, generated-installer syntax, secret publication audit, matching deployed helper/unit fingerprints, database integrity and completed private backup. The first day's late outcome remains in both60/300second denominators.

## Unreleased — 2026-11-15 — Authorized cloud activation and private incident supervision

- Activated the existing-host ten-session pilot after explicit authorization to transfer only necessary Fubon credentials into owner-only private storage. Verified105/105read-only subscriptions both as the service user and inside the production service sandbox, with synchronized time and unchanged frozen prediction source.
- Added08:40premarket probes,30second supervision, private Telegram incident/recovery notifications and daily rolling SQLite backups. Recovery uses fixed service actions, daily retry budgets and cooldowns; timeouts, missing forecasts and late publication remain in the original denominator. Notifications retry after delivery failures and final delivery precedes monitor retirement.
- Preserved the09:30–10:15capture window,10:00full-grid forecasts and separate60/300second scoring. Enabled six cloud timers, disabled and unloaded only the three duplicate Mac jobs, and retained their original settings and private rollback copies. Existing Telegram reminders and VM networking were preserved; no new paid resources, orders or provider-data publication were enabled.
- Reproduced duplicate final-marker failure and made finalization idempotent. Verified isolated synthetic backup integrity, low-space preservation, low-privilege writes, non-trading service execution and an actual private notification. Python670and frontend10tests passed; first live-market capture remains pending November16.

## Unreleased — 2026-11-15 — Existing-host Linux compatibility and bounded collection

- Inspected the user-selected existing Google Cloud host and verified nativeSDK2.4.0loading,17core tests and a clearly synthetic10000row ingestion benchmark in an isolated Debian12/Python3.11environment. No broker authentication, secrets transfer, new cloud resources or existing-service changes were performed.
- Added explicit Python3.11/3.12/3.13 selection and a verified private pip-wheel bootstrap for systems without ensurepip, preserving the system Python and frozen prediction source.
- Added a small-host supervision profile with a09:30–10:15capture window, shared512MiB pilot slice,4GiB free-disk reserve and report deferral around forecasting. Full50stock coverage, targets, missing-answer penalties and model parameters remain intact; acquisition changes are separately declared.
- Added regressions for Python compatibility, storage-stop behavior, bounded process lifetime and memory-unit generation. Deployment support files receive individual fingerprints; cloud authentication and activation remain pending explicit credential-transfer authorization.
- Reproduced and corrected root-umask public-code access for the low-privilege service account. Installed the isolated application without credentials, verified systemd syntax and three non-trading-day service executions, and left every timer disabled. Full Python658and frontend10checks passed; the prediction source snapshot remains unchanged.

## Unreleased — 2026-11-14 — Credential-free cloud pilot preparation

- Added reproducible Linux deployment preparation from the frozen pilot source, public package metadata, pinned official2.4.0Linux SDK fingerprint and explicit-date systemd units. Bundles exclude credentials, private protocols, provider data, databases, model binaries and SDK logs.
- Preserved independent forecast preheating, collector/report retries, publication-time accounting and conditional finalization of only this pilot's services. The inactive installer verifies hashes and refuses to overwrite a deployment; no cloud resources, credentials or timers are changed by preparation.
- Verified package exclusion, source-integrity rejection and unit argument/calendar behavior with six regressions, and checked the generated installer syntax. Documented existing-cloud capacity, cost, credential authorization and target-host runtime validation requirements; local schedules remain intact.

## Unreleased — 2026-11-14 — Ten-session prospective market-data pilot

- Added a bounded, explicitly authorized local pilot for November16–27 with real Fubon trades/books/index observations, event and receipt timestamps, private SQLite persistence and fixed-ten synthetic-reference rule export. Verified all105 required read-only subscriptions; weekend access verification is not represented as collected future market data.
- Added independent prewarmed10:00 full-grid paper forecasts, immutable publication seals, missing-input prior fallbacks and separate60/300second observed-stream labels. Missing, late, timeout and gap-affected answers remain in the audit.
- Added per-day/per-stock reports, constant-direction controls, transfer queries, latency samples and100comparison conditional bounds. Clearly distinguish provider-stream evidence, unverified individual exchange-fill completeness and the statistical limits of ten daily observations per stock.
- Added three bounded macOS LaunchAgents, frozen private source snapshots and retirement of only this pilot's jobs after the final report. Preserved credentials, provider data and native SDK logs outside Git; no orders, paid subscriptions or provider-data publication were enabled.
- Verified17focused regressions,645Python tests and10frontend-state tests before installation; runtime and publication checks are recorded separately.

## Unreleased — 2026-11-14 — Reproducible open-source method experiments

- Inspected pinned GitHub revisions, actual implementation files, dependencies and licenses for fifteen forecasting, agent, backtest and data-platform projects; retained source-file hashes and explicit restrictions, including VectorBT's Commons Clause and separate model/data rights.
- Added an isolated postclose study with nine Alpha158-inspired factors, exact-rank pooled and volatility-scaled residual intervals, and sampled feature-prefix audits with a deliberately leaky test control. Reused existing fixed-ten models and maturity-purged folds; retained missing forecasts, unknown labels, transfer-only assets and the reserved final 126 sessions.
- Completed three experiments on the existing historical snapshot with eight folds per horizon and 48 model fits, including simple baselines and frozen-order slippage stress. Five-session factor Brier improved relative to the current model but remained worse than training frequency; one-session interval quality improved locally, while five-session intervals and strategy results did not improve. No model was promoted.
- Saved independent-run fingerprints, aggregate results, commands and limitations in the research record; kept provider inputs, detailed predictions and runtime artifacts local. Added ten behavior tests; full Python and frontend checks passed. No dependencies, frontend, schedules or trading capabilities changed.

## Unreleased — 2026-11-14 — Depth, activity and separate five-minute targets

- Completed36 frozen depth/persistence worlds and144candidate audits, retaining external thin-book, faster-activity, large-order and missing-input controls. Both selected and fixed bounce policies passed30sample and30joint conditional any-five gates; all thin-book failures remain visible.
- Added an offline coupled-clock runner with input/source fingerprints, one fixed-ten model per seed across every environment, full-grid audits and simultaneous any-five bounds. Completed180 paired60second transfers;96sample and90conditional lower gates passed, with all36slowest worlds failing.
- Added target-specific300second matching/replay and audit support while preserving the60second target. Reject extensions beyond the reference's observed future and verify that cross-version differences contain only the specific deadline guard changes.
- Completed72 separate300second transfers with100pctdaily constituent coverage:39sample any-five gates and36joint conditional lower gates passed. Independently verified all53query panels, fixed-ten refits,3600comparison bounds and1152direct matching replays; preserved every original60second prediction fingerprint.
- Added three frozen configurations, direct lower-rate replay and reference-integrity regressions, and separate depth/activity documentation. No provider connection, subscription, live-data schedule, trading or frontend deployment was enabled.

## Unreleased — 2026-11-14 — Persistent-flow stress and declared initial depth

- Completed36predeclared directional-persistence cases and144candidate audits at1millisecond, preserving all50daily grids, external high-activity interventions, large orders and rapid sign-flip controls. Both policies passed21sample any-five gates and6joint conditional lower gates; retained medium/strong persistence failures.
- Independently checked all fixed-ten refits/query panels,1400dictionary orders,96scalar price paths and10485760order signs. Distinguished hidden-direction alignment from adjacent-order persistence and finite exponential memory from the published LMF model.
- Evaluated eight causal fill-flow features only on fixed-ten training/calibration prefixes;240independent scalar histories and48fresh refits verified all12ablations. The features did not restore stable75pctin stronger flow and were kept out of production.
- Added bounded initial depth ranges and isolated external depth interventions, retaining default5to10initial quantities, finite-grid semantics, primary/index protections and constituent allowlisting. Initial ranges never force current depth or replenish depleted books.
- Preserved the earlier numerical source snapshot and documented default compatibility separately from future custom-depth performance. No provider connection, subscription, real schedule, trading or frontend publication was enabled.

## Unreleased — 2026-11-14 — Operational first-trade latency envelope

- Predeclared twelve operational scenarios with three fresh seeds and a fixed fast spread-bounce design candidate, retaining all four candidate audits and full daily constituent grids.
- Reused the frozen ordered-level matching implementation to examine1/5/20millisecond delays, larger orders, five external high-activity stocks,2pctcommon missing input and an intermediate event mix.
- Completed36experiments and144candidate audits with independent fixed-ten refits, all53query panels, full daily denominators,1400dictionary orders and96scalar paths. Both selected and fixed policies passed27complete sample gates and20joint any-five lower gates; retained every adverse latency and event-mix result.
- Derived any-five-average bounds from the predeclared3600comparison stock family, reporting the stricter minimum-stock diagnostic separately and quantifying a conservative additional-failure allowance.
- Reviewed post2020Taiwan depth/spread research and official trade/book feed schemas, distinguishing best-quote depth from five-level cumulative depth and timestamp precision from transport freshness.
- Documented the operating assumptions separately from historical or live evidence. No broker connection, subscription, schedule, trading or frontend publication was enabled.

## Unreleased — 2026-11-14 — Ordered-level matching and compact bounce inference

- Added signed finite-grid books with ordered partial fills, market and crossing/resting limit orders, IOC remainders, per-side quote updates and explicit depth-loss unknowns.
- Added a causal matching-process adapter with independent event clocks, actual emission anchors, individual-fill targets, variable spread and past-fill-normalized depth; price-grid expansion preserves untouched paths.
- Added an offline fixed-ten study command and a predeclared36-case comparison with all144candidate audits, retaining adverse activity, size, timeout and missing-input conditions.
- Independently checked1400dictionary orders,96scalar paths, all four primary-only refits, full53-target probability panels and both selected/fixed-policy audit families. Selected controls passed23sample gates and4conditional lower gates; fixed bounce passed22and5respectively.
- Added a prepared-array bounce interface that preserves twelve-decimal rounding, missing-input priors and every one of3816000legacy query probabilities. Batch50 inference measured11.833microseconds median and17.593microseconds p99, excluding data preparation, feed reception and publication.
- Documented reduced gains after removing simultaneous quote resets and the remaining distribution/freshness assumptions; no provider connection, subscription, live schedule, trading or frontend publication was enabled.

## Unreleased — 2026-11-13 — Transaction first changes and spread bounce

- Added the accepted transaction-price first-change target with a separate fitting/audit interface, emission-time last-trade anchors, same-price waiting and explicit unknown index answers.
- Reused queue event timing and full-grid accounting while preserving legacy midquote features, predictions and audit counts; retained best-quote-only execution and reset rules as declared synthetic assumptions.
- Added shared prior, spread-bounce, logistic and hist candidates with fixed-ten-only training and calibration, observable feature allowlisting and complete missing-input fallback predictions.
- Completed36 frozen experiments and144 candidate audits. Fifteen passed the conditional synthetic sample gate; retained all adverse latency, timeout, missing-input and external-activity outcomes alongside the mechanical baseline.
- Independently reconstructed scalar execution paths, all four primary-only refits and full daily audits; preserved numerical evidence across an unsupported-index diagnostic-anchor correction.
- Measured the existing50-stock DataFrame inference path and documented its millisecond overhead, official matching/feed differences and the remaining microsecond-design requirements. No provider acquisition, subscription, schedule or trading was enabled.

## Unreleased — 2026-11-13 — Post-emission discrete first-change research

- Added integer-queue first-midquote-change simulation with independent Poisson event clocks, configurable lot-size profiles and complete pre-emission evolution/reset handling.
- Added an event-history shared logistic candidate with explicit event units, observable feature allowlisting, fixed-ten-only selection and prior fallbacks; legacy candidates and numerical outputs remain intact.
- Completed36 frozen experiments and144 model audits, retaining full daily grids, missing inputs, timeouts and adverse external interventions. All36 failed75pct; uncalibrated Brownian depth assumptions cannot establish market feasibility by themselves.
- Independently checked scalar transitions, event clocks, primary-only refits, matched controls and legacy outputs; preserved numerical snapshots across a metadata-only timeout-label correction.
- Recorded acceptance of transaction-price first changes including spread bounce for separate research; reviewed official feed schemas without enabling acquisition, subscriptions, schedules or trading.

## Unreleased — 2026-11-13 — Source-weighted direction research

- Added shared price, US-market and night-session logistic experts with purged chronological inner weight fitting, convex regularized Brier fusion, equal-weight controls and explicit missing-source prior fallbacks.
- Exposed an opt-in five-candidate fusion search and recorded all fusion candidates' source weights, actual training dates and maturity diagnostics; bound cached recipes to the fusion implementation without changing default candidates or production models.
- Added a reproducible six-arm offline study with source ablations, immutable input/source fingerprints, independent complete-grid score checks and matched market-date block diagnostics; retained mixed historical results and failed full-coverage targets locally.
- Reviewed primary TFT, StockNet, Qlib, FinGPT and stacking implementations and official data documentation; documented phased dynamic-weight, announcement, volatility and prospective-validation work with explicit resource gates.
- Added a manual keyless official-announcement plan and completed a single local TWSE snapshot and readiness assessment, preserving actual receipt and the shortage of continuous historical evidence. No subscriptions, background schedules, orders or frontend publication were enabled.

## Unreleased — 2026-11-12 — Bounded first-change direction

- Recorded the user's acceptance of daily fixed-clock forecasts for the first price change within60seconds, retaining timeouts as incorrect and separating this target from clock endpoints.
- Added exact Brownian first-passage races, analytical finite-wait/missing-input bounds and fixed-ten-only shared imbalance classifiers without prescribing a probability of correctness.
- Added independent first-event timing/coverage accounting and explicit guards against feeding first-event labels into clock-endpoint fitting or audits.
- Completed42 frozen experiments and independently verified all126model audits, calibration choices, distribution-based labels, external isolation and actual fitted-policy bounds.
- Preserved the numerical protocol across later scope confirmation, checked source equivalence and exact fresh probabilities, and documented latency sensitivity and adverse depth/activity cases without claiming market75feasibility.

## Unreleased — 2026-11-12 — Privileged-state queue ceilings

- Extracted shared observed-history simulation while preserving original feature frames, endpoint labels and index behavior.
- Added an offline complete-state fork diagnostic with independent future streams, preserved date/stock grids and separate strict versus up/nonup accounting.
- Added Jensen/Hoeffding population upper bounds and independent pilot/evaluation policy lower bounds; branch replicas never count as extra market dates.
- Kept oracle diagnostics outside training, model selection, deployable forecasts and target-success gates, with frozen protocols and credential-free CLI execution.
- Completed15experiments,60horizon panels and3million conditional rows; independently verified scalar transitions, archived labels, complete grids and all simultaneous bounds.
- Ruled out75pct under adverse artificial laws even with complete hidden state, and inherited component bounds for previously fixed mixtures without treating upper bounds or flat-dominated baselines as forecast achievements.

## Unreleased — 2026-11-12 — Heterogeneous liquidity windows

- Added predeclared mixtures of causal liquidity-response coefficients and external-only distribution shifts, with hidden diagnostics excluded from predictor features.
- Added an opt-in shared observable-state hist candidate using relative depth and past price/imbalance interactions; original four-candidate defaults remain unchanged.
- Added explicit candidate declarations to frozen queue studies and a six-scenario, three-seed comparison, preserving full daily predictions, flat penalties and fixed-ten-only selection.
- Completed18 experiments and independently recomputed all108model audits, exact fixed-ten replays, scalar-world couplings, original baselines and external-distribution isolation.
- Retained adverse unseen-environment transfer and the new candidate's inconsistent gains; conditional synthetic success does not establish a market75expectation.

## Unreleased — 2026-11-12 — Causal local book inputs

- Added an offline canonical snapshot/checkpoint frame builder with a one-second input buffer, bounded as-of history and explicit unknown-label retention.
- Added a portable book feature contract invariant to price and quantity units, preserving fixed-ten-only fitting and index queries without fabricated books.
- Kept synthetic and unverified input evidence separate from source names and planned research cutoffs; no provider session or forecast schedule is enabled.
- Reviewed auction sources and documented the distinction between endpoint changes, official-opening direction and indicative-price corrections without changing the active target.

## Unreleased — 2026-11-12 — Queue regimes and liquidity response

- Added hidden daily-window regime mixtures and external-only frequency overrides, keeping weak windows and all daily forecasts in the audit.
- Added a causal liquidity-response counterfactual driven by already observed simulated order signs; its parameters remain uncalibrated.
- Added an optional pure-training historical-logistic reference outside candidate selection, plus environment metadata in frozen study protocols.
- Completed36 declared mixture/liquidity experiments and independently recomputed all180control audits; retained all weak windows, fallback days and adverse transfer outcomes.
- Verified exact legacy pure baselines, hidden-regime counterfactual couplings, external-frequency isolation and a fresh fixed-ten-only replay; documented conditional requirements without treating order-flow memory as price-prediction accuracy.

## Unreleased — 2026-11-12 — Causal queue-process experiments

- Added a finite-horizon, explicitly synthetic queue-depletion process with separate observed/future random streams, date-common feed outages and declared weak-transfer scenarios.
- Added shared fixed-ten prior, snapshot and historical-path classifiers with mature-label and receipt-clock guards; flat training labels and daily fallback forecasts are retained.
- Added an offline, credential-free study command that freezes its protocol before fitting, selects only on fixed-ten calibration and preserves every control's full50 results.
- Documented the simplified process, its uncalibrated parameters and the distinction between conditional simulation success and a credible market75expectation.
- Completed all60 declared comparisons and independently recomputed all240control audits; conditional persistent-flow success did not survive the five-stock weak-transfer intervention.
- Verified a fresh fixed-ten-only replay and all12external-strength intervention pairs; retained numerical source snapshots across a subsequent endpoint-maturity guard extension and exact-probability replay.

## Unreleased — 2026-11-12 — Intraday research scope

- Recorded authorization to research shorter intraday future intervals while retaining daily full coverage and the immutable training universe.
- Added a fixed-clock 1/5/30/60-second accounting interface for all 50 constituents, preserving missing rows, receipt/emission timing, shared model rules and weak stocks.
- Separated raw up/down credit from flat-price and up-versus-nonup diagnostics; supplied timestamps and passing sample counts cannot verify model training or raw quote provenance.
- Documented a five-level shared sequence design, primary-source benchmark limitations and conditional requirements for a 75% design; no market-data subscription, acquisition or training was enabled.

## Unreleased — 2026-11-12 — Shared similarity research

- Added a fixed-ten historical-state similarity classifier with training-only transforms, prior shrinkage, minimum distinct-date support and full daily fallbacks.
- Added a separate seven-candidate comparison with four matched controls and three declared radii, preserving the original targets, features, calendars and transfer roles.
- Reproduced and corrected arbitrary neighbor-cutoff ties, retaining the initial evidence and replaying the study under a new source fingerprint.
- Extended the primary-source feasibility review to Taiwan radius-neighbor, confidence-fusion and centroid-smoothing research without transferring incompatible accuracy claims.
- Completed three matched-control and three similarity comparisons, independently recomputed all audits and verified exact original-control scores and tie-fix replay probabilities; all full-coverage target gates remain unmet.
- Bound reference recipes to the imported similarity implementation and added a regression proving that changed dependency fingerprints reject cached model reuse.

## Unreleased — 2026-11-12 — Direction feasibility

- Reviewed primary GitHub implementations and research, separating daily full-coverage direction from ranking, event-time order-book targets, sentiment classification and filtered-window results.
- Designed a shared multimodal architecture with fixed-ten-only memory and one-way external queries, preserving daily predictions and the immutable training universe.
- Added an offline direction-feasibility command with Gaussian information requirements, feed/answer penalties, exact worst-five expectations, correlated-vote sensitivity and all declared simulation seeds.
- Kept conditional assumption success separate from empirical or supported unconditional75claims; the calculator reads no credentials, prices or reserved outcomes and never promotes a model.

## Unreleased — 2026-11-12

- Evaluated pinned MIT Trader Skill and Day Trader Skill sources and reproduced nonfinite-input risk/screener defects despite passing upstream tests.
- Adapted explained offline screening and five causal daily setup flags; independently implemented fail-closed paper target-weight, cash, exposure, volatility and declared-asset checks.
- Added opt-in matched A/B/C/D and independent rule experiments over existing snapshots, preserving all forecast rows, fixed-ten training, same-model transfer and sealed final dates.
- Extended the existing simulator with validated optional eligibility/weights and rule-only selection, verifying exact original baseline predictions and accounting.
- Completed historical comparisons without robust predictive gains; retained zero-trade filter/risk results, poor cost-adjusted breakout performance and failed daily full-coverage evidence.
- Added boundary, causality, transfer/holdout-isolation and CLI tests, source attribution and MIT notices in both source and wheel distributions; no data acquisition, live orders, schedules or production-model changes.

## Unreleased — 2026-11-08

- Added past-known own-stock US-sector response coefficients and current-window interactions with paired-observation minima, causal clock alignment and unchanged original feature profiles.
- Added a separate fifty-four-candidate response comparison with versioned contracts and fixed-ten-only shared-model selection.
- Added opt-in trusted-local reference pinning and reuse, requiring exact inputs, recipes, numerical environments, artifact hashes and selected-model probability replays before appending freshly fitted candidates.
- Completed four response comparisons with exact original48 calibration controls and a full fresh54-candidate fold replay; independently recomputed audits retain the unmet daily full-coverage target.
- Added own-stock financing and margin-short flow, position-change and capacity ratios with explicit21:00 receipt lags, daily expiry and observed-zero/missing-report distinctions.
- Added a separate forty-eight-candidate margin comparison preserving the original42 inputs, with receipt/evidence contracts in research provenance and model versions.
- Completed the delayed-margin comparisons and preregistered same-day sensitivity with exact original-candidate calibration controls; the full-coverage target remains unmet.
- Independently verified the historical session calendar against official monthly records and checked unresolved quotes without inventing prices or changing the frozen experiments.
- Added four shared opening-residual return heads combining the assumed gap with a training-only remaining-return distribution, including volatility-scaled scores and explicit missing-reference handling.
- Added a nine-candidate synthetic residual comparison preserving the original five opening candidates, the information assumptions and the full-coverage audit.
- Completed all eighteen residual comparisons with exact original-candidate calibrations and reconstructed selected-model training statistics; results remain below the target without consistent weak-stock gains.
- Added an isolated synthetic opening-reference study with independent keyed noise/missingness, forced synthetic evidence, unchanged historical feature profiles and versioned counterfactual assumptions.
- Added a five-candidate opening-proxy comparison and one-day configuration, preserving daily fallback predictions and fixed-ten-only selection.
- Completed all eighteen preregistered opening-proxy scenarios/seeds with exact calibration controls, full-calendar audits and paired diagnostics; even the ideal reference assumption remains below the target.
- Added own-stock foreign-main and investment-trust imbalance, activity and trailing means under an explicit market-session receipt lag, with daily expiry and missing/zero distinction.
- Added an isolated forty-two-candidate flow comparison preserving all original inputs; receipt contracts enter research protocols/model versions and synthetic flow controls the experiment's evidence kind.
- Completed the three delayed-flow comparisons and a preregistered same-day pooled-Brier sensitivity; paired controls match exactly and neither timing assumption reaches the full-coverage direction target.
- Added offline, source-tagged US sector history conversion with explicit reconstructed New York receipts, DST handling and missing-history preservation.
- Added a separate thirty-six-candidate sector comparison while preserving every original candidate's inputs; global sector context enters shared factors only when uniform across the fixed ten.
- Extended overseas windows with an explicit source prefix and recorded sector hashes, feature names and receipt assumptions without changing default FMP behavior.
- Completed three matched sector comparisons with every original candidate's calibration scores unchanged; retained mixed changes and failed full-coverage target evidence locally.
- Acquired and verified isolated company-level institutional histories for later research, preserving observed retrieval times, category changes, missing records and revision limitations.

## Unreleased — 2026-11-07

- Added causal own-price positive/flat return fractions and sign persistence with finite-observation minima, trusted-session windows and unknown-data preservation.
- Added an opt-in thirty-candidate path comparison with six explicit feature ablations, retaining every original candidate's inputs and fixed-ten-only model selection.
- Completed three matched path-policy comparisons, verifying every original candidate's calibration scores exactly while retaining mixed changes and failed daily full-coverage target evidence locally.
- Prepared isolated public overseas sector histories after recording vendor entitlement rejections, preserving source identity, shorter histories and reconstructed-availability limitations for subsequent research.
- Added opt-in past-only stock responses to TX/TE/TF regular-session returns and interactions with valid preopen night moves, preserving original features and full daily evaluation.
- Required paired observations, same-expiry consecutive primary-session references and explicit assumed regular-quote receipt; retained unknown histories and isolated external stocks from primary calendars and model selection.
- Completed all three exposure-interaction comparisons with matched answers, unchanged price-only calibration controls and shared-model verification; preserved mixed changes and unsuccessful full-coverage target evidence locally.
- Added offline TAIFEX CSV/ZIP parsing and relative night-session context using strictly earlier same-contract regular closes, with explicit reconstructed receipt times and no settlement/open-interest inputs.
- Added inclusive event expiry and canonical timestamp resolution to point-in-time joins; stale night context stays unknown, while permanent events retain their previous behavior.
- Exposed opt-in historical-night research provenance and shared market inputs, preserving the fixed-ten universe, original price features and complete daily evaluation.
- Completed matched night-context comparisons under the declared receipt assumption, verifying unchanged price-only controls and preserving improvements, regressions and unsuccessful all-constituent target evidence locally.
- Added opt-in beta-projected shared-factor candidates, using decision-time fixed-ten market exposures and pooled stock residuals without external-asset fitting.
- Added training-only volatility-scaled Gaussian scores, explicit missing-input fallbacks and unavailable-candidate diagnostics; exposed twenty-four-candidate research while retaining original sets and daily full-coverage gates.
- Compared all three exposure policies against frozen controls, reconstructing selected models' training statistics and preserving unsuccessful target evidence; separately acquired official daily/after-hours archive evidence for future input research without changing the experiment matrix.
- Added opt-in incremental overseas-window context: two as-of global FMP close joins measure new cumulative information after the Taiwan anchor cutoff, distinguish updated receipts from stale quotes, and preserve unknown starting history.
- Recorded window feature names and CLI provenance while preserving original price inputs, labels, candidate parameters and default research behavior; added causal, boundary, scale and transfer-isolation tests.
- Verified unchanged price-only calibration controls across all three overseas-window policies; retained inconclusive one-day paired improvements and failed full-coverage target evidence in local reports.
- Added an opt-in sixteen-candidate direction search with six matched price-plus-US-market feature ablations; original candidate sets and fixed-ten-only chronological selection remain available.
- Compared the three declared market-ablation policies against frozen preopen controls, verifying identical original-candidate calibration scores and retaining failed all-constituent target evidence locally.
- Added explicit postclose/preopen decision modes to direction research, including primary-clock validation, frozen timing contracts and mode-specific prediction/model provenance.
- Limited development-freeze timestamps to the fixed ten and reference ETF, preventing external-stock metadata from changing training-label availability.
- Preserved identical price anchors and future-close targets while comparing overnight information; extended full-constituent and leakage tests to both timing modes.
- Extended direction audits to full declared current constituents, with an exact worst-five calculation covering all 2,118,760 subsets of fifty stocks without materializing each subset.
- Added complete official-holdings snapshot validation, checksum traceability and transfer-only CLI configuration; membership documents cannot change training roles or supply a five-stock substitute for the full universe.
- Preserved shared model/threshold checks, complete-calendar missing-data penalties and historical-membership limitations for the full-scope audit.
- Fixed direction-only test calendars to retain valid stock forecasts when ETF quotes are absent, and retained complete-panel outages at fold boundaries without admitting reserved-period labels.
- Added fixed-threshold pooled/worst-stock Brier selection policies and shared market-plus-stock-residual return models, with training-only transforms and unchanged target gates.
- Recorded incomplete historical factor panels as unavailable candidates without fabricating pre-listing data; retained actual fitted sample counts and excluded skipped candidates from trial totals.
- Compared all three declared policies on the same development dates; preserved failed target evidence, original model promotion state and multi-seed synthetic behavior.
- Added a separate nested chronological direction-model search across eight predeclared model/window candidates and five thresholds, preserving the fixed-ten training universe and frozen-model external transfer diagnostics.
- Audited every fixed-ten stock and all 252 five-stock subsets with complete-calendar denominators, missing/abstained prediction penalties, directional baselines and market-date block diagnostics.
- Added isolated, multi-seed hypothetical-information experiments covering absent, weak, strong, delayed and missing signals; synthetic success cannot establish real-market accuracy or change release gates.
- Preserved final-date exclusions, immutable local experiment protocols, data/source fingerprints and research-only CLI commands; documented the unsuccessful historical target and conditional synthetic results without publishing provider data.

## Unreleased — 2026-11-05

- Fixed the user-selected ten as the exclusive shared-model training/calibration/selection universe and predictive market context; added same-fold and reserved-period transfer tests for 0050, 1216, 2002, 2412 and TAIEX, with immutable role-tagged paper forecasts.
- Added portable feature ablations, bounded training windows, chronological calibration shrinkage, fixed/frequency/directional baselines, balanced direction metrics and paired market-date block diagnostics.
- Separated executable open-to-close targets from close-price forecasts, fitted arithmetic-return estimates, and reported frozen-selection gross/net strategy costs.
- Preserved missing index volume without fabricating activity, distinguished index-point returns from adjusted stock returns, and excluded transfer records from core release evidence.
- Added grouped transfer tables to the dashboard and synthetic export, explicit prediction targets, reproducible fixed-ten configurations and protocol documentation.

- Documented feasibility diagnostics and an ordered optimization plan covering constant/directional baselines, calibration, executable-return targets, factor ablations and live-operation gaps; preserved the frozen models and private research evidence.
- Fixed FRED real-time end dates in Taiwan mornings to respect the provider's St. Louis calendar day; retained the requested and effective bounds.
- Separated Fubon login rejections from market-data initialization errors using fixed diagnostic codes without exposing SDK account results.
- Protected native SDK rotating logs with a private directory and publication exclusions; force-tracked logs remain blocked by the audit.
- Added long-history research and market-context acquisition configurations, verified real historical replay/maturity, and documented manual daily paper operations and remaining release gates.
- Displayed synthetic/replayed/timely issuance labels and an independent reserved-holdout panel, keeping development metrics and historical replay counts distinct from live paper evidence.
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
