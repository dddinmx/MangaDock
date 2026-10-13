#!/usr/bin/env python3
"""Plan by default; import a private backup into the authenticated Workers app with --apply."""
import argparse,hashlib,mimetypes,importlib.util,json,os,re,sqlite3,sys,zipfile,xml.etree.ElementTree as ET
from html import unescape
from datetime import datetime,timezone,timedelta
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
def rows(db,table):
 exists=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone()
 return [dict(row) for row in db.execute('SELECT * FROM "'+table+'"')] if exists else []
def stamp(value):
 if not value:return 0
 if isinstance(value,(int,float)):return int(value)
 date=datetime.fromisoformat(str(value));date=date.replace(tzinfo=timezone(timedelta(hours=8))) if date.tzinfo is None else date
 return int(date.timestamp())
def digest(file):
 with file.open('rb') as source:return hashlib.file_digest(source,'sha256').hexdigest()
def stable(prefix,value):return prefix+hashlib.sha256(value.encode()).hexdigest()[:24]
def novel_metadata(file):
 with zipfile.ZipFile(file) as archive:
  container=ET.fromstring(archive.read('META-INF/container.xml'))
  package_path=next(node.attrib['full-path'] for node in container.iter() if node.tag.rsplit('}',1)[-1]=='rootfile')
  package=ET.fromstring(archive.read(package_path));metadata={}
  for field in ['title','creator','description','source','identifier']:
   metadata[field]=next((''.join(node.itertext()).strip() for node in package.iter() if node.tag.rsplit('}',1)[-1]==field),'')
  match=next((match for value in [metadata['source'],metadata['identifier']] if (match:=re.search(r'(?:fanqie-|/page/)(\d{8,24})',value))),None)
  return {'title':metadata['title'] or file.stem,'author':metadata['creator'] or '未知作者','description':unescape(re.sub(r'<[^>]+>','',metadata['description'])),'source_id':match.group(1) if match else file.stem}
