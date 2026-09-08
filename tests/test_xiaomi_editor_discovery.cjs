const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync(require.resolve('../scripts/xiaomi-editor-discovery.cjs'),'utf8');
const sandbox={};
// Evaluate only the browser-independent, self-contained DOM callbacks. Never
// load Playwright, stdin handlers, saved official bundles or private fixtures.
vm.runInNewContext(source.slice(0,source.indexOf("const {chromium}=")),sandbox);
const {exactEditorProof,editorImageInventory}=sandbox;

function fixture(id='101'){
 const attrs={},dom={isConnected:true,matches:selector=>selector==='.pm-container .ProseMirror[contenteditable="true"]',
  getBoundingClientRect:()=>({width:700,height:500}),setAttribute:(key,value)=>attrs[key]=value,
  removeAttribute:key=>delete attrs[key]};
 const detail={contains:node=>node===dom,querySelectorAll:()=>attrs['data-note-bridge-editor']?[dom]:[]};
 const props={noteId:id,note:{get:key=>key==='id'?id:'private text must never leave the callback'},
  isLoading:false,historyOpen:false,locked:false,isTypeCommon:true};
 const widget={view:{dom},dataReceived:true};
 const handle={isReady:()=>true,getEditor:()=>widget,setData:()=>assert.fail('No editor mutation is permitted')};
 const hostRoot={},mapped={memoizedProps:props,return:hostRoot},host={stateNode:detail,return:mapped},forward={ref:{current:handle},return:host};
 hostRoot.stateNode={current:hostRoot};hostRoot.child=mapped;mapped.child=host;host.child=forward;
 detail.__reactFiber$synthetic=host;
 return {attrs,dom,detail,props,widget,handle,hostRoot,mapped,host,forward};
}

test('loaded exact note and its own committed ready editor are required, without mutating the widget',()=>{
 const f=fixture(),result=exactEditorProof([f.detail],'101');
 assert.equal(result.matched,1);assert.equal(f.attrs['data-note-bridge-editor'],'matched');
 assert.ok(Object.values(result.candidates[0]).every(value=>value===true));
 assert.ok(!JSON.stringify(result).includes('private text'));
});

test('a same-title editor for another ID cannot borrow the selected list row identity',()=>{
 for(const field of ['noteId','note']){
  const f=fixture();
  if(field==='noteId')f.props.noteId='202';else f.props.note={get:()=> '202'};
  assert.equal(exactEditorProof([f.detail],'101').matched,0);
  assert.equal(f.attrs['data-note-bridge-editor'],undefined);
 }
});

test('stale DOM fiber alternates do not substitute the previously selected note for current state',()=>{
 for(const currentId of ['101','202']){
  const f=fixture(currentId),oldRoot={stateNode:f.hostRoot.stateNode};
  f.detail.__reactFiber$synthetic={stateNode:f.detail,return:{memoizedProps:fixture('101').props,return:oldRoot}};
  assert.equal(exactEditorProof([f.detail],'101').matched,currentId==='101'?1:0);
 }
});

test('loading, history, locked or unsupported views never authorize samples from a retained editor',()=>{
 for(const change of [{isLoading:true},{historyOpen:true},{locked:true},{isTypeCommon:false}]){
  const f=fixture();Object.assign(f.props,change);
  assert.equal(exactEditorProof([f.detail],'101').matched,0);
 }
});

test('unready, unpopulated or foreign widget DOM cannot produce exact editor proof',()=>{
 for(const failure of ['ready','data','foreign','detached']){
  const f=fixture();
  if(failure==='ready')f.handle.isReady=()=>false;
  if(failure==='data')f.widget.dataReceived=false;
  if(failure==='foreign')f.widget.view.dom=fixture().dom;
  if(failure==='detached')f.dom.isConnected=false;
  assert.equal(exactEditorProof([f.detail],'101').matched,0);
 }
});

test('proof is recomputed and stale selection markers are removed before subsequent sampling',()=>{
 const f=fixture();assert.equal(exactEditorProof([f.detail],'101').matched,1);
 f.props.noteId='202';assert.equal(exactEditorProof([f.detail],'101').matched,0);
 assert.equal(f.attrs['data-note-bridge-editor'],undefined);
});

test('two live details remain ambiguous instead of merging their styles or image totals',()=>{
 const a=fixture(),b=fixture();
 assert.equal(exactEditorProof([a.detail,b.detail],'101').matched,2);
});

test('image inventory includes broken, unexpected-size, hidden and extra images, including zero-image cases',()=>{
 const images=[[640,240,true,1],[0,0,false,1],[80,40,true,1],[640,240,true,0]].map(([naturalWidth,naturalHeight,complete,width])=>({
  naturalWidth,naturalHeight,complete,getBoundingClientRect:()=>({width,height:width}),closest:()=>({})}));
 const result=editorImageInventory(images);
 assert.equal(result.length,4);assert.equal(result[1].complete,false);assert.equal(result[1].width,0);
 assert.equal(result[2].width,80);assert.equal(result[3].visible,false);
 assert.equal(editorImageInventory([images[1]]).length,1);
});
