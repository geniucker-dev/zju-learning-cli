"""跨调用索引缓存与 recording-frame 专用认证链；全部离线。"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import requests
import test_recording_frames as fixture

zju = fixture.zju


class IndexCache(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.identity = dict(resource_sha256="a" * 64, size=10 * 2**20, validator=["ETag", '"v1"'])
        self.moov = zju.mp4_box(b"moov", zju.mp4_box(b"free", b"i" * 400000))

    def cache(self, identity=None, limit=2**20):
        c = zju.FrameIndexCache(self.root / "cache", limit, identity or self.identity)
        self.addCleanup(c.close)
        return c

    def seed(self, cache):
        cache.capture((32, 32 + len(self.moov)), 32, self.moov)
        self.assertIsNotNone(cache.entry)

    def test_cache_options_validate_before_any_source_access_and_windows_lock(self):
        for value in (0, -1, 1.5, True):
            with mock.patch.object(zju, "recording_frame_source") as source:
                with self.assertRaisesRegex(zju.ZjuError, "index-cache-mb"):
                    zju.cmd_recording_frame(fixture.args(index_cache_mb=value))
                source.assert_not_called()
        with self.assertRaisesRegex(zju.FrameError, "不能为空"):
            zju.validate_frame_args(fixture.args(index_cache_dir=""))
        path = self.root / "lock-target"
        calls = []
        win = SimpleNamespace(LK_NBLCK=2, LK_UNLCK=0,
                              locking=lambda fd, operation, size: calls.append((operation, size)))
        with mock.patch.dict(sys.modules, msvcrt=win), mock.patch.object(zju.os, "name", "nt"):
            with zju.frame_output_lock(path):
                self.assertEqual(calls, [(2, 1)])
        self.assertEqual(calls, [(2, 1), (0, 1)])

    def test_exact_index_only_identity_versions_capacity_and_corruption(self):
        first = self.cache()
        self.seed(first)
        self.assertEqual(first.path.read_bytes()[first.header_size:], self.moov)
        self.assertEqual(self.cache().read(32, 32 + len(self.moov) - 1), self.moov)
        other = self.cache(dict(self.identity, resource_sha256="b" * 64))
        changed = self.cache(dict(self.identity, validator=["ETag", '"v2"']))
        self.assertIsNone(other.entry)
        self.assertIsNone(changed.entry)
        self.seed(other)
        os.utime(first.path, (1, 1))
        self.seed(changed)
        self.assertFalse(first.path.exists())
        self.assertLessEqual(sum(p.stat().st_size for p in first.root.iterdir()), 2**20)
        # 元数据、正文和截断都不能作为有效缓存继续使用，并可重建。
        for location in (50, changed.header_size + 100, -1):
            data = bytearray(changed.path.read_bytes())
            if location == -1:
                del data[-10:]
            else:
                data[location] ^= 1
            changed.path.write_bytes(data)
            c = self.cache(dict(self.identity, validator=["ETag", '"v2"']))
            self.assertIsNone(c.read(32, 32 + len(self.moov) - 1))
            self.seed(c)
            self.assertEqual(c.read(32, 32 + len(self.moov) - 1), self.moov)
        self.assertIsNone(self.cache(limit=128 * 1024).entry)
        self.assertLessEqual(sum(p.stat().st_size for p in first.root.iterdir()), 128 * 1024)

    def test_interrupted_and_failed_publish_never_leave_valid_partial_index(self):
        c = self.cache()
        c.capture((32, 32 + len(self.moov)), 32, self.moov[:100])
        self.assertTrue((c.root / ".pending").exists())
        self.assertFalse(c.path.exists())
        c.close()
        self.assertFalse((c.root / ".pending").exists())
        c = self.cache()
        with mock.patch.object(zju.os, "replace", side_effect=OSError("disk error")):
            c.capture((32, 32 + len(self.moov)), 32, self.moov)
        self.assertFalse(c.path.exists())
        self.assertFalse((c.root / ".pending").exists())
        self.seed(self.cache())
        # 用户指定不可写/非目录路径时，缓存失败不影响正常网络读取。
        file = self.root / "not-a-directory"
        file.write_bytes(b"untouched")
        c = zju.FrameIndexCache(file, 2**20, self.identity)
        c.capture((32, 32 + len(self.moov)), 32, self.moov)
        c.close()
        self.assertEqual(file.read_bytes(), b"untouched")

    def test_cross_process_lock_and_crash_recovery(self):
        code = """import json,sys
