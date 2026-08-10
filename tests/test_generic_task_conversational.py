"""GenericTaskWorkflow conversational fast-complete path."""
import inspect

from app.workflows import generic_execute_child, generic_task


def test_conversational_block_notifies_before_memory_promotion():
    source = inspect.getsource(generic_task.GenericTaskWorkflow)
    marker = "if is_conversational and clean_result.strip():"
    start = source.find(marker)
    assert start != -1, "conversational fast-complete block missing"
    block = source[start : start + 3500]
    notify_pos = block.find("notify_slack_user")
    episodic_pos = block.find("write_episodic_observation")
    promote_pos = block.find("promote_completion_memory")
    assert notify_pos != -1, "notify_slack_user missing from conversational path"
    assert episodic_pos != -1, "write_episodic_observation missing from conversational path"
    assert promote_pos != -1, "promote_completion_memory missing from conversational path"
    assert notify_pos < episodic_pos < promote_pos


def test_conversational_path_returns_before_quality_loop():
    source = inspect.getsource(generic_task.GenericTaskWorkflow)
    marker = "if is_conversational and clean_result.strip():"
    start = source.find(marker)
    block = source[start : start + 3500]
    return_pos = block.find('return {"status": "completed"')
    assert return_pos != -1
    conv_only = block[:return_pos]
    assert "verify_response_quality" not in conv_only


def test_conversational_skips_mid_step_memory_refresh():
    source = inspect.getsource(generic_task.GenericTaskWorkflow._plan_driven_loop)
    assert "if not is_conversational:" in source


def test_conversational_defers_episodic_in_child():
    parent = inspect.getsource(generic_task.GenericTaskWorkflow._plan_driven_loop)
    child = inspect.getsource(generic_execute_child.GenericExecuteChildWorkflow.run)
    assert '"defer_episodic_write": is_conversational' in parent
    assert "if not defer_episodic_write:" in child
