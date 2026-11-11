# 上游漏洞挖掘清单（纯静态，离线 PoC 可跑）

配套：`docs/HARVEST.md`（可抄工程）、`muse_bridge/upstream_quirks.py`（兼容层）。
范围：`work/muse-re/` 下已落盘的 30 个 muse 相关仓（muse2api 系 6 个、
museapi-video、new-api、one-api、midjourney-proxy、Muse-Canvas、suno-api 等）。

**硬约束（本清单遵守）**：没有拉任何新仓库、没有对真实站点发请求、没有起真实浏览器。
每条 PoC 都是「基于本地源码的伪输入」的离线片段：用桩替换网络/进程，只证明
**代码路径与判定条件**成立，不证明真实环境可利用。

## 0. 术语

可达性三档：

| 标记 | 含义 |
| --- | --- |
| **external** | 任何能访问 HTTP API 的人（持 key 的下游客户端也算）可直接触发；或浏览器侧可从任意网页触发 |
| **internal** | 只有本机文件系统/容器内进程/运维通道能触及，HTTP 面不可达 |
| **unreachable** | 在本仓默认部署下不可达（写明为什么，如「Chrome 只 bind 127.0.0.1」） |

验证等级：

* `V1` 源码逐行直读，行号可复核，未执行；
* `V2` 跨两个以上仓库复现同一缺陷，或本仓已有对应修复且被离线测试覆盖；
* `V3` 由 V1 推导的影响/利用边界，未实测，按最保守方向陈述。

## 1. 汇总

| ID | 类型 | 上游证据 | 可达性 | 影响 | 等级 |
| --- | --- | --- | --- | --- | --- |
| V-01 | 权限坍缩：单密钥同守 `/v1` 与 `/admin`；空密钥放行分支 | czg `app.py:319-329`、`:2136-2154`、`:2836-2844`；bynne `app.py:263-272`；bitgacon 同构 | external（持 key）；空密钥分支为 internal/条件 | 客户端→管理员：删号、轮换主密钥、推代码、改 `.env` | V1 + V3 |
| V-02 | 媒体接口无鉴权（成品公开可读） | czg `app.py:1822-1829`；bynne `app.py:1535-1542`；bitgacon `app.py:1538-1545` | external | 成品泄露/盗链/绕开配额 | V1 + V2 |
| V-03 | SSRF：参考图 URL 无主机校验、跟随重定向、无大小限制 | bynne `engine.py:1076-1085`；入口 `app.py:1116-1128`、`:1161-1167` | external | 探测/访问内网与元数据端点；端口扫描；内存放大 | V1 + V3 |
| V-04 | 任意本地文件读：参考图参数直接当路径 `open()` | bynne `engine.py:1086-1094` | external | 读服务器任意可读文件（base64 进入生成流程）；文件存在性 oracle | V1 |
| V-05 | 资源耗尽：video 队列/线程无上限、参考体无上限（TOCTOU 准入） | czg `app.py:1451-1488`（对比 image `:1274-1277`）；bynne `engine.py:1079-1080` | external | 线程/内存打满、共享上游账号被风控 | V1 |
| V-06 | CDP 未鉴权 + `--remote-allow-origins=*` | czg `engine.py:346-367`；`config.py:73`（默认 19210）；`config.py:68`（默认 0.0.0.0） | external（浏览器侧，需受害者访问恶意页）；API 不可达 | 任意网页 JS 可连 loopback CDP，读页面 cookie/驱动已登录会话 | V1 + V3 |
| V-07 | 明文 cookie/代理凭据落盘 + `.env` 秘密世界可读 | czg `store.py:86-96`、`:117-124`、`:127-132`；`app.py:2157-2160` | internal（需本地文件/卷/备份） | 拿到 accounts.json 即接管 muse 账号；`.env` 泄露主密钥 | V1 |
| V-08 | 管理面越权写：repo push、`.env` token 覆写、主 key 轮换 | czg `app.py:2836-2844`、`:2568-2571`、`:2145-2154` | external（持 key，与 V-01 同根） | 向维护者仓库提交代码；覆写 `GITHUB_TOKEN`；轮换密钥致全站掉线 | V1 |
| V-09 | 管理面只读接口未鉴权（信息泄露） | bynne `app.py:1795-1804`、`:1807-1828`、`:1831-1838` | external | 助手脚本/扩展包公开下载、目录列表泄露；暴露管理面存在 | V1 |
| V-10 | 回调 SSRF（notifyHook 每任务可控、无主机校验） | mj-proxy `NotifyServiceImpl.java:37-80`；`SubmitController.java:203-204`；`BaseSubmitDTO.java:14-15` | external（持 key 的用户） | 盲 SSRF：服务端向任意内网地址 POST 任务 JSON | V1 + V3 |


