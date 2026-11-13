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
    adapter._turn_started_at = {"oc_1": 1_700_000_000.5}
    adapter._turn_ended_at = {}
    adapter._stopped_turns = {}
    adapter._turn_owner = {"oc_1": "ou_starter"}
    adapter._stop_button_scope = scope
    adapter._admins = set(admins)
    adapter._allowed_group_users = set(allowed)
    adapter.dispatched = []
    adapter.closed = []

    adapter._is_interactive_operator_authorized = lambda *a, **k: authorized

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
    assert adapter._stopped_turns["oc_1"] == 1_700_000_000.5
    assert adapter._turn_stopped("oc_1") is True


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
    # The card is shared, so rendering cannot be per-viewer: the button shows for the chat and
    # the click decides who may use it. Only the explicit "off" switch removes it.
    for scope in ("all", "starter_admin", "starter"):
        payload = _adapter(scope=scope)._stop_payload("oc_1")
        assert payload is not None, scope
        assert payload["hermes_stop_action"]["chat_id"] == "oc_1"
        assert payload["hermes_stop_action"]["turn_id"] == "1700000000.500"

    assert _adapter(scope="off")._stop_payload("oc_1") is None
    # No turn has started: nothing to stop.
    adapter = _adapter()
    assert adapter._stop_payload("oc_unknown") is None


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
