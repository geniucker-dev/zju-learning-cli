"""本地 HTTP 服务器及生成的 MP4 验证音频范围下载，不访问学校。"""
import asyncio
import importlib.util
import hashlib
import json
import os
import subprocess
import sys
from fractions import Fraction
import tempfile
import threading
import time
import unittest
from argparse import Namespace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import httpx
import av
import numpy as np

spec = importlib.util.spec_from_file_location("zju_audio_tests", Path(__file__).resolve().parents[1] / "zju.py")
zju = importlib.util.module_from_spec(spec)
spec.loader.exec_module(zju)


class RangeResponses(unittest.TestCase):
    def test_shared_version_checks_missing_changed_weak_and_redirect(self):
        for second in ({"ETag": '"v2"'}, {}, {"ETag": 'W/"v1"'},
                       {"Last-Modified": "Thu, 01 Jan 2026 00:00:00 GMT"}):
            async def run():
                calls = []
                def respond(request):
                    calls.append(request)
                    return httpx.Response(206, content=b"x" * 16,
                        headers={"Content-Range": "bytes 0-15/100", **({"ETag": '"v1"'} if len(calls) == 1 else second)})
                async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                    source = zju.AudioRanges(client, "https://example.com/video?SECRET")
                    await source.get([(0, 15)])
                    with self.assertRaises(zju.ZjuError) as error:
                        await source.get([(0, 15)])
                    self.assertNotIn("SECRET", str(error.exception))
                    self.assertEqual(len(calls), 2)  # 永久版本错误不应重试。
                    self.assertEqual(calls[-1].headers["If-Range"], '"v1"')
            asyncio.run(run())
        async def redirect():
            calls = []
            def respond(request):
                calls.append(request)
                return httpx.Response(302, headers={"Location": "https://unexpected.invalid/SECRET"})
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond), follow_redirects=True) as client:
                with self.assertRaisesRegex(zju.ZjuError, "302"):
                    await zju.AudioRanges(client, "https://example.com/video").get([(0, 15)])
            self.assertEqual(len(calls), 1)
        asyncio.run(redirect())

    def test_multipart_preserves_binary_crlf_and_orders_parts(self):
        payload = (b'\r\n--test\r\nContent-Range: bytes 20-21/100\r\n\r\nab'
                   b'\r\n--test\r\nContent-Range: bytes 10-13/100\r\n\r\n12\r\n'
                   b'\r\n--test--\r\n')

        async def run():
            transport = httpx.MockTransport(lambda request: httpx.Response(
                206, headers={"Content-Type": 'multipart/byteranges; boundary="test"', "ETag": '"fixture"'}, content=payload))
            async with httpx.AsyncClient(transport=transport) as client:
                reader = zju.AudioRanges(client, "https://example.com/video")
                reader.total = 100
                self.assertEqual(await reader.get([(10, 13), (20, 21)]), b"12\r\nab")
        asyncio.run(run())

    def test_missing_and_wrong_ranges_fail(self):
        for payload in (b"ab", b"abc"):
            async def run():
                transport = httpx.MockTransport(lambda request: httpx.Response(
                    206, headers={"Content-Type": "video/mp4", "Content-Range": "bytes 10-12/100"}, content=payload))
                async with httpx.AsyncClient(transport=transport) as client:
                    reader = zju.AudioRanges(client, "https://example.com/video")
                    reader.total = 100
                    with self.assertRaises(zju.ZjuError):
                        await reader.get([(10, 11)])
            with mock.patch.object(zju.asyncio, "sleep", new_callable=mock.AsyncMock):
                asyncio.run(run())

    def test_ignored_range_never_reads_full_video(self):
        class FullVideo(httpx.AsyncByteStream):
            read = False
            async def __aiter__(self):
                self.read = True
                yield b"full video must not be downloaded"
        body = FullVideo()
        async def run():
            transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=body))
            async with httpx.AsyncClient(transport=transport) as client:
                with self.assertRaisesRegex(zju.ZjuError, "不下载整个视频"):
                    await zju.AudioRanges(client, "https://example.com/video").get([(0, 15)])
        with mock.patch.object(zju.asyncio, "sleep", new_callable=mock.AsyncMock):
            asyncio.run(run())
        self.assertFalse(body.read)

    def test_parallel_failure_waits_for_cancelled_workers(self):
        stopped = asyncio.Event()
        async def work(i):
            if i == 0:
                await asyncio.sleep(0.01)
                raise zju.ZjuError("bad response")
            try:
                await asyncio.sleep(60)
            finally:
                stopped.set()
        async def run():
            with self.assertRaises(zju.ZjuError):
                await zju.audio_parallel(range(2), 2, work)
            self.assertTrue(stopped.is_set())
        asyncio.run(run())

    def test_malformed_mp4_boxes(self):
        for data in (b"short", b"\0\0\0\x14moov", b"\0\0\0\x01moov"):
            with self.assertRaises(zju.ZjuError):
                list(zju.mp4_boxes(data))


class AudioHTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.video = cls.root / "fixture.mp4"
        with av.open(str(cls.video), "w", options={"movflags": "+faststart"}) as output:
            v = output.add_stream("mpeg4", rate=10)
            v.width = v.height = 64
            v.pix_fmt = "yuv420p"
            a = output.add_stream("aac", rate=32000)
            a.layout, a.bit_rate = "mono", 48000
            for i in range(400):
                frame = av.VideoFrame.from_ndarray(np.zeros((64, 64, 3), dtype=np.uint8), format="rgb24")
                frame.pts, frame.time_base = i, Fraction(1, 10)
                for packet in v.encode(frame):
                    output.mux(packet)
                wave = (.1 * np.sin(np.arange(i * 3200, (i + 1) * 3200) * (2 * np.pi * 1000 / 32000))).astype(np.float32)
                frame = av.AudioFrame.from_ndarray(wave.reshape(1, -1), format="fltp", layout="mono")
                frame.sample_rate, frame.pts, frame.time_base = 32000, i * 3200, Fraction(1, 32000)
                for packet in a.encode(frame):
                    output.mux(packet)
            for stream in (v, a):
                for packet in stream.encode():
                    output.mux(packet)
        cls.tail = cls.root / "tail.mp4"
        with av.open(str(cls.video)) as source, av.open(str(cls.tail), "w") as output:
            streams = {s.index: output.add_stream_from_template(s) for s in source.streams}
            for packet in source.demux():
                if packet.dts is not None:
                    packet.stream = streams[packet.stream.index]
                    output.mux(packet)
        # 索引在文件尾时扩展 stco 为 co64，媒体偏移无需改变。
        import array
        import struct
        import sys
        def co64(data):
            output = b""
            for kind, payload in zju.mp4_boxes(data):
                if kind in (b"moov", b"trak", b"mdia", b"minf", b"stbl"):
                    payload = co64(payload)
                elif kind == b"stco":
                    values = array.array("Q", zju.mp4_ints(payload[8:]))
                    if sys.byteorder == "little":values.byteswap()
                    payload = bytes(payload[:8]) + values.tobytes()
                    kind = b"co64"
                output += zju.mp4_box(kind, payload)
            return output
        cls.wide = cls.root / "co64.mp4"
        cls.wide.write_bytes(co64(cls.tail.read_bytes()))
        cls.data = cls.video.read_bytes()
        cls.active = cls.peak = cls.requests = 0
        cls.lock = threading.Lock()
        cls.change_version = False
        cls.validators = {"ETag": '"fixture"'}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                with cls.lock:
                    cls.active += 1
                    cls.peak = max(cls.peak, cls.active)
                    cls.requests += 1
                    number = cls.requests
                try:
                    time.sleep(0.01)
                    data = cls.data
                    ranges = [tuple(map(int, item.split("-"))) for item in self.headers["Range"][6:].split(",")]
                    if len(ranges) == 1:
                        a, b = ranges[0]
                        body = data[a:b + 1]
                        mime = "video/mp4"
                    else:
                        body = b""
                        for a, b in ranges:
                            body += f"\r\n--test\r\nContent-Type: video/mp4\r\nContent-Range: bytes {a}-{b}/{len(data)}\r\n\r\n".encode() + data[a:b + 1]
                        body += b"\r\n--test--\r\n"
                        mime = "multipart/byteranges; boundary=test"
                    self.send_response(206)
                    self.send_header("Content-Type", mime)
                    self.send_header("Content-Length", str(len(body)))
                    for key, value in cls.validators.items():
                        self.send_header(key, '"changed"' if cls.change_version and number > 1 else value)
                    if len(ranges) == 1:
                        self.send_header("Content-Range", f"bytes {a}-{b}/{len(data)}")
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    with cls.lock:
                        cls.active -= 1
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/video.mp4"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.temp.cleanup()

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.cache = Path(temp.name) / "cache"
        patch = mock.patch.object(zju, "default_index_cache_dir", return_value=self.cache)
        patch.start()
        self.addCleanup(patch.stop)
        type(self).data = self.video.read_bytes()
        type(self).peak = type(self).requests = 0
        type(self).change_version = False
        type(self).validators = {"ETag": '"fixture"'}

    def audio_hash(self, file):
        h = hashlib.sha256()
        with av.open(str(file)) as container:
            for packet in container.demux(audio=0):
                h.update(bytes(packet))
        return h.digest()

    def audio_signature(self, file):
        with av.open(str(file)) as c:
            s = c.streams.audio[0]
            packets = [(bytes(p), p.pts, p.dts, p.duration, p.time_base)
                       for p in c.demux(s) if p.size]
            return packets, s.duration, s.time_base, s.codec_context.extradata, s.codec_context.sample_rate

    def child(self, mode, url, out, *options):
        # 两个独立 CLI 进程，只替换学校查询，保留真正的媒体请求与命令参数处理。
        code = '''import sys
from unittest import mock
import zju
url,mode,out,*options=sys.argv[1:]
if mode=='frame':
 zju.Zju=lambda **kw:None
 zju.recording_frame_source=lambda *a:(url,1)
 sys.argv=['zju.py','recording-frame','--course','1','--sub','2','--at','1.25',
           '--output-dir',out,'--json',*options]
else:
 client=mock.Mock()
 client.video_catalogue.return_value={2:[url]}
 zju.Zju=lambda:client
 zju.resolve_subs=lambda z,a:[dict(course_id=1,sub_id=2,course_name='course',sub_name='lesson')]
 sys.argv=['zju.py','--out',out,'recording-audio','--course','1','--sub','2','-j','3',*options]
zju.main()
'''
        result = subprocess.run([sys.executable, "-c", code, url, mode, str(out), *map(str, options)],
                              cwd=Path(zju.__file__).parent, capture_output=True, text=True,
                              encoding="utf-8",
                              env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1",
                                       XDG_CACHE_HOME=str(self.cache.parent), LOCALAPPDATA=str(self.cache.parent)))
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def test_bidirectional_index_reuse_across_cli_processes_and_exact_audio_timeline(self):
        for source in (self.video, self.tail, self.wide):
            type(self).data = source.read_bytes()
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                for first in ("audio", "frame"):
                    cache = root / (first + "-cache")
                    self.child(first, self.url + "?sign=SECRET1", root / (first + "-first"),
                               "--index-cache-dir", cache)
                    entries = list(cache.rglob("*.idx"))
                    self.assertEqual(len(entries), 1)
                    m = next(p for n, p in zju.mp4_boxes(type(self).data) if n == b"moov")
                    stored = entries[0].read_bytes()
                    self.assertEqual(stored[zju.MP4IndexCache.header_size + 8:], m)
                    self.assertNotIn(b"SECRET", stored[:zju.MP4IndexCache.header_size])
                    second = "frame" if first == "audio" else "audio"
                    # 禁止重新下载 moov，证明两个进程通过同一个原始索引双向复用。
                    index = zju.MP4IndexCache(cache, 256 * 2**20,
                            zju.mp4_identity(self.url, len(type(self).data), ("ETag", '"fixture"')))
                    self.assertIsNotNone(index.entry)
                    if second == "audio":
                        old = type(self).requests
                        self.child("audio", self.url + "?sign=SECRET2", root / "audio-second",
                                   "--index-cache-dir", cache)
                        warm_requests = type(self).requests - old
                        old = type(self).requests
                        self.child("audio", self.url, root / "audio-cold", "--no-index-cache")
                        self.assertGreater(type(self).requests - old, warm_requests)
                    else:
                        warm = json.loads(self.child("frame", self.url + "?sign=SECRET2", root / "frame-second",
                                          "--index-cache-dir", cache).stdout)
                        cold = json.loads(self.child("frame", self.url, root / "frame-cold", "--no-index-cache").stdout)
                        self.assertLess(warm["network"]["bytes_received"], cold["network"]["bytes_received"])
                        a, b = warm["frames"][0], cold["frames"][0]
                        self.assertEqual(a["actual_time_sec"], 1.2)
                        self.assertEqual(zju.file_hash(Path(a["path"])), zju.file_hash(Path(b["path"])))
                    for audio in root.rglob("*.m4a"):
                        self.assertEqual(self.audio_signature(audio), self.audio_signature(source))
                    index.close()
                    # 破坏缓存正文后仍能恢复；视频帧与本地相同，音频包与时间线完全一致。
                    with entries[0].open("r+b") as f:
                        f.seek(zju.MP4IndexCache.header_size + 20)
                        f.write(b"bad")
                    out = root / (first + "-recovered.m4a")
                    zju.make_audio(self.url, None, out, jobs=3, index_cache_dir=cache)
                    self.assertEqual(self.audio_signature(out), self.audio_signature(source))

    def test_default_cache_override_disabled_weak_version_and_unwritable_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            # CLI 不传缓存参数也会持久缓存，截图输出仍是独立目录。
            self.child("audio", self.url, root / "default-audio")
            expected = self.cache.parent / "zju-learning"
            if sys.platform == "win32":
                expected /= "cache"
            self.assertEqual(len(list(expected.rglob("*.idx"))), 1)
            self.assertFalse(list((root / "default-audio").rglob("*.idx")))
            self.child("frame", self.url, root / "disabled", "--no-index-cache")
            self.assertFalse(list((root / "disabled").rglob("*.idx")))
            file = root / "not-directory"
            file.write_text("untouched")
            zju.make_audio(self.url, None, root / "unwritable.m4a", index_cache_dir=file)
            self.assertEqual(file.read_text(), "untouched")
            for headers in ({"ETag": 'W/"weak"', "Last-Modified": "Thu, 01 Jan 2026 00:00:00 GMT"},
                            {"Last-Modified": "Thu, 01 Jan 2026 00:00:00 GMT"}):
                type(self).validators = headers
                cache = root / "weak"
                zju.make_audio(self.url, None, root / "weak.m4a", index_cache_dir=cache)
                self.assertFalse(cache.exists())
            # 同大小、同 Last-Modified 的不同版本不能复用旧 moov。
            type(self).validators = {"ETag": '"new"'}
            zju.make_audio(self.url, None, root / "new.m4a", index_cache_dir=expected)
            self.assertEqual(len(list(expected.rglob("*.idx"))), 2)

    def test_index_download_interruption_cleans_pending_and_recovers(self):
        original = zju.AudioRanges.get
        async def stop(reader, ranges):
            if reader.index_cache and reader.index_cache.writer:
                raise asyncio.CancelledError()
            return await original(reader, ranges)
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "audio.m4a"
            with mock.patch.object(zju.AudioRanges, "get", stop):
                with self.assertRaises(asyncio.CancelledError):
                    zju.make_audio(self.url, None, out, index_cache_dir=self.cache)
            self.assertFalse(out.exists())
            self.assertFalse(list(self.cache.rglob("*.idx")))
            self.assertFalse(list(self.cache.rglob(".pending")))
            zju.make_audio(self.url, None, out, index_cache_dir=self.cache)
            self.assertEqual(self.audio_signature(out), self.audio_signature(self.video))
            self.assertEqual(len(list(self.cache.rglob("*.idx"))), 1)

    def test_local_and_remote_audio_match_for_head_and_tail_index(self):
        for source in (self.video, self.tail, self.wide):
            type(self).data = source.read_bytes()
            with tempfile.TemporaryDirectory() as d:
                out = Path(d) / "remote.m4a"
                with mock.patch.object(zju.shutil, "which", return_value=None), \
                        mock.patch.object(zju.subprocess, "run", side_effect=AssertionError("不应调用外部工具")):
                    zju.make_audio(self.url, None, out, jobs=3)
                self.assertEqual(self.audio_hash(out), self.audio_hash(source))
                self.assertLessEqual(type(self).peak, 3)
                self.assertFalse(list(Path(d).glob(".zju-audio-*")))
                local = Path(d) / "local.m4a"
                with mock.patch.object(zju, "download_audio_ranges") as download, \
                        mock.patch.object(zju.shutil, "which", return_value=None), \
                        mock.patch.object(zju.subprocess, "run", side_effect=AssertionError("不应调用外部工具")):
                    zju.make_audio(None, source, local)
                    download.assert_not_called()
                self.assertEqual(self.audio_hash(local), self.audio_hash(out))

    def test_long_offsets_keep_range_headers_below_server_limit(self):
        import array
        data = self.video.read_bytes()
        ftyp = next(zju.mp4_box(n, p) for n, p in zju.mp4_boxes(data) if n == b"ftyp")
        index = zju.AudioIndex(ftyp, zju.mp4_child(data, b"moov"), len(data))
        index.starts = array.array("Q", (10**12 + i * 10000 for i in range(len(index.starts))))
        groups = list(index.groups())
        self.assertEqual(sum(last - first for first, last in groups), len(index.starts))
        for first, last in groups:
            header = "bytes=" + ",".join(f"{index.starts[i]}-{index.starts[i]+index.lengths[i]-1}" for i in range(first, last))
            self.assertLessEqual(len(header), 8000)
            self.assertLessEqual(last - first, 360)

    def test_failure_keeps_previous_output_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "audio.m4a"
            out.write_bytes(b"existing")
            with self.assertRaises(zju.TooBig):
                zju.make_audio(self.url, None, out, jobs=2, limit=1)
            self.assertEqual(out.read_bytes(), b"existing")
            self.assertFalse(list(Path(d).glob(".zju-audio-*")))
            type(self).requests = 0
            type(self).change_version = True
            with self.assertRaisesRegex(zju.ZjuError, "版本已变化"):
                zju.make_audio(self.url, None, out, jobs=2)
            self.assertEqual(out.read_bytes(), b"existing")
            self.assertFalse(list(Path(d).glob(".zju-audio-*")))