严重度排序（按可达性 × 影响，不按 CVSS 套话）：
V-01 ≈ V-08 > V-02 > V-03 ≈ V-04 > V-10 > V-06 > V-05 > V-07 > V-09。

## 2. 逐条

### V-01 权限坍缩：一个 API key 同时是管理员口令

**证据**

* 鉴权函数只有一把 key，且**空值直接放行**：
  `czg app.py:319-329`（`def auth(...)`；`if not CFG.api_key: return True`）；
  bynne 同款 `app.py:263-272`；bitgacon 同构（`app.py` 的 `auth`）。
* `/v1` 与 `/admin` 都挂这同一个依赖：
  `/v1/videos` → `czg app.py:1453`（`Depends(auth)`）；
  `/admin/apikey` → `:2137`；`/admin/apikey/rotate` → `:2146`；
  `/admin/repo/push` → `:2837`。
* 敏感管理动作不需要额外角色：删账号、轮换主 key、改 `.env`、推代码全在同一权限层。

**可达性**：external。部署者把 key 发给下游客户端后，那个客户端就拥有管理员权限
（多租户 SaaS 场景里等于水平越权→垂直越权）。空密钥分支在本仓启动钩子会自动
生成 key（`czg app.py:2878-2883`），正常流程下不可达；若 `.env` 不可写或进程
不经 startup 被嵌入，则**无凭据也可调管理面**（internal/条件可达）。

**离线 PoC（逻辑等价复现，不启动服务）**

```python
# 复刻上游 app.py:319-329 的判定；不 import 上游模块，不发请求
CFG_API_KEY = ""                     # 上游 config.py 读取 .env，可为空
def auth(authorization: str | None = None) -> bool:
    if not CFG_API_KEY:
        return True                  # ← 无凭据直接 true
    return authorization == f"Bearer {CFG_API_KEY}"

assert auth(None) is True            # 空密钥部署：匿名即管理员
CFG_API_KEY = "m2a_demo"
assert auth("Bearer m2a_demo") is True   # 客户端 key 即管理员 key
```

**修复一行**：`/admin` 改用独立密钥/会话（如 `ADMIN_TOKEN` + 仅 loopback 或
cookie 会话），且 `auth()` 在 key 为空时 raise 而不是 `return True`。

**本仓受影响面（同型，未修）**：`app.py:45-56` 的 `allowed_keys()` 与
`require_key`；`MUSE_BRIDGE_API_KEYS` 同时守 `/v1` 与 `/admin/*`
（`app.py:431-487`、`:1115-1253`），密钥表为空时 `require_key` 直接 `return`
（`app.py:52-53`）。V-01 的修法对本仓同样适用，需要 lead 决策（拆密钥或拒绝空表启动）。

**验证等级**：V1（源码）；V3（影响面）。

---

### V-02 媒体接口无鉴权（同一段代码被复制到三个仓）

**证据**

* czg `app.py:1822-1829`：`@app.get("/v1/media/{name})`、`def get_media(name: str)`，
  **没有 `Depends(auth)`**，只有文件名黑名单 + `FileResponse`。
* bynne `app.py:1535-1542`、bitgacon `app.py:1538-1545`：逐行同构。
* 对照修法：lichao `app/routers/v1.py:596-644` 才补 `media_auth_required` +
  Bearer 校验；`app/media_rewrite.py:15-22` 的 `is_safe_media_name`。

**可达性**：external，**不需要任何凭据**。文件名是 public 的（生成结果 URL 会
写进 `data[].url`、日志、客户端缓存），URL 一旦泄露即成公开资源。

**离线 PoC（进程内 TestClient，不联网）**

```python
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

app = FastAPI()
@app.get("/v1/media/{name}")           # 复刻 czg app.py:1822-1829：无鉴权依赖
def get_media(name: str):
    if "/" in name or "\\" in name or ".." in name:
        raise HTTPException(400, "非法文件名")
    return {"served": name}

client = TestClient(app)
r = client.get("/v1/media/deadbeef.png")   # 不带 Authorization
assert r.status_code == 200                 # ← 匿名可读成品
```

