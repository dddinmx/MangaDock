"""Explicit, prefix-scoped project teardown. Tokens remain in environment/memory."""
import hashlib,json,os,re,sys,time,urllib.request,urllib.error,urllib.parse,uuid
from pathlib import Path

def owned(name):
 return bool(re.match(r'^_*mangadock(?:[-_]|$)',str(name),re.I))

class Cloudflare:
 def __init__(self):
  self.account=os.environ['CLOUDFLARE_ACCOUNT_ID'];self.token=os.environ['CLOUDFLARE_API_TOKEN']
  if not re.fullmatch(r'[a-fA-F0-9]{32}',self.account):raise ValueError('Invalid account identifier')
 def call(self,path,method='GET',data=None,raw=False):
  url='https://api.cloudflare.com/client/v4'+('' if path.startswith('/user/') else '/accounts/'+self.account)+path
  headers={'Authorization':'Bearer '+self.token,'Content-Type':'application/json'}
  if raw:body,content_type=data;headers['Content-Type']=content_type
  else:body=None if data is None else json.dumps(data).encode()
  request=urllib.request.Request(url,data=body,headers=headers,method=method)
  try:
   with urllib.request.urlopen(request,timeout=60) as response:payload=json.load(response)
  except urllib.error.HTTPError as error:
   raise RuntimeError('Cloudflare '+method+' '+path.split('?')[0]+' HTTP '+str(error.code)) from None
  if payload.get('success') is False:raise RuntimeError('Cloudflare rejected '+method+' '+path.split('?')[0])
  return payload.get('result')
 def items(self,path,key=None):
  out=[];cursor='';page_size=10 if path=='/pages/projects' else 100
  for page in range(1,101):
   query={'per_page':page_size,'page':page}
   if cursor:query['cursor']=cursor
   data=self.call(path+'?'+urllib.parse.urlencode(query))
   rows=data.get(key,[]) if isinstance(data,dict) and key else data
   if not isinstance(rows,list):raise ValueError('Unexpected resource inventory shape')
   known=len(out)
   for row in rows:
    if row not in out:out.append(row)
   if len(rows)<page_size or len(out)==known:return out
   cursor=data.get('cursor','') if isinstance(data,dict) else ''
  raise ValueError('Resource inventory exceeds its bound')

def multipart(metadata,source=None):
 boundary='mangadock-cleanup-'+uuid.uuid4().hex;parts=[]
 def field(name,content,mime,filename=None):
  disposition='Content-Disposition: form-data; name="'+name+'"'+('; filename="'+filename+'"' if filename else '')
  parts.append(('--'+boundary+'\r\n'+disposition+'\r\nContent-Type: '+mime+'\r\n\r\n').encode()+content+b'\r\n')
 field('metadata',json.dumps(metadata).encode(),'application/json')
 if source is not None:field('cleanup.mjs',source.encode(),'application/javascript+module','cleanup.mjs')
 return b''.join(parts)+('--'+boundary+'--\r\n').encode(),'multipart/form-data; boundary='+boundary

def inventory(cf):
 report={'workers':[],'buckets':[],'queues':[],'d1':[],'durable_objects':[],'kv':[],'pages':[],'workflows':[],'unavailable':[]}
 all_workers=cf.items('/workers/scripts')
 for worker in all_workers:
  name=worker.get('id') or worker.get('name')
  if owned(name):
   settings=cf.call('/workers/scripts/'+urllib.parse.quote(name,safe='')+'/settings')
   report['workers'].append({'name':name,'migration_tag':worker.get('migration_tag'),'bindings':[{k:v for k,v in b.items() if k in ('type','name','bucket_name','database_id','namespace_id','queue_name','script_name','class_name','service')} for b in settings.get('bindings',[])]})
 for path,key,dest,name_key in [('/r2/buckets','buckets','buckets','name'),('/queues',None,'queues','queue_name'),('/d1/database',None,'d1','name'),('/workers/durable_objects/namespaces',None,'durable_objects','script'),('/storage/kv/namespaces',None,'kv','title'),('/pages/projects',None,'pages','name'),('/workflows',None,'workflows','name')]:
  try:
   rows=cf.items(path,key)
   report[dest]=[{k:v for k,v in row.items() if k in ('id','uuid','name','title','script','class','queue_id','queue_name','migration_tag')} for row in rows if owned(row.get(name_key))]
  except RuntimeError as error:
   report['unavailable'].append({'kind':dest,'error':str(error)})
 # Refuse deleting a project resource referenced by a different project.
 buckets={b['name'] for b in report['buckets']};databases={b['uuid'] for b in report['d1']};queues={b['queue_name'] for b in report['queues']};namespaces={b['id'] for b in report['durable_objects']};kv={b['id'] for b in report['kv']}
 report['external_references']=[]
 for worker in all_workers:
  name=worker.get('id') or worker.get('name')
  if owned(name):continue
  settings=cf.call('/workers/scripts/'+urllib.parse.quote(name,safe='')+'/settings')
  for binding in settings.get('bindings',[]):
   if binding.get('bucket_name') in buckets or binding.get('database_id') in databases or binding.get('queue_name') in queues or binding.get('namespace_id') in namespaces|kv or owned(binding.get('script_name')) or owned(binding.get('service')):
    report['external_references'].append({'worker':name,'binding':binding.get('name'),'type':binding.get('type')})
 return report

def s3_client(cf):
 import boto3
 from botocore.config import Config
 token_id=cf.call('/user/tokens/verify')['id']
 return boto3.client('s3',endpoint_url='https://'+cf.account+'.r2.cloudflarestorage.com',region_name='auto',aws_access_key_id=token_id,aws_secret_access_key=hashlib.sha256(cf.token.encode()).hexdigest(),config=Config(signature_version='s3v4',retries={'max_attempts':2},connect_timeout=20,read_timeout=60))

