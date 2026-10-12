import {mkdir,writeFile} from 'node:fs/promises';
import {createRequire} from 'node:module';
import {pbkdf2Sync,createHash} from 'node:crypto';
import {spawnSync} from 'node:child_process';
const require=createRequire(import.meta.url),JSZip=require('../../static/js/vendor/jszip.min.js');
export async function seed({persist='.wrangler/state/e2e',password='local-e2e-password-2026',fixture=true}={}){
 await mkdir('.wrangler/e2e',{recursive:true});
 const args=['--local','--persist-to',persist];
 const cli=argv=>{const result=spawnSync('npx',['wrangler',...argv],{stdio:'inherit',env:{...process.env,WRANGLER_SEND_METRICS:'false'}});if(result.status!==0)throw new Error('本地初始化失败');};
 cli(['d1','migrations','apply','mangadock-local',...args]);
 const encoded=`pbkdf2:sha256:600000$local-test-salt$${pbkdf2Sync(password,'local-test-salt',600000,32,'sha256').toString('hex')}`;
 let sql=`INSERT OR REPLACE INTO users(id,username,password_hash,role) VALUES ('admin','admin','${encoded}','admin'); DELETE FROM sessions; DELETE FROM login_attempts;`;
 if(fixture){
  const zip=new JSZip();zip.file('mimetype','application/epub+zip');zip.file('META-INF/container.xml','<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/book.opf"/></rootfiles></container>');
  zip.file('OEBPS/book.opf','<package xmlns="http://www.idpf.org/2007/opf"><metadata><title>本地测试小说</title></metadata><manifest><item id="a" href="one.xhtml" media-type="application/xhtml+xml"/><item id="b" href="two.xhtml" media-type="application/xhtml+xml"/></manifest><spine><itemref idref="a"/><itemref idref="b"/></spine></package>');
  for(const [name,title] of [['one','第一章'],['two','第二章']])zip.file(`OEBPS/${name}.xhtml`,`<html xmlns="http://www.w3.org/1999/xhtml"><head><title>${title}</title></head><body><h1>${title}</h1><script>window.EPUB_INJECTED=true</script>${'<p>故事在云端继续。这是一段用于验证翻页和阅读进度的测试内容。</p>'.repeat(50)}</body></html>`);
  const bytes=await zip.generateAsync({type:'nodebuffer'}),digest=createHash('sha256').update(bytes).digest('hex'),key=`novels/123456789/${digest}.epub`;
  await writeFile('.wrangler/e2e/fixture.epub',bytes);
  cli(['r2','object','put',`mangadock-local/${key}`,'--file','.wrangler/e2e/fixture.epub',...args]);
  sql+=` INSERT OR REPLACE INTO books VALUES ('123456789','123456789','本地测试小说','测试作者','${key}',${bytes.length},'${digest}',${Math.floor(Date.now()/1000)}); DELETE FROM progress; DELETE FROM jobs;`;
 }
 await writeFile('.wrangler/e2e/seed.sql',sql);cli(['d1','execute','mangadock-local','--file','.wrangler/e2e/seed.sql',...args]);
}
if(process.argv[1]===new URL(import.meta.url).pathname)await seed({persist:process.env.LOCAL_PERSIST||'.wrangler/state/default',fixture:process.env.NO_FIXTURE!=='1'});
