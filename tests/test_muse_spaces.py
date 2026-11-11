"""muse Spaces / idea-cards 只读适配的离线契约测试。

覆盖四层，全部**零网络、零账号库、零浏览器**：

  * 归一化：§2.1 字段表 / 缺失置 null / slug 回退 / 枚举大小写 / 数字有限性；
  * 协议客户端：spaces 列表、单 space、export.html、idea-cards 列表/详情/媒体，
    用注入的假协议客户端驱动（`MuseSpacesClient(protocol_client=...)`），并钉住
    verb/path/body/timeout —— 真机探针看到的路径就是这里断言的那些；
  * 错误映射：401/403/429/404/route-not-found/连接失败，且异常文案不带 cookie；
  * HTTP 端点：503、401、导入卡片契约、导出必须是 `attachment`（**断言不是
    inline**）、idea-card 媒体按文件头 mime + 栅格白名单才 inline。

为什么另起一个测试文件：`tests/test_canvas_api.py` 的 fixture 只装配画布
store，不碰账号；muse 端点要覆盖 `get_account_store` 依赖与协议客户端，混在
同一个文件里会让那边的 fixture 多背一份无关状态。既有文件保持原样。

真机证据来源（2026-11-11，Lead 只读探针，落盘 `work/canvas_re/protocol-route-dump/`）：
`spaces/v2` → `{"spaces":[]}`；`spaces/v2/{slug}` → 404 `space_unavailable`；
`export.html` → 404 `{"code":"not_found","message":"web artifact was not found"}`；
`/api/idea-cards` → 7 sections；`/api/idea-cards/media/icon-56e8…` → PNG 1,045 B。
"""

from __future__ import annotations

import json
import socket
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from muse_bridge import canvas_api as canvas_api_module
from muse_bridge.canvas_api import (
    get_account_store,
    muse_space_card_node,
    pick_muse_account,
    router,
)
from muse_bridge.canvas_store import NODE_ID_RE
from muse_bridge.muse_spaces import (
    MuseAuthError,
    MuseBinary,
    MuseEgressError,
    MuseInvalidResponse,
    MuseInvalidSlug,
    MuseNotFound,
    MuseRateLimited,
    MuseSpacesClient,
    MuseSpacesError,
    normalize_idea_card,
    normalize_idea_cards_response,
    normalize_space,
    normalize_spaces_response,
    validate_slug,
)
from muse_bridge.protocol_client import (
    ProtocolAuthError,
    ProtocolEgressError,
    ProtocolError,
    ProtocolRateLimited,
)

COOKIE_SECRET = "hatch_sess=SECRET-COOKIE-VALUE"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24

#: 一条字段全给满的 raw space（形状照 `2lzbmcnnh3_e0.js@15101`–`@16698`）。
RAW_SPACE_FULL = {
    "slug": "mb-canvas-demo",
    "space_id": "11111111-2222-3333-4444-555555555555",
    "name": "Demo Space",
    "image_url": "https://cdn.example/img.png",
    "is_shared": True,
    "shortcode": "abc123",
    "share_type": "cloudflare",
    "source_url": "https://muse.ai/share/abc",
    "construction_status": "BUILDING",
    "updated_at_ms": 1700000000000,
    "accessed_at_ms": 1700000001000,
    "frequency_score": 3.5,
    "favorite_order": 2,
    "preview_image": {
        "version": "v1",
        "icon_url": "https://cdn.example/icon.png",
        "thumbnail_url": "https://cdn.example/thumb.png",
        "thumbnail_width_px": 320,
        "thumbnail_height_px": 200,
        "preview_card_url": "https://cdn.example/card.png",
        "preview_card_width_px": 1200,
        "preview_card_height_px": 630,
    },
    "runtime": "node",
    "is_current_build_published": True,
    "has_server_actions": False,
    "theme_accent": "#ff8800",
    "saved_target_shortcode": "saved1",
    "view_url": "https://muse.ai/spaces/mb-canvas-demo",
}

#: §2.1 归一化后的完整形态（顺序无关，dict 比较不看顺序）。
NORMALIZED_SPACE_FULL = {
    "construction_status": "constructing",
    "slug": "mb-canvas-demo",
    "space_id": "11111111-2222-3333-4444-555555555555",
    "image_url": "https://cdn.example/img.png",
    "name": "Demo Space",
    "updated_at_ms": 1700000000000,
    "accessed_at_ms": 1700000001000,
    "frequency_score": 3.5,
    "favorite_order": 2,
    "preview_image": {
        "version": "v1",
        "icon_url": "https://cdn.example/icon.png",
        "thumbnail_url": "https://cdn.example/thumb.png",
        "thumbnail_width_px": 320,
        "thumbnail_height_px": 200,
        "preview_card_url": "https://cdn.example/card.png",
        "preview_card_width_px": 1200,
        "preview_card_height_px": 630,
    },
    "is_saved_foreign_share": True,
    "runtime": "node",
    "is_current_build_published": True,
    "has_server_actions": False,
    "theme_accent": "#ff8800",
    "shortcode": "abc123",
    "share_type": "cloudflare",
    "source_url": "https://muse.ai/share/abc",
    "saved_target_shortcode": "saved1",
    "view_url": "https://muse.ai/spaces/mb-canvas-demo",
}


# ---------------------------------------------------------------- 协议假件


