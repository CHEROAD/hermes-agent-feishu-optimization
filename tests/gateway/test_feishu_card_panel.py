"""A running turn's card keeps its tool panel — and its background jobs — when it is repatched.

The card's per-turn state (accumulated body, tool count, background jobs, status, clock) lives
under the turn key ``chat|session``. When a repatch looked those up by the bare chat id, every
lookup missed: the collapsed 「🔧 工具调用 (N)」 panel and the background-jobs panel silently
vanished, and the card was left with a status line and nothing else.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import OrderedDict
from types import SimpleNamespace

chat = "oc_group"


def _adapter():
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
    a._PROGRESS_CARD_MAX = 50
    a._stop_button_scope = "all"
    a._admins = set()
    a._allowed_group_users = set()
    a._client = object()
    a._loop = None
    a.format_message = lambda c: c
    a._decorate_progress_body = lambda _key, c: c
    a._finalize_send_result = lambda response, default_message: SendResult(success=True, message_id="msg-1")
    return a


def _running_turn(a, session="s1"):
    key = a._turn_key(chat, session)
    a._progress_card_by_chat[key] = "msg-1"
    a._progress_card_bodies["msg-1"] = "💻 terminal · 3s"
    a._progress_card_counts["msg-1"] = 4
    a._bg_task_card_tasks[key] = [{"id": "j1", "status": "running", "command": "pytest -q"}]
    a._turn_started_at[key] = time.time() - 30
    a._agent_status[key] = {"state": "tool", "label": "💻 terminal", "since": time.time() - 3}
    return key


def _patch_card(a):
    seen = {}

    async def _update(key, card):
        seen["key"] = key
        seen["card"] = card
        return True

    a._cardkit_update_card = _update
    return seen


def test_repatching_a_running_card_keeps_the_tool_panel():
    a = _adapter()
    key = _running_turn(a)
    seen = _patch_card(a)

    asyncio.run(a.edit_message(chat, "msg-1", "💻 terminal · 3s", turn_key=key))

    blob = json.dumps(seen["card"], ensure_ascii=False)
    assert seen["key"] == key, "the card entity is looked up by turn key, not by chat id"
    assert "collapsible_panel" in blob, "the 🔧 工具调用 panel disappeared from the card"
    assert "工具调用" in blob


def test_repatching_keeps_the_background_jobs_panel():
    a = _adapter()
    key = _running_turn(a)
    seen = _patch_card(a)

    asyncio.run(a.edit_message(chat, "msg-1", "💻 terminal · 3s", turn_key=key))

    blob = json.dumps(seen["card"], ensure_ascii=False)
    assert "pytest -q" in blob, "the background-jobs panel lost this turn's jobs"


def test_the_panel_survives_a_metadata_without_a_tool_count():
    """The count lives in the per-card store, so a caller that forgets it must not drop the panel."""
    a = _adapter()
    key = _running_turn(a)
    seen = _patch_card(a)

    asyncio.run(a.edit_message(chat, "msg-1", "💻 terminal · 3s", metadata={"feishu_progress_card": True},
                               turn_key=key))

    blob = json.dumps(seen["card"], ensure_ascii=False)
    assert "collapsible_panel" in blob


def test_two_turns_in_one_chat_keep_their_own_panels():
    a = _adapter()
    key_a = _running_turn(a, "sA")
    key_b = a._turn_key(chat, "sB")
    a._progress_card_by_chat[key_b] = "msg-2"
    a._progress_card_bodies["msg-2"] = "🌐 web_search · 1s"
    a._progress_card_counts["msg-2"] = 2
    a._bg_task_card_tasks[key_b] = [{"id": "j2", "status": "running", "command": "dflash2 pack"}]
    a._turn_started_at[key_b] = time.time() - 5
    seen = _patch_card(a)

    asyncio.run(a.edit_message(chat, "msg-2", "🌐 web_search · 1s", turn_key=key_b))

    blob = json.dumps(seen["card"], ensure_ascii=False)
    assert "dflash2 pack" in blob
    assert "pytest -q" not in blob, "the other person's jobs bled into this card"
    assert seen["key"] == key_b


def test_a_finished_row_keeps_its_seconds_when_the_card_is_redrawn():
    """The heartbeat redraws the same body every few seconds: a 12-second call must not read 15060s."""
    import plugins.platforms.feishu.adapter as adapter_mod

    a = _adapter()
    # The factory stubs the decoration for the panel tests; these two need the real one.
    a.__dict__.pop("_decorate_progress_body", None)
    key = a._turn_key(chat, "s1")
    a._agent_status[key] = {"state": "tool"}

    clock = [1000.0]
    real_time = adapter_mod.time.time
    adapter_mod.time.time = lambda: clock[0]
    try:
        # 1) the tool is the running row: no stamp yet
        body = "💻 terminal · ls -la"
        assert a._decorate_progress_body(key, body) == body

        # 2) a second row appears, so the first one is finished 12 seconds later
        clock[0] = 1012.0
        body = "💻 terminal · ls -la\n✍️ Writing a.txt"
        first = a._decorate_progress_body(key, body)
        assert "💻 terminal · ls -la · 12s" in first, first
        assert "✍️ Writing a.txt" in first and "✍️ Writing a.txt ·" not in first

        # 3) the heartbeat redraws the same body four minutes later: the number must not move
        clock[0] = 1240.0
        again = a._decorate_progress_body(key, body)
        assert "💻 terminal · ls -la · 12s" in again, again
        assert "15060" not in again and "228s" not in again
    finally:
        adapter_mod.time.time = real_time


def test_a_finished_row_keeps_its_seconds_when_the_card_is_redrawn_again():
    """A row keeps its frozen number even when the turn's status moves on to thinking."""
    import plugins.platforms.feishu.adapter as adapter_mod

    a = _adapter()
    # The factory stubs the decoration for the panel tests; these two need the real one.
    a.__dict__.pop("_decorate_progress_body", None)
    key = a._turn_key(chat, "s1")
    a._agent_status[key] = {"state": "tool"}

    clock = [500.0]
    real_time = adapter_mod.time.time
    adapter_mod.time.time = lambda: clock[0]
    try:
        a._decorate_progress_body(key, "💻 terminal · make")
        clock[0] = 512.0
        a._decorate_progress_body(key, "💻 terminal · make\n🌐 web_search · kotlin")
        clock[0] = 920.0
        a._agent_status[key] = {"state": "thinking"}   # every row is finished now
        out = a._decorate_progress_body(key, "💻 terminal · make\n🌐 web_search · kotlin")
        assert "💻 terminal · make · 12s" in out, out
        assert "🌐 web_search · kotlin · 408s" in out, out
        # ... and ten minutes later the same rows still read the same numbers
        clock[0] = 2100.0
        later = a._decorate_progress_body(key, "💻 terminal · make\n🌐 web_search · kotlin")
        assert later == out, later
    finally:
        adapter_mod.time.time = real_time


def test_send_keeps_the_panel_when_the_metadata_carries_no_tool_count():
    """The real lane: a tool-progress send() with no count in metadata still renders the panel.

    This is the bug the running gateway showed as a bare 「🔧 工具调用中 · terminal · 0s」 card:
    ``send()`` hands ``edit_message`` metadata without a count, and the rebuild then took the
    count from the metadata alone — 0 — which drops the collapsed panel entirely.
    """
    a = _adapter()
    key = a._turn_key(chat, "s1")
    a._progress_card_by_chat[key] = "msg-1"
    a._progress_card_bodies["msg-1"] = "💻 terminal · 4s"
    a._progress_card_counts["msg-1"] = 3          # three tool calls went into this card
    a._turn_started_at[key] = time.time() - 20
    a._agent_status[key] = {"state": "tool", "label": "💻 terminal", "since": time.time() - 4}
    seen = _patch_card(a)

    asyncio.run(a.send(chat, "💻 terminal · 4s", metadata={"feishu_progress_card": True}))

    blob = json.dumps(seen["card"], ensure_ascii=False)
    assert "collapsible_panel" in blob, "send() dropped the tool panel when metadata had no count"
    assert "工具调用 (3)" in blob, blob


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
