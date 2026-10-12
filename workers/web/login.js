const form=document.getElementById('loginForm');
form.addEventListener('submit',async event=>{
 event.preventDefault();
 const error=document.getElementById('login-api-error'),button=document.getElementById('loginButton');
 try {
  const response=await fetch('/api/v1/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:form.username.value,password:form.password.value})});
  const payload=await response.json();
  if(!response.ok)throw new Error(payload.error?.message||'登录失败');
  location.assign('/');
 }catch(problem){error.textContent=problem.message;error.style.display='block';error.classList.remove('hidden');button.disabled=false;button.textContent='登录';}
});
