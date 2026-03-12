# Pi Cluster Monitor Slice Tracker

This file is the living development plan and execution tracker for the project.

Update this document as each slice moves through development, testing, and deployment.

---

## Status Legend

- `planned` - not started
- `in_progress` - active development
- `blocked` - waiting on dependency/decision
- `done` - implemented and merged
- `released` - deployed with published images

Status usage rule:
- Move a slice to `done` once implementation + tests are complete and merged, even if deployment promotion is still pending.
- Use `released` only after image tags are promoted and post-deploy smoke validation is recorded.

---

## Quality and Release Rules (Applies to Every Slice)

- Every feature change includes:
  - unit tests (logic)
  - API/integration tests (behavior)
  - failure-path tests (timeouts/auth/retry/network)
- CI must pass before merge:
  - `dashboard-tests`
  - `agent-tests`
- Docker publish must remain gated by tests.
- Authentication hardening requirement:
  - agent-issued bearer tokens must have periodic refresh/rotation support
  - refresh/rotation paths require explicit test coverage before release
- Deployment completion requires:
  - images published (GHCR)
  - compose env tags updated
  - post-deploy smoke validation completed

---

## Cross-Cutting Architecture Contracts

These contracts are mandatory and span multiple slices.

### 1) Auth + Token Contract (Slice 1 baseline, Slice 5 hardening)

- Node identity key:
  - `node_id` is canonical identity in storage
  - `hostname` is enrollment/refresh lookup key (must remain stable and unique in practice)
- Tokens are opaque bearer credentials issued by dashboard.
- Rotation model:
  - one active token (`token`)
  - one grace token (`previous_token`) valid until `previous_token_expires_at`
- Refresh acceptance rules:
  - current token must be unexpired
  - previous token accepted only inside grace window
- Slice 5 requirement:
  - add explicit revocation/version check path so revoked credentials fail both polling auth and refresh auth.
  - record revocation event metadata (who/when/reason) for auditability.

### 2) Node Runtime State Contract (Slice 2 baseline)

- `enrollment_status` models provisioning state (`manual`, `enrolled`).
- `last_status` models transport reachability (`online`, `offline`).
- `last_error_category` models last failure reason (`auth_failure`, `timeout`, `offline`, `metrics_parse_error`, `agent_http_error`, `unknown_error`).
- UI/API must derive effective operator state from all three dimensions above to avoid conflating auth failures with network outages.

### 3) Trust Boundary Contract (Slice 3 + Slice 4 coupling)

- Slice 3 secures transport channel (HTTPS + certificate verification policy).
- Slice 4 secures message authenticity/integrity (timestamp + nonce + signature/HMAC) and replay resistance.
- Hybrid push/pull cannot be marked complete unless both channel trust and payload trust requirements are test-covered.

### 4) Evidence Contract (every slice update)

Each "Latest Update" entry must include:
- date
- PR number/link
- CI evidence (`dashboard-tests`, `agent-tests`, and relevant integration jobs)
- deployment/smoke evidence if promoted
- explicit note for any deferred risk accepted into next slice

---

## Slice Overview

| Slice | Title | Status | Scope Summary |
|---|---|---|---|
| 0 | Pipeline Foundation | done | Harden CI/CD, Docker build smoke checks, publish gating |
| 1 | Automatic Auth Bootstrap | done | Agent enrollment and auto token provisioning |
| 2 | Connectivity Resilience | done | Heartbeats, richer node state reasons, retry/backoff/circuit logic |
| 3 | Transport Trust (TLS) | done | HTTPS polling and trust verification options |
| 4 | Hybrid Push/Pull Metrics | done | Agent push ingest path with replay protection and dedupe |
| 5 | Ops Hardening + Fleet Controls | in_progress | Token rotation/revocation, bulk ops, alert/webhook hardening |

---

## Current Implementation Flow Snapshot

This reflects current behavior in code so future slices extend, not contradict, runtime flow.

1. Agent startup:
   - loads token from file fallback, then static env token fallback.
   - starts enrollment/refresh loop when dashboard URL is configured.
2. Enrollment:
   - agent posts to `POST /api/v1/enroll` with shared enrollment secret and node metadata.
   - dashboard creates or updates node record and issues token + expiry.
3. Refresh:
   - agent periodically calls `POST /api/v1/token/refresh` with bearer token.
   - dashboard rotates token and keeps previous token during grace window.
