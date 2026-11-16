"""A turn that never delivers an answer must not leave the Feishu card spinning.

Regression for the "stuck, no reply" report: the turn card is opened when the message
arrives, and when the turn ends without any reply (superseded generation, restart drain, or
an exception) nothing used to close it — the last ``思考中 / 工具调用中`` strip stayed on screen
and the next turn reused that same bubble.
"""

from __future__ import annotations

import asyncio
import time


def _make_adapter(*, live_card=None):
    from plugins.platforms.feishu.adapter import FeishuAdapter

    adapter = object.__new__(FeishuAdapter)
    adapter._progress_card_by_chat = {}
    adapter._progress_card_bodies = {}
    adapter._progress_card_counts = {}
    adapter._final_card_by_chat = {}
    adapter._final_card_text = {}
    adapter._final_card_at = {}
    adapter._bg_seen_ids = {}
    adapter._bg_task_card_tasks = {}
    adapter._turn_started_at = {}
    adapter._turn_ended_at = {}
    adapter._heartbeat_tasks = {}
    adapter._last_progress_at = {}
    adapter._card_id_by_chat = {}
    adapter._card_seq = {}
    adapter._decorate_progress_body = lambda chat_id, body: body
    adapter._subagent_lines = lambda chat_id: []
    adapter.update_calls = []

    async def _update(chat_id, card):
        adapter.update_calls.append((chat_id, card))
        return adapter.update_result

    adapter.update_result = True
    adapter._cardkit_update_card = _update
    if live_card:
        adapter._progress_card_by_chat["oc_1"] = live_card
        adapter._progress_card_bodies[live_card] = "🔧 terminal\n  echo hi"
        adapter._progress_card_counts[live_card] = 1
        adapter._card_id_by_chat["oc_1"] = "card_1"
        adapter._turn_started_at["oc_1"] = time.time() - 42
    return adapter


def test_abandon_is_a_noop_without_a_live_card():
    adapter = _make_adapter()
    asyncio.run(adapter.abandon_progress_card("oc_1"))
    assert adapter.update_calls == []
    assert adapter._final_card_by_chat == {}


def test_abandon_closes_the_card_and_states_the_failure():
    adapter = _make_adapter(live_card="om_card")
    asyncio.run(adapter.abandon_progress_card("oc_1", note="⚠️ 本轮没有产出结果"))

    assert len(adapter.update_calls) == 1
    chat_id, card = adapter.update_calls[0]
    assert chat_id == "oc_1"
    payload = str(card)
    assert "本轮没有产出结果" in payload
    # The live strip must be gone: with a final text the card renders as finished, and the
    # frozen elapsed footer is the only timing line left.
    assert "'status'" not in payload
    assert "总耗时" in payload
    # The tool transcript is kept, so the user can still see what the turn did.
    assert "echo hi" in payload

    # The card is retired exactly like a finished turn, so a later background-task patch and
    # the next turn both target the same bubble instead of opening a second one.
    assert "oc_1" not in adapter._progress_card_by_chat
    assert adapter._final_card_by_chat["oc_1"] == "om_card"
    # Elapsed time is frozen at the moment the turn was abandoned.
    assert adapter._turn_ended_at["oc_1"] >= adapter._turn_started_at["oc_1"] + 41


def test_abandon_still_retires_the_card_when_the_patch_fails():
    adapter = _make_adapter(live_card="om_card")
    adapter.update_result = False
    asyncio.run(adapter.abandon_progress_card("oc_1"))

    assert len(adapter.update_calls) == 1
    assert "oc_1" not in adapter._progress_card_by_chat
    assert adapter._final_card_by_chat["oc_1"] == "om_card"
