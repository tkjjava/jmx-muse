"""视频端到端验收：提交 → 轮询 → 下载 → 校验真实视频。

为什么单独一个脚本：视频是 600 s 级的任务，混进 `acceptance.py` 会让整套验收
变成"跑一次二十分钟"。这里只做视频这一条链路，其余项见 `acceptance.py`。

## 检查什么

  * 任务真的推进到 completed（不是 queued 卡死）
  * `media_token` 非空、能下载
  * 成品是**真视频容器**（ftyp/webm/RIFF-AVI），不是错误页也不是图片
  * 走的是协议驱动，没有回退浏览器
  * 任务响应带 mime/size/kind，且 kind == "video"

## 用法

    python tools/accept_video.py "一只猫在草地上奔跑，镜头缓慢推进"
    python tools/accept_video.py --timeout 700 "..."

会**真实消耗上游额度**（视频比图片贵很多），默认超时 700 s。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from tools.bridge_ops import auth_headers, base, get_json  # noqa: E402

VIDEO_MAGIC = (b"ftyp", b"\x1aE\xdf\xa3", b"WEBM", b"RIFF", b"AVI ")


def is_video(head: bytes) -> tuple[bool, str]:
    """按文件头判容器。返回 (是否视频, 命中说明)。"""
    if head[4:8] == b"ftyp":
        return True, f"MP4 家族 brand={head[8:12]!r}"
    if head[:4] == b"\x1aE\xdf\xa3":
        return True, "Matroska/WebM"
    if head[:4] == b"RIFF":
        return True, f"RIFF 容器，form={head[8:12]!r}"
    if head[:4] == b"AVI ":
        return True, "AVI"
    return False, f"未知头 {head[:12]!r}"


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    timeout = 700.0
    if args and args[0] == "--timeout":
        timeout = float(args[1])
        args = args[2:]
    prompt = args[0] if args else "一只猫在草地上奔跑，镜头缓慢推进"

    checks: list[tuple[str, bool, str]] = []

    def add(name: str, ok: bool, observed: str = "") -> None:
        checks.append((name, ok, observed))

    before = get_json("/admin/observability").get("counters") or {}
    print(f"提示词：{prompt}")

    resp = httpx.post(
        f"{base()}/v1/videos", headers=auth_headers(),
        json={"prompt": prompt, "model": "muse-video", "timeout": timeout},
        timeout=120, trust_env=False,
    )
    if resp.status_code >= 300:
        add("提交任务", False, f"{resp.status_code} {resp.text[:150]}")
        return report(checks)

    job = resp.json()
    task_id = job.get("task_id") or job.get("id") or ""
    add("提交任务", bool(task_id), f"id={task_id[:14]} status={job.get('status')}")
    if not task_id:
        return report(checks)

    final: dict = {}
    started = time.time()
    last_state = ""
    while time.time() - started < timeout + 60:
        time.sleep(5)
        try:
            final = get_json(f"/admin/tasks/{task_id}")
        except SystemExit:
            continue
        state = str(final.get("status"))
        if state != last_state:
            print(f"  [{time.time() - started:.0f}s] {state} "
                  f"progress={final.get('progress')}")
            last_state = state
        if state in ("completed", "failed", "cancelled", "expired"):
            break

    elapsed = time.time() - started
    add("任务完成", final.get("status") == "completed",
        f"{final.get('status')} 耗时 {elapsed:.0f}s err={final.get('error')}")

    token = str(final.get("media_token") or "")
    add("有 media_token", bool(token))
    add("kind 标成 video", final.get("kind") == "video", str(final.get("kind")))
    add("响应带 mime", bool(final.get("mime")), str(final.get("mime")))
    add("响应带 size", bool(final.get("size")), str(final.get("size")))

    if token:
        got = httpx.get(f"{base()}/v1/media/{token}", headers=auth_headers(),
                        timeout=600, trust_env=False)
        add("成品可下载", got.status_code == 200,
            f"{got.status_code} {len(got.content)}B")
        ok, why = is_video(got.content[:16])
        add("成品是真视频容器", ok, why)
        if final.get("size"):
            add("下载字节数与 size 一致",
                len(got.content) == int(final["size"]),
                f"{len(got.content)} vs {final['size']}")

        rng = httpx.get(f"{base()}/v1/media/{token}", headers=auth_headers(),
                        timeout=300, trust_env=False)
        probe = httpx.get(f"{base()}/v1/media/{token}",
                          headers={**auth_headers(), "Range": "bytes=0-1023"},
                          timeout=300, trust_env=False)
        add("Range 可分段（播放器拖进度所需）",
            probe.status_code == 206 and len(probe.content) == 1024
            and probe.content == rng.content[:1024],
            f"{probe.status_code} {probe.headers.get('content-range', '-')}")

    after = get_json("/admin/observability").get("counters") or {}
    built = after.get("protocol_driver_built", 0) - before.get("protocol_driver_built", 0)
    bbuilt = after.get("browser_driver_built", 0) - before.get("browser_driver_built", 0)
    fb = after.get("protocol_fallback", 0) - before.get("protocol_fallback", 0)
    add("走了协议驱动", built >= 1, f"protocol+{built}")
    add("未回退浏览器", bbuilt == 0 and fb == 0,
        f"browser+{bbuilt} fallback+{fb}")

    return report(checks)


def report(checks: list[tuple[str, bool, str]]) -> int:
    print("\n=== 视频端到端 ===")
    for name, ok, observed in checks:
        mark = "✓" if ok else "✗"
        tail = f"  [{observed}]" if observed else ""
        print(f"  {mark} {name}{tail}")
    bad = sum(1 for _, ok, _ in checks if not ok)
    print(f"  -> {len(checks) - bad}/{len(checks)} 通过")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
