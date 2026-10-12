import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

class ExportTest(unittest.TestCase):
    def test_sqlite_wal_backup_inventory_and_no_source_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            db=root/'legacy.db'
            connection=sqlite3.connect(db)
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute('CREATE TABLE reading_progress(user_id INTEGER,comic_name TEXT,last_page INTEGER)')
            connection.execute("INSERT INTO reading_progress VALUES (7,'novel:旧书',3)")
            connection.commit()
            novels=root/'novels';novels.mkdir()
            content=b'epub-inventory-test'
            (novels/'旧书.epub').write_bytes(content)
            (novels/'linked.epub').symlink_to(novels/'旧书.epub')
            output=root/'output'
            subprocess.run([sys.executable,'scripts/export-library.py','--database',str(db),'--novels',str(novels),'--output',str(output)],check=True,capture_output=True)
            manifest=json.loads((output/'manifest.json').read_text())
            self.assertEqual(manifest['legacy_table_counts']['reading_progress'],1)
            self.assertEqual(len(manifest['objects']),1)
            entry=manifest['objects'][0]
            self.assertEqual(entry['sha256'],hashlib.sha256(content).hexdigest())
            self.assertEqual(entry['legacy_progress_key'],'novel:旧书')
            snapshot=sqlite3.connect(output/'legacy.sqlite3')
            self.assertEqual(snapshot.execute('SELECT last_page FROM reading_progress').fetchone(),(3,))
            snapshot.close()
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM reading_progress').fetchone(),(1,))
            connection.close()
            self.assertEqual((novels/'旧书.epub').read_bytes(),content)

if __name__=='__main__':unittest.main()
