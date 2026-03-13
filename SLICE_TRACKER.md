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
| 5 | Ops Hardening + Fleet Controls | released | Token rotation/revocation, bulk ops, alert/webhook hardening |
| 6 | Control Plane Security + Identity Integrity | planned | Operator authZ, authenticated audit actor, stable node identity binding, runtime error observability |

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

**Status:** `released`  
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

**Status:** `released`  
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
- 2026-03-12
  - Release promotion confirmed for `v0.5.2-slice5-webhooks`:
    - `CI Tests` completed successfully
    - `Build and Publish Docker Images` completed successfully
  - Slice 5 advanced from `done` to `released`.

---

## Slice 6 - Control Plane Security + Identity Integrity

**Status:** `done`  
**Goal:** Close control-plane security gaps by enforcing operator authorization, hardening node identity semantics, and improving failure observability for background runtime loops.

### Deliverables

- Dashboard control-plane auth for mutating/sensitive endpoints:
  - add explicit operator authentication mechanism (session or API token)
  - add role/permission checks for fleet-control and security endpoints
- Protect sensitive routes:
  - require operator auth for `POST /api/v1/nodes/{id}/token/revoke`
  - require operator auth for `POST /api/v1/nodes/bulk-update`
  - define which read-only APIs remain unauthenticated (if any) and document rationale
- Authenticated audit actor:
  - remove trust in caller-supplied `actor` for security audit events
  - derive actor identity from authenticated operator context
- Node identity hardening:
  - move refresh/ingest/enrollment matching away from hostname-only lookup
  - introduce stable identity binding (`node_id`/`agent_id`) in token lifecycle paths
  - add uniqueness and conflict-handling strategy for identity keys (hostname and/or agent identifier)
- Runtime observability hardening:
  - replace silent background-loop exception swallowing with structured logs/metrics
  - add explicit failure counters/events for enroll, refresh, push, and webhook dispatch loops

### Design Guardrails

- Keep backward compatibility first:
  - maintain a staged migration plan so existing enrolled agents continue to function during rollout
  - prefer dual-path acceptance windows (old + new identity claims) with clear deprecation timeline
- Treat auth boundaries as deny-by-default:
  - all mutating routes must fail closed when auth context is missing/invalid
  - no security-sensitive behavior may rely on request body claims for operator identity
- Preserve security event integrity:
  - audit records must be tamper-evident in source attribution (principal, time, action, target, reason)
  - include request correlation identifiers to link API request logs with audit rows
- Keep implementation test-first:
  - add failing authorization and identity-collision tests before route/controller refactors
  - require regression tests proving current agent enrollment/refresh works during migration window

### Tests Required

- Authorization boundary tests:
  - unauthenticated and insufficient-role requests are rejected for revoke/bulk endpoints
  - authorized operator requests succeed and produce correct audit records
- Audit integrity tests:
  - `security_audit_events.actor` is sourced from auth principal, not payload fields
  - spoofed actor payload values do not alter stored actor attribution
- Identity-binding tests:
  - refresh and ingest reject hostname collision/rebinding attempts
  - identity migration path supports legacy agents and new identity claim flow
- Runtime observability tests:
  - enroll/refresh/push/webhook failures emit structured logs and update failure counters
  - transient network errors do not hide repeated auth/config faults

### Deployment Gate

- New control-plane auth/authorization tests pass in CI.
- Security regression suite validates no unauthorized fleet mutations are possible.
- Identity migration smoke run completed with mixed legacy/new agents.
- Operator auth and key-rotation runbook documented and validated.
- Images published to GHCR for target release tags.
- Compose env image tags updated to promoted release tags.
- Post-deploy smoke validation completed and recorded.

### Checklist

- [x] Operator authentication mechanism implemented
- [x] Authorization checks enforced on mutating/security routes
- [x] Audit actor attribution bound to authenticated principal
- [x] Stable node identity binding implemented for refresh/ingest/enrollment
- [x] Backward-compatible identity migration path implemented
- [x] Secret exposure minimized across UI/API node payloads
- [x] Auth policy parity enforced for HTML mutation surfaces
- [x] Structured background-loop error telemetry implemented
- [x] Tests added and green
- [ ] Images published (GHCR)
- [ ] Compose env tags updated
- [ ] Post-deploy smoke validation completed

