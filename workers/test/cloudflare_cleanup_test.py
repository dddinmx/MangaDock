import importlib.util,json,unittest
from pathlib import Path
spec=importlib.util.spec_from_file_location('cleanup',Path(__file__).parents[1]/'scripts/cloudflare-cleanup.py');cleanup=importlib.util.module_from_spec(spec);spec.loader.exec_module(cleanup)
class CleanupTests(unittest.TestCase):
 def test_scope_excludes_other_projects(self):
  for name in ['mangadock','mangadock-turso-staging','mangadock-full-production','__mangadock-assets','mangadock_legacy']:
   self.assertTrue(cleanup.owned(name))
  for name in ['other-mangadock','mangadockevil','fanqie','other','',None]:self.assertFalse(cleanup.owned(name))
 def test_multipart_does_not_expose_or_mix_data(self):
  metadata={'bindings':[],'migrations':{'new_tag':'cleanup','steps':[{'deleted_classes':['ApplicationService']}]}}
  body,content=cleanup.multipart(metadata,'export default {}')
  self.assertIn(b'ApplicationService',body);self.assertIn(b'export default {}',body);self.assertIn('multipart/form-data; boundary=',content)
 def test_deletes_each_real_object_and_aborts_multipart_before_empty(self):
  class S3:
   objects=[{'Key':'one'},{'Key':'two'}];uploads=[{'Key':'partial','UploadId':'id'}];deleted=[];aborted=[]
   def list_objects_v2(self,**kwargs):return {'Contents':self.objects,'KeyCount':len(self.objects)}
   def delete_objects(self,**kwargs):self.deleted.extend(kwargs['Delete']['Objects']);self.objects=[];return {}
   def list_multipart_uploads(self,**kwargs):return {'Uploads':self.uploads}
   def abort_multipart_upload(self,**kwargs):self.aborted.append(kwargs);self.uploads=[]
  s3=S3();report=cleanup.clear_bucket(s3,'mangadock-test');self.assertEqual(report['objects_deleted'],2);self.assertEqual(report['multipart_aborted'],1)
 def test_external_reference_stops_before_mutation(self):
  class CF:
   def call(self,*args,**kwargs):raise AssertionError('Mutation must not execute')
  with self.assertRaisesRegex(RuntimeError,'Another project'):cleanup.teardown(CF(),{'external_references':[{'worker':'other'}]})
 def test_bulk_delete_errors_stop_without_claiming_empty(self):
  class S3:
   def list_objects_v2(self,**kwargs):return {'Contents':[{'Key':'one'}]}
   def delete_objects(self,**kwargs):return {'Errors':[{'Code':'AccessDenied'}]}
  with self.assertRaisesRegex(RuntimeError,'bulk deletion'):cleanup.clear_bucket(S3(),'mangadock-test')
if __name__=='__main__':unittest.main()
