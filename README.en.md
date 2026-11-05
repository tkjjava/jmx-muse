# muse-bridge

<p align="center">
  <img alt="version" src="https://img.shields.io/badge/version-0.1.0-blue">
  <img alt="python" src="https://img.shields.io/badge/python-3.11%2B-3776AB">
  <img alt="tests" src="https://img.shields.io/badge/tests-46%20passed-brightgreen">
  <img alt="license" src="https://img.shields.io/badge/license-MIT-green">
  <img alt="transport" src="https://img.shields.io/badge/transport-CDP%20%2B%20Noise%20WS-orange">
  <img alt="no registration" src="https://img.shields.io/badge/registration-none-critical">
</p>

<p align="center">
  <b>Expose muse.ai as a standard OpenAI-compatible API</b><br>
  <sub>Author: Junmoxiao (君莫笑) · Eternal Orchid (永恒之兰)</sub>
</p>

<p align="center">
  <a href="README.md">中文文档</a> · <b>English</b>
</p>

---

Expose muse.ai (Meta "Hatch") as a standard OpenAI-compatible API.

**This project contains no signup capability.** Sessions are used from a Chrome profile
you log into yourself.

> ⚠️ This project **requires** a running, logged-in Chrome.
> muse's transport is an encrypted Noise WebSocket with no public protocol-only implementation.

```
OpenAI client ──▶ muse-bridge  :8788/v1  ──▶ CDP-driven real Chrome ──▶ muse.ai
```

## Why a browser is unavoidable

muse's chat transport is not REST and not SSE — it is an **encrypted Noise WebSocket**:

```
wss://hatch.metaaivm.com/v1/noise
X25519 + HKDF + AES-GCM + Ed25519
```

The WS URL embeds signed `auth_token` / `notary_token` values. **No public protocol-only
implementation exists** — every community project (muse2api and others) falls back to
browser automation, and its `http` driver still returns 501.

So this project takes the only viable path:

1. Drive a real Chrome via CDP (you log into muse.ai yourself)
2. Let the frontend perform the encryption — we never reimplement it
3. Watch the assistant message text grow in the DOM
4. Emit it as OpenAI SSE

**This is the only workable approach today, not a shortcut.**

## Hard boundaries

- **No signup.** muse requires Meta OAuth; this project does not touch any login flow.
- **No Cloudflare / risk-control evasion.** No fingerprint spoofing, no notary forgery,
  no challenge solving.
- **No account sharing.** Designed for a single user managing their own accounts.
- **No crypto reimplementation.** The Noise layer runs in the real frontend.

## Features

| | |
|---|---|
| **OpenAI compatible** | `/v1/chat/completions` (incl. SSE streaming), `/v1/models` |
| **CDP driven** | Reuses your logged-in Chrome; disconnects without closing it |
| **Centralised selectors** | A frontend change means editing `selectors.py` only |
| **Concurrency safe** | Per-account mutex covering the whole request including streaming |
| **Session refresh** | In-page fetch renews `hatch_vml` (48h) and wakes the VM |
| **Encrypted storage** | SQLite + Fernet; a missing key raises immediately |
| **Doctor** | `python -m muse_bridge.doctor` checks five preconditions |
| **Zero signup code** | No Meta OAuth automation of any kind |

## Install

```bash
git clone https://github.com/tkjjava/jmx-muse.git
cd jmx-muse
python -m pip install -r requirements.txt
python -m playwright install chromium
```

Requires Python 3.11+.

> `playwright install chromium` installs the driver capability only.
> At runtime this project connects to **your own** Chrome, not the bundled one.

## Configure

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
export MUSE_BRIDGE_KEY="<the key above>"
```

| Variable | Default | Meaning |
|---|---|---|
| `MUSE_BRIDGE_KEY` | required | Fernet key |
| `MUSE_BRIDGE_DB` | `./data/muse.db` | account database |
| `MUSE_BRIDGE_HOST` | `127.0.0.1` | bind address |
| `MUSE_BRIDGE_PORT` | `8788` | listen port |
| `MUSE_BRIDGE_API_KEYS` | empty | client keys, comma-separated |
| `MUSE_CDP_URL` | `http://127.0.0.1:9222` | Chrome debugging endpoint |
| `MUSE_HEADLESS` | `false` | headless mode (not recommended) |
| `MUSE_QUIET_MS` | `600` | quiet window for end-of-answer detection |
| `MUSE_REPLY_TIMEOUT` | `180` | reply wait timeout (seconds) |

## Quick start

### 1. Launch Chrome with a debugging port

```bash
# Linux
google-chrome --remote-debugging-port=9222 --user-data-dir="$HOME/muse-profile"

# macOS
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-port=9222 --user-data-dir="$HOME/muse-profile"

# Windows PowerShell
& "C:\Program Files\Google\Chrome\Application\chrome.exe" `
  --remote-debugging-port=9222 --user-data-dir="$env:USERPROFILE\muse-profile"
