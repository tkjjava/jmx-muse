"""Tolerant reader for `GET /chat/history` pages.

Why this module exists: after a mid-turn WebSocket disconnect, live events from
the gap are not replayed (measured 2026-11-14, see
docs/2026-11-14_resume-continuity-report.md). The frontend therefore reconciles
by re-fetching committed history and merging by seq. This module turns a history
page into plain (role, text) messages so a caller can decide whether the
committed answer is complete.

Evidence level: static frontend anchors **plus live validation** (2026-11-14).
A real page was captured: top level `{channel, chat_events, has_more,
transcript_revision}`; each event carries `event_name` (`message.user` /
`message.assistant`), `role`, `seq`, `message_id`, and both a top-level
`display_text` and `payload.display_text`/`payload.content` (identical length
in the observed page). The parser still stays tolerant about where text lives,
because other event kinds and older pages were not sampled:

  * page envelope: bare page, or wrapped in ``result`` / ``data``
    (anchor: 24thn7in_976_.js @85765 / @103814 reads ``t.chat_events``;
    live page is bare);
  * event text: ``display_text``, ``content``, then ``payload.display_text`` /
    ``payload.content`` (anchor: 24thn7in_976_.js @59874; live-validated);
  * history revisions carry ``transcript_revision`` at page level
    (anchor: 24thn7in_976_.js @36022; live page: integer);
  * message events use ``event_name`` values ``message.user`` /
    ``message.assistant`` with ``seq``/``message_id``/``role``
    (anchors: 24thn7in_976_.js @25966, @74920; 0ikdqggttpa8f.js @19646;
    live-validated).

Nothing here talks to the network or logs message text. Callers must not treat
the result as proof of completeness: it only proves what the server committed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

USER_EVENT = "message.user"
ASSISTANT_EVENT = "message.assistant"
_TEXT_KEYS = ("display_text", "content")


@dataclass(frozen=True)
class HistoryMessage:
    """One committed message recovered from a history page."""

    seq: int
    role: str  # "user" | "assistant" | "other"
    message_id: str | None
    text: str


def _as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def page_of(page: Any) -> dict:
    """Unwrap ``result``/``data`` envelopes; return a dict page or {}."""
    raw = _as_dict(page)
    for key in ("result", "data"):
        inner = _as_dict(raw.get(key))
        if inner.get("chat_events") is not None or inner.get("has_more") is not None:
            return inner
    return raw


def page_events(page: Any) -> list[dict]:
    """Return event dicts sorted by seq (stable for missing/invalid seq)."""
    events = page_of(page).get("chat_events")
    if not isinstance(events, list):
        return []
    rows: list[tuple[int, int, dict]] = []
    for index, item in enumerate(events):
        event = _as_dict(item)
        if not event:
            continue
        seq = event.get("seq")
        order = seq if isinstance(seq, int) and not isinstance(seq, bool) else 1 << 62
        rows.append((order, index, event))
    rows.sort(key=lambda row: (row[0], row[1]))
    return [row[2] for row in rows]


def _text_of(event: dict) -> str:
    payload = _as_dict(event.get("payload"))
    for key in _TEXT_KEYS:
        value = event.get(key)
        if isinstance(value, str) and value:
            return value
    for key in _TEXT_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _role_of(event: dict) -> str:
    role = event.get("role")
    if role in ("user", "assistant"):
        return role
    name = event.get("event_name")
    if name == ASSISTANT_EVENT:
        return "assistant"
    if name == USER_EVENT:
        return "user"
    payload_role = _as_dict(event.get("payload")).get("role")
    if payload_role in ("user", "assistant"):
        return payload_role
    return "other"


def event_text(event: Any) -> str:
    """Best-effort text of one history event ("" when it carries none)."""
    return _text_of(_as_dict(event))


def messages(page: Any) -> list[HistoryMessage]:
    """Committed messages in seq order; events without text are skipped."""
    out: list[HistoryMessage] = []
    for event in page_events(page):
        text = _text_of(event)
        if not text:
            continue
        seq = event.get("seq")
        message_id = event.get("message_id")
        out.append(
            HistoryMessage(
                seq=seq if isinstance(seq, int) and not isinstance(seq, bool) else -1,
                role=_role_of(event),
                message_id=message_id if isinstance(message_id, str) and message_id else None,
                text=text,
            )
        )
    return out


def latest_assistant_text(page: Any) -> str:
    """Text of the last assistant message on the page ("" when absent)."""
    for message in reversed(messages(page)):
        if message.role == "assistant":
            return message.text
    return ""


def newest_seq(page: Any) -> int | None:
    """Highest seq on the page, used to compare against live cursors."""
    seqs = [
        event["seq"]
        for event in page_events(page)
        if isinstance(event.get("seq"), int) and not isinstance(event.get("seq"), bool)
    ]
    return max(seqs) if seqs else None


def transcript_revision(page: Any) -> Any:
    """Page-level revision token; None when the server omitted it."""
    return page_of(page).get("transcript_revision")


def has_more(page: Any) -> bool | None:
    """``has_more`` flag: True/False when present, None when not a bool."""
    value = page_of(page).get("has_more")
    return value if isinstance(value, bool) else None