r"""noise.py 的离线对拍测试。不联网、不碰浏览器，纯 stdlib + cryptography。

覆盖点：

  N-01 nonce = 4B 全 0 ‖ 8B 大端计数器（cipher.ts 的 noiseNonce ↔ protocol_client.py:71）
  N-02 msg1 长度：空载荷 32 / 32B 载荷 64 / 线上载荷 66
       —— 本轮规格里写的"空 payload 必须 64"与两份参考实现冲突，理由见该测试的 docstring
  N-03 **跨实现对拍**（最高价值）：本模块做主动方，测试里按 Noise XX 规范
       手写一个被动方。只跟自己比的话，一个整体对称的错误（mix_key 顺序错一位）
       两边会一起错、测试全绿，所以这个类必须独立于 NoiseXX 存在。
  N-04..N-09 异常路径必须抛错，不得静默降级
  N-10 Reassembler 正常与非法分块（对照 protocol_client.py:149-171）
"""

from __future__ import annotations

import hashlib
import hmac

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from muse_bridge import noise

ZERO32 = b"\x00" * 32


def pub_of(private: X25519PrivateKey) -> bytes:
    """测试自己算公钥，不借道 noise._public_bytes——对拍才有独立意义。"""
    return private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )


class XxResponder:
    """按 XX pattern `-> e / <- e, ee, s, es / -> s, se` 手工实现的被动方。

    只复用底层件（Symmetric / Cipher / dh），握手顺序全部自己写。
    被动方的 DH 方向是易错点，逐条注明：
      ee = DH(e_resp, re_init)
      es = DH(s_resp, re_init)   # 模式里写作 DH(主动方 e, 被动方 s) 的镜像
      se = DH(e_resp, rs_init)   # 模式里写作 DH(主动方 s, 被动方 e) 的镜像
    """

    def __init__(self) -> None:
        self.s = noise.Symmetric()
        self.static = X25519PrivateKey.generate()
        self.e: X25519PrivateKey | None = None
        self.re_init: bytes | None = None
        self.remote_static: bytes | None = None

    def read_msg1(self, message: bytes) -> bytes:
        self.re_init = message[:32]
        self.s.mix_hash(self.re_init)
        return self.s.decrypt_and_hash(message[32:])

    def write_msg2(self, payload: bytes = b"") -> bytes:
        self.e = X25519PrivateKey.generate()
        ephemeral = pub_of(self.e)
        self.s.mix_hash(ephemeral)
        self.s.mix_key(noise.dh(self.e, self.re_init))                    # ee
        enc_static = self.s.encrypt_and_hash(pub_of(self.static))         # s
        self.s.mix_key(noise.dh(self.static, self.re_init))               # es
        return ephemeral + enc_static + self.s.encrypt_and_hash(payload)

    def read_msg3(self, message: bytes) -> bytes:
        self.remote_static = self.s.decrypt_and_hash(message[:48])
        self.s.mix_key(noise.dh(self.e, self.remote_static))              # se
        return self.s.decrypt_and_hash(message[48:])

    def split(self) -> tuple[noise.Cipher, noise.Cipher]:
        first, second = self.s.split()
        return second, first  # 被动方：send=c2, recv=c1


# ---------------------------------------------------------------- 常量与原语


def test_protocol_constants_match_references():
    assert noise.PROTOCOL == b"Noise_XX_25519_AESGCM_SHA256"
    assert noise.MAX_PLAINTEXT_FRAME == 65489
    # 27 是这条断言长期写错的字面量：b"Noise_XX_25519_AESGCM_SHA256" 数下来是
    # 28 字节（Noise_=6, XX_=3, 25519_=6, AESGCM_=7, SHA256=6）。
    # 断言的**意图**不变：协议名 ≤32 字节，所以初值 h 是"补零"而不是哈希
    # （noise.py:199 按 len > 32 分支）。实现本来就对，是这条注释与字面量数错了。
    assert len(noise.PROTOCOL) == 28, "≤32 才允许补零而不是哈希，这个前提变了要改代码"


