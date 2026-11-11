"""媒体落盘与鉴权下载：token 校验、原子写、类型拒收、Range、超龄清理。

全部离线——不启动浏览器、不发网络。判定形状对齐上游实测：
`_pick_ext`（engine.py:1920-1931）与"期望视频拿到图片扩展必须拒收"
（engine.py:1911-1913，上游 U-MVD-02..05）。
"""

from __future__ import annotations

import hashlib
import os
import time

import pytest

from muse_bridge import media as media_mod
from muse_bridge.media import (
    PART_SUFFIX,
    MediaError,
    MediaKindError,
    MediaNotFound,
    MediaRangeError,
    MediaStore,
    is_unsatisfiable_range,
    kind_of,
    parse_range,
    serve_headers,
)

# 文件头取自 refs.sniff_mime 认的那些形态：内容决定类型，声明不算数。
PNG = b"\x89PNG\r\n\x1a\n" + b"p" * 64
JPG = b"\xff\xd8\xff" + b"j" * 64
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"m" * 64
WEBM = b"\x1a\x45\xdf\xa3" + b"w" * 64
GIF = b"GIF89a" + b"g" * 64
BIG = b"\x00\x00\x00\x18ftypmp42" + b"x" * 100


@pytest.fixture
def store(tmp_path) -> MediaStore:
    return MediaStore(tmp_path / "media")


def _files(root) -> list[str]:
    return sorted(entry.name for entry in os.scandir(root))


# ---- token 校验 ----


@pytest.mark.parametrize(
    "bad",
    [
        "../etc/passwd",
        "..",
        "a/b",
        "a\\b",
        "",
        "0" * 31,  # 短一位
        "0" * 33,  # 长一位
        "A" * 32,  # 大写不是 uuid4().hex 的形状
        "0" * 32 + ".png",
        "./" + "0" * 30,
        " ",
        "0123456789abcdef0123456789abcdef ",
        None,
        12345,
        b"0" * 32,
    ],
)
def test_is_token_rejects_everything_but_lowercase_hex(bad):
    assert MediaStore.is_token(bad) is False


def test_is_token_accepts_generated_shape(store):
    ref = store.save(PNG, "image/png")
    assert MediaStore.is_token(ref.token) is True
    assert store.exists(ref.token) is True


@pytest.mark.parametrize("bad", ["../x", "a/b", "0" * 31, "A" * 32])
def test_illegal_token_and_missing_file_are_indistinguishable(store, bad):
    """非法 token 与不存在的文件必须是同一个结论，否则 token 空间可被探测。"""
    illegal = None
    missing = None
    try:
        store.read(bad)
    except MediaError as exc:
        illegal = exc
    try:
        store.read("0" * 32)
    except MediaError as exc:
        missing = exc
    assert type(illegal) is type(missing), "两种失败给了两种信号"
    assert illegal.status_code == missing.status_code == 404


# ---- 写入 ----


def test_save_is_atomic_and_leaves_no_part(store):
    ref = store.save(PNG, "image/png")
    names = _files(store.root)
    assert names == [ref.filename]
    assert not any(name.endswith(PART_SUFFIX) for name in names), "半截文件残留"
    assert ref.path == str(store.root / ref.filename)
    assert ref.size == len(PNG)
    assert ref.sha256 == hashlib.sha256(PNG).hexdigest()
    assert ref.kind == "image" and ref.ext == ".png"
    assert ref.url == f"/v1/media/{ref.token}"
    assert store.read(ref.token) == PNG


def test_save_refuses_empty_and_non_bytes(store):
    with pytest.raises(MediaError):
        store.save(b"", "image/png")
    with pytest.raises(MediaError):
        store.save("这是文本不是字节", "image/png")  # type: ignore[arg-type]
    assert _files(store.root) == [], "被拒绝的输入不许留下文件"


def test_rejected_video_leaves_no_file_on_disk(store):
    """拒收必须发生在落盘之前：留下一个"图片命名的视频"是最坏结果。"""
    with pytest.raises(MediaKindError):
        store.save(PNG, "image/jpeg", expect="video")
    assert _files(store.root) == []


