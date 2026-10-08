"""不連網的單元測試：python -m unittest discover tests（需 requests / img2pdf / pillow / numpy）。"""
import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("zju", ROOT / "zju.py")
zju = importlib.util.module_from_spec(spec)
spec.loader.exec_module(zju)


class Offline(unittest.TestCase):
    def test_rsa_no_padding_roundtrip(self):
        # 與 CAS 前端同一套 textbook RSA：m^e mod n，hex 輸出
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
            for x, y, w in lines:  # 一行字 ≈ 一條細橫線
                d.rectangle([x, y, x + w, y + 8], fill=(0, 0, 0))
            if ink:  # 老師的紅筆註記
                d.line(ink, fill=(220, 0, 0), width=5)
            return im

        a_title = [(100, 60, 600)]
        a_full = a_title + [(120, 200, 800), (120, 280, 700), (120, 360, 900)]
        b = [(100, 60, 400), (150, 250, 500), (150, 450, 600), (300, 600, 300)]
        scribble = [(700, 300), (900, 420), (1100, 300), (900, 200)]
        pages = [
            slide(a_title),           # 0 動畫第一步 → 被 1 包含
            slide(a_full),            # 1 保留
            slide(b),                 # 2 → 被 3（寫了註記）包含
            slide([], bg=0),          # 3 全黑過場
            slide(b, ink=scribble),   # 4 保留
            slide(b),                 # 5 擦掉註記的乾淨版 → 被 4 包含
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
        self.assertEqual(zju.local_time(None), "時間未定")
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
            Image.new("RGBA", (40, 30), (255, 0, 0, 128)).save(d / "a.png")  # alpha：走 Pillow 轉檔分支
            Image.new("RGB", (40, 30)).save(d / "b.jpg")
            zju.images_to_pdf([d / "a.png", d / "b.jpg"], d / "out.pdf")
            self.assertEqual((d / "out.pdf").read_bytes()[:5], b"%PDF-")


    def test_safe_name_windows(self):
        self.assertEqual(zju.safe_name("CON.pdf"), "_CON.pdf")
        self.assertEqual(zju.safe_name("x . "), "x")

    def test_local_time_naive_is_beijing(self):
        # 沒帶時區 = 北京時間；換算成 UTC+8 顯示應不變
        import datetime as dt
        naive = zju.local_time("2026-09-27 23:59:00", "%Y-%m-%d %H:%M")
        want = dt.datetime(2026, 9, 27, 23, 59, tzinfo=zju.CST).astimezone().strftime("%Y-%m-%d %H:%M")
        self.assertEqual(naive, want)

    def test_secure_url(self):
        self.assertEqual(zju.secure_url("http://video.cmc.zju.edu.cn/a.jpg"), "https://video.cmc.zju.edu.cn/a.jpg")
        self.assertEqual(zju.secure_url("http://example.com/a.jpg"), "http://example.com/a.jpg")

    def test_http_never_carries_cookies(self):
        """.zju.edu.cn 的 SSO cookie 沒設 Secure：http:// 請求必須被剝掉 Cookie。"""
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
        # 與前端 getDownloadRefer 同規則：classroom→classroom_activity、exam 不帶、其餘→learning_activity
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
        """排程未開放：前兩層 403、第 3 層帶 snake_case refer 回原檔，不再落到 preview PDF。"""
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
        self.assertEqual(src, "排程原檔")
        self.assertEqual(len(calls), 3)  # refer 成功就不需要 preview PDF
        self.assertTrue(calls[0][0].endswith("/reference/17939358/blob"))
        self.assertEqual(calls[2], ("https://courses.zju.edu.cn/api/uploads/2316787/blob",
                                    {"refer_id": 1164596, "refer_type": "learning_activity"}))

    def test_upload_response_no_refer_for_exam(self):
        """exam 活動不帶 refer：只走原三層，exam 未開放時照樣 403 拋錯。"""
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
        self.assertEqual(len(calls), 3)  # reference blob、uid blob、preview url（沒有 refer 層）
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
            self.assertEqual(sorted(p.name for p in d.iterdir()), [])  # 失敗不留任何檔
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
        """真實本地 HTTP：分片並行、順序、重試、回退和損壞檔保護。"""
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
                with self.assertRaises(zju.TooBig):
                    zju.download_video(client, url, dest, 10, 4)
                for mode in ("bad", "truncated"):
                    state["mode"] = mode
                    with self.assertRaises((zju.ZjuError, zju.requests.RequestException)):
                        zju.download_video(client, url, dest, None, 4, chunk_size=128)
                    self.assertEqual(dest.read_bytes(), body)  # 失敗不能覆蓋原檔
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

    def test_video_command_manifest_and_dry_run(self):
        from argparse import Namespace
        from unittest.mock import patch, MagicMock

        subs = [{"course_id": 1, "sub_id": 2, "course_name": "課程", "sub_name": "第一堂"},
                {"course_id": 1, "sub_id": 3, "course_name": "課程", "sub_name": "第二堂"}]
        client = MagicMock()
        client.video_catalogue.return_value = {2: ["https://example.com/a.mp4"], 3: []}
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "output"
            args = Namespace(out=str(root), dry_run=True, force=False, max_size=0, jobs=4)
            with patch.object(zju, "Zju", return_value=client), patch.object(zju, "resolve_subs", return_value=subs), \
                    patch.object(zju, "download_video") as download:
                zju.cmd_video(args)
                download.assert_not_called()
                self.assertFalse(root.exists())
                self.assertEqual(client.video_catalogue.call_count, 1)
                args.dry_run = False
                def save(z, u, dest, limit, jobs, *, restart=False):
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(b"mp4")
                download.side_effect = save
                zju.cmd_video(args)
                zju.cmd_video(args)
                self.assertEqual(download.call_count, 1)
                args.force = True
                zju.cmd_video(args)
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

                # 每輪在第一片完成後斷線，再用全新 Python 程序續傳。
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
                            self.assertIn("另一個程序", concurrent.stderr)
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
                        self.assertIn("[續傳]", result.stderr)
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


if __name__ == "__main__":
    unittest.main()
