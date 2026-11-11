"""两处"失败即静默"的路径，各自必须留下可追溯的痕迹。

1. `app._job_response`：任务已完成但成品文件已被 TTL 清理 → 响应里没有
   mime/size，客户端只看到一个"completed 却没有元数据"的任务。以前 `pass`
   吞掉，排障只能猜；现在计 `media_meta_missing`。
2. `canvas_api.get_account_store`：账号库打不开（路径错/权限/磁盘）→ 对外按
   "没有账号"处理，但**日志要能看出是库的问题**。以前连异常类型都不记，
   "没有 muse 账号"和"库坏了"在运维眼里长得一模一样。
"""

from __future__ import annotations

import logging

import pytest

from muse_bridge import app as app_mod
from muse_bridge import canvas_api, jobs, media, observability


@pytest.fixture(autouse=True)
def _clean():
    observability.reset()
    yield
    observability.reset()


class _BoomStore:
    def ref(self, token: str):
        raise media.MediaError("已清理")


def test_completed_job_with_missing_media_counts(monkeypatch) -> None:
    monkeypatch.setattr(media, "MediaStore", lambda: _BoomStore())
    job = jobs.Job(
        id="t1", kind="image", prompt="p", state="completed", media_token="tok-1"
    )
    payload = app_mod._job_response(job)
    assert payload["status"] == "completed"
    assert payload["url"].endswith("/v1/media/tok-1")
    # 没有元数据，但请求本身不该出错
    assert "mime" not in payload and "size" not in payload
    assert observability.snapshot()["counters"]["media_meta_missing"] == 1


class _GoodRef:
    mime = "image/png"
    size = 10
    kind = "image"


class _GoodStore:
    def ref(self, token: str):
        return _GoodRef()


def test_completed_job_with_present_media_does_not_count(monkeypatch) -> None:
    monkeypatch.setattr(media, "MediaStore", lambda: _GoodStore())
    job = jobs.Job(id="t2", kind="image", prompt="p", state="completed", media_token="tok-2")
    payload = app_mod._job_response(job)
    assert payload["mime"] == "image/png"
    assert "media_meta_missing" not in observability.snapshot()["counters"]


def test_canvas_store_failure_is_logged_and_still_returns_none(monkeypatch, caplog) -> None:
    def boom(path):
        raise PermissionError("无法打开账号库")

    monkeypatch.setattr(canvas_api, "AccountStore", boom)
    monkeypatch.setenv(canvas_api.DB_ENV, "C:/tmp/never-opened.db")
    canvas_api._account_stores.clear()

    with caplog.at_level(logging.DEBUG, logger=canvas_api.__name__):
        assert canvas_api.get_account_store() is None
    assert "画布账号库不可用" in caplog.text
    canvas_api._account_stores.clear()
