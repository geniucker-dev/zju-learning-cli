"""PPT 时间事件、分页重复和 PDF 映射的离线验证。"""
import importlib.util
import io
import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

from PIL import Image, ImageDraw

spec = importlib.util.spec_from_file_location("zju_ppt_tests", Path(__file__).resolve().parents[1] / "zju.py")
zju = importlib.util.module_from_spec(spec)
spec.loader.exec_module(zju)


class PPTEvents(unittest.TestCase):
    def test_api_keeps_same_image_at_different_times_and_raw_metadata(self):
        def row(sec, url="https://example.com/a.jpg"):
            return {"created_sec": sec, "content": json.dumps({"pptimgurl": url}), "old_id": 0}
        first, repeated, unknown = row(0), row(330), row(None)
        replies = [{"total": 3, "list": [first, repeated]},
                   {"total": 3, "list": [first, repeated, unknown]}]
        client = zju.Zju.__new__(zju.Zju)
        with mock.patch.object(client, "ensure"), mock.patch.object(client, "bearer", return_value={}), \
                mock.patch.object(client, "get"), mock.patch.object(client, "json", side_effect=replies):
            events = client.ppt_events(1, 2)
        self.assertEqual([e["created_sec"] for e in events], [0, 330, None])
        self.assertEqual(events[1]["source"], repeated)
        self.assertEqual(len({e["url"] for e in events}), 1)

    def test_invalid_times_remain_unknown_and_repeated_page_stops(self):
        rows = [{"created_sec": value, "content": json.dumps({"pptimgurl": f"https://example.com/{i}.jpg"})}
                for i, value in enumerate(("", "invalid", -1, "nan", True, "12.5"))]
        client = zju.Zju.__new__(zju.Zju)
        with mock.patch.object(client, "ensure"), mock.patch.object(client, "bearer", return_value={}), \
                mock.patch.object(client, "get") as get, \
                mock.patch.object(client, "json", return_value={"total": 20, "list": rows}):
            events = client.ppt_events(1, 2)
        self.assertEqual([e["created_sec"] for e in events], [None] * 5 + [12.5])
        self.assertEqual(get.call_count, 2)

    def images(self):
        def slide(lines):
            image = Image.new("RGB", (1280, 720), "white")
            draw = ImageDraw.Draw(image)
            for x, y, width in lines:
                draw.rectangle((x, y, x + width, y + 8), fill="black")
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=85)
            return buffer.getvalue()
        title = [(100, 60, 600)]
        full = title + [(120, 200, 800), (120, 280, 700), (120, 360, 900)]
        other = [(100, 60, 400), (150, 250, 500), (150, 450, 600), (300, 600, 300)]
        black = io.BytesIO()
        Image.new("RGB", (1280, 720), "black").save(black, format="JPEG")
        return [slide(title), slide(full), slide(other), slide(full), black.getvalue()]

    def test_dedup_sidecar_revisits_animation_blank_and_incremental_rebuild(self):
        data = self.images()
        times = [0, 20, 70, 330, None]
        events = [dict(url=f"https://example.com/{i}.jpg", created_sec=sec,
                       source={"created_sec": sec, "original_index": i}) for i, sec in enumerate(times)]
        client = mock.Mock()
        client.ppt_events.return_value = events
        client.get.side_effect = lambda url: Namespace(ok=True, content=data[int(url.rsplit("/", 1)[1].split(".")[0])])
        sub = dict(course_id=1, sub_id=2, course_name="课程", sub_name="第一堂")
        args = Namespace(force=False, dedup=True, keep_images=True)
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            pdf = zju.classroom_material_path(root, sub, "智云PPT", "pdf")
            pdf.parent.mkdir(parents=True)
            pdf.write_bytes(b"old PDF without mapping")
            zju.ppt_one(client, args, root, sub)
            self.assertTrue(pdf.read_bytes().startswith(b"%PDF-"))
            mapping_file = pdf.with_suffix(".json")
            mapping = json.loads(mapping_file.read_text())
            self.assertEqual([e["pdf_page"] for e in mapping["events"]], [1, 1, 2, 1, None])
            self.assertEqual([e["created_sec"] for e in mapping["events"]], times)
            self.assertEqual(mapping["pages"][0]["event_indices"], [0, 1, 3])
            self.assertEqual(mapping["pages"][0]["representative_event_index"], 1)
            self.assertEqual([e["relationship"] for e in mapping["events"]],
                             ["represented", "retained", "retained", "represented", "blank"])
            for event in mapping["events"]:
                self.assertEqual(len(event["image_sha256"]), 64)
                self.assertTrue((mapping_file.parent / event["image_file"]).is_file())
            self.assertEqual(mapping["audio_alignment"], "unverified")
            client.ppt_events.reset_mock()
            zju.ppt_one(client, args, root, sub)
            client.ppt_events.assert_not_called()
            # PDF 输出失败后不能留着旧映射假装它仍然有效。
            args.force = True
            with mock.patch.object(zju, "images_to_pdf", side_effect=zju.ZjuError("failed")):
                with self.assertRaises(zju.ZjuError):
                    zju.ppt_one(client, args, root, sub)
            self.assertFalse(mapping_file.exists())
            self.assertTrue(pdf.read_bytes().startswith(b"%PDF-"))

    def test_no_dedup_is_one_page_per_event_and_all_blank_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            paths = [Path(d) / f"{i}.jpg" for i in range(2)]
            for path in paths:
                Image.new("RGB", (64, 64), "black").save(path)
            kept, mapping = zju.dedup_slides(paths)
            self.assertEqual(kept, paths)
            self.assertEqual(mapping, [1, 2])
            self.assertEqual(zju.dedup_slides([]), ([], []))
            self.assertEqual(zju.dedup_slides(paths[:1]), (paths[:1], [1]))