**修复一行**：路由加 `_: None = Depends(require_key)`（或签发不可猜 token）——
上游的修法见 lichao `v1.py:608-626`。

**本仓受影响面（已修）**：`app.py:882-914` 三处都带 `Depends(require_key)`；
token 是 32 位 hex（`media.py:66`），删除也要 key（`app.py:915-918`）。
本仓**仍要守住的边界**：永远不要把 `muse.ai/files/...` 直链当交付 URL
（`media_url_policy(upstream_direct=True)` 会抛 `MediaPolicyError`）。

**验证等级**：V1 + V2（三仓复现 + 本仓已修并被 `tests/test_media.py` 覆盖）。

---

### V-03 参考图 URL 的 SSRF（无主机校验、跟随重定向、无大小上限）

**证据**

* 入口：bynne `app.py:1116-1128` 接受 `image` / `images` / `reference_image` /
  `image_url`，`:1161-1167` 把它们传进生成流程。
* 取回：`engine.py:1076-1085`：

  ```python
  if img.startswith("http://") or img.startswith("https://"):
      req = urllib.request.Request(img, headers={"User-Agent": "Mozilla/5.0"})
      with urllib.request.urlopen(req, timeout=20) as resp:
          data = resp.read()            # 无上限
  ```

  只判了字符串前缀：没有解析主机、没有私网/回环/链路本地拒绝、没有禁用重定向
  （`urllib` 默认跟随）、没有 `Content-Length` 限制。

**可达性**：external（持 key 的客户端）。可打 `http://127.0.0.1:PORT/`、
`http://169.254.169.254/latest/meta-data/`、容器内服务；响应被 base64 后送进
muse 生成流程，攻击者拿不到回显，但可获得**盲 SSRF**（端口/路径存在性差异、
内网状态变更、耗时侧信道）以及内存放大（见 V-05）。

**离线 PoC（用桩替换 urlopen，不联网）**

```python
import base64, urllib.request

seen = {}
def fake_urlopen(req, timeout=None):
    seen["url"] = req.full_url if hasattr(req, "full_url") else req
    class FakeResp:
        headers = {"content-type": "image/png"}
        def read(self): return b"\x89PNG-fake"
        def __enter__(self): return self
        def __exit__(self, *exc): return False
    return FakeResp()

urllib.request.urlopen = fake_urlopen

def normalize_image_data(img):        # 复刻 bynne engine.py:1076-1085
    if img.startswith("http://") or img.startswith("https://"):
        req = urllib.request.Request(img, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            return base64.b64encode(resp.read()).decode()

assert normalize_image_data("http://169.254.169.254/latest/meta-data/")
assert seen["url"] == "http://169.254.169.254/latest/meta-data/"   # ← 内网目标被取回
```

**修复一行**：解析目标 → 只允许 http(s) 且 `ip.is_global` → 每一跳重定向重新校验
→ 限制字节数；本仓现成实现 `refs.py:297-366`（`_PinnedRedirectBlock` 禁自动跳转、
`_PinnedHTTPConnection` 固定已验证 IP、`:364-366` 拒绝非全局地址）。

**本仓受影响面（已修）**：`refs.fetch_public` 是参考素材唯一入口
（`app.py:632-668` 只对 http(s) 调它），并带 10 个/40 MiB/100 MiB 限制
（`refs.py:66-69`）。测试覆盖在 `tests/test_refs*.py`。

**验证等级**：V1（代码路径）+ V3（盲 SSRF 影响）。

---

### V-04 任意本地文件读：参考图参数直接当路径打开

**证据**：bynne `engine.py:1086-1094`

```python
if os.path.isfile(img):
    with open(img, "rb") as f:
        data = f.read()
    mime = mimetypes.guess_type(img)[0] or "image/png"
    return base64.b64encode(data).decode("ascii"), mime
```

`img` 来自请求体（`app.py:1116-1128`），**没有任何目录约束**：`/etc/passwd`、
`~/.ssh/id_rsa`、`data/accounts.json`（含明文 cookie，见 V-07）只要是本进程可读的
普通文件就成立。内容经 base64 进入生成流程，人类看不到直接回显，但可作
文件存在性 oracle；配合 `mimetypes.guess_type` 的差异还能猜扩展名。