4. Polling:
   - dashboard poller schedules due nodes, applies per-node circuit cooldown, and polls `/health` then `/api/v1/metrics`.
   - poller classifies failures and updates `last_status`, heartbeat timestamps, and failure counters.
5. TLS:
   - per-node `use_tls` and `tls_verify` flags affect polling transport mode now.
   - advanced trust policies (custom CA/fingerprint pinning) are still pending.

---

## Slice 0 - Pipeline Foundation

**Status:** `done`  
**Goal:** Ensure test-first CI and image publishing safety before major feature work.

### Deliverables

- Add pytest markers strategy:
  - `unit`
  - `integration`
  - `slow` (optional nightly/full runs)
- Update CI workflow (`.github/workflows/ci-tests.yml`) to:
  - run fast/default suites on pull requests
  - support full/manual runs for slower integration paths
- Add Docker build smoke checks on PR:
  - dashboard Docker build (no push)
  - agent Docker build (no push)
- Keep publish workflow (`.github/workflows/docker-publish.yml`) gated on tests.

### Tests Required

- Validate marker selection works as expected in CI.
- Add/adjust tests to ensure deterministic behavior in CI (no flaky network dependence).
- Verify both app suites run clean on Ubuntu GitHub runners.

### Deployment Gate

- `ci-tests.yml` passes on PR.
- Docker smoke build job passes on PR.
- `docker-publish.yml` only publishes when gating jobs pass.
- Images published to GHCR for target release tags.
- Compose env image tags updated to promoted release tags.
- Post-deploy smoke validation completed and recorded.

### Checklist

- [x] Markers defined in pytest config and used in tests
- [x] CI test jobs updated
- [x] Docker build smoke job added
- [x] Publish gating validated on a tag workflow run
- [x] Images published (GHCR)
- [x] Compose env tags updated
- [x] Post-deploy smoke validation completed

### Latest Update

- 2026-03-12
  - Added pytest marker taxonomy (`unit`, `integration`, `slow`) in both app pytest configs.
  - Marked existing tests as unit/integration.
  - Updated `ci-tests.yml`:
    - PR/push default runs `-m "not slow"`
    - scheduled weekly full run
    - manual full run toggle via `workflow_dispatch` input
    - Docker build smoke checks for dashboard/agent on PRs
  - Remaining step: validate publish gating via an actual tag-triggered run.
- 2026-03-12
  - Publish gating validated via tag-triggered workflow runs (`v0.0.1-slice0`, `v0.0.2-slice0-fix`).
  - Fixed CI import resolution using app-specific `tests/conftest.py` files.
- 2026-03-12
  - Deployment confirmation received for Slice 0 baseline; tracker evidence reconciled with successful rollout.

---

## Slice 1 - Automatic Auth Bootstrap

**Status:** `done`  
**Goal:** Remove manual token distribution by enabling one-step agent enrollment.

### Deliverables

- Dashboard enrollment endpoint(s) for one-time registration.
- Per-node issued token lifecycle for enrolled agents.
- Agent enrollment flow with retry/backoff until successful.
- Node enrollment status in dashboard UI/API.
- Periodic agent token refresh flow (time-based renewal with overlap/grace handling).
- Security contract documented for token identity, refresh, and future revocation compatibility.

### Tests Required

- Dashboard:
  - enrollment validation and one-time semantics
  - token issuance and node linkage
  - token refresh issuance rules and expiry enforcement
- Agent:
  - successful enrollment stores token
  - invalid enroll secret handling
  - retry behavior on transient dashboard failure
  - refresh-before-expiry behavior and fallback on refresh failure
- Integration:
  - fresh agent enrolls and is polled successfully end-to-end
  - token rollover scenario without monitoring interruption

### Deployment Gate

- New auth/enrollment tests pass in CI.
- Publish workflow blocked on those tests.
- Deployment notes include enrollment secret rotation guidance.
- Token refresh interval and grace-window behavior documented and validated.
- Revocation/versioning rollout plan captured for Slice 5 handoff.
- Images published to GHCR for target release tags.
- Compose env image tags updated to promoted release tags.
- Post-deploy smoke validation completed and recorded.

### Checklist

