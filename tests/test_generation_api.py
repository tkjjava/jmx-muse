"""长尾端点的契约：图像编辑 / fal 队列兼容 / 运维任务 / 额度 / keepalive。

为什么单独钉：这些端点服务的是**别人的 SDK 形状**（OpenAI images edits、fal
队列、运维脚本）。业务重构不会让它们报错，只会让它们**悄悄改形状**——客户端
拿到字段消失或 422 时，错在客户端一侧，看不出是 bridge 改了契约。所以这里
逐条钉字段名、状态码与降级行为，全部离线（假 driver / 假 pool，不起浏览器）。
"""

from __future__ import annotations

import base64
import io
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient

from muse_bridge import app as app_module
from muse_bridge import jobs, media, pool
from muse_bridge.driver import BrowserUnavailable
from muse_bridge.session_http import SessionExpired
from muse_bridge.state import AccountState
from muse_bridge.store import AccountStore

PNG = b"\x89PNG\r\n\x1a\n" + b"p" * 64
TOKEN = "a" * 32
ACCOUNT = "acc-1"


@pytest.fixture
def bridge_key(monkeypatch):
    monkeypatch.setenv("MUSE_BRIDGE_KEY", Fernet.generate_key().decode())
    # 生成开关默认读环境变量；把它固定成打开，避免跑测的机器上被关掉。
    monkeypatch.delenv("MUSE_GENERATION_ENABLED", raising=False)


@pytest.fixture
def store(bridge_key, tmp_path) -> AccountStore:
    instance = AccountStore(tmp_path / "api.db")
    yield instance
    instance.close()


@pytest.fixture
def media_dir(tmp_path) -> media.MediaStore:
    return media.MediaStore(tmp_path / "media")


