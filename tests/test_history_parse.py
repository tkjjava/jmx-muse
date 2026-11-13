"""`GET /chat/history` page parser (static-anchor derived, tolerant by design)."""
import pytest

from muse_bridge.history import (
    event_text,
    has_more,
    latest_assistant_text,
    messages,
    newest_seq,
    page_events,
    transcript_revision,
)


def ev(seq, *, name="message.assistant", role=None, text=None, payload=None, **extra):
    event = {"seq": seq, "event_name": name, "message_id": f"m{seq}"}
    if role is not None:
        event["role"] = role
    if text is not None:
        event["content"] = text
    if payload is not None:
        event["payload"] = payload
    event.update(extra)
    return event


def page(*events, **top):
    return {"chat_events": list(events), "has_more": False, **top}


def test_flat_page_reads_one_message_per_event_with_text():
    rows = messages(
        page(
            ev(1, name="message.user", role="user", content="hi"),
            ev(2, content="answer"),
            {"seq": 3, "event_name": "agent.status", "payload": {"status": "online"}},
        )
    )
    assert [(m.seq, m.role, m.text) for m in rows] == [
        (1, "user", "hi"),
        (2, "assistant", "answer"),
    ]
    assert rows[1].message_id == "m2"


def test_role_prefers_explicit_role_then_event_name():
    rows = messages(
        page(
            {"seq": 1, "event_name": "message.user", "content": "来自提问"},
            {"seq": 2, "event_name": "message.assistant", "content": "回答"},
            {"seq": 3, "role": "assistant", "content": "显式角色"},
            {"seq": 4, "event_name": "delta.message_start", "content": "非消息事件"},
        )
    )
    assert [m.role for m in rows] == ["user", "assistant", "assistant", "other"]


def test_payload_content_is_used_when_top_level_text_absent():
    rows = messages(page({"seq": 7, "event_name": "message.assistant",
                          "payload": {"content": "在 payload 里"}}))
    assert rows[0].text == "在 payload 里"
    assert rows[0].seq == 7


def test_text_preference_top_level_before_payload_display_text():
    rows = messages(page({
        "seq": 1, "event_name": "message.assistant",
        "display_text": "顶层 display",
        "payload": {"content": "payload content", "display_text": "payload display"},
    }))
    assert rows[0].text == "顶层 display"


def test_display_text_is_fallback_after_content():
    rows = messages(page({"seq": 1, "event_name": "message.assistant",
                          "display_text": "只有 display"}))
    assert rows[0].text == "只有 display"


def test_events_without_text_are_skipped_but_keep_order():
    rows = messages(
        page(
            {"seq": 3, "event_name": "agent.status", "payload": {"status": "online"}},
            ev(1, content="一"),
            ev(2, content="二"),
        )
    )
    assert [m.text for m in rows] == ["一", "二"]
    assert [m.seq for m in rows] == [1, 2]


def test_non_dict_and_missing_seq_events_are_tolerated():
    events = page_events({"chat_events": ["nope", None, {"seq": 5, "content": "五"}]})
    assert events == [{"seq": 5, "content": "五"}]
    # Events without a usable seq sort after numbered ones (stable order kept).
    assert [m.text for m in messages({"chat_events": ["nope",
            {"content": "无 seq 但有正文"}, {"seq": 2, "content": "二"}]})] == [
        "二", "无 seq 但有正文",
    ]


@pytest.mark.parametrize("envelope", ["result", "data"])
def test_result_and_data_envelopes_are_unwrapped(envelope):
    page_body = page(ev(1, content="信封内的正文"), transcript_revision="rev-9")
    wrapped = {envelope: page_body}
    assert latest_assistant_text(wrapped) == "信封内的正文"
    assert transcript_revision(wrapped) == "rev-9"
    assert newest_seq(wrapped) == 1


def test_bare_envelope_without_page_keys_is_not_unwrapped():
    wrapped = {"result": {"something": 1}, "chat_events": [{"seq": 1, "content": "x"}]}
    assert page_events(wrapped) == [{"seq": 1, "content": "x"}]


def test_latest_assistant_text_picks_last_assistant_by_seq_order():
    page_body = page(
        ev(9, content="旧回答"),
        {"seq": 4, "event_name": "message.user", "content": "提问"},
        ev(12, content="新回答"),
    )
    assert latest_assistant_text(page_body) == "新回答"
    assert latest_assistant_text(page(ev(1, name="message.user", content="只有提问"))) == ""


def test_newest_seq_ignores_bool_and_non_int():
    assert newest_seq(page({"seq": True}, {"seq": 3}, {"seq": "4"})) == 3
    assert newest_seq(page()) is None
    assert newest_seq({"chat_events": "not a list"}) is None


def test_has_more_only_reports_real_booleans():
    assert has_more({"has_more": True}) is True
    assert has_more({"chat_events": [], "has_more": "yes"}) is None
    assert has_more({}) is None


def test_event_text_and_roles_for_unknown_event():
    assert event_text({"event_name": "agent.status", "payload": {"status": "online"}}) == ""
    rows = messages(page({"seq": 1, "event_name": "note", "content": "备注"}))
    assert rows[0].role == "other"


def test_empty_text_values_do_not_shadow_real_ones():
    rows = messages(page({
        "seq": 1, "event_name": "message.assistant",
        "content": "", "display_text": "有效文本",
        "payload": {"content": "payload 文本"},
    }))
    assert rows[0].text == "有效文本"


def live_shaped_event(text, *, seq=680, role="assistant", event_name="message.assistant"):
    """Shape captured live 2026-11-14 (values fabricated, keys verbatim)."""
    return {
        "seq": seq,
        "event_name": event_name,
        "role": role,
        "message_id": "m" * 50,
        "display_text": text,
        "display_text_ready": True,
        "display_text_updated_at_ms": 1_700_000_000_000,
        "occurred_at_ms": 1_700_000_000_001,
        "parent_agent_id": "a" * 36,
        "parent_message_id": None,
        "reply_to_message_id": "",
        "resources": [],
        "reaction_emoji": None,
        "agent_id": None,
        "transcript_surface": "primary",
        "visibility": "visible",
        "chat_context": {
            "chat_id": "c" * 36,
            "chat_kind": "main",
            "device_id": "d" * 36,
            "event_kind": "message",
            "originating_channel": "web",
            "require_mention": False,
        },
        "payload": {
            "content": text,
            "display_text": text,
            "display_text_ready": True,
            "role": role,
            "seq": seq,
            "message_id": "m" * 50,
            "chat_event_seq": seq - 1,
            "is_thread": False,
            "session_id": "s" * 36,
            "source": "assistant",
            "status": "completed",
            "metadata": {},
            "chat_context": {},
            "transcript_surface": "primary",
            "visibility": "visible",
        },
    }


def test_live_shaped_page_is_parsed():
    page_body = {
        "channel": "all",
        "chat_events": [
            live_shaped_event("完整回答"),
            live_shaped_event("提问", seq=679, role="user", event_name="message.user"),
        ],
        "has_more": True,
        "transcript_revision": 42,
    }
    assert [(m.seq, m.role, m.text) for m in messages(page_body)] == [
        (679, "user", "提问"),
        (680, "assistant", "完整回答"),
    ]
    assert latest_assistant_text(page_body) == "完整回答"
    assert newest_seq(page_body) == 680
    assert transcript_revision(page_body) == 42
    assert has_more(page_body) is True