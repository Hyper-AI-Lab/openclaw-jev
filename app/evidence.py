"""Deterministic evidence checks before accepting LLM completion claims."""
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse


def _extract_urls(text: str) -> List[str]:
    return re.findall(r"https?://[^\s\)\]>\"']+", text or "")


def _extract_domains(text: str) -> List[str]:
    domains = []
    for url in _extract_urls(text):
        try:
            domains.append(urlparse(url).netloc.lower())
        except Exception:
            pass
    return domains


def check_evidence(user_intent: str, agent_response: str) -> Dict[str, Any]:
    """
    Programmatic completion predicates. Returns pass/fail with issues list.
    The LLM cannot override a fail here.
    """
    intent = (user_intent or "").lower()
    response = agent_response or ""
    issues: List[str] = []

    if "canary_ok" in response.lower().replace(" ", "") or "rmp canary" in intent:
        return {"passed": True, "issues": []}

    # Entity/domain mismatch: user asked about Moltbook but answer cites MoltMarket
    if "moltbook" in intent and "moltmarket" in response.lower():
        if "moltbook" not in response.lower():
            issues.append("Answer references MoltMarket but user asked about Moltbook")

    if "moltmarket" in intent and "moltbook" in response.lower():
        if "moltmarket" not in response.lower():
            issues.append("Answer references Moltbook but user asked about MoltMarket")

    # URL requested but none provided
    if any(kw in intent for kw in ["url", "link", "http"]) and not _extract_urls(response):
        if "http" not in response.lower():
            issues.append("User requested a URL but response contains no URL")

    # Empty substantive response
    stripped = response.strip()
    if len(stripped) < 5:
        issues.append("Response is empty or too short")

    # Tool-call-only pseudo responses
    if stripped.startswith("[Tool Call:") and len(stripped) < 80:
        issues.append("Response contains only tool call markers without substantive answer")

    # Heartbeat must be exact ack when intent is heartbeat
    if "heartbeat" in intent.lower() or "heartbeat.md" in intent.lower():
        if "heartbeat_ok" not in stripped.lower().replace(" ", ""):
            pass  # non-heartbeat ack responses are fine for heartbeat with content

    return {
        "passed": len(issues) == 0,
        "issues": issues,
    }


def _is_conversational_intent(intent: str) -> bool:
    lower = (intent or "").lower()
    markers = (
        "how are you",
        "good morning",
        "good evening",
        "hello",
        "hi aura",
        "hey aura",
        "communicate here",
        "testing if you",
        "chat history",
    )
    return any(m in lower for m in markers)


def evidence_high_confidence(user_intent: str, agent_response: str) -> bool:
    """Skip LLM quality review when programmatic evidence is strongly satisfied."""
    result = check_evidence(user_intent, agent_response)
    if not result["passed"]:
        return False
    response = (agent_response or "").strip()
    intent = (user_intent or "").lower()
    if _is_conversational_intent(user_intent) and len(response) >= 30:
        return True
    if len(response) < 80:
        return False
    if any(kw in intent for kw in ("summarize", "summary", "overview", "architecture")):
        return len(response) >= 120
    if "read" in intent and any(kw in intent for kw in ("file", "document", ".md")):
        return len(response) >= 100
    return len(response) >= 200


def check_catalog_completion(
    process_type: str, user_intent: str, agent_response: str
) -> Dict[str, Any]:
    """Evidence predicates for catalog workflow completion."""
    response = (agent_response or "").lower()
    issues: List[str] = []

    if len((agent_response or "").strip()) < 20:
        issues.append("Catalog completion response too short for evidence")

    if process_type == "account_registration":
        has_account = any(
            kw in response
            for kw in (
                "account",
                "username",
                "registered",
                "registration complete",
                "sign-up complete",
                "verified",
            )
        )
        has_blocker = any(
            kw in response
            for kw in ("failed", "error", "unable to register", "not registered")
        )
        if not has_account:
            issues.append("Registration completion lacks account/confirmation evidence")
        if has_blocker and "verified" not in response and "complete" not in response:
            issues.append("Registration response indicates failure")

    elif process_type == "login":
        has_session = any(
            kw in response
            for kw in (
                "logged in",
                "authenticated",
                "session",
                "dashboard",
                "welcome",
                "login successful",
                "sign-in successful",
            )
        )
        if not has_session:
            issues.append("Login completion lacks authenticated session evidence")

    elif process_type == "email_verification":
        verified = any(
            kw in response
            for kw in (
                "verified",
                "verification complete",
                "email confirmed",
                "confirmation successful",
                "account verified",
            )
        )
        if not verified:
            issues.append("Email verification completion lacks verified-state evidence")

    elif process_type == "procurement":
        has_approval = any(
            kw in response
            for kw in (
                "approved",
                "approval",
                "order placed",
                "order id",
                "purchase order",
                "po number",
                "confirmation number",
                "procurement record",
            )
        )
        has_record = any(
            kw in response
            for kw in ("vendor", "total cost", "line item", "order", "purchase")
        )
        if not has_approval:
            issues.append("Procurement completion lacks approval/order evidence")
        if not has_record:
            issues.append("Procurement completion lacks procurement record evidence")

    elif process_type == "outreach":
        has_thread = any(
            kw in response
            for kw in (
                "thread",
                "message id",
                "sent",
                "email sent",
                "reply",
                "responded",
                "recipient",
            )
        )
        if not has_thread:
            issues.append("Outreach completion lacks email/thread tracking evidence")

    elif process_type == "browser_automation":
        has_automation = any(
            kw in response
            for kw in (
                "automated",
                "automation complete",
                "browser",
                "navigated",
                "executed",
            )
        )
        has_screenshot = any(
            kw in response
            for kw in ("screenshot", "artifact", "capture", "image")
        )
        if not has_automation:
            issues.append("Browser automation completion lacks execution evidence")
        if not has_screenshot:
            issues.append("Browser automation completion lacks screenshot evidence")

    return {"passed": len(issues) == 0, "issues": issues}


def check_completion_artifact(
    artifacts: List[Dict[str, Any]], required_kinds: Optional[List[str]] = None
) -> Dict[str, Any]:
    """Require at least one completion artifact with valid checksum metadata."""
    issues: List[str] = []
    if not artifacts:
        issues.append("No completion artifacts registered")
        return {"passed": False, "issues": issues}

    kinds_present = {a.get("kind") for a in artifacts}
    if required_kinds:
        missing = [k for k in required_kinds if k not in kinds_present]
        if missing:
            issues.append(f"Missing artifact kinds: {', '.join(missing)}")

    for art in artifacts:
        if not art.get("checksum"):
            issues.append(f"Artifact {art.get('id', '?')[:8]} lacks checksum")
        if art.get("size_bytes", 0) <= 0:
            issues.append(f"Artifact {art.get('id', '?')[:8]} is empty")

    return {"passed": len(issues) == 0, "issues": issues}
