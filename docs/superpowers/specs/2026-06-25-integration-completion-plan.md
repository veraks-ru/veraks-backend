# Integration completion plan (audit follow-up)

Date: 2026-06-25. Source: 5-agent cross-module audit. Baseline: 331 tests pass, mypy strict + ruff clean.

Decisions: build **everything phased**; **defer B2B (Phase 5)**; maker-checker = distinct ADMIN (keep, just document); **enable** `seasons_auto_finalize`.

Discipline: TDD for behavior changes. Keep `pytest` / `mypy app` / `ruff check app tests` green after every phase.

## Phase 0 — Truth & cleanup (low-risk) ✅ DONE (333 tests, mypy+ruff clean)
- [x] E6 deleted dead `AlwaysAllowsDisputeGuard` + its test; fixed stale "stub" docstrings in `seasons_coordination.py`, `worker.py`, `seasons/ports/gateways.py`.
- [x] E2 fixed `ScoreEvent` idempotency docstring (points at resolutions `ScoringDispatch` dedup + latest-wins).
- [x] E4 overturn guard: rejects `new_outcome == current.outcome` + test.
- [x] E5 identity: `UsernameTakenError` race → re-allocate handle + retry (not a 500) + test. display_name-on-relogin DEFERRED to Phase 2 (it's the user-editable field PATCH /users/me owns; real_name_enc already refreshes).
- [x] E7 `seasons_auto_finalize=True` default + positive test; rationale comments updated.
- [x] F4 `.env.example`: added `SEASONS_/RESOLUTIONS_/BILLING_*` + placeholder `WEBHOOK_*`.

## Phase 1 — Audit & immutability invariant ✅ DONE (333 tests, mypy+ruff clean, head 0011)
- [x] C1 predictions: new `AuditTrailRecorder` delegates to shared `SqlAlchemyAuditTrail`; deleted `LoggingAuditRecorder` stub. Events: all 5 mutating use-cases (create/update/publish/close/cancel) now record to `audit_log`; added `FakeAuditTrail` + audit-content assertions.
- [x] C2 migration `0011`: `REVOKE UPDATE, DELETE` on `audit_log`/`resolutions`/`ledger_transactions`/`ledger_entries` from `APP_DB_ROLE` (default `orakul_app`), guarded by role existence (no-op in CI/test). Defense-in-depth atop the triggers.

## Phase 2 — Missing read endpoints  (IN PROGRESS — A4 done)
- [~] A1 Users/Profiles — IN PROGRESS:
  - [x] A1.1 identity-only: `get_by_username` (port/adapter/fake), `GetPublicProfile` (active-only, 404 hidden suspended), `UpdateMyProfile`, `User.edit_profile` (settles E5: display_name is user-owned, NOT clobbered on relogin; real_name_enc stays ЕСИА source-of-truth). New `/users` router (`GET /users/{username}` public, `PATCH /users/me`), `PublicProfileResponse`/`UpdateProfileRequest` schemas, mounted in main. 7 unit + 4 integration tests. 357 green.
  - [~] A1.2 cross-domain enrichment (served from OWNING domain's router under `/users/...` paths to avoid module cycles):
    - [x] `GET /users/me/payouts` — served by billing router (`PayoutRepository.list_by_user`, `ListMyPayouts` use-case). Renamed admin `list`→`list_all` (shadowed builtin). 2 tests. 359 green.
    - [x] `GET /users/me/predictions` (all incl pending) + `GET /users/{username}/predictions` (public, resolved-only) — predictions router. New `UserDirectory` port + `SqlAlchemyUserDirectory` (predictions→identity username→id), `PredictionRepository.list_for_user(resolved_only=)`, `ListMyPredictions`/`ListUserPredictions` use-cases, `ProfileUserNotFoundError`→404. 5 unit/integration tests. 366 green.
    - [x] `GET /users/{username}/calibration` — scoring router. Calibration math already existed; switched endpoint from `{user_id}` (UUID, non-spec) to `{username}` via new scoring `UserDirectory` gateway. `GetUserCalibration` now resolves username→id (404 `ProfileNotFoundError`) and returns `(user_id, report)`. 368 green.

**Phase 2 COMPLETE.** ✅
- [x] A2 crowd-signal `GET /events/{id}/predictions/summary` — `GetEventPredictionSummary` use-case (distribution per grade + consensus `c_e`), hidden until close (`PredictionSummaryHiddenError`→409, anti-anchoring §5). Public endpoint. 6 tests. 343 green.
- [x] A4 `GET /billing/plans` + `GET /billing/subscriptions/me` (added `get_latest_by_user` to port+adapter+fake, `GetMySubscription` use-case, `PlanResponse`/`PlansResponse` schemas, 4 integration tests). 337 tests green.
- [x] A6 `GET /admin/payouts` (list, admin, season filter) + `GET /seasons/{slug}/prize-fund` (public transparency: funds+balances+payouts; new `SeasonDirectory` billing→seasons gateway `SqlAlchemySeasonDirectory`, `PrizeFundRepository.list_by_season`, `GetSeasonPrizeFund` use-case, billing `SeasonNotFoundError`→404). 346 green.

**Phase 2 COMPLETE except A1 (Users/Profiles).**

## Phase 3 — Workers / orchestration
- [x] B1 event auto-close worker: `EventRepository.list_open_due(now)` (port/adapter/fake, `Sequence` return to dodge `list`-shadow), `CloseExpiredEvents` use-case (open→closed + SYSTEM audit, idempotent), worker task `close_expired_events` (every minute) that closes then calls `LockEventPredictions` per closed event. 2 unit tests + worker-wiring updated. 370 green.
- [x] B2 recalibration wiring: `EventScoringGateway.list_season_calibration_entries` (port/adapter/fake), `RecalibrateSeasonGradations` use-case (groups season population by nominal → freq/n → isotonic `recalibrate`), `GradationRecalibration` DTO, admin endpoint `GET /admin/seasons/{id}/recalibration` (returns suggested monotone mapping for next season; freezing into LeagueConfig left to activation by design). Dead PAV code now has a real caller. 4 unit + 2 integration tests. 374 green.
- [x] B3 `reconcile` worker: `LedgerRepository.totals_by_type` (port/adapter/fake, join entries→transactions), `ReconcileLedger` use-case (per-kassa debit==credit integrity check), `LedgerReconciliation` DTO, worker task `reconcile` (hourly, logs ERROR on imbalance). External provider/bank comparison left as TODO(billing-infra). 2 unit tests + worker-wiring (6 tasks, 5 cron). 376 green.

**Phase 3 COMPLETE.** ✅

## Phase 4 — Billing completion
- [x] A5 payout dispatch + webhook lifecycle: entity transition guards added (`mark_processing`←approved, `mark_paid`/`mark_failed`←processing); `DispatchPayout` use-case (calls `send_payout`, approved→processing) + `POST /admin/payouts/{id}/dispatch`; `RecordPayoutResult` use-case (idempotent on terminal) + `POST /webhooks/payouts/yookassa` → paid/failed; `PayoutWebhookRequest` schema. 5 unit + 1 integration (full approve→dispatch→webhook→paid) tests. 383 green.
- [ ] D1 webhook signature verification (payments + payouts), `WEBHOOK_*` settings.
- [x] E1 `events.season_id`: post-publish lock added to `apply_edits` (season frozen once `open`, fairness of season scoring) + tests. ORM now declares `ForeignKey("seasons.id")` — the DB FK already existed (migration `0007`), only the ORM declaration was missing (create_all drift); no new migration. Stale auto-close TODO comment removed (done in B1). 377 green.

## Phase 5 — B2B subsystem  (DEFERRED — later milestone)
- A3 b2b_clients/invoices, API-key auth, quota, `/b2b/signal`, `/b2b/usage`.

## Phase 6 — Test hardening
- [ ] G1 Postgres e2e (testcontainers): ledger separation/balance triggers, append-only + REVOKE, hash-chain persistence, UNIQUE/enum/FK.
- [ ] G2 unit/integration: overturn re-score, audit-content assertions, season_id-after-publish lock.

## Phase 7 — Performance / polish
- [ ] B4 incremental rating recompute (affected (user,scope) only).
- [ ] F1 Redis ZSET leaderboards + cache.
- [ ] F2 fix N+1 in `scoring_gateway` (JOIN + bulk UPDATE).
- [ ] F3 `request_id`/correlation middleware → audit metadata.
