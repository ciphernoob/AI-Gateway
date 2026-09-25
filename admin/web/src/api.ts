let csrf=''
export function setCSRF(value:string){csrf=value}
const messages:Record<string,string>={invalid_credentials:'账号或密码不正确',login_rate_limited:'登录失败次数过多，请稍后重试',revision_conflict:'草稿已被修改，请刷新后重新编辑',invalid_configuration:'配置校验未通过，请检查模型、预算、密钥及关联关系',publication_in_progress:'已有配置正在发布',publication_recovery_required:'发布状态需要恢复，请检查控制代理与网关健康状态',redis_unavailable:'Redis 不可用，用量暂不可查询',audit_unavailable:'审计服务不可用或查询资源不可用',model_path_conflict:'请求模型与 URL 不一致',failed:'发布失败，已恢复原版本',recovery_required:'发布和恢复未完成，请检查网关状态',secret_required:'请输入后端 API Key',csrf_invalid:'会话校验失败，请重新登录'}
export async function api(path:string,method='GET',body?:unknown):Promise<any>{
 const res=await fetch('/admin/api/v1'+path,{method,credentials:'same-origin',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:body===undefined?undefined:JSON.stringify(body)})
 const result=await res.json();if(!res.ok){if(res.status===401&&path!='/login')window.dispatchEvent(new Event('admin-auth-expired'));const code=result.error?.code||'request_failed';throw new Error(messages[code]||code)}return result
}
export function toMicro(value:string):number {if(!/^\d+(\.\d{1,6})?$/.test(value))throw new Error('金额需为非负数，最多保留 6 位小数');const [a,b='']=value.split('.');const n=BigInt(a)*1000000n+BigInt(b.padEnd(6,'0'));if(n>9007199254740991n)throw new Error('金额超出范围');return Number(n)}
export function fromMicro(value:number):string {const n=BigInt(value||0);return `${n/1000000n}.${String(n%1000000n).padStart(6,'0')}`.replace(/\.?0+$/,'')||'0'}
export function formatTime(value:number){return value?new Date(value*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}):'—'}
