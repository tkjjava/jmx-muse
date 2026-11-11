"""异步任务状态机：排队/推进/终态不可改/重启收尸/额度类失败不换号。

全部离线，不启动浏览器。时钟按 tests/test_cooldown_clock.py 的写法冻结
（`monkeypatch.setattr(store_mod, "time", ...)`）：任务的时间戳全部由
`AccountStore` 生成，所以冻住那一处就冻住了整条链。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from muse_bridge import jobs as jobs_mod
from muse_bridge import store as store_mod
from muse_bridge.jobs import (
    INTERRUPTED,
    QUOTA_EXHAUSTED,
    Job,
    JobQueueFull,
    JobTransitionError,
    JobValueError,
    TaskStore,
    classify_error,
    may_rotate_account,
    public_status,
)
from muse_bridge.store import AccountStore

T0 = 1_800_000_000
SECRET = "hunter2-cookie-value"


@pytest.fixture
def bridge_key(monkeypatch):
    monkeypatch.setenv("MUSE_BRIDGE_KEY", Fernet.generate_key().decode())


@pytest.fixture
def clock(monkeypatch):
    value = [T0]
    monkeypatch.setattr(store_mod, "time", SimpleNamespace(time=lambda: value[0]))
    return value


@pytest.fixture
def store(bridge_key, clock, tmp_path):
    instance = AccountStore(tmp_path / "jobs.db")
    yield instance
    instance.close()


@pytest.fixture
def tasks(store):
    return TaskStore(store, queue_ttl=300, queue_max=3, stale_seconds=900, row_ttl=3600)


# ---- 入队 ----


def test_create_queues_and_exposes_no_secrets(tasks, store):
    job = tasks.create(
        "image", "一只柴犬", params={"cookie": SECRET, "size": "2K"}, account="主号"
    )
    assert job.state == "queued" and job.status == "queued"
    assert job.id.startswith("task_")
    assert job.created_at == T0 and job.updated_at == T0
    assert job.expires_at == T0 + 300, "expires_at 是队列预算截止时刻"
    assert job.params["cookie"] == SECRET, "进程内要能用"

    payload = job.to_dict()
    assert "params" not in payload, "params 永不外发"
    assert SECRET not in str(payload)
    assert "account" not in payload, "账号标签默认不外发"
    assert job.to_dict(include_account=True)["account"] == "主号"
    assert payload["object"] == "image.task" and payload["kind"] == "image"

    # 落盘必须是密文（与 accounts 的 profile_enc 同一把 Fernet）
    assert SECRET.encode() not in (store.path).read_bytes()


def test_create_validates_the_request(tasks):
    with pytest.raises(JobValueError):
        tasks.create("audio", "不支持的类型")
    with pytest.raises(JobValueError):
        tasks.create("image", "   ")
    with pytest.raises(JobValueError):
        tasks.create("image", "x", ttl_seconds=0)


def test_queue_is_bounded(tasks):
    """队列满要当场拒绝：无限排队等于把等待与内存都推给下一次故障。"""
    for index in range(3):
        tasks.create("image", f"第 {index} 条")
    with pytest.raises(JobQueueFull):
        tasks.create("image", "第四条")


def test_kind_is_case_insensitive(tasks):
    assert tasks.create("VIDEO", "跑起来").kind == "video"


# ---- 推进：排队预算与生成预算分开 ----


def test_advance_promotes_within_budget(tasks):
    job = tasks.create("video", "五秒镜头")
    changed = tasks.advance(now=T0 + 10)
    assert [item.id for item in changed] == [job.id]
    after = tasks.must_get(job.id)
    assert after.state == "processing"
    assert after.expires_at == T0 + 300, "推进不改预算"


def test_advance_fails_expired_and_does_not_promote(tasks):
    """预算耗尽的 queued 判失败，而不是继续挂在队列里等一个不会来的账号。"""
    job = tasks.create("image", "等太久的那条")
    changed = tasks.advance(now=T0 + 400)
    assert [item.id for item in changed] == [job.id]
    after = tasks.must_get(job.id)
    assert after.state == "failed" and after.status == "failed"
    assert classify_error(after.error) == "queue_expired"
    assert after.state != "processing", "过期任务不得被开工"


def test_advance_ignores_non_queued(tasks):
    job = tasks.create("image", "已经完成的")
    tasks.finish(job.id, state="completed", media_token="0" * 32, progress=100)
    assert tasks.advance(now=T0 + 10) == []


def test_create_does_not_touch_generation_timeout(tasks):
    """create 只排队：调用方的 timeout 不参与任何判据（生成预算另说）。"""
    job = tasks.create("video", "慢活", params={"timeout": 1}, ttl_seconds=999)
    assert job.state == "queued" and job.expires_at == T0 + 999


# ---- 状态迁移闸门 ----


def test_terminal_rows_are_immutable(tasks):
    job = tasks.create("image", "已完成")
    done = tasks.finish(job.id, state="completed", media_token="a" * 32, progress=100)
    assert done.terminal
    with pytest.raises(JobTransitionError):
        tasks.update(done.id, progress=50)
    with pytest.raises(JobTransitionError):
        tasks.finish(done.id, state="failed", error="再来一次")
    with pytest.raises(JobTransitionError):
        tasks.cancel(done.id)


def test_update_cannot_write_terminal_state(tasks):
    """进终态只有 finish() 一条路：否则 worker 与心跳可以各写一份结论。"""
    job = tasks.create("image", "想直接宣布完成")
    with pytest.raises(JobTransitionError):
        tasks.update(job.id, state="completed", media_token="b" * 32)
    assert tasks.must_get(job.id).state == "queued"


def test_completed_must_carry_a_media_token(tasks):
    """没有成品的"完成"就是谎报。"""
    job = tasks.create("image", "假装成功")
    with pytest.raises(JobValueError):
        tasks.finish(job.id, state="completed")
    assert tasks.must_get(job.id).state == "queued"


def test_update_rejects_unknown_fields_and_bad_progress(tasks):
    job = tasks.create("image", "写错列名")
    with pytest.raises(JobValueError):
        tasks.update(job.id, state2="processing")
    with pytest.raises(JobValueError):
        tasks.update(job.id, progress=101)
    with pytest.raises(JobValueError):
        tasks.fail(job.id, "   ")  # 空原因等于把问题藏起来


def test_cas_conflict_is_reported(tasks, store, monkeypatch):
    """CAS 失败必须抛错，不能静默返回旧值。"""
    job = tasks.create("image", "两个 worker 抢")
    monkeypatch.setattr(store, "update_task", lambda *a, **k: False)
    with pytest.raises(JobTransitionError):
        tasks.update(job.id, state="processing")


def test_progress_never_goes_backwards(tasks):
    job = tasks.create("video", "五秒镜头")
    tasks.start(job.id, "主号")
    assert tasks.note_progress(job.id, 60).progress == 60
    assert tasks.note_progress(job.id, 30).progress == 60, "倒退会让客户端以为重新排队"


def test_cancel_keeps_a_reason(tasks):
    job = tasks.create("image", "用户反悔了")
    cancelled = tasks.cancel(job.id)
    assert cancelled.state == "cancelled" and cancelled.error
    assert cancelled.status == "cancelled"


# ---- 重启收尸 ----


def test_reconcile_marks_stale_processing_interrupted(tasks):
    job = tasks.create("video", "跑到一半进程没了")
    tasks.start(job.id, "主号")
    recovered = tasks.reconcile(now=T0 + 1000)
    assert [item.id for item in recovered] == [job.id]
    after = tasks.must_get(job.id)
    assert after.state == "failed"
    assert INTERRUPTED in after.error, "必须显式标 interrupted，否则运维看不出是重启"
    assert after.failure_class == INTERRUPTED


def test_reconcile_leaves_live_processing_tasks_alone(tasks):
    job = tasks.create("video", "还在跑")
    tasks.start(job.id, "主号")
    assert tasks.reconcile(now=T0 + 100) == []
    assert tasks.must_get(job.id).state == "processing"


def test_reconcile_never_touches_queued_or_terminal(tasks):
    queued = tasks.create("image", "还在排队")
    done = tasks.create("image", "已完成")
    tasks.finish(done.id, state="completed", media_token="c" * 32)
    assert tasks.reconcile(now=T0 + 10_000) == []
    assert tasks.must_get(queued.id).state == "queued"
    assert tasks.must_get(done.id).state == "completed"


def test_reconcile_rejects_non_positive_window(tasks):
    with pytest.raises(JobValueError):
        tasks.reconcile(now=T0, stale_seconds=0)


# ---- 失败分类与换号 ----


def test_quota_failure_does_not_rotate_account(tasks):
    """额度不足是账号自身的属性：换号只会把别的号一起烧掉。"""
    job = tasks.create("image", "额度没了")
    failed = tasks.fail(job.id, "quota exhausted: weekly limit reached")
    assert failed.failure_class == QUOTA_EXHAUSTED
    assert may_rotate_account(failed) is False


def test_ordinary_failure_may_rotate_account(tasks):
    job = tasks.create("image", "上游抖了")
    failed = tasks.fail(job.id, "upstream timeout after 420s")
    assert failed.failure_class == "generation_failed"
    assert may_rotate_account(failed) is True


@pytest.mark.parametrize("job_state", ["queued", "processing"])
def test_live_tasks_are_not_retryable(tasks, job_state):
    job = tasks.create("image", "还在跑")
    if job_state == "processing":
        tasks.start(job.id, "主号")
    assert may_rotate_account(tasks.must_get(job.id)) is False


def test_retry_counter_is_separate_from_state(tasks):
    job = tasks.create("image", "换过一次号")
    assert tasks.note_retry(job.id).retries == 1
    assert tasks.note_retry(job.id).retries == 2
    assert tasks.must_get(job.id).state == "queued"


@pytest.mark.parametrize(
    "error,expected",
    [
        ("", ""),
        ("quota exhausted", QUOTA_EXHAUSTED),
        ("Quota 上限", QUOTA_EXHAUSTED),
        ("interrupted: 服务重启", INTERRUPTED),
        ("服务重启或 worker 失联，任务已中断 interrupted", INTERRUPTED),
        ("queue_expired: 排队 300s", "queue_expired"),
        ("upstream refused", "generation_failed"),
    ],
)
def test_classify_error_table(error, expected):
    assert classify_error(error) == expected


# ---- 状态词表 ----


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("queued", "queued"),
        ("processing", "processing"),
        ("completed", "completed"),
        ("failed", "failed"),
        ("cancelled", "cancelled"),
        ("partially_completed", "unknown"),
        ("", "unknown"),
        (None, "unknown"),
    ],
)
def test_public_status_table(raw, expected):
    assert public_status(raw) == expected


def test_raw_state_is_stored_verbatim(tasks, store):
    """外部归一、内部原样：把上游状态改写掉，运维就再也看不到真相了。"""
    job = tasks.create("image", "上游给了个怪状态")
    store.update_task(job.id, expected_state="queued", fields={"state": "stalled"})
    row = store.get_task(job.id)
    assert row["state"] == "stalled"
    fresh = tasks.must_get(job.id)
    assert fresh.state == "stalled" and fresh.status == "unknown"
    assert fresh.to_dict()["status"] == "unknown"


def test_list_filters_and_prune_keeps_live_rows(tasks):
    a = tasks.create("image", "甲")
    b = tasks.create("video", "乙")
    tasks.finish(b.id, state="completed", media_token="d" * 32)
    assert {item.id for item in tasks.list(kind="image")} == {a.id}
    assert {item.id for item in tasks.list(state="completed")} == {b.id}
    assert {item.id for item in tasks.list(limit=1)} == {b.id}  # 新的在前
    assert tasks.queued_count() == 1

    # 只清超龄终态：queued 还活着，删了客户端就 404
    assert tasks.sweep(now=T0 + 100) == 0
    assert tasks.sweep(now=T0 + 3600) == 1
    assert tasks.must_get(a.id).state == "queued"


def test_defaults_come_from_env_parsed_constants(store):
    """模块级常量必须走 env.py 的解析器（与 test_env.py:59 同一类钉子）。"""
    assert jobs_mod.JOB_QUEUE_TTL_SECONDS > 0
    assert jobs_mod.JOB_STALE_SECONDS > 0
    assert jobs_mod.JOB_QUEUE_MAX > 0
    plain = TaskStore(store)
    assert plain.queue_ttl == jobs_mod.JOB_QUEUE_TTL_SECONDS
    assert isinstance(Job(id="task_x", kind="image", prompt="p", state="queued"), Job)