### Execution Breakdown (6.1 / 6.2 / 6.3 / 6.4)

#### Slice 6.1 - Control Plane Auth Boundary

**Status:** `done`  
**Objective:** Enforce authenticated, role-aware access on fleet/security mutation paths first.

**Scope:**
- Introduce operator auth mechanism for dashboard API/UI mutating routes.
- Require auth + authorization for:
  - `POST /api/v1/nodes/{id}/token/revoke`
  - `POST /api/v1/nodes/bulk-update`
- Replace payload-driven `actor` usage with principal-derived identity in audit writes.
- Document public vs protected endpoint policy in README/ops docs.

**Acceptance Criteria:**
- Unauthenticated requests to protected routes return `401` (or redirect for UI as designed).
- Authenticated but insufficient-role requests return `403`.
- Authorized requests succeed and store authenticated principal in `security_audit_events.actor`.
- Existing non-mutating monitoring endpoints continue to function per documented policy.

**Tests Required:**
- auth required tests for revoke + bulk routes
- role boundary tests (`viewer`/`operator`/`admin` semantics as chosen)
- spoofed actor payload ignored tests
- regression tests for existing read-only endpoints

**Out of Scope:**
- Node identity migration mechanics (handled in 6.2).
- Background loop telemetry refactor (handled in 6.3).

#### Slice 6.2 - Node Identity Binding Migration

**Status:** `done`  
**Objective:** Remove hostname-only trust assumptions for refresh/ingest/enrollment paths.

**Scope:**
- Define canonical runtime identity key (for example `node_id` claim or durable `agent_id`).
- Add schema + API contract updates needed for identity binding.
- Implement backward-compatible migration path:
  - legacy hostname-based flow accepted during transition
  - new identity claim preferred and validated
- Add conflict detection/handling for identity collisions and rebinding attempts.

**Acceptance Criteria:**
- Refresh and ingest can authenticate/authorize using stable identity binding.
- Hostname collisions cannot silently hijack or rebind credentials.
- Migration mode supports mixed legacy/new agents with explicit cutoff plan documented.
- Enrollment updates preserve canonical identity semantics from Cross-Cutting Contracts.

**Tests Required:**
- identity collision and rebinding rejection tests
- dual-path migration compatibility tests (legacy + new agent behavior)
- token refresh and ingest parity tests under new identity contract
- DB migration tests for existing node rows

**Out of Scope:**
- Operator auth boundary (6.1).
- Runtime telemetry architecture improvements (6.3).

#### Slice 6.3 - Runtime Error Telemetry + Operational Diagnostics

**Status:** `done`  
**Objective:** Make background failures observable and actionable without noisy false positives.

**Scope:**
- Replace silent `except Exception: pass` patterns in agent/dashboard loops with structured logging.
- Add loop-level health counters/events for:
  - enroll attempts/failures
  - token refresh attempts/failures
  - push ingest attempts/failures
  - webhook dispatch attempts/failures/dead-letter transitions
- Surface minimal diagnostic views or API metrics for operator troubleshooting.
- Define alerting guidance for repeated auth/configuration failures.

**Acceptance Criteria:**
- Repeated background failures are visible in logs and/or API diagnostics.
- Operators can distinguish transient network failure from persistent auth/config failures.
- Telemetry additions do not break current polling/push runtime behavior.
- Failure instrumentation has bounded cardinality and retention controls.

**Tests Required:**
- loop error-path tests validating structured log/event emission
- retry/backoff telemetry progression tests
- webhook dead-letter visibility tests (including recovery path if applicable)
- regression tests ensuring loops keep running after handled exceptions

**Out of Scope:**
- New auth model semantics (6.1 completed already).
- Identity schema migration logic (6.2 completed already).

#### Slice 6.4 - Surface Auth Parity + Secret Exposure Minimization

