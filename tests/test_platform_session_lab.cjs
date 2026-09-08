const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../scripts/platform-session-lab.cjs'),'utf8');
const allPlatforms=['xiaomi','oppo','vivo','huawei','honor','meizu','wps'];

async function start(args,env={}){
 const observed={launches:[],contexts:0,urls:[],directories:[],events:[],exits:[],foreground:0};
 let receiveLine;
 const browser={on(){},close:async()=>{},newContext:async()=>{
  observed.contexts++;
  return {on(){},newPage:async()=>({
   goto:async url=>observed.urls.push(url),bringToFront:async()=>observed.foreground++
  })};
 }};
 const sandbox={__dirname:path.join(__dirname,'../scripts'),URL,URLSearchParams,
  console:{log:line=>observed.events.push(JSON.parse(line))},
  process:{argv:['node','platform-session-lab.cjs',...args],env,stdin:{},exit:code=>observed.exits.push(code)},
  require:name=>{
   if(name==='node:path')return path;
   if(name==='node:fs')return {mkdirSync:directory=>observed.directories.push(directory),
    writeFileSync(){assert.fail('Startup must not persist response bodies or credentials.');}};
   if(name==='node:readline')return {createInterface:()=>({on:(event,callback)=>{assert.equal(event,'line');receiveLine=callback;}})};
   if(name==='node:child_process')return {spawn(){assert.fail('Opening a login window must not start a cloud worker.');}};
   assert.equal(name,env.PLAYWRIGHT_MODULE_PATH||'playwright');
   return {chromium:{launch:async options=>{observed.launches.push(options);return browser;}}};
  }
 };
 await vm.runInNewContext(source,sandbox);
 return {...observed,command:async command=>{receiveLine(JSON.stringify(command));await new Promise(resolve=>setImmediate(resolve));}};
}

test('OPPO-only startup never navigates the other six platforms and retains the Edge override',async()=>{
 const edge='C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';
 const result=await start(['--platform','oppo'],{CHROME_PATH:edge,PLAYWRIGHT_MODULE_PATH:'synthetic-playwright'});
 assert.equal(result.launches.length,1);assert.equal(result.launches[0].executablePath,edge);
 assert.equal(result.contexts,1);assert.deepEqual(result.urls,['https://cloud.oppo.com/']);
 assert.deepEqual(result.directories.map(directory=>path.basename(directory)),['oppo']);
 assert.deepEqual(result.events.find(event=>event.event==='ready').platforms,['oppo']);
 assert.equal(result.events.find(event=>event.event==='ready').credentials_saved,false);
 await result.command({action:'open',platform:'oppo'});
 await result.command({action:'status'});
 assert.deepEqual(result.events.at(-1),{event:'session_status',open:['oppo']});
 assert.deepEqual(result.urls,['https://cloud.oppo.com/'],'Reopening must retain the existing login context.');
});

test('omitting the optional platform keeps the existing seven-platform startup',async()=>{
 const result=await start([]);
 assert.equal(result.contexts,7);assert.equal(result.urls.length,7);
 assert.deepEqual(result.events.find(event=>event.event==='ready').platforms,allPlatforms);
 assert.equal(result.launches[0].executablePath,'C:/Program Files/Google/Chrome/Application/chrome.exe');
 assert.deepEqual(result.exits,[]);
});

test('every known single platform can be selected without changing command semantics',async()=>{
 for(const platform of allPlatforms){
  const result=await start(['--platform',platform]);
  assert.equal(result.contexts,1);assert.equal(result.urls.length,1);
  assert.deepEqual(result.events.find(event=>event.event==='ready').platforms,[platform]);
 }
});

test('invalid, missing, duplicate and unknown arguments fail before any browser or protocol directory opens',async()=>{
 for(const args of [['--platform'],['--platform',''],['--platform','unknown'],['--platform','__proto__'],
  ['--platform','toString'],['--platform','OPPO'],['oppo'],['--unknown','oppo'],['--platform=oppo'],
  ['--platform','oppo','--platform','oppo'],['--platform','oppo','--platform','vivo'],['--platform','oppo','extra']]){
  const result=await start(args);
  assert.equal(result.launches.length,0,JSON.stringify(args));assert.equal(result.contexts,0);
  assert.deepEqual(result.urls,[]);assert.deepEqual(result.directories,[]);
  assert.deepEqual(result.events,[{event:'start_failed',code:'invalid_arguments'}]);
  assert.deepEqual(result.exits,[1]);
 }
});
