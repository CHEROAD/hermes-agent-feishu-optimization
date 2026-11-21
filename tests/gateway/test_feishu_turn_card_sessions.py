"""Two people, one chat: each turn owns its own card, clock and stop label.

Groups isolate sessions per participant (``group_sessions_per_user``), so the turn card
must be keyed by the session too. When it was keyed by ``chat_id``:

* the second person's turn reused the first person's card and reset its clock — read in chat
  as 「我的消息被并进他那轮了」 even though the contexts were separate;
* the first person's still-running card flipped to ``⏱ 总耗时`` because any turn finishing
  in that chat stamped the shared end time.

These tests drive ``begin_turn_card`` itself (I/O stubbed, logic real) rather than poking the
dicts, so a future refactor that drops ``session_key`` fails here.
"""

from __future__ import annotations

import asyncio
import json
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace

chat = "oc_group"


def _adapter():
    """A FeishuAdapter with every I/O boundary stubbed, real card bookkeeping intact."""
    from gateway.platforms.base import SendResult
    from plugins.platforms.feishu.adapter import FeishuAdapter

    a = object.__new__(FeishuAdapter)
    for name in (
        "_turn_started_at", "_turn_ended_at", "_stopped_turns", "_turn_owner", "_turn_internal",
        "_progress_card_by_chat", "_final_card_by_chat", "_final_card_text", "_final_card_at",
        "_card_id_by_chat", "_card_seq", "_agent_status", "_last_progress_at",
        "_last_card_update_at", "_status_refresh_at", "_heartbeat_tasks", "_bg_task_card_tasks",
        "_subagents", "_subagent_seq", "_line_first_seen", "_line_done_at", "_bg_seen_ids",
    ):
        setattr(a, name, {})
    a._progress_card_bodies = {}
    a._progress_card_counts = OrderedDict()
    a._PROGRESS_CARD_MAX = 50          # set in __init__, which object.__new__ skips
    a._stop_button_scope = "all"
    a._admins = set()
    a._allowed_group_users = set()
    a._client = object()          # truthy: begin_turn_card proceeds
    a._loop = None

    a.sent = []                   # (chat_id, payload) for every card it posts
    a.cards_made = 0

    async def _send(*, chat_id, msg_type, payload, reply_to=None, metadata=None):
        a.sent.append((chat_id, msg_type))
        return SimpleNamespace(code=0, msg="ok", data=SimpleNamespace(message_id=f"msg-{len(a.sent)}"))

    async def _create_card(card):
        a.cards_made += 1
        return f"card-{a.cards_made}"

    def _finalize(response, default_message):
        return SendResult(success=True, message_id=f"msg-{len(a.sent)}")

    a._feishu_send_with_retry = _send
    a._cardkit_create_card = _create_card
    a._finalize_send_result = _finalize
    return a


def _key(adapter, session):
    return adapter._turn_key(chat, session)


def test_two_people_get_two_cards_in_one_chat():
    a = _adapter()

    first = asyncio.run(a.begin_turn_card(chat, session_key="sess-A"))
    second = asyncio.run(a.begin_turn_card(chat, session_key="sess-B"))

    assert first == "msg-1" and second == "msg-2"
    assert a._progress_card_by_chat[_key(a, "sess-A")] == "msg-1"
    assert a._progress_card_by_chat[_key(a, "sess-B")] == "msg-2"
    assert a._card_id_by_chat[_key(a, "sess-A")] != a._card_id_by_chat[_key(a, "sess-B")]
    # Both were posted to the real chat.
    assert [c for c, _ in a.sent] == [chat, chat]


def test_second_turn_does_not_reset_the_first_turn_clock():
    a = _adapter()
    asyncio.run(a.begin_turn_card(chat, session_key="sess-A"))
    started_a = a._turn_started_at[_key(a, "sess-A")]

    asyncio.run(a.begin_turn_card(chat, session_key="sess-B"))

    assert a._turn_started_at[_key(a, "sess-A")] == started_a   # untouched
    assert a._turn_started_at[_key(a, "sess-B")] >= started_a


def test_same_session_reuses_its_card_instead_of_stacking():
    a = _adapter()
    first = asyncio.run(a.begin_turn_card(chat, session_key="sess-A"))
    again = asyncio.run(a.begin_turn_card(chat, session_key="sess-A"))
    assert again is None, "a turn already holding a card must not open a second one"
    assert first in a._progress_card_by_chat.values()


