"""Workflow catalog: registration, login, email verification, procurement, outreach, browser automation."""
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence


@dataclass(frozen=True)
class WorkflowStep:
    name: str
    kind: str = "openclaw_dispatch"  # openclaw_dispatch | wait_external | approval_gate
    prompt: str = ""
    predicate_id: str = "catalog_dispatch"
    max_attempts: int = 3
    wait_minutes: int = 30
    user_update: str = ""


@dataclass(frozen=True)
class WorkflowTemplate:
    process_type: str
    display_name: str
    intent_patterns: List[str]
    success_criteria: Dict[str, Any]
    steps: List[WorkflowStep] = field(default_factory=list)
    version: int = 1
    # If any match, this template must not claim the intent (meta / false-positive guards).
    negative_intent_patterns: List[str] = field(default_factory=list)


# Meta / status questions must not start catalog workflows via keyword collision.
_META_NON_CATALOG_PATTERNS = (
    r"\bare you aware\b",
    r"\bdo you (know|realize|understand)\b",
    r"\bi thought you (will|would|were going to)\b",
    r"\bfollow up after you (said|told|mentioned|wrote)\b",
    r"\bnow you can\b",
    r"\bi (just )?(added|built|implemented|shipped)\b.{0,80}\bfor you\b",
)


REGISTRATION = WorkflowTemplate(
    process_type="account_registration",
    display_name="Account Registration",
    version=1,
    intent_patterns=[
        r"\bregister\b",
        r"\bsign\s*up\b",
        r"\bcreate\s+(an?\s+)?account\b",
        r"\bregistration\b",
        r"\bnew\s+account\b",
    ],
    success_criteria={
        "requires_account_id": True,
        "requires_confirmation": True,
        "allows_email_verification_pending": False,
    },
    steps=[
        WorkflowStep(
            name="inspect_form",
            prompt=(
                "STEP: Inspect registration form.\n"
                "Navigate to the target site and locate the registration/sign-up form.\n"
                "Report: page URL, required fields, optional fields, blockers (CAPTCHA, invite-only), "
                "and whether email verification is likely required."
            ),
            user_update="Analyzing registration form…",
        ),
        WorkflowStep(
            name="gather_data",
            prompt=(
                "STEP: Gather registration data.\n"
                "Using process memory and the user request, collect all data needed to register "
                "(email, username, password policy, etc.). Do NOT submit the form yet.\n"
                "List each field value you will use and note any missing user-provided data."
            ),
            user_update="Gathering required registration data…",
        ),
        WorkflowStep(
            name="submit_registration",
            prompt=(
                "STEP: Submit registration.\n"
                "Fill and submit the registration form with the gathered data.\n"
                "Capture confirmation message, errors, or next-step instructions (e.g. verify email)."
            ),
            user_update="Submitting registration…",
        ),
        WorkflowStep(
            name="wait_verification",
            kind="wait_external",
            wait_minutes=45,
            user_update="Waiting for email verification…",
        ),
        WorkflowStep(
            name="confirm_account",
            prompt=(
                "STEP: Confirm account creation.\n"
                "Verify the account exists and registration is complete.\n"
                "Provide: account identifier (username/email), confirmation evidence (URL or message), "
                "and whether the account is fully verified."
            ),
            user_update="Confirming account registration…",
        ),
    ],
)

LOGIN = WorkflowTemplate(
    process_type="login",
    display_name="Login / Sign In",
    version=1,
    intent_patterns=[
        r"\blog\s*in\b",
        r"\bsign\s*in\b",
        r"\blogin\b",
        r"\bauthenticate\b",
    ],
    success_criteria={
        "requires_session": True,
    },
    steps=[
        WorkflowStep(
            name="inspect_login",
            prompt=(
                "STEP: Inspect login page.\n"
                "Navigate to the login/sign-in page. Report URL, required fields, "
                "SSO options, and blockers (CAPTCHA, 2FA prompt)."
            ),
            user_update="Analyzing login page…",
        ),
        WorkflowStep(
            name="submit_login",
            prompt=(
                "STEP: Submit login.\n"
                "Enter credentials and submit the login form.\n"
                "Report success, failure reason, or if additional verification is required."
            ),
            user_update="Submitting login credentials…",
        ),
        WorkflowStep(
            name="verify_session",
            prompt=(
                "STEP: Verify authenticated session.\n"
                "Confirm you are logged in: dashboard/profile visible, session cookie, or auth token.\n"
                "Provide evidence of authenticated state."
            ),
            user_update="Verifying authenticated session…",
        ),
    ],
)

