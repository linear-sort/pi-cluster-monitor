# Raspberry Pi Cluster Monitoring Dashboard

A lightweight, self-hosted cluster monitoring system for Raspberry Pi devices on a LAN.

Planning and execution tracker: see `SLICE_TRACKER.md`.

## Features

- FastAPI dashboard with server-rendered Jinja2 pages
- Lightweight FastAPI agent for each Raspberry Pi
- SQLite-backed node config, metrics, and alert history
- HTMX partial updates for near real-time cluster status
- Polling service with token-authenticated metrics collection
- Threshold and offline alerts (info/warning/critical)
- Simple node management UI (add/edit/enable/disable)

## Project Structure

```text
pi-cluster-monitor/
|- dashboard/
|- agent/
|- docker-compose.dashboard.yml
|- docker-compose.agent.yml
|- docker-compose.dashboard.dev.yml
|- docker-compose.agent.dev.yml
|- deploy/
`- README.md
```

## Requirements

- Docker + Docker Compose plugin
- Python 3.10+ (only needed for non-Docker local development/tests)
- Linux for Raspberry Pi agent deployments
- Security requirement: agent auth tokens must support periodic refresh/rotation (tracked in `SLICE_TRACKER.md`, Slice 1).

## Docker Quick Start (Recommended)

This project is now Docker-first for easier rollout across Raspberry Pis.

### Compose file roles

- `docker-compose.dashboard.yml` and `docker-compose.agent.yml`: deployment files using `image:`.
- `docker-compose.dashboard.dev.yml` and `docker-compose.agent.dev.yml`: development files using `build:`.

### 1) Start the central dashboard host (deployment mode)

```bash
docker compose -f docker-compose.dashboard.yml pull
docker compose -f docker-compose.dashboard.yml up -d
```

Dashboard will be available at [http://localhost:8000](http://localhost:8000).

### 2) Start an agent on each Raspberry Pi (deployment mode)

Copy the repo (or just the needed files) to each Pi, then edit `docker-compose.agent.yml`:

- Set `AGENT_TOKEN` to your shared/per-node token
- Optionally set `AGENT_NAME`
- Optionally set `AGENT_SERVICES`
- Set `AGENT_DASHBOARD_URL` and `AGENT_ENROLL_SECRET` to enable automatic enrollment

Then run:

```bash
docker compose -f docker-compose.agent.yml up -d
```

To use custom image tags:

```bash
cp images.env.example .env
# edit .env with your image names/tags
docker compose -f docker-compose.dashboard.yml pull
docker compose -f docker-compose.dashboard.yml up -d
```

### 3) Register nodes in dashboard

In **Settings**, add each Pi node IP/token and poll configuration.
For secure transport, you can enable per-node TLS in Settings and optionally disable certificate verification for self-signed lab setups.
For stricter trust, set a per-node CA bundle path (`tls_ca_path`) and keep TLS verification enabled.

## Local Python Setup (Optional)

```bash
cd dashboard
python -m venv .venv
source .venv/bin/activate  # Linux/macOS
# .venv\Scripts\activate   # Windows PowerShell
pip install -r requirements.txt
pip install -r requirements-dev.txt  # for tests
cp ../deploy/dashboard.env.example .env
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Open [http://localhost:8000](http://localhost:8000).

## Agent Setup (non-Docker)

```bash
cd agent
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp ../deploy/agent.env.example .env
uvicorn app.main:app --host 0.0.0.0 --port 8001
```

## Initial Flow

1. Start an agent on each Pi with a shared or per-node token.
2. Start the dashboard.
3. In **Settings**, add nodes (name, hostname, IP, token, role, poll interval).
4. Dashboard polling begins automatically and stores metrics + alerts in SQLite.

## Multi-Arch Image Build (Optional)

To publish reusable images for ARM/x86 hosts:

```bash
docker buildx build --platform linux/arm64,linux/amd64 -f dashboard/Dockerfile -t yourrepo/pi-dashboard:latest --push .
docker buildx build --platform linux/arm64,linux/amd64 -f agent/Dockerfile -t yourrepo/pi-agent:latest --push .
```

## Automatic GHCR Publishing (GitHub Actions)

Workflow file: `.github/workflows/docker-publish.yml`

What it does:

- Triggers on:
  - tag push matching `v*` (example: `v0.1.0`)
  - GitHub Release publish
  - manual run (`workflow_dispatch`)
- Builds and pushes:
  - `ghcr.io/<owner>/pi-dashboard`
  - `ghcr.io/<owner>/pi-agent`
- Publishes multi-arch manifests for `linux/amd64` and `linux/arm64`.
- Runs dashboard + agent pytest suites first; images are pushed only if tests pass.

Release flow:

```bash
git tag v0.1.0
git push origin v0.1.0
```

After the workflow finishes, point compose image vars to your GHCR tags:

```bash
# in .env at project root
DASHBOARD_IMAGE=ghcr.io/<owner>/pi-dashboard:v0.1.0
AGENT_IMAGE=ghcr.io/<owner>/pi-agent:v0.1.0
```

## Continuous Test Workflow

Workflow file: `.github/workflows/ci-tests.yml`

- Runs on pushes, pull requests, and manual trigger.
- Executes:
  - dashboard test suite
  - agent test suite
- Default CI mode runs `pytest -m "not slow"` for fast feedback.
- Weekly scheduled runs execute full suites.
- Manual full run:
  - open **Actions -> CI Tests -> Run workflow**
  - set `full_suite=true`
- Pull requests also run Docker smoke builds (dashboard + agent, no push) to catch Dockerfile/runtime regressions early.

## Branch Protection (Recommended)

In GitHub repo settings:

1. Go to **Settings -> Branches -> Add branch protection rule**.
2. Target your main branch (for example `main`).
3. Enable:
   - **Require a pull request before merging**
   - **Require status checks to pass before merging**
   - **Require branches to be up to date before merging**
4. Add these required checks:
   - `dashboard-tests`
   - `agent-tests`
5. Optional hardening:
   - **Require conversation resolution before merging**
   - **Restrict who can push to matching branches**
   - **Require signed commits**

This ensures broken changes do not merge and keeps release tags safer for `docker-publish.yml`.

## Where to host Docker images

Good options:

- **GitHub Container Registry (GHCR)**: great if this code lives in GitHub; private/public images, fine-grained access tokens.
- **Docker Hub**: easiest to start, broad tooling support, but free-tier limits may apply.
- **GitLab Container Registry**: good if your repo/CI is in GitLab.
- **Cloud registries**: AWS ECR, GCP Artifact Registry, Azure Container Registry for enterprise/cloud-native setups.
- **Self-hosted registry**: run your own `registry:2` or Harbor on your LAN if you want no external dependency.

For most Pi clusters, GHCR or Docker Hub is the practical default.

## Dev Compose (local build mode)

If you want to build locally instead of pulling prebuilt images:

```bash
docker compose -f docker-compose.dashboard.dev.yml up -d --build
docker compose -f docker-compose.agent.dev.yml up -d --build
```

## Configuration

### Dashboard env vars

- `DASHBOARD_DB_PATH` (default: `dashboard/data/cluster.db`)
- `DASHBOARD_POLL_BASE_SECONDS` (default: `2`)
- `DASHBOARD_HTTP_TIMEOUT_SECONDS` (default: `4`)
- `DASHBOARD_AGENT_DEFAULT_PORT` (default: `8001`)
- `DASHBOARD_ENROLL_SECRET` (default: `changeme-enroll`)
- `DASHBOARD_TOKEN_TTL_SECONDS` (default: `86400`)
- `DASHBOARD_TOKEN_GRACE_SECONDS` (default: `300`)
- `DASHBOARD_POLL_FAILURE_THRESHOLD` (default: `3`)
- `DASHBOARD_POLL_CIRCUIT_COOLDOWN_SECONDS` (default: `60`)
- `DASHBOARD_METRIC_RETENTION_HOURS` (default: `48`)
- `DASHBOARD_ALERT_RETENTION_DAYS` (default: `14`)
- `DASHBOARD_SERVICE_RETENTION_DAYS` (default: `7`)
- `DASHBOARD_CLEANUP_INTERVAL_SECONDS` (default: `300`)

### Agent env vars

- `AGENT_TOKEN` (required, default: `changeme`)
- `AGENT_NAME` (optional display name override)
- `AGENT_SERVICES` (optional comma-separated list for `/api/v1/services`)
- `AGENT_DASHBOARD_URL` (dashboard base URL for auto-enrollment)
- `AGENT_ENROLL_SECRET` (must match `DASHBOARD_ENROLL_SECRET`)
- `AGENT_ENROLL_ENABLED` (default: `true`)
- `AGENT_ENROLL_RETRY_SECONDS` (default: `10`)
- `AGENT_TOKEN_REFRESH_ENABLED` (default: `true`)
- `AGENT_TOKEN_REFRESH_SECONDS` (default: `3600`)
- `AGENT_TOKEN_FILE` (default: `agent_token.txt`)

## API Endpoints

### Agent

- `GET /health`
- `GET /api/v1/metrics`
- `GET /api/v1/services`

Authenticated via `Authorization: Bearer <token>`.

### Dashboard

- `GET /cluster` overview
- `GET /nodes/{id}` node detail
- `GET/POST /settings` + node management form actions
- HTMX partials under `/partials/*`
- `GET /api/v1/cluster/summary`
- `GET /api/v1/nodes`
- `GET /api/v1/nodes/{id}`
- `GET /api/v1/nodes/{id}/metrics`
- `GET /api/v1/alerts`
- `POST /api/v1/enroll` (agent bootstrap enrollment)
- `POST /api/v1/token/refresh` (agent token rotation/refresh)

## Database

SQLite schema is created automatically on startup with the following tables:

- `nodes`
- `metric_samples`
- `alerts`
- `alert_events`
- `services`

## systemd Units

Sample unit files are in `deploy/systemd/`:

- `pi-dashboard.service`
- `pi-agent.service`

Adjust paths, users, and env file locations to your deployment.

## Notes

- This MVP is LAN-focused and read-only for metrics.
- No shell/control endpoints are implemented by design.
- Charts are rendered with Chart.js from a CDN for minimal frontend overhead.

## Run Tests

```bash
cd dashboard
pytest
```

## Root Startup Scripts

From the project root:

```powershell
.\start.ps1 dashboard
.\start.ps1 agent
```

Optional flags:

- `-Host 0.0.0.0`
- `-Port 8000` (dashboard default: `8000`, agent default: `8001`)
- `-WithDevDeps` to also install `requirements-dev.txt` when present
- `-Debug` to run that module's tests before starting Uvicorn

Linux/macOS:

```bash
chmod +x ./start.sh
./start.sh dashboard
./start.sh agent
```

Optional:

- `./start.sh dashboard 0.0.0.0 8000 --with-dev-deps`
- `./start.sh agent 0.0.0.0 8001 --debug`

```bash
cd agent
pip install -r requirements.txt -r requirements-dev.txt
pytest
```