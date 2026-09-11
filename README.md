# ansible-lab

A self-contained lab that models a multi-tier production environment and deploys to it with Ansible — zero-downtime rolling releases, automatic rollback on failed health checks, and a full Prometheus/Grafana/Loki observability stack. The whole fleet runs as Docker containers on one machine, so the entire system can be stood up and torn down locally.

The point of the project is not "here is some Ansible." It is the failure handling: what happens when a deploy goes wrong, and how the system refuses to put a broken server back into rotation.

---

## Contents

- [What this demonstrates](#what-this-demonstrates)
- [Architecture](#architecture)
- [Repository layout](#repository-layout)
- [Prerequisites](#prerequisites)
- [Quick start](#quick-start)
- [Deployment model](#deployment-model)
- [Roles](#roles)
- [Inventory and variables](#inventory-and-variables)
- [CI/CD](#cicd)
- [Observability](#observability)
- [Operations runbook](#operations-runbook)
- [Ports reference](#ports-reference)
- [Known limitations and roadmap](#known-limitations-and-roadmap)

---

## What this demonstrates

- **Zero-downtime rolling deploys** — web servers are updated one at a time (`serial: 1`), each drained from the load balancer before it is touched and re-enabled only after it proves healthy.
- **Release-based deployment with automatic rollback** — every deploy lands in a timestamped release directory and is activated by flipping a symlink. If post-deploy health checks fail, a `rescue` block restores the previous release and restarts the app.
- **Failing safe rather than failing open** — if a web server cannot pass its final health check, it is deliberately left drained from HAProxy and the deploy fails loudly. A broken node never rejoins the pool.
- **Traceable releases** — the deployed release ID is the Git commit SHA, so `/opt/myapp/current` on any host maps back to an exact commit.
- **Ansible structure that scales** — six roles with defaults, handlers, templates, and Jinja2-generated config; group_vars separated by tier; Ansible Vault for database credentials.
- **SLO-based monitoring** — not just CPU and memory thresholds, but recorded availability and p95 latency rules with explicit SLO breach conditions.
- **CI/CD separation** — a cheap validation workflow on every push and PR, and a deploy workflow that only runs on a self-hosted runner after CI passes on `main`.

---

## Architecture

```
                          ┌──────────────────┐
   host :8080  ──────────▶│   lb01 (HAProxy) │
                          │  roundrobin      │
                          │  httpchk /health │
                          │  admin socket    │
                          └────────┬─────────┘
                                   │
                  ┌────────────────┴────────────────┐
                  ▼                                 ▼
        ┌───────────────────┐             ┌───────────────────┐
        │      web01        │             │      web02        │
        │  Nginx :80        │             │  Nginx :80        │
        │    └─▶ Gunicorn   │             │    └─▶ Gunicorn   │
        │        Flask :5000│             │        Flask :5000│
        │  node_exporter    │             │  node_exporter    │
        └─────────┬─────────┘             └─────────┬─────────┘
                  │                                 │
                  └────────────────┬────────────────┘
                                   ▼
                          ┌──────────────────┐
                          │   db01 (MySQL)   │
                          │   taskdb         │
                          └──────────────────┘

   Observability (separate containers)
   ┌────────────┐  ┌──────────┐  ┌──────────────┐  ┌────────┐  ┌───────┐
   │ Prometheus │─▶│ Grafana  │  │ Alertmanager │  │ Alloy  │─▶│ Loki  │
   │   :9090    │  │  :3000   │  │    :9093     │  │ (logs) │  │ :3100 │
   └────────────┘  └──────────┘  └──────────────┘  └────────┘  └───────┘
```

All four managed hosts (`web01`, `web02`, `db01`, `lb01`) are built from the same SSH-enabled image and are reachable from the control node over mapped SSH ports on `127.0.0.1`. Ansible treats them exactly as it would real servers.

Request path: client → HAProxy on `lb01:80` → Nginx on a web host `:80` → Gunicorn/Flask on `127.0.0.1:5000`.

---

## Repository layout

```
.
├── .github/workflows/
│   ├── ci.yml                  # syntax check + inventory validation
│   └── deploy.yml              # gated CD on self-hosted runner
├── app/
│   ├── app.py                  # Flask tasks API with Prometheus metrics
│   └── requirements.txt
├── docker/                     # base image for all managed hosts
├── inventory/
│   ├── hosts.ini               # web / database / loadbalancer groups
│   └── group_vars/
│       ├── all.yml
│       ├── web.yml             # app_name, app_port, app_directory
│       └── database_vault.yml  # Ansible Vault encrypted
├── monitoring/
│   ├── prometheus.yml          # scrape config
│   ├── alerts.yml              # infrastructure + application alerts
│   ├── slo.yml                 # recording rules and SLO breach conditions
│   ├── alertmanager.yml
│   └── alloy-config.alloy      # Docker log collection → Loki
├── playbooks/
│   ├── site.yml                # full deploy, rolling
│   └── recover-web.yml         # targeted remediation for a down web host
├── roles/
│   ├── application/            # release dirs, venv, Supervisor, rollback
│   ├── common/
│   ├── database/
│   ├── loadbalancer/           # HAProxy
│   ├── nginx/
│   └── node_exporter/
├── ansible.cfg
├── docker-compose.yml
└── requirements.yml            # ansible.posix, community.mysql
```

---

## Prerequisites

- Docker and Docker Compose
- Python 3 with `ansible` installed (a virtualenv is recommended)
- An SSH keypair for Ansible to authenticate with the containers
- `socat` inside the load balancer container (used to drive the HAProxy admin socket)

---

## Quick start

**1. Export the public key the containers will trust**

```bash
export ANSIBLE_PUBLIC_KEY="$(cat ~/.ssh/id_ed25519.pub)"
```

**2. Build and start the fleet**

```bash
docker compose up -d --build
```

This brings up `web01`, `web02`, `db01`, `lb01` and the monitoring stack.

**3. Install Ansible collections**

```bash
ansible-galaxy collection install -r requirements.yml
```

**4. Confirm connectivity**

```bash
ansible all -m ping
```

**5. Deploy**

```bash
ansible-playbook playbooks/site.yml --ask-vault-pass
```

**6. Verify through the load balancer**

```bash
curl http://localhost:8080/health
curl http://localhost:8080/api/tasks
```

Repeated calls to `/health` return different `hostname` values as HAProxy round-robins between `web01` and `web02`.

**Tear down**

```bash
docker compose down -v
```

---

## Deployment model

### Play order in `site.yml`

| Play | Hosts | Roles |
|---|---|---|
| Deploy application infrastructure | `all` | `common`, `node_exporter` |
| Configure database | `database` | `database` |
| Configure load balancer | `loadbalancer` | `common`, `loadbalancer` |
| Rolling deploy web servers | `web` (`serial: 1`) | `application`, `nginx` |

Baseline configuration and metrics exporters go everywhere first. The database and load balancer are configured before any web server is touched, so the backend and the routing layer are ready before traffic-serving nodes start cycling.

### The rolling deploy, one host at a time

Because `serial: 1` is set, the following runs against a single web host before moving on:

1. **Drain.** A `pre_task` writes `set server web_backend/<host> state drain` to the HAProxy admin socket, delegated to the load balancer. Existing connections finish; no new ones arrive.
2. **Pause** briefly so in-flight connections can complete.
3. **Deploy** the `application` role, then the `nginx` role.
4. **Verify through Nginx** — `GET /health` and `GET /api/tasks` on `127.0.0.1:80`, each retried up to 10 times with a 3-second delay. This tests the real request path, not just the Gunicorn socket.
5. **Always re-check health** in an `always` block, so this runs even if the deploy steps raised.
6. **Re-enable only if healthy** — `set server ... state ready` is sent to HAProxy only when the final health check returned 200.
7. **Fail the deploy if unhealthy** — the play fails with an explicit message and the host *remains drained*. A broken server does not go back into the pool, and because the run stops, the remaining web servers are never touched.

That last point is the safety property that matters: a bad release can take down at most one node, and the rest of the fleet keeps serving the previous version.

### Release layout and rollback

The `application` role uses a release-directory strategy:

```
/opt/myapp/
├── releases/
│   ├── <commit-sha-1>/     # app.py, requirements.txt
│   └── <commit-sha-2>/
├── current -> releases/<commit-sha-2>
├── previous_release        # records the prior symlink target
├── venv/
├── .env                    # mode 0600, templated
├── start-app.sh
├── access.log
└── error.log
```

The release ID comes from `app_release_id`, which defaults to the `GITHUB_SHA` environment variable and falls back to `local` for manual runs. CI passes the deployed commit SHA explicitly.

Deploy sequence within the role:

1. Record the current symlink target into `previous_release`.
2. Point `current` at the new release directory.
3. `supervisorctl reread` / `update` / `restart myapp`.
4. Poll `/health` and `/api/tasks` on `127.0.0.1:5000` with retries.

If any step in that block fails, the `rescue` block:

1. Reads `previous_release` and repoints the `current` symlink back to it.
2. Restarts the application.
3. Fails the play with `"Deployment failed health checks. Previous release restored."`

So there are two independent layers of protection: the role rolls the *code* back on the host, and the playbook keeps the *host* out of rotation.

The application runs under Supervisor, so it also survives process crashes between deploys.

---

## Roles

| Role | Applies to | Responsibility |
|---|---|---|
| `common` | all, loadbalancer | Baseline packages and host configuration |
| `node_exporter` | all | Installs and runs Prometheus node_exporter on `:9100` |
| `database` | database | MySQL install, `taskdb` database, `tasks` table, application user |
| `loadbalancer` | loadbalancer | HAProxy with round-robin balancing, `httpchk GET /health`, admin socket at `/run/haproxy/admin.sock` |
| `application` | web | Python venv, release directories, `.env`, Supervisor config, health verification, rollback |
| `nginx` | web | Reverse proxy on `:80` to `127.0.0.1:5000` with standard forwarding headers |

The HAProxy backend is generated from inventory — it loops over `groups['web']` — so adding a web host to `hosts.ini` is enough to put it behind the load balancer.

---

## Inventory and variables

`inventory/hosts.ini` defines three groups, all reachable over mapped SSH ports on localhost as the `ansible` user:

```ini
[web]
web01 ansible_host=127.0.0.1 ansible_port=2221
web02 ansible_host=127.0.0.1 ansible_port=2222

[database]
db01 ansible_host=127.0.0.1 ansible_port=2223

[loadbalancer]
lb01 ansible_host=127.0.0.1 ansible_port=2224 ansible_user=ansible
```

Key variables:

| Variable | Defined in | Default | Purpose |
|---|---|---|---|
| `app_name` | `group_vars/web.yml` | `ansible-demo` | Application identifier |
| `app_port` | `group_vars/web.yml` | `5000` | Gunicorn listen port |
| `app_directory` | `group_vars/web.yml` | `/opt/myapp` | Deployment root |
| `app_release_id` | `roles/application/defaults` | `$GITHUB_SHA` or `local` | Release identifier |
| `app_release_directory` | `roles/application/defaults` | `{{ app_directory }}/releases/{{ app_release_id }}` | This release's directory |
| `app_current_directory` | `roles/application/defaults` | `{{ app_directory }}/current` | Active release symlink |
| `db_password` | `group_vars/database_vault.yml` | vault-encrypted | Database credential |

Database credentials live in `inventory/group_vars/database_vault.yml`, encrypted with Ansible Vault. Run playbooks with `--ask-vault-pass` or `--vault-password-file`.

`ansible.cfg` sets the inventory path and roles path, disables host key checking (appropriate for ephemeral lab containers, not for production), and silences interpreter discovery warnings.

---

## CI/CD

Two workflows, deliberately split.

**`ci.yml` — Ansible CI.** Runs on every push and pull request to `main`, on GitHub-hosted runners. Installs Ansible and the required collections, runs `ansible-playbook playbooks/site.yml --syntax-check`, and validates the inventory with `ansible-inventory --graph`. Fast, cheap, and needs no access to the environment.

**`deploy.yml` — Ansible CD.** Triggered by `workflow_run` when Ansible CI completes on `main`, and gated on `conclusion == 'success'`. Runs on a self-hosted runner with network access to the lab. It:

1. Checks out the exact `head_sha` that CI validated, not whatever `main` points at now.
2. Builds a virtualenv and installs Ansible and collections.
3. Runs `site.yml` with a dedicated deploy key and `-e app_release_id=<head_sha>`.
4. Verifies `/health` and `/api/tasks` through the load balancer on `:8080` with `curl --fail --retry 5`.

Pinning the checkout and the release ID to the same commit SHA means a deploy is always attributable to a specific validated commit, and the post-deploy curl checks gate the pipeline on the system actually serving traffic rather than on Ansible's exit code alone.

---

## Observability

**Metrics.** Prometheus scrapes every 5 seconds:

- `web` job — the Flask app on `web01:5000` and `web02:5000` via `prometheus_flask_exporter`
- `node` job — node_exporter on all four hosts at `:9100`

The application exposes default Flask HTTP metrics plus a custom `app_request_latency_seconds` histogram labelled by method, endpoint, and status.

**Alerts** (`monitoring/alerts.yml`):

| Alert | Condition | For | Severity |
|---|---|---|---|
| `InstanceDown` | `up{job="node"} == 0` | 1m | critical |
| `HighCPU` | CPU utilisation > 80% | 5m | warning |
| `HighMemory` | Memory utilisation > 85% | 5m | warning |
| `HighErrorRate` | 5xx ratio > 5% | 2m | critical |
| `HighLatency` | p95 latency > 500ms | 5m | warning |

**SLOs** (`monitoring/slo.yml`) — recording rules evaluated every 30s:

- `app:request_success_ratio:5m` and `app:request_error_ratio:5m` from 5xx rate over total request rate
- `app:request_latency_p95:5m` from the latency histogram
- `app:availability_slo_breach` — success ratio below 99.9%
- `app:latency_slo_breach` — p95 above 500ms

Separating recording rules from alert rules keeps dashboards and alerts reading the same pre-computed series, and makes the SLO targets explicit and reviewable rather than buried inside alert expressions.

**Logs.** Grafana Alloy reads the Docker socket, collects container logs, and ships them to Loki on `:3100`. Grafana on `:3000` queries both Prometheus and Loki.

---

## Operations runbook

### Recover a web server that has stopped serving

`playbooks/recover-web.yml` is targeted remediation, separate from a full deploy. It checks whether the Gunicorn PID is live, removes a stale PID file if the process is gone, restarts the app detached via `setsid nohup`, and polls `/health` up to 15 times.

```bash
ansible-playbook playbooks/recover-web.yml
```

The host is currently hardcoded to `web01`; override it for another host:

```bash
ansible-playbook playbooks/recover-web.yml -e "target_host=web02"
```

### Inspect HAProxy backend state

```bash
docker exec ansible-lb01 sh -c \
  "echo 'show servers state' | socat unix-connect:/run/haproxy/admin.sock stdio"
```

### Manually drain or re-enable a web server

```bash
docker exec ansible-lb01 sh -c \
  "echo 'set server web_backend/web01 state drain' | socat unix-connect:/run/haproxy/admin.sock stdio"

docker exec ansible-lb01 sh -c \
  "echo 'set server web_backend/web01 state ready' | socat unix-connect:/run/haproxy/admin.sock stdio"
```

If a deploy fails, the affected host is left drained on purpose. Fix the cause and re-run `site.yml` rather than re-enabling it by hand.

### Check which release is live

```bash
ansible web -m command -a "readlink /opt/myapp/current"
```

### Deploy a single host

```bash
ansible-playbook playbooks/site.yml --limit web02 --ask-vault-pass
```

### Edit vaulted variables

```bash
ansible-vault edit inventory/group_vars/database_vault.yml
```

---

## Ports reference

| Service | Host port | Container port | Notes |
|---|---|---|---|
| `web01` SSH | 2221 | 22 | Ansible connection |
| `web01` app | 5001 | 5000 | Gunicorn, bypasses Nginx |
| `web02` SSH | 2222 | 22 | Ansible connection |
| `web02` app | 5002 | 5000 | Gunicorn, bypasses Nginx |
| `db01` SSH | 2223 | 22 | Ansible connection |
| `lb01` SSH | 2224 | 22 | Ansible connection |
| `lb01` HTTP | 8080 | 80 | **Main entry point** |
| Prometheus | 9090 | 9090 | |
| Grafana | 3000 | 3000 | |
| Alertmanager | 9093 | 9093 | |
| Loki | 3100 | 3100 | |
| node_exporter | — | 9100 | Scraped internally |

---

## Application API

A small Flask service backed by MySQL, deliberately minimal so the interesting parts stay in the infrastructure.

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Returns status and the serving hostname — used by HAProxy, Nginx checks, and deploy gates |
| `GET` | `/api/tasks` | Lists tasks from the database |
| `POST` | `/api/tasks` | Creates a task from `{"title": "..."}` |
| `GET` | `/metrics` | Prometheus metrics |

`/health` returning the hostname is what makes round-robin behaviour visible from the client side, and gives deploy verification a way to confirm which node answered.

---

## Known limitations and roadmap

This is a lab, and a few things are scoped down accordingly:

- **`group_vars/all.yml` contains a placeholder `db_password`.** It is a lab default, not a real credential, and the vaulted `database_vault.yml` takes precedence for the database group. It should be removed so the vault is the single source of truth.
- **`host_key_checking = False`** is set for convenience with ephemeral containers. Not appropriate for real infrastructure.
- **`recover-web.yml` hardcodes `web01`** rather than taking a host parameter by default.
- **No `ansible-lint` or `yamllint` in CI** — currently only a syntax check. Linting would catch more before deploy.
- **No Molecule tests** for individual roles.
- **Single load balancer** — `lb01` is a single point of failure by design, to keep the lab small.
- **Grafana dashboards are not provisioned as code** — they need to be recreated manually after a `docker compose down -v`.

Planned next steps: provision Grafana dashboards and datasources as code, add linting and Molecule role tests to CI, parameterise the recovery playbook, and add a blue/green variant alongside the rolling strategy for comparison.