def test_one_turn_finishing_leaves_the_other_running():
    """The reported symptom: A keeps running, B finishes, A's card must stay live."""
    a = _adapter()
    asyncio.run(a.begin_turn_card(chat, session_key="sess-A"))
    asyncio.run(a.begin_turn_card(chat, session_key="sess-B"))

    a.clear_progress_card(chat, session_key="sess-B")           # B's turn ends

    assert a._progress_card_by_chat.get(_key(a, "sess-A")) == "msg-1"   # A still live
    assert a._turn_ended_at.get(_key(a, "sess-A")) is None             # no 总耗时 on A
    assert a._turn_ended_at.get(_key(a, "sess-B")) is not None
    assert a._progress_card_by_chat.get(_key(a, "sess-B")) is None      # B retired


def test_abandoning_one_turn_does_not_touch_the_other():
    a = _adapter()
    asyncio.run(a.begin_turn_card(chat, session_key="sess-A"))
    asyncio.run(a.begin_turn_card(chat, session_key="sess-B"))

    async def _update_ok(chat_id, card):
        return True

    a._cardkit_update_card = _update_ok
    asyncio.run(a.abandon_progress_card(chat, session_key="sess-B"))

    assert a._progress_card_by_chat.get(_key(a, "sess-A")) == "msg-1"   # A untouched
    assert a._turn_ended_at.get(_key(a, "sess-A")) is None
    assert a._turn_ended_at.get(_key(a, "sess-B")) is not None           # only B closed


def test_stop_state_is_per_turn():
    a = _adapter()
    asyncio.run(a.begin_turn_card(chat, session_key="sess-A"))
    asyncio.run(a.begin_turn_card(chat, session_key="sess-B"))

    a._stopped_turns[_key(a, "sess-B")] = a._turn_started_at[_key(a, "sess-B")]

    assert a._turn_stopped(_key(a, "sess-B")) is True
    assert a._turn_stopped(_key(a, "sess-A")) is False


def test_stop_payload_points_at_its_own_turn():
    a = _adapter()
    asyncio.run(a.begin_turn_card(chat, session_key="sess-A"))
    asyncio.run(a.begin_turn_card(chat, session_key="sess-B"))

    payload = a._stop_payload(_key(a, "sess-A"))["hermes_stop_action"]
    assert payload["chat_id"] == chat                  # the synthetic /stop needs the chat
    assert payload["turn_key"] == _key(a, "sess-A")    # bookkeeping needs the turn
    assert payload["turn_id"] == f"{a._turn_started_at[_key(a, 'sess-A')]:.3f}"


def test_turn_key_is_idempotent():
    """Internal call chains pass an already-keyed value back in; it must not stack suffixes."""
    a = _adapter()
    once = a._turn_key(chat, "sess-A")
    assert a._turn_key(once) == once
    assert a._turn_key(once, "sess-A") == once


def test_gateway_call_sites_pass_the_session_key():
    """Wiring guard: a call site that forgets session_key silently re-merges the turns."""
    root = Path(__file__).resolve().parents[2]
    checks = {
        "gateway/run_turn.py": ["session_key=session_key"],
        "gateway/run_turn_progress.py": ["session_key=getattr(ctx, \"session_key\", None)"],
        "gateway/run_turn_runner.py": ["session_key=getattr(ctx, \"session_key\", None)"],
        "gateway/run_busy.py": ["session_key=session_key"],
        "gateway/run_turn_persistence.py": ["session_key=session_key"],
    }
    missing = []
    for rel, needles in checks.items():
        text = (root / rel).read_text(encoding="utf-8")
        missing += [f"{rel}: {n}" for n in needles if n not in text]
    assert not missing, "card call sites lost the session key: " + "; ".join(missing)


async def _async_false(*args, **kwargs):
    return False


class _Awaitable:
    def __init__(self, value):
        self.value = value

    def __await__(self):
        async def _inner():
            return self.value

        return _inner().__await__()


def _awaitable(value):
    return _Awaitable(value)


def _ok():
    from gateway.platforms.base import SendResult

    return SendResult(success=True)


