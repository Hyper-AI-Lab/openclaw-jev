"""The IA's deep recall beside a task: in Aura's memory when it is ready in time, else a judged follow-up.

Only an accepted follow-up reaches Kirill; the report row always records how the task used it.
"""
import asyncio
from datetime import timedelta
from typing import Any, Dict, List

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ChildWorkflowError, WorkflowAlreadyStartedError

with workflow.unsafe.imports_passed_through():
    from app.activities.db_activities import record_event, update_task_status
    from app.activities.deep_memory_activities import judge_recall_novelty, settle_recall_report
    from app.activities.openclaw_activities import notify_slack_user, send_to_openclaw, task_actions_digest
    from app.deep_memory.recall import format_report
    from app.evidence import check_evidence
    from app.notification_policy import sanitize_user_facing_text
    from app.orchestrator.completion_rework import (
        RECALL_NOTICE,
        RECALL_UNCONFIRMED_NOTICE,
        build_recall_refinement_prompt,
        build_rework_prompt,
        followup_brief,
    )
    from app.orchestrator.decision_engine import decide_completion_gate
    from app.orchestrator.process_brief import compose_executor_memory
    from app.orchestrator.step_predicates import extract_agent_facts
    from app.workflows.deep_recall import DeepRecallWorkflow, recall_workflow_id
    from app.workflows.timeouts import AURA_TURN

FOLLOWUP_JUDGED_ATTEMPTS = 3


