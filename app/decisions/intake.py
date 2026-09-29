"""Typed Jev questions for the Intake Analyst.

Code maps the answers onto the intake LLM result shape; apply_intake_policy stays the authority.
Questions and thresholds live here so they can be reviewed in one place.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from app.decisions.jev import MODEL, Policy, choice, get_client, get_policy
from app.memory.policy import redact_secrets
from app.notification_policy import INTERNAL_TAGS, is_internal_task
from app.task_registry.session_identity import session_keys_equivalent

logger = logging.getLogger("rmp.jev.intake")
INTAKE_RUBRIC = "aura.intake.v1"
MAX_RUNNING = 4
MAX_FINISHED = 5
MAX_MEMORY = 3
MAX_DIALOGUE_TURNS = 4
CATALOG_MIN_CONFIDENCE = 0.9

CATALOG_RUBRIC = {
    "account_registration": "Create a new account on a website or service.",
    "browser_automation": "Operate a website in a browser to finish a multi-step web task.",
    "email_verification": "Confirm an email address with a verification link or code from the inbox.",
    "login": "Sign in to an existing account on a website or service.",
    "outreach": "Draft or send a message or follow-up to people or organizations.",
    "procurement": "Find, compare, order or buy products or services.",
    "tool_self_upgrade": "Build or upgrade one of Aura's own tools or capabilities. Not a question about what Aura can already do.",
}
WEB_RUBRIC = {
    "none": "No web access is needed.",
    "search": "Search the web for information.",
    "fetch": "Read one specific web page or URL.",
    "crawl": "Read many pages of one website.",
    "adaptive_extract": "Pull specific information out of web pages whose layout is unknown.",
    "schema_extract": "Extract structured records, such as a table or a list of fields, from web pages.",
    "interact": "Click, log in, fill forms or take screenshots on a website.",
}
RUNNING_ACTIONS = {"add_instructions": "attach_active", "asks_status": "wait_active", "wants_restart": "rebuild_stale"}
STATUS_WORDS = {
    "created": "starting", "pending": "starting", "running": "running", "blocked": "blocked",
    "needs_replan": "needs a new plan", "pending_user_input": "waiting for Kirill's answer",
}
_STAMP = re.compile(r"^\[\d{2}:\d{2}\] ")


def _text(value: Any, limit: int) -> str:
    return redact_secrets(" ".join(str(value or "").split())[:limit])


def _age_words(iso: Any, now: datetime, verb: str) -> str:
    try:
        when = datetime.fromisoformat(str(iso))
    except ValueError:
        return "unknown"
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    minutes = (now - when).total_seconds() / 60
    if minutes < 10:
        return f"{verb} in the last 10 minutes"
    if minutes < 60:
        return f"{verb} within the last hour"
    if minutes < 24 * 60:
        return f"{verb} within the last day"
    if minutes < 7 * 24 * 60:
        return f"{verb} within the last week"
    return f"{verb} more than a week ago"


def build_intake_request(
    context: dict, dialogue: list[str], *, now: datetime | None = None
) -> tuple[dict, dict, dict]:
    """Return (state, questions, aliases); aliases map R1/F1 labels back to task ids."""
    now = now or datetime.now(timezone.utc)
    session_key = context.get("session_key") or ""
    running = [
        t for t in context.get("active_tasks") or []
        if session_keys_equivalent(session_key, t.get("session_key") or "") or t.get("task_kind") == "durable"
    ][:MAX_RUNNING]
    finished = [
        r for r in context.get("recent_registry") or []
        if not is_internal_task(str(r.get("intent_snippet") or ""), str(r.get("process_type") or ""), [])
    ][:MAX_FINISHED]
    aliases: dict[str, str] = {}
    running_state, running_criteria = [], {}
    for i, t in enumerate(running, 1):
        alias = f"R{i}"
        aliases[alias] = str(t["task_id"])
        goal = _text(t.get("goal_snippet") or t.get("goal"), 300)
        running_state.append({"alias": alias, "goal": goal,
            "status": STATUS_WORDS.get(str(t.get("status") or ""), str(t.get("status") or "")),
            "last_update": _age_words(t.get("updated_at"), now, "updated")})
        running_criteria[alias] = f"Running task: {goal}"
    finished_state, finished_criteria = [], {}
    for i, r in enumerate(finished, 1):
        alias = f"F{i}"
        aliases[alias] = str(r["task_id"])
        request, outcome = _text(r.get("intent_snippet"), 300), _text(r.get("outcome_summary"), 300)
        finished_state.append({"alias": alias, "request": request, "outcome": outcome,
            "result": str(r.get("terminal_status") or ""), "ended": _age_words(r.get("task_ended_at"), now, "ended")})
        finished_criteria[alias] = f"Finished task: {request} Outcome: {outcome}"
    state = {
        "message": str(context.get("intent") or "")[:2000],
        "recent_dialogue": [_STAMP.sub("", line) for line in dialogue[-MAX_DIALOGUE_TURNS:]],
        "running_tasks": running_state,
        "finished_tasks": finished_state,
        "memory": [_text(m.get("snippet"), 300) for m in (context.get("memory_hits") or [])[:MAX_MEMORY]],
    }
    from app.workflows.catalog import CATALOG

    questions = {
        "relation": choice(
            "How does state.message relate to Kirill's work with Aura? Use state.recent_dialogue, "
            "state.running_tasks, state.finished_tasks and state.memory as evidence.",
            {"running": "It continues, answers, asks about or changes a task in state.running_tasks.",
             "finished": "It follows up on a task in state.finished_tasks, such as a correction, a repeat or a question about its result.",
             "memory": "It depends on Kirill's remembered facts or preferences in state.memory, not on a listed task.",
             "new": "It is a new request or ordinary conversation that no listed task covers.",
             "unclear": "The evidence does not settle which of the other options applies."}),
        "execution_mode": choice(
            "Can Aura answer state.message directly from the conversation and its own knowledge, "
            "or does the answer need tools, web access, files, code or several steps?",
            {"conversational": "A direct reply is enough: a greeting, thanks, an opinion, or a question about Aura itself.",
             "structured_work": "The answer needs tools, web access, files, code or several steps."}),
        "catalog": choice(
            "Does state.message ask Aura to carry out one of these workflow templates now? "
            "A question about a capability is not a request to run it.",
            {**{k: v for k, v in CATALOG_RUBRIC.items() if k in CATALOG},
             "none": "None of these templates: conversation, questions, research or other work."}),
        "web_intent": choice("What web access does the work in state.message need?", WEB_RUBRIC),
    }
    if running:
        questions["running_target"] = choice(
            "Which running task does state.message continue, answer, ask about or change?",
            {**running_criteria, "none": "None of these running tasks."})
        questions["running_action"] = choice(
            "If state.message refers to a running task, what does it ask Aura to do with that task?",
            {"add_instructions": "Adds details, answers Aura's question, or changes what the task should do.",
             "asks_status": "Only asks about progress or waits for the result, with no new instructions.",
             "wants_restart": "Says the task is stuck, broken or wrong and should start over."})
        if len(running) > 1:
            for alias, criterion in running_criteria.items():
                questions[f"adds_to_{alias}"] = choice(
                    f"Does state.message add details to, answer, or change running task {alias}?",
                    {"yes": f"It does. {criterion}", "no": "It does not concern this task."})
    if finished:
        questions["finished_target"] = choice(
            "Which finished task does state.message follow up on?",
            {**finished_criteria, "none": "None of these finished tasks."})
    return state, questions, aliases


def _passes(answer: dict | None, threshold: float) -> bool:
    return bool(answer) and answer["choice_is_max"] and answer["confidence"] >= threshold


def compose_intake_result(answers: dict, aliases: dict, policy: Policy) -> dict | None:
    """Map typed answers onto the intake LLM result, or None to leave the message to the LLM."""
    relation, mode = answers["relation"], answers["execution_mode"]
    kind, strict = relation["choice"], policy.intake_attach_min_confidence
    if not relation["choice_is_max"]:
        return None
    target, similar, targets = None, [], []
    if kind == "running":
        tgt, act = answers.get("running_target"), answers.get("running_action")
        if not (_passes(relation, strict) and _passes(tgt, strict) and _passes(act, strict)) or tgt["choice"] == "none":
            return None
        target = aliases[tgt["choice"]]
        decision, similar = RUNNING_ACTIONS[act["choice"]], [target]
        support = [relation["confidence"], tgt["confidence"], act["confidence"]]
        rationale = f"Intake (Jev): this message is about running task {target[:8]}."
        if decision == "attach_active":
            targets += [
                aliases[qid.removeprefix("adds_to_")]
                for qid, answer in answers.items()
                if qid.startswith("adds_to_") and answer["choice"] == "yes" and _passes(answer, strict)
                and aliases[qid.removeprefix("adds_to_")] != target
            ]
            if targets:
                rationale = f"Intake (Jev): this message adds to running tasks {', '.join(t[:8] for t in [target, *targets])}."
            targets = [target, *targets]
    else:
        # Not about a running task once little probability is left on running or unclear;
        # a rival workflow next to running work needs the stricter bar.
        settled = 1 - relation["probabilities"].get("running", 0) - relation["probabilities"].get("unclear", 0)
        bar = strict if "running_target" in answers else policy.intake_min_confidence
        if kind == "unclear" or settled < bar or not _passes(mode, policy.intake_min_confidence):
            return None
        tgt = answers.get("finished_target")
        support = [settled, mode["confidence"]]
        if kind == "finished" and _passes(tgt, policy.intake_min_confidence) and tgt["choice"] != "none":
            kind, decision, similar = "finished", "create_guided", [aliases[tgt["choice"]]]
            support.append(tgt["confidence"])
            rationale = f"Intake (Jev): this message follows up on finished task {similar[0][:8]}."
        elif kind == "memory" and _passes(relation, policy.intake_min_confidence):
            decision = "create_guided"
            rationale = "Intake (Jev): this message relies on remembered facts about Kirill, not on a listed task."
        else:
            kind, decision = "new", "create_fresh"
            rationale = "Intake (Jev): this is a new request in the same conversation."
    execution_mode = mode["choice"] if _passes(mode, policy.intake_min_confidence) else None
    catalog, web = answers["catalog"], answers["web_intent"]
    catalog_hint = None
    if execution_mode == "structured_work" and catalog["choice"] != "none" and _passes(catalog, CATALOG_MIN_CONFIDENCE):
        catalog_hint = catalog["choice"]
    return {
        "decision": decision,
        "relation_class": kind,
        "execution_mode": execution_mode,
        "confidence": int(100 * min(support)),
        "rationale": rationale,
        "similar_task_ids": similar,
        "target_task_id": target,
        "target_task_ids": targets or ([target] if target else []),
        "catalog_hint": catalog_hint,
        "web_intent": web["choice"] if _passes(web, policy.intake_min_confidence) else None,
        "guidance_notes": "",
        "decision_source": "jev",
    }


async def _recent_dialogue(session_key: str) -> list[str]:
    from app.task_registry.messages import recent_session_dialogue_block

    try:
        block = await recent_session_dialogue_block(session_key)
    except Exception as exc:
        logger.warning("jev intake dialogue unavailable: %s", type(exc).__name__)
        return []
    return block.splitlines()[1:] if block else []


async def _review(context: dict, policy: Policy) -> tuple[dict | None, dict]:
    session_key = context.get("session_key") or ""
    state, questions, aliases = build_intake_request(context, await _recent_dialogue(session_key))
    purpose = "intake." + hashlib.sha256(session_key.encode()).hexdigest()[:16]
    ev = await get_client().evaluate(state, questions, purpose=purpose, rubric=INTAKE_RUBRIC, policy=policy)
    answers = ev.result["answers"] if ev.status == "ok" else {}
    proposal = compose_intake_result(answers, aliases, policy) if answers else None
    record = {
        "mode": policy.intake_mode, "model": MODEL, "rubric": INTAKE_RUBRIC, "status": ev.status,
        "reason": ev.reason, "latency_ms": ev.latency_ms, "request_hash": ev.request_hash,
        "accepted": proposal is not None,
        "proposal": {k: proposal[k] for k in ("decision", "relation_class", "execution_mode", "confidence",
            "target_task_id", "target_task_ids", "similar_task_ids", "catalog_hint", "web_intent")} if proposal else None,
        "answers": {qid: {"choice": a["choice"], "confidence": round(a["confidence"], 3)} for qid, a in answers.items()},
    }
    logger.info("jev intake %s", json.dumps(record, sort_keys=True))
    return proposal, record


async def review_intake(context: dict, *, tags: list | None = None) -> tuple[dict | None, dict | None]:
    """Return (llm_result to use, audit record). The first item is set only in enforce mode."""
    policy = get_policy()
    if policy.intake_mode == "off" or {str(t).lower() for t in tags or []} & INTERNAL_TAGS:
        return None, None
    if not str(context.get("intent") or "").strip():
        return None, None
    try:
        proposal, record = await _review(context, policy)
    except Exception as exc:
        # Jev is advisory: a consumer bug must leave intake on its existing path.
        logger.warning("jev intake skipped: %s", type(exc).__name__)
        return None, {"mode": policy.intake_mode, "status": "unavailable", "reason": "consumer_error"}
    if policy.intake_mode == "enforce" and proposal:
        return {**proposal, "jev": record}, record
    return None, record
