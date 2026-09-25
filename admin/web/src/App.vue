<script setup lang="ts">
import {onMounted,reactive,ref} from 'vue'
import {api,setCSRF} from './api'
const authenticated=ref(false),loading=ref(true),busy=ref(false),error=ref('')
const login=reactive({username:'admin',password:''})
const pages=[['overview','概览'],['models','模型服务'],['providers','后端密钥'],['users','用户管理'],['usage','用户用量'],['audit','内容审计'],['logs','运行日志'],['publish','配置发布']]
window.addEventListener('admin-auth-expired',()=>{authenticated.value=false;setCSRF('')})
async function signIn(){busy.value=true;error.value='';try{const r=await api('/login','POST',login);setCSRF(r.csrf);login.password='';authenticated.value=true}catch(e){error.value=(e as Error).message}finally{busy.value=false}}
async function logout(){await api('/logout','POST',{});authenticated.value=false;setCSRF('')}
onMounted(async()=>{try{const r=await api('/session');setCSRF(r.csrf);authenticated.value=true}catch{}finally{loading.value=false}})
</script>
<template>
 <div v-if="loading" class="boot">正在连接管理服务…</div>
 <main v-else-if="!authenticated" class="login-shell">
  <div class="login-intro"><span class="eyebrow">OPENRESTY AI GATEWAY</span><h1>统一调用。<br>清晰管理。</h1><p>模型、密钥与用量，在一个入口掌握。</p></div>
  <el-card class="login-card"><h2>管理员登录</h2><p class="muted">使用部署时设置的管理员账号</p><el-form @submit.prevent="signIn" label-position="top">
   <el-form-item label="账号"><el-input v-model="login.username" autocomplete="username" aria-label="账号" /></el-form-item>
   <el-form-item label="密码"><el-input v-model="login.password" type="password" show-password autocomplete="current-password" aria-label="密码" /></el-form-item>
   <el-alert v-if="error" :title="error" type="error" :closable="false"/><el-button native-type="submit" type="primary" :loading="busy" class="login-button">登录控制台</el-button>
  </el-form></el-card>
 </main>
 <div v-else class="shell"><aside class="sidebar"><div class="brand"><span class="brand-mark">G</span><div>AI Gateway<small>管理控制台</small></div></div><nav><router-link v-for="(p,i) in pages" :key="p[0]" :to="'/'+p[0]"><span class="nav-index">0{{i+1}}</span>{{p[1]}}</router-link></nav><div class="sidebar-footer"><span class="status-dot"></span> 单实例管理<el-button text @click="logout">退出登录</el-button></div></aside><main class="workspace"><router-view /></main></div>
</template>