def test_create_dir_when_root_missing(tmp_path):
    root = tmp_path / "deep" / "media"
    assert not root.exists()
    MediaStore(root)
    assert root.is_dir()


def test_save_path_moves_source_and_keeps_rules(store, tmp_path):
    source = tmp_path / "download.mp4"
    source.write_bytes(BIG)
    ref = store.save_path(source, "video/mp4")
    assert ref.kind == "video" and ref.ext == ".mp4"
    assert ref.size == len(BIG)
    assert ref.sha256 == hashlib.sha256(BIG).hexdigest()
    assert not source.exists(), "源文件要交出去，不能两边各留一份"
    assert not any(name.endswith(PART_SUFFIX) for name in _files(store.root))
    with pytest.raises(MediaNotFound):
        store.save_path(tmp_path / "不在这里.mp4")


def test_save_path_rejects_cover_before_writing(store, tmp_path):
    source = tmp_path / "poster.jpg"
    source.write_bytes(JPG)
    with pytest.raises(MediaKindError):
        store.save_path(source, "image/jpeg", expect="video")
    assert _files(store.root) == []
    assert source.exists(), "判定失败时不该把源文件也吃掉"


# ---- 类型判定 ----


@pytest.mark.parametrize(
    "value,expected",
    [
        (".mp4", "video"),
        (".webm", "video"),
        (".mov", "video"),
        (".3gp", "video"),  # 上游 engine.py:1911 漏了它，这里钉住
        ("movie.MP4", "video"),
        ("notes.txt", "document"),
        (".png", "image"),
        (".gif", "image"),
        (".jpg", "image"),
        (".mp3", "audio"),
    ],
)
def test_kind_of_table(value, expected):
    assert kind_of(value) == expected


def test_kind_of_falls_back_to_mime():
    assert kind_of(mime="image/png") == "image"
    assert kind_of("", "application/pdf") == "document"
    assert kind_of(".png", "video/mp4") == "image", "扩展名优先于声明"


def test_video_only_accepts_allowed_extensions():
    assert media_mod.decide(mime="video/mp4") == (".mp4", "video/mp4", "video")
    assert media_mod.decide(mime="video/webm") == (".webm", "video/webm", "video")
    assert media_mod.decide(mime="video/quicktime") == (".mov", "video/quicktime", "video")
    assert media_mod.decide(mime="video/3gpp") == (".3gp", "video/3gpp", "video")
    # avi/mkv 不在允许集合：上游会静默落成 .png，这里必须拒收
    with pytest.raises(MediaKindError):
        media_mod.decide(mime="video/x-msvideo")


def test_cover_image_cannot_be_delivered_as_video():
    """U-MVD-02..05：期望视频、拿到图片扩展 → 拒收，且报错要说清是什么。"""
    with pytest.raises(MediaKindError) as excinfo:
        media_mod.decide(mime="image/jpeg", expect="video")
    assert "封面" in str(excinfo.value) or "图片" in str(excinfo.value)
    with pytest.raises(MediaKindError):
        media_mod.decide(url="https://muse.ai/files/poster.jpg", expect="video")
    with pytest.raises(MediaKindError):
        media_mod.decide(mime="video/mp4", head=PNG, expect="video")  # 文件头是 PNG


def test_image_expectation_rejects_video():
    with pytest.raises(MediaKindError):
        media_mod.decide(mime="video/mp4", expect="image")


def test_file_header_wins_over_declared_mime(store):
    """声明是 mp4、内容其实是 PNG：按内容落盘，别把图片命名为视频。"""
    ref = store.save(PNG, "video/mp4")
    assert ref.ext == ".png" and ref.kind == "image"
    assert ref.mime == "image/png"