**可达性**：external（持 key）。

**离线 PoC（只读自己创建的临时文件，无害）**

```python
import base64, mimetypes, os, tempfile

secret = os.path.join(tempfile.mkdtemp(), "accounts.json")
with open(secret, "w", encoding="utf-8") as f:
    f.write('{"cookies": {"hatch_sess": "S3CR3T"}}')

def read_ref(img):                    # 复刻 bynne engine.py:1086-1094
    if os.path.isfile(img):           # ← 唯一的"校验"：是不是普通文件
        with open(img, "rb") as f:
            return base64.b64encode(f.read()).decode(), mimetypes.guess_type(img)[0]
    return None

b64, mime = read_ref(secret)
assert base64.b64decode(b64).startswith(b'{"cookies"')   # ← 任意本地文件被读
```

**修复一行**：本地路径必须 `resolve()` 后校验落在 `media_dir` 内（`commonpath`
或 `is_relative_to`），其余一律只接受 data URL/http(s)；上游另一分支
（`bynne engine.py:1095`）对未知输入直接原样返回，也要一并收紧。

**本仓受影响面**：`media.py` 的读取只按 token（`TOKEN_RE.fullmatch`）拼路径，
`app.py` 不接受任意本地路径作为参考图来源——`_collect_references` 只处理 http(s)
（`app.py:632-668`）；本仓无此面。

**验证等级**：V1。

---

### V-05 资源耗尽：video 无准入、线程无上限、参考体无上限

**证据**

* image 有准入：czg `app.py:1274-1277`「in-flight ≥ 8 → 429」；
  **video 没有**：`app.py:1451-1488` 直接 `store.create_task` 后
  `threading.Thread(target=worker, daemon=True).start()`（`:1486`），
  每个请求一条线程、一条常驻任务行、一份参考图内存。
* 准入检查在加锁之外：`:1275-1277` 的计数与 `create_task` 之间没有原子性，
  并发突发可越过上限（TOCTOU），只是影响比「完全不设限」小。
* 参考体无上限：bynne `engine.py:1079-1080` 的 `resp.read()`（见 V-03），
  随后 `base64.b64encode` 再翻倍。
* 部署面：czg `config.py:68` 默认 `host=0.0.0.0`，API 直接对网络开放。

**可达性**：external（持 key）。提交 N 个视频任务即可占用 N 条线程 + N 份等待
同一把浏览器锁的协程/线程；上游账号侧还会因并发被风控（`engine.py` 单 worker 设计）。

**离线 PoC（复刻准入判定，不启线程）**

```python
def admitted(tasks, kind):            # 复刻 czg app.py:1274-1277
    if kind != "image":
        return True                   # ← video 没有这条准入
    in_flight = sum(t["kind"] == kind and t["status"] in ("queued", "processing")
                    for t in tasks)
    return in_flight < 8

tasks = [{"kind": "video", "status": "queued"} for _ in range(10_000)]
assert admitted(tasks, "video") is True      # 10000 个视频任务全部受理
```

**修复一行**：image/video 共用同一个准入计数（按 in-flight 总数或分 kind 各自上限），
把「计数 + 创建任务」放进同一把锁，超限返回 429；参考体按 `MAX_REFERENCE_BYTES` 截断。

**本仓受影响面（已修）**：`jobs.py:361-364` 对所有入队任务统一
`queue_max`（默认 8，`MUSE_JOB_QUEUE_MAX`），超出抛 `JobQueueFull`；
参考素材有 10 个/40 MiB/100 MiB/120s 限制（`refs.py:66-69`）。
本仓没有「每请求一条线程」的写法（任务由事件循环驱动）。

**验证等级**：V1。

---

### V-06 CDP 未鉴权且接受任意 Origin

**证据**

* 启动参数：czg `engine.py:363-364`

  ```python
  f"--remote-debugging-port={self.cfg.cdp_port}",
  "--remote-allow-origins=*",
  ```

  默认端口 `19210`（`config.py:73`）；调试 URL 固定 loopback
  （`engine.py:346-347`：`http://127.0.0.1:{cdp_port}/json/version`）。
* `--remote-allow-origins=*` 的官方语义就是**取消 WebSocket 的 Origin 校验**
  （Chromium ≥ 111 默认拒绝带 Origin 的 CDP WS 连接），等于把 CDP 交给任意网页。