@dataclass
class FakeResponse:
    status: int = 200
    body: bytes = b"{}"
    headers: list[tuple[str, str]] = field(
        default_factory=lambda: [("content-type", "application/json")]
    )
    ended: bool = True


def json_response(payload: object, status: int = 200) -> FakeResponse:
    return FakeResponse(status=status, body=json.dumps(payload).encode("utf-8"))


class FakeProtocolClient:
    """`HatchProtocolClient` 的鸭子替身：只记请求 + 回一条预置响应/异常。"""

    def __init__(
        self,
        *,
        response: object | None = None,
        error: BaseException | None = None,
        connect_error: BaseException | None = None,
        close_error: BaseException | None = None,
    ) -> None:
        self.response = response
        self.error = error
        self.connect_error = connect_error
        self.close_error = close_error
        self.requests: list[tuple[str, str, object, float]] = []
        self.connected = False
        self.closed = 0

    async def connect(self) -> None:
        if self.connect_error is not None:
            raise self.connect_error
        self.connected = True

    async def close(self) -> None:
        self.closed += 1
        if self.close_error is not None:
            raise self.close_error

    async def request(
        self,
        verb: str,
        path: str,
        body: object = None,
        *,
        headers: object = None,
        timeout: float = 120.0,
        end_body: bool = True,
    ) -> object:
        self.requests.append((verb, path, body, timeout))
        if self.error is not None:
            raise self.error
        return self.response


# ---------------------------------------------------------------- 归一化


def test_full_space_normalizes_to_protocol_field_table():
    normalized = normalize_space(RAW_SPACE_FULL)
    assert normalized == NORMALIZED_SPACE_FULL
    # `BUILDING` 是大写形态（§2.2 大小写双写），归一化成 constructing。
    assert normalized["construction_status"] == "constructing"


def test_missing_fields_become_null_and_is_shared_is_boolean():
    normalized = normalize_space({"slug": "only", "name": "Only"})
    assert normalized is not None
    assert set(normalized) == {
        "construction_status",
        "slug",
        "image_url",
        "name",
        "updated_at_ms",
        "accessed_at_ms",
        "frequency_score",
        "favorite_order",
        "preview_image",
        "is_saved_foreign_share",
        "runtime",
        "is_current_build_published",
        "has_server_actions",
        "theme_accent",
        "shortcode",
        "share_type",
        "source_url",
        "saved_target_shortcode",
        "view_url",
    }
    assert normalized["construction_status"] is None
    assert normalized["image_url"] is None
    assert normalized["preview_image"] is None
    assert normalized["is_saved_foreign_share"] is False
    assert normalized["is_current_build_published"] is None
    assert normalized["has_server_actions"] is None
    assert normalized["view_url"] is None
    # space_id 与 slug 相同/缺失时不重复输出（前端只在两者不同才保留）。
    assert "space_id" not in normalized


def test_slug_falls_back_to_space_id_when_slug_missing():
    normalized = normalize_space({"space_id": "uuid-1", "name": "Fallback"})
    assert normalized is not None
    assert normalized["slug"] == "uuid-1"
    # 回退值与 slug 相同 => 不再额外输出 space_id（与前端一致）。
    assert "space_id" not in normalized


def test_space_without_name_or_slug_is_unparseable():
    assert normalize_space({"name": "No Slug"}) is None
    assert normalize_space({"slug": "no-name"}) is None
    assert normalize_space("not-a-dict") is None


def test_construction_status_accepts_build_status_and_unknown_is_null():
    assert normalize_space({"slug": "a", "name": "A", "build_status": "ready"})[
        "construction_status"
    ] == "succeeded"
    assert normalize_space({"slug": "b", "name": "B", "build_status": "weird"})[
        "construction_status"
    ] is None
    # construction_status 为空串时**不**回退 build_status（前端是 `??` 不是 `||`）。
    assert normalize_space(
        {"slug": "c", "name": "C", "construction_status": "", "build_status": "failed"}
    )["construction_status"] is None


def test_preview_image_requires_version_thumbnail_and_card_url():
    raw = {
        "version": "v1",
        "thumbnail_url": "https://cdn/thumb.png",
        # 缺 preview_card_url
    }
    assert normalize_space({"slug": "x", "name": "X", "preview_image": raw})[
        "preview_image"
    ] is None
    ok = normalize_space(
        {
            "slug": "y",
            "name": "Y",
            "preview_image": {
                "version": "v1",
                "thumbnail_url": "https://cdn/thumb.png",
                "preview_card_url": "https://cdn/card.png",
            },
        }
    )
    assert ok["preview_image"] == {
        "version": "v1",
        "icon_url": None,
        "thumbnail_url": "https://cdn/thumb.png",
        "thumbnail_width_px": None,
        "thumbnail_height_px": None,
        "preview_card_url": "https://cdn/card.png",
        "preview_card_width_px": None,
        "preview_card_height_px": None,
    }


def test_non_finite_numbers_and_non_bool_flags_become_null():
    normalized = normalize_space(
        {
            "slug": "n",
            "name": "N",
            "updated_at_ms": float("nan"),
            "accessed_at_ms": float("inf"),
            "frequency_score": "3.5",
            "is_current_build_published": "true",
        }
    )
    assert normalized["updated_at_ms"] is None
    assert normalized["accessed_at_ms"] is None
    assert normalized["frequency_score"] is None
    assert normalized["is_current_build_published"] is None


