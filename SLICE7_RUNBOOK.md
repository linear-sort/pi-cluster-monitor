# Slice 7 Security Runbook

Operational runbook for Slice 7 (`Secret Lifecycle + Auth Abuse Hardening`).

## Scope

This runbook covers:

- operator credential lifecycle (issue, rotate, revoke, expire)
- auth lockout/throttle behavior and break-glass recovery
- migration path for legacy plaintext secret files
- release smoke checklist for Slice 7 promotion

Execution checklist for promotion steps and evidence capture:

- `SLICE7_RELEASE_CHECKLIST.md`

## Prerequisites

- Dashboard and agent images deployed from the same release candidate.
- Access to deployment environment variables for dashboard and agent services.
- Ability to restart dashboard and agent services.

## Credential Contract

Dashboard operator credentials are configured through `DASHBOARD_OPERATOR_CREDENTIALS`.

Format:

`token:principal:role[:expires_at_epoch_or_iso][:status]`

- `role` must be one of `viewer`, `operator`, `admin`
- `status` defaults to `active` and can be `revoked`
- `expires_at` is optional but recommended for all non-break-glass credentials

Example:

`admin-token-v2:alice-admin:admin:2026-04-01T00:00:00Z:active`

## Rotation Procedure (No Downtime)

1. Add new credentials while keeping old credentials active in `DASHBOARD_OPERATOR_CREDENTIALS`.
2. Restart dashboard to load the updated credential map.
3. Validate new credentials on protected endpoints:
   - `GET /api/v1/diagnostics/loops` (viewer+)
   - one mutation endpoint such as `POST /api/v1/nodes/bulk-update` (operator/admin)
4. Update clients/automation to use new credentials.
5. Mark old credentials as `revoked` (or set expired timestamp) in env value.
6. Restart dashboard.
7. Validate old credentials now return `401` and audit/diagnostics counters reflect revoked/expired failures.

## Break-Glass Recovery (Lockout/Throttle)

Symptoms:

- repeated protected calls return `429 Too many auth failures; retry later`

Immediate recovery path:

1. Stop invalid auth traffic source (misconfigured automation, stale token, probes).
2. Use a valid admin credential from secure break-glass storage.
3. Wait for `DASHBOARD_OPERATOR_AUTH_LOCKOUT_SECONDS` window or restart dashboard to clear in-memory auth buckets.
4. Validate protected read endpoint with break-glass credential.
5. Rotate compromised/stale credentials and restore normal credential set.

Hardening guidance:

- keep one emergency admin credential with short expiry and restricted storage/access
- never reuse break-glass credentials as day-to-day automation tokens

## Secret-at-Rest Migration Path

### Dashboard node tokens

- Enable `DASHBOARD_NODE_TOKEN_KEY` (32+ random chars recommended).
- Any newly written node tokens are stored as `enc:v2`.
- Existing plaintext rows continue to work and are resealed during normal token write/update paths.
- Legacy `enc:v1` values remain readable for backward compatibility.

### Agent local files

- Enable `AGENT_LOCAL_SECRET_KEY` on each agent.
- `AGENT_TOKEN_FILE` and `AGENT_ID_FILE` are stored as `enc:v2` after next write.
- Existing plaintext files remain readable during migration.

## Slice 7 Release Smoke Checklist

- Dashboard tests pass (`pytest` in `dashboard/`).
- Agent tests pass (`pytest` in `agent/`).
- Protected endpoints enforce:
  - `401` for missing/invalid/expired/revoked credentials
  - `429` after repeated failures from same source bucket
- Secret-at-rest validation:
  - dashboard `nodes.token` and `nodes.previous_token` stored as encrypted values when key configured
  - agent token/id files stored as encrypted values when local key configured
- Rotation validation:
  - new operator credential works before old is revoked
  - old credential fails after revocation/expiry
- Break-glass validation:
  - emergency admin path can recover from lockout state

