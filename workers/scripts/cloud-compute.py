#!/usr/bin/env python3
"""Bounded cloud CI worker. Credentials stay in environment/memory; no private output is published."""
import argparse,importlib.util,hashlib,mimetypes,shutil,gzip,io,json,os,re,subprocess,sys,tempfile,time,urllib.request,urllib.error,zipfile
from pathlib import Path
from PIL import Image,ImageFilter,ImageOps
ROOT=Path(__file__).resolve().parents[2]
class NoRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,*args,**kwargs): return None
class Client:
 def __init__(self,url,password):
  self.url=url.rstrip('/');self.cookie='';self.csrf='';self.opener=urllib.request.build_opener(NoRedirect())
  response=self.request('/api/v1/auth/login','POST',{'username':'admin','password':password},json_response=False)
  self.cookie=response.headers['Set-Cookie'].split(';',1)[0];self.csrf=json.load(response)['data']['csrf_token']
 def request(self,path,method='GET',data=None,mime='application/json',json_response=True,extra_headers=None):
  if not path.startswith('/api/'): raise ValueError('Invalid path')
  body=json.dumps(data).encode() if isinstance(data,(dict,list)) else data
  req=urllib.request.Request(self.url+path,data=body,method=method,headers={'User-Agent':'MangaDock/5.0 Cloud-Compute','Origin':self.url,'Cookie':self.cookie,'X-CSRFToken':self.csrf,'Content-Type':mime,**(extra_headers or {})})
  response=self.opener.open(req,timeout=120)
  if json_response:
   with response:return json.load(response)
  return response
 def download(self,path,destination):
  size=0
  with self.request(path,json_response=False) as response,open(destination,'wb') as output:
   while part:=response.read(1024*1024):
    size+=len(part)
    if size>2*1024**3: raise ValueError('Input exceeds 2 GB cloud job limit')
    output.write(part)
 def upload(self,path,file,mime):
  size=file.stat().st_size
  if size>20*1024**2:
   job_id=path.split('/')[5];variant=path.rsplit('/',1)[-1];sha=hashlib.file_digest(open(file,'rb'),'sha256').hexdigest()
   result=self.request('/api/admin/uploads','POST',{'job_id':job_id,'variant':variant,'size':size,'sha256':sha,'mime':mime})['data']
   if result.get('already_stored'):return result
   upload_id=result['id']
   with file.open('rb') as source:
    number=1
    while part:=source.read(5242880):
     headers={'X-Content-SHA256':hashlib.sha256(part).hexdigest()};self.request(f'/api/admin/uploads/{upload_id}/parts/{number}','PUT',part,'application/octet-stream',extra_headers=headers);number+=1
   return self.request(f'/api/admin/uploads/{upload_id}/complete','POST',{})
  return self.request(path,'PUT',file.read_bytes(),mime)

def resize_steps(image,target):
 for _ in range(8):
  factor=max(target[0]/image.width,target[1]/image.height)
  if factor<=1.02:return image if image.size==target else image.resize(target,Image.Resampling.LANCZOS)
  step=min(factor,2);image=image.resize((round(image.width*step),round(image.height*step)),Image.Resampling.LANCZOS)
  if image.width>=target[0] and image.height>=target[1]:return image.resize(target,Image.Resampling.LANCZOS)
 return image

def enhance(input_path,folder,ai=True):
 with Image.open(input_path) as source:
  source.load();image=ImageOps.exif_transpose(source).convert('RGB')
 width,height=image.size
 if width*height>40_000_000: raise ValueError('Cover exceeds pixel budget')
 image.save(folder/'original.jpg',quality=92,optimize=True,progressive=True)
 factor=min(1920/max(width,height),6,(24_000_000/(width*height))**.5);target=(max(1,round(width*factor)),max(1,round(height*factor))) if max(width,height)<1920 else None
 engine='pillow';actual=(max(target[0]/width,target[1]/height) if target else 1)
 if ai and target and actual>=2:
  try:result=subprocess.run([sys.executable,str(ROOT/'tools/cover_sr_worker.py'),'--input',str(input_path),'--output',str(folder/'sr.png'),'--model',str(ROOT/'tools/models/RealESRGAN_x4plus_anime_6B.onnx'),'--tile','128','--tile-overlap','16','--quiet'],timeout=180,capture_output=True,env={**os.environ,'OMP_NUM_THREADS':'2'})
  except subprocess.TimeoutExpired:result=None
  if result is not None and result.returncode==0:
   with Image.open(folder/'sr.png') as restored:image=restored.convert('RGB')
   engine='onnx'
 if target and image.size!=target:image=resize_steps(image,target)
 image=image.filter(ImageFilter.UnsharpMask(radius=1.3,percent=70 if engine=='onnx' else 150,threshold=2))
 if engine=='pillow':image=image.filter(ImageFilter.UnsharpMask(radius=2.8,percent=55,threshold=3))
 image.save(folder/'hero.jpg',quality=92,optimize=True,progressive=True)
 mobile=image.copy();mobile.thumbnail((1800,1800),Image.Resampling.LANCZOS);mobile.save(folder/'mobile.jpg',quality=84,optimize=True,progressive=True)
 return {'engine':engine,'original':[width,height],'hero':list(image.size),'mobile':list(mobile.size)}