**Status:** `done`  
**Objective:** Eliminate residual credential exposure and ensure web/UI mutation paths follow the same auth model as protected API mutations.

**Scope:**
- Enforce operator auth/role checks on HTML mutation routes and settings actions:
  - `/settings/nodes/save`
  - `/settings/nodes/{id}/toggle`
  - any other state-changing UI routes
- Define and enforce a response redaction contract for node objects:
  - never return `token` or `previous_token` in read APIs/UI payloads
  - only expose token lifecycle metadata required by operators
- Add explicit public/private API classification and guardrails:
  - cluster health/summary can remain public only if documented and intentional
  - sensitive detail routes require operator auth
- Remove or deprecate request-body `actor` fields from API schemas where principal is auth-derived.

**Acceptance Criteria:**
- No read endpoint or template context exposes bearer secrets in plaintext.
- HTML mutation routes reject unauthenticated calls (`401`) and insufficient role (`403`).
- API schema and runtime behavior are aligned (no spoofable or ignored identity fields presented as authoritative inputs).
- Regression tests confirm dashboard UX still works with authenticated operator sessions/tokens.

**Tests Required:**
- redaction tests for `/api/v1/nodes/{id}` and related node-detail payloads
- settings route authorization tests (save/toggle) with role matrix coverage
- template rendering tests confirming token secrets are not present in rendered output
- contract tests ensuring request-body `actor` is absent or ignored consistently with docs

**Out of Scope:**
- Loop telemetry/counters and diagnostics pipeline (6.3).
- New identity-migration protocol changes beyond existing `agent_id` contract (6.2).

**Implementation Ticket Map (Planning Only):**

- `S6.4-T1` Route auth parity for HTML mutations
  - Files:
    - `dashboard/app/routes/web.py`
    - `dashboard/tests/test_web.py`
  - Work:
    - add `require_operator(...)` checks for settings mutation endpoints (`save`, `toggle`, and any state-changing form handlers)
    - define minimum role per action and keep parity with API mutation policy
  - Done when:
    - unauthorized HTML mutations return `401`
    - insufficient role returns `403`
    - existing authorized UX flows still pass integration tests

- `S6.4-T2` Node payload redaction contract
  - Files:
    - `dashboard/app/routes/web.py`
    - `dashboard/templates/node_detail.html`
    - `dashboard/templates/settings.html`
    - `dashboard/templates/partials/node_form.html`
    - `dashboard/tests/test_web.py`
  - Work:
    - replace `SELECT * FROM nodes` read paths with explicit allowlisted columns
    - enforce API/template contract that excludes `token` and `previous_token` from read payloads
    - preserve only operator-safe metadata (expiry/version/revocation fields) needed by UI/API
  - Done when:
    - no read API response includes bearer token secrets
    - rendered HTML does not expose token secrets in DOM/source
    - tests assert secret keys are absent from serialized payloads

- `S6.4-T3` Model/API contract cleanup (actor deprecation)
  - Files:
    - `dashboard/app/models.py`
    - `dashboard/app/routes/web.py`
    - `dashboard/tests/test_web.py`
    - `README.md`
  - Work:
    - deprecate/remove request-body `actor` fields for protected mutation endpoints
    - ensure docs and examples show principal-derived actor behavior only
    - preserve backward compatibility with explicit ignore behavior only if needed for one release window
  - Done when:
    - OpenAPI/request schemas do not imply caller-controlled audit actor
    - docs and runtime behavior are consistent
    - compatibility behavior (if retained) is tested and time-boxed

- `S6.4-T4` Protected/public endpoint policy codification
  - Files:
    - `dashboard/app/routes/web.py`
    - `dashboard/tests/test_web.py`
    - `README.md`
    - `SLICE_TRACKER.md`
  - Work:
    - define canonical list of public read endpoints vs operator-protected endpoints
    - enforce route-level checks and add regression coverage so policy drift is caught early
  - Done when:
    - policy is documented in one source of truth and reflected in route behavior
    - tests fail on accidental protection removal or accidental public exposure

**Test Matrix (Must Pass for 6.4):**