def _capture_rebuilt_card(adapter, session, *, bg_tasks=None, via="bg"):
    """Run a mid-turn card rebuild and return the card dict it pushed."""
    keys = adapter._turn_key("oc_group", session)
    captured = {}

    async def _update(chat_id, card):
        captured["chat_id"] = chat_id
        captured["card"] = card
        return True

    async def _patch(element_id, partial):
        return False

    adapter._cardkit_update_card = _update
    if via == "status":
        from gateway.platforms.base import SendResult

        # The status fallback PATCHes the message itself; capture the card it builds.
        def _body(**kwargs):
            captured["card"] = json.loads(kwargs["content"])
            return {"content": kwargs["content"]}

        async def _update_msg(req):
            return SimpleNamespace(code=0, msg="ok")

        adapter._cardkit_patch_element = lambda chat_id, element_id, partial: _async_false()
        adapter._build_update_message_body = _body
        adapter._build_update_message_request = lambda **k: SimpleNamespace(http_method=None)
        adapter._run_blocking = lambda fn, req: _awaitable(SimpleNamespace(code=0, msg="ok"))
        adapter._finalize_send_result = lambda r, m: SendResult(success=True)
        asyncio.run(adapter._refresh_status_card(keys))
    else:
        asyncio.run(adapter.update_background_tasks_card(
            "oc_group", bg_tasks if bg_tasks is not None else [{"id": "p1", "status": "running", "command": "sleep 60"}],
            session_key=session,
        ))
    return captured["card"]


def _stop_buttons(card):
    return [e for e in card["body"]["elements"] if e.get("element_id") == "stop_btn"]


def _footer_text(card):
    return next(e for e in card["body"]["elements"] if e.get("element_id") == "footer")["content"]


def test_background_task_patch_keeps_the_stop_button():
    """A bg-task refresh mid-turn must not drop the stop button or claim the turn finished."""
    a = _adapter()
    asyncio.run(a.begin_turn_card(chat, session_key="sess-A"))

    card = _capture_rebuilt_card(a, "sess-A", via="bg")

    assert len(_stop_buttons(card)) == 1, "bg-task rebuild dropped the stop button"
    assert "总耗时" not in _footer_text(card), "bg-task rebuild rendered a finished footer mid-turn"
    assert _footer_text(card).startswith("⏳")


def test_answers_folded_into_a_live_card_do_not_finish_it():
    """A notice turn's fold must not turn the running card into a finished one (the report)."""
    a = _adapter()
    asyncio.run(a.begin_turn_card(chat, session_key="sess-A"))
    a._final_card_text["msg-1"] = "通知：后台任务完成"   # folded while the turn still runs

    card = _capture_rebuilt_card(a, "sess-A", via="bg")

    assert "总耗时" not in _footer_text(card)
    assert _footer_text(card).startswith("⏳")
    assert len(_stop_buttons(card)) == 1


def test_status_refresh_patch_keeps_the_stop_button():
    """The status patch falls back to a whole-card rebuild; that path must keep the button too."""
    a = _adapter()
    asyncio.run(a.begin_turn_card(chat, session_key="sess-A"))

    card = _capture_rebuilt_card(a, "sess-A", via="status")

    assert len(_stop_buttons(card)) == 1, "status-refresh rebuild dropped the stop button"
    assert "总耗时" not in _footer_text(card)


def test_final_card_still_reports_the_total_time():
    """The fix must not flatten the finished state: the retired final card keeps ⏱ 总耗时."""
    a = _adapter()
    asyncio.run(a.begin_turn_card(chat, session_key="sess-A"))
    key = _key(a, "sess-A")
    a._final_card_text["msg-1"] = "答案"          # the answer folded into the card
    a.clear_progress_card(chat, session_key="sess-A")

    card = _capture_rebuilt_card(a, "sess-A", via="bg")
    assert "总耗时" in _footer_text(card)


def test_tool_progress_after_an_answer_reuses_the_same_card():
    """A turn that answered mid-run and then kept working used to open a second bubble."""
    from gateway.platforms.base import SendResult

    a = _adapter()
    asyncio.run(a.begin_turn_card(chat, session_key="sess-A"))
    a.clear_progress_card(chat, session_key="sess-A")      # answer folded while still running
    key = _key(a, "sess-A")
    a._agent_status[key] = {"state": "tool", "detail": "terminal", "since": 0.0}

    edited = []

    async def _edit(chat_id, message_id, content, metadata=None, turn_key=None):
        edited.append(message_id)
        return SendResult(success=True, message_id=message_id)

    a.edit_message = _edit
    asyncio.run(a.send(chat, "🔧 terminal · new row", metadata={"feishu_progress_card": True}))

    assert edited == ["msg-1"], "progress must patch the turn's existing card"
    assert len(a.sent) == 1, "a second card bubble was opened for the same turn"
    assert a._progress_card_by_chat.get(key) == "msg-1"


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as exc:  # noqa: BLE001 - a runner, not production code
                failures += 1
                print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    raise SystemExit(1 if failures else 0)
