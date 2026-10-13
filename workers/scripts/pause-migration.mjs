// Management/analytics APIs only: never execute a D1 query or deploy code.
import {writeFile,appendFile} from 'node:fs/promises';
const account=process.env.CLOUDFLARE_ACCOUNT_ID, token=process.env.CLOUDFLARE_API_TOKEN;
if(!account||!token)throw new Error('Cloudflare Secrets missing');
const targetNames=new Set(['mangadock-full-staging','mangadock-novel-staging']);
const result={paused:[],failed:[],d1:[],analytics:null};
async function api(path,method='GET',body){
  const response=await fetch(`https://api.cloudflare.com/client/v4/accounts/${account}${path}`,{method,headers:{Authorization:`Bearer ${token}`,'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined,signal:AbortSignal.timeout(30000)});
  const value=await response.json();
  if(!response.ok||value.success!==true)throw new Error(`Cloudflare ${method} ${path} HTTP ${response.status}; codes ${(value.errors||[]).map(e=>e.code).join(',')}`);
  return value.result;
}
for(const script of await api('/workers/scripts')){
  if(!targetNames.has(script.id))continue;
  for(const [kind,path,method,body] of [
    ['cron',`/workers/scripts/${script.id}/schedules`,'PUT',[]],
    ['workers.dev',`/workers/scripts/${script.id}/subdomain`,'POST',{enabled:false,previews_enabled:false}],
  ]) {
    try {await api(path,method,body);result.paused.push({script:script.id,kind});}
    catch(error){result.failed.push({script:script.id,kind,message:error.message});}
  }
}
for(const queue of await api('/queues?page=1&per_page=100')){
  if(!targetNames.has(queue.queue_name))continue;
  try {
    for(const consumer of await api(`/queues/${queue.queue_id}/consumers`)) {
      const script=consumer.script || consumer.script_name || consumer.service;
      if(consumer.type==='worker' && script && !targetNames.has(script))continue;
      await api(`/queues/${queue.queue_id}/consumers/${consumer.consumer_id}`,'DELETE');
      result.paused.push({queue:queue.queue_name,kind:'queue-consumer'});
    }
  }catch(error){result.failed.push({queue:queue.queue_name,kind:'queue-consumer',message:error.message});}
}
// Cloudflare Analytics reads do not consume D1 row-read/write allowance.
try {
  const databases=await api('/d1/database?page=1&per_page=100');
  result.d1=databases.filter(db=>targetNames.has(db.name)).map(db=>({id:db.uuid,name:db.name}));
  const start=new Date().toISOString().slice(0,10);
  const response=await fetch('https://api.cloudflare.com/client/v4/graphql',{method:'POST',headers:{Authorization:`Bearer ${token}`,'Content-Type':'application/json'},body:JSON.stringify({query:'query($account:string,$day:Date){viewer{accounts(filter:{accountTag:$account}){d1AnalyticsAdaptiveGroups(limit:1000,filter:{date_geq:$day,date_leq:$day}){dimensions{databaseId date} sum{rowsRead rowsWritten readQueries writeQueries}}}}}',variables:{account,day:start}}),signal:AbortSignal.timeout(30000)});
  const payload=await response.json();
  result.analytics=payload.errors?.length?{available:false,errors:payload.errors.map(e=>({message:String(e.message).replaceAll(account,'[account]')}))}:{available:true,groups:payload.data?.viewer?.accounts?.[0]?.d1AnalyticsAdaptiveGroups||[]};
}catch(error){result.analytics={available:false,message:error.message};}
await writeFile('workers/quota-control-results.json',JSON.stringify(result,null,2));
const notice=JSON.stringify(result);
console.log('::notice::'+notice.replaceAll('%','%25').replaceAll('\n','%0A'));
if(process.env.GITHUB_STEP_SUMMARY)await appendFile(process.env.GITHUB_STEP_SUMMARY,'\nMigration test workers paused without executing D1 SQL. See quota-control-results.json.\n');
if(result.failed.length)process.exitCode=1;