- auth matrix:
  - no token -> `401` on HTML/API mutations
  - viewer token -> `403` on operator/admin mutations
  - operator/admin token -> success on authorized actions
- redaction matrix:
  - `/api/v1/nodes`, `/api/v1/nodes/{id}`, and HTML settings/detail contexts exclude bearer secrets
  - ensure legacy fields that remain exposed are explicitly allowlisted
- contract matrix:
  - request schemas/docs match runtime behavior for actor attribution
  - deprecated input fields (if temporarily accepted) do not affect persisted audit actor

**Recommended PR Order for 6.4:**

1. `PR-A` -> `S6.4-T1` (route auth parity + tests)
2. `PR-B` -> `S6.4-T2` (redaction contract + tests)
3. `PR-C` -> `S6.4-T3` + `S6.4-T4` (schema/docs/policy codification + regression suite)

### Recommended Sequence and Gates

- Sequence: `6.1 -> 6.2 -> 6.3` (security boundary first, identity second, observability third).
- Merge gate between sub-slices:
  - 6.1 must land before any additional fleet mutation features.
  - 6.2 must land before removing legacy hostname fallback.
  - 6.4 should land before any external exposure of operator/read APIs.
  - 6.3 and 6.4 must both land before declaring Slice 6 `done`.
- Release gate:
  - promote to `released` only after mixed-fleet migration smoke + operator auth smoke are both recorded.

### Latest Update

- 2026-03-12
  - PR: N/A (planning update)
  - Added Slice 6 based on post-Slice-5 gap analysis of current implementation.
  - Key deferred risks explicitly accepted into Slice 6:
    - control-plane fleet mutation endpoints currently lack enforced operator auth boundaries
    - security audit actor currently trusts caller payload values
    - refresh/ingest identity matching remains hostname-centric and collision-prone
    - agent/push background loops currently suppress exceptions without explicit runtime evidence
  - CI evidence: N/A (planning only)
  - Deploy/smoke evidence: N/A (planning only)
- 2026-03-12
  - Slice 6.1 completed:
    - Added operator-token auth (`DASHBOARD_OPERATOR_CREDENTIALS`) with role model (`viewer`, `operator`, `admin`).
    - Enforced authZ boundaries:
      - revoke endpoint now requires `admin`
      - bulk-update endpoint now requires `operator` or `admin`
    - Audit actor attribution now derives from authenticated principal; caller-supplied actor payload is ignored.
    - Added request correlation ID capture in audit metadata for protected mutations.
    - Documented protected vs read-only API policy in README.
  - Test evidence:
    - auth required / role boundary tests for revoke and bulk endpoints
    - spoofed actor ignored test coverage
    - read-only endpoint regression test coverage
    - local suite status: `dashboard: 31 passed`, `agent: 9 passed`
- 2026-03-12
  - Slice 6.2 completed:
    - Added stable `agent_id` identity binding across enrollment, token refresh, and ingest paths.
    - Implemented dual-path compatibility:
      - prefer `agent_id` when present
      - fallback to hostname for legacy agents during migration window
    - Added identity conflict protections:
      - reject agent-id rebinding collisions (`409 Agent identity conflict`)
      - reject agent-id/hostname mismatch on refresh and ingest (`409 Identity binding mismatch`)
    - Added DB support:
      - `nodes.agent_id`
      - unique partial index for non-empty `agent_id`
    - Added agent-side durable identity:
      - `AGENT_ID` and `AGENT_ID_FILE`
      - generated/persisted `agent_id` fallback when unset
      - identity propagated through enroll/refresh/push
  - Test evidence:
    - identity conflict and mismatch tests on dashboard APIs
    - migration/index coverage in DB migration tests
    - agent identity persistence + payload propagation tests
- 2026-03-12
  - Post-6.2 architecture review identified remaining gaps not yet captured as a dedicated sub-slice:
    - web settings mutation routes still lack explicit operator authorization parity
    - node detail read surfaces still query/pass full node records (`SELECT *`) including secret token columns
    - request models still include optional `actor` fields even though actor is principal-derived at runtime
  - Added Slice 6.4 to track auth-surface parity and secret exposure minimization.
  - CI evidence: N/A (planning/analysis update)
  - Deploy/smoke evidence: N/A (planning only)
