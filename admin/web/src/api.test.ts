// @vitest-environment jsdom
import {describe,it,expect,vi} from 'vitest'
import {mount} from '@vue/test-utils'
import {api,toMicro,fromMicro,setCSRF} from './api'
import {defineComponent} from 'vue'
import App from './App.vue'
import ElementPlus from 'element-plus'
describe('金额与认证',()=>{
 it('使用整数转换，不丢失最小金额',()=>{expect(toMicro('0.000001')).toBe(1);expect(toMicro('12.345678')).toBe(12345678);expect(fromMicro(12345678)).toBe('12.345678');expect(()=>toMicro('0.0000001')).toThrow();expect(()=>toMicro('-1')).toThrow()})
 it('每次写操作携带内存 CSRF',async()=>{const mock=vi.fn().mockResolvedValue({ok:true,json:async()=>({ok:true})});const store=vi.fn();vi.stubGlobal('localStorage',{setItem:store});vi.stubGlobal('fetch',mock);setCSRF('test-csrf');await api('/publish','POST',{revision:1});expect(mock.mock.calls[0][1].headers['X-CSRF-Token']).toBe('test-csrf');expect(store).not.toHaveBeenCalled()})
 it('登录页不显示管理内容',async()=>{vi.stubGlobal('fetch',vi.fn().mockResolvedValue({ok:false,status:401,json:async()=>({error:{code:'login_required'}})}));const wrapper=mount(App,{global:{plugins:[ElementPlus],stubs:{RouterView:defineComponent({template:'<div>私密管理内容</div>'}),RouterLink:true}}});await new Promise(r=>setTimeout(r,30));expect(wrapper.text()).toContain('管理员登录');expect(wrapper.text()).not.toContain('私密管理内容');wrapper.unmount()})
})