import zju
c=zju.FrameIndexCache(sys.argv[1], 1048576, json.loads(sys.argv[2]))
data=zju.mp4_box(b'moov', zju.mp4_box(b'free', b'i'*400000))
c.capture((32,32+len(data)),32,data[:100])
print('locked',flush=True)
sys.stdin.read(1)
"""
        p = subprocess.Popen([sys.executable, "-c", code, str(self.root / "cache"), json.dumps(self.identity)],
                             cwd=fixture.ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(p.stdout.readline().strip(), "locked")
            competing = self.cache()
            competing.capture((32, 32 + len(self.moov)), 32, self.moov)
            self.assertIsNone(competing.writer)
            self.assertFalse(competing.path.exists())
            self.assertTrue((competing.root / ".pending").exists())
        finally:
            p.terminate()
            p.communicate(timeout=10)
        recovered = self.cache()
        self.assertFalse((recovered.root / ".pending").exists())
        self.seed(recovered)
        self.assertEqual(recovered.read(32, 32 + len(self.moov) - 1), self.moov)

    def test_range_reuse_head_tail_budget_and_weak_validator(self):
        block = zju.FrameRangeReader.block_size
        header = zju.mp4_box(b"ftyp", b"isom" * 4)
        index = zju.mp4_box(b"moov", zju.mp4_box(b"free", b"i" * (3 * block)))
        media = zju.mp4_box(b"mdat", b"m" * (3 * block))
        for tail in (False, True):
            path = self.root / f"video-{tail}.mp4"
            content = header + (media + index if tail else index + media)
            path.write_bytes(content)
            start = len(header) + (len(media) if tail else 0)
            with fixture.server(path) as (url, state):
                amounts = []
                for run in range(2):
                    with zju.FrameRangeReader(url, index_cache_dir=self.root / "range-cache") as reader:
                        reader.read(32)
                        reader.seek(start)
                        self.assertEqual(reader.read(len(index)), index)
                        amounts.append(reader.bytes_received)
                        self.assertIsNotNone(reader.index_cache.entry)
                self.assertGreater(amounts[0] - amounts[1], 2 * block)
                # 缓存正文损坏时，经过真实 Range 回退仍得到相同索引。
                cache_file = reader.index_cache.path
                with cache_file.open("r+b") as f:
                    f.seek(zju.FrameIndexCache.header_size + 100)
                    f.write(b"BAD")
                with zju.FrameRangeReader(url, index_cache_dir=self.root / "range-cache") as reader:
                    reader.read(32)
                    reader.seek(start)
                    self.assertEqual(reader.read(len(index)), index)
                    self.assertIsNone(reader.last_error)
                with zju.FrameRangeReader(url, 16, index_cache_dir=self.root / "range-cache") as reader:
                    self.assertEqual(reader.read(32), b"")
                    self.assertIsNotNone(reader.last_error)
                    self.assertEqual(reader.bytes_received, 16)
                state.version = '"new"'
                with zju.FrameRangeReader(url, index_cache_dir=self.root / "range-cache") as reader:
                    self.assertIsNone(reader.index_cache.entry)
                state.mode = "modified"
                with zju.FrameRangeReader(url, index_cache_dir=self.root / "weak-cache") as reader:
                    self.assertIsNone(reader.index_cache)
                self.assertFalse((self.root / "weak-cache").exists())
        for p in (self.root / "range-cache").rglob("*.idx"):
            self.assertNotIn(b"SECRET", p.read_bytes()[:zju.FrameIndexCache.header_size])

    def test_real_decode_across_processes_and_local_priority(self):
        code = """import sys
from unittest import mock
import zju
with mock.patch.object(zju,'recording_frame_source',return_value=(sys.argv[1],1)):
 sys.argv=['zju.py','recording-frame','--course','1','--sub','2','--at',sys.argv[2],
           '--index-cache-dir',sys.argv[3],'--output-dir',sys.argv[4],'--json']
 zju.main()
