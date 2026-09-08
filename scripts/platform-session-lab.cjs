/* Keep user-operated official sessions alive for successive tests. Nothing saves cookies to disk. */
const fs=require('node:fs');
const path=require('node:path');
const readline=require('node:readline');
const {spawn}=require('node:child_process');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE_PATH||'playwright');
const root=path.resolve(__dirname,'..');
const specs={xiaomi:['https://i.mi.com/note/h5',['mi.com','xiaomi.com']],oppo:['https://cloud.oppo.com/',['oppo.com','heytap.com','heytapmobi.com']],vivo:['https://yun.vivo.com.cn/',['vivo.com','vivo.com.cn']],huawei:['https://cloud.huawei.com/home',['huawei.com','hicloud.com']],honor:['https://cloud.honor.com/',['honor.com','hihonor.com']],meizu:['https://cloud.flyme.cn/browser/main.jsp',['flyme.cn','meizu.com']],wps:['https://note.wps.cn/',['wps.cn','wps.com','kdocs.cn']]};
function shape(value,depth=0){
 if(value===null)return 'null';if(depth>7)return Array.isArray(value)?'array':'object';
 if(Array.isArray(value))return {type:'array',samples:[...new Set(value.slice(0,3).map(v=>JSON.stringify(shape(v,depth+1))))].map(JSON.parse)};
 if(typeof value==='object')return Object.fromEntries(Object.entries(value).slice(0,80).map(([k,v])=>[/^[a-zA-Z_][a-zA-Z_]{0,48}$/.test(k)?k:'<dynamic-key>',shape(v,depth+1)]));
 return typeof value;
}
function safePath(url){return url.pathname.split('/').map(p=>/^[a-zA-Z][a-zA-Z_-]{0,35}$/.test(p)||/^v\d$/.test(p)||!p?p:':id').join('/')}
function allowed(host,platform){return specs[platform][1].some(d=>host===d||host.endsWith('.'+d))}
let browser;const sessions=new Map();
async function open(platform){
 if(!specs[platform])throw Error('unsupported_platform');
 if(sessions.has(platform)){await sessions.get(platform).page.bringToFront();return}
 const context=await browser.newContext({viewport:{width:1200,height:850},locale:'zh-CN'});
 const records=[];const directory=path.join(root,'.private/protocol',platform);fs.mkdirSync(directory,{recursive:true});
 context.on('response',async response=>{
  try{
   const url=new URL(response.url());
   if(!allowed(url.hostname,platform)||!/(?:note|notesvr|yunnote)/i.test(url.pathname)||!/json/i.test(response.headers()['content-type']||''))return;
   if(Number(response.headers()['content-length']||0)>4000000)return;
   const data=await response.json();const req=response.request();let request=null;
   if(req.postData()){try{request=shape(JSON.parse(req.postData()))}catch{request=Object.fromEntries([...new URLSearchParams(req.postData()).keys()].map(k=>[/^[a-zA-Z_]{1,48}$/.test(k)?k:'<dynamic-key>','string']))}}
   const item={method:req.method(),host:url.hostname,path:safePath(url),queryFields:[...url.searchParams.keys()].filter(k=>/^[a-zA-Z_]{1,48}$/.test(k)),httpStatus:response.status(),request,response:shape(data)};
   if(typeof data?.code==='number')item.responseCode=data.code;
   if(records.some(x=>JSON.stringify(x)===JSON.stringify(item)))return;
   records.push(item);fs.writeFileSync(path.join(directory,'schema.json'),JSON.stringify({platform,kind:'official-response-shapes-only',records},null,2));
   console.log(JSON.stringify({event:'shape',platform,method:item.method,path:item.path,status:item.httpStatus}));
  }catch{}
 });
 const page=await context.newPage();sessions.set(platform,{context,page});
 await page.goto(specs[platform][0],{waitUntil:'domcontentloaded',timeout:45000}).catch(e=>console.log(JSON.stringify({event:'navigation_pending',platform,reason:e.message.match(/net::[A-Z_]+|Timeout/)?.[0]||e.name})));
 console.log(JSON.stringify({event:'official_window_open',platform}));
}
async function probe(platform,action){
 if(!['probe','fetch'].includes(action)||!sessions.has(platform))throw Error('session_required');
 const {context}=sessions.get(platform);
 const cookies=(await context.cookies()).filter(c=>allowed(c.domain.replace(/^\./,''),platform));
 const result=await new Promise(resolve=>{
  const worker=spawn(path.join(root,'.venv/Scripts/python.exe'),[path.join(root,'scripts/probe-session.py')],{cwd:root,windowsHide:true,stdio:['pipe','pipe','pipe'],env:{...process.env,PYTHONUTF8:'1'}});
  let output='';worker.stdout.on('data',data=>{output+=data.toString()});
  // Raw worker stderr could include credentials in a library exception; it is deliberately not persisted.
  worker.stderr.resume();worker.on('error',()=>resolve({status:'failed',code:'worker_start'}));
  worker.on('close',()=>{try{resolve(JSON.parse(output))}catch{resolve({status:'failed',code:'worker_result'})}});
  worker.stdin.end(JSON.stringify({platform,action,cookies}));
 });
 cookies.length=0;console.log(JSON.stringify({event:'test_result',...result}));
}
async function command(input){
 const cmd=JSON.parse(input);
 if(cmd.action==='stop'){await browser.close();process.exit(0)}
 if(cmd.action==='open')return open(cmd.platform);
 if(cmd.action==='probe'||cmd.action==='fetch')return probe(cmd.platform,cmd.action);
 if(cmd.action==='status'){console.log(JSON.stringify({event:'session_status',open:[...sessions.keys()]}));return}
 throw Error('unsupported_command');
}
(async()=>{
 const args=process.argv.slice(2);
 if(args.length&&(args.length!==2||args[0]!=='--platform'||!Object.hasOwn(specs,args[1])))throw Error('invalid_arguments');
 const platforms=args.length?[args[1]]:Object.keys(specs);
 browser=await chromium.launch({headless:false,executablePath:process.env.CHROME_PATH||'C:/Program Files/Google/Chrome/Application/chrome.exe'});
 browser.on('disconnected',()=>process.exit(0));
 let queue=Promise.resolve();readline.createInterface({input:process.stdin}).on('line',line=>{queue=queue.then(()=>command(line)).catch(e=>console.log(JSON.stringify({event:'command_error',code:e.message.match(/^[a-z_]+$/)?.[0]||e.name})))});
 // Separate browser contexts also isolate old-Honor/Huawei accounts when their domains overlap.
 await Promise.all(platforms.map(open));
 console.log(JSON.stringify({event:'ready',platforms,credentials_saved:false}));
})().catch(async e=>{console.log(JSON.stringify({event:'start_failed',code:e.message==='invalid_arguments'?'invalid_arguments':e.name}));if(browser)await browser.close();process.exit(1)});