class AudioCommands(unittest.TestCase):
    def args(self, root, **kw):
        args = Namespace(out=str(root), max_size=0, force=False, dry_run=False, jobs=32)
        for key, value in kw.items():setattr(args, key, value)
        return args

    def test_local_recording_preference_skip_force_and_dry_run(self):
        sub = dict(course_id=1, sub_id=2, course_name="课程", sub_name="第一堂")
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            video = root / "课程 (1)/录播/第一堂 (2).mp4"
            video.parent.mkdir(parents=True)
            video.write_bytes(b"complete video")
            client = mock.Mock()
            def make(url, source, dest, jobs, limit, **cache):
                self.assertIsNone(url)
                self.assertEqual(source, video)
                self.assertEqual(jobs, 32)
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(b"audio")
            args = self.args(root)
            with mock.patch.object(zju, "make_audio", side_effect=make) as make_mock:
                self.assertEqual(zju.audio_subs(client, args, root, [sub]), 0)
                self.assertEqual(zju.audio_subs(client, args, root, [sub]), 0)
                self.assertEqual(make_mock.call_count, 1)
                args.force = True
                zju.audio_subs(client, args, root, [sub])
                self.assertEqual(make_mock.call_count, 2)
                args.dry_run = True
                zju.audio_subs(client, args, root, [sub])
                self.assertEqual(make_mock.call_count, 2)
            client.video_catalogue.assert_not_called()
            self.assertTrue((root / "课程 (1)/音频/第一堂 (2).m4a").is_file())

    def test_multiple_parts_partial_recording_and_no_write_dry_run(self):
        sub = dict(course_id=1, sub_id=2, course_name="课程", sub_name="第一堂")
        client = mock.Mock()
        client.video_catalogue.return_value = {2: ["https://example.com/1.mp4", "https://example.com/2.mp4"]}
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "new"
            args = self.args(root, dry_run=True)
            with mock.patch.object(zju, "make_audio") as make:
                zju.audio_subs(client, args, root, [sub])
                make.assert_not_called()
                self.assertFalse(root.exists())
            video = root / "课程 (1)/录播/第一堂 (2) - 01.mp4"
            video.parent.mkdir(parents=True)
            video.write_bytes(b"existing")
            args.dry_run = False
            def save(url, source, dest, jobs, limit, **cache):
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(b"audio")
            with mock.patch.object(zju, "make_audio", side_effect=save) as make:
                zju.audio_subs(client, args, root, [sub])
                self.assertEqual(make.call_args_list[0].args[1], video)
                self.assertIsNone(make.call_args_list[1].args[1])
                self.assertEqual(len(list(root.rglob("*.m4a"))), 2)

    def test_delisted_lessons_skip_but_unexpected_missing_catalogue_fails(self):
        import contextlib
        import io
        sub = dict(course_id=1, sub_id=2, course_name="课程", sub_name="第一堂", show="no")
        for dry_run in (False, True):
            for fn in (zju.audio_subs, zju.recording_subs):
                with tempfile.TemporaryDirectory() as d:
                    root = Path(d) / "new"
                    client = mock.Mock()
                    client.video_catalogue.return_value = {}
                    output = io.StringIO()
                    with contextlib.redirect_stderr(output), mock.patch.object(zju, "make_audio") as audio, \
                            mock.patch.object(zju, "download_video") as video:
                        self.assertEqual(fn(client, self.args(root, dry_run=dry_run), root, [sub]), 0)
                        client.video_catalogue.assert_not_called()
                        audio.assert_not_called()
                        video.assert_not_called()
                        self.assertIn("已下架，跳过", output.getvalue())
                        self.assertFalse(root.exists())
                        # 非下架堂次缺少目录仍属异常，不能掩盖接口问题。
                        self.assertEqual(fn(client, self.args(root, dry_run=dry_run), root,
                                            [dict(sub, show="yes")]), 1)

    def test_course_subs_preserves_visibility(self):
        client = zju.Zju.__new__(zju.Zju)
        lessons = [dict(id=2, sub_title="第一堂", show="no"),
                   dict(id=3, sub_title="第二堂", show="yes")]
        detail = {"data": {"title": "课程", "sub_list": {"2026": {"9": {"1": lessons}}}}}
        with mock.patch.object(client, "infosimple", return_value={"account": "test"}), \
                mock.patch.object(client, "get"), mock.patch.object(client, "bearer", return_value={}), \
                mock.patch.object(client, "json", return_value=detail):
            self.assertEqual({s["sub_id"]: s["show"] for s in client.course_subs(1)},
                             {2: "no", 3: "yes"})

    def test_local_extraction_without_external_tools(self):
        import struct
        box = zju.mp4_box
        payload = b"audio-data\r\n"
        ftyp = box(b"ftyp", b"isom\0\0\0\0isom")
        stbl = (box(b"stsz", struct.pack(">III", 0, len(payload), 1))
                + box(b"stsc", struct.pack(">IIIII", 0, 1, 1, 1, 1))
                + box(b"stco", struct.pack(">III", 0, 1, len(ftyp) + 8)))
        track = box(b"trak", box(b"mdia", box(b"hdlr", b"\0" * 8 + b"soun")
                                 + box(b"minf", box(b"stbl", stbl))))
        moov = box(b"moov", box(b"mvhd", b"\0" * 100) + track)
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d) / "new/audio.m4a"
            source = Path(d) / "video.mp4"
            source.write_bytes(ftyp + box(b"mdat", payload) + moov)
            with mock.patch.object(zju.shutil, "which", return_value=None), \
                    mock.patch.object(zju.subprocess, "run", side_effect=AssertionError("不应调用外部工具")), \
                    mock.patch.object(zju, "download_audio_ranges") as download:
                zju.make_audio(None, source, dest)
                download.assert_not_called()
            self.assertTrue(dest.read_bytes().endswith(payload))
            self.assertFalse(list(dest.parent.glob(".zju-audio-*")))

    def test_audio_cli_defaults_and_invalid_options(self):
        import sys
        with mock.patch.object(zju, "cmd_recording_audio") as command, mock.patch.object(zju, "load_config", return_value={}):
            with mock.patch.object(sys, "argv", ["zju.py", "recording-audio", "--course", "1"]):
                zju.main()
            self.assertEqual(command.call_args.args[0].jobs, 32)
            for options in (["-j", "0"], ["--max-size", "-1"], ["--sub", "2"]):
                with mock.patch.object(sys, "argv", ["zju.py", "recording-audio", *options]):
                    with self.assertRaises(SystemExit) as error:
                        zju.main()
                    self.assertEqual(error.exception.code, 2)

    def test_classroom_sync_audio_uses_sync_jobs_and_dry_run(self):
        sub = dict(course_id=1, sub_id=2, course_name="课程", sub_name="第一堂")
        client = mock.Mock()
        client.classroom_courses.return_value = [dict(course_id=1, title="课程", type="multi")]
        client.course_subs.return_value = [sub]
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "new"
            args = self.args(root, course=[], recording_audio=True, recording=False, format="md", jobs=3)
            with mock.patch.object(zju, "Zju", return_value=client), \
                    mock.patch.object(zju, "transcript_one"), mock.patch.object(zju, "ppt_one"), \
                    mock.patch.object(zju, "audio_subs", return_value=0) as audio:
                zju.cmd_classroom_sync(args)
                self.assertEqual(audio.call_args.args[1].jobs, 3)
                args.dry_run = True
                zju.cmd_classroom_sync(args)
                self.assertTrue(audio.call_args.args[1].dry_run)
                self.assertFalse(root.exists())
