/* Developer-only official-site inspection. Writes types/field names, never credentials or note values. */
const fs=require('node:fs');
const path=require('node:path');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE_PATH||'playwright');
const configs={oppo:['https://cloud.oppo.com/',['oppo.com','heytap.com','heytapmobi.com']],vivo:['https://yun.vivo.com.cn/',['vivo.com','vivo.com.cn']],huawei:['https://cloud.huawei.com/home',['huawei.com','hicloud.com']],honor:['https://cloud.honor.com/',['honor.com','hihonor.com']],meizu:['https://cloud.flyme.cn/browser/main.jsp',['flyme.cn','meizu.com']],wps:['https://note.wps.cn/',['wps.cn','wps.com','kdocs.cn']]};
const platform=process.argv[2];if(!configs[platform])throw Error('Choose a supported platform');
const directory=path.resolve(__dirname,'../.private/protocol',platform);fs.mkdirSync(directory,{recursive:true});
function schema(value,depth=0){
  if(value===null)return 'null';if(depth>7)return Array.isArray(value)?'array':'object';
  if(Array.isArray(value))return {type:'array',samples:[...new Set(value.slice(0,3).map(v=>JSON.stringify(schema(v,depth+1))))].map(JSON.parse)};
  if(typeof value==='object')return Object.fromEntries(Object.entries(value).slice(0,80).map(([key,v])=>[/^[a-zA-Z_][a-zA-Z_]{0,48}$/.test(key)?key:'<dynamic-key>',schema(v,depth+1)]));
  return typeof value;
}
function safePath(url){return url.pathname.split('/').map(piece=>/^[a-zA-Z][a-zA-Z_-]{0,35}$/.test(piece)||/^v\d$/.test(piece)||!piece?piece:':id').join('/')}
const evidence=[];
let browser;
(async()=>{
  browser=await chromium.launch({headless:false,executablePath:process.env.CHROME_PATH||'C:/Program Files/Google/Chrome/Application/chrome.exe'});
  const context=await browser.newContext({viewport:{width:1200,height:850},locale:'zh-CN'});
  // No persistent context, storage-state export, passwords, cookies, request headers or raw bodies are saved.
  context.on('response',async response=>{
    try{
      const url=new URL(response.url());const allowed=configs[platform][1].some(d=>url.hostname===d||url.hostname.endsWith('.'+d));
      if(!allowed||!/(?:note|yunnote|notesvr)/i.test(url.pathname)||!/json/i.test(response.headers()['content-type']||''))return;
      const request=response.request();let body=response.headers()['content-length'];if(body&&Number(body)>4000000)return;
      const data=await response.json();
      let requestShape=null;const raw=request.postData();
      if(raw){try{requestShape=schema(JSON.parse(raw))}catch{requestShape=Object.fromEntries([...new URLSearchParams(raw).keys()].map(k=>[/^[a-zA-Z_]{1,48}$/.test(k)?k:'<dynamic-key>','string']))}}
      const item={method:request.method(),host:url.hostname,path:safePath(url),queryFields:[...url.searchParams.keys()].filter(k=>/^[a-zA-Z_]{1,48}$/.test(k)),httpStatus:response.status(),request:requestShape,response:schema(data)};
      if(typeof data?.code==='number')item.responseCode=data.code;
      const serialized=JSON.stringify(item);if(evidence.some(x=>JSON.stringify(x)===serialized))return;
      evidence.push(item);fs.writeFileSync(path.join(directory,'schema.json'),JSON.stringify({platform,kind:'official-response-shapes-only',records:evidence},null,2));
      console.log('Captured shape: '+request.method()+' '+safePath(url));
    }catch{ /* Network cancellation during navigation adds no usable protocol evidence. */ }
  });
  const page=await context.newPage();await page.goto(configs[platform][0],{waitUntil:'domcontentloaded',timeout:45000});
  console.log('Official login page ready: '+platform);
  await new Promise(resolve=>browser.on('disconnected',resolve));
})().catch(async error=>{console.error('Protocol review stopped: '+error.name+' '+(error.message.match(/net::[A-Z_]+|Timeout/)?.[0]||'initialization'));if(browser)await browser.close();process.exitCode=1});
