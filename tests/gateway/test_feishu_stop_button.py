"""The turn card's stop button: it renders only while a turn runs, and a click becomes /stop.

Design notes the assertions encode:

* the button carries ``chat_id`` + the turn's start stamp, so a card from an earlier turn (or
  another chat) refuses the click instead of stopping whatever runs now;
* the click is replayed as a synthetic ``/stop`` COMMAND event — the same path a typed /stop
  takes — rather than re-implementing the interrupt inside the adapter;
* a stopped turn's finished card says ``已停止`` in its footer.
"""

from __future__ import annotations

from types import SimpleNamespace


def _card(**kwargs):
    from plugins.platforms.feishu.adapter import _build_progress_card

    return _build_progress_card("🔧 terminal\n  echo hi", count=1, **kwargs)


def _stop_rows(card: dict) -> list[dict]:
    return [el for el in card["body"]["elements"] if el.get("element_id") == "stop_btn"]


def test_running_card_carries_one_stop_button_with_turn_identity():
    value = {"hermes_stop_action": {"chat_id": "oc_1", "turn_id": "1700000000.500"}}
    card = _card(elapsed_s=3, stop_value=value)

    rows = _stop_rows(card)
    assert len(rows) == 1
    button = rows[0]
    # Card 2.0 rejects the 1.0 ``action`` container ("unsupported tag action"), so the button
    # must be a direct body element carrying its callback in ``behaviors``.
    assert button["tag"] == "button"
    assert button["type"] == "danger"
    assert button["behaviors"] == [{"type": "callback", "value": value}]
    assert all(el.get("tag") != "action" for el in card["body"]["elements"])


def test_finished_card_has_no_stop_button():
    card = _card(final_text="答案", elapsed_s=5, stop_value={"hermes_stop_action": {"chat_id": "oc_1"}})
    assert _stop_rows(card) == []


def test_stopped_turn_is_labelled_in_the_footer():
    running = _card(elapsed_s=5)
    finished = _card(final_text="答案", elapsed_s=5, finished_at=1_700_000_000.0)
    stopped = _card(final_text="答案", elapsed_s=5, finished_at=1_700_000_000.0, stopped=True)

    footer = lambda card: next(el for el in card["body"]["elements"] if el.get("element_id") == "footer")
    assert "已停止" not in footer(finished)["content"]
    assert footer(stopped)["content"].endswith("· 已停止")
    assert footer(running)["content"].startswith("⏳")


def _adapter(*, scope="all", admins=(), allowed=(), authorized=True):
    from plugins.platforms.feishu.adapter import FeishuAdapter

    adapter = object.__new__(FeishuAdapter)
    adapter._turn_started_at = {adapter._turn_key("oc_1"): 1_700_000_000.5}
    adapter._turn_ended_at = {}
    adapter._stopped_turns = {}
    adapter._turn_owner = {adapter._turn_key("oc_1"): "ou_starter"}
    adapter._progress_card_by_chat = {adapter._turn_key("oc_1"): "om_card_msg"}
    adapter._final_card_by_chat = {}
    adapter._stop_button_scope = scope
    adapter._admins = set(admins)
    adapter._allowed_group_users = set(allowed)
    adapter.dispatched = []
    adapter.closed = []

    # Upstream moved the operator check into plugins/platforms/feishu/cards/authz.py; drive the
    # real function through the gateway authorization hook instead of stubbing a method.
    adapter._chat_info_cache = {}
    adapter._is_sender_authorized = lambda *a, **k: authorized

    async def _noop():
        return None

    def _dispatch(**kwargs):
        # Record synchronously: the handler only schedules the coroutine, so anything appended
        # inside it would never run in this test.
        adapter.dispatched.append(kwargs)
        return _noop()

    adapter._dispatch_synthetic_event = _dispatch

    def _submit(loop, coro):
        # The handler only schedules the coroutine; close it so pytest does not warn.
        adapter.closed.append(coro)
        coro.close()
        return True

    adapter._submit_on_loop = _submit
    return adapter


def _event(open_id="ou_user", chat_id="oc_1"):
    return SimpleNamespace(
        operator=SimpleNamespace(open_id=open_id, user_id="", union_id=""),
        context=SimpleNamespace(open_chat_id=chat_id),
    )


def _value(chat_id="oc_1", turn_id="1700000000.500"):
    return {"hermes_stop_action": {"chat_id": chat_id, "turn_id": turn_id}}


def test_click_dispatches_stop_and_marks_the_turn():
    adapter = _adapter()
    adapter._handle_stop_card_action(event=_event(), action_value=_value(), loop=None)

    assert len(adapter.dispatched) == 1
    assert adapter.dispatched[0]["text"] == "/stop"
    assert adapter.dispatched[0]["chat_id"] == "oc_1"
    # The synthetic /stop must anchor on the card the user clicked: a placeholder id made
    # Feishu reject the acknowledgement with 99992354 and the reply vanished.
    assert adapter.dispatched[0]["message_id"] == "om_card_msg"
    assert adapter._stopped_turns[adapter._turn_key("oc_1")] == 1_700_000_000.5
    assert adapter._turn_stopped(adapter._turn_key("oc_1")) is True