* 进程常驻 headless（`--headless=new`），暴露窗口 = 服务运行时长。

**可达性**：**external（浏览器侧）**，不依赖 API key，也不依赖 CDP 端口对公网开放：

* API 端口 → CDP：Chrome 的 DevTools 只 bind `127.0.0.1`，外部 HTTP 客户端**不可达**；
* 任意网页 → CDP：受害者浏览器打开恶意页面时，页面 JS 可 `new WebSocket(
  "ws://127.0.0.1:19210/devtools/browser/...")`，因 `*` 不校验 Origin 而被接受，
  之后 `Target.getTargets` / `Network.getCookies` / `Runtime.evaluate` 可读已登录
  muse 会话、驱动浏览器发请求（等同账号接管）。

**离线 PoC（静态断言；不连真实 CDP）**

```python
# 从 czg engine.py:356-367 抄出的参数列表，验证危险开关确实存在
args = ["--headless=new", "--no-sandbox", "--disable-gpu",
        "--remote-debugging-port=19210", "--remote-allow-origins=*",
        "about:blank"]
assert "--remote-allow-origins=*" in args
assert any(a.startswith("--remote-debugging-port") for a in args)
```

**修复一行**：删掉 `--remote-allow-origins=*`（需要给页面脚本用就白名单具体 origin），
并让 CDP 端口随机化 + 仅本机可连（Unix socket 或 loopback + 已知 token）。

**本仓受影响面（默认安全，需文档约束）**：本仓不自启 Chrome，只连接运维提供的
CDP（`driver.py:31` 默认 `MUSE_CDP_URL=http://127.0.0.1:9222`）。因此风险取决于
用户怎么起 Chrome：**不要**加 `--remote-allow-origins=*` 或
`--remote-debugging-address=0.0.0.0`。本仓无代码改动，属于部署约束（写入本清单备查）。

**验证等级**：V1（参数与语义）；V3（浏览器侧利用链，未实测）。

---

### V-07 明文 cookie 落盘 + `.env` 秘密世界可读

**证据**

* czg `store.py:86-96`（`_write`：`json.dump` 明文写到 `data/accounts.json`，
  原子替换但**不加密**）；
* `store.py:117-124`（账号对象里直接存 `"cookies": cookies`；
  `cookies_exp` 也是明文）；
* `store.py:127-132`（**读接口**很干净：`public_account` 剔除 `cookies`/`proxy`，
  只给 `cookie_count`——这一点值得抄）；
* `app.py:2157-2172`（`_persist_env` 用临时文件 + `os.replace` 写 `.env`，无权限收紧；
  默认 umask 下通常 0644，同机其它用户可读）。

**可达性**：internal。HTTP 面读不到（`public_account` 已剥离 cookies），需要
本机文件读/容器卷/备份/打包分发；与 V-04（任意文件读）同机时构成「读文件→
进生成流程」的受限链，但不能直接把 cookie 回显给攻击者，故不标 external。

**离线 PoC（写读自己创建的临时文件）**

```python
import json, os, tempfile

d = tempfile.mkdtemp()
path = os.path.join(d, "accounts.json")
account = {"id": "a1", "cookies": {"hatch_sess": "S3CR3T", "hatch_vml": "S3CR3T2"}}
with open(path, "w", encoding="utf-8") as f:
    json.dump({"accounts": [account]}, f, ensure_ascii=False, indent=1)

with open(path, encoding="utf-8") as f:
    raw = f.read()
assert "hatch_sess" in raw and "S3CR3T" in raw      # ← 明文落盘，任何读取者即得会话
```

**修复一行**：账号库整体加密（本仓做法：`store.py:123-124` Fernet 加密
`profile_enc`，cookie 存在 `profile["cookies"]` 里，`:227-245`），`.env` 写后
`os.chmod(0o600)`。

**本仓受影响面（已修）**：`store.py` 用 Fernet 加密整个 profile（含 cookies），
`MUSE_BRIDGE_KEY` 缺省直接拒绝启动（`store.py:66-77`）。

**验证等级**：V1。

---

### V-08 管理面越权写：推代码、覆写运维凭据、轮换主密钥

**证据**

* czg `app.py:2836-2844`：`POST /admin/repo/push`，`Depends(auth)`（= 与 `/v1`
  同一把 key），**从请求体接受 `github_token`** 并
  `_persist_env("GITHUB_TOKEN", new_token)` + `os.environ[...] = ...`；
