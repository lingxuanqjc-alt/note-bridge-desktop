"""Keep missing renderer styles blocking while exposing only bounded diagnostic counts."""
import json
import subprocess

from test_oppo_native_run import ROOT, helper, report


def test_style_diagnostics_explain_missing_marks_without_accepting_plain_text():
    script = r"""
const assert=require('node:assert/strict');
const {inspectEditor}=require('./scripts/oppo-matrix-browser.cjs');
const marks=['bold','italic','underline','strike','highlight','code','link'];
const scope={fixtureId:'synthetic-id',group_id:'synthetic-group',group_name:'',title:'synthetic-title',
 expectedText:'AB',expectedRuns:[{text:'A',bold:true,italic:true,underline:true,strike:true,highlight:true,code:true,
 link:'https://example.invalid/expected'},{text:'B'}],headings:[],todos:[],lists:[],quotes:[],codes:[],tables:[],dividers:0,image_specs:[]};
const normal={fontWeight:'400',fontStyle:'normal',textDecorationLine:'none',backgroundColor:'transparent',fontFamily:'sans-serif'};
const rich={fontWeight:'700',fontStyle:'italic',textDecorationLine:'underline line-through',backgroundColor:'rgb(255, 255, 0)',fontFamily:'monospace'};
const titleText={textContent:scope.title};
const title={tagName:'DIV',innerText:scope.title,contains:n=>n===titleText};
titleText.parentElement=title;
const node={isConnected:true,parentElement:null,firstElementChild:title,getBoundingClientRect:()=>({width:800,height:600}),querySelectorAll:()=>[]};
title.parentElement=node;
const richElement={tagName:'A',parentElement:node,closest:()=>null,getAttribute:()=>scope.expectedRuns[0].link};
const plainElement={tagName:'SPAN',parentElement:node,closest:()=>null};
const detail={recordId:scope.fixtureId,groupGuid:scope.group_id,rawTitle:scope.title,status:0};
const parent={currentNote:{...detail},$refs:{},$parent:null};
const owner={noteDetail:{...detail},groupName:'',$el:{contains:n=>n===node},$parent:parent,
 editor:{view:{dom:node},state:{doc:{firstChild:{type:{name:'paragraph'},attrs:{},nodeSize:20,textContent:scope.title},content:{size:24},textBetween:()=>scope.expectedText}}}};
parent.$refs.richEditorVueRef=owner;node.parentElement={__vue__:owner,parentElement:null};
global.document={querySelectorAll:()=>[node],createTreeWalker:()=>{let index=0;return {nextNode:()=>
 [titleText,{parentElement:richElement,textContent:'A'},{parentElement:plainElement,textContent:'B'},null][index++]}}};
global.NodeFilter={SHOW_TEXT:4};let applied={...rich};
global.getComputedStyle=element=>element===richElement?applied:normal;
(async()=>{
 const good=await inspectEditor(scope);assert.equal(good.checks.styles,true);
 assert.equal(good.diagnostics.styleTextAligned,true);
 assert.equal(good.diagnostics.styleExpectedCharacters,2);assert.equal(good.diagnostics.styleRenderedCharacters,2);
 for(const key of marks){assert.equal(good.diagnostics.styleExpectedCounts[key],1);assert.equal(good.diagnostics.styleMatchedCounts[key],1);}
 for(const key of marks){
  applied={...rich};richElement.getAttribute=()=>scope.expectedRuns[0].link;
  if(key==='bold')applied.fontWeight='400';
  if(key==='italic')applied.fontStyle='normal'; // Official EM projection must not count as visible italic.
  if(key==='underline')applied.textDecorationLine='line-through';
  if(key==='strike')applied.textDecorationLine='underline';
  if(key==='highlight')applied.backgroundColor='transparent';
  if(key==='code')applied.fontFamily='sans-serif';
  if(key==='link')richElement.getAttribute=()=> 'https://example.invalid/different';
  const failed=await inspectEditor(scope);assert.equal(failed.checks.renderedText,true);
  assert.equal(failed.checks.styles,false,key+' must remain a failure, not an inferred downgrade');
  assert.equal(failed.diagnostics.styleChecks[key],false);assert.equal(failed.diagnostics.styleMatchedCounts[key],0);
  assert.equal(failed.diagnostics.styleExpectedCounts[key],1);
 }
 applied={...rich};richElement.getAttribute=()=>scope.expectedRuns[0].link;
 scope.expectedRuns=[{text:'BA'}];
 const misplaced=await inspectEditor(scope);assert.equal(misplaced.checks.styles,false);
 assert.equal(misplaced.diagnostics.styleTextAligned,false,'Count equality cannot prove ordered style alignment');
 const safe=JSON.stringify(misplaced.diagnostics);
 for(const secret of ['synthetic-id','synthetic-title','example.invalid','AB','BA'])assert(!safe.includes(secret));
})().catch(error=>{console.error(error);process.exitCode=1});
"""
    result = subprocess.run(['node', '-e', script], cwd=ROOT, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_python_style_diagnostics_reject_text_unknown_keys_and_non_count_values():
    raw = report()
    raw['items'][0]['diagnostics'] = {
        'styleTextAligned': True, 'styleExpectedCharacters': 12, 'styleRenderedCharacters': True,
        'styleExpectedCounts': {'italic': 4, 'code': -1, 'link': 'synthetic-secret', 'unknown': 'synthetic-secret'},
        'styleMatchedCounts': {'italic': 0, 'code': 1000001, 'link': False},
        'styleChecks': {'italic': False, 'bold': 'synthetic-secret', 'link': True, 'private': 'synthetic-secret'},
        'rawText': 'synthetic-secret', 'href': 'synthetic-secret'}
    diagnostic = helper._report(raw)['items'][0]['diagnostics']
    assert diagnostic['styleTextAligned'] is True
    assert diagnostic['styleExpectedCharacters'] == 12 and diagnostic['styleRenderedCharacters'] is None
    assert diagnostic['styleExpectedCounts'] == {'italic': 4, 'code': None, 'link': None}
    assert diagnostic['styleMatchedCounts'] == {'italic': 0, 'code': None, 'link': None}
    assert diagnostic['styleChecks'] == {'bold': None, 'italic': False, 'link': True}
    assert 'synthetic-secret' not in json.dumps(diagnostic)


def test_official_gradient_requires_visible_line_exact_model_mark_and_text_position():
    script = r"""
const assert=require('node:assert/strict');
const {inspectEditor}=require('./scripts/oppo-matrix-browser.cjs');
const scope={fixtureId:'synthetic-id',group_id:'synthetic-group',group_name:'',title:'Title',
 expectedText:'AB',expectedRuns:[{text:'A',underline:true},{text:'B'}],headings:[],todos:[],lists:[],
 quotes:[],codes:[],tables:[],dividers:0,image_specs:[]};
const normal={fontWeight:'400',fontStyle:'normal',textDecorationLine:'none',backgroundColor:'transparent',
 fontFamily:'sans-serif',visibility:'visible',display:'inline',opacity:'1',backgroundImage:'none',
 backgroundSize:'auto',backgroundRepeat:'repeat',backgroundPosition:'0% 0%',paddingBottom:'0px'};
const official={...normal,backgroundImage:'linear-gradient(90deg, rgb(25, 25, 25), rgb(25, 25, 25))',
 backgroundSize:'100% 1.3px',backgroundRepeat:'no-repeat',backgroundPosition:'0% 100%',paddingBottom:'3px'};
const box=()=>({width:100,height:20});
const title={tagName:'DIV',innerText:'Title',contains:()=>false};
const node={isConnected:true,parentElement:null,firstElementChild:title,getBoundingClientRect:box,querySelectorAll:()=>[]};
const under={tagName:'U',isConnected:true,getBoundingClientRect:box,parentElement:node,closest:()=>null};
const plain={tagName:'SPAN',parentElement:node,closest:()=>null};
const first={textContent:'A',parentElement:under},second={textContent:'B',parentElement:plain};
const detail={recordId:scope.fixtureId,groupGuid:scope.group_id,rawTitle:scope.title,status:0};
const parent={currentNote:{...detail},$refs:{},$parent:null};
let modelMark=true,positionMismatch=false;
const doc={firstChild:{type:{name:'paragraph'},attrs:{},nodeSize:7,textContent:scope.title},content:{size:11},
 textBetween:(from,to)=>to-from===1?(from===7?'A':'B'):'AB',
 nodesBetween:(from,to,callback)=>{
  callback({isText:true,nodeSize:1,marks:modelMark?[{type:{name:'underline'},attrs:{type:'solid'}}]:[]},7);
  callback({isText:true,nodeSize:1,marks:[]},8);
 }};
const owner={noteDetail:{...detail},groupName:'',$el:{contains:n=>n===node},$parent:parent,
 editor:{view:{dom:node,posAtDOM:text=>positionMismatch?8:text===first?7:8},state:{doc}}};
parent.$refs.richEditorVueRef=owner;node.parentElement={__vue__:owner,parentElement:null};
global.document={querySelectorAll:()=>[node],createTreeWalker:()=>{let index=0;return {nextNode:()=>[first,second,null][index++]}}};
global.NodeFilter={SHOW_TEXT:4};let applied={...official},ancestorStyle={...normal};
global.getComputedStyle=element=>element===under?applied:ancestorStyle;
(async()=>{
 for(const position of ['0% 100%','100% 100%']){
  applied={...official,backgroundPosition:position};
  assert.equal((await inspectEditor(scope)).checks.styles,true,'Official LTR/RTL line remains visible despite text-decoration:none');
 }
 for(const changed of [
  {backgroundImage:'none',backgroundColor:'rgb(255, 255, 0)'},
  {backgroundImage:'linear-gradient(90deg, rgba(25, 25, 25, 0), rgba(25, 25, 25, 0))'},
  {backgroundImage:'linear-gradient(90deg, rgb(25, 25, 25), rgb(60, 60, 60))'},
  {backgroundSize:'100% 0px'},{backgroundSize:'100% 20px'},{backgroundRepeat:'repeat'},
  {backgroundPosition:'0% 0%'},{paddingBottom:'0px'},{visibility:'hidden'},{opacity:'0'},{display:'none'}
 ]){
  applied={...official,...changed};
  const result=await inspectEditor(scope);
  assert.equal(result.checks.renderedText,true);
  assert.equal(result.checks.styles,false,'A colored background or absent/invisible official line cannot prove underline');
 }
 applied={...official};under.tagName='SPAN';
 assert.equal((await inspectEditor(scope)).checks.styles,false,'The official U element is required');under.tagName='U';
 modelMark=false;
 assert.equal((await inspectEditor(scope)).checks.styles,false,'DOM styling cannot invent a missing model underline');modelMark=true;
 ancestorStyle={...normal,opacity:'0'};
 assert.equal((await inspectEditor(scope)).checks.styles,false,'An invisible ancestor hides the gradient too');ancestorStyle={...normal};
 positionMismatch=true;
 assert.equal((await inspectEditor(scope)).checks.styles,false,'A different model range must not prove this DOM text');positionMismatch=false;
 first.parentElement=plain;second.parentElement=under;
 assert.equal((await inspectEditor(scope)).checks.styles,false,'Another U elsewhere cannot satisfy the expected position');
 first.parentElement=under;second.parentElement=plain;
 scope.expectedRuns=[{text:'A'},{text:'B',underline:true}];
 const misplaced=await inspectEditor(scope);
 assert.equal(misplaced.diagnostics.styleTextAligned,true);
 assert.equal(misplaced.checks.styles,false,'Identical text and counts with a displaced mark are still a failure');
})().catch(error=>{console.error(error);process.exitCode=1});
"""
    result = subprocess.run(['node', '-e', script], cwd=ROOT, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
