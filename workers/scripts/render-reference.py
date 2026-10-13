#!/usr/bin/env python3
"""Render unchanged original Jinja templates for browser screenshot comparisons."""
import json,sys
from pathlib import Path
from urllib.parse import quote,urlencode
from types import SimpleNamespace
from datetime import datetime,timezone,timedelta
from jinja2 import Environment,FileSystemLoader
ROOT=Path(__file__).resolve().parents[2]
data=json.load(sys.stdin);name=data['template'];endpoint=data['endpoint'];values=data.get('values',{})
routes={row['endpoint']:row['path'] for row in json.loads((ROOT/'workers/legacy-routes.json').read_text()) if not row['path'].startswith('/api/v1')}
def url_for(endpoint,**kwargs):
 if endpoint=='static':return '/static/'+kwargs['filename']
 import re
 path=routes[endpoint];used=set()
 def replace(match):
  key=match.group(1);used.add(key);return quote(str(kwargs.get(key,'')),safe='')
 path=re.sub(r'<(?:(?:int|path):)?(\w+)>',replace,path);query=urlencode({key:value for key,value in kwargs.items() if key not in used and value is not None})
 return path+('?' +query if query else '')
env=Environment(loader=FileSystemLoader(ROOT/'templates'),autoescape=True)
user={'id':1,'username':'admin','role':'admin','is_admin':True,'can_download':False,'can_view_adult':False}
context={'asset_url':lambda path:'/static/'+path,'url_for':url_for,'csrf_token':lambda:'reference-csrf','get_flashed_messages':lambda:[],'request':SimpleNamespace(endpoint=endpoint,args=data.get('args',{})),'session':{'user_id':1,'username':'admin'},'current_user':user,'is_admin_user':True,'can_manage_library':True,'release_version':'2.11.29','library_mode':data.get('mode','comic'),'adult_content_enabled':False,'anilist_account':None,'cover_image_url':lambda name,**kwargs:'/static/cover/'+quote(name,safe='')+'.jpg','cover_hero_image_url':lambda name,**kwargs:'/static/cover/hero/'+quote(name,safe='')+'.jpg','cover_hero_mobile_image_url':lambda name,**kwargs:'/static/cover/hero/mobile/'+quote(name,safe='')+'.jpg','is_adult_comic':lambda comic:bool(comic.get('adult')),'display_reading_minutes':lambda minutes:max(0,int(minutes or 0))}
if name=='login.html':context['session']={};context['current_user']=None;context['is_admin_user']=False;context['can_manage_library']=False
for field in ['created_at','last_read_at']:
 for key in ['novel','spotlight','task']:
  if isinstance(values.get(key),dict) and isinstance(values[key].get(field),(int,float)):values[key][field]=datetime.fromtimestamp(values[key][field],timezone(timedelta(hours=8)))
def display_minutes(minutes):
 safe=max(0,int(minutes or 0));hours,rest=divmod(safe,60)
 return f'{hours}小时 {rest}分钟' if hours and rest else f'{hours}小时' if hours else f'{rest}分钟'
context['display_reading_minutes']=display_minutes
context.update(values)
print(env.get_template(name).render(**context))
