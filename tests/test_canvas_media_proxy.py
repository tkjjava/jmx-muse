"""画布同源媒体代理（`/canvas/api/media/{token}`）的离线测试。

只测 HTTP 映射这一层：`media.MediaStore` 用假实现替换（真实现要读
`data/media`，测试不能依赖仓库里恰好有一张图）。真正需要保证的四件事：
1. 200 时 `Content-Type` 是真实 mime、`Content-Disposition: inline`
   （`/v1/media` 的 octet-stream+attachment 会挡住 `<img>` 就地渲染）；
2. `Range` 请求回 206 + `Content-Range`（视频节点要拖进度条）；
3. 未知 token 回同形 404 JSON，不给探测信号；
4. 该路由**不需要** Bearer（`<img>` 发不了自定义头）。
"""

from __future__ import annotations

import types

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from muse_bridge import canvas_api as canvas_api_module
from muse_bridge import media
from muse_bridge.canvas_api import router

IMAGE_BYTES = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x01" * 200


class _NotFound(media.MediaError):
    status_code = 404


class _FakeMediaStore:
    """只实现 canvas_api 用到的那几个方法。"""

    TOKEN = "6de2c149984d457a99c24c0aa38bf62b"

    def ref(self, token: str) -> types.SimpleNamespace:
        if token != self.TOKEN:
            raise _NotFound("媒体不存在")
        return types.SimpleNamespace(
            size=len(IMAGE_BYTES),
            filename=f"{self.TOKEN}.webp",
            mime="image/webp",
            token=self.TOKEN,
        )

    def read(self, token: str) -> bytes:
        return IMAGE_BYTES

    def read_range(self, token: str, first: int, last: int):
        end = min(last, len(IMAGE_BYTES) - 1)
        chunk = IMAGE_BYTES[first : end + 1]
        return chunk, first, first + len(chunk) - 1, len(IMAGE_BYTES)


@pytest.fixture
async def api(monkeypatch):
    monkeypatch.delenv("MUSE_BRIDGE_API_KEYS", raising=False)
    monkeypatch.setattr(
        canvas_api_module.media, "MediaStore", lambda: _FakeMediaStore()
    )
    application = FastAPI()
    application.include_router(router)
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://canvas") as client:
        yield client


async def test_media_proxy_serves_inline_with_real_mime(api):
    response = await api.get(f"/canvas/api/media/{_FakeMediaStore.TOKEN}")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/webp"
    assert response.headers["content-disposition"] == "inline"
    assert response.headers["x-muse-media-mime"] == "image/webp"
    assert response.content == IMAGE_BYTES


async def test_media_proxy_honours_range_single_interval(api):
    response = await api.get(
        f"/canvas/api/media/{_FakeMediaStore.TOKEN}", headers={"range": "bytes=0-99"}
    )
    assert response.status_code == 206
    assert response.headers["content-range"] == f"bytes 0-99/{len(IMAGE_BYTES)}"
    assert response.headers["content-length"] == "100"
    assert response.content == IMAGE_BYTES[:100]


async def test_media_proxy_unknown_token_is_404_json(api):
    response = await api.get("/canvas/api/media/nope")
    assert response.status_code == 404
    assert response.json() == {"error": "not_found", "note": "媒体不存在"}


async def test_media_proxy_needs_no_authorization_header(api, monkeypatch):
    """配了 key 也要能匿名读：`<img>` 带不了头，这是本路由存在的全部理由。"""

    monkeypatch.setenv("MUSE_BRIDGE_API_KEYS", "secret-key")
    response = await api.get(f"/canvas/api/media/{_FakeMediaStore.TOKEN}")
    assert response.status_code == 200
