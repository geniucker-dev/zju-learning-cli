"""PyAV 生成离线 H.264 fixture；测试不调用 ffmpeg/ffprobe 等可执行程序。"""
import argparse
import contextlib
from fractions import Fraction
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import errno
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

import av
import httpx
import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

spec = importlib.util.spec_from_file_location("zju_frame_tests", ROOT / "zju.py")
zju = importlib.util.module_from_spec(spec)
spec.loader.exec_module(zju)
rf = zju


def video(path, *, fast=False, count=100, noisy=False, offset=0, vfr=False):
    opts = {"movflags": "+faststart"} if fast else {}
    with av.open(str(path), "w", format="mp4", options=opts) as c:
        s = c.add_stream("libx264", rate=10)
        s.width, s.height, s.pix_fmt = 192, 128, "yuv420p"
        s.options = {"preset": "ultrafast", "crf": "20"}
        s.codec_context.gop_size = 20
        s.codec_context.max_b_frames = 2
        rng = np.random.default_rng(1)
        for i in range(count):
            data = rng.integers(0, 256, (128, 192, 3), dtype=np.uint8) if noisy else np.zeros((128, 192, 3), dtype=np.uint8)
            # 在固定位置编码帧序号，截图颜色能验证是真实目标帧。
            data[:64, :64] = (i % 100 * 2, 60, 80)
            f = av.VideoFrame.from_ndarray(data, format="rgb24")
            f.pts = offset + (i * 2 if vfr and i >= 20 else i)
            f.time_base = Fraction(1, 10)
            for p in s.encode(f):
                c.mux(p)
        for p in s.encode():
            c.mux(p)


def args(**kw):
    data = dict(course=89253, sub=2038840, part=None, at=[1.25], frame=None, output=None,
                output_dir=None, around=None, interval=None, quality=95, format="jpg",
                max_fetch_mb=64, max_frames=100, force=False, json=False)
    data.update(kw)
    return argparse.Namespace(**data)