* `app.py:2157-2172`：`_persist_env`（无任何校验：请求体里的值直接写回 `.env`）；
* `app.py:2568-2571`：`_ensure_git_repo` 把 token 拼进
  `https://x-access-token:{token}@github.com/czg86389-hub/muse2api.git`；
* `app.py:2145-2154`：`POST /admin/apikey/rotate` 生成新主 key 并写回 `.env`
  ——任何持 key 的客户端都能把所有下游一起踢下线（自伤式 DoS）；
* `app.py:2136-2142`：`GET /admin/apikey` 把主 key 原样返回（同 key 下不算提权，
  但让 key 在日志/代理里多出现一次）。

**可达性**：external（持 key，与 V-01 同根）。攻击者不能改 push 目标仓库
（`REPO_URL` 常量写死 `:2512`），但可以：用自己的 token 触发一次向维护者仓库的
提交（内容来自当前工作目录 `TRACKED_REPO_PATHS` `:2513`）、把 `.env` 里的
`GITHUB_TOKEN` 换成自己的（之后维护者的推送会用攻击者 token，或直接失败）、
或轮换 key 造成全站认证中断。

**离线 PoC（复刻请求体→`.env` 的写入路径，不执行 git）**

```python
import os, tempfile

def _persist_env(path, key, value):    # 复刻 czg app.py:2157-2160（无任何校验）
    lines = []
    replaced = False
    for line in open(path) if os.path.exists(path) else []:
        if line.startswith(key + "="):
            lines.append(f"{key}={value}\n"); replaced = True
        else:
            lines.append(line)
    if not replaced:
        lines.append(f"{key}={value}\n")
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines)

d = tempfile.mkdtemp(); env = os.path.join(d, ".env")
open(env, "w").write("MUSE2API_KEY=m2a_demo\n")
payload = {"github_token": "ghp_attacker", "message": "x"}   # 请求体
token = payload.get("github_token")
assert token                                   # ← 无校验即进入下一行
_persist_env(env, "GITHUB_TOKEN", token)
assert "GITHUB_TOKEN=ghp_attacker" in open(env).read()
```

**修复一行**：删除 `/admin/repo/push` 与 `/admin/apikey` 系列，或移到构建/运维脚本
（进程内不再接受请求体提供的凭据、不再写 `.env`）。

**本仓受影响面**：本仓无 repo push / `.env` 改写端点（`app.py` 的 `/admin` 只读写
账号库与任务），但**同根问题**是 `/admin` 与 `/v1` 共用一把 key（见 V-01 本仓段）。

**验证等级**：V1。

---

### V-09 管理面只读接口未鉴权

**证据**：bynne `app.py`

* `:1795-1804` `GET /admin/cookie-helper`（返回取 cookie 的助手脚本，`?download=1` 直接下载）；
* `:1807-1828` `GET /admin/extension`（即时打 zip 的浏览器扩展包）；
* `:1831-1838` `GET /admin/extension/files`（列扩展目录文件名）。

三条都**没有 `Depends(auth)`**，而同文件的 `/admin/tasks`（`:1842-1844`）是有的。

**可达性**：external，无凭据。影响：助手脚本/扩展包公开下载（扩展包一旦被替换分发，
配合宽权限就是供应链风险，但本清单只静态、不评估其 manifest）；目录列表属信息泄露；
同时暴露「这里有管理面」给扫描器。这是**低危但不该留**的一类。

**离线 PoC（进程内 TestClient，不联网）**

```python
from fastapi import FastAPI
from fastapi.testclient import TestClient

app = FastAPI()
@app.get("/admin/extension/files")     # 复刻 bynne app.py:1831-1838：无鉴权依赖
def extension_files():
    return {"files": ["manifest.json", "background.js", "popup.html"]}

client = TestClient(app)
assert client.get("/admin/extension/files").status_code == 200   # 匿名可读
```

**修复一行**：三个路由统一加 `_: None = Depends(auth)`（若扩展必须公开下载，
改为 `/public/extension` 并明确它是公开资源）。

**本仓受影响面**：本仓 `/admin` 下所有路由均带 `Depends(require_key)`
（`app.py:431-487`、`:1115-1253`）；管理台页面 `/console`（`app.py:626-628`）
是公开 HTML（页面壳不含数据，数据接口都要 key）。无此面。

