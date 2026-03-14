# Slice 7 Release Execution Checklist

Use this checklist to promote Slice 7 from `in_progress` to `released`.

Related docs:

- `SLICE_TRACKER.md`
- `SLICE7_RUNBOOK.md`

## Inputs (Fill Before Starting)

- Release tag (follow prior convention): `v0.7.1-slice7-secret-hardening`
- GitHub owner/org: `linear-sort`
- Commit SHA (release target): `8d80b63`
- Deployment environment: `<staging|prod>`
- Operator smoke token principal: `<principal>`

Convention notes (from prior slices):

- `v0.3.1-slice3-trust`
- `v0.5.2-slice5-webhooks`
- `v0.6.3-slice6-telemetry-diagnostics`

## 1) Preflight Local Validation

From repo root:

```powershell
.\.venv\Scripts\python -m pytest dashboard/tests
.\.venv\Scripts\python -m pytest agent/tests
```

Acceptance:

- both commands pass with no failures

## 2) Trigger CI Full Suite (Optional but Recommended)

In GitHub Actions:

- Workflow: `CI Tests`
- Trigger: `Run workflow`
- Set `full_suite=true`

Record:

- workflow run URL
- `dashboard-tests` status
- `agent-tests` status

## 3) Create and Push Release Tag

```powershell
git tag v0.7.1-slice7-secret-hardening
git push origin v0.7.1-slice7-secret-hardening
```

Example:

```powershell
git tag v0.7.1-slice7-secret-hardening
git push origin v0.7.1-slice7-secret-hardening
```

## 4) Verify Image Publish Workflow

Expected workflow:

- `Build and Publish Docker Images` (`.github/workflows/docker-publish.yml`)

Expected jobs:

- `test-dashboard` -> success
- `test-agent` -> success
- `build-and-push` -> success

Expected images:

- `ghcr.io/<owner>/pi-dashboard:<release_tag>`
- `ghcr.io/<owner>/pi-agent:<release_tag>`

Record:

- workflow run URL
- image digest(s)

## 5) Update Deploy Image Tags

Create or update root `.env` from `images.env.example`:

```powershell
Copy-Item images.env.example .env
```

Set:

```dotenv
DASHBOARD_IMAGE=ghcr.io/linear-sort/pi-dashboard:v0.7.1-slice7-secret-hardening
AGENT_IMAGE=ghcr.io/linear-sort/pi-agent:v0.7.1-slice7-secret-hardening
```

Commit these env/tag updates in deployment repo/process as applicable.

## 6) Deploy

Dashboard host:

```powershell
docker compose -f docker-compose.dashboard.yml pull
docker compose -f docker-compose.dashboard.yml up -d
```

Each agent host:

```powershell
docker compose -f docker-compose.agent.yml pull
docker compose -f docker-compose.agent.yml up -d
```

## 7) Post-Deploy Slice 7 Smoke Validation

### A. Control-plane auth behavior

- protected route with valid credential -> `200`
- missing/invalid credential -> `401`
- repeated invalid credential attempts -> `429`

### B. Secret-at-rest behavior

- dashboard tokens persisted encrypted (`enc:v2`) when `DASHBOARD_NODE_TOKEN_KEY` set
- agent token/id files persisted encrypted (`enc:v2`) when `AGENT_LOCAL_SECRET_KEY` set

### C. Rotation behavior

- add new operator credential and validate success
- revoke/expire old credential and validate it fails with `401`

### D. Break-glass recovery behavior

- simulate lockout (`429`) via repeated invalid auth
- recover using break-glass admin path from `SLICE7_RUNBOOK.md`

Record:

- smoke test timestamp
- who executed smoke
- endpoint checks + outcomes
- any deviations and mitigation

## 8) Update `SLICE_TRACKER.md`

Update Slice 7 checklist:

- `[x] Images published (GHCR)`
- `[x] Compose env tags updated`
- `[x] Post-deploy smoke validation completed`

Add a `Latest Update` entry with:

- date
- release tag
- PR number/link (if applicable)
- CI evidence URLs
- publish workflow URL + image tags/digests
- smoke evidence summary
- deferred risk note (or explicit `none`)

## Tracker Entry Template

Copy this into `SLICE_TRACKER.md` under Slice 7 `Latest Update`:

```markdown
- YYYY-MM-DD
  - Slice 7 release promotion completed for `v0.7.1-slice7-secret-hardening`.
  - CI evidence:
    - `CI Tests` run: <url> (`dashboard-tests`: success, `agent-tests`: success)
    - `Build and Publish Docker Images` run: <url> (`test-dashboard`: success, `test-agent`: success, `build-and-push`: success)
  - Image evidence:
    - `ghcr.io/linear-sort/pi-dashboard:v0.7.1-slice7-secret-hardening` digest: `<digest>`
    - `ghcr.io/linear-sort/pi-agent:v0.7.1-slice7-secret-hardening` digest: `<digest>`
  - Deployment evidence:
    - compose tags updated in `.env` to `v0.7.1-slice7-secret-hardening`
    - dashboard + agent services restarted successfully
  - Smoke evidence:
    - auth contract checks (`200/401/429`) passed
    - secret-at-rest checks passed (`enc:v2` observed for dashboard + agent)
    - rotation and break-glass recovery checks passed
  - Deferred risk: none
```