def test_spaces_response_strictness_matches_frontend():
    assert normalize_spaces_response({"spaces": []}) == []
    with pytest.raises(MuseInvalidResponse):
        normalize_spaces_response({"spaces": "nope"})
    with pytest.raises(MuseInvalidResponse):
        normalize_spaces_response({"spaces": [{"slug": "ok", "name": "OK"}, {"name": "坏"}]})
    with pytest.raises(MuseInvalidResponse):
        normalize_spaces_response([])


# ---------------------------------------------------------------- 校验


@pytest.mark.parametrize("bad", ["../escape", "a/b", "a?b", "a#b", "", " ", "无限长" * 40])
def test_validate_slug_rejects_path_injection(bad):
    with pytest.raises(MuseInvalidSlug):
        validate_slug(bad)


def test_validate_slug_accepts_real_probe_shape():
    assert validate_slug("mb-canvas-probe-20261111") == "mb-canvas-probe-20261111"


# ---------------------------------------------------------------- 协议客户端


def _client(protocol: FakeProtocolClient, **kwargs) -> MuseSpacesClient:
    return MuseSpacesClient(
        {"hatch_sess": "SECRET-COOKIE-VALUE"},
        account="muse1",
        protocol_client=protocol,
        timeout=12.5,
        **kwargs,
    )


async def test_list_spaces_returns_normalized_list_and_sends_documented_request():
    protocol = FakeProtocolClient(response=json_response({"spaces": [RAW_SPACE_FULL]}))
    client = _client(protocol)
    spaces = await client.list_spaces()
    assert spaces == [NORMALIZED_SPACE_FULL]
    assert protocol.requests == [("GET", "/spaces/v2", {}, 12.5)]
    assert protocol.connected is True
    await client.close()
    assert protocol.closed == 1


async def test_list_spaces_include_constructing_passes_upstream_param():
    protocol = FakeProtocolClient(response=json_response({"spaces": []}))
    client = _client(protocol)
    await client.list_spaces(include_constructing=True)
    assert protocol.requests[0][2] == {"include_constructing": True}


async def test_list_spaces_invalid_payload_maps_to_invalid_response():
    protocol = FakeProtocolClient(response=json_response({"spaces": [{"slug": "x"}]}))
    client = _client(protocol)
    with pytest.raises(MuseInvalidResponse):
        await client.list_spaces()


async def test_get_space_accepts_nested_or_flat_success_shape():
    protocol = FakeProtocolClient(response=json_response({"space": RAW_SPACE_FULL}))
    client = _client(protocol)
    assert await client.get_space("mb-canvas-demo") == NORMALIZED_SPACE_FULL
    assert protocol.requests[0][:2] == ("GET", "/spaces/v2/mb-canvas-demo")

    protocol2 = FakeProtocolClient(response=json_response(RAW_SPACE_FULL))
    assert await _client(protocol2).get_space("mb-canvas-demo") == NORMALIZED_SPACE_FULL


async def test_get_space_404_from_real_probe_is_not_found():
    body = json.dumps(
        {
            "ok": False,
            "error": {
                "code": "space_unavailable",
                "message": "space source directory was not found",
            },
        }
    ).encode()
    protocol = FakeProtocolClient(response=FakeResponse(status=404, body=body))
    client = _client(protocol)
    with pytest.raises(MuseNotFound) as caught:
        await client.get_space("mb-canvas-probe-20261111")
    assert caught.value.status_code == 404
    assert caught.value.code == "not_found"
    assert "space source directory was not found" in caught.value.note


async def test_export_html_returns_bytes_and_filename():
    protocol = FakeProtocolClient(
        response=FakeResponse(
            status=200,
            body=b"<!doctype html><html><body>x</body></html>",
            headers=[("content-type", "text/html; charset=utf-8")],
        )
    )
    client = _client(protocol)
    result = await client.export_html("demo")
    assert result.data == b"<!doctype html><html><body>x</body></html>"
    assert result.mime == "text/html"
    assert result.filename == "demo.html"
    assert protocol.requests[0][:3] == ("GET", "/spaces/demo/export.html", None)


async def test_export_html_empty_body_is_invalid_response():
    protocol = FakeProtocolClient(response=FakeResponse(status=200, body=b""))
    with pytest.raises(MuseInvalidResponse):
        await _client(protocol).export_html("demo")


async def test_export_404_uses_real_probe_body():
    body = (
        b'{"ok":false,"error":{"code":"not_found",'
        b'"message":"web artifact was not found"}}'
    )
    protocol = FakeProtocolClient(response=FakeResponse(status=404, body=body))
    with pytest.raises(MuseNotFound) as caught:
        await _client(protocol).export_html("mb-canvas-probe-20261111")
    assert "web artifact was not found" in caught.value.note


async def test_404_route_not_found_is_protocol_error_not_business_404():
    """真机结论：`route not found` = 我们拼错路径（本进程 bug），不能当 404 交给前端。"""
    protocol = FakeProtocolClient(
        response=FakeResponse(
            status=404,
            body=b'{"ok":false,"error":{"code":"not_found","message":"route not found"}}',
        )
    )
    with pytest.raises(MuseSpacesError) as caught:
        await _client(protocol).export_html("demo")
    assert not isinstance(caught.value, MuseNotFound)
    assert caught.value.status_code == 502


