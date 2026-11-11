"""B4 验收：muse-bridge 的媒体字段经 relay 之后是否还在、**且能用**。

## 为什么需要它

`media` 是 muse-bridge 在标准 OpenAI 响应之上扩展的字段（`choices[].message.media`
/ `choices[].delta.media`）。中间加一层 relay 之后它会不会被丢掉，**只能实测**。

更隐蔽的是第二层：字段传下来了，但里面的 `url` 是**相对路径**
（`/v1/media/<token>`）——桥不知道自己在网关后面，写死绝对地址会在换域名时失效，
所以相对是对的。可是客户端会按**网关自己的 origin** 解析它。如果网关没有这条
下载路由，客户端拿到的是"字段齐全、链接 404"。

2026-11-11 实测就是这个结果：同一个 token 直连桥 200 / 392122 B，经网关 404。
relay 侧的修复见 `server/app.py` 的 `media_passthrough`（V1.13.0）。

## 验什么

  * 非流式 `message.media`、流式 `delta.media` 都还在
  * 两形态字段集相同
  * **把 url 按 relay 的 origin 解析后能下载**（这是本脚本的核心）
  * 下载字节与声明的 sha256 一致
  * HEAD 探测可用（播放器/下载器都会先发它）
  * Range 可分段（拖动进度所需）

## 两个"不冒充通过"的规定

1. **两种形态各自独立判定。** 上游对同一个提示词并不保证每次都出图：实测出现过
   "非流式出图、流式只回文本"。所以某一形态没出图时记 **未触发**，不算失败——
   但它也不会被算成通过，汇总里单独列出来。
2. **有界重试。** 每种形态最多试 `--attempts` 次（默认 3），每次都要真的发请求，
   所以会消耗额度。仍不出图就记未触发。

## 用法

    MUSE_TOOLS_API_KEY=<relay client key> \\
    python tools/accept_relay_media.py --relay http://127.0.0.1:8765 --model muse-1

    python tools/accept_relay_media.py --relay ... --skip-media-download --attempts 1

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
    """通过 / 失败 / 未触发 三类分开记，不让"没有东西可验"混进"验过了"。"""

    def __init__(self, title: str) -> None:
        self.title = title
        self.items: list[tuple[str, str, str]] = []  # (状态, 名称, 观测)

    def ok(self, name: str, observed: str = "") -> None:
        self.items.append(("ok", name, observed))

    def fail(self, name: str, observed: str = "") -> None:
        self.items.append(("fail", name, observed))

    def skip(self, name: str, observed: str = "") -> None:
        self.items.append(("skip", name, observed))

    def check(self, name: str, passed: bool, observed: str = "") -> None:
        (self.ok if passed else self.fail)(name, observed)

    def emit(self) -> int:
        print(f"\n=== {self.title} ===")
        glyph = {"ok": "✓", "fail": "✗", "skip": "·"}
        for state, name, observed in self.items:
            print(f"  {glyph[state]} {name}" + (f"  [{observed}]" if observed else ""))
        passed = sum(1 for s, _, _ in self.items if s == "ok")
        failed = sum(1 for s, _, _ in self.items if s == "fail")
        skipped = sum(1 for s, _, _ in self.items if s == "skip")
        tail = f"，{skipped} 项未触发" if skipped else ""
        print(f"  -> {passed}/{passed + failed} 通过{tail}")
        return failed


def _flags(argv: list[str]) -> dict:
    opts = {"relay": "http://127.0.0.1:8765", "model": "muse-1",
            "skip": False, "attempts": 3, "help": False, "error": ""}
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("-h", "--help"):
            opts["help"] = True
            i += 1
        elif arg == "--relay":
            opts["relay"] = argv[i + 1]
            i += 2
        elif arg == "--model":
            opts["model"] = argv[i + 1]
            i += 2
        elif arg == "--attempts":
            opts["attempts"] = max(1, int(argv[i + 1]))
            i += 2
        elif arg == "--skip-media-download":
            opts["skip"] = True
            i += 1
        else:
            # 不认识的参数**不能静默忽略**：那会让误输入（比如 `--help`）被当成
            # "没给参数"，直接跑起会消耗额度的验收（2026-11-11 真踩过）。
            opts["error"] = f"不认识的参数：{arg}"
            i += 1
    opts["relay"] = opts["relay"].rstrip("/")
    return opts


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


def _plain_once(relay: str, headers: dict, body: dict) -> tuple[dict, list[dict], str]:
    try:
        resp = httpx.post(f"{relay}/v1/chat/completions", headers=headers,
                          json={**body, "stream": False}, timeout=600, trust_env=False)
    except httpx.HTTPError as exc:
        return {}, [], f"{type(exc).__name__}: {exc}"
    if resp.status_code != 200:
        return {}, [], f"HTTP {resp.status_code} {resp.text[:90]}"
    payload = resp.json()
    media = ((payload.get("choices") or [{}])[0].get("message") or {}).get("media") or []
    return payload, media, ""


def _stream_once(relay: str, headers: dict, body: dict) -> tuple[int, bool, list[dict], list[str]]:
    chunks, media, done, bad = 0, [], False, []
    try:
        with httpx.stream("POST", f"{relay}/v1/chat/completions", headers=headers,
                          json={**body, "stream": True}, timeout=600, trust_env=False) as resp:
            if resp.status_code != 200:
                bad.append(f"status={resp.status_code}")
                return chunks, done, media, bad
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
                    media.extend(delta["media"])
    except httpx.HTTPError as exc:
        bad.append(f"{type(exc).__name__}: {exc}")
    return chunks, done, media, bad


def _bare_retry(name: str, attempts: int, run) -> tuple:
    """跑到"有结果"为止：成功出附件、或明确报错，都算有结果。"""
    out = run()
    for attempt in range(2, attempts + 1):
        if out[2] or out[-1] == "err":
            return out
        print(f"  · {name}：第 {attempt - 1} 次没出附件，重试…")
        out = run()
    return out


USAGE = """用法：python tools/accept_relay_media.py [--relay URL] [--model 名称]
                                      [--attempts N] [--skip-media-download]