- 2026-03-12
  - Added planning-only ticket decomposition for Slice 6.4 (`S6.4-T1` to `S6.4-T4`):
    - file-level implementation map
    - acceptance criteria per ticket
    - mandatory 6.4 test matrix
    - recommended PR ordering for low-risk rollout
  - CI evidence: N/A (planning only)
  - Deploy/smoke evidence: N/A (planning only)
- 2026-03-12
  - Slice 6.4 completed:
    - Enforced operator auth parity on HTML mutation routes (`/settings/nodes/save`, `/settings/nodes/{id}/toggle`).
    - Added viewer-level protection to sensitive detail routes (`/nodes/{id}`, `/api/v1/nodes/{id}`, `/api/v1/nodes/{id}/metrics`, `/api/v1/webhooks/deliveries`).
    - Implemented node-read redaction contract by removing `token`/`previous_token` exposure from API payloads and template contexts.
    - Updated node form behavior to avoid displaying existing bearer token values and preserve token on update when left blank.
    - Removed `actor` fields from protected mutation request schemas and validated OpenAPI contract parity.
    - Codified protected/public endpoint policy in README.
  - Test evidence:
    - settings mutation auth matrix (`401` unauth, `403` viewer, success for operator/admin)
    - sensitive detail route auth regression coverage
    - redaction checks for API payloads and rendered HTML/template responses
    - OpenAPI schema checks confirming no caller-controlled `actor` mutation fields
- 2026-03-13
  - Slice 6.3 completed:
    - Replaced silent background-loop exception swallowing with structured logging in:
      - dashboard poller paths (poll failures, services fetch failures, webhook delivery failures)
      - agent enrollment loop
      - agent push loop
    - Added bounded runtime loop telemetry counters and recent error buffers:
      - dashboard poller diagnostics include poll/service/cleanup/webhook counters and recent error samples
      - agent runtime diagnostics include enrollment and push loop counters + last error
    - Added diagnostics API surfaces:
      - dashboard `GET /api/v1/diagnostics/loops` (viewer+ protected)
      - agent `GET /api/v1/diagnostics/loops` (agent bearer-token protected)
  - Test evidence:
    - dashboard:
      - diagnostics endpoint auth + payload coverage
      - poller telemetry progression coverage across success/failure paths
      - webhook diagnostics coverage for delivery attempts/failures
      - local suite status: `41 passed`
    - agent:
      - enrollment loop telemetry coverage (failure + recovery)
      - push loop telemetry coverage (failure + recovery)
      - diagnostics endpoint payload contract coverage
      - local suite status: `12 passed`

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
- 2026-03-12 post-release architecture consistency audit:
  - Tracker status mismatch corrected: Slice 5 section status aligned to `released`.
  - Identified control-plane boundary gap:
    - fleet mutation and token revocation endpoints are callable without operator authentication/authorization.
  - Identified audit attribution gap:
    - security audit actor values are currently caller-provided, not principal-derived.
  - Identified identity-hardening gap:
    - refresh/ingest flows still anchor primarily on hostname lookup with no enforced uniqueness constraint contract at DB level.
  - Identified observability gap:
    - agent enroll/refresh/push loops swallow runtime exceptions, reducing incident diagnosability.
  - These risks are now tracked as Slice 6 scope.
- 2026-03-12 post-6.2 security surface audit:
  - Confirmed previously identified 6.1 and 6.2 risks are implemented in code paths for protected mutation APIs and identity binding.
  - Identified remaining exposure risks:
    - settings HTML mutation routes are not yet covered by the same explicit operator auth checks as protected API mutation routes
    - some node read payloads still originate from `SELECT * FROM nodes`, increasing accidental secret-leak risk
    - API models retain optional `actor` request fields despite principal-derived audit attribution
  - These residual risks are now tracked as Slice 6.4 scope.
