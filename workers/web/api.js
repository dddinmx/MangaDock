export async function api(path,options={}) {
 const response=await fetch(path,options),payload=await response.json();
 if(response.status===401){location.assign('/login');throw new Error('请登录');}
 if(!response.ok)throw new Error(payload.error?.message||'请求失败');
 return payload.data;
}
export const me=()=>api('/api/v1/auth/me');
export const mutation=(csrf,data,method='POST')=>({method,headers:{'Content-Type':'application/json','X-CSRFToken':csrf},body:JSON.stringify(data)});