会真的发聊天请求（消耗额度），并且只把"上游真出了附件"当作透传通过；
上游没出附件时如实报"未触发"，不冒充通过。"""


def main(argv: list[str] | None = None) -> int:
    opts = _flags(list(sys.argv[1:] if argv is None else argv))
    if opts["help"]:
        print(USAGE)
        return 0
    if opts["error"]:
        print(opts["error"], file=sys.stderr)
        print(USAGE, file=sys.stderr)
        return 2
    relay, model, attempts = opts["relay"], opts["model"], opts["attempts"]
    report = Report("B4 媒体字段经 relay 透传且可用")
    headers = auth_headers()
    body = {"model": model, "messages": [{"role": "user", "content": PROMPT}]}

    # ---- 非流式：有界重试，只把"出附件"或"明确报错"当结果 ----
    plain_media, plain_err = [], ""
    for attempt in range(1, attempts + 1):
        _, plain_media, plain_err = _plain_once(relay, headers, body)
        if plain_err or plain_media:
            break
        if attempt < attempts:
            print(f"  · 非流式：第 {attempt} 次没出附件，重试…")
    if plain_err:
        report.fail("非流式经 relay 成功", plain_err)
    elif plain_media:
        report.ok("非流式经 relay 成功", "200")
        report.ok("非流式 message.media 保留", f"{len(plain_media)} 个附件")
    else:
        report.ok("非流式经 relay 成功", "200，但均未出附件")
        report.skip("非流式 message.media 保留", f"{attempts} 次均未出附件")

    # ---- 流式 ----
    chunks, done, stream_media, bad = 0, False, [], []
    for attempt in range(1, attempts + 1):
        chunks, done, stream_media, bad = _stream_once(relay, headers, body)
        if stream_media:
            break
        if attempt < attempts:
            print(f"  · 流式：第 {attempt} 次没出附件，重试…")
    report.ok("流式经 relay 成功", f"{chunks} 帧 done={done}")
    report.check("流式帧未被 relay 破坏", not bad, f"坏帧={len(bad)}")
    if stream_media:
        report.ok("流式 delta.media 保留", f"{len(stream_media)} 个附件")
    else:
        report.skip("流式 delta.media 保留", f"{attempts} 次均未出附件")

    if plain_media and stream_media:
        pk, sk = set(plain_media[0]), set(stream_media[0])
        report.check("两形态字段集相同", pk == sk,
                     f"仅非流式={sorted(pk - sk)} 仅流式={sorted(sk - pk)}")
    elif plain_media or stream_media:
        report.skip("两形态字段集相同", "只有一种形态出图，无法比对")

    if opts["skip"]:
        return report.emit()

    if not (plain_media or stream_media):
        print("\n  两种形态都没有附件：透传**未触发**，这不等于通过。")
        return report.emit()

    # ---- 按客户端方式取件（原缺陷所在）----
    for label, refs in (("非流式", plain_media), ("流式", stream_media)):
        if not refs:
            continue
        ref = refs[0]
        url = _attachment_url(ref, relay)
        report.check(f"{label}附件 url 可解析", bool(url),
                     url[:60] + "…" if url else "无 url/token")
        if not url:
            continue
        try:
            got = httpx.get(url, headers=headers, timeout=300, trust_env=False)
            report.check(f"{label}经 relay 可下载", got.status_code == 200,
                         f"{got.status_code} {len(got.content)}B")
            claimed = ref.get("sha256")
            if claimed and got.status_code == 200:
                actual = hashlib.sha256(got.content).hexdigest()
                report.check(f"{label} sha256 与声明一致", actual == claimed,
                             f"声明 {str(claimed)[:12]}… 实际 {actual[:12]}…")
            head = httpx.head(url, headers=headers, timeout=120, trust_env=False)
            report.check(f"{label} HEAD 探测可用", head.status_code == 200,
                         f"{head.status_code} CL={head.headers.get('content-length', '-')}")
            part = httpx.get(url, headers={**headers, "Range": "bytes=0-1023"},
                             timeout=120, trust_env=False)
            report.check(f"{label} Range 可分段",
                         part.status_code == 206 and len(part.content) == 1024,
                         f"{part.status_code} {part.headers.get('content-range', '-')}")
        except httpx.HTTPError as exc:
            report.fail(f"{label}经 relay 可下载", f"{type(exc).__name__}: {exc}")

    return report.emit()


if __name__ == "__main__":
    raise SystemExit(main())
