"""课程目录自描述及转写精度：只使用离线 API 替身和 wheel 生成的真实文件。"""
import contextlib
import datetime as dt
import importlib.util
import io
import json
import tempfile
import unittest
from argparse import Namespace
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import av
import numpy as np
from PIL import Image

spec = importlib.util.spec_from_file_location("zju_metadata_tests", Path(__file__).resolve().parents[1] / "zju.py")
zju = importlib.util.module_from_spec(spec)
spec.loader.exec_module(zju)


class CourseMetadata(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = tempfile.TemporaryDirectory()
        root = Path(cls.fixture.name)
        video = root / "fixture.mp4"
        with av.open(str(video), "w") as output:
            v = output.add_stream("mpeg4", rate=10)
            v.width = v.height = 64
            v.pix_fmt = "yuv420p"
            a = output.add_stream("aac", rate=32000)
            a.layout = "mono"
            for i in range(20):
                frame = av.VideoFrame.from_ndarray(np.zeros((64, 64, 3), dtype=np.uint8), format="rgb24")
                frame.pts, frame.time_base = i, Fraction(1, 10)
                for packet in v.encode(frame):
                    output.mux(packet)
                frame = av.AudioFrame.from_ndarray(np.zeros((1, 3200), dtype=np.float32), format="fltp", layout="mono")
                frame.sample_rate, frame.pts, frame.time_base = 32000, i * 3200, Fraction(1, 32000)
                for packet in a.encode(frame):
                    output.mux(packet)
            for stream in (v, a):
                for packet in stream.encode():
                    output.mux(packet)
        audio = root / "fixture.m4a"
        with contextlib.redirect_stderr(io.StringIO()):
            zju.make_audio(None, video, audio)
        image = root / "slide.png"
        Image.new("RGB", (16, 16), "white").save(image)
        pdf = root / "slide.pdf"
        zju.images_to_pdf([image], pdf)
        cls.video, cls.audio, cls.pdf, cls.image = (p.read_bytes() for p in (video, audio, pdf, image))

    @classmethod
    def tearDownClass(cls):
        cls.fixture.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "output"
        self.course = dict(course_id=1, title="课程", teacher="教师", term="2026秋", type="multi", task_count=2)
        self.sub = dict(course_id=1, course_name="课程", sub_id=11, sub_name="第一堂",
                        lecturer="授课教师", day=dt.date(2026, 10, 1), show="yes")
        self.client = mock.MagicMock()
        self.client.classroom_courses.return_value = [self.course]
        self.client.course_subs.return_value = [self.sub]
        self.client.subtitle.return_value = []
        self.client.ppt_events.return_value = []
        self.client.video_catalogue.return_value = {11: ["https://example.com/1", "https://example.com/2"]}
        self.args = Namespace(out=str(self.root), course=[], jobs=2, force=False, dry_run=False,
                              recording=False, recording_audio=False, format="md", dedup=False,
                              keep_images=False, max_size=0)

    @property
    def directory(self):
        return self.root / "课程 (1)"

    def sync(self, failure=False):
        with mock.patch.object(zju, "Zju", return_value=self.client), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            if failure:
                with self.assertRaises(SystemExit) as error:
                    zju.cmd_classroom_sync(self.args)
                self.assertEqual(error.exception.code, 2)
            else:
                zju.cmd_classroom_sync(self.args)

    def metadata(self, directory=None):
        return json.loads(((directory or self.directory) / "course.json").read_text(encoding="utf-8"))

    def seed(self, *, parts=2):
        """模拟旧版本留下的完整目录，尚无 course.json。"""
        records = {}
        for extension, content in (("md", "# 旧转写\n\n**[00:00:00 → 00:00:02]** 内容\n"),
                                   ("srt", "1\n00:00:00,000 --> 00:00:02,000\n内容\n"), ("txt", "内容\n")):
            path = zju.classroom_material_path(self.root, self.sub, "转录", extension)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        pdf = zju.classroom_material_path(self.root, self.sub, "智云PPT", "pdf")
        pdf.parent.mkdir(parents=True, exist_ok=True)
        pdf.write_bytes(self.pdf)
        pdf.with_suffix(".json").write_text(json.dumps(dict(schema_version=1, course_id=1, sub_id=11,
            pdf_file=pdf.name, events=[dict(created_sec=1.23, source={"url": "https://example.com/?SECRET"})],
            pages=[])), encoding="utf-8")
        for part in range(1, parts + 1):
            suffix = f" - {part:02d}" if parts > 1 else ""
            for kind, extension, content, key in (("音频", "m4a", self.audio, "audio"),
                                                  ("录播", "mp4", self.video, "video")):
                path = zju.classroom_material_path(self.root, self.sub, kind, extension, suffix)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
                records[f"{key}:1:11:{part}"] = dict(path=str(path.relative_to(self.root)), size=len(content),
                                                     course_id=1, sub_id=11)
        (self.root / ".zju_manifest.json").write_text(json.dumps(records), encoding="utf-8")

    def test_first_sync_generates_materials_and_self_description_without_extra_queries(self):
        self.client.subtitle.return_value = [dict(BeginSec=5025.12, EndSec=5032.875, Text="内容")]
        self.client.ppt_events.return_value = [dict(url="https://example.com/slide?SECRET", created_sec=1.23,
                                                  source={"created_sec": 1.23})]
        self.client.get.return_value = SimpleNamespace(ok=True, content=self.image)
        self.client.course_subs.return_value = [self.sub, dict(self.sub, sub_id=12, show="no", day=None),
                                                dict(self.sub, sub_id=13, show=None)]
        self.sync()
        data = self.metadata()
        self.assertEqual((data["schema_version"], data["source"], data["course_id"]), (1, "zju-classroom", 1))
        self.assertEqual((data["name"], data["teacher"], data["term"]), ("课程", "教师", "2026秋"))
        self.assertIsNotNone(dt.datetime.fromisoformat(data["updated_at"]).tzinfo)
        self.assertEqual(data["lessons_fetch_status"], "ok")
        lessons = {s["sub_id"]: s for s in data["lessons"]}
        self.assertEqual(lessons[11]["date"], "2026-10-01")
        self.assertEqual(lessons[11]["lecturer"], "授课教师")
        self.assertEqual([lessons[i]["availability"] for i in (11, 12, 13)], ["listed", "unlisted", "unknown"])
        for lesson in lessons.values():
            files = lesson["files"]
            self.assertEqual([len(files[k]) for k in ("transcripts", "ppt_pdfs", "ppt_events", "audio", "recordings")],
                             [1, 1, 1, 0, 0])
            for entries in files.values():
                for entry in entries:
                    self.assertFalse(Path(entry["path"]).is_absolute())
                    self.assertTrue((self.directory / entry["path"]).is_file())
                    self.assertNotIn("\\", entry["path"])
        self.assertNotIn("SECRET", json.dumps(data))
        self.client.classroom_courses.assert_called_once_with()
        self.client.course_subs.assert_called_once_with(1)
        self.client.video_catalogue.assert_not_called()

    def test_existing_directory_backfilled_without_download_with_all_media_combinations(self):
        self.seed()
        old_md = self.directory / "转录/第一堂 (11).md"
        before = old_md.read_bytes(), old_md.stat().st_mtime_ns
        manifest = (self.root / ".zju_manifest.json").read_bytes()
        for recording, audio in ((False, False), (True, False), (False, True), (True, True)):
            with self.subTest(recording=recording, audio=audio):
                self.args.recording, self.args.recording_audio = recording, audio
                with mock.patch.object(zju, "download_video", side_effect=AssertionError("重新下载视频")), \
                        mock.patch.object(zju, "make_audio", side_effect=AssertionError("重新提取音频")):
                    self.sync()
                files = self.metadata()["lessons"][0]["files"]
                self.assertEqual({x["format"] for x in files["transcripts"]}, {"md", "txt", "srt"})
                for kind in ("audio", "recordings"):
                    self.assertEqual([x["part"] for x in files[kind]], [1, 2])
                    self.assertTrue(all(" - 0" in x["path"] for x in files[kind]))
                self.client.subtitle.assert_not_called()
                self.client.ppt_events.assert_not_called()
                self.assertEqual((old_md.read_bytes(), old_md.stat().st_mtime_ns), before)
                self.assertEqual((self.root / ".zju_manifest.json").read_bytes(), manifest)

    def test_incremental_updates_names_and_keeps_omitted_lessons_without_moving_files(self):
        self.seed(parts=1)
        old_lesson = dict(self.sub, sub_id=12, sub_name="第二堂", show="no")
        self.client.course_subs.return_value = [self.sub, old_lesson]
        self.sync()
        before = self.metadata()
        self.course.update(title="新课程名", teacher="新教师", term="2027春")
        changed = dict(self.sub, course_name="新课程名", sub_name="更正堂次", lecturer="新授课教师",
                       day=dt.date(2026, 10, 2), show=None)
        self.client.course_subs.return_value = [changed, dict(changed, sub_id=13, show="yes")]
        self.sync()
        data = self.metadata()
        self.assertEqual((data["name"], data["teacher"], data["term"]), ("新课程名", "新教师", "2027春"))
        lessons = {s["sub_id"]: s for s in data["lessons"]}
        self.assertEqual(set(lessons), {11, 12, 13})
        self.assertEqual((lessons[11]["name"], lessons[11]["date"], lessons[11]["availability"]),
                         ("更正堂次", "2026-10-02", "listed"))
        self.assertEqual(lessons[12], next(s for s in before["lessons"] if s["sub_id"] == 12))
        self.assertTrue(lessons[11]["files"]["audio"])
        new = self.metadata(self.root / "新课程名 (1)")
        self.assertFalse(any(s["files"]["audio"] for s in new["lessons"]))
        self.assertTrue((self.directory / "音频/第一堂 (11).m4a").is_file())

    def test_failed_lesson_query_preserves_known_information_and_marks_uncertainty(self):
        self.seed(parts=1)
        self.sync()
        before = self.metadata()
        self.client.course_subs.side_effect = zju.ZjuError("接口暂时失败")
        self.sync(failure=True)
        data = self.metadata()
        self.assertEqual(data["lessons"], before["lessons"])
        self.assertEqual(data["lessons_checked_at"], before["lessons_checked_at"])
        self.assertEqual(data["lessons_fetch_status"], "error")
        # 首次补建也可辨认已存文件，但不由文件名猜日期或下架状态。
        (self.directory / "course.json").unlink()
        self.sync(failure=True)
        lesson = self.metadata()["lessons"][0]
        self.assertEqual(lesson["availability"], "unknown")
        self.assertIsNone(lesson["date"])
        self.assertTrue(lesson["files"]["audio"])

    def test_partial_force_failures_preserve_existing_files_and_do_not_claim_new_downloads(self):
        self.seed(parts=1)
        self.sync()
        before = self.metadata()["lessons"][0]
        self.client.course_subs.return_value = [self.sub, dict(self.sub, sub_id=12, sub_name="第二堂")]
        self.client.subtitle.side_effect = zju.ZjuError("转写失败")
        self.client.ppt_events.side_effect = zju.ZjuError("PPT 失败")
        self.client.video_catalogue.return_value = {11: ["https://example.com/video"], 12: ["https://example.com/new"]}
        self.args.force = self.args.recording = self.args.recording_audio = True
        with mock.patch.object(zju, "download_video", side_effect=zju.ZjuError("视频失败")), \
                mock.patch.object(zju, "make_audio", side_effect=zju.ZjuError("音频失败")):
            self.sync(failure=True)
        lessons = {s["sub_id"]: s for s in self.metadata()["lessons"]}
        self.assertEqual(lessons[11]["files"], before["files"])
        self.assertTrue(all(not files for files in lessons[12]["files"].values()))
        self.assertEqual(lessons[12]["availability"], "listed")

    def test_invalid_unfinished_and_orphan_files_not_reported(self):
        self.seed()
        (self.directory / "音频/第一堂 (11) - 01.m4a").write_bytes(self.audio[:-1])
        records_path = self.root / ".zju_manifest.json"
        records = json.loads(records_path.read_text())
        records["video:1:11:2"]["size"] += 1
        records_path.write_text(json.dumps(records))
        for relative, content in (("录播/第一堂 (11) - 03.mp4.part", self.video),
                                  ("录播/.第一堂 (11) - 04.mp4", self.video),
                                  ("音频/第一堂 (11) - 03.m4a", b""),
                                  ("转录/第一堂 (11).txt", b"\xff"),
                                  ("智云PPT/错误 (12).pdf", b"<html>error</html>")):
            (self.directory / relative).write_bytes(content)
        events = json.loads((self.directory / "智云PPT/第一堂 (11).json").read_text())
        events.update(sub_id=12, pdf_file="错误 (12).pdf")
        (self.directory / "智云PPT/错误 (12).json").write_text(json.dumps(events))
        self.sync()
        lessons = {s["sub_id"]: s for s in self.metadata()["lessons"]}
        files = lessons[11]["files"]
        self.assertEqual([f["part"] for f in files["audio"]], [2])
        self.assertEqual([f["part"] for f in files["recordings"]], [1])
        self.assertEqual({f["format"] for f in files["transcripts"]}, {"md", "srt"})
        self.assertFalse(any(lessons.get(12, {}).get("files", {}).values()))

    def test_dry_run_does_not_create_or_modify_metadata_or_other_files(self):
        self.args.dry_run = self.args.recording = self.args.recording_audio = True
        self.sync()
        self.assertFalse(self.root.exists())
        self.seed()
        self.args.dry_run = False
        self.sync()
        snapshot = {p.relative_to(self.root): (p.read_bytes(), p.stat().st_mtime_ns)
                    for p in self.root.rglob("*") if p.is_file()}
        self.args.dry_run = True
        self.sync()
        self.assertEqual({p.relative_to(self.root): (p.read_bytes(), p.stat().st_mtime_ns)
                          for p in self.root.rglob("*") if p.is_file()}, snapshot)

    def test_multiple_courses_and_output_roots_are_isolated_even_if_one_query_fails(self):
        other = dict(self.course, course_id=2, title="另一课程")
        other_sub = dict(self.sub, course_id=2, course_name="另一课程")
        self.client.classroom_courses.return_value = [self.course, other]
        self.client.course_subs.side_effect = lambda cid: [self.sub] if cid == 1 else [other_sub]
        self.client.subtitle.return_value = [dict(BeginSec=0, EndSec=1, Text="内容")]
        for root in (self.root, self.root.parent / "other-output"):
            self.args.out = str(root)
            self.sync()
            for cid, name in ((1, "课程"), (2, "另一课程")):
                directory = root / f"{name} ({cid})"
                data = self.metadata(directory)
                self.assertEqual(data["course_id"], cid)
                entry = data["lessons"][0]["files"]["transcripts"][0]
                self.assertTrue((directory / entry["path"]).is_file())
        def subs(cid):
            if cid == 1:
                raise zju.ZjuError("课程一失败")
            return [other_sub, dict(other_sub, sub_id=12)]
        self.client.course_subs.side_effect = subs
        self.sync(failure=True)
        self.assertEqual(len(self.metadata(Path(self.args.out) / "另一课程 (2)")["lessons"]), 2)

    def test_metadata_atomic_write_failure_keeps_old_bytes_and_cleans_temp(self):
        self.seed()
        self.sync()
        path = self.directory / "course.json"
        before = path.read_bytes()
        replace = zju.os.replace
        def fail(source, dest):
            if Path(dest) == path:
                raise OSError("模拟磁盘写入失败")
            return replace(source, dest)
        self.course["teacher"] = "新教师"
        with mock.patch.object(zju.os, "replace", side_effect=fail):
            self.sync(failure=True)
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(list(self.directory.glob(".part-*")))

    def test_invalid_existing_metadata_is_not_overwritten(self):
        self.seed()
        path = self.directory / "course.json"
        for content in ("{", "[]", json.dumps(dict(schema_version=1, source="zju-classroom", course_id=2))):
            path.write_text(content, encoding="utf-8")
            self.sync(failure=True)
            self.assertEqual(path.read_text(encoding="utf-8"), content)


class TranscriptPrecision(unittest.TestCase):
    def test_milliseconds_rounding_carries_and_formatting(self):
        cases = [(5025.12, "01:23:45.120"), (5032.875, "01:23:52.875"),
                 (61.12, "00:01:01.120"), (0, "00:00:00.000"),
                 (1.0004, "00:00:01.000"), (1.9995, "00:00:02.000"),
                 (59.9995, "00:01:00.000"), (3599.9995, "01:00:00.000")]
        for seconds, expected in cases:
            with self.subTest(seconds=seconds):
                items = [dict(BeginSec=seconds, EndSec=seconds, Text="内容")]
                self.assertEqual(zju.render_transcript(items, "md", "标题"),
                                 f"# 标题\n\n**[{expected} → {expected}]** 内容  \n")
                self.assertEqual(zju.render_transcript(items, "srt", "标题"),
                                 f"1\n{expected.replace('.', ',')} --> {expected.replace('.', ',')}\n内容\n\n")
        self.assertEqual(zju.render_transcript([dict(BeginSec=61.12, Text="内容")], "txt", "标题"),
                         "[00:01:01] 内容\n")

    def test_old_markdown_skipped_and_force_only_rewrites_after_successful_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sub = dict(course_id=1, course_name="课程", sub_id=11, sub_name="堂次")
            path = zju.classroom_material_path(root, sub, "转录", "md")
            path.parent.mkdir(parents=True)
            old = "# 课程 堂次\n\n**[00:00:01 → 00:00:02]** 旧内容  \n"
            path.write_text(old, encoding="utf-8")
            before = path.stat().st_mtime_ns
            client = mock.MagicMock()
            client.subtitle.return_value = [dict(BeginSec=1.12, EndSec=2.875, Text="新内容")]
            args = Namespace(format="md", force=False)
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                zju.transcript_one(client, args, root, sub)
                client.subtitle.assert_not_called()
                self.assertEqual((path.read_text(encoding="utf-8"), path.stat().st_mtime_ns), (old, before))
                args.force = True
                with mock.patch.object(zju.os, "replace", side_effect=OSError("写入失败")):
                    with self.assertRaises(OSError):
                        zju.transcript_one(client, args, root, sub)
                self.assertEqual(path.read_text(encoding="utf-8"), old)
                self.assertFalse(list(path.parent.glob(".part-*")))
                zju.transcript_one(client, args, root, sub)
            self.assertIn("**[00:00:01.120 → 00:00:02.875]** 新内容", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
