import argparse
import importlib.util
import sqlite3
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from PIL import Image

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
def load(name, filename):
    spec=importlib.util.spec_from_file_location(name,SCRIPTS/filename)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module
importer=load('library_import','import-library.py')
compute=load('cloud_compute','cloud-compute.py')
legacy=load('legacy_source_tests','legacy-source.py')

class BaozimhImagesTests(unittest.TestCase):
    def test_amp_reader_and_noscript_are_one_page_each_without_adverts(self):
        content='''<img data-src="https://s1.bzcdn.net/advert.jpg">
        <amp-img class="comic-contain__item" src="/placeholder.gif" data-src="https://s1.bzcdn.net/001.jpg">
        <noscript><img src="https://s1.bzcdn.net/001.jpg"></noscript></amp-img>
        <amp-img class="comic-contain__item" src="//s1.bzcdn.net/002.jpg">
        <noscript><img src="//s1.bzcdn.net/002.jpg"></noscript></amp-img>'''
        self.assertEqual(legacy.baozimhcn_image_urls(content,'https://www.baozimh.com/comic/chapter/fixture/0_0.html'),
                         ['https://s1.bzcdn.net/001.jpg','https://s1.bzcdn.net/002.jpg'])

    def test_old_reader_and_nested_images_keep_order_and_skip_empty_tags(self):
        content='''<img class="comic-contain__item" data-original="/001.jpg">
        <div class="comic-contain__item"><img data-src="/002.jpg"></div>
        <img class="comic-contain__item">'''
        self.assertEqual(legacy.baozimhcn_image_urls(content,'https://www.baozimh.com/reader'),
                         ['https://www.baozimh.com/001.jpg','https://www.baozimh.com/002.jpg'])

    def test_compatibility_dispatch_uses_amp_parser_after_fetching_page(self):
        content='<amp-img class="comic-contain__item" src="//s1.bzcdn.net/001.jpg"></amp-img>'
        with patch.object(legacy,'namespace',return_value={'_fetch_page':lambda *args:content}):
            self.assertEqual(legacy.image_urls('baozimhcn',{'url':'https://www.baozimh.com/reader'}),
                             ['https://s1.bzcdn.net/001.jpg'])

class CompleteMigrationTests(unittest.TestCase):
    def test_readonly_import_preserves_epub_metadata_old_ids_and_anchors(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);database=root/'app.db';novels=root/'novels';novels.mkdir()
            with sqlite3.connect(database) as db:
                db.executescript("CREATE TABLE user(id,username,password_hash,role,can_download,can_view_adult); CREATE TABLE reading_progress(user_id,comic_name,last_chapter,last_page,anchor_paragraph,anchor_offset,client_progress_ms,last_read_at); INSERT INTO user VALUES(1,'admin','pbkdf2:sha256:260000$salt$"+'a'*64+"','admin',0,0); INSERT INTO reading_progress VALUES(1,'novel:旧文件名',2,3,8,15,10000,'2025-01-01T12:00:00');")
            with zipfile.ZipFile(novels/'旧文件名.epub','w') as epub:
                epub.writestr('META-INF/container.xml','<container><rootfile full-path="book.opf"/></container>')
                epub.writestr('book.opf','<package xmlns:dc="http://purl.org/dc/elements/1.1/"><metadata><dc:title>真实书名</dc:title><dc:creator>原作者</dc:creator><dc:identifier>fanqie-6838936284967209991</dc:identifier><dc:description>原简介</dc:description></metadata></package>')
            with sqlite3.connect(database) as db:
                db.execute("INSERT INTO user VALUES (2,'reader',?,'user',0,0)",('pbkdf2:sha256:260000$salt$'+'a'*64,))
            before=database.read_bytes()
            records,files=importer.plan(argparse.Namespace(database=database,novels=novels,comics=None,covers=None))
            novel=records['novels'][0]
            self.assertEqual((novel['id'],novel['title'],novel['author'],novel['source_id']),('旧文件名','真实书名','原作者','6838936284967209991'))
            progress=records['progress'][0]
            self.assertEqual((progress['media_id'],progress['chapter'],progress['page'],progress['anchor_paragraph'],progress['anchor_offset']),('旧文件名',2,3,8,15))
            self.assertEqual(database.read_bytes(),before)
            self.assertEqual(len(files),1)
            self.assertIn({'kind':'novel','user_id':'2','group_name':'默认分组'},records['permissions'])

    def test_onnx_timeout_retains_pillow_fallback_and_original_variants(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);source=root/'input.jpg';Image.new('RGB',(100,140),'red').save(source)
            with patch.object(compute.subprocess,'run',side_effect=subprocess.TimeoutExpired('onnx',180)):
                result=compute.enhance(source,root)
            self.assertEqual(result['engine'],'pillow')
            self.assertEqual(result['original'],[100,140])
            self.assertEqual(result['hero'],[600,840])
            for name in ['original','hero','mobile']:
                with Image.open(root/(name+'.jpg')) as image:
                    image.verify()