def clear_bucket(s3,bucket):
 count=0
 # Re-read the first page after each deletion, avoiding stale continuation tokens.
 for _ in range(10000):
  page=s3.list_objects_v2(Bucket=bucket,MaxKeys=1000);objects=page.get('Contents',[])
  if not objects:break
  result=s3.delete_objects(Bucket=bucket,Delete={'Objects':[{'Key':o['Key']} for o in objects],'Quiet':True})
  if result.get('Errors'):raise RuntimeError('R2 bulk deletion rejected for '+bucket)
  count+=len(objects)
 else:raise RuntimeError('R2 object deletion exceeded its bound')
 aborted=0
 for _ in range(10000):
  page=s3.list_multipart_uploads(Bucket=bucket,MaxUploads=1000);uploads=page.get('Uploads',[])
  if not uploads:break
  for upload in uploads:s3.abort_multipart_upload(Bucket=bucket,Key=upload['Key'],UploadId=upload['UploadId']);aborted+=1
 else:raise RuntimeError('R2 multipart cleanup exceeded its bound')
 if s3.list_objects_v2(Bucket=bucket,MaxKeys=1).get('KeyCount',0):raise RuntimeError('R2 bucket is not empty')
 return {'bucket':bucket,'objects_deleted':count,'multipart_aborted':aborted}

def teardown(cf,before):
 if before['external_references']:raise RuntimeError('Another project references MangaDock resources; refusing deletion')
 critical={'buckets','queues','d1','durable_objects','kv'}
 if any(x['kind'] in critical for x in before['unavailable']):raise RuntimeError('Cannot verify all project storage before deleting it')
 s3=s3_client(cf) if before['buckets'] else None
 # Verify S3 credentials before taking the app offline.
 for bucket in before['buckets']:s3.list_objects_v2(Bucket=bucket['name'],MaxKeys=1)
 for queue in before['queues']:
  base='/queues/'+queue['queue_id'];cf.call(base,'PATCH',{'queue_name':queue['queue_name'],'settings':{'delivery_paused':True}})
  consumers=cf.call(base+'/consumers')
  if isinstance(consumers,dict):consumers=consumers.get('consumers',[])
  for consumer in consumers:
   if consumer.get('script_name') and not owned(consumer['script_name']):raise RuntimeError('A different project consumes a MangaDock queue')
   cf.call(base+'/consumers/'+consumer['consumer_id'],'DELETE')
 for worker in before['workers']:
  base='/workers/scripts/'+worker['name'];cf.call(base+'/schedules','PUT',[])
  cf.call(base+'/settings','PATCH',multipart({'bindings':[]}),raw=True)
 removed={'workers':[],'buckets':[],'queues':[],'d1':[],'kv':[],'pages':[],'workflows':[]}
 for workflow in before['workflows']:
  cf.call('/workflows/'+workflow['name'],'DELETE');removed['workflows'].append(workflow['name'])
 for worker in before['workers']:
  name=worker['name'];classes=sorted({n['class'] for n in before['durable_objects'] if n.get('script')==name})
  if classes:
   tag=worker['migration_tag']
   if not tag:
    service=cf.call('/workers/services/'+name);tag=service.get('default_environment',{}).get('script',{}).get('migration_tag')
   metadata={'main_module':'cleanup.mjs','compatibility_date':'2026-10-15','bindings':[],'migrations':{'new_tag':'cleanup-'+uuid.uuid4().hex,'steps':[{'deleted_classes':classes}],**({'old_tag':tag} if tag else {})}}
   cf.call('/workers/scripts/'+name,'PUT',multipart(metadata,"export default {fetch(){return new Response('MangaDock removed',{status:410})}}"),raw=True)
  cf.call('/workers/scripts/'+name,'DELETE');removed['workers'].append(name)
 for page in before['pages']:
  cf.call('/pages/projects/'+page['name'],'DELETE');removed['pages'].append(page['name'])
 for queue in before['queues']:
  cf.call('/queues/'+queue['queue_id'],'DELETE');removed['queues'].append(queue['queue_name'])
 for bucket in before['buckets']:
  removed['buckets'].append(clear_bucket(s3,bucket['name']));cf.call('/r2/buckets/'+bucket['name'],'DELETE')
 for database in before['d1']:cf.call('/d1/database/'+database['uuid'],'DELETE');removed['d1'].append(database['name'])
 for namespace in before['kv']:cf.call('/storage/kv/namespaces/'+namespace['id'],'DELETE');removed['kv'].append(namespace['title'])
 return removed

def main():
 mode=os.environ.get('CLEANUP_MODE','inventory')
 if mode not in ('inventory','delete_all'):raise ValueError('Invalid cleanup mode')
 cf=Cloudflare();report={'mode':mode,'before':inventory(cf),'finished':False}
 path=Path('cloudflare-cleanup-results.json');path.write_text(json.dumps(report,indent=2))
 try:
  if mode=='delete_all':
   report['deleted']=teardown(cf,report['before']);report['after']=inventory(cf)
   if any(report['after'][k] for k in ('workers','buckets','queues','d1','durable_objects','kv','pages','workflows')):raise RuntimeError('MangaDock resources remain after teardown')
  report['finished']=True
 finally:path.write_text(json.dumps(report,indent=2))
 print(json.dumps(report))
if __name__=='__main__':
 try:main()
 except Exception as error:
  # Print neither credentials nor provider response payloads.
  print('::error::MangaDock cleanup failed: '+type(error).__name__+(': '+str(error) if isinstance(error,(ValueError,RuntimeError)) else ''),file=sys.stderr);sys.exit(1)