EMAIL_VERIFICATION = WorkflowTemplate(
    process_type="email_verification",
    display_name="Email Verification",
    version=1,
    intent_patterns=[
        r"\bverify\s+(my\s+)?email\b",
        r"\bemail\s+verification\b",
        r"\bconfirm\s+(my\s+)?email\b",
        r"\bverification\s+(link|code|email)\b",
        r"\bclick\s+the\s+confirmation\b",
    ],
    success_criteria={
        "requires_verification_complete": True,
    },
    steps=[
        WorkflowStep(
            name="check_inbox",
            prompt=(
                "STEP: Check inbox for verification email.\n"
                "Find the verification message for the relevant service/account.\n"
                "Report sender, subject, and whether a link or code is present."
            ),
            user_update="Checking inbox for verification email…",
        ),
        WorkflowStep(
            name="complete_verification",
            prompt=(
                "STEP: Complete verification.\n"
                "Follow the verification link or enter the code on the site.\n"
                "Report the outcome and any confirmation page content."
            ),
            user_update="Completing email verification…",
        ),
        WorkflowStep(
            name="confirm_verified",
            prompt=(
                "STEP: Confirm verification succeeded.\n"
                "Verify the account/email is marked verified on the service.\n"
                "Provide evidence that verification is complete."
            ),
            user_update="Confirming verification status…",
        ),
    ],
)

PROCUREMENT = WorkflowTemplate(
    process_type="procurement",
    display_name="Procurement",
    version=1,
    intent_patterns=[
        r"\bprocure\b",
        r"\bprocurement\b",
        r"\bpurchase\b",
        r"\bbuy\b",
        r"\bplace\s+(an?\s+)?order\b",
        r"\bvendor\s+quote\b",
        r"\brequest\s+for\s+quote\b",
        r"\brfq\b",
    ],
    success_criteria={
        "requires_approval": True,
        "required_artifact_kinds": ["completion_output", "procurement_record"],
    },
    steps=[
        WorkflowStep(
            name="gather_requirements",
            prompt=(
                "STEP: Gather procurement requirements.\n"
                "Extract item/service specs, quantity, budget, deadline, and vendor constraints "
                "from the user request and process memory.\n"
                "List any missing information needed before sourcing."
            ),
            user_update="Gathering procurement requirements…",
        ),
        WorkflowStep(
            name="research_options",
            prompt=(
                "STEP: Research vendor options.\n"
                "Identify viable vendors or suppliers matching the requirements.\n"
                "Report: vendor names, pricing estimates, lead times, and URLs or contact paths."
            ),
            user_update="Researching vendor options…",
        ),
        WorkflowStep(
            name="prepare_recommendation",
            prompt=(
                "STEP: Prepare purchase recommendation.\n"
                "Summarize the recommended vendor, line items, total cost, and rationale.\n"
                "Do NOT place an order yet — this step is for human review."
            ),
            user_update="Preparing purchase recommendation…",
        ),
        WorkflowStep(
            name="approval_gate",
            kind="approval_gate",
            user_update=(
                "Procurement approval required. Review the recommendation above and reply "
                "'approve' to proceed or 'stop' to cancel."
            ),
        ),
        WorkflowStep(
            name="execute_purchase",
            prompt=(
                "STEP: Execute approved purchase.\n"
                "Place the order or submit the purchase request as approved.\n"
                "Capture order ID, confirmation number, or submission receipt."
            ),
            user_update="Executing approved purchase…",
        ),
        WorkflowStep(
            name="record_artifact",
            prompt=(
                "STEP: Register procurement record artifact.\n"
                "Register a procurement_record artifact containing: vendor, items, total cost, "
                "order/PO reference, and approval timestamp.\n"
                "Confirm the artifact was stored with checksum metadata."
            ),
            user_update="Recording procurement artifact…",
        ),
    ],
)

