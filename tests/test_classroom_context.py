"""本地课堂上下文：真实 wheel 素材、只读检索、身份与路径安全。"""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from fractions import Fraction
from pathlib import Path
from unittest import mock

import av
import numpy as np
from PIL import Image

SCRIPT = Path(__file__).resolve().parents[1] / "skills/zju-learnpilot/scripts/zju.py"
spec = importlib.util.spec_from_file_location("zju_context_tests", SCRIPT)
zju = importlib.util.module_from_spec(spec)
spec.loader.exec_module(zju)


class ClassroomContext(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = tempfile.TemporaryDirectory()
        root = Path(cls.fixture.name)
        video = root / "sample.mp4"
        with av.open(str(video), "w") as output:
            stream = output.add_stream("mpeg4", rate=10)
            stream.width = stream.height = 32
            stream.pix_fmt = "yuv420p"
            audio = output.add_stream("aac", rate=32000)
            audio.layout = "mono"
            for i in range(10):
                frame = av.VideoFrame.from_ndarray(np.zeros((32, 32, 3), np.uint8), format="rgb24")
                frame.pts, frame.time_base = i, Fraction(1, 10)
                for packet in stream.encode(frame):
                    output.mux(packet)
                frame = av.AudioFrame.from_ndarray(np.zeros((1, 3200), np.float32), format="fltp", layout="mono")
                frame.sample_rate, frame.pts, frame.time_base = 32000, i * 3200, Fraction(1, 32000)
                for packet in audio.encode(frame):
                    output.mux(packet)
            for encoder in (stream, audio):
                for packet in encoder.encode():
                    output.mux(packet)
        with contextlib.redirect_stderr(io.StringIO()):
            zju.make_audio(None, video, root / "sample.m4a")
        cls.video, cls.audio = video.read_bytes(), (root / "sample.m4a").read_bytes()
        image = root / "image.png"
        Image.new("RGB", (16, 16), "white").save(image)
        zju.images_to_pdf([image] * 3, root / "sample.pdf")
        cls.pdf = (root / "sample.pdf").read_bytes()

    @classmethod
    def tearDownClass(cls):
        cls.fixture.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = self.root / "历史目录，与显示名不同"
        self.directory.mkdir()
        self.files = dict(transcripts=[dict(path="资料/任意名字.md", format="md")],
                          ppt_pdfs=[dict(path="PPT/讲义.pdf")], ppt_events=[dict(path="PPT/讲义.json")],
                          audio=[dict(path="音频/讲课.m4a", part=1)], recordings=[dict(path="视频/讲课.mp4", part=1)])
        self.lesson = dict(sub_id=11, name="第一堂", date="2026-10-01", lecturer="教师", availability="listed", files=self.files)
        self.course = dict(schema_version=2, platform="classroom", course_id=1, name="课程新名称",
                           teacher="教师", term="2026秋", lessons=[self.lesson])
        self.save_metadata()
        self.put("资料/任意名字.md", "# 标题\n\n**[00:00:05 → 00:00:10]** 旧版秒级\n"
                 "**[00:00:10.120 → 00:00:12.875]** 中文 **加粗** [x=y] → <符号> & `代码`  \n"
                 "续行：a=b，不拆分\n**[00:00:20.000 → 00:00:22.000]** 窗口边界\n")
        self.put("PPT/讲义.pdf", self.pdf)
        self.mapping = dict(schema_version=1, course_id=1, sub_id=11, pdf_file="讲义.pdf", events=[
            dict(event_index=i, created_sec=t, pdf_page=page, representative_event_index=rep,
                 relationship=relation, source={"url": "https://example.com/?TOKEN_SECRET", "Cookie": "COOKIE_SECRET"})
            for i, t, page, rep, relation in [(0, 0, 1, 1, "represented"), (1, 10, 1, 1, "retained"),
                (2, 20, 2, 2, "retained"), (3, 330, 1, 1, "represented"), (4, None, None, None, "blank"),
                (5, 22, 3, 5, "retained"), (6, 21, 3, 5, "represented")]], pages=[
            dict(pdf_page=1, representative_event_index=1, event_indices=[0, 1, 3]),
            dict(pdf_page=2, representative_event_index=2, event_indices=[2]),
            dict(pdf_page=3, representative_event_index=5, event_indices=[5, 6])])
        self.save_mapping()
        self.put("音频/讲课.m4a", self.audio)
        self.put("视频/讲课.mp4", self.video)
        self.args = Namespace(library=str(self.root), course_dir=None, course=1, sub=11, at=20., window=2., json=True)

    def put(self, relative, content):
        path = self.directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
        return path

    def save_metadata(self):
        self.put("course.json", json.dumps(self.course, ensure_ascii=False))

    def save_mapping(self):
        self.put("PPT/讲义.json", json.dumps(self.mapping, ensure_ascii=False))

    def query(self, **kwargs):
        return zju.classroom_context(Namespace(**(vars(self.args) | kwargs)))

    def test_identity_millisecond_text_and_closed_window_edges(self):
        result = self.query(at=10.12, window=0.)
        self.assertEqual(result["course"]["name"], "课程新名称")
        self.assertEqual(result["lesson"]["sub_id"], 11)
        self.assertEqual(result["query"]["start_sec"], 10.12)
        row, = result["transcript"]["segments"]
        self.assertEqual((row["begin_sec"], row["end_sec"]), (10.12, 12.875))
        self.assertEqual(row["text"], "中文 **加粗** [x=y] → <符号> & `代码`\n续行：a=b，不拆分")
        self.assertEqual(row["file"], self.files["transcripts"][0]["path"])
        self.assertEqual(len(self.query(at=10., window=0.)["transcript"]["segments"]), 1)
        self.assertEqual(len(self.query(at=12.875, window=0.)["transcript"]["segments"]), 1)
        self.assertEqual(self.query(at=12.876, window=0.)["transcript"]["status"], "no_matches")
        self.assertEqual(self.query(at=0., window=20.)["query"]["start_sec"], 0.)
        self.put("资料/任意名字.md", "**[00:00:00.800 → 00:00:00.800]** 精确边界\n")
        self.assertEqual(len(self.query(at=.7, window=.1)["transcript"]["segments"]), 1)

    def test_zero_long_overlapping_reversed_and_unknown_end(self):
        self.put("资料/任意名字.md", "\n".join([
            "**[00:00:10 → 00:00:10]** 零时长", "**[00:00:00 → 01:00:00]** 异常长段",
            "**[00:00:09 → 00:00:11]** 重叠", "**[00:00:10 → 00:00:08]** 反向区间",
            "**[00:00:10]** 只有起点", "**[bad → 00:00:11]** 非法时间", "不能并入前一段"]))
        result = self.query(at=10., window=0.)
        rows = result["transcript"]["segments"]
        self.assertEqual(len(rows), 5)
        self.assertIn("zero_duration", rows[0]["flags"])
        self.assertIn("long_interval", rows[1]["flags"])
        self.assertIn("overlap", rows[2]["flags"])
        self.assertIn("reversed_interval", rows[3]["flags"])
        self.assertEqual(rows[3]["end_sec"], 8.)
        self.assertIsNone(rows[4]["end_sec"])
        self.assertNotIn("不能并入", rows[4]["text"])
        self.assertIn("invalid_transcript_time", {w["code"] for w in result["warnings"]})

    def test_ppt_dedup_animation_revisit_neighbors_and_unknown_times(self):
        result = self.query()["ppt"]
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["window_match_count"], 3)
        self.assertEqual([e["event_index"] for e in result["events"]], [1, 2, 6, 5, 3])
        self.assertEqual([e["selection"] for e in result["events"]], ["previous", "window", "window", "window", "next"])
        revisit = result["events"][-1]
        self.assertEqual((revisit["pdf_page"], revisit["representative_event_index"], revisit["relationship"]), (1, 1, "represented"))
        self.assertEqual(revisit["pdf_path"], "PPT/讲义.pdf")
        self.assertEqual(result["untimed_events"][0]["relationship"], "blank")
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertEqual(self.query(at=40., window=0.)["ppt"]["status"], "neighbors_only")
        # 同一相邻时刻的多个事件都保留，不强选唯一页面。
        self.mapping["events"][5]["created_sec"] = 10
        self.save_mapping()
        previous = [e["event_index"] for e in self.query()["ppt"]["events"] if e["selection"] == "previous"]
        self.assertEqual(previous, [1, 5])

    def test_inconsistent_mapping_does_not_claim_a_valid_page(self):
        self.mapping["events"][2]["pdf_page"] = 99
        self.save_mapping()
        event = next(e for e in self.query()["ppt"]["events"] if e["event_index"] == 2)
        self.assertIsNone(event["pdf_page"])
        self.assertEqual(event["relationship"], "invalid_mapping")
        self.assertIn("invalid_ppt_mapping", event["flags"])

    def test_missing_corrupt_and_no_match_are_distinct(self):
        (self.directory / "音频/讲课.m4a").unlink()
        self.put("视频/讲课.mp4", b"not mp4")
        self.put("PPT/讲义.json", "{broken")
        result = self.query(at=100., window=0.)
        self.assertEqual(result["audio"]["parts"][0]["status"], "missing")
        self.assertEqual(result["video"]["parts"][0]["status"], "invalid")
        self.assertEqual(result["ppt"]["event_files"][0]["status"], "invalid")
        self.assertEqual(result["transcript"]["status"], "no_matches")
        self.assertEqual(result["recording_frame_hint"]["remote_availability"], "unchecked")

    def test_pdf_missing_keeps_event_identity_with_unavailable_page_reference(self):
        (self.directory / "PPT/讲义.pdf").unlink()
        result = self.query()["ppt"]
        self.assertEqual(result["status"], "partial")
        self.assertTrue(all(e["pdf_status"] == "missing" for e in result["events"]))

    def test_unreadable_unfinished_and_duplicate_media_are_explicit(self):
        original = zju.valid_course_material
        def inspect(path, *args):
            if path.suffix == ".m4a":
                raise PermissionError("private")
            return original(path, *args)
        with mock.patch.object(zju, "valid_course_material", side_effect=inspect):
            self.assertEqual(self.query()["audio"]["parts"][0]["status"], "unavailable")
        self.files["recordings"].append(dict(path="视频/incomplete.mp4.part", part=2))
        self.put("视频/incomplete.mp4.part", self.video)
        self.files["audio"].append(dict(path="音频/重复.m4a", part=1))
        self.put("音频/重复.m4a", self.audio)
        self.save_metadata()
        result = self.query()
        self.assertEqual(result["video"]["parts"][1]["status"], "invalid")
        self.assertIn("duplicate_media_part", {w["code"] for w in result["warnings"]})

    def test_only_pdf_or_untimed_events_and_unsupported_transcripts(self):
        self.files["ppt_events"] = []
        self.files["transcripts"] = [dict(path="资料/text.txt", format="txt")]
        self.put("资料/text.txt", "没有时间信息")
        self.save_metadata()
        result = self.query()
        self.assertEqual(result["ppt"]["status"], "missing_events")
        self.assertEqual(result["transcript"]["status"], "unsupported_format")
        self.assertEqual(result["transcript"]["files"][0]["status"], "ok")
        self.files["ppt_events"] = [dict(path="PPT/讲义.json")]
        self.mapping["events"] = [self.mapping["events"][4]]
        self.mapping["pages"] = []
        self.save_mapping()
        self.save_metadata()
        self.assertEqual(self.query()["ppt"]["status"], "untimed_only")

    def test_missing_everything_and_malformed_material_list(self):
        self.lesson["files"] = {}
        self.save_metadata()
        result = self.query()
        for section in ("transcript", "ppt", "audio", "video"):
            self.assertEqual(result[section]["status"], "missing")
        self.lesson["files"] = dict(audio="bad")
        self.save_metadata()
        self.assertEqual(self.query()["audio"]["parts"][0]["status"], "invalid")

    def test_multipart_never_inferrs_classroom_mapping_or_duration(self):
        self.files["audio"].append(dict(path="音频/第二段.m4a", part=2))
        self.files["recordings"].append(dict(path="视频/第二段.mp4", part=2))
        self.put("音频/第二段.m4a", self.audio)
        self.put("视频/第二段.mp4", self.video)
        self.save_metadata()
        result = self.query(at=5000.)
        for section in ("audio", "video"):
            self.assertEqual(result[section]["classroom_time_mapping"], "unknown")
            for ref in result[section]["parts"]:
                self.assertIsNone(ref["duration_sec"])
                self.assertIsNone(ref["time_range_sec"])
        hint = result["recording_frame_hint"]
        self.assertEqual(hint["known_local_parts"], [1, 2])
        self.assertTrue(hint["part_selection_required"])
        self.assertEqual(hint["candidate_window_sec"], [4998., 5002.])
        self.assertNotIn("part", hint)

    def test_historical_directories_require_explicit_choice_and_survive_moves(self):
        second = self.root / "更旧目录"
        shutil.copytree(self.directory, second)
        with self.assertRaisesRegex(zju.ZjuError, "多个本地目录"):
            self.query()
        result = self.query(library=None, course_dir=str(second))
        self.assertEqual(result["location"]["course_dir"], str(second.resolve()))
        self.assertEqual(result["transcript"]["segments"][0]["file"], "资料/任意名字.md")

    def test_corrupt_metadata_identity_and_duplicate_lessons(self):
        for update in (dict(platform="other"), dict(schema_version=999), dict(lessons={}),
                       dict(lessons=[self.lesson, self.lesson]), dict(lessons=[])):
            with self.subTest(update=update):
                original = dict(self.course)
                self.course.update(update)
                self.save_metadata()
                with self.assertRaises(zju.ZjuError):
                    self.query()
                self.course = original
        self.put("course.json", "{broken")
        with self.assertRaisesRegex(zju.ZjuError, "元数据不可用"):
            self.query()

    def test_other_course_is_not_selected_and_broken_sibling_is_reported(self):
        sibling = self.root / "其他课程"
        sibling.mkdir()
        (sibling / "course.json").write_text(json.dumps(dict(self.course, course_id=2)), encoding="utf-8")
        self.assertEqual(self.query()["course"]["course_id"], 1)
        (sibling / "course.json").write_text("broken", encoding="utf-8")
        with self.assertRaisesRegex(zju.ZjuError, "元数据不可用"):
            self.query()

    def test_paths_cannot_escape_even_with_windows_syntax_or_symlinks(self):
        bad = ["../outside.md", str(self.root / "outside.md"), "C:\\private\\data.md", "C:relative.md",
               "\\\\server\\share\\data.md", "https://example.com/?TOKEN_SECRET", "资料/../course.json", "a\x00b"]
        self.files["transcripts"] = [dict(path=p, format="md") for p in bad]
        self.save_metadata()
        result = self.query()
        self.assertTrue(all(r["status"] == "unsafe_path" and r["path"] is None for r in result["transcript"]["files"]))
        self.assertNotIn("TOKEN_SECRET", json.dumps(result))

    def test_symlink_cannot_escape_course_directory(self):
        outside = self.root / "outside.md"
        outside.write_text("PRIVATE", encoding="utf-8")
        try:
            (self.directory / "link.md").symlink_to(outside)
        except OSError:
            self.skipTest("当前平台不允许创建符号链接")
        self.files["transcripts"] = [dict(path="link.md", format="md")]
        self.save_metadata()
        self.assertEqual(self.query()["transcript"]["files"][0]["status"], "unsafe_path")

    def test_invalid_ppt_paths_and_nonfinite_times_cannot_leak(self):
        self.mapping["pdf_file"] = "../private.pdf"
        self.save_mapping()
        self.assertEqual(self.query()["ppt"]["event_files"][0]["status"], "invalid")
        self.mapping["pdf_file"] = "讲义.pdf"
        for sec in (True, -1, "nan", "bad"):
            self.mapping["events"][2]["created_sec"] = sec
            self.save_mapping()
            result = self.query()["ppt"]
            self.assertIn(2, [e["event_index"] for e in result["untimed_events"]])
        self.mapping["events"][2]["created_sec"] = float("nan")
        self.save_mapping()
        self.assertEqual(self.query()["ppt"]["event_files"][0]["status"], "invalid")

    def test_ppt_identity_duplicate_indices_and_absent_representatives(self):
        self.mapping["course_id"] = 2
        self.save_mapping()
        self.assertEqual(self.query()["ppt"]["event_files"][0]["status"], "invalid")
        self.mapping["course_id"] = 1
        self.mapping["events"][1]["event_index"] = 0
        self.save_mapping()
        self.assertEqual(self.query()["ppt"]["event_files"][0]["status"], "invalid")
        self.mapping["events"][1]["event_index"] = 1
        self.mapping["pages"][0]["representative_event_index"] = 999
        self.mapping["pages"][0]["event_indices"].append(999)
        self.save_mapping()
        self.assertEqual(self.query()["ppt"]["event_files"][0]["status"], "invalid")

    def test_cli_json_offline_read_only_without_decoder_or_account(self):
        def snapshot():
            return {p.relative_to(self.root).as_posix(): (p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
                    for p in self.root.rglob("*") if p.is_file()}
        before = snapshot()
        out = io.StringIO()
        argv = [str(SCRIPT), "classroom", "context", "--library", str(self.root), "--course", "1", "--sub", "11",
                "--at", "00:00:20.120", "--window", "0", "--json"]
        with mock.patch.object(sys, "argv", argv), mock.patch.object(zju, "load_config", return_value={}), \
                mock.patch.object(zju, "Zju", side_effect=AssertionError("不应认证")), \
                mock.patch.object(zju.requests.sessions.Session, "request", side_effect=AssertionError("不应联网")), \
                mock.patch.object(zju.httpx.Client, "request", side_effect=AssertionError("不应联网")), \
                mock.patch.object(av, "open", side_effect=AssertionError("不应初始化解码器")), contextlib.redirect_stdout(out):
            zju.main()
        result = json.loads(out.getvalue())
        self.assertEqual(result["query"]["at_sec"], 20.12)
        self.assertFalse(result["recording_frame_hint"]["executed"])
        self.assertEqual(before, snapshot())
        self.assertNotIn("SECRET", out.getvalue())
        human = io.StringIO()
        with contextlib.redirect_stdout(human):
            zju.cmd_classroom_context(Namespace(**(vars(self.args) | dict(json=False))))
        self.assertIn("PPT/讲义.pdf", human.getvalue())
        self.assertIn("时间轴未校准", human.getvalue())

    def test_standalone_script_and_cli_errors(self):
        standalone = self.root / "standalone"
        standalone.mkdir()
        shutil.copy2(SCRIPT, standalone / "zju.py")
        base = [shutil.which("uv"), "run", "--script", str(standalone / "zju.py"), "classroom", "context", "--library", str(self.root),
                "--course", "1", "--sub", "11", "--json"]
        env = dict(os.environ, ZJU_STATE_DIR=str(self.root / "no-account"), PYTHONUTF8="1")
        result = subprocess.run(base + ["--at", "20"], capture_output=True, text=True, encoding="utf-8", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["schema_version"], 1)
        for tail in (["--at", "nan"], ["--at", "1", "--window", "-1"], ["--at", "1", "--sub", "999"],
                     ["--at", "1e308", "--window", "1e308"], ["--at", "1", "--course", "0"]):
            with self.subTest(tail=tail):
                result = subprocess.run(base + tail, capture_output=True, text=True, encoding="utf-8", env=env)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