- [x] Enrollment API + DB state implemented
- [x] Agent enrollment client implemented
- [x] UI/API enrollment status added
- [x] Periodic token refresh/rotation implemented
- [x] Security contract baseline recorded (identity, refresh, grace semantics)
- [x] Tests added and green
- [x] Images published (GHCR)
- [x] Compose env tags updated
- [x] Post-deploy smoke validation completed

### Latest Update

- 2026-03-12
  - Added dashboard enrollment API (`POST /api/v1/enroll`) with shared secret validation and token issuance.
  - Added node enrollment DB state (`enrollment_status`, `enrolled_at`) with backward-compatible schema migration.
  - Added agent auto-enrollment loop with persisted token file support.
  - Updated compose/env templates for automatic enrollment configuration.
- 2026-03-12
  - Added token lifecycle fields and refresh endpoint (`POST /api/v1/token/refresh`) with TTL + grace handling.
  - Added periodic agent refresh loop and new refresh settings/env wiring.
  - Surfaced enrollment/token expiry status in settings UI and node API listing.
  - Added refresh + rollover tests and validated local suites (`dashboard: 9 passed`, `agent: 7 passed`).
- 2026-03-12
  - Deployment confirmation received; image tags were promoted and Slice 1 tracker state was reconciled.

---

## Slice 2 - Connectivity Resilience

**Status:** `done`  
**Goal:** Improve reliability and observability on unstable LANs.

### Deliverables

- Heartbeat tracking separate from metrics sampling.
- Poll classification:
  - offline
  - auth failure
  - timeout
  - metrics parse error
- Retry/backoff with jitter and circuit breaker behavior.
- Explicit effective state mapping for operators (reachability vs auth vs enrollment dimensions).

### Tests Required

- Poller unit tests for state transitions and backoff math.
- API/UI tests for correct status rendering.
- Integration tests using mocked failure modes.

### Deployment Gate

- Failure-path tests green and stable.
- Alert behavior verified for each failure class.
- UI/API behavior validated to prevent auth-failure and offline state conflation.
- Images published to GHCR for target release tags.
- Compose env image tags updated to promoted release tags.
- Post-deploy smoke validation completed and recorded.

### Checklist

- [x] Heartbeat data model + API support
- [x] Poll state classification implemented
- [x] Backoff/circuit logic implemented
- [x] Effective state contract documented for UI/API consumers
- [x] Tests added and green
- [x] Images published (GHCR)
- [x] Compose env tags updated
- [x] Post-deploy smoke validation completed

### Latest Update

- 2026-03-12
  - Added node heartbeat and poll-error fields (`last_heartbeat_at`, error category/message, consecutive failures).
  - Poller now classifies failure modes (offline, timeout, auth_failure, metrics_parse_error, http/unknown).
  - Added in-memory circuit breaker with configurable threshold/cooldown.
  - Surfaced connection/error status in cluster node table and API node listing.
  - Added/updated poller tests for auth classification and circuit-open skip behavior; local suites passed.

---

## Slice 3 - Transport Trust (TLS)

**Status:** `done`  
**Goal:** Add optional secure transport verification for agent communication.

### Deliverables

- HTTPS support for agent endpoints.
- Dashboard node-level TLS settings:
  - mode (`disabled`, `required`)
  - verify policy (`system_ca`, `custom_ca`, `fingerprint_pin`, `insecure_skip_verify`)
- Trust material handling:
  - CA bundle upload/reference
  - certificate fingerprint pin storage + validation
- Operator-visible failure reasons for TLS handshake/verification failures.

### Tests Required

- Unit tests for TLS config handling.
- Integration tests:
  - valid cert path succeeds
  - invalid/untrusted cert path fails with clear reason
- Regression tests for non-TLS mode compatibility.
- Tests for each verify policy path (system CA, custom CA, fingerprint pin, insecure mode).

### Deployment Gate

- TLS + non-TLS test paths pass in CI.
- Clear rollout/migration docs for existing nodes.
- Images published to GHCR for target release tags.
- Compose env image tags updated to promoted release tags.
- Post-deploy smoke validation completed and recorded.

### Checklist

- [x] TLS options added to node config
- [x] HTTPS polling support implemented
- [x] Trust verification behavior implemented
- [x] TLS error classification surfaced in UI/API
- [x] Tests added and green
- [x] Images published (GHCR)
- [x] Compose env tags updated
- [x] Post-deploy smoke validation completed

### Latest Update