"""
        for fast in (False, True):
            path = self.root / f"real-{fast}.mp4"
            fixture.video(path, fast=fast)
            with fixture.server(path) as (url, _):
                for sec in (1.25, 3.25):
                    result = subprocess.run([sys.executable, "-c", code, url, str(sec),
                                             str(self.root / "real-cache"), str(self.root / f"remote-{fast}")],
                                            cwd=fixture.ROOT, capture_output=True, text=True, check=True)
                    remote = json.loads(result.stdout)
                    local = zju.extract_recording_frames(fixture.args(at=[sec], output_dir=str(self.root / "local")), path, 1)
                    self.assertEqual(remote["frames"][0]["actual_time_sec"], local["frames"][0]["actual_time_sec"])
                    self.assertEqual(zju.file_hash(Path(remote["frames"][0]["path"])),
                                     zju.file_hash(Path(local["frames"][0]["path"])))
            self.assertTrue(list((self.root / "real-cache").rglob("*.idx")))
        unused = self.root / "local-cache"
        zju.extract_recording_frames(fixture.args(index_cache_dir=str(unused), output_dir=str(self.root / "local")), path, 1)
        self.assertFalse(unused.exists())


class FrameAuthentication(unittest.TestCase):
    info_url = "https://classroom.zju.edu.cn/userapi/v1/infosimple"

    def client(self, token="old", fast=True):
        with mock.patch.object(zju.Zju, "_load_cookies"):
            c = zju.Zju(classroom_first=fast)
        state = {"token": token}
        c._token = mock.Mock(side_effect=lambda silent=False: state["token"])
        def login(*args):
            state["token"] = "fresh"
            c.logged_in = True
        c.login = mock.Mock(side_effect=login)
        c.courses_alive = mock.Mock(side_effect=AssertionError("unexpected LMS preflight"))
        self.addCleanup(c.s.close)
        return c

    def response(self, code=200, data=None, location=None):
        r = requests.Response()
        r.status_code, r.url = code, self.info_url
        r._content = json.dumps(data if data is not None else {"params": {"account": "test"}}).encode()
        r._content_consumed = True
        if location:
            r.headers["Location"] = location
        return r

    def test_valid_session_preserves_lesson_visibility_and_catalogue_checks(self):
        c = self.client()
        detail = {"data": {"title": "course", "sub_list": {"y": {"m": {"w": [
            {"id": 2, "sub_title": "lesson", "show": "no"}]}}}}}
        with mock.patch.object(c.s, "request", side_effect=[self.response(), self.response(data=detail)]) as request:
            with tempfile.TemporaryDirectory() as root, self.assertRaisesRegex(zju.FrameError, "下架"):
                zju.recording_frame_source(c, fixture.args(course=1, sub=2), Path(root))
        self.assertEqual(request.call_count, 2)
        c.login.assert_not_called()
        c.courses_alive.assert_not_called()
        self.assertTrue(all(call.kwargs["allow_redirects"] is False for call in request.call_args_list))

    def test_expired_tokens_retry_with_fresh_credentials_once(self):
        failures = [self.response(403), self.response(401), self.response(data={"success": False,
                    "result": {"name": "Unauthorized", "status": 401}}),
                    self.response(302, location="https://zjuam.zju.edu.cn/cas/login")]
        for failed in failures:
            with self.subTest(status=failed.status_code):
                c = self.client()
                with mock.patch.object(c.s, "request", side_effect=[failed, self.response()]) as request, \
                        mock.patch.object(zju, "get_credentials", return_value=("test", "password")):
                    self.assertEqual(c.infosimple()["account"], "test")
                self.assertEqual(c.login.call_count, 1)
                self.assertEqual([call.kwargs["headers"]["Authorization"] for call in request.call_args_list],
                                 ["Bearer old", "Bearer fresh"])

    def test_missing_token_failed_login_and_repeated_denial(self):
        c = self.client(token=None)
        with mock.patch.object(c.s, "request", return_value=self.response()) as request, \
                mock.patch.object(zju, "get_credentials", return_value=("test", "password")):
            self.assertEqual(c.infosimple()["account"], "test")
            self.assertEqual(request.call_count, 1)

            c.login.assert_called_once()
        c = self.client()
        with mock.patch.object(c.s, "request", return_value=self.response(403)) as request, \
                mock.patch.object(zju, "get_credentials", return_value=("test", "password")):
            with self.assertRaisesRegex(zju.ZjuError, "仍被拒绝"):
                c.infosimple()
            self.assertEqual(request.call_count, 2)
            c.login.assert_called_once()
        c = self.client()
        c.login.side_effect = zju.ZjuError("login failed")
        with mock.patch.object(c.s, "request", return_value=self.response(403)) as request, \
                mock.patch.object(zju, "get_credentials", return_value=("test", "password")):
            with self.assertRaisesRegex(zju.ZjuError, "login failed"):
                c.infosimple()
            self.assertEqual(request.call_count, 1)

    def test_expiry_during_course_detail_keeps_source_checks_and_refreshes_headers(self):
        c = self.client()
        detail = {"data": {"title": "course", "sub_list": {"y": {"m": {"w": [
            {"id": 2, "sub_title": "lesson", "show": "yes"}]}}}}}
        catalogue = {"success": True, "result": {"data": [
            {"sub_id": 2, "content": {"url": "https://video.example/part.mp4"}}]}}
        replies = [self.response(), self.response(data={"result": {"status": 401}}),
                   self.response(data=detail), self.response(data=catalogue)]
        with mock.patch.object(c.s, "request", side_effect=replies) as request, \
                mock.patch.object(zju, "get_credentials", return_value=("test", "password")), \
                tempfile.TemporaryDirectory() as root:
            result = zju.recording_frame_source(c, fixture.args(course=1, sub=2), Path(root))
        self.assertEqual(result, ("https://video.example/part.mp4", 1))
        self.assertEqual(request.call_count, 4)
        c.login.assert_called_once()
        self.assertEqual([call.kwargs["headers"]["Authorization"] for call in request.call_args_list],
                         ["Bearer old", "Bearer old", "Bearer fresh", "Bearer fresh"])

    def test_non_auth_failures_and_other_commands_do_not_reauthenticate(self):
        for response in [self.response(500), self.response(302, location="https://evil.invalid/login")]:
            c = self.client()
            with mock.patch.object(c.s, "request", return_value=response):
                with self.assertRaises(zju.ZjuError):
                    c.infosimple()
            c.login.assert_not_called()
        c = self.client(fast=False)
        c.courses_alive = mock.Mock(return_value=True)
        with mock.patch.object(c.s, "request", return_value=self.response()):
            c.infosimple()
        c.courses_alive.assert_called_once()