def archive_pages(input_path,folder):
 suffix=input_path.suffix.lower();paths=[]
 if suffix=='.pdf':
  import pypdfium2
  pdf=pypdfium2.PdfDocument(str(input_path))
  if len(pdf)>10000:raise ValueError('Too many PDF pages')
  for i in range(len(pdf)):
   page=pdf[i];image=page.render(scale=min(2,2048/max(page.get_size()))).to_pil().convert('RGB');file=folder/f'{i:05}.jpg';image.save(file,quality=92);paths.append(file);page.close()
  pdf.close()
 else:
  with zipfile.ZipFile(input_path) as archive:
   entries=[entry for entry in archive.infolist() if not entry.is_dir() and re.search(r'\.(jpe?g|png|webp|gif|avif)$',entry.filename,re.I)]
   entries.sort(key=lambda entry:[int(p) if p.isdigit() else p.lower() for p in re.split(r'(\d+)',entry.filename)])
   if not entries or len(entries)>10000:raise ValueError('Invalid archive page count')
   total=0
   for i,entry in enumerate(entries):
    total+=entry.file_size
    if entry.file_size>20*1024**2 or total>2*1024**3 or entry.compress_size and entry.file_size/entry.compress_size>200:raise ValueError('Archive exceeds extraction budget')
    content=archive.read(entry)
    with Image.open(io.BytesIO(content)) as image:
     image.load();suffix={'JPEG':'jpg','PNG':'png','WEBP':'webp','GIF':'gif','AVIF':'avif','BMP':'bmp'}.get(image.format)
     if not suffix:raise ValueError('Unsupported archive image format')
    file=folder/f'{i:05}.{suffix}';file.write_bytes(content);paths.append(file)
 return paths