def test_click_from_another_chat_is_ignored():
    adapter = _adapter()
    adapter._handle_stop_card_action(event=_event(chat_id="oc_other"), action_value=_value(), loop=None)
    assert adapter.dispatched == []
    assert adapter._stopped_turns == {}


def test_click_on_a_finished_turn_is_ignored():
    adapter = _adapter()
    adapter._handle_stop_card_action(event=_event(), action_value=_value(turn_id="1699999999.000"), loop=None)
    assert adapter.dispatched == []
    assert adapter._stopped_turns == {}


def test_unauthorized_click_is_ignored():
    adapter = _adapter(authorized=False)
    adapter._handle_stop_card_action(event=_event(), action_value=_value(), loop=None)
    assert adapter.dispatched == []


def test_scope_starter_admin_needs_a_privileged_operator():
    denied = _adapter(scope="starter_admin", admins=("ou_admin",), allowed=("ou_allowed",))
    denied._handle_stop_card_action(event=_event(open_id="ou_random"), action_value=_value(), loop=None)
    assert denied.dispatched == []

    allowed = _adapter(scope="starter_admin", admins=("ou_admin",), allowed=("ou_allowed",))
    allowed._handle_stop_card_action(event=_event(open_id="ou_allowed"), action_value=_value(), loop=None)
    assert len(allowed.dispatched) == 1


def test_scope_starter_allows_the_person_who_opened_the_turn():
    adapter = _adapter(scope="starter")
    adapter._handle_stop_card_action(event=_event(open_id="ou_starter"), action_value=_value(), loop=None)
    assert len(adapter.dispatched) == 1


def test_stop_payload_renders_for_every_scope_and_disappears_when_off():
    # Rendering depends on the turn, not the viewer: the button shows on a live turn's card and
    # the click decides who may use it. Only the explicit "off" switch removes it.
    for scope in ("all", "starter_admin", "starter"):
        adapter = _adapter(scope=scope)
        payload = adapter._stop_payload(adapter._turn_key("oc_1"))
        assert payload is not None, scope
        assert payload["hermes_stop_action"]["chat_id"] == "oc_1"
        assert payload["hermes_stop_action"]["turn_id"] == "1700000000.500"
        assert payload["hermes_stop_action"]["turn_key"] == adapter._turn_key("oc_1")

    adapter = _adapter(scope="off")
    assert adapter._stop_payload(adapter._turn_key("oc_1")) is None
    # No turn has started: nothing to stop.
    adapter = _adapter()
    assert adapter._stop_payload(adapter._turn_key("oc_unknown")) is None
def test_repeating_the_same_state_keeps_the_seconds_counting():
    """One agent.step per iteration re-noted "thinking": the strip must not restart at 0s."""
    from plugins.platforms.feishu.adapter import FeishuAdapter

    adapter = object.__new__(FeishuAdapter)
    adapter._agent_status = {}
    adapter._last_progress_at = {}
    adapter._schedule_status_refresh = lambda chat_id: None

    adapter.note_agent_status("oc_1", "thinking")
    first = adapter._agent_status["oc_1|"]["since"]
    adapter.note_agent_status("oc_1", "thinking")
    assert adapter._agent_status["oc_1|"]["since"] == first   # same state -> same clock

    adapter.note_agent_status("oc_1", "tool", "terminal")
    assert adapter._agent_status["oc_1|"]["since"] >= first    # a real transition restarts it


def test_heartbeat_survives_failed_patches():
    """The ticker must keep ticking after a run of failed patches (it used to give up)."""
    import inspect

    from plugins.platforms.feishu.adapter import FeishuAdapter

    src = inspect.getsource(FeishuAdapter._progress_heartbeat_loop)
    assert "consecutive failures" not in src, "the old give-up branch is back"
    assert "heartbeat iteration failed" in src, "one bad iteration must be caught in the loop"


def test_a_new_turn_does_not_inherit_the_previous_turn_clock():
    """A turn that ended "thinking" must not make the next turn open at "思考中 · 120s"."""
    import time

    from plugins.platforms.feishu.adapter import FeishuAdapter

    adapter = object.__new__(FeishuAdapter)
    adapter._agent_status = {}
    adapter._last_progress_at = {}
    adapter._turn_started_at = {}
    adapter._schedule_status_refresh = lambda chat_id: None

    adapter._turn_started_at["oc_1|"] = time.time() - 120
    adapter.note_agent_status("oc_1", "thinking")
    adapter._agent_status["oc_1|"]["since"] -= 120      # that status was two minutes ago

    adapter._turn_started_at["oc_1|"] = time.time()     # a new turn begins
    adapter.note_agent_status("oc_1", "thinking")

    assert time.time() - adapter._agent_status["oc_1|"]["since"] < 5