@pytest.fixture
async def api(monkeypatch, store):
    """把 app 的进程级 store 换成临时库，并端到端走 ASGI（不 bind 端口）。"""
    monkeypatch.setattr(app_module, "store", lambda: store)
    transport = ASGITransport(app=app_module.app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        yield client


@pytest.fixture
def generation(monkeypatch):
    """把 pool.generate_media 换成记录参数的假实现。"""
    calls: list[dict] = []
    result = {
        "id": "task_abc",
        "kind": "image",
        "status": "completed",
        "progress": 100,
        "created_at": 100,
        "url": f"/v1/media/{TOKEN}",
        "media": {"token": TOKEN, "width": 512, "height": 512},
    }

    async def fake(store_arg, **kwargs):
        calls.append(kwargs)
        return dict(result)

    monkeypatch.setattr(pool, "generate_media", fake)
    return calls


def _task_row(store: AccountStore, *, state: str, kind: str = "image", token: str = ""):
    """在真库里造一条任务。终态必须走 finish()/fail()——update() 拒绝写终态。"""
    tasks = jobs.TaskStore(store)
    job = tasks.create(kind, "测试提示词", ttl_seconds=3600)
    if state == jobs.JobStatus.QUEUED.value:
        return tasks.must_get(job.id)
    if state == jobs.JobStatus.FAILED.value:
        return tasks.fail(job.id, "generation_failed: 假失败")
    return tasks.finish(
        job.id,
        state=state,
        media_token=token,
        progress=100 if state == jobs.JobStatus.COMPLETED.value else 0,
    )


# ---- /v1/images/edits --------------------------------------------------


@pytest.mark.asyncio
async def test_images_edits_requires_at_least_one_reference(api):
    """没有参考图就不是"编辑"：必须 400 并点名 images，不能静默当纯文本生成。"""
    response = await api.post(
        "/v1/images/edits", json={"prompt": "把背景换成海", "images": []}
    )
    assert response.status_code == 400, response.text
    body = response.json()
    assert "images" in body["error"]["message"]


@pytest.mark.asyncio
async def test_images_edits_multipart_gets_reasoned_400(api):
    """multipart 是官方的形状，我们只支持 JSON：错误里必须写清怎么改。"""
    response = await api.post(
        "/v1/images/edits",
        files={"image": ("a.png", PNG, "image/png")},
        data={"prompt": "改背景"},
    )
    assert response.status_code == 400, response.text
    message = response.json()["error"]["message"]
    assert "multipart" in message
    assert "JSON" in message


@pytest.mark.asyncio
async def test_images_edits_runs_generation_with_references(api, generation):
    """编辑端点必须把 images 当参考素材传下去，并把 task_id 回成真实 id。"""
    response = await api.post(
        "/v1/images/edits",
        json={"prompt": "把天空换成黄昏", "images": ["本地参考.png"]},
    )
    assert response.status_code == 200, response.text
    assert generation[-1]["references"] == ["本地参考.png"]
    assert generation[-1]["kind"] == "image"
    body = response.json()
    assert body["data"][0]["url"] == f"/v1/media/{TOKEN}"
    assert body["task_id"] == "task_abc"
    assert body["data"][0]["width"] == 512


@pytest.mark.asyncio
async def test_images_edits_b64_json_reads_media_once(api, generation, monkeypatch, media_dir):
    """response_format=b64_json 要回真字节；之前对同一个 token 读两次盘。"""
    ref = media_dir.save(PNG, "image/png", "image", expect="image")
    monkeypatch.setattr(media, "MediaStore", lambda *a, **k: media_dir)
    monkeypatch.setattr(
        pool,
        "generate_media",
        _generation_returning("task_b64", ref.token),
    )
    response = await api.post(
        "/v1/images/edits",
        json={
            "prompt": "给猫戴帽子",
            "images": ["x.png"],
            "response_format": "b64_json",
        },
    )
    assert response.status_code == 200, response.text
    item = response.json()["data"][0]
    assert base64.b64decode(item["b64_json"]) == PNG


@pytest.mark.asyncio
async def test_images_generations_b64_webp_is_delivered_as_png(
    api, monkeypatch, media_dir
):
    """muse 出图常是 WebP；b64_json 没有 mime 字段，严格客户端按 PNG 解。"""
    Image = pytest.importorskip("PIL.Image")
    raw = io.BytesIO()
    Image.new("RGBA", (4, 4), (255, 0, 0, 128)).save(raw, format="WEBP")
    ref = media_dir.save(raw.getvalue(), "image/webp", "image", expect="image")
    monkeypatch.setattr(media, "MediaStore", lambda *a, **k: media_dir)
    monkeypatch.setattr(
        pool, "generate_media", _generation_returning("task_webp", ref.token)
    )
    response = await api.post(
        "/v1/images/generations",
        json={"prompt": "红点", "response_format": "b64_json"},
    )
    assert response.status_code == 200, response.text
    delivered = base64.b64decode(response.json()["data"][0]["b64_json"])
    assert delivered.startswith(b"\x89PNG\r\n\x1a\n"), "b64_json 必须交付真 PNG"


def _generation_returning(task_id: str, token: str):
    async def fake(store_arg, **kwargs):
        return {
            "id": task_id,
            "kind": "image",
            "status": "completed",
            "created_at": 1,
            "url": f"/v1/media/{token}",
            "media": {"token": token},
        }

    return fake


# ---- fal 队列兼容 ------------------------------------------------------


@pytest.mark.asyncio
async def test_fal_models_lists_supported_models(api):
    response = await api.get("/v1/fal/models")
    assert response.status_code == 200, response.text
    ids = [item["id"] for item in response.json()["models"]]
    assert ids == ["muse-image", "muse-video"]


@pytest.mark.asyncio
async def test_fal_submit_returns_queue_shape_and_encodes_params(api, generation):
    """fal 客户端拿到 request_id + status IN_QUEUE 才会开始轮询。"""
    response = await api.post(
        "/v1/fal/muse/video",
        json={"prompt": "海边走路", "duration": 8, "aspect_ratio": "9:16"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "IN_QUEUE"
    assert body["request_id"].startswith("task_")
    assert body["status_url"].endswith(f"/{body['request_id']}/status")

    # 入队后 runner 在后台跑一拍：把参数编码钉住（时长/比例只能写进提示词）。
    import asyncio

    await asyncio.sleep(0.05)
    assert generation, "后台 runner 没有调用 generate_media"
    assert generation[-1]["kind"] == "video"
    # 措辞与 protocol_generation 一致（gen_params 是唯一出处），不是 --duration。
    assert "时长严格为 8 秒" in generation[-1]["prompt"]
    assert "画面比例严格 9:16" in generation[-1]["prompt"]


@pytest.mark.asyncio
async def test_fal_rejects_unverified_seed_instead_of_encoding_it(api, generation):
    """seed 在 HYPOTHESIS_FIELDS 里：fal 客户端常用它，但我们必须拒绝而不是假装支持。"""
    response = await api.post("/v1/fal/muse/image", json={"prompt": "画只猫", "seed": 42})
    assert response.status_code == 400, response.text
    assert "尚未实证" in response.text
    assert generation == []


@pytest.mark.asyncio
async def test_fal_rejects_duration_outside_whitelist(api, generation):
    response = await api.post("/v1/fal/muse/video", json={"prompt": "走路", "duration": 7})
    assert response.status_code == 400, response.text
    assert generation == []


@pytest.mark.asyncio
async def test_fal_status_maps_job_states(api, store):
    job = _task_row(store, state=jobs.JobStatus.QUEUED.value)
    response = await api.get(f"/v1/fal/muse/requests/{job.id}/status")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "IN_QUEUE"
    assert response.json()["queue_position"] == 1

    tasks = jobs.TaskStore(store)
    tasks.start(job.id, ACCOUNT)
    response = await api.get(f"/v1/fal/muse/requests/{job.id}/status")
    assert response.json()["status"] == "IN_PROGRESS"
    assert response.json()["queue_position"] == 0

    tasks.finish(
        job.id, state=jobs.JobStatus.COMPLETED.value, media_token=TOKEN, progress=100
    )
    response = await api.get(f"/v1/fal/muse/requests/{job.id}/status")
    assert response.json()["status"] == "COMPLETED"


@pytest.mark.asyncio
async def test_fal_status_unknown_request_is_404(api):
    response = await api.get("/v1/fal/muse/requests/task_nope/status")
    assert response.status_code == 404, response.text


@pytest.mark.asyncio
async def test_fal_result_pending_is_202(api, store):
    """没跑完要回 202：fal 客户端把 200 当"可以取结果"，回 200 会让它拿到空。"""
    job = _task_row(store, state=jobs.JobStatus.QUEUED.value)
    response = await api.get(f"/v1/fal/muse/requests/{job.id}")
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "IN_QUEUE"


@pytest.mark.asyncio
async def test_fal_result_completed_shape(api, store):
    job = _task_row(
        store, state=jobs.JobStatus.COMPLETED.value, token=TOKEN
    )
    response = await api.get(f"/v1/fal/muse/requests/{job.id}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "COMPLETED"
    assert body["images"][0]["url"] == f"/v1/media/{TOKEN}"
    assert body["task"]["id"] == job.id


@pytest.mark.asyncio
async def test_fal_result_failed_is_error_with_class(api, store):
    job = _task_row(store, state=jobs.JobStatus.QUEUED.value)
    jobs.TaskStore(store).fail(job.id, "quota_exhausted: 额度用尽")
    response = await api.get(f"/v1/fal/muse/requests/{job.id}")
    assert response.status_code == 500, response.text
    assert response.json()["error"]["code"] == "quota_exhausted"


# ---- /admin/tasks ------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_tasks_list_and_queued_count(api, store):
    _task_row(store, state=jobs.JobStatus.QUEUED.value)
    _task_row(store, state=jobs.JobStatus.COMPLETED.value, token=TOKEN)
    response = await api.get("/admin/tasks")
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["tasks"]) == 2
    assert body["queued"] == 1
    # 任务对外形状里不能带 account（内部标签）。
    assert all("account" not in item for item in body["tasks"])


@pytest.mark.asyncio
async def test_admin_task_by_id_and_404(api, store):
    job = _task_row(store, state=jobs.JobStatus.QUEUED.value)
    response = await api.get(f"/admin/tasks/{job.id}")
    assert response.status_code == 200, response.text
    assert response.json()["id"] == job.id
    assert response.json()["status"] == "queued"

    response = await api.get("/admin/tasks/task_nope")
    assert response.status_code == 404, response.text


@pytest.mark.asyncio
async def test_admin_tasks_clear_only_terminal_and_keeps_newest(api, store):
    """清空只删终态；活动任务一条不动，`keep` 保留最新 N 条."""
    first = _task_row(store, state=jobs.JobStatus.COMPLETED.value, token=TOKEN)
    second = _task_row(store, state=jobs.JobStatus.FAILED.value)
    live = _task_row(store, state=jobs.JobStatus.QUEUED.value)

    response = await api.post("/admin/tasks/clear?keep=1")
    assert response.status_code == 200, response.text
    # 只有 first 被删（second 是更"新"的一条终态，被 keep 保住）。
    assert response.json()["removed"] == 1

    remaining = {job.id for job in jobs.TaskStore(store).list(limit=200)}
    assert remaining == {second.id, live.id}
    assert first.id not in remaining


# ---- 媒体删除 ----------------------------------------------------------


@pytest.mark.asyncio
async def test_media_delete_drops_token(api, monkeypatch, media_dir):
    ref = media_dir.save(PNG, "image/png", "image", expect="image")
    monkeypatch.setattr(media, "MediaStore", lambda *a, **k: media_dir)

    response = await api.delete(f"/v1/media/{ref.token}")
    assert response.status_code == 200, response.text
    assert response.json()["deleted"] is True
    assert media_dir.exists(ref.token) is False

    # 非法/不存在都回 deleted=False，而不是 404：删除不该成为 token 探测器。
    assert (await api.delete("/v1/media/not-a-token")).json()["deleted"] is False


# ---- 运维面：额度 / keepalive ------------------------------------------


class _FakeDriver:
    def __init__(self, cdp_url: str) -> None:
        self.cdp_url = cdp_url
        self.calls: list[str] = []

    async def touch_session(self) -> bool:
        self.calls.append("touch")
        return True

    async def wake_vm(self) -> bool:
        self.calls.append("wake")
        return True

    async def quota(self, *, timeout: float = 12.0) -> dict:
        self.calls.append("quota")
        return {"found": True, "weekly_used_pct": 7}

    async def close(self) -> None:
        return None


class _DeadDriver(_FakeDriver):
    async def touch_session(self) -> bool:
        raise BrowserUnavailable("cdp 连不上")

    async def quota(self, *, timeout: float = 12.0) -> dict:
        raise BrowserUnavailable("cdp 连不上")


def _seed_account(store: AccountStore) -> None:
    store.upsert(
        ACCOUNT,
        cdp_url="http://127.0.0.1:9222",
        state=AccountState.READY,
        vm_ready=False,
    )


@pytest.mark.asyncio
async def test_admin_keepalive_runs_both_stages(api, store, monkeypatch):
    _seed_account(store)
    driver = _FakeDriver("http://127.0.0.1:9222")
    monkeypatch.setattr(app_module, "MuseDriver", lambda cdp: driver)
    response = await api.post(f"/admin/accounts/{ACCOUNT}/keepalive")
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["refreshed"], body["woken"], body["ok"]) == (True, True, True)
    assert driver.calls == ["touch", "wake"]
    assert store.get(ACCOUNT).vm_ready is True


@pytest.mark.asyncio
async def test_admin_keepalive_maps_missing_account_to_404(api):
    response = await api.post("/admin/accounts/不存在的号/keepalive")
    assert response.status_code == 404, response.text


@pytest.mark.asyncio
async def test_admin_keepalive_browser_failure_is_503(api, store, monkeypatch):
    _seed_account(store)
    monkeypatch.setattr(app_module, "MuseDriver", _DeadDriver)
    response = await api.post(f"/admin/accounts/{ACCOUNT}/keepalive")
    assert response.status_code == 503, response.text
    assert response.json()["detail"]["error"] == "browser_unavailable"


@pytest.mark.asyncio
async def test_admin_quota_returns_parsed_panel(api, store, monkeypatch):
    _seed_account(store)
    monkeypatch.setattr(app_module, "MuseDriver", _FakeDriver)
    response = await api.get(f"/admin/accounts/{ACCOUNT}/quota")
    assert response.status_code == 200, response.text
    assert response.json()["quota"]["weekly_used_pct"] == 7


@pytest.mark.asyncio
async def test_admin_quota_timeout_is_504(api, store, monkeypatch):
    _seed_account(store)

    class _TimeoutDriver(_FakeDriver):
        async def quota(self, *, timeout: float = 12.0) -> dict:
            raise TimeoutError("额度面板未读到有效用量")

    monkeypatch.setattr(app_module, "MuseDriver", _TimeoutDriver)
    response = await api.get(f"/admin/accounts/{ACCOUNT}/quota")
    assert response.status_code == 504, response.text


# ---- cookie 导入 + 纯 HTTP keepalive ------------------------------------


@pytest.mark.asyncio
async def test_import_cookies_stores_names_and_hides_values(api, store):
    """导入只回名字：响应体会进日志与浏览器缓存，那里不该有会话凭据。"""
    _seed_account(store)
    response = await api.post(
        f"/admin/accounts/{ACCOUNT}/cookies",
        json={"cookies": {"hatch_sess": "s3cret-sess", "hatch_vml": "vml", "": "x", "empty": "  "}},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["stored"] == ["hatch_sess", "hatch_vml"]
    assert body["count"] == 2
    assert "s3cret-sess" not in response.text
    # 真的落进了加密 profile（读回来是干净的非空键值对）。
    assert store.cookies(ACCOUNT) == {"hatch_sess": "s3cret-sess", "hatch_vml": "vml"}


@pytest.mark.asyncio
async def test_import_cookies_empty_is_400(api, store):
    _seed_account(store)
    response = await api.post(f"/admin/accounts/{ACCOUNT}/cookies", json={"cookies": {"a": ""}})
    assert response.status_code == 400, response.text
    assert store.cookies(ACCOUNT) == {}


@pytest.mark.asyncio
async def test_import_cookies_unknown_account_is_404(api):
    response = await api.post("/admin/accounts/不存在的号/cookies", json={"cookies": {"hatch_sess": "v"}})
    assert response.status_code == 404, response.text


@pytest.mark.asyncio
async def test_keepalive_prefers_http_and_never_touches_browser(api, store, monkeypatch):
    """导入过 cookie 就必须走纯 HTTP：浏览器路径一次都不能碰。"""
    _seed_account(store)
    store.save_cookies(ACCOUNT, {"hatch_sess": "old"})

    def _no_browser(*_a, **_k):
        raise AssertionError("有 cookie 时不该构造 MuseDriver")

    monkeypatch.setattr(app_module, "MuseDriver", _no_browser)

    async def fake_renew(cookies, **_k):
        assert cookies == {"hatch_sess": "old"}
        return SimpleNamespace(
            cookies={"hatch_sess": "new", "hatch_vml": "fresh"},
            assigned=True,
            reason="",
            status="assigned",
        )

    async def fake_wake(session, **_k):
        assert session.assigned
        return SimpleNamespace(wake_ok=True, vm_state="RUNNING")

    monkeypatch.setattr(app_module, "renew_session", fake_renew)
    monkeypatch.setattr(app_module, "http_wake_vm", fake_wake)

    response = await api.post(f"/admin/accounts/{ACCOUNT}/keepalive")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["via"] == "http"
    assert (body["refreshed"], body["woken"], body["ok"]) == (True, True, True)
    # 服务端回的新 cookie 必须落回去，否则下一轮还用旧串。
    assert store.cookies(ACCOUNT)["hatch_sess"] == "new"
    assert store.get(ACCOUNT).vm_ready is True


@pytest.mark.asyncio
async def test_wake_prefers_http_and_never_touches_browser(api, store, monkeypatch):
    """`/wake` 与 keepalive 走同一条纯 HTTP 路径：有 cookie 时不碰浏览器。

    真机事故：这个端点早期只会走浏览器点击，页面停在「Muse 正在连接…」时
    返回 `{"ok": false}`，既不说"VM 没醒"也不说"根本点不动"，排障信息全丢。
    现在有 cookie 时只走 `hatch/vm/wake`，并逐段报告 refreshed/woken/vm_state。
    """
    _seed_account(store)
    store.save_cookies(ACCOUNT, {"hatch_sess": "old"})

    def _no_browser(*_a, **_k):
        raise AssertionError("有 cookie 时 /wake 不该构造 MuseDriver")

    monkeypatch.setattr(app_module, "MuseDriver", _no_browser)

    async def fake_renew(cookies, **_k):
        return SimpleNamespace(
            cookies={"hatch_sess": "new"},
            assigned=True,
            reason="",
            status="assigned",
        )

    async def fake_wake(session, **_k):
        return SimpleNamespace(wake_ok=True, vm_state="RUNNING")

    monkeypatch.setattr(app_module, "renew_session", fake_renew)
    monkeypatch.setattr(app_module, "http_wake_vm", fake_wake)

    response = await api.post(f"/admin/accounts/{ACCOUNT}/wake")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["via"] == "http"
    assert (body["ok"], body["refreshed"], body["woken"]) == (True, True, True)
    assert body["vm_state"] == "RUNNING"


@pytest.mark.asyncio
async def test_keepalive_http_session_error_maps_status(api, store, monkeypatch):
    """401 就是会话死了：不能折成 503，否则运维会去查代理而不是换号/重登。"""
    _seed_account(store)
    store.save_cookies(ACCOUNT, {"hatch_sess": "dead"})

    async def fake_renew(cookies, **_k):
        raise SessionExpired("cookie 已失效")

    monkeypatch.setattr(app_module, "renew_session", fake_renew)
    response = await api.post(f"/admin/accounts/{ACCOUNT}/keepalive")
    assert response.status_code == 401, response.text


@pytest.mark.asyncio
async def test_keepalive_http_unassigned_reports_reason(api, store, monkeypatch):
    """status 不是 assigned 时给 ok=False + 原因，而不是假装成功。"""
    _seed_account(store)
    store.save_cookies(ACCOUNT, {"hatch_sess": "x"})

    async def fake_renew(cookies, **_k):
        return SimpleNamespace(
            cookies={"hatch_sess": "x"},
            assigned=False,
            reason="会话状态未确认（status=pending）",
            status="pending",
        )

    monkeypatch.setattr(app_module, "renew_session", fake_renew)
    response = await api.post(f"/admin/accounts/{ACCOUNT}/keepalive")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is False and body["refreshed"] is False
    assert "pending" in body["detail"]


@pytest.mark.asyncio
async def test_refresh_prefers_http_and_never_touches_browser(api, store, monkeypatch):
    """`/refresh` 与 keepalive 同口径：有 cookie 就纯 HTTP，不构造 MuseDriver。

    真机事故：refresh 曾是唯一无条件走 CDP 的运维端点。浏览器没开时 keepalive
    能把号救回来，refresh 却 503——同一个"续期"动作两条路径两套前提。
    """
    _seed_account(store)
    store.save_cookies(ACCOUNT, {"hatch_sess": "old"})

    def _no_browser(*_a, **_k):
        raise AssertionError("有 cookie 时 /refresh 不该构造 MuseDriver")

    monkeypatch.setattr(app_module, "MuseDriver", _no_browser)

    async def fake_renew(cookies, **_k):
        assert cookies == {"hatch_sess": "old"}
        return SimpleNamespace(
            cookies={"hatch_sess": "new", "hatch_vml": "fresh"},
            assigned=True,
            reason="",
            status="assigned",
        )

    monkeypatch.setattr(app_module, "renew_session", fake_renew)

    response = await api.post(f"/admin/accounts/{ACCOUNT}/refresh")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["via"] == "http"
    assert body["ok"] is True and body["refreshed"] is True
    # 新 cookie 必须落回去，否则下一轮还是旧串。
    assert store.cookies(ACCOUNT)["hatch_sess"] == "new"
    assert store.cookies(ACCOUNT)["hatch_vml"] == "fresh"


@pytest.mark.asyncio
async def test_refresh_http_unassigned_reports_reason(api, store, monkeypatch):
    """status 不是 assigned 时给 ok=False + 原因，不假装续期成功。"""
    _seed_account(store)
    store.save_cookies(ACCOUNT, {"hatch_sess": "x"})

    async def fake_renew(cookies, **_k):
        return SimpleNamespace(
            cookies={"hatch_sess": "x"},
            assigned=False,
            reason="会话状态未确认（status=pending）",
            status="pending",
        )

    monkeypatch.setattr(app_module, "renew_session", fake_renew)
    response = await api.post(f"/admin/accounts/{ACCOUNT}/refresh")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is False and body["refreshed"] is False
    assert "pending" in body["detail"]


@pytest.mark.asyncio
async def test_refresh_http_session_error_maps_status(api, store, monkeypatch):
    """401 就是会话死了：不能折成 503，否则运维会去查代理。"""
    _seed_account(store)
    store.save_cookies(ACCOUNT, {"hatch_sess": "dead"})

    async def fake_renew(cookies, **_k):
        raise SessionExpired("cookie 已失效")

    monkeypatch.setattr(app_module, "renew_session", fake_renew)
    response = await api.post(f"/admin/accounts/{ACCOUNT}/refresh")
    assert response.status_code == 401, response.text


# ---- 幂等：同一把 key 重放同一个任务，不白建 ----------------------------


@pytest.mark.asyncio
async def test_idempotency_key_replays_same_task_without_leaking(api, store):
    """旧实现先建任务再查键：重复请求会白建一条没人轮询的任务。"""
    headers = {"Idempotency-Key": "test-idem-key-1"}
    first = await api.post("/v1/images/tasks", json={"prompt": "画只猫"}, headers=headers)
    assert first.status_code == 200, first.text
    second = await api.post("/v1/images/tasks", json={"prompt": "画只猫"}, headers=headers)
    assert second.status_code == 200, second.text
    assert first.json()["id"] == second.json()["id"]

    # 队列里只能有这一条（不能因为重复请求多出一条永远没人轮询的任务）。
    listing = (await api.get("/admin/tasks")).json()
    ids = [item["id"] for item in listing["tasks"]]
    assert ids.count(first.json()["id"]) == 1
    assert len(ids) == 1


async def _wait_task(
    api: AsyncClient, task_id: str, states: set[str], *, timeout: float = 2.0
) -> dict:
    """轮询任务直到进入 `states`。后台 runner 是 asyncio task，测试要给它让出时间片。"""
    import asyncio

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    last: dict = {}
    while loop.time() < deadline:
        last = (await api.get(f"/v1/images/tasks/{task_id}")).json()
        if last.get("status") in states:
            return last
        await asyncio.sleep(0.02)
    raise AssertionError(f"任务 {task_id} 未在 {timeout}s 内进入 {states}，最后状态 {last}")


@pytest.mark.asyncio
async def test_async_task_reuses_the_row_it_returned_to_client(api, store, monkeypatch):
    """旧实现两边各建一条：客户端轮询的那条永远 queued，真正干活的那条没人见。

    这里钉住两点：`generate_media` 收到的是**同一个** job_id；任务表里只有一行，
    且它最终能变成 completed（不会多出一条幽灵任务）。
    """
    seen: dict = {}

    async def fake(store_arg, **kwargs):
        seen.update(kwargs)
        jobs.TaskStore(store_arg).finish(
            kwargs["job_id"], state=jobs.JobStatus.COMPLETED.value, media_token=TOKEN
        )
        return {"media_token": TOKEN, "url": f"/v1/media/{TOKEN}"}

    monkeypatch.setattr(pool, "generate_media", fake)
    response = await api.post("/v1/images/tasks", json={"prompt": "画只猫"})
    assert response.status_code == 200, response.text
    task_id = response.json()["id"]

    body = await _wait_task(api, task_id, {"completed"})
    assert seen.get("job_id") == task_id
    assert body["media_token"] == TOKEN
    listing = (await api.get("/admin/tasks")).json()["tasks"]
    assert [item["id"] for item in listing] == [task_id]


@pytest.mark.asyncio
async def test_async_task_marks_failed_when_generation_raises_early(api, store, monkeypatch):
    """generate_media 在写任务表之前就抛（开关关闭/参数非法）时，任务不能永远 queued。"""

    async def boom(*_args, **_kwargs):
        raise pool.GenerationError("生成能力已关闭")

    monkeypatch.setattr(pool, "generate_media", boom)
    response = await api.post("/v1/images/tasks", json={"prompt": "画只猫"})
    assert response.status_code == 200, response.text
    task_id = response.json()["id"]

    body = await _wait_task(api, task_id, {"failed"})
    assert "生成能力已关闭" in (body.get("error") or "")
    listing = (await api.get("/admin/tasks")).json()["tasks"]
    assert [item["id"] for item in listing] == [task_id]


# ---- 管理密钥分离（V-01）+ 对外绝对 URL ----------------------------------


@pytest.mark.asyncio
async def test_admin_keys_are_separate_from_client_keys(api, store, monkeypatch):
    """持普通调用 key 的客户端不能进 /admin：单 key 同守两面就是权限坍缩。"""
    monkeypatch.setenv("MUSE_BRIDGE_API_KEYS", "client-key")
    monkeypatch.setenv("MUSE_BRIDGE_ADMIN_KEYS", "admin-key")

    # 调用面：admin-key 不是调用密钥，应被拒；client-key 可用。
    assert (
        await api.get("/v1/models", headers={"Authorization": "Bearer admin-key"})
    ).status_code == 401
    assert (
        await api.get("/v1/models", headers={"Authorization": "Bearer client-key"})
    ).status_code == 200

    # 管理面：client-key 不是管理密钥，必须 401；admin-key 才通。
    assert (
        await api.get("/admin/tasks", headers={"Authorization": "Bearer client-key"})
    ).status_code == 401
    assert (
        await api.get("/admin/tasks", headers={"Authorization": "Bearer admin-key"})
    ).status_code == 200


@pytest.mark.asyncio
async def test_healthz_reports_admin_key_separation(api, monkeypatch):
    """降级必须可审计：没设 ADMIN_KEYS 时 healthz 要如实说 false。"""
    monkeypatch.setenv("MUSE_BRIDGE_API_KEYS", "client-key")
    monkeypatch.delenv("MUSE_BRIDGE_ADMIN_KEYS", raising=False)
    body = (await api.get("/healthz")).json()
    assert body["admin_key_separated"] is False

    monkeypatch.setenv("MUSE_BRIDGE_ADMIN_KEYS", "admin-key")
    assert (await api.get("/healthz")).json()["admin_key_separated"] is True


@pytest.mark.asyncio
async def test_public_base_makes_media_urls_absolute(api, store, monkeypatch):
    """反代/公网部署：绝对 URL 才是客户端能直接下载的地址。"""
    monkeypatch.setenv("MUSE_PUBLIC_BASE", "https://api.example.com/")
    job = _task_row(store, state=jobs.JobStatus.COMPLETED.value, token=TOKEN)
    body = (await api.get(f"/v1/images/tasks/{job.id}")).json()
    assert body["url"] == f"https://api.example.com/v1/media/{TOKEN}"
    assert body["result"]["url"] == body["url"]

    monkeypatch.delenv("MUSE_PUBLIC_BASE", raising=False)
    body = (await api.get(f"/v1/images/tasks/{job.id}")).json()
    assert body["url"] == f"/v1/media/{TOKEN}"


# ---- 鉴权：新端点不能是后门 --------------------------------------------


@pytest.mark.asyncio
async def test_new_endpoints_require_key_when_configured(monkeypatch, store):
    """配了密钥之后，新加的这些端点必须和 /v1/* 一样过闸门。"""
    monkeypatch.setenv("MUSE_BRIDGE_API_KEYS", "s3cret")
    monkeypatch.setattr(app_module, "store", lambda: store)
    transport = ASGITransport(app=app_module.app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        calls = [
            await client.post("/v1/images/edits", json={"prompt": "x", "images": []}),
            await client.get("/v1/fal/models"),
            await client.post("/v1/fal/muse/image", json={"prompt": "x"}),
            await client.get("/v1/fal/muse/requests/task_x"),
            await client.get("/v1/fal/muse/requests/task_x/status"),
            await client.get("/admin/tasks"),
            await client.get("/admin/tasks/task_x"),
            await client.post("/admin/tasks/clear"),
            await client.get("/admin/accounts/a/quota"),
            await client.post("/admin/accounts/a/keepalive"),
            await client.delete(f"/v1/media/{TOKEN}"),
            await client.post("/admin/accounts/a/cookies", json={"cookies": {"hatch_sess": "x"}}),
        ]
    for response in calls:
        assert response.status_code == 401, (response.request.url, response.text)
