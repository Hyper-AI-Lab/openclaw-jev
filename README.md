# Aura

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](requirements.txt)
[![OpenClaw](https://img.shields.io/badge/runtime-OpenClaw-0ea5e9.svg)](https://github.com/openclaw/openclaw)
[![Org](https://img.shields.io/badge/org-Hyper--AI--Lab-111827.svg)](https://github.com/Hyper-AI-Lab)

**Aura is a reliability & memory control plane for a production Slack agent.**  
It sits beside [OpenClaw](https://github.com/openclaw/openclaw) and turns every user turn into a durable Temporal workflow: intake routing, process memory, evidence gates, idempotent Slack delivery, and multi-key LLM orchestration.

Built by [Hyper-AI-Lab](https://github.com/Hyper-AI-Lab) · Homepage: [hyperailab.com](https://hyperailab.com/)

---

## Why Aura exists

Chat-agent stacks are great at tools and models — and terrible at **ops truth**:

- Slack replies race with native gateway delivery  
- Sessions forget process context across turns  
- Rate limits stall the whole agent with no fair key rotation  
- “Done” is whatever the model claimed, not what evidence allows  

Aura (the **Reliability & Memory Plane**, RMP) is the sidecar that owns those guarantees while OpenClaw stays the execution engine.

## What you get

| Capability | What Aura provides |
| --- | --- |
| Durable task intake | 3-layer funnel (fast path → vector gate → LLM classify) with `off` / `shadow` / `enforce` modes |
| Workflow control plane | Temporal `GenericTask` / `CatalogTask` workflows, child steps, reconciler + janitor |
| Process memory | Process-scoped recall + promotion; Qdrant vectors (`nv-embed-v1`) |
| Slack ownership | OpenClaw plugin routes DMs to RMP; RMP posts the final reply (no double-send) |
| LLM orchestration | Balanced NVIDIA key rotation, concurrency caps, cooldowns, usage ledger |
| Production gates | Readiness API, hourly canaries, patch verify, backup/restore runbooks |

## Architecture

```mermaid
flowchart LR
  User["Slack user"] --> OC["OpenClaw gateway"]
  OC --> Plugin["rmp_adapter plugin"]
  Plugin -->|"POST /tasks"| API["RMP FastAPI"]
  API --> Temporal["Temporal workflows"]
  Temporal --> Worker["rmp-worker"]
  Worker -->|"hooks/agent"| OC
  Worker --> PG["PostgreSQL"]
  Worker --> Qdrant["Qdrant"]
  Worker -->|"notify_slack_user"| Slack["Slack DM"]
```

| Layer | Role |
| --- | --- |
| **OpenClaw** | Slack socket, LLM/tools, isolated `rmp_task_*` sessions |
| **RMP (`app/`)** | API, workflows, intake, memory, quota broker, evidence |
| **Plugin (`plugins/rmp_adapter`)** | Intercepts Slack → creates RMP tasks; suppresses native double-posts |

Deep dive: [`ARCHITECTURE.md`](ARCHITECTURE.md) · Runbooks: [`docs/runbooks/`](docs/runbooks/)

## Repository layout

```text
aura/
├── app/                  # FastAPI + Temporal + memory + intake
├── plugins/rmp_adapter/  # OpenClaw plugin
├── ops/                  # Canaries, backup, janitor, patch verify
├── tests/                # Pytest suite
├── docs/                 # Runbooks + planning history
├── patch_openclaw.sh     # Re-apply dist patches after OpenClaw upgrades
├── settings.example.json # Config template (no secrets)
├── worker.py             # Temporal worker entrypoint
└── ARCHITECTURE.md       # Full system design
```

## Quick start

### Prerequisites

- Linux host (or VM) with Docker optional for Qdrant/observability  
- Python 3.12+, Node.js ≥ 22.23 (OpenClaw engines)  
- PostgreSQL, Temporal, [OpenClaw](https://github.com/openclaw/openclaw) gateway  
- NVIDIA NIM (or compatible) API keys for chat + embeddings  

### Setup

```bash
git clone https://github.com/Hyper-AI-Lab/aura.git
cd aura
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

cp settings.example.json settings.json
# set api_key, production.slack_owner_user_id, vector/qdrant, task_registry.intake_mode

# Link plugin into your OpenClaw plugins dir, then:
bash patch_openclaw.sh
bash ops/verify_openclaw_patch.sh

# Start API + worker (systemd units or process manager of your choice)
# then:
make production-check
```

### Useful commands

```bash
make production-check   # health + OpenClaw patch verify + intake canaries
make canary             # manual E2E canary task
pytest -q               # unit/integration tests
```

## Configuration notes

- **Never commit** `settings.json`, `.env`, auth profiles, or `data/`.  
- Example config: [`settings.example.json`](settings.example.json).  
- After every `npm install -g openclaw`, re-run `patch_openclaw.sh` (hook persistence, Slack suppress, allowUnsafe passthrough for RMP sessions).  
- Model stack (typical): MiniMax M3 primary → DeepSeek V4 Flash → GLM-5.2; intake/subagents on DeepSeek Flash.

## Status

This repository is a **production-shaped public snapshot** of Aura’s RMP control plane. Paths and host assumptions in older docs may reflect the original single-VPS deployment; adapt ports, systemd units, and secrets to your environment.

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md). Issues and PRs welcome for docs, tests, and portable packaging improvements.

## License

[MIT](LICENSE) © Hyper-AI-Lab
