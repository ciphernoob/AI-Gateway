import {test,expect} from '@playwright/test'
import type {Page} from '@playwright/test'
import {readFileSync} from 'node:fs'
import {resolve} from 'node:path'
const root=resolve(process.cwd(),'../..')
const password=readFileSync(resolve(root,'.admin/secrets/ADMIN_PASSWORD'),'utf8').trim()
const environment=Object.fromEntries(readFileSync(resolve(root,'.env'),'utf8').split(/\r?\n/).filter(x=>x&&!x.startsWith('#')).map(x=>[x.slice(0,x.indexOf('=')),x.slice(x.indexOf('=')+1)]))
const gateway=process.env.GATEWAY_TEST_URL||'http://localhost:18081'
async function login(page:Page){await page.goto('/');await page.getByLabel('密码',{exact:true}).fill(password);await page.getByRole('button',{name:'登录控制台'}).click();await expect(page.getByRole('heading',{name:'服务概览'})).toBeVisible()}
async function admin(page:Page,path:string,method='GET',body?:unknown){return page.evaluate(async({path,method,body})=>{const session=await (await fetch('/admin/api/v1/session')).json();const r=await fetch('/admin/api/v1'+path,{method,headers:{'Content-Type':'application/json','X-CSRF-Token':session.csrf},body:body===undefined?undefined:JSON.stringify(body)});return {status:r.status,body:await r.json()}},{path,method,body})}
test('浏览器配置模型、用户、发布、调用与审计',async({page,request})=>{
 await login(page)
 const suffix=Date.now().toString(36),provider='e2e_'+suffix,model='route_'+suffix,user='user_'+suffix
 await page.getByRole('link',{name:'后端密钥'}).click();await page.getByRole('button',{name:'新增后端密钥'}).click();await page.getByLabel('名称',{exact:true}).fill(provider);await page.getByLabel('后端地址').fill('http://mock-b:8000');await page.getByLabel('API Key',{exact:true}).fill(environment.PROVIDER_KEY_B);await page.getByRole('button',{name:'保存草稿',exact:true}).click();await expect(page.getByRole('dialog')).toBeHidden()
 await page.getByRole('link',{name:'模型服务'}).click();await page.getByRole('button',{name:'新增模型入口'}).click();await page.getByLabel('名称',{exact:true}).fill(model);await page.getByLabel('模型预算').fill('100');await page.getByRole('dialog').locator('.el-select__wrapper').first().click();await page.getByRole('option',{name:provider,exact:true}).click();await page.getByLabel('真实模型名').fill('mock-model-b');await page.getByLabel('输入价格').fill('1');await page.getByLabel('输出价格').fill('2');await page.getByRole('button',{name:'保存草稿',exact:true}).click();await expect(page.getByRole('dialog')).toBeHidden()
 await page.getByRole('link',{name:'用户管理'}).click();await page.getByRole('button',{name:'新增用户'}).click();await page.getByLabel('名称',{exact:true}).fill(user);await page.getByRole('button',{name:'保存草稿',exact:true}).click();await expect(page.getByRole('dialog')).toBeHidden()
 await page.getByRole('button',{name:'签发 Gateway Key',exact:true}).click();await page.getByRole('dialog').locator('.el-select__wrapper').first().click();await page.getByRole('option',{name:user,exact:true}).click();await page.getByRole('button',{name:'保存草稿',exact:true}).click();await expect(page.getByText('请保存新 Gateway Key',{exact:true})).toBeVisible();const key=(await page.locator('pre.key-once').innerText()).trim();await page.getByRole('dialog',{name:'请保存新 Gateway Key'}).getByRole('button',{name:'Close this dialog'}).click()
 await page.getByRole('link',{name:'配置发布'}).click();await page.getByRole('button',{name:'校验草稿'}).click();await expect(page.getByText('配置校验通过',{exact:true})).toBeVisible();await page.getByRole('button',{name:'预览并发布'}).click();await expect(page.getByRole('dialog')).toBeVisible();expect(await page.getByRole('dialog').innerText()).not.toContain(environment.PROVIDER_KEY_B);await page.getByRole('button',{name:'确认发布',exact:true}).click();await expect(page.getByRole('dialog')).toBeHidden({timeout:60000})
 const headers={Authorization:'Bearer '+key};const payload={messages:[{role:'user',content:'admin browser acceptance'}],max_tokens:16}
 const response=await request.post(gateway+'/models/'+model+'/v1/chat/completions',{headers,data:payload});expect(response.status()).toBe(200);const rid=response.headers()['x-request-id'];expect((await response.json()).model).toBe('mock-model-b')
 const stream=await request.post(gateway+'/models/'+model+'/v1/chat/completions',{headers,data:{...payload,stream:true}});expect(stream.status()).toBe(200);expect(await stream.text()).toContain('[DONE]')
 expect((await request.post(gateway+'/models/'+model+'/v1/chat/completions',{headers,data:{...payload,model:'balanced'}})).status()).toBe(400)
 expect((await request.post(gateway+'/models/missing/v1/chat/completions',{headers,data:payload})).status()).toBe(404)
 await expect.poll(async()=>{const r=await admin(page,'/usage?user_id='+user);return r.body.data?.[0]?.daily.total_tokens||0}).toBeGreaterThan(0)
 await page.getByRole('link',{name:'内容审计'}).click();await page.getByPlaceholder('请求编号').fill(rid);await page.getByRole('button',{name:'查询',exact:true}).click();await expect(page.getByRole('button',{name:'查看',exact:true})).toHaveCount(1);await page.getByRole('button',{name:'查看',exact:true}).click();await page.getByRole('button',{name:'读取请求与回复内容'}).click();await expect(page.getByText('admin browser acceptance',{exact:false}).first()).toBeVisible();await page.getByRole('button',{name:'Close this dialog'}).click()
 const draft=(await admin(page,'/draft')).body;expect(JSON.stringify(draft)).not.toContain(key);expect(JSON.stringify(draft)).not.toContain(environment.PROVIDER_KEY_B)
 const conflict=await admin(page,'/users/'+user,'PUT',{revision:draft.revision-1,value:{disabled:true}});expect(conflict.status).toBe(409)
 const disable=await admin(page,'/users/'+user,'PUT',{revision:draft.revision,value:{disabled:true,daily_token_limit:0,monthly_token_limit:0}});expect(disable.status).toBe(200);expect((await admin(page,'/publish','POST',{revision:disable.body.revision})).status).toBe(200)
 expect((await request.post(gateway+'/models/'+model+'/v1/chat/completions',{headers,data:payload})).status()).toBe(401)
 await page.getByRole('link',{name:'运行日志'}).click();await page.getByPlaceholder('请求编号').fill(rid);await page.getByRole('button',{name:'查询',exact:true}).click();await expect(page.getByRole('cell',{name:'attempt.finished',exact:true})).toBeVisible()
 await page.getByRole('link',{name:'模型服务'}).click();await page.screenshot({path:resolve(root,'test-results/admin-models.png'),fullPage:true})
})
test('HTTPS、管理权限与失败发布不影响运行配置',async({page,request})=>{
 expect((await request.get('/admin/api/v1/draft')).status()).toBe(401)
 expect((await request.get('/admin/api/v1/draft',{headers:{Authorization:'Bearer '+environment.GATEWAY_KEY_USER1}})).status()).toBe(401)
 await login(page);const noCSRF=await page.evaluate(async()=> (await fetch('/admin/api/v1/publish',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})).status);expect(noCSRF).toBe(403)
 const before=(await admin(page,'/overview')).body.config_revision;const draft=(await admin(page,'/draft')).body
 const bad=await admin(page,'/models/invalid_candidate','PUT',{revision:draft.revision,value:{budget_limit:1000000,candidates:[{provider:'not_found',model:'x',context_tokens:4096,max_output_tokens:1024,n_max:1,input_rate:1,output_rate:1,price_version:'v1',capabilities:[],supports_stream_usage:false}]}})
 expect(bad.status).toBe(200);expect((await admin(page,'/publish','POST',{revision:bad.body.revision})).status).toBe(422);expect((await admin(page,'/overview')).body.config_revision).toBe(before)
 expect((await admin(page,'/models/invalid_candidate','DELETE',{revision:bad.body.revision,value:{}})).status).toBe(200)
 await page.getByRole('button',{name:'退出登录'}).click();await expect(page.getByRole('heading',{name:'管理员登录'})).toBeVisible()
})