@pytest.mark.parametrize(
    ("protocol_error", "muse_error"),
    [
        (ProtocolAuthError("401"), MuseAuthError),
        (ProtocolEgressError("403"), MuseEgressError),
        (ProtocolRateLimited("429"), MuseRateLimited),
        (ProtocolError("boom"), MuseSpacesError),
    ],
)
async def test_protocol_errors_are_mapped_and_never_leak_cookie(protocol_error, muse_error):
    protocol = FakeProtocolClient(error=protocol_error)
    client = _client(protocol)
    with pytest.raises(muse_error) as caught:
        await client.list_spaces()
    assert "SECRET-COOKIE-VALUE" not in str(caught.value)
    assert "muse1" in str(caught.value)  # 账号名可以出现（排障用）


async def test_connect_failure_is_mapped():
    protocol = FakeProtocolClient(connect_error=ProtocolAuthError("expired"))
    client = _client(protocol)
    with pytest.raises(MuseAuthError):
        await client.list_spaces()


async def test_close_is_idempotent_and_swallows_protocol_close_errors():
    protocol = FakeProtocolClient(close_error=RuntimeError("ws gone"))
    client = _client(protocol)
    await client.connect()
    await client.close()
    await client.close()  # 不抛
    assert protocol.closed == 2


async def test_idempotent_connect():
    protocol = FakeProtocolClient(response=json_response({"spaces": []}))
    client = _client(protocol)
    await client.connect()
    await client.connect()
    assert protocol.connected is True


async def test_list_idea_cards_normalizes_sections_and_cards():
    payload = {
        "ok": True,
        "result": {
            "sections": [
                {
                    "id": "ideas",
                    "sectionId": "ideas",
                    "title": "Ideas",
                    "layout": "ideas",
                    "presentation": {"layout": "hero"},
                    "cards": [
                        {
                            "id": "idea_58a45ab582e84f1283dceedfe00f53c2",
                            "ideaCardId": "idea_58a45ab582e84f1283dceedfe00f53c2",
                            "title": "A card",
                            "summary": "A summary",
                            "primaryLabel": "Shopping",
                            "actionLabel": "Let's do it",
                            "buildStatus": "not_started",
                            "prerequisiteNotes": "Budget",
                            "content": {
                                "title": "A card",
                                "summary": "A summary",
                                "buildSummary": "Build summary",
                            },
                            "presentation": {
                                "icon": {
                                    "variants": [
                                        {
                                            "url": "/api/idea-cards/media/icon-57e8",
                                            "role": "original",
                                            "width": 72,
                                            "height": 72,
                                            "mimeType": "image/png",
                                            "byteSize": 1045,
                                            "version": "57e8",
                                        }
                                    ]
                                },
                                "labels": [{"text": "Shopping", "role": "primary"}],
                                "badges": [{"text": "1 space"}],
                                "display_badge": {"text": "1 space"},
                                "item_count": 1,
                            },
                            "sharing": {"kind": "unavailable"},
                            "futureField": {"kept": True},
                        }
                    ],
                },
                {"id": "", "cards": []},  # 坏 section 跳过
            ],
            "viewerState": {"hasBuiltIdea": False},
            "pagination": {"hasNextPage": False, "nextCursor": None},
        },
    }
    protocol = FakeProtocolClient(response=json_response(payload))
    normalized = await _client(protocol).list_idea_cards()
    assert len(normalized["sections"]) == 1
    section = normalized["sections"][0]
    assert section["id"] == "ideas"
    assert section["title"] == "Ideas"
    card = section["cards"][0]
    assert card["id"] == section["cards"][0]["ideaCardId"]
    assert card["iconUrl"] == "/api/idea-cards/media/icon-57e8"
    assert card["iconMime"] == "image/png"
    assert card["buildStatus"] == "not_started"
    assert card["labels"] == [{"text": "Shopping", "role": "primary"}]
    assert card["extra"] == {"futureField": {"kept": True}}
    assert normalized["viewerState"] == {"hasBuiltIdea": False}
    assert protocol.requests[0][:2] == ("GET", "/api/idea-cards")


def test_idea_card_requires_id_and_title():
    assert normalize_idea_card({"title": "no id"}) is None
    assert normalize_idea_card({"id": "idea_x"}) is None
    assert normalize_idea_card("nope") is None


def test_idea_cards_response_requires_result_and_sections():
    with pytest.raises(MuseInvalidResponse):
        normalize_idea_cards_response({"ok": True})
    with pytest.raises(MuseInvalidResponse):
        normalize_idea_cards_response({"result": {"sections": "nope"}})


async def test_get_idea_card_detail_shape():
    payload = {"ok": True, "result": {"card": {"id": "idea_x", "title": "X", "items": []}}}
    protocol = FakeProtocolClient(response=json_response(payload))
    card = await _client(protocol).get_idea_card("idea_x")
    assert card["id"] == "idea_x"
    assert card["items"] == []
    assert protocol.requests[0][:2] == ("GET", "/api/idea-cards/idea_x")


async def test_get_idea_card_detail_without_card_is_invalid():
    protocol = FakeProtocolClient(response=json_response({"ok": True, "result": {}}))
    with pytest.raises(MuseInvalidResponse):
        await _client(protocol).get_idea_card("idea_x")


