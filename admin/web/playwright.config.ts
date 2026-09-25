import {defineConfig} from '@playwright/test'
export default defineConfig({testDir:'./e2e',workers:1,timeout:120000,use:{baseURL:process.env.ADMIN_TEST_URL||'https://localhost:18443',ignoreHTTPSErrors:true,channel:'chrome',headless:true,trace:'off',screenshot:'off'},reporter:'list'})