def process(client,job,folder):
 job_id=job['id'];base=f'/api/admin/compute/jobs/{job_id}'
 claimed=client.request(base+'/claim','POST',{})['data'];kind=claimed['kind']
 if kind=='cover':
  client.download(base+'/input',folder/'source');result=enhance(folder/'source',folder)
  for name in ['original','hero','mobile']:client.upload(base+'/output/'+name,folder/(name+'.jpg'),'image/jpeg')
  client.request(base+'/complete','POST',{});return result
 if kind in ('legacy-source','legacy-chapter'):
  spec=importlib.util.spec_from_file_location('legacy_sources',ROOT/'workers/scripts/legacy-source.py');legacy=importlib.util.module_from_spec(spec);spec.loader.exec_module(legacy)
  if kind=='legacy-source':
   source=legacy.resolve(claimed['provider'],claimed['url']);file=folder/'source.json';file.write_text(json.dumps(source),encoding='utf-8');client.upload(base+'/output/source',file,'application/json');client.request(base+'/complete','POST',{});return {'chapters':len(source['chapters'])}
  paths=legacy.download_pages(claimed['provider'],claimed['chapter'],folder);pages=[]
  for i,path in enumerate(paths):
   with Image.open(path) as image:pages.append({'width':image.width,'height':image.height})
   client.upload(base+f'/output/page/{i}',path,mimetypes.guess_type(path)[0] or 'image/jpeg')
  client.request(base+'/complete','POST',{'pages':pages});return {'pages':len(paths)}
 if kind=='banner':
  spec=importlib.util.spec_from_file_location('legacy_sources',ROOT/'workers/scripts/legacy-source.py');legacy=importlib.util.module_from_spec(spec);spec.loader.exec_module(legacy);ns=legacy.namespace('baozimh_org');legacy.load_functions(ROOT/'mangadock/services/home_banner.py',ns);candidates=[]
  for function in ['_search_anilist','_search_kitsu','_search_jikan']:
   if function in ns:
    try:candidates+=ns[function](claimed['comic_name'])
    except Exception:pass
  ranked=ns['_rank_candidates'](claimed['comic_name'],candidates)
  for candidate in ranked[:3]:
   try:
    response=ns['safe_http_get'](candidate['image_url'],headers=ns['default_headers'](),timeout=20);response.raise_for_status();file=folder/'banner-source';file.write_bytes(response.content)
    with Image.open(file) as image:
     if not (960<=image.width and 1.35<=image.width/image.height<=3.2 and image.width*image.height<=40_000_000):continue
    result=enhance(file,folder);client.upload(base+'/output/hero',folder/'hero.jpg','image/jpeg');client.request(base+'/complete','POST',{'found':True});return {'found':True,**result}
   except Exception:continue
  client.request(base+'/complete','POST',{'found':False});return {'found':False}
 if kind=='index-epub':
  spec=importlib.util.spec_from_file_location('legacy_sources',ROOT/'workers/scripts/legacy-source.py');legacy=importlib.util.module_from_spec(spec);spec.loader.exec_module(legacy);ns=legacy.namespace('baozimh_org');ns['NOVEL_ROOT']=str(folder);ns['NOVEL_COVER_ROOT']=str(folder/'covers');legacy.load_functions(ROOT/'mangadock/services/novels.py',ns);source=folder/(claimed['book_id']+'.epub');client.download(base+'/input',source);chapters=ns['get_novel_chapters'](claimed['book_id']);index=[]
  for item in chapters:
   chapter=ns['get_novel_chapter'](claimed['book_id'],item['index']);file=folder/f"chapter-{item['index']}.gz";file.write_bytes(gzip.compress(json.dumps(chapter,ensure_ascii=False).encode()));client.upload(base+f"/output/chapter/{item['index']}",file,'application/gzip');index.append({'index':item['index'],'title':item['title'],'key':f"compute/{job_id}/chapter/{item['index']}",'gzip':True})
  file=folder/'index.json';file.write_text(json.dumps({'chapters':index,'total_chapters':len(index)},ensure_ascii=False));client.upload(base+'/output/index',file,'application/json');client.request(base+'/complete','POST',{});return {'chapters':len(index)}
 if kind=='fanqie-archive':
  client.download(base+'/input',folder/'outer.zip')
  with zipfile.ZipFile(folder/'outer.zip') as archive:
   info=archive.getinfo('manifest.json')
   if info.file_size>2*1024**2:raise ValueError('Comic manifest too large')
   manifest=json.loads(archive.read(info))
   if str(manifest.get('metadata',{}).get('book_id'))!=claimed['book_id'] or manifest.get('format')!=claimed['format']:raise ValueError('Comic manifest mismatch')
   items=manifest.get('chapters') or []
   if len(items)!=1 or str(items[0].get('id'))!=claimed['source_id']:raise ValueError('Comic chapter selection mismatch')
   name=items[0]['file']
   if not name.startswith('chapters/') or len(Path(name).parts)!=2 or '..' in Path(name).parts or not name.endswith('.'+claimed['format']):raise ValueError('Unsafe chapter path')
   info=archive.getinfo(name)
   if info.file_size>2*1024**3:raise ValueError('Comic chapter too large')
   file=folder/('chapter.'+claimed['format'])
   with archive.open(info) as source,file.open('wb') as output:shutil.copyfileobj(source,output,1024**2)
  client.upload(base+'/output/artifact',file,'application/pdf' if claimed['format']=='pdf' else 'application/vnd.comicbook+zip');client.request(base+'/complete','POST',{});return {'bytes':file.stat().st_size}
 if kind=='archive':
  file=folder/('source.pdf' if claimed.get('format')=='pdf' else 'source.cbz');client.download(base+'/input',file);paths=archive_pages(file,folder);pages=[]
  for i,path in enumerate(paths):
   with Image.open(path) as image:pages.append({'width':image.width,'height':image.height})
   client.upload(base+f'/output/page/{i}',path,mimetypes.guess_type(path)[0] or 'image/jpeg')
  client.request(base+'/complete','POST',{'pages':pages});return {'pages':len(paths)}
 if kind=='pdf':
  import img2pdf
  with client.request(base+'/input',json_response=False) as response:manifest=json.load(response)
  files=[]
  for i,_ in enumerate(manifest):
   file=folder/f'{i:05}.jpg';client.download(base+f'/pages/{i}',file)
   with Image.open(file) as image:ImageOps.exif_transpose(image).convert('RGB').save(file,quality=95)
   files.append(str(file))
  output=folder/'chapter.pdf';output.write_bytes(img2pdf.convert(files));client.upload(base+'/output/artifact',output,'application/pdf');client.request(base+'/complete','POST',{});return {'pages':len(files),'bytes':output.stat().st_size}
 raise ValueError('Unknown compute kind')

def main():
 parser=argparse.ArgumentParser();parser.add_argument('--url',default=os.getenv('MANGADOCK_URL'));parser.add_argument('--limit',type=int,default=10);args=parser.parse_args()
 if not args.url:args.url=json.loads((ROOT/'workers/deployment.json').read_text())['url']
 password=os.environ.get('MANGADOCK_ADMIN_PASSWORD');
 if not password:raise SystemExit('MANGADOCK_ADMIN_PASSWORD is missing')
 client=Client(args.url,password);client.request('/api/admin/compute/discover','POST',{});results=[]
 for job in client.request('/api/admin/compute/jobs')['data'][:max(0,min(args.limit,20))]:
  start=time.monotonic()
  try:
   with tempfile.TemporaryDirectory(prefix='md-cloud-compute-') as temp:result=process(client,job,Path(temp))
   results.append({'id':job['id'],'kind':job['kind'],'ok':True,'elapsed_s':round(time.monotonic()-start,2),**result})
  except Exception as error:
   try:client.request(f"/api/admin/compute/jobs/{job['id']}/fail",'POST',{})
   except Exception:pass
   results.append({'id':job['id'],'kind':job['kind'],'ok':False,'error_type':type(error).__name__})
 print(json.dumps(results,ensure_ascii=False))
 if any(not result['ok'] for result in results):raise SystemExit(1)
if __name__=='__main__':main()
