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
from course_fixtures import initialize, register
import numpy as np
from PIL import Image

spec = importlib.util.spec_from_file_location("zju_metadata_tests", Path(__file__).resolve().parents[1] / "skills/zju-learnpilot/scripts/zju.py")
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
        self.root = Path(self.temp.name) / "library"
        self.course = dict(course_id=1, title="课程", teacher="教师", term="2026秋", type="multi", task_count=2)
        self.sub = dict(course_id=1, course_name="课程", sub_id=11, sub_name="第一堂",
                        lecturer="授课教师", day=dt.date(2026, 10, 1), show="yes")
        self.client = mock.MagicMock()
        self.client.classroom_courses.return_value = [self.course]
        self.client.course_subs.return_value = [self.sub]
        self.client.subtitle.return_value = [dict(BeginSec=1.12, EndSec=2.875, Text="课堂内容")]
        self.client.ppt_events.return_value = [dict(url="https://example.com/slide", created_sec=1.23, source={})]
        self.client.get.return_value = SimpleNamespace(ok=True, content=self.image)
        self.client.video_catalogue.return_value = {11: ["https://example.com/1", "https://example.com/2"]}
        self.args = Namespace(library=str(self.root), course_dir=None, course=[1], all=False, jobs=2,
                              force=False, dry_run=False, recording=False, recording_audio=False,
                              format="md", dedup=False, keep_images=False, max_size=0)

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

    def seed_media(self, parts=2):
        initialize(zju, self.directory)
        for part in range(1, parts + 1):
            suffix = f" - {part:02d}" if parts > 1 else ""
            for kind, extension, content in (("音频", "m4a", self.audio), ("录播", "mp4", self.video)):
                path = zju.classroom_material_path(self.directory, self.sub, kind, extension, suffix)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
                register(zju, self.directory, path, self.sub, kind, extension, part=part, part_count=parts)

    def test_first_sync_incremental_and_metadata_without_repeat_downloads(self):
        self.sync()
        data = self.metadata()
        self.assertEqual((data["schema_version"], data["platform"], data["course_id"]), (2, "classroom", 1))
        self.assertEqual((data["teacher"], data["term"]), ("教师", "2026秋"))
        self.assertEqual(set(data["lessons"][0]["files"]), {"transcripts", "ppt_pdfs", "ppt_events", "audio", "recordings"})
        self.assertTrue((self.directory / ".zju/downloads.json").is_file())
        self.client.subtitle.reset_mock(); self.client.ppt_events.reset_mock()
        self.sync()
        self.client.subtitle.assert_not_called(); self.client.ppt_events.assert_not_called()
        self.assertFalse((self.root / ".zju_manifest.json").exists())

    def test_direct_directory_infers_id_and_keeps_user_files(self):
        direct = Path(self.temp.name) / "我的课程"
        direct.mkdir()
        user = direct / "老师讲义.pdf"; user.write_bytes(b"user material")
        self.args.library, self.args.course_dir = None, str(direct)
        self.sync()
        self.assertTrue((direct / "course.json").exists())
        self.assertFalse((direct / "课程 (1)").exists())
        self.args.course = []
        self.sync()
        self.assertEqual(user.read_bytes(), b"user material")
        self.assertEqual(self.metadata(direct)["course_id"], 1)

    def test_moved_and_renamed_course_and_lesson_reuse_registered_paths(self):
        self.seed_media()
        self.sync()
        before = self.metadata()
        moved = Path(self.temp.name) / "另一块磁盘" / "任意名称"
        moved.parent.mkdir(); self.directory.rename(moved)
        self.args.library, self.args.course_dir, self.args.course = None, str(moved), []
        self.args.recording = self.args.recording_audio = True
        self.course["title"] = "改名课程"
        self.sub.update(course_name="改名课程", sub_name="改名堂次")
        self.client.subtitle.reset_mock();self.client.ppt_events.reset_mock()
        with mock.patch.object(zju, "download_video", side_effect=AssertionError("不应下载")), \
                mock.patch.object(zju, "make_audio", side_effect=AssertionError("不应提取")):
            self.sync()
        self.assertEqual(self.metadata(moved)["name"], "改名课程")
        self.assertEqual(self.metadata(moved)["lessons"][0]["files"], before["lessons"][0]["files"])
        self.client.subtitle.assert_not_called(); self.client.ppt_events.assert_not_called()
        source, part = zju.recording_frame_source(None, Namespace(course=1, sub=11, part=2, offline=True), moved)
        self.assertTrue(source.is_file());self.assertEqual(part, 2)
        self.assertFalse(self.directory.exists())

    def test_course_rename_in_library_reuses_original_directory(self):
        self.sync();self.sub.update(course_name="新名字")
        self.sync()
        self.assertEqual(self.metadata()["name"], "新名字")
        self.assertEqual(len(list(self.root.glob('*/course.json'))), 1)

    def test_multiple_courses_and_duplicate_identity_conflict(self):
        self.args.course = [1, 2]
        self.client.classroom_courses.return_value = [self.course, dict(self.course, course_id=2)]
        self.client.course_subs.side_effect = lambda cid: [dict(self.sub, course_id=cid)]
        self.sync()
        self.assertTrue((self.root / "课程 (2)/course.json").is_file())
        import shutil
        shutil.copytree(self.directory, self.root / "副本")
        self.sync(failure=True)
        self.assertTrue((self.root / "副本/course.json").is_file())

    def test_user_file_collision_is_not_overwritten_even_with_force(self):
        initialize(zju, self.directory)
        path = zju.classroom_material_path(self.directory, self.sub, "转录", "md")
        path.parent.mkdir();path.write_text("user content", encoding="utf-8")
        self.args.force = True
        self.sync(failure=True)
        self.assertEqual(path.read_text(), "user content")
        self.assertEqual(self.metadata()["lessons"][0]["files"]["transcripts"], [])

    def test_dry_run_does_not_create_directories_or_change_existing_files(self):
        self.args.dry_run = True
        self.sync();self.assertFalse(self.root.exists())
        self.args.dry_run = False;self.sync()
        before = {p.relative_to(self.root):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.args.dry_run = True;self.args.force = True;self.sync()
        self.assertEqual(before, {p.relative_to(self.root):p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_partial_failure_preserves_known_lessons_and_materials(self):
        self.seed_media(parts=1);self.sync()
        before = self.metadata()
        self.client.course_subs.side_effect = zju.ZjuError("API failure")
        self.sync(failure=True)
        self.assertEqual(self.metadata()["lessons"], before["lessons"])
        self.assertEqual(self.metadata()["lessons_fetch_status"], "error")
        self.client.course_subs.side_effect = None
        self.client.course_subs.return_value = [self.sub, dict(self.sub, sub_id=12)]
        self.client.subtitle.side_effect = zju.ZjuError("subtitle failure")
        self.sync(failure=True)
        self.assertEqual(self.metadata()["lessons"][0]["files"], before["lessons"][0]["files"])
        self.assertEqual(self.metadata()["lessons"][1]["files"]["transcripts"], [])

    def test_single_material_commands_refresh_metadata(self):
        args = Namespace(**vars(self.args));args.course = 1;args.sub = [11];args.days = None
        args.action = "transcript"
        with mock.patch.object(zju, "Zju", return_value=self.client), mock.patch.object(zju, "load_config", return_value={}):
            zju.cmd_classroom_material(args)
            self.assertTrue(self.metadata()["lessons"][0]["files"]["transcripts"])
            args.action = "ppt";zju.cmd_classroom_material(args)
            self.assertTrue(self.metadata()["lessons"][0]["files"]["ppt_events"])

    def test_missing_corrupt_and_pending_files_are_not_reported_complete(self):
        self.seed_media(parts=2);self.sync()
        state = zju.DownloadState(self.directory)
        records = list(state.data.items())
        audio = [(k,r) for k,r in records if r.get('kind') == 'audio']
        (self.directory/audio[0][1]['path']).unlink()
        (self.directory/audio[1][1]['path']).write_bytes(b'broken')
        state.put('unfinished', dict(path='录播/incomplete.mp4', status='pending', kind='recordings', sub_id=11, part=3))
        self.sync()
        self.assertFalse(self.metadata()['lessons'][0]['files']['audio'])
        self.assertEqual(len(self.metadata()['lessons'][0]['files']['recordings']),2)

    def test_pending_video_state_moves_with_resume_files(self):
        self.args.recording=True
        self.client.video_catalogue.return_value={11:['https://example.com/video']}
        original=[]
        def interrupted(client,url,dest,limit,jobs,**kw):
            original.append(dest)
            dest.parent.mkdir(parents=True,exist_ok=True)
            dest.with_name('.'+dest.name+'.part').write_bytes(b'validated fragment fixture')
            raise zju.ZjuError('connection interrupted')
        with mock.patch.object(zju,'download_video',side_effect=interrupted): self.sync(failure=True)
        state=zju.DownloadState(self.directory)
        self.assertEqual(state.get('video:1:11:1')['status'],'pending')
        self.assertEqual(self.metadata()['lessons'][0]['files']['recordings'],[])
        moved=Path(self.temp.name)/'moved';self.directory.rename(moved)
        self.args.library=None;self.args.course_dir=str(moved);self.args.course=[]
        self.sub['sub_name']='renamed lesson'
        def resume(client,url,dest,limit,jobs,**kw):
            self.assertFalse(kw['restart'])
            self.assertEqual(dest.name,original[0].name)
            partial=dest.with_name('.'+dest.name+'.part')
            self.assertEqual(partial.read_bytes(),b'validated fragment fixture')
            dest.write_bytes(self.video);partial.unlink()
        with mock.patch.object(zju,'download_video',side_effect=resume):self.sync()
        self.assertEqual(zju.DownloadState(moved).get('video:1:11:1')['status'],'complete')
        self.assertEqual(len(self.metadata(moved)['lessons'][0]['files']['recordings']),1)

    def test_metadata_write_failure_retains_previous_file(self):
        self.sync();before=(self.directory/'course.json').read_bytes()
        replace = zju.os.replace
        def fail(source, target):
            if Path(target).resolve() == (self.directory/'course.json').resolve():raise OSError('failure')
            return replace(source,target)
        with mock.patch.object(zju.os,'replace',side_effect=fail): self.sync(failure=True)
        self.assertEqual((self.directory/'course.json').read_bytes(),before)
        self.assertFalse(list(self.directory.glob('.part-*')))



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
            register(zju, root, path, sub, "转录", "md")
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
