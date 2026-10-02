"""Coding tasks: Claude Code changes a repository in its own checkout, RMP runs the tests itself, Aura
and the Process Evaluator review every round, and nothing ships before Kirill approves in Slack.

One coding job runs at a time (the coding slot). A round is a Claude Code run (a rework resumes its
session), RMP's collection and test run, Aura's review, and the evaluator once the round looks ready.
A stop stops whatever runs within seconds; the checkout is kept and nothing ships.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError
from temporalio.workflow import ActivityCancellationType

with workflow.unsafe.imports_passed_through():
    from app.activities.coding_activities import (
        acquire_coding_slot,
        coding_settings,
        deploy_coding_change,
        draft_coding_brief,
        prepare_coding_workspace,
        refresh_coding_base,
        release_coding_slot,
        review_coding_round,
        run_claude_round,
        stop_coding_units,
        verify_coding_round,
    )
    from app.activities.db_activities import (
        confirm_approval_provenance,
        ensure_process_run,
        finalize_task_failure,
        record_event,
        update_process_state,
        update_task_status,
    )
    from app.activities.openclaw_activities import notify_slack_user, send_to_openclaw
    from app.coding import prompts
    from app.coding.deploy import restarts_for
    from app.coding.units import RUNS_DIR
    from app.notification_policy import sanitize_user_facing_text
    from app.orchestrator.process_brief import user_words
    from app.orchestrator.step_predicates import extract_agent_facts
    from app.task_registry.stop_command import is_whole_message_stop
    from app.workflows.approval import CLOSE_AFTER, REMINDER_AFTER, gate_decision
    from app.workflows.judgment import EvaluatorRetry
    from app.workflows.user_messages import AttachedMessages

QUICK = timedelta(seconds=30)
AURA_TURN = timedelta(minutes=45)
SLOT_POLL = timedelta(minutes=2)
USAGE_LIMIT_WAIT = timedelta(hours=1)
BRIEF_QUESTIONS = 2
RUNNER_FAILURES = {
    "auth_failed": "Claude Code's login was rejected, so its token needs renewing (ops/claude_login.sh)",
    "api_error": "Claude Code's API kept failing",
    "timeout": "the run hit its time limit",
    "no_result": "Claude Code ended without a result",
    "stopped": "the run was stopped outside this task",
}


class Stopped(Exception):
    """Kirill stopped the task."""


class Unanswered(Exception):
    """Kirill did not answer within seven days."""


def _cause(exc: BaseException) -> str:
    while getattr(exc, "cause", None) is not None:
        exc = exc.cause
    return str(exc)[:300]


@workflow.defn
class CodingTaskWorkflow(EvaluatorRetry, AttachedMessages):
    def __init__(self) -> None:
        self.user_inputs: List[str] = []
        self._catchup_chunks: List[str] = []
        self._cancel_requested = False
        self.process_run_id = ""
        self._ctx: Dict[str, Any] = {}
        self._job: Optional[Dict[str, Any]] = None
        self._runs = 0
        self._round = 0
        self._asked_at: Optional[datetime] = None
        self._reminded = False
        self._handed_off = False

    @workflow.signal
    def user_input(self, message: str) -> None:
        if not str(message).startswith("[RECONCILER]"):
            self.user_inputs.append(message)

    @workflow.signal
    def cancel(self, reason: str = "") -> None:
        self._cancel_requested = True

    @workflow.signal
    def approve(self, message: str = "") -> None:
        """The API's approve; the gate still ships only on Kirill's own approval in Slack."""
        self.user_inputs.append(message or "approve")

    def _stop_requested(self) -> bool:
        return self._cancel_requested or any(is_whole_message_stop(user_words(str(m))) for m in self.user_inputs)

    @workflow.run
    async def run(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        task_id = payload["task_id"]
        self._ctx = {"task_id": task_id, "session_key": payload.get("session_key") or "",
                     "intent": payload.get("intent") or "", "task_type": payload.get("task_type") or "",
                     "tags": payload.get("tags") or []}
        self.process_run_id = await workflow.execute_activity(
            ensure_process_run, {"task_id": task_id, "process_type": "coding_task"}, start_to_close_timeout=QUICK)
        if payload.get("report"):
            return await self._report(payload["report"])
        settings = await workflow.execute_activity(coding_settings, {}, start_to_close_timeout=QUICK)
        holding = False
        try:
            await self._status("running", "running")
            await self._take_slot()
            holding = True
            brief = await self._brief(payload, settings)
            if brief is None:
                return await self._fail("I couldn't work out a clear brief for this change, so nothing was started. "
                                        "Please ask again with more detail.")
            try:
                self._job = await workflow.execute_activity(
                    prepare_coding_workspace, {"task_id": task_id, "repo": brief["repo"], "title": brief["title"]},
                    start_to_close_timeout=timedelta(minutes=30), retry_policy=RetryPolicy(maximum_attempts=3))
            except ActivityError as exc:
                return await self._fail(f"I couldn't prepare a checkout of {brief['repo']}: {_cause(exc)}. Nothing was changed.")
            await self._notice(f"Starting on {brief['repo']}: {brief['title']}. Claude Code works in its own checkout on "
                               f"branch {self._job['branch']}, RMP runs the tests itself, and I review every round. "
                               "Nothing ships until you approve.")
            return await self._rounds(settings, brief)
        except Stopped:
            return await self._finish_stopped()
        except Unanswered:
            await self._close("cancelled", "canceled")
            await self._notice(f"Task {task_id[:8]} closed: no answer from you within 7 days, so nothing shipped.")
            return {"status": "cancelled", "task_id": task_id}
        finally:
            if holding and not self._handed_off:
                await workflow.execute_activity(release_coding_slot, {"task_id": task_id}, start_to_close_timeout=QUICK)

    async def _report(self, report: Dict[str, Any]) -> Dict[str, Any]:
        """The run the deploy unit starts after a self-deploy: the judged reply, on the code it deployed."""
        try:
            return await self._final(report["brief"], report["review"], report["evidence"], report["shipped"])
        finally:
            await workflow.execute_activity(release_coding_slot, {"task_id": self._ctx["task_id"]}, start_to_close_timeout=QUICK)

    async def _status(self, task_status: str, process_state: str, *, minutes: Optional[int] = None) -> None:
        later = {"next_check_minutes": minutes} if minutes else {}
        await workflow.execute_activity(update_task_status, {"task_id": self._ctx["task_id"], "status": task_status, **later},
                                        start_to_close_timeout=QUICK)
        await workflow.execute_activity(update_process_state, {"process_run_id": self.process_run_id, "state": process_state, **later},
                                        start_to_close_timeout=QUICK)

    async def _close(self, task_status: str, process_state: str) -> None:
        await workflow.execute_activity(update_task_status, {"task_id": self._ctx["task_id"], "status": task_status},
                                        start_to_close_timeout=QUICK)
        await workflow.execute_activity(update_process_state, {"process_run_id": self.process_run_id, "state": process_state,
                                                               "ended": True}, start_to_close_timeout=QUICK)

    async def _notice(self, message: str) -> None:
        await workflow.execute_activity(notify_slack_user, {**self._ctx, "message": message, "process_run_id": self.process_run_id},
                                        start_to_close_timeout=QUICK)

    async def _sleep(self, duration: timedelta) -> None:
        """Sleep unless Kirill stops the task."""
        try:
            await workflow.wait_condition(self._stop_requested, timeout=duration)
        except asyncio.TimeoutError:
            return
        raise Stopped()

    async def _unless_stopped(self, handle) -> Any:
        """The activity's result; on a stop its units are stopped first, then it is cancelled."""
        await workflow.wait_condition(lambda: handle.done() or self._stop_requested())
        if handle.done():
            return handle.result()
        await workflow.execute_activity(stop_coding_units, {"task_id": self._ctx["task_id"]},
                                        start_to_close_timeout=timedelta(minutes=2))
        handle.cancel()
        try:
            await handle
        except ActivityError:
            pass
        raise Stopped()

    async def _take_slot(self) -> None:
        queued = False
        while True:
            slot = await workflow.execute_activity(acquire_coding_slot, {"task_id": self._ctx["task_id"]}, start_to_close_timeout=QUICK)
            if slot["granted"]:
                if queued:
                    await self._status("running", "running")
                return
            if not queued:
                queued = True
                await self._status("blocked", "queued")
                await self._notice(f"Another coding job is running (task {slot['holder'][:8]}). Yours starts as soon as "
                                   "it finishes; reply stop to cancel it.")
            await self._sleep(SLOT_POLL)

    async def _ask(self, message: str, process_state: str) -> None:
        await self._status("pending_user_input", process_state, minutes=60)
        await self._notice(message)
        self._asked_at, self._reminded = workflow.now(), False

    async def _reply(self, reminder: str) -> List[str]:
        """Kirill's next messages: one reminder after 12 hours, closed unanswered after 7 days."""
        while True:
            if self._stop_requested():
                raise Stopped()
            if self._has_user_messages():
                return self._take_user_messages()
            wait_until = self._asked_at + (CLOSE_AFTER if self._reminded else REMINDER_AFTER)
            try:
                await workflow.wait_condition(lambda: self._stop_requested() or self._has_user_messages(),
                                              timeout=max(wait_until - workflow.now(), timedelta(seconds=1)))
            except asyncio.TimeoutError:
                if self._reminded:
                    raise Unanswered()
                self._reminded = True
                await self._notice(reminder)

    async def _brief(self, payload: Dict[str, Any], settings: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        answers: List[str] = []
        for asked in range(BRIEF_QUESTIONS + 1):
            answers += self._take_user_messages()
            brief = await self._unless_stopped(workflow.start_activity(
                draft_coding_brief, {**self._ctx, "answers": answers, "memory": payload.get("initial_memory_block") or ""},
                start_to_close_timeout=AURA_TURN, retry_policy=RetryPolicy(maximum_attempts=2)))
            if brief.get("error"):
                return None
            if brief["repo"] in settings["repositories"] and not brief["questions"]:
                return brief
            if asked == BRIEF_QUESTIONS:
                return None
            questions = "\n".join(brief["questions"])
            await self._ask(f"Before I start on this, I need to know:\n{questions}", "awaiting_input")
            answers += await self._reply(f"Still waiting on your answer before I start:\n{questions}")
            await self._status("running", "running")
        return None

    async def _claude(self, prompt: str, system: str, session: Optional[str]) -> Dict[str, Any]:
        """One round's Claude Code run; a usage limit pauses it and the same session continues after the reset."""
        while True:
            self._runs += 1
            run = {**self._ctx, "number": self._runs, "round": self._round, "checkout": self._job["checkout"],
                   "prompt": prompt, "system_prompt": system, "resume_session": session}
            try:
                claude = await self._unless_stopped(workflow.start_activity(
                    run_claude_round, run, start_to_close_timeout=timedelta(hours=3), heartbeat_timeout=timedelta(minutes=2),
                    retry_policy=RetryPolicy(initial_interval=timedelta(seconds=10), maximum_interval=timedelta(minutes=2),
                                             maximum_attempts=10),
                    cancellation_type=ActivityCancellationType.WAIT_CANCELLATION_COMPLETED))
            except ActivityError as exc:
                return {"kind": "error", "error": _cause(exc), "number": self._runs}
            if claude["kind"] != "usage_limit":
                return claude
            resume = (datetime.fromtimestamp(claude["resume_at"], timezone.utc) if claude.get("resume_at")
                      else workflow.now() + USAGE_LIMIT_WAIT)
            wait = max(resume - workflow.now(), timedelta(minutes=1))
            await self._status("blocked", "paused", minutes=int(wait.total_seconds() // 60) + 1)
            await self._notice(f"Claude Code reached its usage limit on round {self._round}. I'll continue at "
                               f"{resume:%H:%M} UTC on {resume:%b %d}; reply stop to cancel.")
            await self._sleep(wait)
            await self._status("running", "running")
            session = claude.get("session_id") or session
            prompt = prompts.CONTINUE_PROMPT if session else prompt

    async def _verify(self, brief: Dict[str, Any]) -> Dict[str, Any]:
        verify = {**self._ctx, "number": self._runs, "job": self._job, "message": f"{brief['title']} (round {self._round})"}
        return await self._unless_stopped(workflow.start_activity(
            verify_coding_round, verify, start_to_close_timeout=timedelta(hours=2), heartbeat_timeout=timedelta(minutes=2),
            retry_policy=RetryPolicy(maximum_attempts=3), cancellation_type=ActivityCancellationType.WAIT_CANCELLATION_COMPLETED))

    def _evidence(self, claude: Dict[str, Any], evidence: Dict[str, Any]) -> Dict[str, Any]:
        collected = evidence.get("collected") or {}
        diff = f"{RUNS_DIR}/{self._ctx['task_id']}/diff-{evidence['number']}.patch" if evidence.get("number") else None
        return {"claude": {"outcome": claude.get("kind"), "num_turns": claude.get("num_turns"),
                           "commands": claude.get("commands"), "files_edited": claude.get("files_edited")},
                "tests": evidence.get("tests"), "commits": collected.get("commits"),
                "diffstat": collected.get("diffstat"), "secrets": collected.get("secrets"), "diff_file": diff}

    async def _judge_round(self, brief: Dict[str, Any], reply: str, external: Dict[str, Any],
                           stage: str = prompts.ROUND_STAGE) -> Optional[Dict[str, Any]]:
        return await self._judge(
            {"task_id": self._ctx["task_id"], "user_intent": self._ctx["intent"], "agent_response": reply,
             "process_run_id": self.process_run_id, "attempt": self._round,
             "process_brief": f"{prompts.brief_text(brief)}\n\n{stage}", "external_evidence": external},
            session_key=self._ctx["session_key"], user_intent=self._ctx["intent"],
            task_type=self._ctx["task_type"], tags=self._ctx["tags"])

    async def _rounds(self, settings: Dict[str, Any], brief: Dict[str, Any]) -> Dict[str, Any]:
        entry = settings["repositories"][brief["repo"]]
        target = entry.get("deploy")
        system = prompts.system_prompt(entry, self._job["branch"], self._job["tests"])
        prompt, session, batch = prompts.brief_text(brief), None, 0
        while True:
            self._round += 1
            batch += 1
            claude = await self._claude(prompt, system, session)
            if claude["kind"] not in ("success", "max_turns"):
                reason = RUNNER_FAILURES.get(claude["kind"], "Claude Code failed")
                detail = f" ({claude['error']})" if claude.get("error") else ""
                return await self._fail(f"The coding task failed on round {self._round}: {reason}{detail}. Nothing shipped; "
                                        f"the work so far is kept on branch {self._job['branch']}.")
            session = claude.get("session_id") or session
            try:
                evidence = await self._verify(brief)
            except ActivityError as exc:
                return await self._fail(f"RMP's verification of round {self._round} failed: {_cause(exc)}. Nothing shipped; "
                                        f"the work is kept on branch {self._job['branch']}.")
            evidence["number"] = self._runs
            problems, feedback, review = self._checks(claude, evidence), [], None
            if not problems:
                review = await self._unless_stopped(workflow.start_activity(
                    review_coding_round, {**self._ctx, "number": self._runs, "round": self._round, "brief": brief,
                                          "claude": claude, "evidence": evidence},
                    start_to_close_timeout=AURA_TURN, retry_policy=RetryPolicy(maximum_attempts=2)))
                if review["verdict"] != "ready":
                    found = f": {review['feedback'][0][:300].rstrip('.')}" if review["feedback"] else ""
                    problems.append(review.get("error") or f"my review found more to do{found}")
                    feedback += review["feedback"]
            if not problems:
                judged = await self._judge_round(brief, review["reply"], self._evidence(claude, evidence))
                if judged is None:
                    if self._stop_requested():
                        raise Stopped()
                    return await self._reviewer_unavailable(self._ctx["task_id"], self._ctx["session_key"],
                                                            self._ctx["intent"], self._ctx["task_type"], self._ctx["tags"])
                if judged["verdict"] != "accept":
                    problems.append(f"the evaluator: {judged['issues'].rstrip('.')}")
                    feedback += [item for item in (judged.get("command_to_aura"), judged.get("issues")) if item]
            if problems and batch < int(settings["max_rounds"]):
                # An approval sent before the card is no instruction for Claude; the gate asks again.
                feedback += problems + [f"Kirill added: {words}" for words in self._take_user_messages()
                                        if gate_decision(words) != "approve"]
                await self._notice(f"Round {self._round}: {problems[0]}. Claude Code is working on it again.")
                prompt = prompts.rework_prompt(feedback, evidence.get("tests"))
                continue
            change = await self._gate(self._card(review, evidence, problems, target), not problems, target)
            if change is not None:
                prompt, batch = prompts.change_request_prompt(change), 0
                continue
            shipped = await self._ship(brief, review, evidence, target)
            if shipped["status"] != "needs_rebase":
                return shipped
            self._job = await workflow.execute_activity(refresh_coding_base, {"job": self._job},
                                                        start_to_close_timeout=timedelta(minutes=10),
                                                        retry_policy=RetryPolicy(maximum_attempts=3))
            await self._notice(f"main moved since this change was made, so Claude Code is rebasing it onto "
                               f"{shipped['main'][:12]}. You'll get a new card to approve.")
            prompt, batch = prompts.rebase_prompt(self._job["bundle"], shipped["main"]), 0

    def _checks(self, claude: Dict[str, Any], evidence: Dict[str, Any]) -> List[str]:
        """What RMP's own record says is wrong with a round, before anyone reviews it."""
        if evidence.get("error"):
            return [f"RMP could not collect the work: {evidence['error']}"]
        collected, problems = evidence["collected"], []
        if not collected["commits"]:
            problems.append("Claude Code made no changes")
        if not evidence["tests"]["ok"]:
            problems.append("RMP's test run failed")
        if collected["secrets"]:
            found = ", ".join(sorted({f"{s['kind']} in {s['path']}" for s in collected["secrets"]}))
            problems.append(f"the diff contains what looks like a secret ({found}); remove it")
        return problems

    def _card(self, review: Optional[Dict[str, Any]], evidence: Dict[str, Any], problems: List[str], target: str) -> str:
        """Exactly what Kirill approves: the reviewed summary and RMP's record of the change."""
        collected = evidence.get("collected") or {}
        commits = collected.get("commits") or []
        stat = (collected.get("diffstat") or "").strip().splitlines()
        facts = [f"• Change: {len(commits)} commit(s) on {self._job['branch']}, head {(collected.get('head') or '')[:12]}",
                 f"• Tests (RMP's own run): {prompts.tests_line(evidence.get('tests'))}",
                 f"• Diffstat: {stat[-1].strip() if stat else 'no changes'}",
                 f"• Dependency changes: {', '.join(collected.get('dependencies_changed') or []) or 'none'}"]
        if target == "self":
            changed = [item["path"] for item in collected.get("changed") or []]
            facts.insert(3, f"• Restarts on deploy: {', '.join(restarts_for(changed)) or 'none'}")
        if problems:
            return (f"After {self._round} round(s) this isn't ready to ship:\n" + "\n".join(f"- {p}" for p in problems)
                    + "\n\n" + "\n".join(facts) + "\n\nTell me what to change, or reply stop.")
        how = "deploy it" if target == "self" else "push the branch and open a pull request"
        return f"{review['reply']}\n\n" + "\n".join(facts) + f"\n\nReply approve to {how}, tell me what to change, or stop."

    async def _gate(self, card: str, ready: bool, target: str) -> Optional[str]:
        """None once Kirill approved in Slack after the card; otherwise his change request."""
        await self._ask(card, "awaiting_approval")
        opened = self._asked_at
        how = "deploy it" if target == "self" else "open the pull request"
        reminder = (f"Reminder: the change is waiting for you. Reply approve to {how}, tell me what to change, or stop."
                    if ready else "Reminder: the change still needs your direction. Tell me what to change, or stop.")
        while True:
            replies = await self._reply(reminder)
            decisions = [gate_decision(words) for words in replies]
            if "stop" in decisions:
                raise Stopped()
            changes = [words for words, decision in zip(replies, decisions) if decision == "other"]
            if changes:
                await self._status("running", "running")
                return "\n".join(changes)
            if not ready:
                await self._notice("I can't ship this yet: it hasn't passed review and RMP's tests. "
                                   "Tell me what to change, or stop.")
                continue
            provenance = await workflow.execute_activity(
                confirm_approval_provenance, {"task_id": self._ctx["task_id"], "gate_opened_at": opened.isoformat()},
                start_to_close_timeout=QUICK)
            if provenance.get("ok"):
                await self._status("running", "deploying")
                return None
            await self._notice("I can only ship this on your own approve, sent in Slack after the card above. "
                               "Reply approve here, tell me what to change, or stop.")

    async def _ship(self, brief: Dict[str, Any], review: Dict[str, Any], evidence: Dict[str, Any], target: str) -> Dict[str, Any]:
        """Done (then the judged reply), handed off to the deploy unit, or ``needs_rebase``."""
        await self._notice("Approved. RMP runs the full suite on the approved commit, then deploys it; I'll report when "
                           "it's verified." if target == "self" else "Approved. Pushing the branch and opening a pull request.")
        try:
            shipped = await workflow.execute_activity(
                deploy_coding_change, {**self._ctx, "job": self._job, "head": evidence["collected"]["head"], "target": target,
                                       "title": brief["title"], "summary": review["reply"],
                                       "report": {"brief": brief, "review": review, "evidence": evidence}},
                start_to_close_timeout=timedelta(hours=2), heartbeat_timeout=timedelta(minutes=2),
                retry_policy=RetryPolicy(maximum_attempts=3))
        except ActivityError as exc:
            shipped = {"status": "failed", "summary": f"Shipping failed ({_cause(exc)}); nothing was deployed."}
        if shipped["status"] == "needs_rebase":
            return shipped
        if shipped["status"] == "handed_off":
            self._handed_off = True
            await self._status("deploying", "deploying")
            return {"status": "deploying", "task_id": self._ctx["task_id"], "shipped": shipped}
        return await self._final(brief, review, evidence, shipped)

    async def _final(self, brief: Dict[str, Any], review: Dict[str, Any], evidence: Dict[str, Any],
                     shipped: Dict[str, Any]) -> Dict[str, Any]:
        """Aura's judged final reply; RMP's own record of what shipped when no reply passes the evaluator."""
        external = {**self._evidence({}, evidence), "deploy": shipped}
        prompt = prompts.final_prompt(review["reply"], shipped)
        message = ""
        for _ in range(2):
            response = await workflow.execute_activity(send_to_openclaw, {**self._ctx, "message": prompt},
                                                       start_to_close_timeout=AURA_TURN)
            text = str(((response.get("result") or {}).get("payloads") or [{}])[0].get("text") or "")
            draft = sanitize_user_facing_text(extract_agent_facts(text).get("body") or text)
            judged = await self._judge_round(brief, draft, external, prompts.FINAL_STAGE)
            if judged is not None and judged["verdict"] == "accept":
                message = draft
                break
            if judged is None:
                break
            prompt = f"{prompts.final_prompt(review['reply'], shipped)}\n\nYour last draft was not accepted: {judged['issues']}"
        ok = shipped.get("status") in ("deployed", "pr_opened")
        if not message:
            await workflow.execute_activity(
                record_event, {"correlation_id": self._ctx["task_id"], "entity_type": "task", "entity_id": self._ctx["task_id"],
                               "event_type": "coding.reported_by_rmp", "event_payload": {"status": shipped.get("status")}},
                start_to_close_timeout=QUICK)
        delivered = await self._deliver_final({**self._ctx, "message": message or shipped.get("summary") or str(shipped)})
        if delivered:
            await self._close("completed" if ok else "failed", "completed" if ok else "failed_terminal")
        await self._resubmit_leftovers(self._ctx["task_id"], self._ctx["session_key"])
        return {"status": "completed" if ok and delivered else "failed", "task_id": self._ctx["task_id"], "shipped": shipped}

    async def _fail(self, message: str) -> Dict[str, Any]:
        await workflow.execute_activity(finalize_task_failure, {"task_id": self._ctx["task_id"], "process_run_id": self.process_run_id,
                                                                "task_status": "failed", "process_state": "failed_terminal"},
                                        start_to_close_timeout=QUICK)
        await self._notice(message)
        await self._resubmit_leftovers(self._ctx["task_id"], self._ctx["session_key"])
        return {"status": "failed", "task_id": self._ctx["task_id"], "reason": message}

    async def _finish_stopped(self) -> Dict[str, Any]:
        await workflow.execute_activity(stop_coding_units, {"task_id": self._ctx["task_id"]}, start_to_close_timeout=timedelta(minutes=2))
        await self._close("stopped_by_user", "stopped_by_user")
        kept = f" The work so far is kept on branch {self._job['branch']}." if self._job else ""
        await self._notice(f"Stopped. Nothing shipped.{kept}")
        await self._resubmit_leftovers(self._ctx["task_id"], self._ctx["session_key"])
        return {"status": "stopped_by_user", "task_id": self._ctx["task_id"]}
