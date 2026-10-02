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
    assert "coding_task" in ids and "tool_self_upgrade" not in ids


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


def test_loose_browser_mention_does_not_force_catalog():
    """Plugin-style soft hints must not force catalog on conversational questions."""
    assert (
        catalog_type_for_workflow(
            "browser_automation",
            "How do you do the web search? I open a browser and type into google.",
            "user",
        )
        is None
    )


def test_hint_confirms_matching_browser_intent():
    assert (
        catalog_type_for_workflow(
            "browser_automation",
            "Automate the browser to navigate to the dashboard",
            "user",
        )
        == "browser_automation"
    )


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


def test_resolve_coding_request_intent():
    assert resolve_catalog_template("Please fix the bug in the reconciler code and open a PR") == "coding_task"
    assert resolve_catalog_template("Please self-upgrade and add a new plugin to your arsenal") == "coding_task"


def test_self_upgrade_names_are_aliases_of_coding_task():
    for alias in ("tool_self_upgrade", "self_upgrade", "capability_upgrade"):
        assert normalize_catalog_type(alias, "") == "coding_task"


def test_coding_task_is_run_by_the_coding_workflow():
    template = get_template("coding_task")
    assert template is not None and template.steps == [], "CodingTaskWorkflow runs it, not catalog steps"
    assert template.success_criteria.get("requires_human_approval") is True
    assert template.success_criteria.get("requires_tests_passed") is True
    assert template.success_criteria.get("requires_verify_ok") is True


def test_loose_upgrade_mention_does_not_force_catalog():
    assert (
        catalog_type_for_workflow(
            "tool_self_upgrade",
            "How do software upgrades usually work in general?",
            "user",
        )
        is None
    )


def test_hint_confirms_matching_coding_intent():
    assert (
        catalog_type_for_workflow(
            "coding_task",
            "Please fix the failing test in the reconciler and open a PR",
            "user",
        )
        == "coding_task"
    )


def test_awareness_of_self_upgrade_is_not_catalog():
    intent = (
        "I added some functionality for you: now you can self-upgrade and add a "
        "plugin to your arsenal. Are you aware of that?"
    )
    assert resolve_catalog_template(intent) is None
    assert catalog_type_for_workflow(None, intent, "user") is None
    assert catalog_type_for_workflow("coding_task", intent, "user") is None


def test_questions_about_code_and_awareness_of_coding_are_not_catalog():
    for intent in ("How does the reconciler decide a task is stale?", "Can you code now with Claude Code?",
                   "Explain what the coding workflow does when tests fail."):
        assert resolve_catalog_template(intent) is None, intent


def test_follow_up_after_you_said_is_not_outreach():
    intent = (
        'I thought you will follow up after you said "Let me check the plugins '
        'directory". no?'
    )
    assert resolve_catalog_template(intent) is None
    assert catalog_type_for_workflow(None, intent, "user") is None


def test_actionable_coding_request_still_matches():
    assert resolve_catalog_template("Add a test for the stale task threshold and fix the bug it finds") == "coding_task"


def test_degraded_intake_never_regex_assigns_catalog():
    from app.workflows.catalog import catalog_assignment_from_intake

    assert (
        catalog_assignment_from_intake(
            intake_ran=False, intake_catalog_type="coding_task"
        )
        is None
    )
    assert (
        catalog_assignment_from_intake(intake_ran=True, intake_catalog_type="login")
        == "login"
    )
    assert (
        catalog_assignment_from_intake(intake_ran=True, intake_catalog_type=None) is None
    )
