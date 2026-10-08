"""不连网的单元测试：python -m unittest discover tests（需 requests / img2pdf / pillow / numpy）。"""
import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("zju", ROOT / "zju.py")
zju = importlib.util.module_from_spec(spec)
spec.loader.exec_module(zju)


class Offline(unittest.TestCase):
    def test_classroom_courses_pagination(self):
        from unittest import mock
        z = zju.Zju.__new__(zju.Zju)
        rows = [{"Id": 10, "Title": "课程一", "Teacher": "教师", "TermName": "2026-20271",
                 "Type": "multi", "progress": {"subjectNum": 8}},
                {"Id": 11, "Title": "课程二", "progress": None}]
        pages = [{"code": 1000, "params": {"result": {"total": 2, "data": [row]}}} for row in rows]
        with mock.patch.object(z, "ensure"), mock.patch.object(z, "bearer", return_value={}), \
                mock.patch.object(z, "get") as get, mock.patch.object(z, "json", side_effect=pages):
            courses = z.classroom_courses()
        self.assertEqual([c["task_count"] for c in courses], [8, 0])
        self.assertEqual([c["course_id"] for c in courses], [10, 11])
        self.assertEqual([call.kwargs["params"]["nowpage"] for call in get.call_args_list], [1, 2])
        self.assertTrue(all(call.kwargs["params"]["force_mycourse"] == 1 for call in get.call_args_list))
        with mock.patch.object(z, "ensure"), mock.patch.object(z, "bearer", return_value={}), \
                mock.patch.object(z, "get"), mock.patch.object(z, "json", return_value=pages[0]):
            with self.assertRaisesRegex(zju.ZjuError, "分页重复"):
                z.classroom_courses()
        with mock.patch.object(z, "ensure"), mock.patch.object(z, "bearer", return_value={}), \
                mock.patch.object(z, "get"), mock.patch.object(z, "json", return_value={"code": 401}):
            with self.assertRaises(zju.ZjuError):
                z.classroom_courses()

    def test_classroom_courses_filter_and_json(self):
        import contextlib
        import io
        import json
        from types import SimpleNamespace
        from unittest import mock
        rows = [{"course_id": 10, "title": "课程一", "teacher": "教师", "term": "学期", "task_count": 8},
                {"course_id": 11, "title": "课程二", "teacher": "", "term": "", "task_count": 0}]
        for has_tasks, want in ((False, rows), (True, rows[:1])):
            output = io.StringIO()
            with mock.patch.object(zju, "Zju") as client, contextlib.redirect_stdout(output):
                client.return_value.classroom_courses.return_value = rows
                zju.cmd_classroom(SimpleNamespace(action="courses", has_tasks=has_tasks, json=True))
            self.assertEqual(json.loads(output.getvalue()), want)

    def test_classroom_sync_workers_and_incremental(self):
        import io
        import threading
        import time
        from argparse import Namespace
        from concurrent.futures import ThreadPoolExecutor
        from types import SimpleNamespace
        from unittest.mock import MagicMock, patch
        from PIL import Image

        image = io.BytesIO()
        Image.new("RGB", (10, 10), "white").save(image, format="PNG")
        client = MagicMock()
        client.classroom_courses.return_value = [{"course_id": 1, "title": "课程", "type": "multi"}]
        subs = [{"course_id": 1, "sub_id": i, "course_name": "课程", "sub_name": f"第{i}堂"}
                for i in range(1, 4)]
        client.course_subs.return_value = subs
        client.ppt_urls.return_value = [f"https://example.com/{i}.png" for i in range(5)]
        client.video_catalogue.return_value = {i: [f"https://example.com/{i}.mp4"] for i in range(1, 4)}
        active = peak = 0
        lock = threading.Lock()

        def transfer(result):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                time.sleep(0.02)
                return result
            finally:
                with lock:
                    active -= 1

        client.subtitle.side_effect = lambda sid: transfer([{"BeginSec": 0, "Text": "转写内容"}])
        client.get.side_effect = lambda url: transfer(SimpleNamespace(ok=True, content=image.getvalue()))
        pools = []

        def create_pool(**kwargs):
            pool = ThreadPoolExecutor(**kwargs)
            pools.append(pool)
            return pool

        def save_video(z, url, dest, limit, jobs, *, restart=False, pool=None):
            self.assertIs(pool, pools[-1])
            futures = [pool.submit(transfer, b"fragment") for _ in range(5)]
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"".join(f.result() for f in futures))

        for jobs in (1, 3):
            active = peak = 0
            pools.clear()
            with tempfile.TemporaryDirectory() as d:
                root = Path(d) / "output"
                args = Namespace(out=str(root), course=["1", "课程"], dry_run=False, force=False,
                                 recordings=True, jobs=jobs, max_size=0, format="md", dedup=False,
                                 keep_images=False)
                with patch.object(zju, "Zju", return_value=client), \
                        patch.object(zju, "ThreadPoolExecutor", side_effect=create_pool), \
                        patch.object(zju, "download_video", side_effect=save_video) as download:
                    zju.cmd_classroom_sync(args)
                    self.assertEqual(len(pools), 1)
                    self.assertEqual(peak, jobs)
                    self.assertEqual(len(list(root.rglob("*.pdf"))), 3)
                    self.assertEqual(len(list(root.rglob("*.md"))), 3)
                    self.assertEqual(len(list(root.rglob("*.mp4"))), 3)
                    self.assertEqual(download.call_count, 3)
                    self.assertEqual((root / "课程/转录/第1堂.md").read_text(), "# 课程 第1堂\n\n**[00:00:00]** 转写内容  \n")
                    client.subtitle.reset_mock()
                    client.ppt_urls.reset_mock()
                    zju.cmd_classroom_sync(args)
                    client.subtitle.assert_not_called()
                    client.ppt_urls.assert_not_called()
                    self.assertEqual(download.call_count, 3)
                # 默认不下载录播；预览也不创建目录或调用材料下载。
                args.out = str(Path(d) / "preview")
                args.dry_run, args.recordings = True, False
                with patch.object(zju, "Zju", return_value=client), \
                        patch.object(zju, "ppt_one") as ppt, patch.object(zju, "transcript_one") as transcript, \
                        patch.object(zju, "recording_subs") as recording:
                    zju.cmd_classroom_sync(args)
                    ppt.assert_not_called()
                    transcript.assert_not_called()
                    recording.assert_not_called()
                    self.assertFalse(Path(args.out).exists())
                    args.dry_run = False
                    zju.cmd_classroom_sync(args)
                    recording.assert_not_called()

    def test_classroom_sync_continues_after_failure(self):
        from argparse import Namespace
        from unittest.mock import MagicMock, patch
        client = MagicMock()
        client.classroom_courses.return_value = [{"course_id": 1, "title": "课程", "type": "multi"}]
        client.course_subs.return_value = [dict(course_id=1, sub_id=1, course_name="课程", sub_name="第一堂")]
        with tempfile.TemporaryDirectory() as d:
            args = Namespace(out=d, course=[], dry_run=False, force=False, jobs=1, recordings=True,
                             format="md", dedup=False, keep_images=False, max_size=0)
            with patch.object(zju, "Zju", return_value=client), \
                    patch.object(zju, "transcript_one", side_effect=zju.ZjuError("转写失败")), \
                    patch.object(zju, "ppt_one") as ppt, \
                    patch.object(zju, "recording_subs", return_value=0) as recording:
                with self.assertRaises(SystemExit) as exc:
                    zju.cmd_classroom_sync(args)
                self.assertEqual(exc.exception.code, 2)
                ppt.assert_called_once()
                recording.assert_called_once()

    def test_rsa_no_padding_roundtrip(self):
        # 与 CAS 前端同一套 textbook RSA：m^e mod n，hex 输出
        p, q, e = 1000000007, 998244353, 65537
        n = p * q
        d = pow(e, -1, (p - 1) * (q - 1))
        pwd = "abc"
        enc = format(pow(int.from_bytes(pwd.encode(), "big"), e, n), "x")
        self.assertEqual(pow(int(enc, 16), d, n).to_bytes(3, "big").decode(), pwd)

    def test_safe_name(self):
        self.assertEqual(zju.safe_name('a/b:c*?"<>|'), "a_b_c______")
        self.assertEqual(zju.safe_name("..."), "_")

    def test_dedup_slides(self):
        from PIL import Image, ImageDraw

        def slide(lines, ink=None, bg=255):
            im = Image.new("RGB", (1280, 720), (bg,) * 3)
            d = ImageDraw.Draw(im)
            for x, y, w in lines:  # 一行字 ≈ 一条细横线
                d.rectangle([x, y, x + w, y + 8], fill=(0, 0, 0))
            if ink:  # 老师的红笔注记
                d.line(ink, fill=(220, 0, 0), width=5)
            return im

        a_title = [(100, 60, 600)]
        a_full = a_title + [(120, 200, 800), (120, 280, 700), (120, 360, 900)]
        b = [(100, 60, 400), (150, 250, 500), (150, 450, 600), (300, 600, 300)]
        scribble = [(700, 300), (900, 420), (1100, 300), (900, 200)]
        pages = [
            slide(a_title),           # 0 动画第一步 → 被 1 包含
            slide(a_full),            # 1 保留
            slide(b),                 # 2 → 被 3（写了注记）包含
            slide([], bg=0),          # 3 全黑过场
            slide(b, ink=scribble),   # 4 保留
            slide(b),                 # 5 擦掉注记的干净版 → 被 4 包含
            slide(a_full),            # 6 翻回前面 → 被 1 包含
        ]
        with tempfile.TemporaryDirectory() as d:
            paths = []
            for i, im in enumerate(pages):
                p = Path(d) / f"{i:04d}.jpg"
                im.save(p, quality=85)
                paths.append(p)
            kept = zju.dedup_slides(paths)
        self.assertEqual([p.stem for p in kept], ["0001", "0004"])

    def test_srt(self):
        out = zju.render_transcript([{"BeginSec": 61.5, "EndSec": 63, "Text": "你好"}], "srt", "t")
        self.assertEqual(out, "1\n00:01:01,500 --> 00:01:03,000\n你好\n\n")

    def test_local_time(self):
        self.assertEqual(zju.local_time(None), "时间未定")
        self.assertRegex(zju.local_time("2026-09-27T15:59:00Z", "%Y-%m-%d"), r"2026-09-2[78]")

    def test_current_year(self):
        cs = [{"academic_year_id": 15, "is_closed": False, "id": 1},
              {"academic_year_id": 14, "is_closed": False, "id": 2},
              {"academic_year_id": 15, "is_closed": True, "id": 3}]
        self.assertEqual([c["id"] for c in zju.current_year(cs)], [1])

    def test_images_to_pdf(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            Image.new("RGBA", (40, 30), (255, 0, 0, 128)).save(d / "a.png")  # alpha：走 Pillow 转档分支
            Image.new("RGB", (40, 30)).save(d / "b.jpg")
            zju.images_to_pdf([d / "a.png", d / "b.jpg"], d / "out.pdf")
            self.assertEqual((d / "out.pdf").read_bytes()[:5], b"%PDF-")


    def test_safe_name_windows(self):
        self.assertEqual(zju.safe_name("CON.pdf"), "_CON.pdf")
        self.assertEqual(zju.safe_name("x . "), "x")

    def test_local_time_naive_is_beijing(self):
        # 没带时区 = 北京时间；换算成 UTC+8 显示应不变
        import datetime as dt
        naive = zju.local_time("2026-09-27 23:59:00", "%Y-%m-%d %H:%M")
        want = dt.datetime(2026, 9, 27, 23, 59, tzinfo=zju.CST).astimezone().strftime("%Y-%m-%d %H:%M")
        self.assertEqual(naive, want)

    def test_secure_url(self):
        self.assertEqual(zju.secure_url("http://video.cmc.zju.edu.cn/a.jpg"), "https://video.cmc.zju.edu.cn/a.jpg")
        self.assertEqual(zju.secure_url("http://example.com/a.jpg"), "http://example.com/a.jpg")

    def test_http_never_carries_cookies(self):
        """.zju.edu.cn 的 SSO cookie 没设 Secure：http:// 请求必须被剥掉 Cookie。"""
        from unittest import mock
        import requests
        seen = {}

        def fake_send(self_, request, **kw):
            seen["headers"] = dict(request.headers)
            r = requests.Response()
            r.status_code = 200
            r._content = b"ok"
            r.url = request.url
            r.request = request
            return r

        z = zju.Zju.__new__(zju.Zju)
        z.jar = requests.cookies.RequestsCookieJar()
        z.jar.set("iPlanetDirectoryPro", "SECRET", domain=".zju.edu.cn", path="/")
        import threading
        z._tl = threading.local()
        with mock.patch.object(requests.adapters.HTTPAdapter, "send", fake_send):
            z.s.get("http://video.cmc.zju.edu.cn/x.jpg")
            self.assertNotIn("Cookie", seen["headers"])
            z.s.get("https://courses.zju.edu.cn/api/x")
            self.assertIn("SECRET", seen["headers"].get("Cookie", ""))

    def test_refer_params(self):
        # 与前端 getDownloadRefer 同规则：classroom→classroom_activity、exam 不带、其余→learning_activity
        self.assertEqual(zju.refer_params({"id": 1164596, "type": "online_video"}),
                         {"refer_id": 1164596, "refer_type": "learning_activity"})
        self.assertEqual(zju.refer_params({"id": 1159399, "type": "material"}),
                         {"refer_id": 1159399, "refer_type": "learning_activity"})
        self.assertEqual(zju.refer_params({"id": 7, "type": "classroom"}),
                         {"refer_id": 7, "refer_type": "classroom_activity"})
        self.assertIsNone(zju.refer_params({"id": 7, "type": "exam"}))
        self.assertIsNone(zju.refer_params({"type": "material"}))  # 缺 id
        self.assertIsNone(zju.refer_params(None))

    def test_upload_response_refer_fallback(self):
        """排程未开放：前两层 403、第 3 层带 snake_case refer 回原档，不再落到 preview PDF。"""
        import io
        import requests
        from unittest import mock

        calls = []

        def fake_get(self_, url, **kw):
            calls.append((url, kw.get("params")))
            ok = bool((kw.get("params") or {}).get("refer_id"))
            r = requests.Response()
            r.raw = io.BytesIO(b"x" * 10 if ok else b"")
            r.status_code = 200 if ok else 403
            r.headers["Content-Type"] = "application/octet-stream"
            if ok:
                r.headers["Content-Length"] = "10"
            return r

        z = zju.Zju.__new__(zju.Zju)
        with mock.patch.object(zju.Zju, "get", fake_get):
            r, src = z.upload_response(2316787, 17939358, {"id": 1164596, "type": "online_video"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(src, "排程原档")
        self.assertEqual(len(calls), 3)  # refer 成功就不需要 preview PDF
        self.assertTrue(calls[0][0].endswith("/reference/17939358/blob"))
        self.assertEqual(calls[2], ("https://courses.zju.edu.cn/api/uploads/2316787/blob",
                                    {"refer_id": 1164596, "refer_type": "learning_activity"}))

    def test_upload_response_no_refer_for_exam(self):
        """exam 活动不带 refer：只走原三层，exam 未开放时照样 403 抛错。"""
        import io
        import requests
        from unittest import mock

        calls = []

        def fake_get(self_, url, **kw):
            calls.append((url, kw.get("params")))
            r = requests.Response()
            r.raw = io.BytesIO(b"")
            r.status_code = 403
            return r

        z = zju.Zju.__new__(zju.Zju)
        with mock.patch.object(zju.Zju, "get", fake_get):
            with self.assertRaises(zju.DownloadError) as cm:
                z.upload_response(1, 2, {"id": 9, "type": "exam"})
        self.assertEqual(len(calls), 3)  # reference blob、uid blob、preview url（没有 refer 层）
        self.assertTrue(all("refer_id" not in (p or {}) for _, p in calls))
        self.assertEqual(cm.exception.codes, [403, 403, 403])

    def test_stream_to_rejects_truncated(self):
        import io
        import requests

        def resp(body, length=None, ctype="application/octet-stream"):
            r = requests.Response()
            r.status_code = 200
            r.raw = io.BytesIO(body)
            r.headers["Content-Type"] = ctype
            if length is not None:
                r.headers["Content-Length"] = str(length)
            return r

        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            with self.assertRaises(zju.ZjuError):
                zju.stream_to(resp(b"abc", 10), d / "a.bin")
            with self.assertRaises(zju.ZjuError):
                zju.stream_to(resp(b""), d / "b.bin")
            with self.assertRaises(zju.ZjuError):
                zju.stream_to(resp(b"<html>", ctype="text/html; charset=utf-8"), d / "c.pdf")
            with self.assertRaises(zju.TooBig):
                zju.stream_to(resp(b"x" * 10, 10), d / "d.bin", limit=5)
            self.assertEqual(sorted(p.name for p in d.iterdir()), [])  # 失败不留任何档
            out = zju.stream_to(resp(b"%PDF-1.4 ok", 11), d / "e.pptx")
            self.assertEqual(out.name, "e.pptx.pdf")

    def test_video_catalogue_formats(self):
        import json
        items = [
            {"sub_id": "1", "content": json.dumps({"playback": {"url": [
                "http://resource.cmc.zju.edu.cn/a.mp4", "http://resource.cmc.zju.edu.cn/a.mp4",
                "https://resource.cmc.zju.edu.cn/b.mp4"]}})},
            {"sub_id": 2, "content": {"url": "https://example.com/c.mp4"}},
            {"sub_id": 3, "content": "{}"},
        ]
        self.assertEqual(zju.parse_video_catalogue(items), {
            1: ["https://resource.cmc.zju.edu.cn/a.mp4", "https://resource.cmc.zju.edu.cn/b.mp4"],
            2: ["https://example.com/c.mp4"], 3: []})
        for content in ("invalid json", {"url": "file:///etc/passwd"}, {"url": 42}):
            with self.assertRaises(zju.ZjuError):
                zju.parse_video_catalogue([{"sub_id": 1, "content": content}])

    def test_video_download_ranges(self):
        """真实本地 HTTP：分片并行、顺序、重试、回退和损坏档保护。"""
        import threading
        import time
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from unittest.mock import patch

        body = b"\x00\x00\x00\x20ftypisom" + bytes(range(256)) * 4
        state = {"mode": "range", "active": 0, "max_active": 0, "retried": False}
        lock = threading.Lock()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                start, end = map(int, self.headers["Range"][6:].split("-"))
                if state["mode"] == "fallback":
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                with lock:
                    state["active"] += 1
                    state["max_active"] = max(state["max_active"], state["active"])
                    retry_once = start == 128 and not state["retried"]
                    if retry_once:
                        state["retried"] = True
                try:
                    time.sleep(0.02)
                    self.send_response(206)
                    bad_range = (state["mode"] == "bad" or retry_once) and end != 0
                    self.send_header("Content-Range", f"bytes {start + int(bad_range)}-{end}/{len(body)}")
                    self.send_header("Content-Length", str(end - start + 1))
                    self.send_header("ETag", '"test-video"')
                    self.end_headers()
                    try:
                        stop = end if state["mode"] == "truncated" and end != 0 else end + 1
                        self.wfile.write(body[start:stop])
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                finally:
                    with lock:
                        state["active"] -= 1

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with tempfile.TemporaryDirectory() as d, patch.object(zju.Zju, "_load_cookies"):
                client = zju.Zju()
                url = f"http://127.0.0.1:{server.server_port}/video.mp4"
                dest = Path(d) / "video.mp4"
                zju.download_video(client, url, dest, None, 4, chunk_size=128)
                self.assertEqual(dest.read_bytes(), body)
                self.assertGreater(state["max_active"], 1)
                self.assertTrue(state["retried"])
                from concurrent.futures import ThreadPoolExecutor
                with ThreadPoolExecutor(max_workers=2) as pool, \
                        patch.object(zju, "ThreadPoolExecutor", side_effect=AssertionError("不应创建嵌套线程池")):
                    state["max_active"] = 0
                    zju.download_video(client, url, dest, None, 99, chunk_size=128, pool=pool, restart=True)
                    self.assertEqual(dest.read_bytes(), body)
                    self.assertEqual(state["max_active"], 2)
                    state["mode"] = "bad"
                    with self.assertRaises(zju.ZjuError):
                        zju.download_video(client, url, dest, None, 99, chunk_size=128, pool=pool, restart=True)
                    self.assertEqual(state["active"], 0)
                    self.assertEqual(pool.submit(lambda: "可复用").result(), "可复用")
                    state["mode"] = "good"
                with self.assertRaises(zju.TooBig):
                    zju.download_video(client, url, dest, 10, 4)
                for mode in ("bad", "truncated"):
                    state["mode"] = mode
                    with self.assertRaises((zju.ZjuError, zju.requests.RequestException)):
                        zju.download_video(client, url, dest, None, 4, chunk_size=128)
                    self.assertEqual(dest.read_bytes(), body)  # 失败不能覆盖原档
                    self.assertTrue((Path(d) / ".video.mp4.part").exists())
                    self.assertTrue((Path(d) / ".video.mp4.part.json").exists())
                state["mode"] = "fallback"
                zju.download_video(client, url, dest, None, 4)
                self.assertEqual(dest.read_bytes(), body)
                self.assertFalse((Path(d) / ".video.mp4.part").exists())
                self.assertFalse((Path(d) / ".video.mp4.part.json").exists())
        finally:
            server.shutdown()
            server.server_close()
            worker.join()

    def test_recording_command_manifest_and_dry_run(self):
        from argparse import Namespace
        from unittest.mock import patch, MagicMock

        subs = [{"course_id": 1, "sub_id": 2, "course_name": "课程", "sub_name": "第一堂"},
                {"course_id": 1, "sub_id": 3, "course_name": "课程", "sub_name": "第二堂"}]
        client = MagicMock()
        client.video_catalogue.return_value = {2: ["https://example.com/a.mp4"], 3: []}
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "output"
            args = Namespace(out=str(root), dry_run=True, force=False, max_size=0, jobs=4)
            with patch.object(zju, "Zju", return_value=client), patch.object(zju, "resolve_subs", return_value=subs), \
                    patch.object(zju, "download_video") as download:
                zju.cmd_recording(args)
                download.assert_not_called()
                self.assertFalse(root.exists())
                self.assertEqual(client.video_catalogue.call_count, 1)
                args.dry_run = False
                def save(z, u, dest, limit, jobs, *, restart=False):
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(b"mp4")
                download.side_effect = save
                zju.cmd_recording(args)
                zju.cmd_recording(args)
                self.assertEqual(download.call_count, 1)
                args.force = True
                zju.cmd_recording(args)
                self.assertEqual(download.call_count, 2)
                self.assertTrue(download.call_args.kwargs["restart"])

    def test_video_resume_across_processes(self):
        import json
        import os
        import subprocess
        import sys
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading
        import time

        body = b"\x00\x00\x00\x20ftypisom" + bytes(range(256)) * 4
        state = {"fail": True, "etag": '"v1"', "requests": []}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                start, end = map(int, self.headers["Range"][6:].split("-"))
                state["requests"].append((start, end))
                time.sleep(0.02)
                self.send_response(206)
                wrong = state["fail"] and start == 128
                self.send_header("Content-Range", f"bytes {start + int(wrong)}-{end}/{len(body)}")
                self.send_header("Content-Length", str(end - start + 1))
                if state["etag"]:
                    self.send_header("ETag", state["etag"])
                self.end_headers()
                try:
                    self.wfile.write(body[start:end + 1])
                except (BrokenPipeError, ConnectionResetError):
                    pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with tempfile.TemporaryDirectory() as d:
                dest = Path(d) / "video.mp4"
                partial = dest.with_name(".video.mp4.part")
                checkpoint = dest.with_name(".video.mp4.part.json")
                script = "import sys, zju; from pathlib import Path; zju.download_video(zju.Zju(), sys.argv[1], Path(sys.argv[2]), None, 1, chunk_size=128, restart=sys.argv[3] == 'True')"
                url = f"http://127.0.0.1:{server.server_port}/video.mp4"

                def run(restart=False, query=""):
                    return subprocess.run([sys.executable, "-c", script, url + query, str(dest), str(restart)],
                                          cwd=ROOT, capture_output=True, text=True, timeout=20,
                                          env=dict(os.environ, ZJU_STATE_DIR=str(Path(d) / "state")))

                # 每轮在第一片完成后断线，再用全新 Python 程序续传。
                for mode in ("interrupted", "resume", "refresh", "corrupt", "version", "force", "checkpoint", "no_validator"):
                    state["fail"] = True
                    state["etag"] = "" if mode == "no_validator" else '"v1"'
                    if mode == "interrupted":
                        process = subprocess.Popen([sys.executable, "-c", script, url, str(dest), "False"],
                                                   cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                                   env=dict(os.environ, ZJU_STATE_DIR=str(Path(d) / "state")))
                        try:
                            deadline = time.monotonic() + 5
                            while time.monotonic() < deadline:
                                try:
                                    if "0" in json.loads(checkpoint.read_text())["done"]:
                                        break
                                except (OSError, ValueError, KeyError):
                                    pass
                                time.sleep(0.01)
                            self.assertIn("0", json.loads(checkpoint.read_text())["done"])
                            concurrent = run()
                            self.assertNotEqual(concurrent.returncode, 0)
                            self.assertIn("另一个程序", concurrent.stderr)
                        finally:
                            if process.poll() is None:
                                process.terminate()
                            process.communicate(timeout=10)
                    else:
                        self.assertNotEqual(run().returncode, 0)
                    self.assertTrue(partial.exists())
                    saved = json.loads(checkpoint.read_text())
                    self.assertIn("0", saved["done"])
                    self.assertFalse(dest.exists())
                    if mode == "corrupt":
                        with partial.open("r+b") as f:
                            f.write(b"bad!")
                    elif mode == "version":
                        state["etag"] = '"v2"'
                    elif mode == "checkpoint":
                        checkpoint.write_text("broken json")
                    state["requests"].clear()
                    state["fail"] = False
                    result = run(restart=mode == "force", query="?signature=renewed" if mode == "refresh" else "")
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(dest.read_bytes(), body)
                    self.assertFalse(partial.exists())
                    self.assertFalse(checkpoint.exists())
                    chunks = [start for start, end in state["requests"] if end != 0]
                    if mode in ("resume", "interrupted", "refresh"):
                        for start in saved["done"]:
                            self.assertNotIn(int(start), chunks)
                        self.assertIn("[续传]", result.stderr)
                    else:
                        self.assertIn(0, chunks)
                    dest.unlink()
        finally:
            server.shutdown()
            server.server_close()
            worker.join()

    def test_video_rejects_non_mp4(self):
        import io
        import requests
        with tempfile.TemporaryDirectory() as d:
            r = requests.Response()
            r.status_code = 200
            r.raw = io.BytesIO(b"#EXTM3U\nhttps://example.com/segment.ts")
            with self.assertRaises(zju.ZjuError):
                zju.stream_to(r, Path(d) / "video.mp4", mp4=True)
            self.assertEqual(list(Path(d).iterdir()), [])

    def test_simplified_paths_preserve_existing_materials(self):
        with tempfile.TemporaryDirectory() as d:
            course_dir = Path(d) / "课程"
            for kind, legacy, name in (("智云PPT", "智雲PPT", "课堂.pdf"),
                                       ("转录", "轉錄", "课堂.md"),
                                       ("录播", "錄播", "课堂.mp4")):
                self.assertEqual(zju.material_path(course_dir, kind, name), course_dir / kind / name)
                old = course_dir / legacy / name
                old.parent.mkdir(parents=True, exist_ok=True)
                old.write_bytes(b"existing")
                self.assertEqual(zju.material_path(course_dir, kind, name), old)
                new = course_dir / kind / name
                new.parent.mkdir(parents=True, exist_ok=True)
                new.write_bytes(b"new")
                self.assertEqual(zju.material_path(course_dir, kind, name), new)
            old_video = course_dir / "錄播" / "未完成.mp4"
            partial = old_video.with_name(f".{old_video.name}.part")
            partial.write_bytes(b"partial")
            self.assertEqual(zju.material_path(course_dir, "录播", old_video.name), old_video)
            self.assertEqual(partial.read_bytes(), b"partial")


if __name__ == "__main__":
    unittest.main()