def test_hkdf_matches_noise_construction():
    """N-04：独立重算 Noise 版 HKDF（不是 RFC 5869 的 Expand）。"""
    ck, ikm = bytes(range(32)), b"ee"
    prk = hmac.new(ck, ikm, hashlib.sha256).digest()
    o1 = hmac.new(prk, b"\x01", hashlib.sha256).digest()
    o2 = hmac.new(prk, o1 + b"\x02", hashlib.sha256).digest()
    assert noise.hkdf(ck, ikm, 2) == [o1, o2]
    assert noise.hkdf(ck, ikm, 1) == [o1]
    with pytest.raises(noise.NoiseError):
        noise.hkdf(ck, ikm, 0)


def test_symmetric_state_initial_hash_is_mixed_exactly_once():
    """h 初值 = sha256(补零协议名)：空 prologue 只混一次。

    两次混（lichao 在 Symmetric.__init__ 里混、TS 在 XxInitiator 里混）会滚出
    完全不同的 h，握手必挂。这里钉住"一次"。
    """
    fresh = noise.Symmetric()
    padded = noise.PROTOCOL.ljust(32, b"\0")
    assert fresh.ck == padded
    assert fresh.h == hashlib.sha256(padded).digest()
    assert noise.Symmetric(b"x" * 33).ck == hashlib.sha256(b"x" * 33).digest()


def test_nonce_layout_is_four_zero_bytes_then_big_endian_counter():
    """N-01：用"4B0 + 8B 大端"手工还原第一块密文，证明 nonce 排布没写反。"""
    key = bytes(range(32))
    cipher = noise.Cipher(key)
    encrypted = cipher.enc(b"ctx", b"frame-bytes")
    assert AESGCM(key).decrypt(b"\0" * 4 + (0).to_bytes(8, "big"), encrypted, b"ctx") == (
        b"frame-bytes"
    )
    assert cipher.nonce(1) == b"\0" * 4 + (1).to_bytes(8, "big")
    assert cipher.nonce(2**64 - 1) == b"\0" * 4 + b"\xff" * 8
    with pytest.raises(noise.NoiseError):
        cipher.nonce(1 << 64)


def test_cipher_without_key_passes_through_without_counting():
    plain = noise.Cipher()
    assert plain.enc(b"", b"raw") == b"raw"
    assert plain.dec(b"", b"raw") == b"raw"
    assert plain.counter == 0, "明文直通不推进计数器，否则 split 后错位"


def test_cipher_rejects_wrong_key_length():
    with pytest.raises(noise.NoiseError):
        noise.Cipher(b"short")


def test_dh_rejects_all_zero_result_and_bad_lengths():
    """N-05：全零 DH（小序攻击指纹）必须抛错，绝不能当共享密钥用。"""
    private = X25519PrivateKey.generate()
    with pytest.raises(noise.NoiseError, match="全零"):
        noise.dh(private, ZERO32)
    with pytest.raises(noise.NoiseError, match="长度"):
        noise.dh(private, b"\x01" * 31)
    assert len(noise.dh(private, pub_of(X25519PrivateKey.generate()))) == 32


# ---------------------------------------------------------------- msg1 形状


def test_msg1_lengths_reveal_the_real_wire_shape():
    """N-02：msg1 阶段 cipher.key is None → 载荷明文直通，长度 = 32 + len(payload)。

    规格写"msg1() 空 payload 必须 64"，两份参考实现都不支持这个说法：
      * protocol_client.py:69-70 与 cipher.ts 一致，无密钥时 enc 直接返回明文；
      * protocol_client.py:124 实际发的是 `b"\\x0a\\x20" + 32B`（34 字节）→ 帧长 66，
        与抓到的线上实测帧长 66 吻合（HANDOFF_re-wire.md §1）。
    所以 64 对应"32 字节载荷"，而不是"空载荷"。三档全部钉住，避免以后有人改回去。
    """
    assert len(noise.NoiseXX().msg1()) == 32
    assert len(noise.NoiseXX().msg1(b"")) == 32
    assert len(noise.NoiseXX().msg1(ZERO32)) == 64
    payload = noise.msg1_payload()
    assert payload[:2] == b"\x0a\x20" and len(payload) == 34
    assert len(noise.NoiseXX().msg1(payload)) == 66
    with pytest.raises(noise.NoiseError):
        noise.msg1_payload(b"too-short")


