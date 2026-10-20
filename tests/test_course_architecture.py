"""课程对象、配置、状态并发与 CLI 端到端边界；不访问真实账户或课程。"""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from unittest import mock

from course_fixtures import initialize

SCRIPT = Path(__file__).resolve().parents[1] / 'skills/zju-learnpilot/scripts/zju.py'
spec = importlib.util.spec_from_file_location('architecture', SCRIPT)
zju = importlib.util.module_from_spec(spec)
spec.loader.exec_module(zju)


class Architecture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.args = Namespace(library=str(self.root/'library'), course_dir=None)

    def invoke(self, *args, config=None):
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with mock.patch.object(sys, 'argv', ['zju.py', *map(str,args)]), \
                mock.patch.object(zju, 'CONFIG_FILE', config or self.root/'missing.json'), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:zju.main()
            except SystemExit as error:code=error.code
        return code, out.getvalue(), err.getvalue()

    def test_config_lazy_help_explicit_directory_and_defaults(self):
        cfg=self.root/'config.json';cfg.write_text('{broken')
        course=self.root/'single';initialize(zju,course)
        for args in [('--help',),('classroom','context','--help'),
                     ('classroom','subs','--course-dir',str(course),'--json')]:
            code,out,err=self.invoke(*args,config=cfg)
            self.assertEqual(code,0,err)
        code,_,err=self.invoke('classroom','courses','--local',config=cfg)
        self.assertEqual(code,1);self.assertIn('格式错误',err)
        with mock.patch.object(zju,'load_config',return_value={'libraries':{'classroom':str(self.root/'custom')}}):
            self.assertEqual(zju.library_path(Namespace(library=None),'classroom'),self.root/'custom')
            self.assertEqual(zju.library_path(self.args,'classroom'),self.root/'library')
        with mock.patch.object(zju,'load_config',return_value={}):
            self.assertEqual(zju.library_path(Namespace(library=None),'lms'),(Path.home()/'ZJU-Courses/lms').resolve())

    def test_parameter_contracts_no_ignored_options_or_old_commands(self):
        for args in [('courses',),('--out',str(self.root),'classroom','sync','1'),
                     ('classroom','ppt','--sub','11'),
                     ('classroom','recording','--course','1','--days','2'),
                     ('classroom','transcript','--course-dir',str(self.root),'--days','1'),
                     ('classroom','context','--library',str(self.root),'--course-dir',str(self.root)),
                     ('classroom','courses','--library',str(self.root)),
                     ('classroom','subs','--library',str(self.root),'--course','1'),
                     ('classroom','sync','-j','0')]:
            code,_,_=self.invoke(*args)
            self.assertNotEqual(code,0,args)
        with mock.patch.object(zju,'Zju',side_effect=AssertionError('must validate before API')):
            for args in [('classroom','sync'),('lms','sync'),('classroom','sync','1','--all'),
                         ('classroom','sync','1','2','--course-dir',str(self.root)),
                         ('classroom','sync','--course-dir',str(self.root))]:
                self.assertNotEqual(self.invoke(*args)[0],0,args)
        with mock.patch.object(zju,'cmd_classroom_material') as command:
            self.assertEqual(self.invoke('classroom','transcript','--course','1')[0],0)
            self.assertEqual(command.call_args.args[0].format,'md')

    def test_local_discovery_identity_collision_and_cwd(self):
        a=self.root/'library'/'arbitrary';initialize(zju,a,1)
        b=a.parent/'same display';initialize(zju,b,2)
        c=a.parent/'other platform';initialize(zju,c,1,'lms')
        with mock.patch.object(zju,'Zju',side_effect=AssertionError('offline')):
            code,out,err=self.invoke('classroom','courses','--local','--library',a.parent,'--json')
            self.assertEqual(code,0,err);self.assertEqual([d['course_id'] for d in json.loads(out)],[1,2])
            self.assertEqual(self.invoke('classroom','subs','--course-dir',a,'--course','2')[0],1)
            old=Path.cwd()
            try:
                os.chdir(a)
                self.assertEqual(self.invoke('classroom','subs','--json')[0],0)
                with self.assertRaises(zju.ZjuError):
                    zju.resolve_course(Namespace(course_dir=None,library=str(self.root/'empty')),'classroom',None,writing=True)
            finally:os.chdir(old)
        duplicate=a.parent/'duplicate';initialize(zju,duplicate,1)
        self.assertEqual(self.invoke('classroom','courses','--local','--library',a.parent)[0],1)
        self.assertEqual(self.invoke('classroom','subs','--course-dir',a,'--json')[0],0)
        self.assertEqual(self.invoke('classroom','subs','--course-dir',c,'--json')[0],1)

    def test_download_state_process_merging_failure_and_corruption(self):
        directory=self.root/'course';initialize(zju,directory)
        code='''import sys
from pathlib import Path
import zju
root=Path(sys.argv[1]); prefix=sys.argv[2]
state=zju.DownloadState(root)
for i in range(12):
 state.put(prefix+str(i),dict(path=prefix+str(i)+'.md',kind='transcripts',sub_id=1,format='md',status='pending'))
'''
        processes=[subprocess.Popen([sys.executable,'-B','-c',code,str(directory),str(i)+'-'],cwd=SCRIPT.parent,
                    stdout=subprocess.PIPE,stderr=subprocess.PIPE) for i in range(4)]
        for process in processes:
            out,err=process.communicate(timeout=30)
            self.assertEqual(process.returncode,0,err.decode())
        state=zju.DownloadState(directory);self.assertEqual(len(state.data),48)
        previous=state.path.read_bytes()
        with mock.patch.object(zju.os,'replace',side_effect=OSError('write failure')):
            with self.assertRaises(OSError):state.put('new',dict(path='new.md',status='pending',kind='transcripts',sub_id=1,format='md'))
        self.assertEqual(state.path.read_bytes(),previous)
        self.assertFalse(list(state.path.parent.glob('.part-*')))
        state.path.write_text('{broken')
        with self.assertRaises(zju.ZjuError):zju.DownloadState(directory)
        self.assertEqual(state.path.read_text(),'{broken')

    def test_user_symlink_and_reserved_state_protection(self):
        directory=self.root/'course';initialize(zju,directory)
        outside=self.root/'outside';outside.mkdir()
        try:(directory/'.zju').symlink_to(outside,target_is_directory=True)
        except OSError:self.skipTest('symlinks unavailable')
        with self.assertRaises(ValueError):zju.DownloadState(directory)
        self.assertFalse(list(outside.iterdir()))

    def test_lms_same_name_isolation_move_preview_and_user_collision(self):
        client=mock.Mock()
        client.courses.return_value=[dict(id=i,name='同名课程') for i in (1,2)]
        client.uploads.side_effect=lambda cid:[(dict(title='材料'),dict(id=cid*10,name='讲义.pptx',size=20))]
        def response(*args):
            r=mock.Mock();r.headers={};r.iter_content.return_value=[b'%PDF-fixture'];return r,'preview'
        client.upload_response.side_effect=response
        args=Namespace(**vars(self.args),course=[1],all=False,dry_run=False,jobs=2,videos=False,max_size=200)
        with mock.patch.object(zju,'Zju',return_value=client):
            zju.cmd_sync(args);args.course=[2];zju.cmd_sync(args)
            folders=list(Path(args.library).glob('*/course.json'));self.assertEqual(len(folders),2)
            first=next(p.parent for p in folders if json.loads(p.read_text())['course_id']==1)
            second=next(p.parent for p in folders if json.loads(p.read_text())['course_id']==2)
            self.assertNotEqual(first,second)
            self.assertTrue((first/'附件/讲义 (10).pptx.pdf').is_file())
            moved=self.root/'moved';first.rename(moved)
            client.upload_response.reset_mock();args.course=[];args.library=None;args.course_dir=str(moved)
            zju.cmd_sync(args);client.upload_response.assert_not_called()
            user=moved/'附件/用户材料 (30).pdf';user.write_bytes(b'user')
            client.uploads.return_value=[]
            client.uploads.side_effect=lambda cid:[({},dict(id=30,name='用户材料.pdf'))]
            with self.assertRaises(SystemExit):zju.cmd_sync(args)
            self.assertEqual(user.read_bytes(),b'user')

    def test_courses_json_and_text_have_identical_filters(self):
        client=mock.Mock();client.courses.return_value=[dict(id=1,name='old'),dict(id=2,name='current')]
        client.semesters.return_value={}
        with mock.patch.object(zju,'Zju',return_value=client),mock.patch.object(zju,'current_year',side_effect=lambda rows:rows[1:]):
            for all_years in (False,True):
                expected=[1,2] if all_years else [2]
                flags=['--all'] if all_years else []
                code,out,err=self.invoke('lms','courses',*flags,'--json')
                self.assertEqual(code,0,err);self.assertEqual([d['id'] for d in json.loads(out)],expected)
                code,out,err=self.invoke('lms','courses',*flags)
                self.assertEqual(code,0,err);self.assertEqual([int(line.split('\t')[0]) for line in out.splitlines()],expected)

    def test_pdf_preview_destination_and_unowned_resume_are_protected(self):
        course=self.root/'course';initialize(zju,course,1,'lms')
        client=mock.Mock();client.courses.return_value=[dict(id=1,name='course')]
        client.uploads.return_value=[({},dict(id=1,name='lecture.pptx'))]
        target=course/'附件/lecture (1).pptx.pdf';target.parent.mkdir();target.write_bytes(b'user PDF')
        response=mock.Mock();response.headers={};response.iter_content.return_value=[b'%PDF-preview']
        client.upload_response.return_value=(response,'preview')
        args=Namespace(library=None,course_dir=str(course),course=[],all=False,jobs=1,dry_run=False,max_size=0,videos=False)
        with mock.patch.object(zju,'Zju',return_value=client),self.assertRaises(SystemExit):zju.cmd_sync(args)
        self.assertEqual(target.read_bytes(),b'user PDF')
        media=self.root/'media';initialize(zju,media)
        partial=media/'.lesson.mp4.part';partial.write_bytes(b'user partial')
        state=zju.DownloadState(media)
        with self.assertRaises(zju.ZjuError):
            state.claim('video:1:1:1',media/'lesson.mp4',kind='recordings',sub_id=1,part=1)
        self.assertEqual(partial.read_bytes(),b'user partial')
        self.assertFalse(state.path.exists())

    def test_corrupt_course_lists_fail_before_writing(self):
        course=self.root/'course';initialize(zju,course)
        for items in (None, {}, ['bad'], [dict(sub_id=1),dict(sub_id=1)]):
            data=dict(schema_version=2,platform='classroom',course_id=1,lessons=items)
            path=course/'course.json';path.write_text(json.dumps(data))
            before=path.read_bytes()
            self.assertEqual(self.invoke('classroom','subs','--course-dir',course,'--json')[0],1)
            self.assertEqual(path.read_bytes(),before)
        path.write_text(json.dumps(dict(schema_version=2,platform=[],course_id=1,lessons=[])))
        self.assertEqual(self.invoke('classroom','subs','--course-dir',course,'--json')[0],1)

    def test_login_reset_preserves_settings(self):
        cfg=self.root/'config.json';cookie=self.root/'cookies.json'
        cfg.write_text(json.dumps(dict(username='u',libraries={'classroom':'/custom'},tracked_courses={'1':'x'})))
        cookie.write_text('[]')
        with mock.patch.object(zju,'CONFIG_FILE',cfg),mock.patch.object(zju,'COOKIE_FILE',cookie), \
             mock.patch.object(zju,'STATE_DIR',self.root),mock.patch.object(zju,'keychain_get',return_value='password'), \
             mock.patch.object(zju,'keychain_set_interactive'),mock.patch.object(zju,'get_credentials',return_value=('u','password')), \
             mock.patch.object(zju,'Zju'):
            zju.cmd_login(Namespace(username='u',reset=True))
        self.assertEqual(json.loads(cfg.read_text())['libraries'],{'classroom':'/custom'})
        self.assertEqual(json.loads(cfg.read_text())['tracked_courses'],{'1':'x'})
        self.assertFalse(cookie.exists())


if __name__=='__main__':unittest.main()