def plan(args):
 db=sqlite3.connect(args.database.resolve().as_uri()+'?mode=ro',uri=True);db.row_factory=sqlite3.Row
 records={kind:[] for kind in ['users','groups','permissions','comics','chapters','novels','covers','progress','reading-time','hidden','login-logs','settings']};files={};comic_ids={};novel_ids={}
 records['users']=[{'id':str(row['id']),'username':row['username'],'password_hash':row['password_hash'],'role':row.get('role') or 'user','can_download':bool(row.get('can_download')),'can_view_adult':bool(row.get('can_view_adult'))} for row in rows(db,'user')]
 for kind,table in [('comic','comic_group'),('novel','novel_group')]:
  records['groups'] += [{'kind':kind,'name':row['name'],'created_at':stamp(row.get('created_at'))} for row in rows(db,table)]
 records['permissions']=[{'user_id':str(row['user_id']),'group_name':row['group_name']} for row in rows(db,'user_group_permission')]
 comic_groups={row['comic_name']:row['group_name'] for row in rows(db,'comic_group_membership')};novel_groups={row['novel_id']:row['group_name'] for row in rows(db,'novel_group_membership')};identities={row['comic_name']:row for row in rows(db,'comic_identity')}
 source_mapping={}
 mapping_file=args.comics/'comic.json' if args.comics else None
 if mapping_file and mapping_file.is_file():source_mapping=json.loads(mapping_file.read_text())
 def add_file(file,root):
  if file.is_symlink() or not file.resolve().is_relative_to(root.resolve()):raise ValueError('Source file outside library root')
  sha=digest(file);files[sha]={'path':str(file),'sha256':sha,'bytes':file.stat().st_size,'extension':file.suffix.lower().lstrip('.')};return sha
 if args.comics:
  for folder in sorted(args.comics.iterdir()):
   if not folder.is_dir() or folder.is_symlink() or folder.name.startswith('.'):continue
   chapters=sorted((file for file in folder.iterdir() if file.suffix.lower() in ('.cbz','.pdf')),key=lambda p:[int(v) if v.isdigit() else v.lower() for v in re.split(r'(\d+)',p.name)])
   if not chapters:continue
   identity=identities.get(folder.name,{});id=identity.get('comic_id') or stable('c_',folder.name)[:14];comic_ids[folder.name]=id
   records['comics'].append({'id':id,'name':folder.name,'provider':'import','source_url':source_mapping.get(folder.name,''),'description':identity.get('description') or '','group_name':comic_groups.get(folder.name,'默认分组'),'comic_format':1 if chapters[0].suffix.lower()=='.pdf' else 2,'created_at':int(folder.stat().st_mtime)})
   for index,file in enumerate(chapters):
    sha=add_file(file,args.comics);public_id='ch_'+hashlib.sha1(file.name.strip().encode()).hexdigest()[:12]
    records['chapters'].append({'id':stable('ch_',id+'|'+file.name),'public_id':public_id,'legacy_filename':file.name,'comic_id':id,'ordinal':index,'title':file.stem,'source_id':file.name,'object_key':'@'+sha,'size':file.stat().st_size,'format':file.suffix.lower()[1:]})
 if args.novels:
  for file in sorted(args.novels.glob('*.epub')):
   sha=add_file(file,args.novels);id=file.stem;novel_ids[id]=id
   records['novels'].append({'id':id,**novel_metadata(file),'object_key':'@'+sha,'sha256':sha,'size':file.stat().st_size,'created_at':int(file.stat().st_mtime),'group_name':novel_groups.get(id,'默认分组')})
 if args.covers:
  for file in sorted(args.covers.rglob('*')):
   if not file.is_file() or file.is_symlink() or file.suffix.lower() not in ('.jpg','.jpeg','.png','.webp'):continue
   relative=file.relative_to(args.covers);name=file.stem;kind='novel' if relative.parts[0]=='novels' else 'comic';media=(novel_ids if kind=='novel' else comic_ids).get(name)
   if not media:continue
   variant='mobile' if 'mobile' in relative.parts else 'hero' if 'hero' in relative.parts else 'original';sha=add_file(file,args.covers);records['covers'].append({'kind':kind,'media_id':media,'variant':variant,'object_key':'@'+sha})
 def media(name):
  if name.startswith('novel:'):return ('novel',novel_ids.get(name[6:]))
  return ('comic',comic_ids.get(name))
 for row in rows(db,'reading_progress'):
  kind,id=media(row['comic_name'])
  if not id:continue
  records['progress'].append({'kind':kind,'user_id':str(row.get('user_id') or 1),'media_id':id,'chapter':row.get('last_chapter') or 0,'page':row.get('last_page') or 0,'scroll':row.get('scroll_position') or 0,'client_ms':row.get('client_progress_ms') or stamp(row.get('last_read_at'))*1000,'updated_at':stamp(row.get('last_read_at')),'anchor_paragraph':row.get('anchor_paragraph'),'anchor_offset':row.get('anchor_offset')})
 times={}
 for row in rows(db,'reading_time'):
  kind,id=media(row['comic_name'])
  if not id:continue
  key=(str(row.get('user_id') or 1),('novel:' if kind=='novel' else '')+id,str(row.get('read_at') or row.get('created_at'))[:10]);seconds=row.get('duration_seconds') or (row.get('duration') or 0)*60
  if key not in times:times[key]={'user_id':key[0],'media_id':key[1],'day':key[2],'seconds':0,'media_name':row['comic_name']}
  times[key]['seconds']+=seconds
 records['reading-time']=list(times.values())
 records['hidden']=[{'user_id':str(row['user_id']),'kind':row['target_type'],'target':row['target_value']} for row in rows(db,'admin_hidden_library_item')]
 records['login-logs']=[{'username':row['username'],'ip_address':row.get('ip_address') or '','user_agent':row.get('user_agent') or '','success':bool(row.get('success')),'created_at':stamp(row.get('login_time'))} for row in rows(db,'login_log')][-1000:]
 records['settings']=[{'key':row['key'],'value':row['value']} for row in rows(db,'app_setting') if row['key'] in ('adult_content_enabled','comic_update_mode')]
 # Legacy novels had an open shelf. Preserve old reader access without granting
 # comic groups of the same name; new accounts use explicit novel grants.
 novel_names={row['name'] for row in records['groups'] if row['kind']=='novel'}|{row['group_name'] for row in records['novels']}
 for name in sorted(novel_names):
  if not any(row['kind']=='novel' and row['name']==name for row in records['groups']):records['groups'].append({'kind':'novel','name':name,'created_at':0})
 for user in records['users']:
  if user['role']=='user':records['permissions'] += [{'kind':'novel','user_id':user['id'],'group_name':name} for name in sorted(novel_names)]
 db.close();return records,files