async def test_idea_card_media_uses_header_sniff_over_declared_mime():
    protocol = FakeProtocolClient(
        response=FakeResponse(
            status=200,
            body=PNG_BYTES,
            headers=[("content-type", "text/html")],  # 声明是错的，文件头才是事实
        )
    )
    result = await _client(protocol).idea_card_media("icon-57e8")
    assert result.data == PNG_BYTES
    assert result.mime == "image/png"
    assert protocol.requests[0][:2] == ("GET", "/api/idea-cards/media/icon-57e8")


async def test_idea_card_media_404_from_real_probe():
    body = (
        b'{"ok":false,"error":{"code":"not_found",'
        b'"message":"idea card media was not found"}}'
    )
    protocol = FakeProtocolClient(response=FakeResponse(status=404, body=body))
    with pytest.raises(MuseNotFound) as caught:
        await _client(protocol).idea_card_media("icon-x")
    assert "idea card media was not found" in caught.value.note


async def test_unknown_config_exception_does_not_leak_into_messages():
    protocol = FakeProtocolClient(error=RuntimeError("proxy http://user:pw@host"))
    with pytest.raises(MuseSpacesError) as caught:
        await _client(protocol).list_spaces()
    assert "user:pw" not in str(caught.value)
    assert "RuntimeError" in str(caught.value)


# ---------------------------------------------------------------- HTTP 端点假件


class FakeAccountStore:
    """只为 `/muse/*` 端点服务的账号库替身（`list_all` / `get` / `cookies`）。"""

    def __init__(self, rows: dict[str, dict[str, str]] | None = None) -> None:
        self.rows = dict(rows or {})

    def list_all(self) -> list[SimpleNamespace]:
        return [SimpleNamespace(account=name) for name in self.rows]

    def get(self, name: str) -> SimpleNamespace | None:
        return SimpleNamespace(account=name) if name in self.rows else None

    def cookies(self, name: str) -> dict[str, str]:
        return dict(self.rows.get(name) or {})


class FakeMuseClient:
    """`MuseSpacesClient` 的端点级替身（异步上下文 + 六个只读方法）。"""

    def __init__(self) -> None:
        self.spaces: object = []
        self.list_error: BaseException | None = None
        self.connect_error: BaseException | None = None
        self.export = MuseBinary(data=b"", mime="text/html", filename="x.html")
        self.export_error: BaseException | None = None
        self.idea_cards: object = {"sections": [], "viewerState": None, "pagination": None}
        self.idea_error: BaseException | None = None
        self.card: object = None
        self.card_error: BaseException | None = None
        self.media = MuseBinary(data=b"", mime="image/png", filename="m")
        self.media_error: BaseException | None = None
        self.calls: list[tuple[str, object]] = []
        self.entered = False
        self.closed = 0

    async def __aenter__(self) -> FakeMuseClient:
        self.entered = True
        if self.connect_error is not None:
            raise self.connect_error
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        self.closed += 1

    async def list_spaces(self, *, include_constructing: bool = False):
        self.calls.append(("list_spaces", include_constructing))
        if self.list_error is not None:
            raise self.list_error
        return self.spaces

    async def export_html(self, slug: str):
        self.calls.append(("export_html", slug))
        if self.export_error is not None:
            raise self.export_error
        return self.export

    async def list_idea_cards(self):
        self.calls.append(("list_idea_cards", None))
        if self.idea_error is not None:
            raise self.idea_error
        return self.idea_cards

    async def get_idea_card(self, card_id: str):
        self.calls.append(("get_idea_card", card_id))
        if self.card_error is not None:
            raise self.card_error
        return self.card

    async def idea_card_media(self, handle: str):
        self.calls.append(("idea_card_media", handle))
        if self.media_error is not None:
            raise self.media_error
        return self.media


@pytest.fixture
def api_factory(monkeypatch):
    """装配只含本 router 的 FastAPI；账号库依赖由每个用例指定（可为 None）。"""
    monkeypatch.delenv("MUSE_BRIDGE_API_KEYS", raising=False)

    def build(accounts: object | None):
        application = FastAPI()
        application.include_router(router)
        application.dependency_overrides[get_account_store] = lambda: accounts
        return AsyncClient(
            transport=ASGITransport(app=application), base_url="http://canvas"
        )

    return build


def use_client(monkeypatch, fake: FakeMuseClient) -> None:
    """让 `build_muse_client` 返回假客户端：端点因此零网络、零账号库。"""
    monkeypatch.setattr(canvas_api_module, "build_muse_client", lambda store: fake)


def accounts_with_cookie() -> FakeAccountStore:
    return FakeAccountStore({"muse1": {"hatch_sess": "SECRET-COOKIE-VALUE"}})


# ---------------------------------------------------------------- 账号选择


def test_pick_muse_account_prefers_muse1_then_falls_back():
    store = FakeAccountStore(
        {"muse1": {"hatch_sess": "a"}, "muse2": {"hatch_sess": "b"}}
    )
    assert pick_muse_account(store) == ("muse1", {"hatch_sess": "a"})

    store = FakeAccountStore({"muse1": {}, "muse2": {"hatch_sess": "b"}})
    assert pick_muse_account(store) == ("muse2", {"hatch_sess": "b"})

    assert pick_muse_account(FakeAccountStore({"muse1": {}})) is None
    assert pick_muse_account(FakeAccountStore()) is None


