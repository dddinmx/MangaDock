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

class CompleteMigrationTests(unittest.TestCase):
    def test_readonly_import_preserves_epub_metadata_old_ids_and_anchors(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);database=root/'app.db';novels=root/'novels';novels.mkdir()
            with sqlite3.connect(database) as db:
                db.executescript("CREATE TABLE user(id,username,password_hash,role,can_download,can_view_adult); CREATE TABLE reading_progress(user_id,comic_name,last_chapter,last_page,anchor_paragraph,anchor_offset,client_progress_ms,last_read_at); INSERT INTO user VALUES(1,'admin','pbkdf2:sha256:260000$salt$"+'a'*64+"','admin',0,0); INSERT INTO reading_progress VALUES(1,'novel:旧文件名',2,3,8,15,10000,'2025-01-01T12:00:00');")
            with zipfile.ZipFile(novels/'旧文件名.epub','w') as epub:
                epub.writestr('META-INF/container.xml','<container><rootfile full-path="book.opf"/></container>')
                epub.writestr('book.opf','<package xmlns:dc="http://purl.org/dc/elements/1.1/"><metadata><dc:title>真实书名</dc:title><dc:creator>原作者</dc:creator><dc:identifier>fanqie-6838936284967209991</dc:identifier><dc:description>原简介</dc:description></metadata></package>')
            before=database.read_bytes()
            records,files=importer.plan(argparse.Namespace(database=database,novels=novels,comics=None,covers=None))
            novel=records['novels'][0]
            self.assertEqual((novel['id'],novel['title'],novel['author'],novel['source_id']),('旧文件名','真实书名','原作者','6838936284967209991'))
            progress=records['progress'][0]
            self.assertEqual((progress['media_id'],progress['chapter'],progress['page'],progress['anchor_paragraph'],progress['anchor_offset']),('旧文件名',2,3,8,15))
            self.assertEqual(database.read_bytes(),before)
            self.assertEqual(len(files),1)

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