def test_msg1_nonce_is_random_each_time():
    assert noise.msg1_payload() != noise.msg1_payload()


def test_static_key_is_stable_and_injectable():
    """对拍结论第 2 条：取 TS 语义（构造期定下静态密钥），同时保留每连接新身份。"""
    fixed = X25519PrivateKey.generate()
    holder = noise.NoiseXX(static_key=fixed)
    assert holder.st is fixed
    assert noise.NoiseXX().st is not noise.NoiseXX().st


# ---------------------------------------------------------------- 握手对拍


def test_full_handshake_interop():
    """N-03：跨实现对拍。握手哈希、对端静态密钥、载荷、传输期全部要一致。"""
    initiator, responder = noise.NoiseXX(), XxResponder()
    payload1 = noise.msg1_payload()

    assert responder.read_msg1(initiator.msg1(payload1)) == payload1

    msg2 = responder.write_msg2(b"server-hello")
    assert len(msg2) == 32 + 48 + 16 + len(b"server-hello")
    assert initiator.read_msg2(msg2) == b"server-hello"
    assert initiator.remote_static == pub_of(responder.static)

    msg3 = initiator.msg3(b"client-auth")
    assert responder.read_msg3(msg3) == b"client-auth"
    assert responder.remote_static == pub_of(initiator.st)

    assert initiator.handshake_hash == responder.s.h, "握手哈希不一致 = 某一方 mix 错"
    assert len(initiator.handshake_hash) == 32

    i_send, i_recv = initiator.split()
    r_send, r_recv = responder.split()
    assert i_send.key != i_recv.key, "两个方向必须用不同密钥"

    frame = b'{"kind":"action"}'
    assert r_recv.dec(b"", i_send.enc(b"", frame)) == frame
    assert i_recv.dec(b"", r_send.enc(b"", b"pong")) == b"pong"
    assert i_send.counter == 1 == r_recv.counter


def test_handshake_with_empty_msg2_payload_still_works():
    """空载荷的 msg2 仍是 96 字节：enc_s 48 + 载荷密文(0 明文 + 16 tag)。"""
    initiator, responder = noise.NoiseXX(), XxResponder()
    responder.read_msg1(initiator.msg1())
    msg2 = responder.write_msg2()
    assert len(msg2) == 96
    assert initiator.read_msg2(msg2) == b""


def test_msg3_mix_key_position_is_observable():
    """msg3 里 mix_key(se) 必须夹在两次 encrypt_and_hash 之间。

    负向对照：把 mix_key 挪到两次加密**之后**。那样主动方自己仍然自洽
    （所以"自己跟自己比"发现不了），但被动方按规范解就必然认证失败。
    两份参考实现都写成 encrypt(s) -> mix_key(se) -> encrypt(payload)：
    protocol_client.py:141-143、handshake.ts 的 writeMessage3。
    """
    initiator, responder = _to_phase3()

    class WrongOrderInitiator(noise.NoiseXX):
        """只改 mix_key 的位置，其余与 NoiseXX 完全一致。"""

        def msg3(self, payload: bytes = b"") -> bytes:
            encrypted_static = self.s.encrypt_and_hash(pub_of(self.st))
            encrypted_payload = self.s.encrypt_and_hash(payload)
            self.s.mix_key(noise.dh(self.st, self.re))  # 错：应夹在两次加密之间
            self._phase = 4
            return encrypted_static + encrypted_payload

    broken = WrongOrderInitiator(static_key=initiator.st)
    broken.s.ck, broken.s.h, broken.s.c = initiator.s.ck, initiator.s.h, initiator.s.c
    broken.re = initiator.re
    with pytest.raises(noise.NoiseError, match="认证失败"):
        responder.read_msg3(broken.msg3(b"payload"))

    # 顺序正确时同一位置必须互通：证明上一步的失败只由 mix_key 位置造成
    control, control_responder = _to_phase3()
    assert control_responder.read_msg3(control.msg3(b"payload")) == b"payload"


