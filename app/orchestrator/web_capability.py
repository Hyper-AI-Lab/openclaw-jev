"""Analyze user intent and route to the right web capability tools."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

# Intent classes → preferred OpenClaw / aura_web tools (primary first).
ROUTING: Dict[str, Dict[str, Any]] = {
    "search": {
        "preferred_tools": ["web_search", "langsearch_search"],
        "fallbacks": ["langsearch_search"],
        "catalog_hint": None,
        "tool_budget": 3,
    },
    "fetch": {
        "preferred_tools": ["jina_reader", "web_fetch", "crawl4ai"],
        "fallbacks": ["crawl4ai", "scrapling"],
        "catalog_hint": None,
        "tool_budget": 3,
    },
    "crawl": {
        "preferred_tools": ["crawl4ai", "crawlee_crawl"],
        "fallbacks": ["crawlee_crawl", "scrapling"],
        "catalog_hint": None,
        "tool_budget": 4,
    },
    "adaptive_extract": {
        "preferred_tools": ["scrapling", "crawl4ai"],
        "fallbacks": ["jina_reader", "web_fetch"],
        "catalog_hint": None,
        "tool_budget": 4,
    },
    "schema_extract": {
        "preferred_tools": ["scrapegraph_extract", "scrapling"],
        "fallbacks": ["jina_reader", "crawl4ai"],
        "catalog_hint": None,
        "tool_budget": 4,
    },
    "interact": {
        "preferred_tools": ["browser", "browser_use", "obscura_browse"],
        "fallbacks": ["browser_use", "obscura_browse"],
        "catalog_hint": "browser_automation",
        "tool_budget": 6,
    },
    "none": {
        "preferred_tools": [],
        "fallbacks": [],
        "catalog_hint": None,
        "tool_budget": None,
    },
}

_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)

_INTERACT_RE = re.compile(
    r"\b("
    r"browser\s*automation|open\s+(the\s+)?browser|click|fill\s+(in\s+)?(the\s+)?form|"
    r"log\s*in|login|sign\s*in|screenshot|navigate\s+to|type\s+into|"
    r"press\s+(the\s+)?button|interact\s+with\s+(the\s+)?(page|site)|playwright|"
    r"moltmarket|captcha"
    r")\b",
    re.I,
)

_SCHEMA_RE = re.compile(
    r"\b("
    r"extract\s+(these\s+)?fields|pull\s+(these\s+)?fields|schema\s+extract|"
    r"structured\s+extract|scrapegraph|json\s+schema|typed\s+json|"
    r"extract\s+.+\s+from\s+(this\s+)?(url|page|site)"
    r")\b",
    re.I,
)

_ADAPTIVE_RE = re.compile(
    r"\b("
    r"scrapling|adaptive\s+scrape|fragile\s+dom|selector\s+broke|"
    r"cloudflare|anti[- ]?bot\s+scrape|turnstile"
    r")\b",
    re.I,
)

_CRAWL_RE = re.compile(
    r"\b("
    r"crawl\s+(the\s+)?(site|website|domain)|multi[- ]?page|site\s*map|"
    r"bulk\s+(crawl|scrape|research)|crawlee|all\s+pages|spider\s+the"
    r")\b",
    re.I,
)

_FETCH_RE = re.compile(
    r"\b("
    r"fetch\s+(the\s+)?(url|page)|read\s+(this|the)\s+(url|page|article|link)|"
    r"open\s+(this|the)\s+link|summarize\s+(this|the)\s+(url|page|article|link)|"
    r"jina|web_fetch|what\s+does\s+this\s+page"
    r")\b",
    re.I,
)

_SEARCH_RE = re.compile(
    r"\b("
    r"search\s+(the\s+)?web|web\s*search|google\s+|look\s+up\s+online|"
    r"find\s+(online|on\s+the\s+web|on\s+the\s+internet)|langsearch|"
    r"brave\s+search|latest\s+news|what\s+is\s+the\s+current"
    r")\b",
    re.I,
)

# Soft: questions about our own web tooling — not a live web job.
_META_TOOLS_RE = re.compile(
    r"\b("
    r"what\s+web[- ]?(search\s+)?tools|which\s+web[- ]?tools|"
    r"web\s+capability|do\s+you\s+have\s+(web\s+)?search|"
    r"langsearch|crawl4ai|perplexity|jina\s+reader"
    r")\b",
    re.I,
)


def classify_web_intent(intent: str) -> str:
    text = (intent or "").strip()
    if not text:
        return "none"
    if _META_TOOLS_RE.search(text) and not _URL_RE.search(text) and not _INTERACT_RE.search(text):
        # Asking about tools → still useful to bias status tool, treat as none for catalog
        if re.search(r"\b(status|available|configured|what\s+do\s+you\s+have)\b", text, re.I):
            return "none"
    if _INTERACT_RE.search(text):
        return "interact"
    if _SCHEMA_RE.search(text):
        return "schema_extract"
    if _ADAPTIVE_RE.search(text):
        return "adaptive_extract"
    if _CRAWL_RE.search(text):
        return "crawl"
    if _URL_RE.search(text) or _FETCH_RE.search(text):
        # URL alone often means fetch; search keywords override below
        if _SEARCH_RE.search(text) and not _URL_RE.search(text):
            return "search"
        return "fetch"
    if _SEARCH_RE.search(text):
        return "search"
    return "none"


def analyze_web_capability(
    intent: str,
    *,
    llm_web_intent: Optional[str] = None,
) -> Dict[str, Any]:
    """Return routing decision for RMP intake / prompt injection."""
    rule_intent = classify_web_intent(intent)
    web_intent = rule_intent
    if llm_web_intent:
        candidate = str(llm_web_intent).strip().lower()
        if candidate in ROUTING:
            # Soft: LLM may upgrade/downgrade but interact requires rule confirmation
            if candidate == "interact" and rule_intent != "interact":
                web_intent = rule_intent
            elif rule_intent == "none" and candidate != "none":
                web_intent = candidate
            elif rule_intent != "none":
                web_intent = rule_intent

    route = ROUTING[web_intent]
    preferred = list(route["preferred_tools"])
    if web_intent == "none" and _META_TOOLS_RE.search(intent or ""):
        preferred = ["web_capability_status"]

    brief = format_web_capability_brief(web_intent, preferred, route.get("fallbacks") or [])
    return {
        "web_intent": web_intent,
        "preferred_tools": preferred,
        "fallbacks": list(route.get("fallbacks") or []),
        "catalog_hint": route.get("catalog_hint"),
        "tool_budget": route.get("tool_budget"),
        "web_brief": brief,
    }


def format_web_capability_brief(
    web_intent: str,
    preferred: List[str],
    fallbacks: List[str],
) -> str:
    if web_intent == "none" and not preferred:
        return ""
    lines = [
        "WEB CAPABILITY BRIEF (Aura galaxy stack — use these real tools; do NOT invent Perplexity/Firecrawl/Tavily unless configured):",
        f"- web_intent: {web_intent}",
        f"- preferred_tools: {', '.join(preferred) if preferred else '(none)'}",
    ]
    if fallbacks:
        lines.append(f"- fallbacks: {', '.join(fallbacks)}")
    lines.append(
        "- routing: search→Brave web_search then langsearch_search; "
        "fetch→jina_reader then web_fetch then crawl4ai; "
        "crawl→crawl4ai then crawlee_crawl; "
        "adaptive→scrapling; schema→scrapegraph_extract; "
        "interact→browser then browser_use then obscura_browse."
    )
    lines.append("- For tool inventory/health call web_capability_status.")
    return "\n".join(lines) + "\n"


def merge_web_into_intake(
    decision: Dict[str, Any],
    intent: str,
    *,
    llm_result: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Attach web_capability to intake policy result; soft-set catalog for interact."""
    llm_web = None
    if llm_result:
        llm_web = llm_result.get("web_intent") or llm_result.get("web_capability")
        if isinstance(llm_web, dict):
            llm_web = llm_web.get("web_intent")
    analysis = analyze_web_capability(intent, llm_web_intent=llm_web)
    decision["web_capability"] = analysis

    # Soft catalog: only if analyzer says interact AND catalog not already set
    if analysis.get("catalog_hint") == "browser_automation" and not decision.get("catalog_type"):
        from app.workflows.catalog import catalog_type_for_workflow

        cat = catalog_type_for_workflow(
            "browser_automation",
            intent,
            "browser_automation",
        )
        if cat:
            decision["catalog_type"] = cat
            overrides = list(decision.get("policy_overrides") or [])
            overrides.append("web_capability_interact_catalog")
            decision["policy_overrides"] = overrides

    # Append brief into guidance_notes for create_guided / memory path
    brief = analysis.get("web_brief") or ""
    if brief:
        notes = (decision.get("guidance_notes") or "").strip()
        decision["guidance_notes"] = f"{notes}\n{brief}".strip() if notes else brief
    return decision
