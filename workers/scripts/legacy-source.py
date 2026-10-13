"""Reuse the original provider parsers in an isolated CI process, without starting Flask or local daemons."""
import ast,base64,hashlib,unicodedata,concurrent.futures,html,importlib.util,json,os,re,shutil,sys,time,types
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin,urlparse,urlsplit,urlunsplit,parse_qs,quote,unquote,urlencode
from difflib import SequenceMatcher
from urllib.parse import urlencode
from functools import lru_cache
from threading import Lock,get_ident
from concurrent.futures import ThreadPoolExecutor
import requests
from bs4 import BeautifulSoup
from PIL import Image
ROOT=Path(__file__).resolve().parents[2]
def load_functions(path,namespace,only=None):
 tree=ast.parse(path.read_text());nodes=[]
 for node in tree.body:
  if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)) and (only is None or node.name in only):nodes.append(node)
  elif only is None and isinstance(node,ast.Assign):
   try:exec(compile(ast.Module(body=[node],type_ignores=[]),str(path),'exec'),namespace)
   except (NameError,AttributeError):pass
 exec(compile(ast.Module(body=nodes,type_ignores=[]),str(path),'exec'),namespace)
 return namespace

def namespace(provider):
 # The package initializer starts Flask, so load only the actual settings and pure parser functions.
 for name,path in [('mangadock',ROOT/'mangadock'),('mangadock.utils',ROOT/'mangadock/utils')]:
  package=types.ModuleType(name);package.__path__=[str(path)];sys.modules[name]=package
 spec=importlib.util.spec_from_file_location('mangadock.settings',ROOT/'mangadock/settings.py');settings=importlib.util.module_from_spec(spec);sys.modules['mangadock.settings']=settings;spec.loader.exec_module(settings)
 spec=importlib.util.spec_from_file_location('mangadock.utils.http',ROOT/'mangadock/utils/http.py');http=importlib.util.module_from_spec(spec);sys.modules['mangadock.utils.http']=http;spec.loader.exec_module(http)
 ns={**vars(settings),**globals(),'__name__':'legacy_provider','safe_http_get':http.safe_http_get,'safe_http_post':http.safe_http_post,'validate_safe_upstream_url':http.validate_safe_upstream_url,'safe_print':lambda *args,**kwargs:None}
 load_functions(ROOT/'mangadock/utils/__init__.py',ns,{'safe_int'})
 load_functions(ROOT/'mangadock/utils/media.py',ns,{'sanitize_filename','default_headers'})
 load_functions(ROOT/'mangadock/services/library.py',ns,{'normalize_comic_description'})
 load_functions(ROOT/'mangadock/services/providers/common.py',ns,{'extract_description_from_html'})
 path=ROOT/('mangadock/services/manhuagui.py' if provider=='manhuagui' else 'mangadock/services/providers/'+provider+'.py')
 return load_functions(path,ns)

FUNCTIONS={'baozimh_org':'load_baozimh_org_source','baozimhcn':'load_baozimhcn_source','hipmh':'load_hipmh_source','mxs':'load_mxs_source','guazimanhua':'load_guazimanhua_source','kalamanhua':'load_kalamanhua_source','manhuagui':'load_manhuagui_source'}
def resolve(provider,url):
 ns=namespace(provider);source=ns[FUNCTIONS[provider]](url);chapters=[]
 for i,chapter in enumerate(source['chapters']):
  chapters.append({'order':i,'title':chapter['title'],'id':str(chapter.get('chapter_id') or chapter.get('chapter_hid') or chapter.get('chapter_url') or chapter.get('id') or i),'url':chapter.get('chapter_url'),'mid':source.get('mid'),'hid':chapter.get('chapter_hid')})
 return {'provider':provider,'title':source['title'],'description':source.get('description') or '','cover':source.get('cover_url'),'chapters':chapters,'adult':provider in ('mxs','kalamanhua')}

def image_urls(provider,chapter):
 ns=namespace(provider)
 if provider=='baozimh_org':
  images,line=ns['load_baozimh_org_chapter_images'](chapter['mid'],chapter['id']);host=ns['resolve_baozimh_org_image_host'](line);return [ns['build_baozimh_org_image_url'](image['url'],host) for image in sorted(images,key=lambda image:image.get('order',0))]
 if provider=='hipmh':return ns['load_hipmh_chapter_images'](chapter.get('hid') or chapter['id'])
 if provider=='guazimanhua':return ns['load_guazimanhua_chapter_images'](chapter['url'])
 if provider=='kalamanhua':return ns['load_kalamanhua_chapter_images'](chapter['url'])
 if provider=='manhuagui':
  content=ns['fetch_html'](chapter['url']);config=ns['extract_chapter_config'](content);host=ns['pick_image_host'](config,chapter['url']);return ns['build_image_urls'](config,host)
 if provider=='baozimhcn':
  content=ns['_fetch_page'](chapter['url'],requests.Session());soup=BeautifulSoup(content,'html.parser');return [urljoin(chapter['url'],img.get('data-src') or img.get('src')) for img in soup.select('img.comic-contain__item') if img.get('data-src') or img.get('src')]
 if provider=='mxs':
  response=ns['safe_http_get'](chapter['url'],headers=ns['default_headers'](),timeout=30);response.raise_for_status();soup=BeautifulSoup(response.text,'html.parser');return [urljoin(chapter['url'],img['data-original']) for img in soup.select('img.lazy[data-original]')]
 raise ValueError('Unsupported provider')

def download_pages(provider,chapter,folder):
 ns=namespace(provider);urls=image_urls(provider,chapter);paths=[]
 if not urls or len(urls)>10000:raise ValueError('Invalid page count')
 for i,url in enumerate(urls):
  response=ns['safe_http_get'](url,headers=ns['default_headers'](referer=chapter.get('url')),timeout=30,stream=True);response.raise_for_status();parts=[];size=0
  try:
   for part in response.iter_content(65536):
    size+=len(part)
    if size>20*1024**2:raise ValueError('Page exceeds limit')
    parts.append(part)
  finally:response.close()
  from io import BytesIO
  with Image.open(BytesIO(b''.join(parts))) as image:
   image.load();suffix={'JPEG':'jpg','PNG':'png','WEBP':'webp','GIF':'gif','AVIF':'avif','BMP':'bmp'}.get(image.format)
   if not suffix:raise ValueError('Unsupported image format')
   path=folder/f'{i:05}.{suffix}';path.write_bytes(b''.join(parts));paths.append(path)
 return paths
