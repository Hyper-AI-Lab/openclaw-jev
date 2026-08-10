# Vision Completion Plan — Layers A, B, C

**Created:** 2026-06-04  
**Source:** Original vision in `/root/request/request.txt` and gap analysis vs current RMP.

**Execution rule:** Execute steps in order. Append results to `VISION_COMPLETION_PROGRESS.md` after each step. Do not add sub-steps or reorder unless a step is blocked.

---

## Layer A — Control-plane skeleton (complete remaining ~15%)

| Step | Deliverable |
|------|-------------|
| **A1** | MemoryRouter: fail-soft vector/graph reads; fix `read_ordered` procedural scope via `process_type` parameter |
| **A2** | Quota broker: atomic state write (no tmp race); ensure data dir exists |
| **A3** | `compensated` terminal state: DB activity + workflow hooks on catalog/generic failure paths |
| **A4** | Reconciler: notify Slack on stale user-task repair; always refresh `next_check_at` |
| **A5** | `CatalogStepChildWorkflow` + worker registration for isolated step execution |

## Layer B — Non-LLM orchestration behavior

| Step | Deliverable |
|------|-------------|
| **B1** | `app/orchestrator/decision_engine.py` — deterministic status transitions |
| **B2** | Integrate decision engine into `parse_agent_evaluation` and workflow retry logic |
| **B3** | `app/orchestrator/prompt_policy.py` — execution prompts (tool budget, English, task_status JSON) |
| **B4** | GenericTaskWorkflow uses prompt policy + orchestrator decisions (code decides retry/complete) |
| **B5** | CatalogTaskWorkflow: orchestrator step progress messages (factual, from step metadata) |
| **B6** | Code-first quality gate: skip LLM verify when evidence passes with high confidence |

## Layer C — Process memory in daily use

| Step | Deliverable |
|------|-------------|
| **C1** | `build_context_block` uses `read_ordered`; workflows pass `process_type` |
| **C2** | Auto-write episodic memory on each agent observation (generic + catalog) |
| **C3** | Pinned memory pool + promotion stage E in `promotion.py` |
| **C4** | Heartbeat tasks trigger `compact_episodic_memory` activity |
| **C5** | `GET /memory/process/{process_run_id}/context` API for scoped recall |
| **C6** | Intent routing: `summarize` / `read file` / `architecture` → memory-first generic profile |

## Integration & verification

| Step | Deliverable |
|------|-------------|
| **V1** | Tests: decision_engine, read_ordered, compensation, prompt_policy, memory context API |
| **V2** | Update `ARCHITECTURE.md` §9–10 to reflect completion |
| **V3** | `pytest` full suite + `make production-check` pass |
| **V4** | Restart services + manual canary smoke |

**Total steps:** 24 (A1–A5, B1–B6, C1–C6, V1–V4)
