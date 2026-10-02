from app.orchestrator.process_brief import with_catchup
from app.task_registry.stop_command import is_whole_message_stop

BRIEF = "USER CATCH-UP (attach/rebuild):\nKirill asked to stop the old order and keep the new one."


def test_exact_stop_words():
    assert is_whole_message_stop("stop")
    assert is_whole_message_stop("STOP")
    assert is_whole_message_stop("cancel")
    assert is_whole_message_stop("abort")
    assert is_whole_message_stop("halt")
    assert is_whole_message_stop("please stop")
    assert is_whole_message_stop("Please stop.")
    assert is_whole_message_stop("...stop")
    assert is_whole_message_stop("stop!")


def test_incidental_stop_is_not_a_command():
    assert not is_whole_message_stop("don't stop the canary yet")
    assert not is_whole_message_stop("please stop the task")
    assert not is_whole_message_stop("cancel the order after you finish")
    assert not is_whole_message_stop("are you here")
    assert not is_whole_message_stop("")


def test_a_signal_with_a_catch_up_brief_is_judged_on_kirills_words():
    assert is_whole_message_stop(with_catchup(BRIEF, "stop"))
    assert not is_whole_message_stop(with_catchup(BRIEF, "keep going"))