def test_ext_from_expect_fallback_matches_upstream():
    assert media_mod.ext_for("", "", "video") == ".mp4"
    assert media_mod.ext_for("", "", "image") == ".png"
    # 上游 _pick_ext 的 URL 扫描顺序是 (.mp4,.webm,.png,.jpg,.webp)，URL 里只出现
    # `.webp` 就返回 `.webp`（大小写先被 lower）。这里原写 `.webm` 是笔误：
    # `.webm` 根本不在该 URL 里，任何实现都不可能返回它。
    assert media_mod.ext_from_url("https://muse.ai/files/a.WEBP?sig=1") == ".webp"


# ---- Range ----


@pytest.mark.parametrize(
    "header,size,expected",
    [
        ("bytes=0-9", 100, (0, 9)),
        ("bytes=50-", 100, (50, 99)),
        ("bytes=-10", 100, (90, 99)),
        ("bytes=0-999", 100, (0, 99)),  # 终点越界 clamp
        ("BYTES=10-20", 100, (10, 20)),  # 大小写不敏感
        # 起点越界**不得** clamp。`bytes=999999999-` 过去会走 `not last` 分支被
        # `min(start, size-1)` 夹回末尾，返回 206 + 末尾 1 字节。播放器会把它当成
        # 一段有效数据，而不是"不可满足"。RFC 9110 §14.1.2 要求起点 >= 长度时
        # 必须 416，故这里返回 None 之外的专用哨兵由上层映射（见下方 416 用例）。
        ("bytes=200-", 100, None),
        ("bytes=100-", 100, None),  # 起点恰等于长度：同样不可满足
        ("bytes=abc-def", 100, None),
        ("bytes=0-1,4-5", 100, None),  # 多区间：忽略而不是回半截
        ("items=0-1", 100, None),
        ("bytes-", 100, None),
        ("", 100, None),
        (None, 100, None),
        ("bytes=0-9", 0, None),
    ],
)
def test_parse_range_table(header, size, expected):
    assert parse_range(header, size) == expected


def test_read_range_clamps_end_and_reports_total(store):
    ref = store.save(BIG, "video/mp4")
    data, first, last, total = store.read_range(ref.token, 0, 10_000)
    assert (first, last, total) == (0, len(BIG) - 1, len(BIG))
    assert data == BIG


def test_read_range_middle_slice(store):
    ref = store.save(BIG, "video/mp4")
    data, first, last, total = store.read_range(ref.token, 10, 19)
    assert data == BIG[10:20] and (first, last, total) == (10, 19, len(BIG))


def test_read_range_unsatisfiable_raises(store):
    """起点越界/区间倒置要抛错：空 200 会被播放器当成 0 秒视频，比 416 难查得多。"""
    ref = store.save(BIG, "video/mp4")
    with pytest.raises(MediaRangeError):
        store.read_range(ref.token, len(BIG) + 5, len(BIG) + 9)
    with pytest.raises(MediaRangeError):
        store.read_range(ref.token, 20, 10)
    with pytest.raises(MediaRangeError):
        store.read_range(ref.token, 0, -1)


@pytest.mark.parametrize(
    "header,size,expected",
    [
        ("bytes=999999999-", 100, True),
        ("bytes=100-", 100, True),   # 起点恰等于长度
        ("bytes=50-10", 100, True),  # 起止倒置
        ("bytes=-0", 100, True),     # suffix 0 字节
        ("bytes=0-9", 100, False),
        ("bytes=50-", 100, False),
        ("bytes=-10", 100, False),
        ("bytes=0-999", 100, False),  # 终点越界是可满足的（clamp）
        ("bytes=0-1,4-5", 100, False),  # 多区间：不可解析，按 RFC 忽略
        ("items=0-1", 100, False),
        ("bytes=abc-def", 100, False),
        ("", 100, False),
        (None, 100, False),
        ("bytes=0-9", 0, False),
    ],
)
def test_is_unsatisfiable_range_table(header, size, expected):
    """把"该回 416"与"该忽略这个头"分开判。

    两者混成一个 None 分支时只有一个能对：要么垃圾头被判成 416，
    要么越界被判成 200。2026-11-11 活网验收抓到的正是后者。
    """
    assert is_unsatisfiable_range(header, size) is expected


