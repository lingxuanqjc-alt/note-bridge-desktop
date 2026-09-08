"""Official NodeViews must be bound to their model, not inferred from appearance."""
import subprocess
from pathlib import Path


def test_task_divider_and_image_nodeviews_retain_identity_and_semantic_checks():
    script = r"""
const assert=require('node:assert/strict');
const {inspectEditor}=require('./scripts/oppo-matrix-browser.cjs');
const scope={fixtureId:'selected',group_id:'group',group_name:'synthetic',title:'title',expectedText:'todo',
 expectedRuns:[{text:'todo'}],headings:[],todos:[{text:'todo',checked:true}],lists:[],quotes:[],codes:[],tables:[],dividers:1,
 image_specs:[{id:'attachment',width:20,height:20}]};
const visible={isConnected:true,getBoundingClientRect:()=>({width:20,height:20})};
const title={tagName:'H1',innerText:'title',contains:()=>false};
const input={checked:true},todoModel={type:{name:'taskItem'},attrs:{checked:true},textContent:'todo'};
const todo={innerText:'todo',getAttribute:()=> 'true',querySelector:()=>input,closest:()=>({}),parentElement:{tagName:'UL'}};
const divider={...visible,contains:el=>el===divider,querySelector:selector=>selector==='.hr-style-solid'?{}:null};
const dividerModel={type:{name:'divider'},attrs:{lineType:'solid'}};
divider.__vue__={$parent:{$props:{node:dividerModel},$el:divider}};
const wrapper={contains:el=>el===image},image={...visible,complete:true,naturalWidth:20,naturalHeight:20,decode:async()=>{},
 closest:()=>wrapper,parentElement:wrapper};
const imageModel={type:{name:'image'},attrs:{attachId:'attachment'}};
wrapper.__vue__={$parent:{$props:{node:imageModel},$el:wrapper}};
const node={...visible,firstElementChild:title,querySelectorAll:selector=>({
 'h1,h2,h3,h4,h5,h6':[title],'ul[data-type="taskList"] li[data-checked]':[todo],li:[todo],'.divider':[divider],img:[image]
 }[selector]||[])};
wrapper.parentElement=node;divider.parentElement=node;
const detail={recordId:'selected',groupGuid:'group',rawTitle:'title',status:0},parent={currentNote:detail,$refs:{}};
const doc={firstChild:{type:{name:'heading'},attrs:{level:1},nodeSize:7,textContent:'title'},content:{size:13},
 textBetween:()=> 'todo',descendants:callback=>callback(todoModel)};
const owner={noteDetail:detail,groupName:'synthetic',$el:{contains:el=>el===node},$parent:parent,editor:{view:{dom:node},state:{doc}}};
parent.$refs.richEditorVueRef=owner;node.parentElement={__vue__:owner,parentElement:null};
const span={tagName:'SPAN',parentElement:node,closest:()=>null};
global.document={querySelectorAll:()=>[node],createTreeWalker:()=>{let read=false;return{nextNode:()=>read?null:(read=true,{parentElement:span,textContent:'todo'})}}};
global.NodeFilter={SHOW_TEXT:4};global.getComputedStyle=()=>({fontWeight:'400',fontStyle:'normal',textDecorationLine:'none',backgroundColor:'transparent',fontFamily:'sans-serif'});
(async()=>{
 let result=await inspectEditor(scope);assert(Object.values(result.checks).every(Boolean));
 assert(result.images[0].identityMatched&&result.images[0].dimensionsMatched);
 input.checked=false;assert.equal((await inspectEditor(scope)).structures.todos,false,'Visual check state must agree with model and expected state.');
 input.checked=true;todoModel.attrs.checked=false;assert.equal((await inspectEditor(scope)).structures.todos,false);
 todoModel.attrs.checked=true;dividerModel.attrs.lineType='dashed';assert.equal((await inspectEditor(scope)).structures.dividers,false);
 dividerModel.attrs.lineType='solid';const old=divider.__vue__;divider.__vue__={};
 assert.equal((await inspectEditor(scope)).structures.dividers,false,'A decorative line without its divider model is insufficient.');
 divider.__vue__=old;imageModel.attrs.attachId='private-neighbour';assert.equal((await inspectEditor(scope)).checks.images,false);
 imageModel.attrs.attachId='attachment';wrapper.__vue__.$parent.$el={contains:()=>true};
 assert.equal((await inspectEditor(scope)).checks.images,false,'An outside component cannot lend its attachment identity.');
 wrapper.__vue__.$parent.$el=wrapper;scope.codes=['todo'];
 assert.equal((await inspectEditor(scope)).structures.codes,false,'Plain text must not silently satisfy an expected code block.');
 assert(!JSON.stringify(await inspectEditor(scope)).includes('attachment'));
})().catch(error=>{console.error(error);process.exitCode=1});
"""
    result = subprocess.run(['node', '-e', script], cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
