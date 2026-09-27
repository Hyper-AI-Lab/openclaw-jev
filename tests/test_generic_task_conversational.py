"""GenericTaskWorkflow conversational path is still RMP-owned and always gated."""
import inspect

from app.workflows import generic_execute_child, generic_task


def test_quality_gate_runs_before_slack_on_generic_complete():
    source = inspect.getsource(generic_task.GenericTaskWorkflow._plan_driven_loop)
    assert "verify_response_quality" in source
    assert "notify_slack_user" in source
    assert source.find("verify_response_quality") < source.rfind("notify_slack_user")


def test_conversational_skips_mid_step_memory_refresh():
    source = inspect.getsource(generic_task.GenericTaskWorkflow._plan_driven_loop)
    assert "if not is_conversational:" in source


def test_execute_child_reserves_with_task_type_and_tags():
    parent = inspect.getsource(generic_task.GenericTaskWorkflow._plan_driven_loop)
    child = inspect.getsource(generic_execute_child.GenericExecuteChildWorkflow.run)
    assert '"task_type": task_type' in parent
    assert '"tags": tags' in parent
    assert '"task_type": payload.get("task_type") or ""' in child
    assert '"tags": payload.get("tags") or []' in child


def test_generic_stop_acks_and_aborts_deliver_step():
    parent = inspect.getsource(generic_task.GenericTaskWorkflow._plan_driven_loop)
    finish = inspect.getsource(generic_task.GenericTaskWorkflow._finish_stop)
    assert "notify_slack_user" in finish
    assert "stopped as requested" in finish
    assert "start_child_workflow" in parent
    assert "ParentClosePolicy.TERMINATE" in parent
    assert "_stop_pending()" in parent
    assert parent.find("wait_condition") < parent.find("result = await handle")


def test_conversational_defers_episodic_in_child():
    parent = inspect.getsource(generic_task.GenericTaskWorkflow._plan_driven_loop)
    child = inspect.getsource(generic_execute_child.GenericExecuteChildWorkflow.run)
    assert '"defer_episodic_write": is_conversational' in parent
    assert "if not defer_episodic_write:" in child
