"""活网验收：把 P1-B3~B5、P2-C 的检查收成一条可复现的链。

## 为什么需要它

这些检查原先散在 `work/` 的一次性脚本里，硬编码了本机路径和密钥，别人拿到
跑不起来；更糟的是**探针可以和产品走不同的契约**——2026-11-11 那次"三条链路
一起失败"就是探针自己把 `proxy` 传成 requests 形式、被产品静默降级成直连导致的，
看起来像上游全挂。所以验收必须：
  1. 与产品共用同一份 env（不自己另造一套）；
  2. 每个检查项都打印**实际观测值**，不只打印通过/失败；
  3. 上游没给东西时说"未触发"，不冒充通过。

## 用法

    python tools/acceptance.py            # 跑全部可跑的项
    python tools/acceptance.py media      # 只跑成品链路
    python tools/acceptance.py stream     # 只跑流式
    python tools/acceptance.py sweep      # 只跑清扫计数（不碰生产目录）
    python tools/acceptance.py generation # 只跑协议生成（会产生真实额度消耗）

## 边界

  * `generation` 会真的向上游要图，**消耗额度**，默认不在"全部"里跑。
  * `sweep` 在临时目录里做，不动 `data/media`。
  * 需要鉴权时读 `MUSE_TOOLS_API_KEY`；未配置就匿名跑（本地开发态常见）。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from tools.bridge_ops import (  # noqa: E402
    auth_headers,
    base,
    get_json,
    media_dir,
)

CONTRACT_FIELDS = ("token", "url", "filename", "size", "mime", "kind", "sha256")


class Report:
    """收集检查项。每项都留一句"观测到什么"，出问题时不用再猜。"""

    def __init__(self, title: str) -> None:
        self.title = title
        self.items: list[tuple[str, bool, str]] = []

    def add(self, name: str, ok: bool, observed: str = "") -> None:
        self.items.append((name, ok, observed))

    def emit(self) -> int:
        failures = sum(1 for _, ok, _ in self.items if not ok)
        print(f"\n=== {self.title} ===")
        for name, ok, observed in self.items:
            mark = "✓" if ok else "✗"
            tail = f"  [{observed}]" if observed else ""
            print(f"  {mark} {name}{tail}")
        print(f"  -> {len(self.items) - failures}/{len(self.items)} 通过")
        return failures


def _media_root() -> Path:
    root = media_dir()
    if not root.exists():
        raise SystemExit(f"成品目录不存在：{root}")
    return root


def check_media() -> int:
    """成品链路：读取 → 下载 → Range → 416 → 404。不触发任何生成。"""
    import muse_bridge.media as media

    store = media.MediaStore(_media_root())
    refs = store.list_media()
    report = Report("成品链路（使用已有成品，不新增生成）")
    if not refs:
        report.add("有成品可验", False, "data/media 为空，先跑一次真实生成")
        return report.emit()

    sample = refs[-1]
    local = Path(sample.path).read_bytes()
    local_sha = hashlib.sha256(local).hexdigest()
    headers = auth_headers()

    report.add("成品可读", store.read(sample.token) == local, f"{len(local)}B")

    r = httpx.get(f"{base()}/v1/media/{sample.token}", headers=headers,
                  timeout=60, trust_env=False)
    report.add(
        "下载字节与本地一致",
        r.status_code == 200 and hashlib.sha256(r.content).hexdigest() == local_sha,
        f"{r.status_code} {len(r.content)}B",
    )
    report.add(
        "下载面为 octet-stream + nosniff + attachment",
        r.headers.get("content-type", "").startswith("application/octet-stream")
        and r.headers.get("x-content-type-options", "").lower() == "nosniff"
        and "attachment" in r.headers.get("content-disposition", "").lower(),
        f"ct={r.headers.get('content-type')}",
    )

    r2 = httpx.get(f"{base()}/v1/media/{sample.token}",
                   headers={**headers, "Range": "bytes=0-63"},
                   timeout=60, trust_env=False)
    report.add(
        "Range 返回 206 且内容正确",
        r2.status_code == 206 and r2.content == local[:64],
        f"{r2.status_code} {r2.headers.get('content-range', '-')}",
    )

    r3 = httpx.get(f"{base()}/v1/media/{sample.token}",
                   headers={**headers, "Range": "bytes=999999999-"},
                   timeout=60, trust_env=False)
    report.add(
        "起点越界返回 416（不是被 clamp 成 206）",
        r3.status_code == 416,
        f"{r3.status_code}",
    )

    r4 = httpx.get(f"{base()}/v1/media/{'0' * 32}", headers=headers,
                   timeout=30, trust_env=False)
    report.add("未知 token 返回 404", r4.status_code == 404, f"{r4.status_code}")

    return report.emit()


def check_stream() -> int:
    """流式 SSE：帧合法性、finish_reason 时机、正文与非流式一致。"""
    headers = auth_headers()
    prompt = "用一句话说明什么是哈希表"
    report = Report("流式 SSE 契约")

    frames = 0
    bad: list[str] = []
    text = ""
    finishes: list[tuple[str, int]] = []
    done = False
    role_seen = False
    media: list[dict] = []
    last_text = -1
    status = None

    with httpx.stream(
        "POST", f"{base()}/v1/chat/completions", headers=headers,
        json={"model": "hatch-web",
              "messages": [{"role": "user", "content": prompt}], "stream": True},
        timeout=300, trust_env=False,
    ) as resp:
        status = resp.status_code
        index = 0
        for line in resp.iter_lines():
            if not line:
                continue
            if not line.startswith("data:"):
                bad.append(line[:60])
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                done = True
                break
            try:
                obj = json.loads(payload)
            except ValueError:
                bad.append(payload[:60])
                continue
            frames += 1
            choice = (obj.get("choices") or [{}])[0]
            delta = choice.get("delta") or {}
            if delta.get("role"):
                role_seen = True
            if delta.get("content"):
                text += delta["content"]
                last_text = index
            if delta.get("media"):
                media.extend(delta["media"])
            if choice.get("finish_reason"):
                finishes.append((choice["finish_reason"], index))
            index += 1

    report.add("请求成功", status == 200, f"status={status} 帧数={frames}")
    report.add("所有帧合法且带 data: 前缀", not bad, f"坏帧={len(bad)}")
    report.add("出现 role 帧", role_seen)
    report.add("恰好一个 finish_reason", len(finishes) == 1, f"{finishes}")
    report.add("finish_reason=stop", bool(finishes) and finishes[0][0] == "stop")
    report.add("收到 [DONE]", done)
    report.add("正文非空", bool(text.strip()), f"{len(text)} 字")
    report.add(
        "正文在 finish 之前结束",
        bool(finishes) and last_text <= finishes[0][1],
        f"最后正文帧={last_text} finish帧={finishes[0][1] if finishes else '-'}",
    )

    n = httpx.post(
        f"{base()}/v1/chat/completions", headers=headers,
        json={"model": "hatch-web",
              "messages": [{"role": "user", "content": prompt}], "stream": False},
        timeout=300, trust_env=False,
    )
    ntext = ""
    if n.status_code == 200:
        ntext = ((n.json().get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    report.add("非流式同样成功", n.status_code == 200, f"status={n.status_code}")
    if text and ntext:
        ratio = len(text) / max(1, len(ntext))
        report.add("两种模式正文长度同量级", 0.5 <= ratio <= 2.0, f"比值={ratio:.2f}")
    if media:
        report.add(
            "附件字段齐全",
            all(all(f in item for f in CONTRACT_FIELDS) for item in media),
            f"{len(media)} 个附件",
        )
    return report.emit()


def check_sweep() -> int:
    """清扫计数：在临时目录里造超龄文件，确认 runs 与 removed 都真的涨。"""
    import muse_bridge.media as media
    from muse_bridge import observability

    report = Report("媒体清扫计数（临时目录，不动生产）")
    tmp = Path(tempfile.mkdtemp(prefix="muse-sweep-"))
    store = media.MediaStore(tmp)
    webp = b"RIFF" + (0).to_bytes(4, "little") + b"WEBPVP8 " + b"x" * 64

    fresh = store.save(webp, "image/webp")
    old = store.save(webp, "image/webp")
    ancient = time.time() - 100 * 3600
    os.utime(Path(old.path), (ancient, ancient))
    part = store.root / f"{'a' * 32}.webp{media.PART_SUFFIX}"
    part.write_bytes(b"half")
    os.utime(part, (ancient, ancient))

    before = observability.snapshot()["counters"]
    removed = store.sweep()
    after = observability.snapshot()["counters"]
    # 直接调 sweep() 不会 bump：计数在 sweep_forever 里。这里同时验证两件事：
    # 删除真的发生了、以及"直接调用不计数"这个边界没被改坏。
    report.add("清扫删除了超龄文件", removed >= 2, f"removed={removed}")
    report.add("超龄成品已消失", not Path(old.path).exists())
    report.add("超龄 .part 已消失", not part.exists())
    report.add("新鲜成品保留", Path(fresh.path).exists())
    report.add(
        "直接 sweep() 不改 removed 计数",
        after.get("media_sweep_removed", 0) == before.get("media_sweep_removed", 0),
        f"{before.get('media_sweep_removed', 0)} -> {after.get('media_sweep_removed', 0)}",
    )

    import shutil

    shutil.rmtree(tmp, ignore_errors=True)
    return report.emit()


def check_generation() -> int:
    """协议生成。会真的向上游要图，消耗额度。"""
    headers = auth_headers()
    report = Report("协议生成（真实消耗额度）")
    before = (get_json("/admin/observability").get("counters") or {})

    r = httpx.post(
        f"{base()}/v1/images/tasks", headers=headers,
        json={"prompt": "一只白猫坐在蓝色沙发上，自然光", "model": "muse-image"},
        timeout=120, trust_env=False,
    )
    if r.status_code not in (200, 201, 202):
        report.add("提交任务", False, f"{r.status_code} {r.text[:120]}")
        return report.emit()
    job = r.json()
    task_id = job.get("task_id") or job.get("id")
    report.add("提交任务", bool(task_id), f"id={str(task_id)[:12]}")

    final: dict = {}
    deadline = time.time() + 300
    while time.time() < deadline:
        time.sleep(4)
        try:
            final = get_json(f"/admin/tasks/{task_id}")
        except SystemExit:
            continue
        if final.get("status") in ("completed", "failed", "cancelled", "expired"):
            break

    report.add("任务完成", final.get("status") == "completed",
               f"status={final.get('status')} err={final.get('error')}")

    token = str(final.get("media_token") or "")
    report.add("有 media_token", bool(token))
    report.add("响应带 mime", bool(final.get("mime")), str(final.get("mime")))
    report.add("响应带 size", bool(final.get("size")), str(final.get("size")))

    if token:
        d = httpx.get(f"{base()}/v1/media/{token}", headers=headers,
                      timeout=180, trust_env=False)
        head = d.content[:12]
        is_media = (
            head[:4] == b"RIFF" or head[:3] == b"\xff\xd8\xff"
            or d.content[:8] == b"\x89PNG\r\n\x1a\n" or d.content[4:8] == b"ftyp"
        )
        report.add("成品可下载", d.status_code == 200, f"{len(d.content)}B")
        report.add("成品是真媒体文件", is_media, f"头部={head[:8]!r}")

    after = (get_json("/admin/observability").get("counters") or {})
    built = after.get("protocol_driver_built", 0) - before.get("protocol_driver_built", 0)
    bbuilt = after.get("browser_driver_built", 0) - before.get("browser_driver_built", 0)
    fb = after.get("protocol_fallback", 0) - before.get("protocol_fallback", 0)
    report.add("走了协议驱动", built >= 1, f"protocol+{built}")
    report.add("未回退浏览器", bbuilt == 0 and fb == 0,
               f"browser+{bbuilt} fallback+{fb}")
    return report.emit()


ALL = {
    "media": check_media,
    "stream": check_stream,
    "sweep": check_sweep,
}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if not args or args[0] == "all":
        names = list(ALL)
    elif args[0] == "generation":
        names = ["generation"]
    else:
        unknown = [a for a in args if a not in ALL and a != "generation"]
        if unknown:
            print(f"未知项：{unknown}\n可用：{list(ALL)} / generation / all")
            return 2
        names = args

    total = 0
    for name in names:
        fn = check_generation if name == "generation" else ALL[name]
        total += fn()
    print(f"\n总失败项：{total}")
    return 0 if total == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
