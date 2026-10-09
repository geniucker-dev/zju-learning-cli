"""上游功能与本地课堂命令、目录及状态字段的合并验证。"""
import datetime as dt
import importlib.util
import io
import unittest
import zipfile
from argparse import Namespace
from pathlib import Path
from unittest import mock

spec = importlib.util.spec_from_file_location("zju_upstream_tests", Path(__file__).resolve().parents[1] / "zju.py")
zju = importlib.util.module_from_spec(spec)
spec.loader.exec_module(zju)


class UpstreamSync(unittest.TestCase):
    def test_tracking_cli_and_existing_sync_options(self):
        import sys
        with mock.patch.object(zju, "load_config", return_value={}), \
                mock.patch.object(zju, "cmd_classroom") as command:
            for action in ("add", "rm"):
                with mock.patch.object(sys, "argv", ["zju.py", "classroom", action, "1", "2"]):
                    zju.main()
                args = command.call_args.args[0]
                self.assertEqual((args.action, args.arg, args.more), (action, "1", ["2"]))
            with mock.patch.object(sys, "argv", ["zju.py", "classroom", "tracked"]):
                zju.main()
            self.assertEqual(command.call_args.args[0].action, "tracked")
        with mock.patch.object(zju, "load_config", return_value={}), \
                mock.patch.object(zju, "cmd_classroom_sync") as command, \
                mock.patch.object(sys, "argv", ["zju.py", "classroom", "sync", "1", "--recording",
                                                 "--recording-audio", "--dedup", "-j", "3"]):
            zju.main()
        args = command.call_args.args[0]
        self.assertTrue(args.recording and args.recording_audio and args.dedup)
        self.assertEqual(args.jobs, 3)

    def test_tracked_range_deduplicates_and_preserves_delisted_state(self):
        end = dt.date(2026, 10, 9)
        first = dict(course_id=1, sub_id=10, course_name="课程", sub_name="第一堂", lecturer="教师")
        tracked = dict(course_id=2, sub_id=20, course_name="课程", sub_name="第二堂", lecturer="教师", day=end, show="no")
        client = mock.Mock()
        client.day_subs.side_effect = lambda day: [first] if day == end else []
        client.course_subs.return_value = [dict(first, day=end), tracked,
                                           dict(tracked, sub_id=21, day=end - dt.timedelta(days=4))]
        with mock.patch.object(zju, "tracked_courses", return_value={"2": "课程 教师"}):
            subs = zju.range_subs(client, end, 2)
        self.assertEqual([s["sub_id"] for _, s in subs], [10, 20])
        self.assertEqual(subs[1][1]["show"], "no")
        self.assertEqual(zju.classroom_material_path(Path("output"), subs[1][1], "音频", "m4a"),
                         Path("output/课程 教师 (2)/音频/第二堂 (20).m4a"))

    def test_course_detail_keeps_both_day_and_visibility(self):
        client = zju.Zju.__new__(zju.Zju)
        timestamp = int(dt.datetime(2026, 10, 9, 0, 15, tzinfo=zju.CST).timestamp())
        detail = {"data": {"title": "课程", "sub_list": {"year": {"month": {"week": [
            dict(id=2, sub_title="第一堂", show="no", class_begin=str(timestamp))]}}}}}
        with mock.patch.object(client, "infosimple", return_value={"account": "test"}), \
                mock.patch.object(client, "get"), mock.patch.object(client, "bearer", return_value={}), \
                mock.patch.object(client, "json", return_value=detail):
            sub = client.course_subs(1)[0]
        self.assertEqual(sub["day"], dt.date(2026, 10, 9))
        self.assertEqual(sub["show"], "no")

    def test_attachment_text_and_html_keep_links_images_and_table_cells(self):
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            archive.writestr("word/document.xml", '<w:p><w:t>公式</w:t><m:t>x</m:t><w:drawing></w:drawing></w:p>')
        self.assertEqual(zju.doc_to_text(data.getvalue()), "公式x [图]")
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            for i in (10, 2):
                archive.writestr(f"ppt/slides/slide{i}.xml", f"<a:p><a:t>第{i}页</a:t></a:p>")
        text = zju.doc_to_text(data.getvalue())
        self.assertLess(text.index("第2页"), text.index("第10页"))
        text = zju.html_to_text('<a href="https://example.com">参考</a><img src="/image.png">'
                               '<ul><li>项目</li></ul><table><tr><td>A</td><td>B</td></tr></table>')
        self.assertIn("参考 (https://example.com)", text)
        self.assertIn("[图 ", text)
        self.assertIn("- 项目", text)
        self.assertIn("A\tB", text)