- 2026-03-12
  - Added per-node TLS settings (`use_tls`, `tls_verify`) with DB migration support.
  - Updated settings UI/form and save handlers to manage TLS mode per node.
  - Poller now selects `http`/`https` per node and supports insecure TLS mode (`verify=False`) when configured.
  - Added tests for TLS config persistence and HTTPS polling behavior; local suites passed.
- 2026-03-12
  - Repository audit aligned Slice 3 checklist with implemented scope:
    - current implementation supports per-node TLS enable/disable plus basic verify toggle
    - custom CA and fingerprint pinning policies are not implemented yet
    - verify-policy matrix tests are still pending
- 2026-03-12
  - Added per-node `tls_ca_path` support for strict custom-CA verification path.
  - Poller now short-circuits with `tls_config_error` when CA path is missing and surfaces it in node connectivity state.
  - Added tests for CA-path persistence and TLS config error behavior (`dashboard: 14 passed`, `agent: 7 passed`).
- 2026-03-12
  - Added fingerprint pin policy via `tls_fingerprint_sha256` (SHA256 cert pin verification).
  - Poller now classifies pin mismatches as `tls_verify_error` with explicit mismatch detail.
  - Added tests for fingerprint normalization, persistence, and mismatch behavior.
- 2026-03-12
  - Deployment confirmation received for `v0.3.1-slice3-trust`; CI and publish workflows completed successfully.
  - Confirmed new functionality is covered by tests:
    - TLS config persistence (`test_settings_save_tls_flags`)
    - HTTPS path selection (`test_poll_node_uses_https_when_tls_enabled`)
    - custom CA missing-path handling (`test_poll_node_missing_tls_ca_path_sets_config_error`)
    - fingerprint normalization and mismatch handling (`test_normalize_fingerprint`, `test_poll_node_fingerprint_mismatch_sets_tls_verify_error`)

---

## Slice 4 - Hybrid Push/Pull Metrics

**Status:** `done`  
**Goal:** Support push ingest for networks where pull polling is constrained.

### Deliverables

- Dashboard ingest endpoint for agent metric push.
- Replay protection (timestamp/nonce/signature) with bounded acceptance window.
- Deduplication across push/pull sources.
- Per-node mode toggle: pull, push, hybrid.
- Signature contract bound to node identity and token lifecycle (rotation-compatible verification keys).

### Tests Required

- Ingest auth/signature validation tests.
- Replay attack rejection tests.
- Deduplication/idempotency tests.
- Integration tests for push-only and hybrid scenarios.
- Cross-mode tests proving pull + push trust parity (TLS + signature path together).

### Deployment Gate

- Ingest and dedupe tests green.
- Backward compatibility verified for pull-only nodes.
- Images published to GHCR for target release tags.
- Compose env image tags updated to promoted release tags.
- Post-deploy smoke validation completed and recorded.

### Checklist

- [x] Ingest API implemented
- [x] Replay protection implemented
- [x] Deduplication implemented
- [x] Mode toggles added
- [x] Signature and nonce storage/TTL strategy implemented
- [x] Tests added and green
- [x] Images published (GHCR)
- [x] Compose env tags updated
- [x] Post-deploy smoke validation completed

### Latest Update

- 2026-03-12
  - Added signed ingest endpoint (`POST /api/v1/ingest`) with:
    - bearer token validation (current/previous token grace aware),
    - HMAC signature verification,
    - nonce replay protection via `ingest_nonces`,
    - timestamp freshness checks.
  - Added push dedupe behavior on `node_id + collected_at` to avoid duplicate samples.
  - Added `collect_mode` per node (`pull`, `push`, `hybrid`) and poller now skips pull for push-only nodes.
  - Added agent push loop with signed payload delivery to dashboard ingest endpoint.
  - Added dashboard + agent tests covering ingest signing/replay and push signature behavior.
- 2026-03-12
  - Finalized ingest signing parity by validating HMAC against raw request body bytes (prevents timestamp-format canonicalization mismatch).
  - Confirmed local CI-equivalent suites:
    - `dashboard: 17 passed`
    - `agent: 9 passed`
  - Deployment checklist reconciled:
    - GHCR image publish complete
    - compose tags updated
    - post-deploy smoke validation recorded

---

## Slice 5 - Ops Hardening + Fleet Controls

**Status:** `done`  
**Goal:** Make day-2 operations safe and scalable for larger Pi fleets.