def _to_phase3() -> tuple[noise.NoiseXX, XxResponder]:
    """推进到"msg2 已交换、只差 msg3"的状态（两条独立会话都要走到这里）。"""
    initiator, responder = noise.NoiseXX(), XxResponder()
    responder.read_msg1(initiator.msg1(noise.msg1_payload()))
    initiator.read_msg2(responder.write_msg2(b"server"))
    return initiator, responder


def test_split_before_msg3_still_derives_but_handshake_incomplete():
    initiator = noise.NoiseXX()
    assert initiator.phase == 1
    send, recv = initiator.split()
    assert send.key and recv.key and send.key != recv.key


@pytest.mark.parametrize("size", [0, 32, 95])
def test_short_msg2_is_rejected(size):
    """N-06：msg2 至少 96 字节（re 32 + enc_s 48 + tag 16）。"""
    initiator = noise.NoiseXX()
    initiator.msg1()
    with pytest.raises(noise.NoiseError, match="msg2 过短"):
        initiator.read_msg2(b"\x01" * size)


def test_msg2_from_another_handshake_fails_loudly():
    """N-07：对端密钥不对 → 认证失败必须抛错，不能返回垃圾字节。"""
    initiator = noise.NoiseXX()
    initiator.msg1()
    stranger = XxResponder()
    stranger.read_msg1(noise.NoiseXX().msg1())  # 别人的 msg1，状态与本 initiator 无关
    foreign_msg2 = stranger.write_msg2(b"x")
    with pytest.raises(noise.NoiseError, match="认证失败"):
        initiator.read_msg2(foreign_msg2)


def test_msg3_from_another_handshake_fails_loudly():
    """N-07：把 B 会话的 msg3 塞给 A 会话的被动方，必须认证失败而不是解出垃圾。

    两条会话各自完整走到 phase 3，互不干扰——之前复用同一个 responder 连发
    两次 write_msg2 会把它自己的 SymmetricState 推走，测的就不是"跨会话"了。
    """
    _init_a, responder_a = _to_phase3()
    init_b, _responder_b = _to_phase3()
    assert init_b.phase == 3 and responder_a.s.h != _responder_b.s.h
    with pytest.raises(noise.NoiseError, match="认证失败"):
        responder_a.read_msg3(init_b.msg3(b"nope"))


def test_transport_tampering_detected():
    key = bytes(range(32))
    send, recv = noise.Cipher(key), noise.Cipher(key)
    blob = bytearray(send.enc(b"", b"payload"))
    blob[-1] ^= 0x01
    with pytest.raises(noise.NoiseError, match="认证失败"):
        recv.dec(b"", bytes(blob))


def test_counter_desync_is_detected():
    """N-08：丢一帧之后，后面的帧必须全部认证失败——nonce 是计数器，错一位就全错。

    先把接收方推进到与"第二块"对齐的位置（等价于丢了第一块），
    再回头解第一块：此时双方的计数器已经反了，必须报错。
    """
    key = bytes(range(32))
    send, recv = noise.Cipher(key), noise.Cipher(key)
    first, second = send.enc(b"", b"1"), send.enc(b"", b"2")
    recv.n = 1  # 模拟"第一块没收到"
    assert recv.dec(b"", second) == b"2"
    with pytest.raises(noise.NoiseError, match="认证失败"):
        recv.dec(b"", first)