**验证等级**：V1。

---

### V-10 回调 SSRF：每任务 `notifyHook` 可控且无主机校验

**证据**

* mj-proxy `BaseSubmitDTO.java:14-15`：`notifyHook` 是**每任务**请求字段
  （「回调地址, 为空时使用全局notifyHook」）；
* `SubmitController.java:203-204`：`newTask()` 直接把请求里的 `getNotifyHook()`
  塞进任务属性，无任何校验；
* `NotifyServiceImpl.java:37-41`：取任务属性里的 hook；
  `:76-80`：`private ResponseEntity<String> postJson(...)` →
  `new RestTemplate().postForEntity(notifyHook, httpEntity, String.class)` ——
  没有 scheme/主机白名单、没有私网拒绝、没有超时/重定向策略。

**可达性**：external（持 proxy API key 的普通用户）。回调在任务状态变化时异步触发，
每次状态变化都会 POST 一次任务 JSON 到攻击者指定地址：

* 指向攻击者自己的服务器 → 合法回调用途，不构成问题；
* 指向 `http://169.254.169.254/...`、`http://127.0.0.1:PORT/...` 或内网服务 →
  **盲 SSRF**（服务端向目标 POST JSON）。攻击者看不到响应体，但可借错误/耗时差异
  探测端口、对内网「只读/无 CSRF 防护的 POST 接口」产生副作用。
* 不校验 scheme 还能传 `file:`/`jar:` 等（`RestTemplate` 对不同 scheme 的处理依
  实现而定，`V3` 未验证）。

**离线 PoC（跨语言逻辑等价复现：Java 流程用 Python 桩）**

```python
from types import SimpleNamespace

posts = []
class FakeRestTemplate:
    def postForEntity(self, url, entity, response_type):
        posts.append(url)                       # ← 记录服务端打到的地址
        return SimpleNamespace(status_code=200)

def new_task(base_notify_hook, global_hook=""):  # 复刻 SubmitController.java:203-204
    return base_notify_hook or global_hook

def notify_task_change(task, rest):             # 复刻 NotifyServiceImpl.java:37-80
    notify_hook = task.getPropertyGeneric("notifyHook")
    if not notify_hook:
        return
    rest.postForEntity(notify_hook, "{}", str)

task = SimpleNamespace(getPropertyGeneric=lambda k: new_task("http://169.254.169.254/latest/meta-data/"))
notify_task_change(task, FakeRestTemplate())
assert posts == ["http://169.254.169.254/latest/meta-data/"]   # ← 内网地址被 POST
```

**修复一行**：`notifyHook` 只允许管理台配置的全局值（不接受每任务覆盖），并对 URL
做 scheme 白名单 + 解析 IP 拒绝私网/回环/链路本地 + 禁重定向（可用本仓
`refs.py:297-366` 同一套策略）。

**本仓受影响面**：本仓无用户可配置回调地址；`refs.fetch_public` 只用于 GET 参考素材
且已带上述全部限制。无此面。

**验证等级**：V1（代码路径）；V3（盲 SSRF 具体利用）。

## 3. 核查未成立（写下来防止后人重复挖）

