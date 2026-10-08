"""本地 HTTP 服务器及生成的 MP4 验证音频范围下载，不访问学校。"""
import asyncio
import importlib.util
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from argparse import Namespace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import httpx

spec = importlib.util.spec_from_file_location("zju_audio_tests", Path(__file__).resolve().parents[1] / "zju.py")
zju = importlib.util.module_from_spec(spec)
spec.loader.exec_module(zju)


class RangeResponses(unittest.TestCase):
    def test_multipart_preserves_binary_crlf_and_orders_parts(self):
        payload = (b'\r\n--test\r\nContent-Range: bytes 20-21/100\r\n\r\nab'
                   b'\r\n--test\r\nContent-Range: bytes 10-13/100\r\n\r\n12\r\n'
                   b'\r\n--test--\r\n')

        async def run():
            transport = httpx.MockTransport(lambda request: httpx.Response(
                206, headers={"Content-Type": 'multipart/byteranges; boundary="test"'}, content=payload))
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


@unittest.skipUnless(shutil.which("ffmpeg"), "需要 ffmpeg 生成测试 MP4")
class AudioHTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.video = cls.root / "fixture.mp4"
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                        "color=c=black:s=64x64:r=10", "-f", "lavfi", "-i",
                        "sine=frequency=1000:sample_rate=32000", "-t", "40", "-c:v", "mpeg4",
                        "-c:a", "aac", "-b:a", "48k", "-movflags", "+faststart", str(cls.video)], check=True)
        cls.tail = cls.root / "tail.mp4"
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(cls.video),
                        "-c", "copy", str(cls.tail)], check=True)
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
                    self.send_header("ETag", '"changed"' if cls.change_version and number > 1 else '"fixture"')
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
        type(self).data = self.video.read_bytes()
        type(self).peak = type(self).requests = 0
        type(self).change_version = False

    def audio_hash(self, file):
        return subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(file),
                               "-map", "0:a:0", "-c:a", "copy", "-f", "hash", "-hash", "sha256", "-"],
                              check=True, capture_output=True).stdout

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
            with self.assertRaisesRegex(zju.ZjuError, "发生变化"):
                zju.make_audio(self.url, None, out, jobs=2)
            self.assertEqual(out.read_bytes(), b"existing")
            self.assertFalse(list(Path(d).glob(".zju-audio-*")))


class AudioCommands(unittest.TestCase):
    def args(self, root, **kw):
        args = Namespace(out=str(root), max_size=0, force=False, dry_run=False, jobs=32)
        for key, value in kw.items():setattr(args, key, value)
        return args

    def test_local_legacy_recording_preference_skip_force_and_dry_run(self):
        sub = dict(course_id=1, sub_id=2, course_name="课程", sub_name="第一堂")
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            video = root / "课程 (1)/錄播/第一堂 (2).mp4"
            video.parent.mkdir(parents=True)
            video.write_bytes(b"complete video")
            client = mock.Mock()
            def make(url, source, dest, jobs, limit):
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
            def save(url, source, dest, jobs, limit):
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(b"audio")
            with mock.patch.object(zju, "make_audio", side_effect=save) as make:
                zju.audio_subs(client, args, root, [sub])
                self.assertEqual(make.call_args_list[0].args[1], video)
                self.assertIsNone(make.call_args_list[1].args[1])
                self.assertEqual(len(list(root.rglob("*.m4a"))), 2)

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
        with mock.patch.object(zju, "cmd_audio") as command, mock.patch.object(zju, "load_config", return_value={}):
            with mock.patch.object(sys, "argv", ["zju.py", "audio", "--course", "1"]):
                zju.main()
            self.assertEqual(command.call_args.args[0].jobs, 32)
            for options in (["-j", "0"], ["--max-size", "-1"], ["--sub", "2"]):
                with mock.patch.object(sys, "argv", ["zju.py", "audio", *options]):
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
            args = self.args(root, course=[], audio=True, recordings=False, format="md", jobs=3)
            with mock.patch.object(zju, "Zju", return_value=client), \
                    mock.patch.object(zju, "transcript_one"), mock.patch.object(zju, "ppt_one"), \
                    mock.patch.object(zju, "audio_subs", return_value=0) as audio:
                zju.cmd_classroom_sync(args)
                self.assertEqual(audio.call_args.args[1].jobs, 3)
                args.dry_run = True
                zju.cmd_classroom_sync(args)
                self.assertTrue(audio.call_args.args[1].dry_run)
                self.assertFalse(root.exists())
