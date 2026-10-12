import {chromium,expect} from '@playwright/test';
import {readFile,mkdir,writeFile} from 'node:fs/promises';
const {url}=JSON.parse(await readFile('deployment.json','utf8')),id=process.env.SOURCE_BOOK_ID||'6838936284967209991';
const browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE||undefined}),page=await browser.newPage({viewport:{width:1280,height:900}});
const errors=[];page.on('pageerror',error=>errors.push(error.message));
try{
 await page.goto(url+'/login');
 await page.getByLabel('用户名').fill('admin');await page.getByLabel('密码').fill(process.env.MANGADOCK_ADMIN_PASSWORD);
 await page.locator('#loginButton').click();await page.waitForURL(url+'/');
 await page.goto(`${url}/reader?book=${id}`);
 await page.waitForFunction(()=>{const title=document.querySelector('#reader-flow .novel-reader-title')?.textContent;return title&&title!=='正在加载…';},{timeout:30000});
 const title=await page.locator('#reader-flow .novel-reader-title').textContent();
 await page.keyboard.press('ArrowRight');
 await page.waitForTimeout(1500);
 const progress=await page.evaluate(async id=>{const response=await fetch(`/api/books/${id}/progress`);return (await response.json()).data;},id);
 if(!progress)throw new Error('真实阅读器没有保存进度');
 await page.reload();await page.waitForFunction(()=>{const title=document.querySelector('#reader-flow .novel-reader-title')?.textContent;return title&&title!=='正在加载…';});
 await expect(page.locator('#reader-page-status')).toContainText(`${progress.page+1} /`);
 if(errors.length)throw new Error('真实浏览器出现 JavaScript 错误');
 await mkdir('test-results',{recursive:true});await page.screenshot({path:'test-results/live-reader.png'});
 await writeFile('live-browser-results.json',JSON.stringify({url,book_id:id,chapter_title:title,progress,restored_after_reload:true,errors},null,2));
 console.log('Deployed real-source browser reading and progress restoration passed.');
}finally{await browser.close();}