# ---- 响应头 ----


def test_serve_headers_are_octet_stream_with_real_mime(store):
    ref = store.save(GIF, "image/gif")
    headers = serve_headers(size=ref.size, filename=ref.filename, mime=ref.mime)
    assert headers["Content-Type"] == "application/octet-stream"
    assert headers["X-Muse-Media-Mime"] == "image/gif"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Accept-Ranges"] == "bytes"
    assert "attachment" in headers["Content-Disposition"]
    assert headers["Content-Length"] == str(ref.size)


def test_serve_headers_filename_cannot_inject_response_headers():
    headers = serve_headers(size=1, filename='a"\r\nX-Evil: 1', mime="image/png")
    for value in headers.values():
        assert "\r" not in value and "\n" not in value
    assert "X-Evil" not in str(headers)


# ---- 清理 ----


def test_sweep_only_removes_expired_and_only_our_own_files(store, monkeypatch):
    fresh = store.save(PNG, "image/png")
    stale = store.save(GIF, "image/gif")
    unrelated = store.root / "notes.txt"
    unrelated.write_text("不是我们生成的", encoding="utf-8")
    orphan = store.root / f"{'0' * 32}.mp4{PART_SUFFIX}"  # 崩溃残留的半截文件
    orphan.write_bytes(b"half")

    old = time.time() - 4000
    for path in (stale.path, str(unrelated), str(orphan)):
        os.utime(path, (old, old))
    # 时钟要**往前走** 1000 秒：文件 mtime 是 T-4000，cutoff = (T+1000) - 3600
    # = T-2600，于是 4000 秒前的文件算超龄、fresh 的不算。原来写 `- 1000`
    # 是把钟拨回去，age 只剩 3000 秒不到阈值，一条都删不掉（算术自相矛盾）。
    monkeypatch.setattr(
        media_mod, "time", SimpleNamespaceShim(now=time.time() + 1000)
    )

    assert store.sweep(3600) == 2  # 过期成品 + 孤儿 .part
    assert store.exists(fresh.token)
    assert not store.exists(stale.token)
    assert unrelated.exists(), "清理不得变成任意删除"
    assert not orphan.exists()


def test_sweep_negative_budget_is_a_config_error(store):
    with pytest.raises(MediaError):
        store.sweep(-1)


def test_list_ref_delete_round_trip(store):
    a = store.save(PNG, "image/png")
    b = store.save(BIG, "video/mp4")
    listed = store.list_media()
    assert {item.token for item in listed} == {a.token, b.token}
    assert {item.kind for item in listed} == {"image", "video"}
    assert all(item.sha256 == "" for item in listed), "列表不许为算散列整读每个成品"
    assert store.ref(b.token).size == len(BIG)
    assert store.delete(a.token) is True
    assert store.delete(a.token) is False
    assert store.delete("不是合法token") is False


def test_png_deliverable_leaves_png_and_unknown_bytes_alone():
    """PNG 原样返回；不是 WebP 的字节不转（避免 JPEG 被放大成 PNG、坏字节被吞）。"""
    assert media_mod.png_deliverable(PNG) == PNG
    assert media_mod.png_deliverable(b"not an image") == b"not an image"


def test_png_deliverable_converts_webp_for_b64_json_clients():
    """muse 的图片成品常是 WebP，而 OpenAI 的 b64_json 没有 mime 字段。"""
    Image = pytest.importorskip("PIL.Image")
    import io

    raw = io.BytesIO()
    Image.new("RGBA", (4, 4), (255, 0, 0, 128)).save(raw, format="WEBP")
    webp = raw.getvalue()
    converted = media_mod.png_deliverable(webp)
    assert converted.startswith(media_mod.PNG_MAGIC), "WebP 必须转成真 PNG 交付"
    assert converted != webp


class SimpleNamespaceShim:
    """只要一个 time.time()。单独一个类，避免测试里到处 SimpleNamespace。"""

    def __init__(self, now: float) -> None:
        self._now = now
        self.time = lambda: self._now
