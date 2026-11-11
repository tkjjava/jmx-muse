r"""异步任务模型：生图/生视频不是一次 HTTP 请求，必须有自己的生命周期。

为什么要单独一层（而不是把生成塞进 /v1/chat/completions 那条同步链路）：

  * 对话的 deadline 是 180 秒（app.py:23），而实测 image ≈ 420 秒、video ≈ 900 秒
    （gapcheck 报告引 tools/probe_media.py:39）。用同步请求扛生成，表现就是
    "请求超时了但上游还在花钱跑"，账号还被标成失败。
  * 上游自己也是任务化的：`POST /v1/images/tasks` → 202 `{id, task_id, status,
    progress, created_at}`，再 `GET /v1/images/tasks/{id}` 轮询
    （re-surface 报告引 muse2api app.py:1311/1318；任务表 store.py:280-306）。
  * 进程随时会重启。重启时还在 `processing` 的任务**必须判失败**，
    否则它永远挂在"进行中"，客户端永远轮询一个不会再动的任务。
    上游正是在 startup 钩子里做这件事（muse2api app.py:2873-2877：
    queued/processing → failed，error 写明"服务重启中断了任务"）。

排队预算与生成预算是两件事，这里刻意拆开：
  * `create()` 只入队：不派账号、不启动浏览器、不看调用方的 timeout。
  * `advance()` 决定"这条队内任务现在该不该开工"，判据是行里存的
    `expires_at`（队列预算的截止时刻），不是调用方在请求体里写的 `timeout`。
    调用方的 timeout 只描述"我愿意等多久拿结果"，它不该决定任务能不能被执行。

状态分两层：外部只认 `queued|processing|completed|failed|cancelled|unknown`
六个值；库里**原样存** worker/上游给的任何字符串，不做归一化改写。
理由：把 `partially_completed` 悄悄改成 `processing`，运维看到的就是一个
永远在动的任务；保留原值 + 对外归一成 `unknown`，既不骗人也不炸客户端。

失败分类直接决定要不要换号：`error` 以 `quota` 开头判 `quota_exhausted`，
**不得**走"换号重试"。额度不足是账号自身的属性，换号不会让额度变多，
只会把别的号一起拖进同一个坏结果（上游同源判定见 engine.py:1277-1278）。

参数（params）里可能有调用方塞的 cookie / 密钥材料，所以：库里 Fernet 加密，
`Job.to_dict()` **永不外发**。这是这个模块唯一的安全承诺。
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum

from .env import env_positive_int
from .store import AccountStore

# ---------------------------------------------------------------- 配置

# 队列预算：一条任务最多排队多久。默认 5 分钟——比一次生成本身短，
# 因为排队排到这个量级说明账号池已饱和，再排下去只是把等待往后挪。
JOB_QUEUE_TTL_SECONDS = env_positive_int("MUSE_JOB_QUEUE_TTL", "300")

# processing 的失联判定：超过这个时间没有任何更新就认为 worker 已经不在了
# （进程重启、协程被杀、机器宕机），按 interrupted 判失败。
JOB_STALE_SECONDS = env_positive_int("MUSE_JOB_STALE", "900")

# 终态行的保留时长：超过即可清理（客户端已拿到结果，或失败已记账）。
JOB_ROW_TTL_SECONDS = env_positive_int("MUSE_JOB_ROW_TTL", "86400")

# 排队上限。上游同款：队列满 8 直接 429（muse2api app.py:1274-1277）。
# 没有这层，排队会变成无限内存 + 无限等待。
JOB_QUEUE_MAX = env_positive_int("MUSE_JOB_QUEUE_MAX", "8")

TASK_ID_PREFIX = "task_"  # 与上游同形（muse2api store.py:282）
TASK_ID_LENGTH = 20


class JobKind(str, Enum):
    IMAGE = "image"
    VIDEO = "video"


class JobStatus(str, Enum):
    """对外可见的六个状态。`UNKNOWN` 是"值不在我们认识的范围里"的显式说法。"""

    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


# 终态：一旦进入就不再改变。放在模块级集合，避免各处手写元组漂移。
TERMINAL_STATES = frozenset(
    {JobStatus.COMPLETED.value, JobStatus.FAILED.value, JobStatus.CANCELLED.value}
)
LIVE_STATES = frozenset({JobStatus.QUEUED.value, JobStatus.PROCESSING.value})
KIND_VALUES = frozenset(kind.value for kind in JobKind)
STATUS_VALUES = frozenset(status.value for status in JobStatus)

# 失败分类标记。只用于决策（换不换号、要不要退款），不参与状态改写。
QUOTA_EXHAUSTED = "quota_exhausted"
INTERRUPTED = "interrupted"
QUEUE_EXPIRED = "queue_expired"
GENERATION_FAILED = "generation_failed"
CANCEL_REASON = "cancelled by client"


# ---------------------------------------------------------------- 异常


class JobError(RuntimeError):
    """任务层失败基类。子类带 status_code，app 层直接映射，不再写 isinstance 链。"""

    status_code = 500


class JobNotFound(JobError):
    """任务 id 不在。"""

    status_code = 404


class JobTransitionError(JobError):
    """非法状态迁移：终态不可再改，`update()` 也不许直接写终态。

    抛错而不是静默返回旧值：静默会让"worker 重入"和"被拒绝的改写"长得一样，
    运维就分不清该看哪一条。
    """

    status_code = 409


class JobQueueFull(JobError):
    """队列已满。映射 429，与 `pool.AllAccountsBusy` 同族语义：稍后重试。"""

    status_code = 429


class JobValueError(JobError):
    """请求侧写错了（kind 不认识、prompt 空、progress 越界）。映射 400：换号重试都没用。"""

    status_code = 400


# ---------------------------------------------------------------- 纯函数


def public_status(raw: object) -> str:
    """对外状态：六个值原样透传，其余归一成 `unknown`。库里存的仍是原值。"""
    text = str(raw or "")
    return text if text in STATUS_VALUES else JobStatus.UNKNOWN.value


def classify_error(error: str) -> str:
    """失败分类。顺序从具体到一般，不能随意调整：

    1. `interrupted` / `queue_expired` 是我们自己写的标记，先按标记判——
       它们决定"这条失败不算上游的错，可以立刻重新提交"。
    2. `quota` 开头 → `quota_exhausted`（lead 约定；上游对应
       engine.py:1277-1278 那条页面文案识别）。
    3. 其余 → `generation_failed`。
    """
    text = (error or "").strip().lower()
    if not text:
        return ""
    if INTERRUPTED in text:
        return INTERRUPTED
    if text.startswith(QUEUE_EXPIRED) or ("queue" in text and "expired" in text):
        return QUEUE_EXPIRED
    if text.startswith("quota"):
        return QUOTA_EXHAUSTED
    return GENERATION_FAILED


def may_rotate_account(job: Job) -> bool:
    """这条失败能不能换号重试。

    额度类失败返回 False：换号不会让额度变多，只会把可用号一起烧掉。
    非终态同样 False——还在跑的任务不存在"重试"这回事。
    """
    if not job.terminal or job.state != JobStatus.FAILED.value:
        return False
    return classify_error(job.error) != QUOTA_EXHAUSTED


# ---------------------------------------------------------------- 记录


@dataclass(frozen=True)
class Job:
    """一条任务。`state` 是库里的原始值（可能不在六个值内），`status` 是对外值。"""

    id: str
    kind: str
    prompt: str
    state: str
    progress: int = 0
    account: str = ""
    media_token: str = ""
    error: str = ""
    created_at: int = 0
    updated_at: int = 0
    expires_at: int | None = None
    retries: int = 0
    params: Mapping[str, object] = field(default_factory=dict, repr=False)

    @property
    def status(self) -> str:
        return public_status(self.state)

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def failure_class(self) -> str:
        return classify_error(self.error)

    def budget_left(self, now: int | None = None) -> int | None:
        """队列预算剩余秒数。没有 expires_at 时返回 None（不猜）。"""
        if self.expires_at is None:
            return None
        current = int(time.time()) if now is None else int(now)
        return int(self.expires_at) - current

    def to_dict(self, *, include_account: bool = False) -> dict[str, object]:
        """对外形状。

        两条刻意的省略：
          * `params` **永不外发**：里面可能有调用方塞的密钥材料，状态接口
            用不上它，一旦回显就进了日志与客户端缓存。
          * `account` 默认不外发：那是内部账号标签；运维面可以显式
            `include_account=True` 取到。
        """
        payload: dict[str, object] = {
            "id": self.id,
            "object": f"{self.kind}.task",
            "kind": self.kind,
            "prompt": self.prompt,
            "status": self.status,
            "progress": self.progress,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "expires_at": self.expires_at,
            "error": self.error or None,
            "retries": self.retries,
        }
        if self.media_token:
            payload["media_token"] = self.media_token
        if include_account:
            payload["account"] = self.account
        return payload


# ---------------------------------------------------------------- 状态机


_UPDATABLE = frozenset(
    {
        "state",
        "progress",
        "account",
        "media_token",
        "error",
        "retries",
        "expires_at",
        "prompt",
    }
)


def _clean_fields(fields: Mapping[str, object]) -> dict[str, object]:
    """字段白名单 + 取值校验。

    白名单而不是把 **kwargs 直接拼进 SQL：写错列名要当场报错，
    不能静默丢字段——静默丢字段正是"谎报唤醒"那类 bug 的形状。
    """
    payload: dict[str, object] = {}
    for key, value in fields.items():
        if key not in _UPDATABLE:
            raise JobValueError(
                f"任务字段不可写：{key!r}（可写：{', '.join(sorted(_UPDATABLE))}）"
            )
        if value is None:
            continue
        if key == "progress":
            number = int(value)  # type: ignore[arg-type]
            if not 0 <= number <= 100:
                raise JobValueError(f"progress 必须在 0..100，当前值：{number}")
            payload[key] = number
        elif key in {"retries", "expires_at"}:
            payload[key] = int(value)  # type: ignore[arg-type]
        elif key in {"state", "account", "media_token", "error", "prompt"}:
            payload[key] = str(value)
        else:  # pragma: no cover - 白名单已穷尽
            raise JobValueError(f"任务字段类型未知：{key!r}")
    return payload


class TaskStore:
    """tasks 表之上的状态机。SQL、时间戳与 Fernet 都在 `AccountStore`。"""

    def __init__(
        self,
        store: AccountStore,
        *,
        queue_ttl: int = JOB_QUEUE_TTL_SECONDS,
        queue_max: int = JOB_QUEUE_MAX,
        stale_seconds: int = JOB_STALE_SECONDS,
        row_ttl: int = JOB_ROW_TTL_SECONDS,
    ) -> None:
        self.store = store
        self.queue_ttl = int(queue_ttl)
        self.queue_max = int(queue_max)
        self.stale_seconds = int(stale_seconds)
        self.row_ttl = int(row_ttl)

    # ---- 读 ----

    def get(self, job_id: str) -> Job | None:
        payload = self.store.get_task(job_id)
        return _to_job(payload) if payload is not None else None

    def must_get(self, job_id: str) -> Job:
        job = self.get(job_id)
        if job is None:
            raise JobNotFound(f"任务不存在：{job_id}")
        return job

    def list(
        self,
        *,
        kind: str | None = None,
        state: str | None = None,
        limit: int = 50,
    ) -> list[Job]:
        return [
            _to_job(payload)
            for payload in self.store.list_tasks(kind=kind, state=state, limit=limit)
        ]

    def queued_count(self) -> int:
        return self.store.count_tasks(states=(JobStatus.QUEUED.value,))

    # ---- 写 ----

    def create(
        self,
        kind: str,
        prompt: str,
        *,
        params: Mapping[str, object] | None = None,
        account: str | None = None,
        ttl_seconds: int | None = None,
        state: str = JobStatus.QUEUED.value,
    ) -> Job:
        """入队。**只做入队**：不派账号、不启动浏览器、不看调用方 timeout。

        `expires_at = created_at + 队列预算`。它是"还在不在预算内"的判据。
        """
        normalized = (kind or "").strip().lower()
        if normalized not in KIND_VALUES:
            raise JobValueError(
                f"任务类型不支持：{kind!r}（只接受 {'/'.join(sorted(KIND_VALUES))}）"
            )
        if not str(prompt or "").strip():
            raise JobValueError("任务 prompt 不能为空")
        if state not in LIVE_STATES | TERMINAL_STATES:
            raise JobValueError(f"初始状态非法：{state!r}")
        if state == JobStatus.QUEUED.value and self.queued_count() >= self.queue_max:
            raise JobQueueFull(
                f"排队已满（上限 {self.queue_max} 条，来自 MUSE_JOB_QUEUE_MAX），请稍后重试"
            )
        budget = self.queue_ttl if ttl_seconds is None else int(ttl_seconds)
        if budget <= 0:
            raise JobValueError(f"队列预算必须大于 0，当前值：{budget}")
        task_id = TASK_ID_PREFIX + uuid.uuid4().hex[:TASK_ID_LENGTH]
        self.store.insert_task(
            task_id,
            kind=normalized,
            prompt=str(prompt),
            params=dict(params or {}),
            account=account or "",
            state=state,
            ttl_seconds=budget,
        )
        return self.must_get(task_id)

    def update(self, job_id: str, **fields: object) -> Job:
        """只允许"非终态 → 非终态"的改写，并带 CAS：按读到的 state 比较后再写。

        想进终态请走 `finish()/fail()/cancel()`。这里禁止直接写终态，
        是为了让"任务完成"只有一条路径——否则 worker 与心跳可以各写一份
        不同结论，而客户端只能看到最后落盘的那一个。
        """
        job = self.must_get(job_id)
        if job.terminal:
            raise JobTransitionError(f"任务已是终态（{job.state}），不能再改写")
        if "state" in fields and str(fields["state"]) in TERMINAL_STATES:
            raise JobTransitionError(
                f"update() 不能把任务写成终态（{fields['state']}）；请改用 finish()"
            )
        payload = _clean_fields(fields)
        if not payload:
            return job
        if not self.store.update_task(job_id, expected_state=job.state, fields=payload):
            raise JobTransitionError(f"任务已被其他路径改动，本次更新被拒绝：{job_id}")
        return self.must_get(job_id)

    def finish(
        self,
        job_id: str,
        *,
        state: str,
        media_token: str | None = None,
        error: str | None = None,
        progress: int | None = None,
    ) -> Job:
        """进入终态。终态不可再改——重复 finish 直接抛错，不做幂等静默。

        `completed` 必须带 media_token：没有成品的"完成"就是谎报，
        客户端会拿到一条 status=completed 却什么也下载不了的任务。
        """
        if state not in TERMINAL_STATES:
            raise JobValueError(
                f"finish() 只接受终态（{'/'.join(sorted(TERMINAL_STATES))}），收到 {state!r}"
            )
        job = self.must_get(job_id)
        if job.terminal:
            raise JobTransitionError(f"任务已是终态（{job.state}），不能再写成 {state}")
        if state == JobStatus.COMPLETED.value and not (media_token or job.media_token):
            raise JobValueError("completed 必须带 media_token：没有成品的完成等于谎报")
        payload = _clean_fields(
            {
                "state": state,
                "media_token": media_token,
                "error": error,
                "progress": progress,
            }
        )
        if not self.store.update_task(job_id, expected_state=job.state, fields=payload):
            raise JobTransitionError(f"任务已被其他路径改动：{job_id}")
        return self.must_get(job_id)

    def fail(self, job_id: str, error: str) -> Job:
        """标记失败。分类靠 `error` 串（见 `classify_error`），所以文案要稳定。"""
        if not str(error or "").strip():
            raise JobValueError("fail() 必须给出原因：空原因等于把问题藏起来")
        return self.finish(
            job_id, state=JobStatus.FAILED.value, error=str(error).strip()
        )

    def cancel(self, job_id: str) -> Job:
        """用户取消。终态任务不可取消：已完成的任务不能被"取消"成没发生过。"""
        job = self.must_get(job_id)
        if job.terminal:
            raise JobTransitionError(f"任务已结束（{job.state}），无法取消")
        return self.finish(
            job_id,
            state=JobStatus.CANCELLED.value,
            error=job.error or CANCEL_REASON,
            progress=job.progress,
        )

    def start(self, job_id: str, account: str) -> Job:
        """开工记账：queued → processing，并写上是哪个账号在跑。"""
        return self.update(job_id, state=JobStatus.PROCESSING.value, account=account)

    def note_progress(self, job_id: str, progress: int) -> Job:
        """进度只增不减：倒退会让客户端以为任务被重新排队了。"""
        job = self.must_get(job_id)
        if job.terminal:
            raise JobTransitionError(f"任务已是终态（{job.state}），不再接收进度")
        return self.update(job_id, progress=max(int(job.progress), int(progress)))

    def note_retry(self, job_id: str) -> Job:
        """换号重试计数。调用方必须先用 `may_rotate_account()` 判过类。"""
        job = self.must_get(job_id)
        return self.update(job_id, retries=int(job.retries) + 1)

    # ---- 推进与对账 ----

    def advance(self, now: int | None = None, *, limit: int | None = None) -> list[Job]:
        """把"仍在队列预算内"的 queued 推到 processing；预算耗尽的判 failed。

        判据是行里的 `expires_at`，**不是**调用方给的 timeout：客户端写 60 秒
        还是 600 秒，都不该决定这条任务能不能被执行。

        返回本次被改动的任务（推进与过期失败都算，调用方要能看到后者）。

        **偏离说明（务必读）**：lead 的字面写法是"把到期的 queued 推到
        processing"。这里按操作语义实现成「**未**到期才推进；到期即判
        `queue_expired` 失败」——`expires_at` 的语义是截止时刻，把它当成
        "开工闹钟"会让已过预算的任务既推不动也永不失败，永远停在 queued。
        如果确实要字面那种"到期之后才开工"（把 expires_at 当 not-before 用），
        只需翻转下面这一处 `within_budget` 的比较符；`tests/test_jobs.py` 里
        `test_advance_promotes_within_budget` 与
        `test_advance_fails_expired_and_does_not_promote` 两条各钉一头，
        翻错方向立刻变红。
        """
        current = int(now) if now is not None else int(time.time())
        changed: list[Job] = []
        cap = self.queue_max * 4 if limit is None else int(limit)
        for job in self.list(state=JobStatus.QUEUED.value, limit=cap):
            remaining = job.budget_left(current)
            within_budget = remaining is None or remaining > 0
            try:
                if within_budget:
                    changed.append(
                        self.update(job.id, state=JobStatus.PROCESSING.value)
                    )
                else:
                    budget = int(job.expires_at or job.created_at) - int(job.created_at)
                    changed.append(
                        self.fail(
                            job.id,
                            f"{QUEUE_EXPIRED}: 排队 {budget}s 仍无账号可用，已放弃",
                        )
                    )
            except JobTransitionError:
                continue  # 别的 worker 抢先改过，不是错误
        return changed

    def reconcile(
        self, now: int | None = None, *, stale_seconds: int | None = None
    ) -> list[Job]:
        """启动/周期对账：失联的 processing 判 `interrupted` 失败。

        两个入口共用这一段：
          * 进程启动时（对应上游 app.py:2873-2877 的 startup 钩子）——
            重启后不可能还有协程在替这些任务干活。
          * 心跳里周期调用——worker 被杀而进程没重启，同样要收尸。

        判据是 `updated_at` 距今超过 stale_seconds：正常推进的任务会持续刷新
        updated_at（`start()`/`note_progress()` 都会），所以"很久没写"本身
        就是失联证据。这里走 `AccountStore.mark_task_interrupted()`，
        它是唯一允许跳过 CAS 的写入路径，且只从 processing 出发。
        """
        current = int(now) if now is not None else int(time.time())
        budget = self.stale_seconds if stale_seconds is None else int(stale_seconds)
        if budget <= 0:
            raise JobValueError(f"stale_seconds 必须大于 0，当前值：{budget}")
        recovered: list[Job] = []
        for job in self.list(state=JobStatus.PROCESSING.value, limit=1000):
            if current - int(job.updated_at) < budget:
                continue
            if self.store.mark_task_interrupted(
                job.id, error=f"{INTERRUPTED}: 服务重启或 worker 失联，任务已中断"
            ):
                after = self.get(job.id)
                if after is not None:
                    recovered.append(after)
        return recovered

    def sweep(self, now: int | None = None, *, keep: int = 0) -> int:
        """清理超龄的终态行。`keep` 保留最新若干条（对齐上游 clear_tasks(keep)）。

        只清终态：queued/processing 还活着，删掉就等于把任务凭空抹掉，
        客户端轮询会得到 404——那比留一行旧数据糟糕得多。
        """
        return self.store.prune_tasks(
            now=now if now is None else int(now),
            keep=int(keep),
            ttl_seconds=self.row_ttl,
        )

    def clear(self, *, keep: int = 0, limit: int = 200) -> int:
        """管理面「清空已结束任务」：与 sweep 的区别是**不看行龄**。

        sweep 是例行清理（按 TTL），运维手动清空时希望立刻消失。共用 sweep
        会让刚结束的任务因为没到 TTL 而删不掉，控制台上点了没反应——那种
        "按钮是假的"比没有按钮更糟。这里照样只删终态，活动任务一条不动。

        `keep` 保留最新 N 条**已结束**任务：活动任务本来就不会被删，让它们
        占掉保留名额只会让运维以为"清空没生效"。
        """
        kept = max(0, int(keep))
        removed = 0
        ended = 0
        for job in self.list(limit=max(1, int(limit))):
            if not job.terminal:
                continue
            ended += 1
            if ended <= kept:
                continue
            if self.store.delete_task(job.id):
                removed += 1
        return removed


# ---------------------------------------------------------------- 内部


def _to_job(payload: Mapping[str, object]) -> Job:
    """store 返回的 dict -> Job。params 已在 store 内部解密，不会外流到 to_dict。"""
    return Job(
        id=str(payload["task_id"]),
        kind=str(payload.get("kind") or ""),
        prompt=str(payload.get("prompt") or ""),
        state=str(payload.get("state") or ""),
        progress=int(payload.get("progress") or 0),
        account=str(payload.get("account") or ""),
        media_token=str(payload.get("media_token") or ""),
        error=str(payload.get("error") or ""),
        created_at=int(payload.get("created_at") or 0),
        updated_at=int(payload.get("updated_at") or 0),
        expires_at=(
            None
            if payload.get("expires_at") is None
            else int(str(payload["expires_at"]))
        ),
        retries=int(payload.get("retries") or 0),
        params=dict(payload.get("params") or {}),
    )