if __name__=='__main__':unittest.main()

class BoundedComputeTests(unittest.TestCase):
    def test_job_timeout_terminates_child_process(self):
        import time
        class Client:
            def request(self,*args,**kwargs):return {'data':{'kind':'cover','lease_token':'synthetic'}}
        with tempfile.TemporaryDirectory() as temporary,patch.object(compute,'process',side_effect=lambda *args:time.sleep(5)):
            with self.assertRaises(TimeoutError):compute.process_bounded(Client(),{'id':'test'},Path(temporary),0.05)
    def test_successful_child_returns_only_result(self):
        class Client:
            def request(self,*args,**kwargs):return {'data':{'kind':'cover','lease_token':'synthetic'}}
        with tempfile.TemporaryDirectory() as temporary,patch.object(compute,'process',return_value={'pages':2}):
            self.assertEqual(compute.process_bounded(Client(),{'id':'test'},Path(temporary),2),{'pages':2})

class CloudIndexTests(unittest.TestCase):
    def test_cloud_epub_index_preserves_navigation_paragraphs_and_lease_output_keys(self):
        import gzip
        import json
        import shutil
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);source=root/'fixture.epub';folder=root/'job';folder.mkdir()
            with zipfile.ZipFile(source,'w') as epub:
                epub.writestr('META-INF/container.xml','<container><rootfiles><rootfile full-path="book.opf"/></rootfiles></container>')
                epub.writestr('book.opf','<package><manifest><item id="a" href="one.xhtml" media-type="application/xhtml+xml"/><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/></manifest><spine><itemref idref="a"/></spine></package>')
                epub.writestr('nav.xhtml','<html><body><nav><a href="one.xhtml">第一章</a></nav></body></html>')
                epub.writestr('one.xhtml','<html><body><h1>第一章</h1><p>原文内容</p><script>bad()</script><p>原文内容</p></body></html>')
            uploads={};requests=[]
            class Client:
                def download(self,path,destination):shutil.copyfile(source,destination)
                def upload(self,path,file,mime):uploads[path]=file.read_bytes()
                def request(self,*args,**kwargs):requests.append(args);return {'ok':True}
            claimed={'kind':'index-epub','book_id':'local-index','output_prefix':'compute/job/current-lease'}
            result=compute.process(Client(),{'id':'job'},folder,claimed)
            self.assertEqual(result,{'chapters':1})
            index=json.loads(uploads['/api/admin/compute/jobs/job/output/index'])
            self.assertEqual(index['chapters'][0]['title'],'第一章')
            self.assertEqual(index['chapters'][0]['key'],'compute/job/current-lease/chapter/0')
            chapter=json.loads(gzip.decompress(uploads['/api/admin/compute/jobs/job/output/chapter/0']))
            self.assertEqual(chapter['paragraphs'],['第一章','原文内容'])
            self.assertNotIn('bad()',str(chapter))
            self.assertEqual(requests[-1][0],'/api/admin/compute/jobs/job/complete')

