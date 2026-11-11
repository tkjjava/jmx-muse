"""B4 验收：muse-bridge 的媒体字段经 relay 之后是否还在、**且能用**。

## 为什么需要它

`media` 是 muse-bridge 在标准 OpenAI 响应之上扩展的字段（`choices[].message.media`
/ `choices[].delta.media`）。中间加一层 relay 之后它会不会被丢掉，**只能实测**。

更隐蔽的是第二层：字段传下来了，但里面的 `url` 是**相对路径**
（`/v1/media/<token>`）——桥不知道自己在网关后面，写死绝对地址会在换域名时失效，
所以相对是对的。可是客户端会按**网关自己的 origin** 解析它。如果网关没有这条
下载路由，客户端拿到的是"字段齐全、链接 404"。

2026-11-11 实测就是这个结果：同一个 token 直连桥 200 / 392122 B，经网关 404。
relay 侧的修复见 `server/app.py` 的 `media_passthrough`（V1.13）。

## 验什么

  * 非流式 `message.media`、流式 `delta.media` 都还在
  * 两形态字段集相同
  * **把 url 按 relay 的 origin 解析后能下载**（这是本脚本的核心）
  * 下载字节与声明的 sha256 一致
  * HEAD 探测可用（播放器/下载器都会先发它）
  * Range 可分段（拖动进度所需）

## 用法

    MUSE_TOOLS_API_KEY=<relay client key> \
    python tools/accept_relay_media.py --relay http://127.0.0.1:8765 --model muse-1
    python tools/accept_relay_media.py --relay ... --skip-media-download

会触发真实上游生成（要出图才有 media），消耗额度。
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from urllib.parse import urljoin

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from tools.bridge_ops import auth_headers  # noqa: E402

PROMPT = "画一只橘猫坐在窗台上，只要一张图"


class Report:
    def __init__(self, title: str) -> None:
        self.title = title
        self.items: list[tuple[str, bool, str]] = []

    def add(self, name: str, ok: bool, observed: str = "") -> None:
        self.items.append((name, ok, observed))

    def emit(self) -> int:
        print(f"\n=== {self.title} ===")
        for name, ok, observed in self.items:
            print(f"  {'✓' if ok else '✗'} {name}" + (f"  [{observed}]" if observed else ""))
        bad = sum(1 for _, ok, _ in self.items if not ok)
        print(f"  -> {len(self.items) - bad}/{len(self.items)} 通过")
        return bad


def _args(argv: list[str]) -> tuple[str, str, bool]:
    relay, model, skip = "http://127.0.0.1:8765", "muse-1", False
    i = 0
    while i < len(argv):
        if argv[i] == "--relay":
            relay, i = argv[i + 1], i + 2
        elif argv[i] == "--model":
            model, i = argv[i + 1], i + 2
        elif argv[i] == "--skip-media-download":
            skip, i = True, i + 1
        else:
            i += 1
    return relay.rstrip("/"), model, skip


def _attachment_url(ref: dict, relay: str) -> str:
    """按**客户端的方式**拼下载地址：相对路径以 relay 的 origin 为基准。

    这正是原缺陷所在——不是"桥能不能给"，而是"客户端照着响应里的 url 去取，
    取得到吗"。把这一步单独抽出来，是为了让失败点一眼可见。
    """
    raw = str(ref.get("url") or "")
    if not raw:
        token = str(ref.get("token") or "")
        return urljoin(relay + "/", f"/v1/media/{token}") if token else ""
    return urljoin(relay + "/", raw)


def main(argv: list[str] | None = None) -> int:
    relay, model, skip = _args(list(sys.argv[1:] if argv is None else argv))
    report = Report("B4 媒体字段经 relay 透传且可用")
    headers = auth_headers()
    base_body = {"model": model, "messages": [{"role": "user", "content": PROMPT}]}

    # ---- 非流式 ----
    plain: dict = {}
    try:
        resp = httpx.post(f"{relay}/v1/chat/completions", headers=headers,
                          json={**base_body, "stream": False}, timeout=600,
                          trust_env=False)
        report.add("非流式经 relay 成功", resp.status_code == 200,
                   f"{resp.status_code} {resp.text[:90] if resp.status_code != 200 else ''}")
        if resp.status_code == 200:
            plain = resp.json()
    except httpx.HTTPError as exc:
        report.add("非流式经 relay 成功", False, f"{type(exc).__name__}: {exc}")

    message = ((plain.get("choices") or [{}])[0].get("message") or {})
    plain_media = message.get("media") or []
    report.add("非流式 message.media 保留", bool(plain_media), f"{len(plain_media)} 个附件")

    # ---- 流式 ----
    chunks = 0
    stream_media: list[dict] = []
    done = False
    bad: list[str] = []
    try:
        with httpx.stream("POST", f"{relay}/v1/chat/completions", headers=headers,
                          json={**base_body, "stream": True}, timeout=600,
                          trust_env=False) as resp:
            if resp.status_code != 200:
                bad.append(f"status={resp.status_code}")
            else:
                for line in resp.iter_lines():
                    if not line:
                        continue
                    if not line.startswith("data:"):
                        bad.append(line[:50])
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        done = True
                        break
                    try:
                        obj = json.loads(payload)
                    except ValueError:
                        bad.append(payload[:50])
                        continue
                    chunks += 1
                    delta = ((obj.get("choices") or [{}])[0].get("delta") or {})
                    if delta.get("media"):
                        stream_media.extend(delta["media"])
    except httpx.HTTPError as exc:
        bad.append(f"{type(exc).__name__}: {exc}")

    report.add("流式经 relay 成功", chunks > 0 and done, f"{chunks} 帧 done={done}")
    report.add("流式帧未被 relay 破坏", not bad, f"坏帧={len(bad)}")
    report.add("流式 delta.media 保留", bool(stream_media), f"{len(stream_media)} 个附件")

    if plain_media and stream_media:
        pk, sk = set(plain_media[0]), set(stream_media[0])
        report.add("两形态字段集相同", pk == sk,
                   f"仅非流式={sorted(pk - sk)} 仅流式={sorted(sk - pk)}")

    if not (plain_media or stream_media):
        report.add("上游是否有产出", False, "本次没有附件，透传**未触发**（不是通过）")
        return 1 if report.emit() else 1

    if skip:
        return 1 if report.emit() else 0

    # ---- 按客户端方式取件（原缺陷所在）----
    for label, refs in (("非流式", plain_media), ("流式", stream_media)):
        if not refs:
            continue
        ref = refs[0]
        url = _attachment_url(ref, relay)
        report.add(f"{label}附件 url 可解析", bool(url), url[:60] + "…" if url else "无 url/token")
        if not url:
            continue
        try:
            got = httpx.get(url, headers=headers, timeout=300, trust_env=False)
            report.add(f"{label}经 relay 可下载", got.status_code == 200,
                       f"{got.status_code} {len(got.content)}B")
            claimed = ref.get("sha256")
            if claimed and got.status_code == 200:
                actual = hashlib.sha256(got.content).hexdigest()
                report.add(f"{label} sha256 与声明一致", actual == claimed,
                           f"声明 {str(claimed)[:12]}… 实际 {actual[:12]}…")
            head = httpx.head(url, headers=headers, timeout=120, trust_env=False)
            report.add(f"{label} HEAD 探测可用", head.status_code == 200,
                       f"{head.status_code} CL={head.headers.get('content-length', '-')}")
            part = httpx.get(url, headers={**headers, "Range": "bytes=0-1023"},
                             timeout=120, trust_env=False)
            report.add(f"{label} Range 可分段",
                       part.status_code == 206 and len(part.content) == 1024,
                       f"{part.status_code} {part.headers.get('content-range', '-')}")
        except httpx.HTTPError as exc:
            report.add(f"{label}经 relay 可下载", False, f"{type(exc).__name__}: {exc}")

    return 1 if report.emit() else 0


if __name__ == "__main__":
    raise SystemExit(main())