```

> **Use a separate `--user-data-dir`.** Without it, an already-running Chrome instance
> swallows the flag and the port never opens.

### 2. Log in to muse.ai in that Chrome

### 3. Start the bridge

```bash
# self-check: five preconditions, one at a time
python -m muse_bridge.doctor

# then start
python -m muse_bridge
```

### 4. Attach the account

```bash
curl -X POST http://127.0.0.1:8788/admin/attach \
  -H "Content-Type: application/json" \
  -d '{"account": "muse-main", "label": "main"}'
```

Or use <http://127.0.0.1:8788/console>.

### 5. Call it

```bash
curl http://127.0.0.1:8788/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"muse-1","messages":[{"role":"user","content":"Hello"}]}'
```

## Endpoints

| Endpoint | Purpose |
|---|---|
| `POST /v1/chat/completions` | OpenAI-compatible, streaming supported |
| `GET /v1/models` | model aliases (all route to `muse-spark-1.3`) |
| `GET /healthz` | health check |
| `GET /console` | attach console |
| `POST /admin/attach` | connect CDP and register the account |
| `POST /admin/accounts/{id}/refresh` | renew `hatch_vml` |
| `POST /admin/accounts/{id}/wake` | wake the cloud VM |
| `GET /admin/accounts` | pool status (redacted) |

## Client usage

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8788/v1",
    api_key="not-needed",   # unless MUSE_BRIDGE_API_KEYS is set
)

response = client.chat.completions.create(
    model="muse-1",
    messages=[{"role": "user", "content": "Hello"}],
)
print(response.choices[0].message.content)
```

`muse-1`, `gpt-4o`, `claude-opus-4`, `gemini-pro` and friends are **all equivalent** —
muse has a single agent and every alias routes to `muse-spark-1.3`.

## Architecture

```
muse_bridge/
  selectors.py   every DOM selector, each a candidate list
  driver.py      CDP connection, DOM reading, session refresh, VM wake
  state.py       9-state machine, probe freshness gate
  store.py       SQLite + Fernet
  pool.py        dispatch, per-account mutex, release semantics
  app.py         FastAPI routes
  doctor.py      five-precondition self-check
```

## Why one request at a time

One Chrome window has one input box and one message stream. Two concurrent requests would:

1. both `fill()` the same input — the later write overwrites the earlier prompt
2. both watch "the last assistant message" — each sees the other's answer
3. finish at different times — reply attribution breaks completely

So each account holds an `asyncio.Lock` **covering the entire request including streaming**.
The lock is released by `DispatchResult.release()`, shielded from cancellation so a
disconnecting client can never leave it held forever.

| Situation | Behaviour |
|---|---|
| another account is idle | automatically switched, request succeeds |
| every account is busy | HTTP 429, "muse can only run serially" |
| dispatch fails | lock is always released |

## Known limitations

- **A running Chrome is required.** Close the browser and the bridge stops working.
  This is a transport constraint, not a defect.
- **No model selection.** muse is a single server-chosen agent.
- **DOM changes are the main breakage point.** A frontend redesign invalidates selectors;
  update the table in `selectors.py` (see `docs/PROTOCOL.md`).
- **`hatch_vml` expires in ~48 hours.** The heartbeat tries to renew it, but upstream policy may change.
- **403 usually means your egress IP differs from the login browser's.**
  Chrome and muse-bridge must run on the same machine.
- **Keep concurrency low.** Only one conversation should generate at a time.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| cannot reach CDP | Chrome started without the flag, or already running | relaunch with `--remote-debugging-port` and a separate `--user-data-dir` |
| input box not found | selectors stale | inspect the element, add its selector to the top of `COMPOSER_INPUTS` |
| reply times out | `ASSISTANT_MESSAGES` stale, or VM asleep | fix selectors / call `/wake` |
| 403 errors | egress IP mismatch | run Chrome and the bridge on the same machine |

Full guide: [docs/USAGE.md](docs/USAGE.md) (Chinese) · Protocol details: [docs/PROTOCOL.md](docs/PROTOCOL.md) (Chinese)

## Security notes

1. **Never expose the Chrome debugging port to the internet.** It grants full browser control.
2. The `muse-profile` directory holds a complete login state — treat it like a password.
3. `MUSE_BRIDGE_KEY` decrypts the account store; watch its visibility.
4. Default bind is `127.0.0.1` — do not change it without understanding the risk.

## License

MIT © 2026 **tkjjava (君莫笑)** · **Eternal Orchid (永恒之兰)**

## Disclaimer

Not affiliated with, endorsed by, or authorized by muse.ai or Meta. This project accesses a
third party's service through browser automation. You are responsible for confirming and
complying with that service's terms of use and applicable law. Provided for interoperability
and educational purposes.

---

<p align="center">
  <a href="README.md">中文文档</a> · <b>English</b>
</p>