OUTREACH = WorkflowTemplate(
    process_type="outreach",
    display_name="Outreach / Email",
    version=1,
    intent_patterns=[
        r"\boutreach\b",
        r"\b(send|draft|write|do)\s+(an?\s+)?follow[\s-]*up\b",
        r"\bfollow[\s-]*up\s+(email|message|with|to)\b",
        r"\breach\s+out\b",
        r"\bsend\s+(an?\s+)?email\b",
        r"\bemail\s+thread\b",
        r"\bthread\s+id\b",
        r"\bcontact\s+(them|him|her|vendor|client)\b",
    ],
    negative_intent_patterns=[
        r"\bfollow up after you (said|told|mentioned|wrote)\b",
        r"\bi thought you (will|would)\b",
    ],
    success_criteria={
        "requires_thread_tracking": True,
    },
    steps=[
        WorkflowStep(
            name="draft_outreach",
            prompt=(
                "STEP: Draft outreach message.\n"
                "Compose the email or message based on the user request.\n"
                "Report recipient, subject, and draft body. Do NOT send yet unless explicitly approved."
            ),
            user_update="Drafting outreach message…",
        ),
        WorkflowStep(
            name="send_outreach",
            prompt=(
                "STEP: Send outreach.\n"
                "Send the message via the appropriate channel (email, platform DM, etc.).\n"
                "Capture: thread ID, message ID, sent timestamp, and recipient confirmation."
            ),
            user_update="Sending outreach message…",
        ),
        WorkflowStep(
            name="wait_for_reply",
            kind="wait_external",
            wait_minutes=60,
            user_update="Waiting for reply on outreach thread…",
        ),
        WorkflowStep(
            name="process_reply",
            prompt=(
                "STEP: Process reply.\n"
                "Check the outreach thread for new replies.\n"
                "Summarize reply content, sender, timestamp, and recommended next action."
            ),
            user_update="Processing thread reply…",
        ),
    ],
)

# MoltMarket: scheduled cron jobs should dispatch via the RMP adapter
# (CatalogTaskWorkflow — typically browser_automation or outreach), not OpenClaw
# cron delivery. Re-enable with RMP-owned delivery + ledger per Phase 18.4.
BROWSER_AUTOMATION = WorkflowTemplate(
    process_type="browser_automation",
    display_name="Browser Automation",
    version=1,
    intent_patterns=[
        r"\bbrowser\s+automation\b",
        r"\bweb\s+automation\b",
        r"\bautomate\s+(the\s+)?browser\b",
        r"\bnavigate\s+to\b",
        r"\btake\s+(a\s+)?screenshot\b",
        r"\bmoltmarket\b",
        r"\bcheck\s+(my\s+)?moltmarket\b",
    ],
    success_criteria={
        "requires_human_approval": True,
        "required_artifact_kinds": ["completion_output", "screenshot"],
    },
    steps=[
        WorkflowStep(
            name="plan_automation",
            prompt=(
                "STEP: Plan browser automation.\n"
                "Outline the target URL(s), actions to perform, and expected outcome.\n"
                "List any credentials, CAPTCHA, or human-interaction risks."
            ),
            user_update="Planning browser automation…",
        ),
        WorkflowStep(
            name="approval_gate",
            kind="approval_gate",
            user_update=(
                "Browser automation approval required. Review the plan above and reply "
                "'approve' to execute or 'stop' to cancel."
            ),
        ),
        WorkflowStep(
            name="execute_automation",
            prompt=(
                "STEP: Execute browser automation.\n"
                "Perform the approved browser actions using available tools.\n"
                "Capture page state, errors, and intermediate results."
            ),
            user_update="Running browser automation…",
        ),
        WorkflowStep(
            name="capture_screenshot",
            prompt=(
                "STEP: Capture screenshot artifact.\n"
                "Take a screenshot of the final page state and register it as a screenshot "
                "artifact with checksum metadata.\n"
                "Report artifact ID and describe what the screenshot shows."
            ),
            user_update="Capturing screenshot evidence…",
        ),
    ],
)

CODING_TASK = WorkflowTemplate(
    process_type="coding_task",
    display_name="Coding task (Claude Code)",
    version=1,
    # Advisory hits for the intake LLM only; CodingTaskWorkflow (app/workflows/coding_task.py) runs the task.
    intent_patterns=[
        r"\b(fix|change|modify|refactor|implement|add|write|update|remove)\b.{0,60}\b(code|function|module|bug|tests?|"
        r"feature|endpoint|workflow|plugin|repo|repository)\b",
        r"\b(open|create|make|send)\s+(a\s+)?(pr|pull request)\b",
        r"\bclaude code\b.{0,40}\b(fix|change|implement|build|write|add)\b",
        r"\b(upgrade\s+yourself|self[_\s-]?upgrade|capability[_\s-]?upgrade)\b",
        r"\b(add|install|build|create)\s+(a\s+)?(new\s+)?(tool|plugin)\b",
    ],
    negative_intent_patterns=[
        r"\bare you aware\b",
        r"\bnow you can\b",
        r"\bcan you (write )?code\b",
        r"\bhow does\b",
        r"\bwhat does\b.{0,60}\bdo\b",
        r"\bexplain\b",
        r"\bi (just )?(added|built|implemented|shipped)\b",
    ],
    success_criteria={
        "requires_human_approval": True,
        "requires_tests_passed": True,
        "requires_verify_ok": True,
    },
)