class FailureMetadataTests(unittest.TestCase):
    def test_child_http_failure_retains_status_without_exporting_url_or_body(self):
        import urllib.error
        class Client:
            def request(self,*args,**kwargs):return {'data':{'kind':'cover','lease_token':'synthetic'}}
        failure=urllib.error.HTTPError('https://private.invalid/?token=secret',503,'private body',{},None)
        with tempfile.TemporaryDirectory() as temporary,patch.object(compute,'process',side_effect=failure):
            with self.assertRaises(compute.CloudJobError) as result:
                compute.process_bounded(Client(),{'id':'test'},Path(temporary),2)
        self.assertEqual(result.exception.details,{'error_type':'HTTPError','status':503})
        self.assertNotIn('private',str(result.exception))

    def test_legacy_page_uploads_only_one_failed_image(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as temporary:
            folder=Path(temporary);page=folder/'page.png';Image.new('RGB',(2,3)).save(page)
            calls=[];uploads=[]
            class Client:
                def request(self,*args,**kwargs):calls.append(args);return {'ok':True}
                def upload(self,path,file,mime):uploads.append((path,file,mime))
            legacy=SimpleNamespace(download_page=lambda *args,**kwargs:page)
            spec=SimpleNamespace(loader=SimpleNamespace(exec_module=lambda module:None))
            with patch.object(compute.importlib.util,'spec_from_file_location',return_value=spec),patch.object(compute.importlib.util,'module_from_spec',return_value=legacy):
                result=compute.process(Client(),{'id':'test'},folder,{'kind':'legacy-page','provider':'hipmh','url':'https://fixture.invalid/page.webp','referer':'https://reader.hipmh.top/'})
            self.assertEqual(result,{'pages':1});self.assertEqual(len(uploads),1)
            self.assertEqual(uploads[0][0],'/api/admin/compute/jobs/test/output/page/0')
            self.assertEqual(calls[-1][0],'/api/admin/compute/jobs/test/complete')

class ScheduledContinuationTests(unittest.TestCase):
    def run_batch(self,jobs,*,fail_job=False,fail_ack=False,scheduled=True,pending=False):
        import os
        calls=[]
        class Client:
            def __init__(self,*args):pass
            def request(self,path,method='GET',data=None,**kwargs):
                calls.append((path,method,data))
                if path.endswith('/fail') and fail_ack:raise RuntimeError('synthetic acknowledgement failure')
                if path=='/api/admin/compute/jobs':return {'data':jobs if len([c for c in calls if c[0]==path])==1 else [],'maintenance_pending':pending}
                return {'ok':True}
        argv=['cloud-compute.py','--url','https://fixture.invalid','--limit','5']+(['--scheduled'] if scheduled else [])
        with tempfile.TemporaryDirectory() as folder:
            output=Path(folder)/'github-output'
            with patch.object(compute,'Client',Client),patch.object(compute,'process_bounded',side_effect=TimeoutError('synthetic timeout') if fail_job else None,return_value={'pages':2}),patch.object(compute.sys,'argv',argv),patch.dict(os.environ,{'MANGADOCK_ADMIN_PASSWORD':'synthetic','MANGADOCK_COMPUTE_SEQUENCE':'7','MANGADOCK_COMPUTE_TOKEN':'synthetic','GITHUB_OUTPUT':str(output)}):
                compute.main()
            return calls,output.read_text() if output.exists() else ''

    def test_acknowledged_failed_job_keeps_other_work_and_bounded_retry_eligible(self):
        calls,output=self.run_batch([{'id':'one','kind':'legacy-chapter'},{'id':'two','kind':'index-epub'}],fail_job=True,pending=True)
        self.assertEqual(len([c for c in calls if c[0].endswith('/fail')]),2)
        self.assertIn('continue_work=true',output)
        self.assertEqual([c[2] for c in calls if c[0].endswith('/finished')],[{'sequence':7,'pending':True}])

    def test_failed_failure_acknowledgement_stops_the_launcher(self):
        with self.assertRaisesRegex(RuntimeError,'acknowledgement'):
            self.run_batch([{'id':'one','kind':'index-epub'}],fail_job=True,fail_ack=True)

    def test_empty_tail_does_not_chain_even_when_maintenance_or_backoff_is_pending(self):
        calls,output=self.run_batch([],pending=True)
        self.assertIn('continue_work=false',output)
        self.assertEqual([c[2] for c in calls if c[0].endswith('/finished')],[{'sequence':7,'pending':True}])

    def test_unscheduled_manual_failures_still_fail_the_command(self):
        with self.assertRaises(SystemExit) as error:
            self.run_batch([{'id':'one','kind':'index-epub'}],fail_job=True,scheduled=False)
        self.assertEqual(error.exception.code,1)
