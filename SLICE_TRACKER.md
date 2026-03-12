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
- Deployment completion requires:
  - images published (GHCR)
  - compose env tags updated
  - post-deploy smoke validation completed

---

## Slice Overview

| Slice | Title | Status | Scope Summary |
|---|---|---|---|
| 0 | Pipeline Foundation | in_progress | Harden CI/CD, Docker build smoke checks, publish gating |
| 1 | Automatic Auth Bootstrap | planned | Agent enrollment and auto token provisioning |
| 2 | Connectivity Resilience | planned | Heartbeats, richer node state reasons, retry/backoff/circuit logic |
| 3 | Transport Trust (TLS) | planned | HTTPS polling and trust verification options |
| 4 | Hybrid Push/Pull Metrics | planned | Agent push ingest path with replay protection and dedupe |
| 5 | Ops Hardening + Fleet Controls | planned | Token rotation/revocation, bulk ops, alert/webhook hardening |

---

## Slice 0 - Pipeline Foundation

**Status:** `in_progress`  
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

### Checklist

- [x] Markers defined in pytest config and used in tests
- [x] CI test jobs updated
- [x] Docker build smoke job added
- [ ] Publish gating validated on a tag workflow run

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

---

## Slice 1 - Automatic Auth Bootstrap

**Status:** `planned`  
**Goal:** Remove manual token distribution by enabling one-step agent enrollment.

### Deliverables

- Dashboard enrollment endpoint(s) for one-time registration.
- Per-node issued token lifecycle for enrolled agents.
- Agent enrollment flow with retry/backoff until successful.
- Node enrollment status in dashboard UI/API.

### Tests Required

- Dashboard:
  - enrollment validation and one-time semantics
  - token issuance and node linkage
- Agent:
  - successful enrollment stores token
  - invalid enroll secret handling
  - retry behavior on transient dashboard failure
- Integration:
  - fresh agent enrolls and is polled successfully end-to-end

### Deployment Gate

- New auth/enrollment tests pass in CI.
- Publish workflow blocked on those tests.
- Deployment notes include enrollment secret rotation guidance.

### Checklist

- [ ] Enrollment API + DB state implemented
- [ ] Agent enrollment client implemented
- [ ] UI/API enrollment status added
- [ ] Tests added and green
- [ ] Deployed image tags promoted

---

## Slice 2 - Connectivity Resilience

**Status:** `planned`  
**Goal:** Improve reliability and observability on unstable LANs.

### Deliverables

- Heartbeat tracking separate from metrics sampling.
- Poll classification:
  - offline
  - auth failure
  - timeout
  - metrics parse error
- Retry/backoff with jitter and circuit breaker behavior.

### Tests Required

- Poller unit tests for state transitions and backoff math.
- API/UI tests for correct status rendering.
- Integration tests using mocked failure modes.

### Deployment Gate

- Failure-path tests green and stable.
- Alert behavior verified for each failure class.

### Checklist

- [ ] Heartbeat data model + API support
- [ ] Poll state classification implemented
- [ ] Backoff/circuit logic implemented
- [ ] Tests added and green
- [ ] Deployed image tags promoted

---

## Slice 3 - Transport Trust (TLS)

**Status:** `planned`  
**Goal:** Add optional secure transport verification for agent communication.

### Deliverables

- HTTPS support for agent endpoints.
- Dashboard node-level TLS settings (mode, CA/fingerprint).
- Optional strict verification policies.

### Tests Required

- Unit tests for TLS config handling.
- Integration tests:
  - valid cert path succeeds
  - invalid/untrusted cert path fails with clear reason
- Regression tests for non-TLS mode compatibility.

### Deployment Gate

- TLS + non-TLS test paths pass in CI.
- Clear rollout/migration docs for existing nodes.

### Checklist

- [ ] TLS options added to node config
- [ ] HTTPS polling support implemented
- [ ] Trust verification behavior implemented
- [ ] Tests added and green
- [ ] Deployed image tags promoted

---

## Slice 4 - Hybrid Push/Pull Metrics

**Status:** `planned`  
**Goal:** Support push ingest for networks where pull polling is constrained.

### Deliverables

- Dashboard ingest endpoint for agent metric push.
- Replay protection (timestamp/nonce/signature).
- Deduplication across push/pull sources.
- Per-node mode toggle: pull, push, hybrid.

### Tests Required

- Ingest auth/signature validation tests.
- Replay attack rejection tests.
- Deduplication/idempotency tests.
- Integration tests for push-only and hybrid scenarios.

### Deployment Gate

- Ingest and dedupe tests green.
- Backward compatibility verified for pull-only nodes.

### Checklist

- [ ] Ingest API implemented
- [ ] Replay protection implemented
- [ ] Deduplication implemented
- [ ] Mode toggles added
- [ ] Tests added and green
- [ ] Deployed image tags promoted

---

## Slice 5 - Ops Hardening + Fleet Controls

**Status:** `planned`  
**Goal:** Make day-2 operations safe and scalable for larger Pi fleets.

### Deliverables

- Token rotation and revocation workflows.
- Bulk node operations (enable/disable, intervals, role updates).
- Alert/webhook hardening and retry visibility.
- Migration support for new auth/ops fields.

### Tests Required

- Rotation and grace window tests.
- Revocation enforcement tests.
- Bulk operation correctness and rollback tests.
- Migration tests for fresh + existing databases.

### Deployment Gate

- Security and migration suites pass.
- Release checklist confirms no breaking upgrade path.

### Checklist

- [ ] Rotation/revocation implemented
- [ ] Bulk ops implemented
- [ ] Webhook hardening implemented
- [ ] Migration tests added and green
- [ ] Deployed image tags promoted

---

## Update Protocol (How to Maintain This File)

On each PR:

1. Update the relevant slice `Status`.
2. Check/uncheck checklist items to reflect implementation reality.
3. Add a short note under the slice:
   - date
   - PR number
   - what changed
   - test evidence
4. When deployed, change status to `released` and record image tags.