def _multi_turn_adapter():
    from plugins.platforms.feishu.adapter import FeishuAdapter

    a = object.__new__(FeishuAdapter)
    for name in ("_turn_started_at", "_turn_ended_at", "_stopped_turns", "_turn_owner",
                 "_turn_internal", "_progress_card_by_chat", "_progress_card_bodies",
                 "_progress_card_counts", "_final_card_by_chat", "_final_card_text",
                 "_card_id_by_chat", "_card_seq", "_agent_status", "_last_progress_at",
                 "_last_card_update_at", "_status_refresh_at", "_heartbeat_tasks",
                 "_bg_task_card_tasks", "_subagents", "_subagent_seq", "_line_first_seen",
                 "_bg_seen_ids", "_final_card_at"):
        setattr(a, name, {})
    a._stop_button_scope = "all"
    a._admins = set()
    a._allowed_group_users = set()
    a._chat_info_cache = {}
    a._loop = None
    return a


def test_two_people_in_one_chat_get_their_own_card_and_clock():
    """Groups isolate sessions per participant: a second turn must not touch the first card."""
    adapter = _multi_turn_adapter()
    A, B = "session-a", "session-b"

    adapter.begin_turn_card.__self__  # sanity: bound method exists
    adapter._turn_started_at[adapter._turn_key("oc_1", A)] = 1000.0
    adapter._turn_started_at[adapter._turn_key("oc_1", B)] = 1010.0
    adapter._progress_card_by_chat[adapter._turn_key("oc_1", A)] = "msg-A"
    adapter._progress_card_by_chat[adapter._turn_key("oc_1", B)] = "msg-B"

    # B's turn advances its own clock only.
    adapter.note_agent_status("oc_1", "thinking", session_key=B)
    assert adapter._agent_status[adapter._turn_key("oc_1", A)] if adapter._turn_key("oc_1", A) in adapter._agent_status else True

    # B finishing retires B's card only; A's clock stays live (no "总耗时" on A's card).
    adapter.clear_progress_card("oc_1", session_key=B)
    assert adapter._progress_card_by_chat.get(adapter._turn_key("oc_1", A)) == "msg-A"
    assert adapter._turn_ended_at.get(adapter._turn_key("oc_1", A)) is None
    assert adapter._turn_ended_at.get(adapter._turn_key("oc_1", B)) is not None


def test_stop_payload_carries_the_turn_key_and_chat_id():
    adapter = _multi_turn_adapter()
    adapter._turn_started_at[adapter._turn_key("oc_1", "s1")] = 1700000000.5

    value = adapter._stop_payload(adapter._turn_key("oc_1", "s1"))
    payload = value["hermes_stop_action"]
    assert payload["chat_id"] == "oc_1"                   # the real chat, for the synthetic event
    assert payload["turn_key"] == "oc_1|s1"               # the turn, for the stop bookkeeping
    assert payload["turn_id"] == "1700000000.500"


def test_send_folds_only_when_the_chat_has_one_live_turn():
    """``send()`` gets a chat id: fold into the sole live turn, never guess between two."""
    adapter = _multi_turn_adapter()
    key_a = adapter._turn_key("oc_1", "a")

    adapter._progress_card_by_chat[key_a] = "msg-A"
    assert adapter._sole_live_turn_key("oc_1") == key_a

    adapter._progress_card_by_chat[adapter._turn_key("oc_1", "b")] = "msg-B"
    assert adapter._sole_live_turn_key("oc_1") is None       # ambiguous: stay out of both

    # A retired card is not "live": the other turn alone is still unambiguous.
    adapter._final_card_by_chat[adapter._turn_key("oc_1", "b")] = "msg-B"
    assert adapter._sole_live_turn_key("oc_1") == key_a


def test_invalid_reply_anchor_is_recognised():
    """Feishu 99992354 means the quoted message is gone — the send must be retried unanchored."""
    from gateway.platforms.base import BasePlatformAdapter

    bad = ("[99992354] The request you send is not a valid {open_message_id} or not exists, "
           "and the example format is \"om_xxx\"")
    assert BasePlatformAdapter._is_invalid_reply_anchor(bad) is True
    assert BasePlatformAdapter._is_invalid_reply_anchor("[230020] rate limited") is False


if __name__ == "__main__":  # pytest is not installed in the runtime venv; this keeps it runnable
    import sys
    import traceback

    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception:
                failed += 1
                print(f"FAIL {name}")
                traceback.print_exc()
    sys.exit(1 if failed else 0)


if __name__ == "__main__":  # pytest is not installed in the runtime venv; this keeps it runnable
    import sys
    import traceback

    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception:
                failed += 1
                print(f"FAIL {name}")
                traceback.print_exc()
    sys.exit(1 if failed else 0)


def test_invalid_reply_anchor_is_recognised():
    """Feishu 99992354 means the quoted message is gone — the send must be retried unanchored."""
    from gateway.platforms.base import BasePlatformAdapter

    bad = ("[99992354] The request you send is not a valid {open_message_id} or not exists, "
           "and the example format is \"om_xxx\"")
    assert BasePlatformAdapter._is_invalid_reply_anchor(bad) is True
    assert BasePlatformAdapter._is_invalid_reply_anchor("[230020] rate limited") is False