CATALOG: Dict[str, WorkflowTemplate] = {
    t.process_type: t
    for t in (
        REGISTRATION,
        LOGIN,
        EMAIL_VERIFICATION,
        PROCUREMENT,
        OUTREACH,
        BROWSER_AUTOMATION,
        CODING_TASK,
    )
}

# Legacy / plugin hints that are not catalog keys but map to a template.
CATALOG_ALIASES: Dict[str, str] = {
    "moltmarket_check": "browser_automation",
    "email_followup": "outreach",
    "tool_self_upgrade": "coding_task",
    "self_upgrade": "coding_task",
    "capability_upgrade": "coding_task",
}

# Order matters: more specific patterns first.
_CLASSIFY_ORDER = (
    "coding_task",
    "email_verification",
    "browser_automation",
    "procurement",
    "outreach",
    "account_registration",
    "login",
)


def _any_pattern(text: str, patterns: Sequence[str]) -> bool:
    return any(re.search(p, text, re.IGNORECASE) for p in patterns)


def soft_catalog_candidates(intent: str) -> List[str]:
    """Advisory keyword hits for the intake LLM — never assigns a workflow.

    The intake agent must dismiss false positives (awareness, meta chat, etc.).
    """
    text = (intent or "").lower()
    hits: List[str] = []
    for key in _CLASSIFY_ORDER:
        template = CATALOG[key]
        for pattern in template.intent_patterns:
            if re.search(pattern, text, re.IGNORECASE):
                hits.append(template.process_type)
                break
    return hits


def resolve_catalog_template(intent: str, task_type: str = "") -> Optional[str]:
    """Return process_type if intent matches a catalog template, else None.

    Used for soft candidates / legacy callers. Live Slack DMs must not hard-assign
    catalog workflows from this alone — intake LLM ``catalog_hint`` is authoritative.
    """
    if task_type in CATALOG:
        return task_type
    alias = CATALOG_ALIASES.get(task_type)
    if alias:
        return alias
    text = (intent or "").lower()
    # Awareness / meta chat must not open catalog workflows on keyword collisions.
    if _any_pattern(text, _META_NON_CATALOG_PATTERNS):
        return None
    for key in _CLASSIFY_ORDER:
        template = CATALOG[key]
        if template.negative_intent_patterns and _any_pattern(
            text, template.negative_intent_patterns
        ):
            continue
        for pattern in template.intent_patterns:
            if re.search(pattern, text, re.IGNORECASE):
                return template.process_type
    return None


def normalize_catalog_type(process_type: str, intent: str = "") -> Optional[str]:
    """Map hints/aliases to a registered catalog process_type, or None."""
    key = (process_type or "").strip()
    if not key:
        return resolve_catalog_template(intent, "")
    if key in CATALOG:
        return key
    alias = CATALOG_ALIASES.get(key)
    if alias:
        return alias
    return resolve_catalog_template(intent, key)


def catalog_type_for_workflow(
    process_type_hint: Optional[str],
    intent: str,
    task_type: str = "",
) -> Optional[str]:
    """Resolve a catalog process_type from intent; plugin hints are soft only.

    A bare process_type_hint must not force a catalog workflow when the intent
    does not match that template (false positives like mentioning "browser"
    in a how-to question). Hints only confirm an intent-based match.
    """
    if task_type in CATALOG or task_type in CATALOG_ALIASES:
        candidate = normalize_catalog_type(task_type, intent)
        if candidate and get_template(candidate):
            return candidate

    intent_match = resolve_catalog_template(intent, "")
    if process_type_hint:
        hinted = normalize_catalog_type(process_type_hint, "")
        if hinted and get_template(hinted) and hinted == intent_match:
            return hinted
    return intent_match


def catalog_assignment_from_intake(
    *,
    intake_ran: bool,
    intake_catalog_type: Optional[str],
) -> Optional[str]:
    """Live catalog assignment is intake-LLM only.

    When intake did not run (off/degraded path with empty result), never
    regex-assign a catalog workflow. Templates and ``intent_patterns`` stay
    advisory via ``soft_catalog_candidates``.
    """
    if intake_ran:
        return intake_catalog_type
    return None


def get_template(process_type: str) -> Optional[WorkflowTemplate]:
    return CATALOG.get(process_type)


def list_catalog() -> List[Dict[str, Any]]:
    return [
        {
            "process_type": t.process_type,
            "display_name": t.display_name,
            "version": t.version,
            "step_count": len(t.steps),
            "steps": [
                {
                    "name": s.name,
                    "kind": s.kind,
                    "max_attempts": s.max_attempts,
                    "wait_minutes": s.wait_minutes if s.kind == "wait_external" else None,
                }
                for s in t.steps
            ],
            "success_criteria": t.success_criteria,
        }
        for t in CATALOG.values()
    ]