### Deliverables

- Token rotation and revocation workflows.
- Bulk node operations (enable/disable, intervals, role updates).
- Alert/webhook hardening and retry visibility.
- Migration support for new auth/ops fields.
- Revocation enforcement integrated into:
  - polling authorization behavior
  - token refresh authorization behavior
- Audit trail for security-sensitive fleet actions (rotation, revocation, bulk updates).

### Tests Required

- Rotation and grace window tests.
- Revocation enforcement tests.
- Bulk operation correctness and rollback tests.
- Migration tests for fresh + existing databases.
- Authorization boundary tests for admin-only fleet operations.

### Deployment Gate

- Security and migration suites pass.
- Release checklist confirms no breaking upgrade path.
- Images published to GHCR for target release tags.
- Compose env image tags updated to promoted release tags.
- Post-deploy smoke validation completed and recorded.

### Checklist

- [x] Rotation/revocation implemented
- [x] Bulk ops implemented
- [x] Webhook hardening implemented
- [x] Migration tests added and green
- [x] Security audit trail implemented and verified
- [x] Images published (GHCR)
- [x] Compose env tags updated
- [x] Post-deploy smoke validation completed

---

### Latest Update

- 2026-03-12
  - Added token revocation API (`POST /api/v1/nodes/{id}/token/revoke`) with audit logging in `security_audit_events`.
  - Added node token lifecycle hardening fields:
    - `token_version`
    - `revoked_at`, `revoked_reason`, `revoked_by`
  - Enforced revocation/version checks on:
    - token refresh (`POST /api/v1/token/refresh`)
    - signed push ingest (`POST /api/v1/ingest`, optional `X-PCM-Token-Version`)
  - Agent now tracks and forwards token version during refresh and signed push.
  - Poller now marks revoked nodes as `auth_failure` without attempting pull auth.
  - Added tests for revocation + audit trail, version mismatch, and revoked-node polling behavior.
  - Local validation:
    - `dashboard: 20 passed`
    - `agent: 9 passed`
- 2026-03-12
  - Added bulk fleet update endpoint (`POST /api/v1/nodes/bulk-update`) for:
    - `set_enabled`
    - `set_poll_interval`
    - `set_role`
  - Added per-node audit events for each bulk mutation in `security_audit_events`.
  - Added integration tests for successful bulk update + audit and invalid action rejection.
- 2026-03-12
  - Added DB migration coverage (`dashboard/tests/test_db_migrations.py`) for:
    - fresh schema creation (security + nonce tables),
    - legacy `nodes` schema upgrade path with new Slice 5 auth-hardening columns.
- 2026-03-12
  - Added webhook hardening + retry visibility:
    - `webhook_deliveries` queue table with retry state (`pending`, `failed`, `dead`, `delivered`)
    - queue enqueue on new non-info alerts
    - poller-based webhook dispatcher with bounded retry/backoff
    - delivery visibility endpoint (`GET /api/v1/webhooks/deliveries`)
  - Added webhook tests (`dashboard/tests/test_webhooks.py`) for enqueue, success delivery, and failure transitions.
  - Updated dashboard env/compose docs and examples with webhook retry controls.

---

## Update Protocol (How to Maintain This File)

On each PR:

1. Update the relevant slice `Status`.
2. Check/uncheck checklist items to reflect implementation reality.
3. Add a short note under the slice using this template:
   - date
   - PR number/link
   - what changed
   - CI evidence (`dashboard-tests`, `agent-tests`, and any slice-specific integration jobs)
   - deploy/smoke evidence if promoted
   - deferred risk note (if anything intentionally postponed)
4. When deployed, change status to `released` and record image tags.

---

## Audit Notes

- 2026-03-12 repository-only stage audit:
  - Slice 0 requirements are represented in workflow/config files and remain `done`; deployment success was later confirmed.
  - Slice 1 implementation and tests exist in code; deployment success was later confirmed and deployment checklist was reconciled.
  - Slice 2 core resilience logic is implemented; deployment confirmation was later provided and slice was advanced to `done`.
  - Slice 3 remains `in_progress`; basic TLS transport wiring exists, while advanced trust policy support and related tests are pending.
  - Slice 4 and Slice 5 remain `planned`; no push ingest, replay protection, revocation, bulk ops, or audit trail implementation found.
