"""Self-contained Skill, uv entry points and local learning workflows; no school account."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from fractions import Fraction

import av
import numpy as np
from PIL import Image, ImageDraw

REPO = Path(__file__).resolve().parents[1]
SKILL = REPO / 'skills/zju-learnpilot'
CORE = SKILL / 'scripts/zju.py'
spec = importlib.util.spec_from_file_location('distribution_core', CORE)
zju = importlib.util.module_from_spec(spec)
spec.loader.exec_module(zju)


class SkillDistribution(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='learnpilot-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.uv = shutil.which('uv')
        self.assertIsNotNone(self.uv, 'The CLI and its tests require uv')
        self.env = dict(os.environ, ZJU_STATE_DIR=str(self.root / 'unused state'),
                        PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1', UV_OFFLINE='1')

    def run_cli(self, script, *args):
        result = subprocess.run([self.uv, 'run', '--script', str(script), *map(str, args)],
                                cwd=self.root, env=self.env, capture_output=True,
                                text=True, encoding='utf-8', timeout=90)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_repository_and_skill_entrypoints(self):
        # A dependency-free launcher must forward all arguments, including parse failures.
        for args in [('--help',), ('classroom', 'sync', '--help'), ('lms', 'submit', '--help')]:
            self.assertEqual(self.run_cli(REPO / 'zju.py', *args), self.run_cli(CORE, *args))
        for script in [REPO / 'zju.py', CORE]:
            result = subprocess.run([self.uv, 'run', '--script', str(script), 'bad-command'],
                                    cwd=self.root, env=self.env, capture_output=True, timeout=90)
            self.assertEqual(result.returncode, 2)
        if os.name != 'nt':
            for script in [REPO / 'zju.py', CORE]:
                result = subprocess.run([str(script), '--help'], cwd=self.root, env=self.env,
                                        capture_output=True, text=True, encoding='utf-8', timeout=90)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('ZJU LearnPilot', result.stdout)

    def test_missing_uv_and_incomplete_repository_diagnostics(self):
        launcher = self.root / 'zju.py'
        shutil.copy2(REPO / 'zju.py', launcher)
        env = dict(self.env, PATH='')
        result = subprocess.run([sys.executable, str(launcher), '--help'], cwd=self.root,
                                env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 127)
        self.assertIn('requires uv', result.stderr)
        if os.name != 'nt':
            for script in [launcher, CORE]:
                result = subprocess.run([str(script), '--help'], env=env, capture_output=True,
                                        text=True, timeout=30)
                self.assertEqual(result.returncode, 127)
                self.assertIn('requires uv', result.stderr)
        result = subprocess.run([self.uv, 'run', '--script', str(launcher), '--help'],
                                cwd=self.root, env=self.env, capture_output=True, text=True, timeout=90)
        self.assertEqual(result.returncode, 1)
        self.assertIn('missing skills/', result.stderr)
        self.assertNotIn('Traceback', result.stderr)

    def fixture(self):
        library = self.root / '课程 library'
        course = library / '用户自行命名'
        course.mkdir(parents=True)
        transcript = course / '转录/第一堂.md'
        transcript.parent.mkdir()
        transcript.write_text('# 控制理论\n\n**[00:00:00.250 → 00:00:01.000]** '
                              '能控性：C=[B AB]，rank(C)=n 时状态能控。\n', encoding='utf-8')
        pdf = course / '智云PPT/第一堂.pdf'
        pdf.parent.mkdir()
        slide = self.root / 'slide.png'
        page = Image.new('RGB', (640, 320), 'white')
        ImageDraw.Draw(page).multiline_text((40, 40), 'Controllability\nC = [B AB]\nrank(C) = n', fill='black', spacing=16)
        page.save(slide)
        zju.images_to_pdf([slide], pdf)
        events = dict(schema_version=1, course_id=456, sub_id=789, pdf_file=pdf.name,
                      events=[dict(event_index=0, created_sec=.3, pdf_page=1,
                                   representative_event_index=0, relationship='retained')],
                      pages=[dict(pdf_page=1, representative_event_index=0, event_indices=[0])])
        pdf.with_suffix('.json').write_text(json.dumps(events), encoding='utf-8')
        video = course / '录播/第一堂.mp4'
        video.parent.mkdir()
        with av.open(str(video), 'w') as output:
            stream = output.add_stream('mpeg4', rate=10)
            stream.width = stream.height = 32
            stream.pix_fmt = 'yuv420p'
            for i in range(20):
                frame = av.VideoFrame.from_ndarray(np.full((32, 32, 3), i * 10, np.uint8), format='rgb24')
                frame.pts, frame.time_base = i, Fraction(1, 10)
                for packet in stream.encode(frame):
                    output.mux(packet)
            for packet in stream.encode():
                output.mux(packet)
        files = dict(transcripts=[dict(path='转录/第一堂.md', format='md')],
                     ppt_pdfs=[dict(path='智云PPT/第一堂.pdf')], ppt_events=[dict(path='智云PPT/第一堂.json')],
                     audio=[], recordings=[dict(path='录播/第一堂.mp4', part=1)])
        meta = dict(schema_version=2, platform='classroom', course_id=456, name='控制理论',
                    lessons=[dict(sub_id=789, name='第一堂', files=files)])
        (course / 'course.json').write_text(json.dumps(meta), encoding='utf-8')
        zju.DownloadState(course).put('video:456:789:1', dict(path='录播/第一堂.mp4', status='complete',
            kind='recordings', sub_id=789, part=1, part_count=1, size=video.stat().st_size))
        return library, course, transcript, video

    def test_installed_skill_three_learning_tasks_without_repository(self):
        library, course, transcript, video = self.fixture()
        def snapshot():
            return {p.relative_to(course).as_posix(): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
                    for p in course.rglob('*') if p.is_file()}
        before = snapshot()
        with av.open(str(video)) as source:
            expected = list(source.decode(video=0))[8].to_image()
        # Both a project install and an unrelated user/Agent install; only the Skill is copied.
        for location in ['project/.agents/skills', '用户 home/other-agent/skills']:
            installed = self.root / location / 'zju-learnpilot'
            shutil.copytree(SKILL, installed, ignore=shutil.ignore_patterns('__pycache__'))
            self.assertFalse((installed.parent / 'zju.py').exists())
            for relative in ['SKILL.md', 'LICENSE', 'references/learning.md', 'references/operations.md']:
                self.assertTrue((installed / relative).is_file())
            cli = installed / 'scripts/zju.py'
            # Course-material query: identity comes from metadata, not directory names.
            discovered = json.loads(self.run_cli(cli, 'classroom', 'courses', '--local', '--library', library, '--json'))
            self.assertIn('456', json.dumps(discovered))
            lessons = json.loads(self.run_cli(cli, 'classroom', 'subs', '--course-dir', course, '--json'))
            self.assertIn('789', json.dumps(lessons))
            # Topic task: Agent searches the referenced transcript, then requests local evidence.
            metadata = json.loads((course / 'course.json').read_text(encoding='utf-8'))
            reference = metadata['lessons'][0]['files']['transcripts'][0]['path']
            self.assertEqual(course / reference, transcript)
            self.assertIn('能控性', (course / reference).read_text(encoding='utf-8'))
            context = json.loads(self.run_cli(cli, 'classroom', 'context', '--course-dir', course,
                                            '--sub', '789', '--at', '00:00:00.800', '--window', '1', '--json'))
            self.assertEqual(context['transcript']['segments'][0]['begin_sec'], .25)
            self.assertIn('rank(C)=n', context['transcript']['segments'][0]['text'])
            self.assertEqual(context['ppt']['events'][0]['pdf_page'], 1)
            # Time task: verify actual picture/PTS and zero network, not just an existing file.
            result = json.loads(self.run_cli(cli, 'classroom', 'recording-frame', '--course-dir', course,
                '--sub', '789', '--at', '0.8', '--offline', '--output', installed.parent / 'frame.png', '--json'))
            self.assertEqual(result['network'], dict(requests=0, bytes_received=0))
            frame = result['frames'][0]
            self.assertAlmostEqual(frame['actual_time_sec'], .8)
            with Image.open(frame['path']) as actual:
                self.assertEqual(actual.tobytes(), expected.tobytes())
        self.assertEqual(before, snapshot())
        self.assertFalse(Path(self.env['ZJU_STATE_DIR']).exists())