| 编号 | 假设 | 结论 | 证据 |
| --- | --- | --- | --- |
| N-01 | 任务 ID 可枚举 → 越权读任务/成品 | **不成立（在默认单租户部署下）**：czg/muse2api 任务 ID = `"task_" + uuid4().hex[:20]`（80 位随机，`store.py:282`），不可枚举。mj-proxy 的 ID 确实弱（`SubmitController.java:200`：毫秒时间戳 + 3 位随机），但落盘子集里**没有鉴权拦截器实现**（只有 `Constants.java:19` 的 header 常量与 `ProxyProperties.java:53-55` 的 `apiSecret` 配置项），无法确认 `/task/{id}/fetch`、`/task/list`（`TaskController.java:37-58`）是否受 `mj-api-secret` 保护；按单实例单租户假设，列表本就返回全量，枚举不增加泄露面。**结论只到这一层**，不宣称未授权访问 | V1（有缺口） |
| N-02 | 媒体文件名路径穿越（`../`、编码绕过） | **不成立**：上游黑名单挡 `"/"`、`"\\"`、`".."`（czg `app.py:1824`）；且 Starlette 在**解码后**的 path 上匹配 `{name}`（`[^/]+`），`%2e%2e%2f` 解码成含 `/` 的路径根本匹配不到该路由。lichao 另加 `is_safe_media_name`（`media_rewrite.py:15-22`）；本仓用 `TOKEN_RE.fullmatch` 更严（`media.py:66/379`）。下游 `os.path.join` 没有 `commonpath` 校验，但输入已被 fullmatch/黑名单收紧，单点绕过才成立 | V1 + V2 |
| N-03 | Muse-Canvas 静态文件服务路径穿越 | **不成立（已正确防护）**：`canvas_server.py:102-115` 先 `unquote` 再 `(ROOT / path).resolve()`，然后 `candidate != ROOT and ROOT not in candidate.parents → 403`——这是本清单里唯一主动做 containment 校验的上游文件服务，值得当正确样例抄 | V1 |
| N-04 | `/admin/media` 删除路径穿越 | **不成立**：czg `app.py:2267-2288`（批量）与 `:2291-2302`（单个）都带 `Depends(auth)` 且逐个文件名做 `"/" / "\\" / ".."` 检查 | V1 |
| N-05 | 参考图类型混淆（图片冒充视频成品） | **不成立（上游已拒）**：`czg engine.py:1888-1890`、`:1911-1913` 在 kind 不匹配时 raise「拒绝以图片冒充视频交付」；本仓 `media.py` 的 `kind_of`/`expected_kind` 更严 | V1 + V2 |
| N-06 | `admin/tasks/clear` 无鉴权 | **不成立**：czg `app.py:2245-2247` 带 `Depends(auth)` | V1 |

## 4. 修复优先级（给 lead）

**Top-3（先修这三条，收益最大）**

1. **V-01 权限坍缩** —— 一条修法同时收掉 V-08（推代码/改 `.env`/轮换 key）。
   本仓同型面 `app.py:45-56` 需要决策：拆 `/admin` 密钥 或 空密钥表时拒绝启动。
2. **V-02 媒体无鉴权** —— 上游三仓复现、外部无凭据可达；本仓已修，
   只要守住「不直连上游、不关鉴权」两条即可（`media_url_policy` 已固化）。
3. **V-03 + V-04 参考图 SSRF/任意文件读** —— 同一个取图函数的两条分支，一次修复
   覆盖两个洞；本仓 `refs.py:297-366` 已是正确样板，可作为 PR 参考实现。

**其次**：V-05（资源耗尽，修法简单）、V-10（回调 SSRF，改一行校验）、V-06（CDP 参数，改一行）。

**低危/文档级**：V-07（内部面，本仓已加密）、V-09（信息泄露，加依赖即可）。

**不建议的“修法”**：把 403 判成 cookie 失效、给进度加连续插值、把上游直链
暴露成交付 URL——这三件事分别是 muse2api 与我们的历史踩坑，见 `docs/HARVEST.md`。

## 5. 复核命令

```powershell
cd 'D:\deep seek\muse-bridge'

# 本清单涉及的上游行号（按条目逐个看）
$r = 'D:\deep seek\work\muse-re'
Get-Content "$r\sources\czg86389-hub__muse2api\app.py" | Select-Object -Skip 1818 -First 14   # V-02
Get-Content "$r\sources\czg86389-hub__muse2api\engine.py" | Select-Object -Skip 355 -First 12 # V-06
Get-Content "$r\sources\bynne2602__muse2api\engine.py" | Select-Object -Skip 1075 -First 20    # V-03/04
Get-ChildItem "$r\_src\files\novicezk__midjourney-proxy" -Recurse -Filter 'NotifyServiceImpl.java' |
  ForEach-Object { Get-Content $_.FullName | Select-Object -Skip 58 -First 24 }                # V-10

# 本仓对应修复面
Select-String -Path muse_bridge\app.py -Pattern 'def require_key|/v1/media/{token}|_normalize_state' -Context 0,6
Select-String -Path muse_bridge\refs.py -Pattern 'ALLOW_PRIVATE_FETCH|is_global|_PinnedRedirectBlock'
```

**限制声明**：全部结论为静态阅读 + 离线逻辑复现。没有对 muse.ai 或任何内网发过
请求；`V3` 标记的利用链（浏览器侧 CDP、盲 SSRF、明文凭据组合链）未在真实环境
验证，不能当作「已可利用」的证据。真实利用需要授权环境与动态验证，超出本任务
「只静态」的范围。