def test_transport_ad_is_empty_bytes():
    """传输期 AD 是 b""（protocol_client.py:382/457 两处都写 enc(b"", ...)）。"""
    key = bytes(range(32))
    send, recv = noise.Cipher(key), noise.Cipher(key)
    assert recv.dec(b"", send.enc(b"", b"payload")) == b"payload"
    with pytest.raises(noise.NoiseError, match="认证失败"):
        noise.Cipher(key).dec(b"non-empty", send.enc(b"", b"payload"))


def test_handshake_phase_violations_raise():
    initiator = noise.NoiseXX()
    initiator.msg1()
    with pytest.raises(noise.NoiseError, match="phase"):
        initiator.msg1()
    with pytest.raises(noise.NoiseError, match="phase"):
        initiator.msg3()
    with pytest.raises(noise.NoiseError, match="msg1"):
        noise.NoiseXX().read_msg2(b"\x01" * 96)


# ---------------------------------------------------------------- Reassembler


def test_reassembler_out_of_order():
    """N-10：三块乱序到达也要拼对，收齐后状态必须清空（不然长连接会涨）。"""
    stream = noise.Reassembler()
    assert stream.push(7, 2, 3, b"CCC") is None
    assert stream.pending() == {7: (1, 3)}
    assert stream.push(7, 0, 3, b"AAA") is None
    assert stream.push(7, 1, 3, b"BB") == b"AAABBCCC"
    assert stream.parts == {}


def test_reassembler_single_chunk():
    assert noise.Reassembler().push(1, 0, 1, b"only") == b"only"


def test_reassembler_two_streams_interleaved():
    stream = noise.Reassembler()
    assert stream.push(1, 0, 2, b"a") is None
    assert stream.push(2, 0, 2, b"x") is None
    assert stream.push(1, 1, 2, b"b") == b"ab"
    assert stream.push(2, 1, 2, b"y") == b"xy"


def test_reassembler_duplicate_chunk_rejected():
    """N-08：重复块必须报错。静默覆盖会让一条流悄悄错一半。"""
    stream = noise.Reassembler()
    stream.push(1, 0, 2, b"a")
    with pytest.raises(noise.NoiseError, match="重复"):
        stream.push(1, 0, 2, b"a2")


def test_reassembler_inconsistent_total_rejected():
    """N-09：同一 chunk_id 上 total 变了，拼出来的必然是坏消息。"""
    stream = noise.Reassembler()
    stream.push(1, 0, 3, b"a")
    with pytest.raises(noise.NoiseError, match="不一致"):
        stream.push(1, 1, 4, b"b")


def test_reasolver_signed_chunk_ids_supported():
    """线上 chunk_id 是 signed int64（protocol_client.py:449），必须能当字典键。"""
    stream = noise.Reassembler()
    negative = -(2**63)
    assert stream.push(negative, 0, 1, b"neg") == b"neg"


@pytest.mark.parametrize(
    ("index", "total", "payload", "pattern"),
    [
        pytest.param(0, 0, b"", "total_chunks", id="total-zero"),
        pytest.param(0, 257, b"", "total_chunks", id="total-too-big"),
        pytest.param(-1, 2, b"", "越界", id="index-negative"),
        pytest.param(2, 2, b"", "越界", id="index-equals-total"),
        pytest.param(
            0, 2, b"x" * (noise.MAX_PLAINTEXT_FRAME + 1), "超过上限", id="payload-too-big"
        ),
    ],
)
def test_reassembler_argument_validation(index, total, payload, pattern):
    with pytest.raises(noise.NoiseError, match=pattern):
        noise.Reassembler().push(1, index, total, payload)


def test_reassembler_accepts_boundary_sizes():
    """边界：正好 256 块、正好 MAX_PLAINTEXT_FRAME 都要放行。"""
    assert noise.Reassembler().push(9, 0, 1, b"y" * noise.MAX_PLAINTEXT_FRAME)
    stream = noise.Reassembler()
    for index in range(255):
        assert stream.push(3, index, 256, b".") is None
    assert stream.push(3, 255, 256, b".") == b"." * 256