def main():
 parser=argparse.ArgumentParser();parser.add_argument('--database',type=Path,required=True);parser.add_argument('--comics',type=Path);parser.add_argument('--novels',type=Path);parser.add_argument('--covers',type=Path);parser.add_argument('--url');parser.add_argument('--apply',action='store_true');parser.add_argument('--restore-admin-password',action='store_true');args=parser.parse_args()
 records,files=plan(args);total=sum(row['bytes'] for row in files.values());print(json.dumps({'planned_records':{kind:len(values) for kind,values in records.items()},'objects':len(files),'bytes':total,'apply':args.apply,'r2_free_storage_bytes':10_000_000_000},ensure_ascii=False))
 if not args.apply:return
 if not args.url or not os.environ.get('MANGADOCK_ADMIN_PASSWORD'):raise ValueError('Configure URL and administrator password in environment')
 spec=importlib.util.spec_from_file_location('cloud_compute',ROOT/'workers/scripts/cloud-compute.py');compute=importlib.util.module_from_spec(spec);spec.loader.exec_module(compute);client=compute.Client(args.url,os.environ['MANGADOCK_ADMIN_PASSWORD']);keys={};users={}
 def batches(kind,values):
  pending=[]
  for value in values:
   if pending and len(json.dumps(pending).encode())+len(json.dumps(value).encode())>12000:
    yield pending;pending=[]
   pending.append(value)
  if pending:yield pending
 def send(kind,values):
  output=[]
  for batch in batches(kind,values):output += client.request('/api/admin/import/metadata','POST',{'kind':kind,'records':batch,'restore_admin_password':args.restore_admin_password})['data'].get('mappings',[])
  return output
 users={row['old_id']:row['id'] for row in send('users',records['users'])};send('groups',records['groups'])
 for record in records['permissions']:record['user_id']=users.get(record['user_id'],record['user_id'])
 send('permissions',records['permissions']);send('comics',records['comics'])
 for sha,info in files.items():
  result=client.request('/api/admin/uploads','POST',{'size':info['bytes'],'sha256':sha,'extension':info['extension'],'mime':mimetypes.guess_type(info['path'])[0] or 'application/octet-stream'})['data'];keys['@'+sha]=result['key']
  if not result.get('already_stored'):
   with open(info['path'],'rb') as source:
    part=1
    while data:=source.read(5242880):
     client.request(f"/api/admin/uploads/{result['id']}/parts/{part}",'PUT',data,'application/octet-stream',extra_headers={'X-Content-SHA256':hashlib.sha256(data).hexdigest()});part+=1
   client.request(f"/api/admin/uploads/{result['id']}/complete",'POST',{})
 for kind in ['chapters','novels','covers','progress','reading-time','hidden','login-logs','settings']:
  for record in records[kind]:
   if 'object_key' in record:record['object_key']=keys[record['object_key']]
   if 'user_id' in record:record['user_id']=users.get(record['user_id'],record['user_id'])
  send(kind,records[kind])
 print(json.dumps({'imported':True,'objects':len(files),'bytes':total}))
if __name__=='__main__':
 try:main()
 except Exception as error:print('Migration failed: '+type(error).__name__,file=sys.stderr);raise SystemExit(1)