def test_account_store_dependency_is_none_without_key(monkeypatch, tmp_path):
    monkeypatch.delenv("MUSE_BRIDGE_KEY", raising=False)
    monkeypatch.setenv("MUSE_BRIDGE_DB", str(tmp_path / "accounts.db"))
    assert canvas_api_module.get_account_store() is None


# ---------------------------------------------------------------- 503 / 401


async def test_spaces_503_when_account_store_unavailable(api_factory):
    client = api_factory(None)
    async with client:
        response = await client.get("/canvas/api/muse/spaces")
    assert response.status_code == 503
    body = response.json()
    assert body["error"] == "muse_unavailable"
    assert "账号库" in body["note"]


async def test_spaces_503_when_no_account_has_cookie(api_factory):
    client = api_factory(FakeAccountStore({"muse1": {}, "muse2": {}}))
    async with client:
        response = await client.get("/canvas/api/muse/spaces")
    assert response.status_code == 503
    assert response.json()["error"] == "muse_unavailable"
    assert "cookie" in response.json()["note"]


@pytest.mark.parametrize(
    "path",
    [
        "/canvas/api/muse/spaces",
        "/canvas/api/muse/import",
        "/canvas/api/muse/export/demo",
        "/canvas/api/muse/idea-cards",
        "/canvas/api/muse/idea-cards/idea_x",
    ],
)
async def test_muse_endpoints_require_bearer(api_factory, monkeypatch, path):
    monkeypatch.setenv("MUSE_BRIDGE_API_KEYS", "key-1")
    client = api_factory(None)  # 没有账号库：鉴权必须先于 503 生效
    async with client:
        response = await client.get(path) if not path.endswith("/import") else (
            await client.post(path, json={"slug": "demo"})
        )
    assert response.status_code == 401


async def test_muse_endpoints_accept_bearer(api_factory, monkeypatch):
    monkeypatch.setenv("MUSE_BRIDGE_API_KEYS", "key-1")
    fake = FakeMuseClient()
    use_client(monkeypatch, fake)
    client = api_factory(accounts_with_cookie())
    async with client:
        response = await client.get(
            "/canvas/api/muse/spaces", headers={"Authorization": "Bearer key-1"}
        )
    assert response.status_code == 200


# ---------------------------------------------------------------- spaces 端点


async def test_spaces_200_returns_normalized_spaces(api_factory, monkeypatch):
    fake = FakeMuseClient()
    fake.spaces = [NORMALIZED_SPACE_FULL]
    use_client(monkeypatch, fake)
    client = api_factory(accounts_with_cookie())
    async with client:
        response = await client.get("/canvas/api/muse/spaces")
    assert response.status_code == 200
    assert response.json() == {"spaces": [NORMALIZED_SPACE_FULL]}
    assert fake.calls == [("list_spaces", False)]
    assert fake.entered is True and fake.closed == 1


async def test_spaces_include_constructing_query_is_passed(api_factory, monkeypatch):
    fake = FakeMuseClient()
    use_client(monkeypatch, fake)
    client = api_factory(accounts_with_cookie())
    async with client:
        response = await client.get(
            "/canvas/api/muse/spaces", params={"include_constructing": "true"}
        )
    assert response.status_code == 200
    assert fake.calls == [("list_spaces", True)]


async def test_spaces_protocol_error_maps_to_502(api_factory, monkeypatch):
    fake = FakeMuseClient()
    fake.list_error = MuseAuthError("账号 muse1：muse 会话失效")
    use_client(monkeypatch, fake)
    client = api_factory(accounts_with_cookie())
    async with client:
        response = await client.get("/canvas/api/muse/spaces")
    assert response.status_code == 502
    assert response.json()["error"] == "muse_session_expired"
    assert "SECRET-COOKIE-VALUE" not in response.text


# ---------------------------------------------------------------- 导入端点


