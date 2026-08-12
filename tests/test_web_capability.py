"""Tests for WebCapabilityAnalyzer routing matrix."""
from app.orchestrator.web_capability import (
    analyze_web_capability,
    classify_web_intent,
    merge_web_into_intake,
)


def test_search_intent():
    assert classify_web_intent("search the web for MiniMax M3 benchmarks") == "search"
    a = analyze_web_capability("look up online the latest Brave Search API pricing")
    assert a["web_intent"] == "search"
    assert a["preferred_tools"][0] == "web_search"
    assert "langsearch_search" in a["preferred_tools"]
    assert "WEB CAPABILITY BRIEF" in a["web_brief"]


def test_fetch_intent_with_url():
    assert classify_web_intent("summarize https://example.com/docs") == "fetch"
    a = analyze_web_capability("read this page https://example.com/a")
    assert a["preferred_tools"][0] == "jina_reader"


def test_crawl_intent():
    assert classify_web_intent("crawl the site https://example.com and list all pages") == "crawl"
    a = analyze_web_capability("bulk crawl research on example.com")
    assert a["web_intent"] == "crawl"
    assert "crawlee_crawl" in a["preferred_tools"] or "crawl4ai" in a["preferred_tools"]


def test_schema_extract_intent():
    assert (
        classify_web_intent("extract these fields from this site: name, price")
        == "schema_extract"
    )
    a = analyze_web_capability("pull these fields from https://shop.example/product")
    assert a["preferred_tools"][0] == "scrapegraph_extract"


def test_adaptive_extract_intent():
    assert classify_web_intent("use scrapling adaptive scrape on this fragile DOM") == "adaptive_extract"


def test_interact_intent():
    assert classify_web_intent("open the browser and take a screenshot of moltmarket") == "interact"
    a = analyze_web_capability("click the login button and fill the form")
    assert a["catalog_hint"] == "browser_automation"
    assert a["preferred_tools"][0] == "browser"


def test_meta_tools_question_not_interact():
    # Asking about tools should not force browser catalog
    intent = "what web-search tools do you have?"
    assert classify_web_intent(intent) == "none"
    a = analyze_web_capability(intent)
    assert "web_capability_status" in a["preferred_tools"]


def test_llm_cannot_force_interact_without_rules():
    a = analyze_web_capability(
        "what is the weather conceptually",
        llm_web_intent="interact",
    )
    assert a["web_intent"] != "interact"


def test_merge_web_into_intake_adds_brief():
    decision = {
        "decision": "create_fresh",
        "catalog_type": None,
        "guidance_notes": "",
        "policy_overrides": [],
    }
    out = merge_web_into_intake(decision, "search the web for OpenClaw plugins")
    assert out["web_capability"]["web_intent"] == "search"
    assert "WEB CAPABILITY BRIEF" in out["guidance_notes"]