class DeepRecallPhase:
    """For task workflows that also mix in EvaluatorRetry and AttachedMessages and keep `_memory_block`."""

    process_run_id: str
    _memory_block: str

    def _init_recall(self) -> None:
        self._recall: Any = None
        self._recall_report_id = ""
        self._recall_open = False  # started and not yet used or let go
        self._recall_block = ""  # the report as Aura and the evaluator read it, once merged
        self._recall_followups = False
        self._recall_wait_sec = 300

    async def _start_recall(self, task_id: str, user_intent: str, session_key: str, cfg: Dict[str, Any]) -> None:
        """Start the recall child beside the run; nothing waits for it."""
        self._recall_report_id = str(workflow.uuid4())
        try:
            self._recall = await workflow.start_child_workflow(
                DeepRecallWorkflow.run,
                {"task_id": task_id, "query": user_intent, "session_key": session_key,
                 "report_id": self._recall_report_id},
                id=recall_workflow_id(task_id),
                task_queue=workflow.info().task_queue,
                execution_timeout=timedelta(seconds=int(cfg.get("deadline_sec") or 180)),
            )
        except WorkflowAlreadyStartedError:
            return
        self._recall_open = True
        self._recall_followups = bool(cfg.get("followups"))
        self._recall_wait_sec = int(cfg.get("wait_sec") or 300)

    async def _settle_recall(self, consumed_by: str, *, status: str = "", reason: str = "") -> None:
        self._recall_open = False
        await workflow.execute_activity(
            settle_recall_report,
            {"report_id": self._recall_report_id, "consumed_by": consumed_by, "status": status, "reason": reason},
            start_to_close_timeout=timedelta(seconds=15),
        )

    async def _drop_recall(self, reason: str) -> None:
        if self._recall_open:
            await self._settle_recall("none", status="cancelled", reason=reason)

    async def _recall_outcome(self) -> Dict[str, Any]:
        """What the finished recall found; a failed or timed-out recall is settled here."""
        try:
            return await self._recall
        except ChildWorkflowError as exc:
            await self._settle_recall("none", status="failed", reason=str(exc.cause or exc)[:300])
            return {}

    async def _merge_recall(self, consumer: str) -> None:
        """A recall that finished in time joins the memory Aura and the evaluator read; no follow-up then."""
        if not self._recall_open or not self._recall.done():
            return
        outcome = await self._recall_outcome()
        if not self._recall_open:
            return
        block = format_report(outcome.get("report") or {}) if outcome.get("status") == "ready" else ""
        if block:
            self._recall_block = block
            self._memory_block = compose_executor_memory(self._memory_block, block)
        await self._settle_recall(consumer if block else "none")

    async def _recall_event(self, task_id: str, event_type: str, payload: Dict[str, Any]) -> None:
        await workflow.execute_activity(
            record_event,
            {"correlation_id": task_id, "entity_type": "task", "entity_id": task_id, "event_type": event_type,
             "event_payload": {"report_id": self._recall_report_id, **payload}},
            start_to_close_timeout=timedelta(seconds=10),
        )

    async def _notice(self, task_id: str, session_key: str, user_intent: str, task_type: str, tags: List[str],
                      message: str) -> None:
        await workflow.execute_activity(
            notify_slack_user,
            {"session_key": session_key, "task_id": task_id, "intent": user_intent, "task_type": task_type,
             "tags": tags, "message": message, "message_kind": "notice"},
            start_to_close_timeout=timedelta(seconds=30),
        )

    async def _deep_recall_phase(
        self, task_id: str, session_key: str, user_intent: str, task_type: str, tags: List[str], reply: str,
        attempt: int,
    ) -> Dict[str, Any]:
        """After the reply: wait a bounded time for the recall; follow up, judged, when it adds or corrects.

        Returns {"followup": text} when one reached Kirill, {"stopped": True} on a stop,
        {"refused": True} when Slack refused the follow-up for good, else {}.
        """
        if not self._recall_followups:
            await self._drop_recall("follow-ups are off")
            return {}
        await workflow.execute_activity(
            update_task_status,
            {"task_id": task_id, "status": "running", "next_check_minutes": 10},
            start_to_close_timeout=timedelta(seconds=10),
        )
        try:
            await workflow.wait_condition(
                lambda: self._recall.done() or self._stop_requested() or self._has_user_messages(),
                timeout=timedelta(seconds=self._recall_wait_sec),
            )
        except asyncio.TimeoutError:
            await self._drop_recall("not ready within the follow-up wait")
            return {}
        if self._stop_requested():
            return {"stopped": True}
        if not self._recall.done():
            await self._drop_recall("Kirill wrote again; his message starts over on its own")
            return {}
        outcome = await self._recall_outcome()
        if not self._recall_open:
            return {}
        report = outcome.get("report") or {}
        if outcome.get("status") != "ready" or not format_report(report):
            await self._settle_recall("none")
            return {}
        try:
            novelty = await workflow.execute_activity(
                judge_recall_novelty,
                {"report_id": self._recall_report_id, "query": user_intent, "reply": reply, "report": report},
                start_to_close_timeout=timedelta(seconds=150),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )
        except ActivityError:
            novelty = {"verdict": "none", "points": [], "reason": "novelty judge unavailable"}
        verdict = novelty.get("verdict")
        await self._recall_event(task_id, "deep_recall.novelty",
                                 {"verdict": verdict, "points": len(novelty.get("points") or [])})
        if verdict not in ("adds", "corrects"):
            await self._settle_recall("none")
            return {}
        if self._stop_requested():
            return {"stopped": True}
        await self._notice(task_id, session_key, user_intent, task_type, tags, RECALL_NOTICE)
        refined = await self._refine_from_recall(
            task_id, session_key, user_intent, task_type, tags, reply, report, novelty, attempt
        )
        if refined.get("stopped"):
            return refined
        if refined.get("text"):
            delivered = await self._deliver_final(
                {"session_key": session_key, "task_id": task_id, "intent": user_intent, "task_type": task_type,
                 "tags": tags, "message": refined["text"], "message_kind": "followup",
                 "attempt": refined["attempt"]}
            )
            await self._settle_recall("followup")
            await self._recall_event(task_id, "deep_recall.followup",
                                     {"outcome": "delivered" if delivered else "refused",
                                      "attempt": refined["attempt"]})
            return {"followup": refined["text"]} if delivered else {"refused": True}
        await self._notice(task_id, session_key, user_intent, task_type, tags, RECALL_UNCONFIRMED_NOTICE)
        await self._settle_recall("none")
        await self._recall_event(task_id, "deep_recall.followup", {"outcome": "unconfirmed"})
        return {}

    async def _aura(self, task_id: str, session_key: str, task_type: str, tags: List[str], prompt: str,
                    suffix: str) -> str:
        resp = await workflow.execute_activity(
            send_to_openclaw,
            {"message": prompt, "task_id": task_id, "session_key": session_key, "task_type": task_type,
             "tags": tags, "session_suffix": suffix},
            start_to_close_timeout=AURA_TURN,
        )
        try:
            text = resp["result"]["payloads"][0]["text"]
        except Exception:
            text = str(resp)
        return sanitize_user_facing_text(extract_agent_facts(text).get("body") or text)

    async def _refine_from_recall(
        self, task_id: str, session_key: str, user_intent: str, task_type: str, tags: List[str], reply: str,
        report: Dict[str, Any], novelty: Dict[str, Any], attempt: int,
    ) -> Dict[str, Any]:
        """Aura refines in a fresh session; the evaluator judges it as a follow-up to her reply.

        Returns {"text", "attempt"} once accepted, {"stopped": True} on a stop, else {}.
        """
        actions = await workflow.execute_activity(
            task_actions_digest, {"task_id": task_id}, start_to_close_timeout=timedelta(seconds=60)
        )
        memory = compose_executor_memory(self._memory_block, format_report(report))
        verdict, points = novelty["verdict"], list(novelty.get("points") or [])
        draft = await self._aura(
            task_id, session_key, task_type, tags,
            build_recall_refinement_prompt(user_intent, reply, verdict=verdict, points=points,
                                           memory_block=memory, actions=actions),
            "__recall",
        )
        brief = compose_executor_memory(memory, followup_brief(reply, verdict))
        for tries in range(1, FOLLOWUP_JUDGED_ATTEMPTS + 1):
            attempt += 1
            if self._stop_requested():
                return {"stopped": True}
            added = self._take_user_messages()
            if added:
                draft = await self._fold_in(task_id, session_key, task_type, tags, draft, added)
            evidence = check_evidence(user_intent, draft)
            judged = await self._judge(
                {"task_id": task_id, "user_intent": user_intent, "agent_response": draft,
                 "process_run_id": self.process_run_id, "attempt": attempt, "process_brief": brief},
                session_key=session_key, user_intent=user_intent, task_type=task_type, tags=tags,
            )
            if judged is None:
                return {"stopped": True} if self._stop_requested() else {}
            gate = decide_completion_gate(
                evidence_passed=evidence["passed"],
                evidence_issues=evidence.get("issues", []),
                quality_passed=judged.get("quality") != "fail",
                quality_issues=judged.get("issues", ""),
                skip_quality_llm=False,
            )
            if gate["action"] == "complete":
                if self._has_user_messages():
                    continue  # the accepted draft predates them: fold in and judge again
                return {"text": draft, "attempt": attempt}
            if tries == FOLLOWUP_JUDGED_ATTEMPTS:
                break
            draft = await self._aura(
                task_id, session_key, task_type, tags,
                build_rework_prompt(
                    user_intent, draft,
                    evidence_issues=evidence.get("issues"),
                    quality_issues=judged.get("issues", ""),
                    command_to_aura=judged.get("command_to_aura", ""),
                    attempt=tries + 1,
                    max_attempts=FOLLOWUP_JUDGED_ATTEMPTS,
                    memory_block=brief,
                    actions=actions,
                ),
                f"__r{attempt + 1}",
            )
        return {}
