"""A stopped turn still delivers what it had produced, marked （已停止）.

``/stop`` invalidates the run generation; the in-flight result used to be dropped entirely, so
the answer showed up in the transcript (and the desktop) but never in the chat. Only a *stopped*
turn gets this treatment — a turn displaced by a newer message stays silent.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace


class _Adapter:
    def __init__(self, ok=True):
        self.ok = ok
        self.sent = []

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append({"chat_id": chat_id, "content": content, "reply_to": reply_to})
        return SimpleNamespace(success=self.ok)


class _Mixin:
    """Stands in for GatewayTurnPersistenceMixin: only the collaborators matter here."""

    def __init__(self, reason, adapter):
        self.reason = reason
        self.adapter = adapter
        self.logged = []

    def _peek_session_state(self, session_key):
        return SimpleNamespace(persistent=SimpleNamespace(run_invalidation_reason=self.reason))

    def _delivery_adapter_for(self, source):
        return self.adapter

    def _reply_anchor_for_event(self, event):
        return "om_anchor"


def _run(reason, agent_result, ok=True):
    from gateway.run_turn_persistence import GatewayTurnPersistenceMixin

    adapter = _Adapter(ok=ok)
    stub = _Mixin(reason, adapter)
    source = SimpleNamespace(chat_id="oc_1", platform=SimpleNamespace(value="feishu"))
    event = SimpleNamespace(message_id="om_user_msg")
    asyncio.run(GatewayTurnPersistenceMixin._hmwa_deliver_stopped_partial(
        stub, source, event, agent_result, "agent:main:feishu:group:oc_1:on_x"))
    return adapter, agent_result


def test_stopped_turn_delivers_its_partial_answer():
    adapter, result = _run("stop_command", {"final_response": "已经跑完一半的结论"})
    assert len(adapter.sent) == 1
    assert adapter.sent[0]["content"] == "已经跑完一半的结论\n\n（已停止）"
    assert adapter.sent[0]["chat_id"] == "oc_1"
    assert adapter.sent[0]["reply_to"] == "om_anchor"
    assert result["already_sent"] is True, "the finally-block would otherwise paint the no-output notice"


def test_superseded_turn_stays_silent():
    adapter, result = _run("new_message", {"final_response": "旧回答"})
    assert adapter.sent == []
    assert not result.get("already_sent")


def test_already_delivered_turn_is_not_sent_twice():
    adapter, _ = _run("stop_command", {"final_response": "流式已经发出去了", "already_sent": True})
    assert adapter.sent == []


def test_empty_answer_is_not_sent():
    adapter, _ = _run("stop_command", {"final_response": "  "})
    assert adapter.sent == []


def test_failed_send_does_not_claim_delivery():
    adapter, result = _run("stop_command", {"final_response": "半成品"}, ok=False)
    assert len(adapter.sent) == 1
    assert not result.get("already_sent")


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