@contextlib.contextmanager
def server(path, mode="normal"):
    state = argparse.Namespace(mode=mode, requests=0, sent=0, ranges=[], headers=[], version='"v1"')
    size = path.stat().st_size

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            state.requests += 1
            state.headers.append(dict(self.headers))
            lo, hi = map(int, self.headers["Range"].removeprefix("bytes=").split("-"))
            hi = min(hi, size - 1)
            state.ranges.append((lo, hi))
            self.send_response(200 if state.mode == "ignore" else 206)
            if state.mode == "change" and state.requests > 1:
                state.version = '"v2"'
            if state.mode != "modified":
                self.send_header("ETag", 'W/' + state.version if state.mode == "weak" else state.version)
            self.send_header("Last-Modified", state.modified)
            self.send_header("Content-Range", f"bytes {lo + (state.mode == 'bad')}-{hi}/{size}")
            self.send_header("Content-Length", str(hi - lo + 1))
            self.end_headers()
            if state.mode == "ignore":
                return  # 更严格的“不读取 200 body”在 MockTransport 中验证。
            with path.open("rb") as f:
                f.seek(lo)
                data = f.read(hi - lo + 1)
            if state.mode == "short":
                data = data[:len(data) // 2]
            try:
                self.wfile.write(data)
                state.sent += len(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

    state.modified = "Mon, 19 Oct 2026 00:00:00 GMT"
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}/video.mp4?signature=SECRET", state
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()


class Frames(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = tempfile.TemporaryDirectory()
        cls.root = Path(cls.fixture.name)
        cls.small = cls.root / "small.mp4"
        cls.fast = cls.root / "fast.mp4"
        cls.large = cls.root / "large.mp4"
        cls.vfr = cls.root / "vfr.mp4"
        video(cls.small)
        video(cls.fast, fast=True)
        video(cls.large, count=600, noisy=True)
        video(cls.vfr, count=40, offset=50, vfr=True)

    @classmethod
    def tearDownClass(cls):
        cls.fixture.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dest = Path(self.tmp.name).resolve()

    def run_frames(self, source=None, **kw):
        kw.setdefault("output_dir", str(self.dest))
        return rf.extract_recording_frames(args(**kw), source or self.small, kw.get("part") or 1)

    def assert_frame(self, result, expected_sec, index=0, frame_number=None):
        f = result["frames"][index]
        self.assertAlmostEqual(f["actual_time_sec"], expected_sec, places=5)
        with Image.open(f["path"]) as image:
            self.assertEqual(image.size, (192, 128))
            color = image.getpixel((30, 30))
            number = round(expected_sec * 10) if frame_number is None else frame_number
            self.assertLess(abs(color[0] - number % 100 * 2), 5)

    def test_local_precise_b_frames_and_batch(self):
        # 1.25 位于 1.2 和 1.3 之间，且不是 0/2 秒的关键帧。
        r = self.run_frames(at=[7.95, 1.25, 1.3, 1.25, 9.99])
        for i, t in enumerate([7.9, 1.2, 1.3, 1.2, 9.9]):
            self.assert_frame(r, t, i)
        self.assertEqual(r["network"], {"requests": 0, "bytes_received": 0})
        self.assertEqual(r["decode"]["seeks"], 1)
        self.assertEqual(len({f["path"] for f in r["frames"]}), 5)
        saved = json.loads(rf.sidecar(Path(r["frames"][0]["path"])).read_text())
        self.assertEqual(saved["request"]["frame_count"], 5)
        self.assertEqual(saved["request"]["network"]["requests"], 0)

    def test_distant_times_reseek_without_reopening_and_no_external_tools(self):
        with server(self.large) as (url, _), mock.patch.object(av, "open", wraps=av.open) as opened, \
                mock.patch.object(subprocess, "run", side_effect=AssertionError("禁止外部音视频程序")):
            r = self.run_frames(url, at=[1.25, 58.25])
        self.assert_frame(r, 1.2, 0)
        self.assert_frame(r, 58.2, 1)
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(r["decode"]["seeks"], 2)

    def test_nonzero_start_and_variable_frame_rate(self):
        r = self.run_frames(self.vfr, at=[0, 1.95, 2.5, 4.05, 6.25])
        # 源 PTS 从 5 秒开始，帧 19 到 20 的间隔是 2.1 秒。
        for i, (sec, number) in enumerate([(0, 0), (1.9, 19), (1.9, 19), (4, 20), (6.2, 31)]):
            self.assert_frame(r, sec, i, number)

    def test_remote_tail_and_faststart_and_io_reuse(self):
        for fixture in (self.fast, self.large):
            with self.subTest(file=fixture.name), server(fixture) as (url, state):
                t = 8.05 if fixture == self.fast else 58.05
                with mock.patch.object(av, "open", wraps=av.open) as opened:
                    r = self.run_frames(url, at=[t, t + .2, t + .4])
                self.assertEqual(opened.call_count, 1)
                self.assert_frame(r, t - .05)
                self.assertEqual(r["decode"]["seeks"], 1)
                self.assertEqual(r["network"]["requests"], state.requests)
                self.assertEqual(r["network"]["bytes_received"], state.sent)
                self.assertTrue(all("Cookie" not in h and "Authorization" not in h for h in state.headers))
                self.assertTrue(all(h.get("If-Range") == '"v1"' for h in state.headers[1:]))
                if fixture == self.large:
                    self.assertLess(r["network"]["bytes_received"], fixture.stat().st_size // 2)
                    self.assertTrue(any(lo > fixture.stat().st_size // 2 for lo, hi in state.ranges))
                for f in r["frames"]:
                    self.assertNotIn("SECRET", rf.sidecar(Path(f["path"])).read_text())

    def test_bad_range_truncated_changed_and_budget(self):
        for mode, message in [("bad", "Content-Range"), ("short", "不完整"), ("change", "版本已变化")]:
            with self.subTest(mode=mode), server(self.large, mode) as (url, state):
                with self.assertRaisesRegex(rf.FrameError, message):
                    self.run_frames(url, at=[58])
        with server(self.large) as (url, state):
            with self.assertRaisesRegex(rf.FrameError, "上限"):
                self.run_frames(url, max_fetch_mb=.01)
            self.assertLessEqual(state.sent, .01 * 2**20)
        with server(self.large) as (url, state), rf.FrameRangeReader(url, 2**20 + 16) as reader:
            self.assertEqual(len(reader.read(1)), 1)
            reader.seek(2**20)
            self.assertEqual(reader.read(1), b"")
            self.assertIn("上限", str(reader.last_error))
            self.assertEqual(reader.bytes_received, reader.max_bytes)
            before = reader.requests
            self.assertEqual(reader.read(1), b"")
            self.assertEqual(reader.requests, before)
        # 缓存占用不会随整个视频大小增长；重复读取命中已取块。
        with server(self.large) as (url, state), rf.FrameRangeReader(url) as reader:
            reader.cache_size = 2 * reader.block_size
            for pos in range(0, min(reader.total, 2**20), reader.block_size):
                reader.seek(pos)
                reader.read(1)
            self.assertLessEqual(sum(map(len, reader.cache.values())), reader.cache_size)
            before = reader.requests
            reader.seek(pos)
            reader.read(1)
            self.assertEqual(before, reader.requests)

    def test_200_and_redirect_never_read_body_and_safe_error(self):
        class Forbidden(httpx.SyncByteStream):
            read = False
            def __iter__(self):
                self.read = True
                raise AssertionError("不应读取响应体")
                yield b""
        for status in (200, 302, 401, 403):
            body = Forbidden()
            with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(
                    status, headers={"Location": "https://evil.invalid/?SECRET"}, stream=body))) as client:
                with self.assertRaisesRegex(rf.FrameError, str(status)) as e:
                    rf.FrameRangeReader("https://example.invalid/v?SECRET", client=client)
            self.assertFalse(body.read)
            self.assertNotIn("SECRET", str(e.exception))

    def test_contiguous_index_reads_merge_without_more_bytes_or_budget_overrun(self):
        block = rf.FrameRangeReader.block_size
        # 大索引测试只检查真实 Range IO；解码正确性由 H.264 head/tail fixture 验证。
        header = zju.mp4_box(b"ftyp", b"isom" * 4)
        moov = zju.mp4_box(b"moov", b"index" * (block + 1))
        media = zju.mp4_box(b"mdat", b"m" * (2 * block))
        for tail in (False, True):
            # 尾部情况同时覆盖 64-bit extended-size box。
            index = ((1).to_bytes(4, "big") + b"moov" + (len(moov) + 8).to_bytes(8, "big") + moov[8:]
                     if tail else moov)
            content = header + (media + index if tail else index + media)
            path = self.dest / f"index-{tail}.mp4"
            path.write_bytes(content)
            start = len(header) + (len(media) if tail else 0)
            stop = start + len(index)
            with server(path) as (url, state), rf.FrameRangeReader(url) as reader:
                reader.cache_size = 4 * block
                reader.read(32768)  # 记录 mdat 后 moov 的已知边界。
                reader.seek(start)
                data = bytearray()
                while len(data) < len(index):
                    data.extend(reader.read(min(32768, len(index) - len(data))))
                self.assertEqual(bytes(data), index)
                self.assertTrue(any(hi - lo + 1 > block for lo, hi in state.ranges))
                # 最后一个块可能到文件末尾，且头部发现块可能已含 moov 开始。
                expected = sum(min(block, len(content) - base) for base in
                               ({0} | set(range(start // block * block, stop, block))))
                self.assertEqual(reader.bytes_received, expected + 16)
                self.assertLessEqual(sum(map(len, reader.cache.values())), reader.cache_size)
                self.assertEqual(reader.bytes_received, state.sent)
                # 回读被淘汰的块时，不重新下载其后仍在缓存中的索引块。
                first_cached = min(reader.cache)
                before = reader.bytes_received
                reader.seek(first_cached - block)
                reader.read(1)
                self.assertEqual(reader.bytes_received - before, block)
            with server(path) as (url, _), rf.FrameRangeReader(url, 3 * block + 16) as reader:
                reader.read(32768)
                reader.seek(start)
                for _ in range(10):
                    if not reader.read(block):
                        break
                self.assertIsNotNone(reader.last_error)
                self.assertLessEqual(reader.bytes_received, 3 * block + 16)

    def test_missing_validator_wrong_length_and_retry_accounting(self):
        for headers, body in [({"Content-Range": "bytes 0-15/20"}, b"a" * 16),
                              ({"Content-Range": "bytes 0-15/20", "ETag": '"v"', "Content-Length": "17"}, b"a" * 17)]:
            with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(206, headers=headers, content=body))) as client:
                with self.assertRaises(rf.FrameError):
                    rf.FrameRangeReader("https://example.invalid/v", client=client)
        modified = "Mon, 19 Oct 2026 00:00:00 GMT"
        for etag in ('W/"weak"', 'malformed-unquoted-tag'):
            with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(206, headers={
                    "Content-Range": "bytes 0-15/100", "ETag": etag, "Last-Modified": modified},
                    content=b"0" * 16))) as client:
                with rf.FrameRangeReader("https://example.invalid/v", client=client) as reader:
                    self.assertEqual(reader.identity["validator"], ["Last-Modified", modified])
                    self.assertFalse(reader.cache_reusable)
        with server(self.large) as (url, _), rf.FrameRangeReader(url) as reader:
            class Partial(httpx.SyncByteStream):
                def __iter__(self):
                    yield b"1234"
                    raise httpx.ReadError("SECRET")
            def response(req):
                return httpx.Response(206, headers={"Content-Range": f"bytes 0-15/{reader.total}", "ETag": '"v1"',
                                                   "Content-Length": "16"}, stream=Partial())
            with httpx.Client(transport=httpx.MockTransport(response)) as client:
                reader.client = client
                before = reader.bytes_received
                with self.assertRaisesRegex(rf.FrameError, "3 次"):
                    reader._fetch(0, 15)
                self.assertEqual(reader.bytes_received - before, 12)

    def test_paths_cache_force_and_source_changes(self):
        out = self.dest / "中文 空格" / "board=a.png"
        kw = dict(output_dir=None, output=str(out))
        r = self.run_frames(**kw)
        self.assertEqual(r["frames"][0]["path"], str(out))
        with Image.open(out) as image:
            self.assertEqual(image.format, "PNG")
        self.assertTrue(self.run_frames(**kw)["frames"][0]["cache_hit"])
        out.write_bytes(b"unrelated image")
        with self.assertRaisesRegex(rf.FrameError, "--force"):
            self.run_frames(**kw)
        self.assertFalse(self.run_frames(force=True, **kw)["frames"][0]["cache_hit"])
        # 内容或源身份不符时，自动名称生成新文件；显式路径明确报错。
        first = self.run_frames()["frames"][0]["path"]
        Path(first).write_bytes(b"other")
        other = self.run_frames()["frames"][0]["path"]
        self.assertNotEqual(first, other)
        copy = self.dest / "copy.mp4"
        copy.write_bytes(self.small.read_bytes())
        self.run_frames(copy, **dict(kw, force=True))
        os.utime(copy, ns=(copy.stat().st_atime_ns, copy.stat().st_mtime_ns + 1000000))
        with self.assertRaisesRegex(rf.FrameError, "缓存"):
            self.run_frames(copy, **kw)

    def test_frame_limits_precede_source_access_and_count_outputs(self):
        oversized = [dict(at=[1] * 101), dict(at=None, frame=[(1, str(self.dest / f"{i}.jpg")) for i in range(101)]),
                     dict(at=[5], around=5, interval=.01)]
        for kw in oversized:
            with self.subTest(kw=kw), mock.patch.object(rf, "recording_frame_source") as source:
                with self.assertRaisesRegex(zju.ZjuError, "max-frames"):
                    zju.cmd_recording_frame(args(**kw))
                source.assert_not_called()
        for value in (0, -1, 1.5, True):
            with self.assertRaisesRegex(rf.FrameError, "正整数"):
                rf.validate_frame_args(args(max_frames=value))
        # 普通重复项实际产生不同图片，逐项计数；窗口重叠取并集。
        self.assertEqual(len(rf.recording_frame_times(args(at=[1] * 101, max_frames=101))), 101)
        r = self.run_frames(at=[1] * 3, max_frames=3)
        self.assertEqual(r["frame_count"], 3)
        r = self.run_frames(at=[1, 1, 2], around=1, interval=1, max_frames=4)
        self.assertEqual([f["requested_time_sec"] for f in r["frames"]], [0, 1, 2, 3])
        # 在获取时长前检查，视频尾裁剪不能绕过请求上限。
        with self.assertRaisesRegex(rf.FrameError, "max-frames"):
            self.run_frames(at=[9], around=2, interval=1, max_frames=4)
        # 极密采样也不能无限迭代，舍入后重复点直接跳过。
        with self.assertRaisesRegex(rf.FrameError, "max-frames"):
            rf.validate_frame_args(args(at=[1], around=1, interval=1e-30))
        self.assertEqual(rf.recording_frame_times(args(at=[0], around=1e-12, interval=1e-30)), [(0, None)])

    def test_pair_write_failures_and_force_preserve_original(self):
        path = self.dest / "pair.jpg"
        opts = dict(output_dir=None, output=str(path))
        with mock.patch.object(rf, "atomic_json", side_effect=OSError("disk full")), self.assertRaises(rf.FrameError):
            self.run_frames(**opts)
        self.assertFalse(path.exists())
        self.assertFalse(rf.sidecar(path).exists())
        self.assertFalse(list(self.dest.glob(".zju-frame-*")))
        self.run_frames(**opts)
        original, metadata = path.read_bytes(), rf.sidecar(path).read_bytes()
        with mock.patch.object(rf, "atomic_json", side_effect=OSError("disk full")), self.assertRaises(rf.FrameError):
            self.run_frames(at=[2], force=True, **opts)
        self.assertEqual((path.read_bytes(), rf.sidecar(path).read_bytes()), (original, metadata))
        # 模拟图片已替换、sidecar rename 失败，必须恢复原来的有效一对文件。
        replace = os.replace
        def fail_sidecar(src, dest):
            if Path(dest) == rf.sidecar(path):
                raise OSError("sidecar publish failed")
            return replace(src, dest)
        with mock.patch.object(rf.os, "replace", side_effect=fail_sidecar), self.assertRaises(rf.FrameError):
            self.run_frames(at=[2], force=True, **opts)
        self.assertEqual((path.read_bytes(), rf.sidecar(path).read_bytes()), (original, metadata))
        self.assertTrue(self.run_frames(**opts)["frames"][0]["cache_hit"])
        self.assertFalse(list(self.dest.glob(".zju-frame-*")))
        # 相同失败发生在新建结果时，只移除本次发布的图片，不影响其他文件。
        other = self.dest / "other.jpg"
        def fail_new_sidecar(src, dest):
            if Path(dest) == rf.sidecar(other):
                raise OSError("sidecar publish failed")
            return replace(src, dest)
        with mock.patch.object(rf.os, "replace", side_effect=fail_new_sidecar), self.assertRaises(rf.FrameError):
            self.run_frames(output_dir=None, output=str(other))
        self.assertFalse(other.exists())
        self.assertFalse(rf.sidecar(other).exists())
        self.assertEqual(path.read_bytes(), original)

    def test_inconsistent_cache_and_competing_publishers(self):
        path = self.dest / "pair.jpg"
        opts = dict(output_dir=None, output=str(path))
        self.run_frames(**opts)
        record = json.loads(rf.sidecar(path).read_text())
        record["image_sha256"] = "incorrect"
        rf.sidecar(path).write_text(json.dumps(record))
        with self.assertRaisesRegex(rf.FrameError, "缓存"):
            self.run_frames(**opts)
        self.assertFalse(self.run_frames(force=True, **opts)["frames"][0]["cache_hit"])
        old = path.read_bytes(), rf.sidecar(path).read_bytes()
        with rf.frame_output_lock(path):
            with self.assertRaisesRegex(rf.FrameError, "另一进程"):
                self.run_frames(at=[2], force=True, **opts)
        self.assertEqual((path.read_bytes(), rf.sidecar(path).read_bytes()), old)
        # 另一发布者在解码完成后抢先创建文件，必须拒绝而非覆盖/清理它。
        competing = self.dest / "competing.jpg"
        publish = rf.publish_frame_pair
        def compete(staged, dest, record, overwrite):
            dest.write_bytes(b"from another process")
            return publish(staged, dest, record, overwrite)
        with mock.patch.object(rf, "publish_frame_pair", side_effect=compete), self.assertRaises(rf.FrameError):
            self.run_frames(output_dir=None, output=str(competing))
        self.assertEqual(competing.read_bytes(), b"from another process")

    def test_last_modified_is_not_cross_invocation_content_identity(self):
        for mode in ("modified", "weak"):
            with self.subTest(mode=mode), server(self.large, mode) as (url, state):
                out = self.dest / f"{mode}.jpg"
                opts = dict(at=[58], output_dir=None, output=str(out))
                self.run_frames(url, **opts)  # 弱验证器仍可进行远端读取。
                with self.assertRaisesRegex(rf.FrameError, "缓存"):
                    self.run_frames(url, **opts)
                self.assertFalse(self.run_frames(url, force=True, **opts)["frames"][0]["cache_hit"])
                with rf.FrameRangeReader(url) as reader:
                    self.assertFalse(reader.cache_reusable)
                    state.modified = "Tue, 20 Oct 2026 00:00:00 GMT"
                    with self.assertRaisesRegex(rf.FrameError, "版本已变化"):
                        reader._fetch(16, 31)

    def test_remote_cache_signed_url_refresh_and_version_change(self):
        with server(self.large) as (url, state):
            out = self.dest / "remote.jpg"
            kw = dict(at=[58.05], output_dir=None, output=str(out))
            first = self.run_frames(url, **kw)
            second = self.run_frames(url.replace("SECRET", "NEW_SIGNATURE"), **kw)
            self.assertTrue(second["frames"][0]["cache_hit"])
            self.assertEqual(second["decode"]["frames"], 0)
            state.version = '"new-version"'
            with self.assertRaisesRegex(rf.FrameError, "缓存"):
                self.run_frames(url, **kw)
            self.assertFalse(self.run_frames(url, force=True, **kw)["frames"][0]["cache_hit"])
            self.assertLess(first["network"]["bytes_received"], self.large.stat().st_size)

    def test_auto_names_no_collisions_and_batch_explicit_paths(self):
        paths = set()
        for course, sub, part in [(1, 1, 1), (1, 2, 1), (2, 1, 1), (1, 1, 2)]:
            r = self.run_frames(course=course, sub=sub, part=part)
            p = Path(r["frames"][0]["path"])
            self.assertTrue(p.name.startswith(f"{course}_{sub}_p{part:02d}_"))
            self.assertNotIn(":", p.name)
            paths.add(p)
        self.assertEqual(len(paths), 4)
        pair = [(1.25, str(self.dest / "first.jpg")), (1.35, str(self.dest / "elsewhere" / "板书 = 2.png"))]
        r = self.run_frames(at=None, frame=pair, output_dir=None)
        self.assertEqual([f["path"] for f in r["frames"]], [p for _, p in pair])
        self.assertEqual(rf.frame_arg('1.25=C:\\课程 空格\\a=b.jpg'), (1.25, 'C:\\课程 空格\\a=b.jpg'))
        with self.assertRaisesRegex(rf.FrameError, "同一个"):
            self.run_frames(at=None, frame=[(1, pair[0][1]), (2, pair[0][1])], output_dir=None)

    def test_window_clipping_and_invalid_options_and_times(self):
        r = self.run_frames(at=[1], around=2, interval=1)
        self.assertEqual([f["requested_time_sec"] for f in r["frames"]], [0, 1, 2, 3])
        for overrides in [dict(at=[10]), dict(at=[1, 2], output="a.jpg"), dict(around=1), dict(interval=1),
                          dict(at=None, frame=[(1, "a.jpg")]), dict(quality=0), dict(max_fetch_mb=0),
                          dict(output_dir=None, output="a.gif"), dict(around=float("nan"), interval=1)]:
            with self.subTest(overrides=overrides), self.assertRaises(rf.FrameError):
                self.run_frames(**overrides)
        for value in ("-1", "nan", "inf", "01:60:00", "a", "01:01", "00:00:61"):
            with self.assertRaises(argparse.ArgumentTypeError):
                rf.parse_frame_time(value)
        self.assertEqual(rf.parse_frame_time("01:23:45.125"), 5025.125)

    def test_atomic_encoding_failure(self):
        p = self.dest / "frame.jpg"
        def fail(*a, **kw):
            a[1].write(b"broken")
            raise OSError("disk error")
        with mock.patch.object(Image.Image, "save", fail), self.assertRaises(rf.FrameError):
            self.run_frames(output_dir=None, output=str(p))
        self.assertFalse(p.exists())
        self.assertEqual(list(self.dest.iterdir()), [])
        p.write_bytes(b"original")
        with mock.patch.object(Image.Image, "save", fail), self.assertRaises(rf.FrameError):
            self.run_frames(output_dir=None, output=str(p), force=True)
        self.assertEqual(p.read_bytes(), b"original")

    def test_relative_paths_and_duplicate_millisecond_names(self):
        # Windows runner 的仓库与系统临时目录可能分别在 D: / C:；跨盘不存在
        # 相对路径。在临时目录内验证真实相对路径解析，并保证异常时恢复 cwd。
        previous = Path.cwd()
        try:
            os.chdir(self.dest)
            r = self.run_frames(output_dir=None, output=str(Path("rel") / "frame.jpg"))
        finally:
            os.chdir(previous)
        self.assertEqual(Path(r["frames"][0]["path"]), (self.dest / "rel" / "frame.jpg"))
        r = self.run_frames(at=[1.25001, 1.25002])
        self.assertNotEqual(r["frames"][0]["path"], r["frames"][1]["path"])
        self.assertTrue(Path(r["frames"][1]["path"]).stem.endswith("_1"))

    def test_failed_publish_keeps_existing_image(self):
        path = self.dest / "exists.jpg"
        path.write_bytes(b"original")
        with self.assertRaisesRegex(rf.FrameError, "未覆盖"):
            self.run_frames(output_dir=None, output=str(path))
        self.assertEqual(path.read_bytes(), b"original")
        self.assertEqual(list(self.dest.iterdir()), [path])

    def test_filesystem_without_hardlinks_uses_atomic_replace(self):
        path = self.dest / "cloud.jpg"
        with mock.patch.object(rf.os, "link", side_effect=OSError(errno.ENOTSUP, "unsupported")):
            r = self.run_frames(output_dir=None, output=str(path))
            self.assert_frame(r, 1.2)
            original = path.read_bytes()
            with self.assertRaisesRegex(rf.FrameError, "未覆盖"):
                self.run_frames(at=[2], output_dir=None, output=str(path))
            self.assertEqual(path.read_bytes(), original)
            self.assert_frame(self.run_frames(at=[2], output_dir=None, output=str(path), force=True), 2)
        self.assertFalse(list(self.dest.glob(".zju-frame-*")))

    def test_part_selection_and_local_priority(self):
        self.assertEqual(rf.select_recording_part(["one"], None), (1, "one"))
        self.assertEqual(rf.select_recording_part(["one", "two"], 2), (2, "two"))
        for urls, part in [([], None), (["a", "b"], None), (["a"], 2)]:
            with self.assertRaises(rf.FrameError):
                rf.select_recording_part(urls, part)
        sub = dict(course_id=89253, sub_id=2038840, course_name="课", sub_name="堂")
        z = mock.Mock()
        z.course_subs.return_value = [sub]
        z.video_catalogue.return_value = {2038840: ["remote"]}
        a = args()
        path = zju.classroom_material_path(self.dest, sub, "录播", "mp4")
        path.parent.mkdir(parents=True)
        path.write_bytes(self.small.read_bytes())
        man = zju.Manifest(self.dest)
        man.put("video:89253:2038840:1", dict(size=path.stat().st_size, path=str(path.relative_to(self.dest))))
        self.assertEqual(zju.recording_frame_source(z, a, self.dest), (path, 1))
        z.course_subs.assert_not_called()
        z.video_catalogue.assert_not_called()
        path.rename(path.with_suffix(".mp4.part"))
        self.assertEqual(zju.recording_frame_source(z, a, self.dest), ("remote", 1))
        z.video_catalogue.return_value = {2038840: ["a", "b"]}
        with self.assertRaisesRegex(rf.FrameError, "--part"):
            zju.recording_frame_source(z, a, self.dest)
        # 仅下载了多段中的一段仍不能猜测；明确选段可直接离线使用。
        multi = path.with_name(path.stem + " - 02.mp4")
        multi.write_bytes(self.small.read_bytes())
        man.put("video:89253:2038840:2", dict(size=multi.stat().st_size, path=str(multi.relative_to(self.dest))))
        z.reset_mock()
        self.assertEqual(zju.recording_frame_source(z, args(part=2), self.dest), (multi, 2))
        z.course_subs.assert_not_called()
        with self.assertRaisesRegex(rf.FrameError, "--part"):
            zju.recording_frame_source(z, a, self.dest)

    def test_cli_json_temp_survives_process_and_no_sync_writes(self):
        # 真正退出 Python 进程，确认不会被 TemporaryDirectory 生命周期清理。
        code = """import sys
from unittest import mock
from pathlib import Path
import zju
with mock.patch.object(zju, 'recording_frame_source', return_value=(Path(sys.argv[1]), 1)):
    sys.argv = ['zju.py', '--out', sys.argv[2], 'recording-frame', '--course', '89253', '--sub', '2038840', '--at', '1.25', '--json']
    zju.main()
"""
        outputs = []
        for _ in range(2):
            result = subprocess.run([sys.executable, "-c", code, str(self.small), str(self.dest / "sync")],
                                    cwd=ROOT, capture_output=True, text=True, check=True)
            r = json.loads(result.stdout)
            outputs.append(Path(r["output_dir"]))
            self.addCleanup(lambda p=outputs[-1]: __import__('shutil').rmtree(p))
            self.assertTrue(r["temporary"])
            self.assertEqual(outputs[-1].parent.resolve(), Path(tempfile.gettempdir()).resolve())
            self.assertTrue(Path(r["frames"][0]["path"]).is_file())
            self.assertEqual(r["frames"][0]["requested_time_sec"], 1.25)
        self.assertNotEqual(*outputs)
        self.assertFalse((self.dest / "sync").exists())

    def test_standalone_script_pep723_and_lazy_decoder(self):
        isolated = self.dest / "standalone"
        isolated.mkdir()
        script = isolated / "zju.py"
        shutil.copy2(ROOT / "zju.py", script)
        # 独立目录只有脚本；检查普通 CLI 不导入 PyAV，也没有仓库搜索路径。
        code = "import runpy,sys; runpy.run_path(sys.argv[1], run_name='standalone'); assert 'av' not in sys.modules"
        subprocess.run([sys.executable, "-I", "-c", code, str(script)], cwd=isolated, check=True, capture_output=True)
        source = self.dest / "local.mp4"
        source.write_bytes(self.small.read_bytes())
        manifest = {"video:89253:2038840:1": {"path": source.name, "size": source.stat().st_size}}
        (self.dest / ".zju_manifest.json").write_text(json.dumps(manifest))
        uv = shutil.which("uv")
        runner = [uv, "run", "--script", str(script)] if uv else [sys.executable, "-I", str(script)]
        env = dict(os.environ, ZJU_STATE_DIR=str(self.dest / "state"), PYTHONIOENCODING="utf-8")
        env.pop("PYTHONPATH", None)
        for option in ("--help", "recording-frame"):
            options = ([option] if option == "--help" else
                       ["--out", str(self.dest), option, "--course", "89253", "--sub", "2038840",
                        "--at", "1.25", "--output", str(self.dest / "isolated.jpg"), "--json"])
            run = subprocess.run(runner + options, cwd=isolated, env=env, capture_output=True, text=True, check=True)
            if option == "recording-frame":
                result = json.loads(run.stdout)
                self.assert_frame(result, 1.2)
                self.assertEqual(result["source_type"], "local")
        self.assertEqual(list(isolated.iterdir()), [script])


if __name__ == "__main__":
    unittest.main()
