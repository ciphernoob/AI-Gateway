import {createApp} from 'vue'
import {createRouter,createWebHistory} from 'vue-router'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import './style.css'
import App from './App.vue'
import Console from './Console.vue'
const router=createRouter({history:createWebHistory(),routes:[{path:'/',redirect:'/overview'},{path:'/:page',component:Console}]})
createApp(App).use(router).use(ElementPlus).mount('#app')
