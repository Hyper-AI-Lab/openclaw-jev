from app.workflows.catalog import (
    CATALOG,
    catalog_type_for_workflow,
    get_template,
    list_catalog,
    normalize_catalog_type,
    resolve_catalog_template,
)


def test_catalog_lists_templates():
    templates = list_catalog()
    ids = {t["process_type"] for t in templates}
    assert "account_registration" in ids
    assert "login" in ids
    assert "email_verification" in ids
    assert "procurement" in ids
    assert "outreach" in ids
    assert "browser_automation" in ids


def test_catalog_templates_have_version():
    for entry in list_catalog():
        assert entry["version"] == 1
    for template in CATALOG.values():
        assert template.version == 1


def test_resolve_login_intent():
    t = resolve_catalog_template("Please log in to my GitHub account")
    assert t == "login"


def test_resolve_procurement_intent():
    t = resolve_catalog_template("Procure 50 office chairs from the best vendor")
    assert t == "procurement"


def test_resolve_outreach_intent():
    t = resolve_catalog_template("Send an email follow-up to the vendor")
    assert t == "outreach"


def test_resolve_browser_automation_intent():
    t = resolve_catalog_template("Automate the browser to navigate to the dashboard")
    assert t == "browser_automation"


def test_resolve_moltmarket_intent():
    t = resolve_catalog_template("Check my MoltMarket notifications")
    assert t == "browser_automation"


def test_moltmarket_check_alias():
    assert normalize_catalog_type("moltmarket_check", "") == "browser_automation"
    assert catalog_type_for_workflow(
        "moltmarket_check",
        "Check my MoltMarket notifications",
        "moltmarket_check",
    ) == "browser_automation"


def test_unknown_hint_falls_back_to_generic_routing():
    assert catalog_type_for_workflow(
        "totally_unknown_type",
        "What is the weather today?",
        "user",
    ) is None


def test_email_followup_alias():
    assert normalize_catalog_type("email_followup", "") == "outreach"


def test_resolve_none_for_generic():
    t = resolve_catalog_template("What is the weather today?")
    assert t is None


def test_procurement_has_approval_gate():
    template = get_template("procurement")
    assert template is not None
    kinds = [s.kind for s in template.steps]
    assert "approval_gate" in kinds
    assert template.success_criteria.get("required_artifact_kinds") == [
        "completion_output",
        "procurement_record",
    ]


def test_outreach_has_wait_external():
    template = get_template("outreach")
    assert template is not None
    wait_steps = [s for s in template.steps if s.kind == "wait_external"]
    assert len(wait_steps) == 1
    assert wait_steps[0].name == "wait_for_reply"


def test_browser_automation_has_approval_and_screenshot():
    template = get_template("browser_automation")
    assert template is not None
    assert any(s.kind == "approval_gate" for s in template.steps)
    assert template.success_criteria.get("required_artifact_kinds") == [
        "completion_output",
        "screenshot",
    ]
    assert any(s.name == "capture_screenshot" for s in template.steps)