async def test_import_returns_image_card_with_contract_keys(api_factory, monkeypatch):
    fake = FakeMuseClient()
    fake.spaces = [NORMALIZED_SPACE_FULL]
    use_client(monkeypatch, fake)
    client = api_factory(accounts_with_cookie())
    async with client:
        response = await client.post("/canvas/api/muse/import", json={"slug": "mb-canvas-demo"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"node", "space"}
    node = body["node"]
    assert set(node) == {"id", "type", "x", "y", "w", "h", "z", "data"}
    assert NODE_ID_RE.fullmatch(node["id"])
    assert node["type"] == "image"
    assert node["w"] == 1200 and node["h"] == 630
    assert set(node["data"]) == {
        "text",
        "url",
        "thumbUrl",
        "width",
        "height",
        "status",
        "muse",
    }
    assert node["data"]["url"] == "https://cdn.example/card.png"
    assert node["data"]["thumbUrl"] == "https://cdn.example/thumb.png"
    assert node["data"]["muse"]["spaceSlug"] == "mb-canvas-demo"
    assert node["data"]["muse"]["constructionStatus"] == "constructing"
    assert body["space"] == NORMALIZED_SPACE_FULL
    # include_constructing=True：构建中的 Space 也能导入。
    assert fake.calls == [("list_spaces", True)]


async def test_import_without_preview_makes_text_card(api_factory, monkeypatch):
    space = normalize_space({"slug": "no-preview", "name": "No Preview"})
    fake = FakeMuseClient()
    fake.spaces = [space]
    use_client(monkeypatch, fake)
    client = api_factory(accounts_with_cookie())
    async with client:
        response = await client.post(
            "/canvas/api/muse/import", json={"slug": "no-preview"}
        )
    assert response.status_code == 200
    node = response.json()["node"]
    assert node["type"] == "text"
    assert node["data"]["url"] is None
    assert node["w"] == 360 and node["h"] == 160


async def test_import_unknown_slug_is_404(api_factory, monkeypatch):
    fake = FakeMuseClient()
    fake.spaces = [NORMALIZED_SPACE_FULL]
    use_client(monkeypatch, fake)
    client = api_factory(accounts_with_cookie())
    async with client:
        response = await client.post("/canvas/api/muse/import", json={"slug": "missing"})
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


async def test_import_invalid_slug_is_400(api_factory, monkeypatch):
    fake = FakeMuseClient()
    use_client(monkeypatch, fake)
    client = api_factory(accounts_with_cookie())
    async with client:
        response = await client.post(
            "/canvas/api/muse/import", json={"slug": "../../etc/passwd"}
        )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_slug"
    assert fake.calls == []  # 校验没过就不该建客户端/发请求


async def test_import_protocol_error_maps_to_502(api_factory, monkeypatch):
    fake = FakeMuseClient()
    fake.list_error = MuseRateLimited("账号 muse1：被限流")
    use_client(monkeypatch, fake)
    client = api_factory(accounts_with_cookie())
    async with client:
        response = await client.post("/canvas/api/muse/import", json={"slug": "x"})
    assert response.status_code == 429
    assert response.json()["error"] == "muse_rate_limited"


def test_card_node_helper_is_pure_and_unique():
    space = normalize_space(RAW_SPACE_FULL)
    first = muse_space_card_node(space)
    second = muse_space_card_node(space)
    assert first["id"] != second["id"]
    assert first["data"]["muse"]["viewUrl"] == "https://muse.ai/spaces/mb-canvas-demo"


# ---------------------------------------------------------------- 导出端点（安全）


async def test_export_is_attachment_and_never_inline(api_factory, monkeypatch):
    payload = b"<!doctype html><html><script>alert(1)</script></html>"
    fake = FakeMuseClient()
    fake.export = MuseBinary(data=payload, mime="text/html", filename="demo.html")
    use_client(monkeypatch, fake)
    client = api_factory(accounts_with_cookie())
    async with client:
        response = await client.get("/canvas/api/muse/export/demo")
    assert response.status_code == 200
    assert response.content == payload
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert "inline" not in disposition.lower()
    # 即便某层忽略 disposition，也不能把它当 HTML 解析。
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"] == "sandbox"
    assert 'filename="demo.html"' in disposition
    assert fake.calls == [("export_html", "demo")]


async def test_export_404_maps_to_not_found(api_factory, monkeypatch):
    fake = FakeMuseClient()
    fake.export_error = MuseNotFound(
        "/spaces/demo/export.html 不存在（HTTP 404）：web artifact was not found"
    )
    use_client(monkeypatch, fake)
    client = api_factory(accounts_with_cookie())
    async with client:
        response = await client.get("/canvas/api/muse/export/demo")
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


async def test_export_connection_failure_is_502_without_stack_or_cookie(
    api_factory, monkeypatch
):
    fake = FakeMuseClient()
    fake.connect_error = MuseSpacesError("账号 muse1：muse 协议连接失败")
    use_client(monkeypatch, fake)
    client = api_factory(accounts_with_cookie())
    async with client:
        response = await client.get("/canvas/api/muse/export/demo")
    assert response.status_code == 502
    assert response.json()["error"] == "muse_protocol_error"
    assert "Traceback" not in response.text
    assert "SECRET-COOKIE-VALUE" not in response.text


async def test_export_invalid_slug_is_400(api_factory, monkeypatch):
    fake = FakeMuseClient()
    use_client(monkeypatch, fake)
    client = api_factory(accounts_with_cookie())
    async with client:
        response = await client.get("/canvas/api/muse/export/bad..name")
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_slug"


# ---------------------------------------------------------------- idea-cards 端点


async def test_idea_cards_endpoint_returns_normalized_sections(api_factory, monkeypatch):
    fake = FakeMuseClient()
    fake.idea_cards = {
        "sections": [
            {
                "id": "ideas",
                "title": "Ideas",
                "cards": [
                    {
                        "id": "idea_58a4",
                        "title": "A card",
                        "iconUrl": "/api/idea-cards/media/icon-57e8?v=57e8",
                        "iconVariants": [
                            {
                                "url": "/api/idea-cards/media/icon-57e8?v=57e8",
                                "mimeType": "image/png",
                            }
                        ],
                        "extra": {"kept": True},
                    },
                    {
                        "id": "idea_absolute",
                        "title": "Absolute",
                        # 绝对 URL 与无关相对路径不替上游改写。
                        "iconUrl": "https://cdn.example/icon.png",
                    },
                ],
            }
        ],
        "viewerState": {"hasBuiltIdea": False},
        "pagination": {"hasNextPage": False},
    }
    use_client(monkeypatch, fake)
    client = api_factory(accounts_with_cookie())
    async with client:
        response = await client.get("/canvas/api/muse/idea-cards")
    assert response.status_code == 200
    body = response.json()
    assert body["viewerState"] == {"hasBuiltIdea": False}
    cards = body["sections"][0]["cards"]
    # 上游相对路径改写成同源代理，`?v=` 版本串保留（图标缓存键）。
    assert cards[0]["iconUrl"] == (
        "/canvas/api/muse/idea-cards/media/icon-57e8?v=57e8"
    )
    assert cards[0]["iconVariants"][0]["url"] == cards[0]["iconUrl"]
    assert cards[0]["extra"] == {"kept": True}
    assert cards[1]["iconUrl"] == "https://cdn.example/icon.png"


async def test_idea_card_detail_endpoint(api_factory, monkeypatch):
    fake = FakeMuseClient()
    fake.card = {
        "id": "idea_x",
        "title": "X",
        "items": [],
        "iconUrl": "/api/idea-cards/media/icon-57e8?v=57e8",
    }
    use_client(monkeypatch, fake)
    client = api_factory(accounts_with_cookie())
    async with client:
        response = await client.get("/canvas/api/muse/idea-cards/idea_x")
    assert response.status_code == 200
    assert response.json() == {
        "card": {
            "id": "idea_x",
            "title": "X",
            "items": [],
            "iconUrl": "/canvas/api/muse/idea-cards/media/icon-57e8?v=57e8",
        }
    }


async def test_idea_card_detail_invalid_id_is_400(api_factory, monkeypatch):
    fake = FakeMuseClient()
    use_client(monkeypatch, fake)
    client = api_factory(accounts_with_cookie())
    async with client:
        response = await client.get("/canvas/api/muse/idea-cards/bad..id")
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_slug"


async def test_idea_card_media_inlines_png_with_sniffed_mime(api_factory, monkeypatch):
    fake = FakeMuseClient()
    fake.media = MuseBinary(data=PNG_BYTES, mime="image/png", filename="icon-x")
    use_client(monkeypatch, fake)
    client = api_factory(accounts_with_cookie())
    async with client:
        response = await client.get("/canvas/api/muse/idea-cards/media/icon-57e8")
    assert response.status_code == 200
    assert response.content == PNG_BYTES
    assert response.headers["content-type"] == "image/png"
    assert response.headers["content-disposition"].startswith("inline")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert fake.calls == [("idea_card_media", "icon-57e8")]


@pytest.mark.parametrize("mime", ["image/svg+xml", "text/html", "application/octet-stream"])
async def test_idea_card_media_forces_attachment_for_non_raster(
    api_factory, monkeypatch, mime
):
    fake = FakeMuseClient()
    fake.media = MuseBinary(data=b"<svg>alert(1)</svg>", mime=mime, filename="x")
    use_client(monkeypatch, fake)
    client = api_factory(accounts_with_cookie())
    async with client:
        response = await client.get("/canvas/api/muse/idea-cards/media/handle-x")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["content-disposition"].startswith("attachment")
    assert response.headers["content-security-policy"] == "sandbox"


async def test_idea_card_media_invalid_handle_is_404(api_factory, monkeypatch):
    fake = FakeMuseClient()
    use_client(monkeypatch, fake)
    client = api_factory(accounts_with_cookie())
    async with client:
        response = await client.get("/canvas/api/muse/idea-cards/media/bad..handle")
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"
    assert fake.calls == []


async def test_idea_card_media_404_maps_and_requires_no_bearer(api_factory, monkeypatch):
    monkeypatch.setenv("MUSE_BRIDGE_API_KEYS", "key-1")
    fake = FakeMuseClient()
    fake.media_error = MuseNotFound("idea card media was not found")
    use_client(monkeypatch, fake)
    client = api_factory(accounts_with_cookie())
    async with client:
        # 不带 Bearer：`<img>` 发不了请求头，这是与 /canvas/api/media 一致的先例。
        response = await client.get("/canvas/api/muse/idea-cards/media/icon-x")
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


# ---------------------------------------------------------------- 零网络


async def test_endpoints_never_open_a_socket(api_factory, monkeypatch):
    """把 socket 入口全部打断；只要有一条真网络路径，这里就会炸。"""

    def refuse(*args, **kwargs):
        raise AssertionError("测试不允许打开真实网络连接")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)

    fake = FakeMuseClient()
    fake.spaces = [NORMALIZED_SPACE_FULL]
    fake.export = MuseBinary(data=PNG_BYTES, mime="image/png", filename="demo.html")
    use_client(monkeypatch, fake)
    client = api_factory(accounts_with_cookie())
    async with client:
        assert (await client.get("/canvas/api/muse/spaces")).status_code == 200
        assert (await client.get("/canvas/api/muse/idea-cards")).status_code == 200
        assert (await client.get("/canvas/api/muse/idea-cards/idea_x")).status_code == 200
        response = await client.get("/canvas/api/muse/idea-cards/media/icon-x")
        assert response.status_code == 200
        response = await client.get("/canvas/api/muse/export/demo")
        assert response.status_code == 200
        response = await client.post(
            "/canvas/api/muse/import", json={"slug": "mb-canvas-demo"}
        )
        assert response.status_code == 200
    assert fake.closed == 6  # 六个请求，每个都收尾了连接
