r"""Noise_XX_25519_AESGCM_SHA256 —— muse Hatch 通道的握手与帧层（同步、纯函数、可离线测）。

为什么值得有这一层
------------------
muse.ai 的网页生成没有公开 REST，浏览器自动化是唯一被广泛实现的路径；但真正的
通道其实是一条 **Noise 握手之后的 WebSocket**（`wss://hatch.metaaivm.com/v1/noise`），
内层跑 protobuf 封装的 HTTP。证据与端到端跑通记录见
work/muse-re/HANDOFF_re-wire.md §1（WS URL 与帧长 66/166 出自真实 chunk）。
把握手与帧层做成纯函数，是后面所有协议化改动的前提：它不需要网络、不需要浏览器，
可以在 CI 里对拍，也不会把凭据带进测试。

两份参考实现的对拍结论（本模块的选择依据）
------------------------------------------
  * lichao199208__muse2api/app/muse_engine/protocol_client.py:36-171（Python）
  * manishkumar1601__muse-ai-api/src/noise/{cipher,symmetric,handshake}.ts（TS）

一致的地方照抄，不一致的地方逐条写在下面的注释里，共三处：

  1) **nonce 结构**（两边一致）：12 字节 = 4B 全 0 ‖ 8B 大端计数器。
     TS 用两个 setUint32(4, n>>32) / setUint32(8, n&0xffffffff) 拼出来，
     Python 用 `b"\0"*4 + n.to_bytes(8,"big")`，字节级等价。这一条必须由测试钉死，
     因为 AES-GCM 的 nonce 排错一位就是"能加密、不能互解"，而且不会报错。
  2) **静态密钥的生命周期**（两边不一致）：TS 在构造时
     `this.s = staticKey ?? generateKeyPair()`（可注入、整个握手期内稳定）；
     lichao 在 `msg3()` 里现场 `X25519PrivateKey.generate()`。
     **本模块取 TS 的语义**：Noise XX 里 `s` 是"静态"密钥，负责身份绑定；
     在 msg3 里现生成等于把它降级成第二个临时密钥，调用方无法复用同一个身份，
     也无法在测试里注入固定密钥做向量对拍。每次新建 NoiseXX 仍然得到一把新静态键，
     所以"每连接一个新身份"的行为并没有丢。
  3) **msg1 载荷**（两边不一致）：lichao 固定发 `b"\x0a\x20" + 32B CSPRNG`
     （protobuf field1 里放 32 字节 client nonce，实测帧长 66）；
     TS 的 `writeMessage1(payload)` 由调用方决定。
     **本模块取 TS 的接口 + 提供 lichao 的默认载荷生成器 `msg1_payload()`**：
     默认参数写成 `b""` 能让空载荷测试保持简单，真连服时由调用方显式带上 nonce。
     注意一个容易踩空的推论：msg1 阶段还没有密钥（cipher=None，明文直通），
     所以 `msg1(b"")` 只有 32 字节、`msg1(32B)` 才是 64 字节、
     `msg1(msg1_payload())` 是 66 字节（= 线上实测值）。

绝不静默降级的地方
------------------
  * 任何一次 X25519 DH 算出全 0（小序攻击 / 贡献点）→ 抛错，不返回空密钥。
  * 任何一次 AES-GCM 解密认证失败 → 抛错。握手期的解密失败就是"对端不是 muse"，
    吞掉它会表现为后面每一帧都解不开，排查成本极高。
  * Reassembler 的分块参数非法、同一 chunk_id 上 total 变了、重复块 → 全部抛错。
    默默丢弃会让一条流永远凑不齐，然后以"超时"的形式出现在最外层。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass, field

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

PROTOCOL = b"Noise_XX_25519_AESGCM_SHA256"

# 一帧明文的上限。线上取值 = 65535(WS 帧) - 46(NoiseTransportFrame 头与 tag 开销)。
# 分块超过这个数，服务端会直接断连而不是报错，所以它是硬约束而不是建议值。
MAX_PLAINTEXT_FRAME = 65489

PUBKEY_LEN = 32
TAG_LEN = 16
DH_LEN = 32
# msg2 的最小开销 = re(32) + enc(s)(48) = 80，**再算 16B tag**；所以 read_msg2 的
# 实际下限是 `MSG2_MIN_LEN + TAG_LEN == 96`，与浏览器前端
# `2knhq5aux36zv.js@4385` 的 `if(e.length<96) throw "message 2 too short"` 一致
# （2026-11-11 前端静态产物复核，见 docs/NOISE_BUNDLE_NOTES.md）。
MSG2_MIN_LEN = PUBKEY_LEN + (PUBKEY_LEN + TAG_LEN)
MAX_CHUNKS = 256

_ZERO_DH = b"\0" * DH_LEN


class NoiseError(RuntimeError):
    """Noise 层任何不可恢复的协议错误。上层按"这条连接废了"处理。"""


# ---------------------------------------------------------------- 基础原语


def sha256(value: bytes) -> bytes:
    return hashlib.sha256(value).digest()


def hkdf(chaining_key: bytes, ikm: bytes, outputs: int = 2) -> list[bytes]:
    """Noise 版 HKDF（不是 RFC 5869 的 Expand 全流程）：只有 Extract + N 次链式 Expand。

    为什么不用 hashlib 自带的或现成库：Noise 规定
        temp_key = HMAC(ck, ikm)
        output_i = HMAC(temp_key, output_{i-1} || i)      # output_0 为空串
    和 TLS 那套 HKDF-Expand(info, L) 不一样。写成纯 HMAC 循环，两份参考实现
    （protocol_client.py:48-54 与 symmetric.ts 的 hkdf）字节级一致，便于对拍。
    """
    if outputs < 1 or outputs > 255:
        raise NoiseError(f"hkdf outputs 必须在 1..255，收到 {outputs}")
    prk = hmac.new(chaining_key, ikm, hashlib.sha256).digest()
    result: list[bytes] = []
    last = b""
    for index in range(1, outputs + 1):
        last = hmac.new(prk, last + bytes([index]), hashlib.sha256).digest()
        result.append(last)
    return result


def _public_bytes(private: X25519PrivateKey) -> bytes:
    """原始 32 字节公钥。X25519 只有 Raw/Compressed 两种格式，这里固定 Raw。"""
    return private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )


def dh(private: X25519PrivateKey, public: bytes) -> bytes:
    """X25519 共享密钥。全 0 结果必须报错——那是小序攻击的指纹，不能当密钥用。"""
    if len(public) != PUBKEY_LEN:
        raise NoiseError(f"X25519 公钥长度必须是 {PUBKEY_LEN}，收到 {len(public)}")
    if public == _ZERO_DH:
        # 恒等点：任何私钥与它做 DH 都只能得到全零。cryptography 会抢先抛
        # ValueError("Error computing shared key.")，把原因糊成"公钥不合法"；
        # 提前判一次，报出来的才是真正的原因（小序 / 恒等点）。
        raise NoiseError("X25519 DH 结果为全零（恒等点，小序攻击形态），已拒绝")
    try:
        shared = private.exchange(X25519PublicKey.from_public_bytes(public))
    except ValueError as exc:  # 其它非法点：同样不能放行
        raise NoiseError("X25519 公钥不合法（可能是小序点）") from exc
    if shared == _ZERO_DH:
        raise NoiseError("X25519 DH 结果为全零（疑似小序攻击），已拒绝")
    return shared


# ---------------------------------------------------------------- CipherState


@dataclass
class Cipher:
    """AES-256-GCM，AD 由调用方给（握手期是 h，传输期是 b""）。

    key=None 表示"还没建立密钥"：Noise 规定此时明文直通。msg1 的载荷、
    以及任何加密前的探测帧都走这条分支——这不是"关掉加密"的开关。
    """

    key: bytes | None = None
    n: int = field(default=0, repr=False)

    def __post_init__(self) -> None:
        if self.key is not None and len(self.key) != 32:
            raise NoiseError(f"Cipher key 必须是 32 字节，收到 {len(self.key)}")

    @property
    def counter(self) -> int:
        return self.n

    @staticmethod
    def nonce(counter: int) -> bytes:
        """12B = 4B 全 0 ‖ 8B 大端计数器（见模块头对拍结论第 1 条）。"""
        if not 0 <= counter < (1 << 64):
            raise NoiseError("nonce 计数器超出 64 位范围")
        return b"\0" * 4 + counter.to_bytes(8, "big")

    def enc(self, ad: bytes, plain: bytes) -> bytes:
        if self.key is None:
            return plain
        nonce = self.nonce(self.n)
        self.n += 1
        return AESGCM(self.key).encrypt(nonce, plain, ad)

    def dec(self, ad: bytes, cipher: bytes) -> bytes:
        if self.key is None:
            return cipher
        nonce = self.nonce(self.n)
        self.n += 1
        try:
            return AESGCM(self.key).decrypt(nonce, cipher, ad)
        except InvalidTag as exc:
            # 不回显密文/AD：日志里留它们没有诊断价值，只会被当敏感数据。
            raise NoiseError(
                f"AES-GCM 认证失败（第 {self.n - 1} 号帧）：对端密钥或帧序不对"
            ) from exc


# ---------------------------------------------------------------- SymmetricState


class Symmetric:
    """h / ck / c 三件套。协议名 28 字节 ≤ 32，所以 h 的初值是"补零"而不是哈希。"""

    def __init__(self, protocol_name: bytes = PROTOCOL, prologue: bytes = b"") -> None:
        if len(protocol_name) > 255:
            raise NoiseError("协议名过长")
        base = (
            sha256(protocol_name)
            if len(protocol_name) > 32
            else protocol_name.ljust(32, b"\0")
        )
        self.ck = base
        self.h = base
        self.c = Cipher()
        # 空的 prologue 也要 mix 一次：lichao 在 Symmetric.__init__ 里 mix_hash(b"")，
        # TS 在 XxInitiator 构造里 mixHash(prologue)。两边净效果相同，都是"混一次"。
        # 因此调用方**不能**再额外 mix 一次 prologue，否则 h 会多滚一轮、握手必挂。
        self.mix_hash(prologue)

    def mix_hash(self, value: bytes) -> None:
        self.h = sha256(self.h + value)

    def mix_key(self, value: bytes) -> None:
        # 顺序是硬的：ck 先换成 temp_key 的 o1、密钥取 o2，且**立刻**换新 Cipher。
        # 复用旧 Cipher 只换 key 会让计数器继续往上走，对端从 0 开始 → 静默解不开。
        self.ck, key = hkdf(self.ck, value, 2)
        self.c = Cipher(key)

    def encrypt_and_hash(self, plain: bytes) -> bytes:
        cipher = self.c.enc(self.h, plain)
        self.mix_hash(cipher)
        return cipher

    def decrypt_and_hash(self, cipher: bytes) -> bytes:
        plain = self.c.dec(self.h, cipher)
        self.mix_hash(cipher)
        return plain

    def split(self) -> tuple[Cipher, Cipher]:
        first, second = hkdf(self.ck, b"", 2)
        return Cipher(first), Cipher(second)


def msg1_payload(client_nonce: bytes | None = None) -> bytes:
    """线上实测的 msg1 载荷：protobuf field1 里放 32 字节 CSPRNG nonce。

    `0a 20` = field 1 / wire type 2 / 长度 32。帧长 66 = 32(re) + 34(本载荷)。
    """
    nonce = client_nonce if client_nonce is not None else secrets.token_bytes(PUBKEY_LEN)
    if len(nonce) != PUBKEY_LEN:
        raise NoiseError(f"client nonce 必须是 {PUBKEY_LEN} 字节，收到 {len(nonce)}")
    return b"\x0a\x20" + nonce


# ---------------------------------------------------------------- 握手（主动方）


class NoiseXX:
    """XX 的 initiator。三个方法按 -> e / <- e, ee, s, es / -> s, se 的顺序调用。"""

    def __init__(self, static_key: X25519PrivateKey | None = None) -> None:
        self.s = Symmetric()
        self.e: X25519PrivateKey | None = None
        self.re: bytes | None = None
        self.rs: bytes | None = None
        # 取 TS 语义：静态密钥在构造期定下、整个握手期内稳定（见模块头对拍第 2 条）。
        self.st = static_key or X25519PrivateKey.generate()
        self._phase = 1

    @property
    def remote_static(self) -> bytes | None:
        return self.rs

    @property
    def handshake_hash(self) -> bytes:
        """握手完成后的 h。双方一致才算同一条握手；也是派生会话 id 的正确来源。"""
        return self.s.h

    @property
    def phase(self) -> int:
        return self._phase

    def _require(self, phase: int, name: str, hint: str = "") -> None:
        """状态机守卫。hint 用来写"那该先做什么"：只报"phase 不对"的话，
        调用方分不清是自己漏了一步还是对端乱序，这里直接把缺的那一步点名。
        """
        if self._phase != phase:
            raise NoiseError(
                f"{name} 只能在 phase {phase} 调用（当前 phase {self._phase}）{hint}"
            )

    def msg1(self, payload: bytes = b"") -> bytes:
        """-> e。ephemeral 先 mix_hash、**再**加密载荷（顺序反了对端 h 就对不上）。

        注意此时还没有任何 DH，c 仍是明文直通，所以 len(返回值) == 32 + len(payload)。
        """
        self._require(1, "msg1", "：每次握手只能发一次 msg1，重开一个 NoiseXX")
        self.e = X25519PrivateKey.generate()
        ephemeral = _public_bytes(self.e)
        self.s.mix_hash(ephemeral)
        self._phase = 2
        return ephemeral + self.s.encrypt_and_hash(payload)

    def read_msg2(self, message: bytes) -> bytes:
        """<- e, ee, s, es。"""
        self._require(2, "read_msg2", "：必须先调用 msg1")
        if self.e is None:
            raise NoiseError("read_msg2 之前必须先调用 msg1")
        if len(message) < MSG2_MIN_LEN + TAG_LEN:
            raise NoiseError(
                f"msg2 过短（{len(message)} 字节，至少需要 {MSG2_MIN_LEN + TAG_LEN}）"
            )
        self.re = message[:PUBKEY_LEN]
        self.s.mix_hash(self.re)                              # e
        self.s.mix_key(dh(self.e, self.re))                   # ee
        self.rs = self.s.decrypt_and_hash(                    # es: 对端静态密钥 32B+tag
            message[PUBKEY_LEN:PUBKEY_LEN + PUBKEY_LEN + TAG_LEN]
        )
        self.s.mix_key(dh(self.e, self.rs))                   # es
        payload = self.s.decrypt_and_hash(message[PUBKEY_LEN + PUBKEY_LEN + TAG_LEN:])
        self._phase = 3
        return payload

    def msg3(self, payload: bytes = b"") -> bytes:
        """-> s, se。

        两份参考实现在这里完全一致，顺序不可调换：
            encrypt_and_hash(静态公钥)   # 先把 s 的密文混进 h
            mix_key(dh(st_priv, re))     # se —— 插在两次 encrypt 之间
            encrypt_and_hash(payload)
        `mix_key` 必须在两个 encrypt_and_hash **之间**：它是 XX 模式里的 `se` token，
        位置决定载荷用的是哪一把密钥。 lichao(protocol_client.py:141-143) 与
        manishkumar(handshake.ts writeMessage3) 都是这个顺序，测试 2 做交叉对拍。
        """
        self._require(3, "msg3", "：必须先完成 read_msg2")
        if self.re is None:
            raise NoiseError("msg3 之前必须先 read_msg2")
        encrypted_static = self.s.encrypt_and_hash(_public_bytes(self.st))
        self.s.mix_key(dh(self.st, self.re))
        self._phase = 4
        return encrypted_static + self.s.encrypt_and_hash(payload)

    def split(self) -> tuple[Cipher, Cipher]:
        """(send, recv)。initiator 用第一个作发送——写反就是双方都在等自己的密文。"""
        return self.s.split()


# ---------------------------------------------------------------- 分块重组


class Reassembler:
    """把 NoiseTransportFrame 的分块拼回原始消息（对照 protocol_client.py:149-171）。

    为什么要 chunk_id：多条并发流的分块会交错到达，没有分组 id 就没法边收边拼。
    """

    def __init__(self) -> None:
        self.parts: dict[int, dict[str, dict[int, bytes] | int]] = {}

    def push(
        self, chunk_id: int, chunk_index: int, total_chunks: int, payload: bytes
    ) -> bytes | None:
        if not 1 <= total_chunks <= MAX_CHUNKS:
            raise NoiseError(
                f"total_chunks 必须在 1..{MAX_CHUNKS}，收到 {total_chunks}"
            )
        if not 0 <= chunk_index < total_chunks:
            raise NoiseError(
                f"chunk_index 越界：{chunk_index} 不在 0..{total_chunks - 1}"
            )
        if len(payload) > MAX_PLAINTEXT_FRAME:
            raise NoiseError(
                f"分块明文 {len(payload)} 字节，超过上限 {MAX_PLAINTEXT_FRAME}"
            )
        entry = self.parts.setdefault(chunk_id, {"total": total_chunks, "chunks": {}})
        if entry["total"] != total_chunks:
            # 同一个 chunk_id 上 total 变了：要么是上游把 id 复用错了，
            # 要么是被截断后重发。按任一情况拼出来的消息都是坏的，立刻报错。
            raise NoiseError(
                f"chunk_id={chunk_id} 的 total_chunks 不一致："
                f"{entry['total']} → {total_chunks}"
            )
        chunks: dict[int, bytes] = entry["chunks"]  # type: ignore[assignment]
        if chunk_index in chunks:
            raise NoiseError(f"chunk_id={chunk_id} 收到重复的 chunk_index={chunk_index}")
        chunks[chunk_index] = bytes(payload)
        if len(chunks) != total_chunks:
            return None
        del self.parts[chunk_id]  # 收齐即释放，防止长连接上无限堆积
        return b"".join(chunks[index] for index in range(total_chunks))

    def pending(self) -> dict[int, tuple[int, int]]:
        """调试用：chunk_id -> (已收到块数, 总块数)。"""
        return {
            key: (len(entry["chunks"]), int(entry["total"]))
            for key, entry in self.parts.items()
        }
