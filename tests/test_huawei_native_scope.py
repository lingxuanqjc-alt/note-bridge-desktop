"""Native proof must bind the current editor, not a same-title neighbouring note."""
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from note_bridge.models import Block, NoteDocument, Span

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

import huawei_native_scope as scope_module  # noqa: E402


@pytest.mark.parametrize('selected', [
    {'manifest': 'wps-huawei-BD18-job.json', 'index': 1},
    {'manifest': 'wps-huawei-BD20-job.json', 'index': True},
    {'manifest': 'honor-huawei-BD19-job.json', 'index': 2},
    {'manifest': '../other.json', 'index': 0},
])
def test_scope_never_expands_to_rejected_or_unrelated_entries(tmp_path, selected):
    with pytest.raises(ValueError, match='scope_unverified'):
        scope_module.browser_target(selected, tmp_path, 'synthetic-target')


def test_wrong_account_stops_before_any_cache_access(tmp_path, monkeypatch):
    checkpoints = tmp_path / '.private/checkpoints'
    checkpoints.mkdir(parents=True)
    (checkpoints / 'wps-huawei-BD18-job.json').write_text(json.dumps({
        'kind': 'cloud-matrix-batch', 'target': 'huawei', 'armed': False,
        'source_policy': 'direct_seed_only', 'expected_account': 'different-account',
        'id': 'matrix-20260907-wps-huawei-BD18',
    }), 'utf8')
    monkeypatch.setattr(scope_module.sqlite3, 'connect', lambda *a, **kw: pytest.fail('Private cache was accessed'))
    with pytest.raises(ValueError, match='scope_unverified'):
        scope_module.browser_target({'manifest': 'wps-huawei-BD18-job.json', 'index': 0}, tmp_path, 'synthetic-target')


@pytest.mark.parametrize(('kind', 'first', 'candidate'), [
    ('paragraph', 'same title', True), ('heading', 'same title', False),
    ('paragraph', 'same title and more', False), ('paragraph', 'different title', False),
])
def test_title_projection_preserves_repeated_body_and_original_document(kind, first, candidate):
    note = NoteDocument(platform='huawei', account_id='synthetic', source_id='synthetic', title='same title',
                        blocks=[Block(kind=kind, spans=[Span(text=first)]),
                                Block(spans=[Span(text='same title', bold=True)]),
                                Block(spans=[Span(text='body')])])
    before = note.model_dump_json()
    result = scope_module.render_expectations(note)
    assert result['expectedText'] == note.plain_text
    assert ''.join(r['text'] for r in result['expectedRuns']) == first + 'same titlebody'
    assert ('editedTitleProjection' in result) is candidate
    if candidate:
        projection = result['editedTitleProjection']
        assert projection['expectedText'] == 'same title\nbody', 'Only the first exact title block moves to the title editor.'
        assert projection['expectedRuns'][0]['bold'] is True
        assert projection['titleRuns'] + projection['expectedRuns'] == result['expectedRuns']
        assert first + '\n' + projection['expectedText'] == result['expectedText']
    assert note.model_dump_json() == before, 'Rendering projection must not alter the cached note or its fingerprint.'


@pytest.mark.parametrize('batch', [False, True])
def test_discovery_uses_one_browser_and_retains_unverified_status(tmp_path, monkeypatch, batch):
    from session_discovery import discover

    private = tmp_path / '.private'
    (private / 'evidence').mkdir(parents=True)
    requests = [{'manifest': 'honor-huawei-BD19-job.json', 'index': n} for n in range(2 if batch else 1)]
    job = {'platform': 'huawei', 'operation': 'huawei_target_native',
           'fixture_scopes' if batch else 'fixture_scope': requests if batch else requests[0]}
    (private / 'lab-discovery.json').write_text(json.dumps(job), 'utf8')
    closed, calls = [], []
    monkeypatch.setattr('note_bridge.providers.factory.create_provider', lambda *a: SimpleNamespace(
        probe=lambda: 'synthetic-target', close=lambda: closed.append(True)))
    monkeypatch.setattr(scope_module, 'browser_target', lambda request, root, account: {
        'fixtureId': str(request['index']), 'scopeLabel': 'synthetic', 'target_fingerprint': 'synthetic',
        'evidence_sha256': {}})

    def run(command, **kwargs):
        assert closed == [True]
        assert command[-1].endswith('huawei-matrix-browser.cjs')
        assert kwargs['timeout'] is None, 'Node must reach its browser cleanup, including a multi-note check.'
        assert len(json.loads(kwargs['input'])['scopes']) == len(requests)
        calls.append(command)
        return SimpleNamespace(stdout=json.dumps({'status': 'needs_review', 'cloud_writes': 0}))

    monkeypatch.setattr(subprocess, 'run', run)
    result = discover('huawei', [], tmp_path)
    assert len(calls) == 1 and result['status'] == 'needs_review'
    assert len(result['local_evidence_bindings']) == len(requests)
    assert len(list((private / 'evidence').glob('huawei-target-native-*.json'))) == 1


def test_native_guard_rejects_cloud_writes_and_same_title_wrong_editor():
    script = r"""
const assert=require('node:assert/strict');
const {validScope,allowRequest,inspectEditor}=require('./scripts/huawei-matrix-browser.cjs');
const ids=new Set(['synthetic-target']);
const allowed=(method,path,body,type='xhr')=>allowRequest(method,'https://cloud.huawei.com'+path,type,body,ids);
assert(allowed('POST','/notepad/note/query',{guid:'synthetic-target'}));
assert(allowed('POST','/html/queryCookieValuesByNames',{}));
assert(!allowed('POST','/html/setCookieValue',{}));
assert(!allowed('POST','/notepad/note/query',{guid:'unrelated-private-note'}));
for(const verb of ['GET','POST','PUT','DELETE']) {
 for(const path of ['/notepad/note/create','/notepad/note/update','/notepad/note/delete','/proxyserver/upload'])
  assert(!allowed(verb,path,{guid:'synthetic-target'},'image'));
}
assert(allowed('POST','/proxyserver/driveFileProxy/preProcess',
 {httpMethod:'GET',generateSignFlag:false,needToSignUrl:''}));
assert(!allowed('POST','/proxyserver/driveFileProxy/preProcess',
 {httpMethod:'PUT',generateSignFlag:false,needToSignUrl:''}));
const download=id=>'/proxy/v1/download/'+encodeURIComponent('/v2/dataSync/callback/v1/1001/kind/note/record/'+id+'/assets/a/revisions/r');
assert(allowed('GET',download('synthetic-target'),null));
assert(!allowed('GET',download('unrelated-private-note'),null));
assert(!allowRequest('GET','https://unrelated.invalid/image','image',null,ids));
assert(!allowed('POST','/unknown/read',{}));
const scope={scopeVersion:1,scopeLabel:'honor-huawei-BD19:0',fixtureId:'synthetic-target',group_id:'123',
 title:'笔记互迁验收 · 合成同名',group_name:'synthetic',expectedText:'A 😀',expectedRuns:[{text:'A 😀',bold:true}],
 image_specs:[{width:640,height:240},{width:640,height:240}]};
assert(validScope(scope));assert(!validScope({...scope,scopeLabel:'wps-huawei-BD18:1'}));
for (const label of ['oppo-huawei-BI4:0','oppo-huawei-BI4:1']) assert(validScope({...scope,scopeLabel:label}));
for (const label of ['oppo-huawei-BI4:2','oppo-huawei-BI5:0']) assert(!validScope({...scope,scopeLabel:label}));
assert(validScope({...scope,group_id:'synthetic$tag'}));
assert(!validScope({...scope,group_id:'../private'}));
assert(!validScope({...scope,fixtureId:'../private'}));
let actualText='A 😀',fontWeight='700';const editorNode={},pre={};
const span={parentElement:pre,tagName:'SPAN'};
const detail={noteDetail:{guid:'synthetic-target'},noteContent:{tag_id:'123'},noteTitle:scope.title,
 $el:{isConnected:true,contains:el=>el===editorNode},$refs:{editor:{
  getValue:()=>({simpleLines:[{lineType:'Text',content:'A 😀'}]}),
  Editor:{setOption(){},refresh(){},doc:{eachLine:fn=>fn({lineType:'Text',text:'A 😀'})}}}}};
global.window={router:{currentRoute:{value:{name:'note',params:{itemId:'synthetic-target',categoryId:'123',kind:'note'}}}}};
global.document={querySelector:s=>s==='#app'?{_vnode:{component:{type:{name:'note-detail'},proxy:detail}}}:editorNode,
 querySelectorAll:()=>[pre],createTreeWalker:()=>{let read=false;return {nextNode:()=>{
  if(read)return null;read=true;return {parentElement:span,textContent:actualText};
 }}}};
global.NodeFilter={SHOW_TEXT:4};
global.getComputedStyle=()=>({fontWeight,fontStyle:'normal',textDecorationLine:'none',
 backgroundColor:'rgba(0, 0, 0, 0)',fontFamily:'sans-serif'});
assert(Object.values(inspectEditor({scope}).checks).every(Boolean));
detail.noteDetail.guid='same-title-neighbour';
assert.equal(inspectEditor({scope}).checks.detail,false);
assert.equal(inspectEditor({scope}).checks.body,false);
detail.noteDetail.guid='synthetic-target';
window.router.currentRoute.value.params.itemId='same-title-neighbour';
assert.equal(inspectEditor({scope}).checks.route,false);
window.router.currentRoute.value.params.itemId='synthetic-target';
detail.noteContent.tag_id='456';assert.equal(inspectEditor({scope}).checks.group,false);
detail.noteContent.tag_id='123';fontWeight='400';assert.equal(inspectEditor({scope}).checks.styles,false);
fontWeight='700';actualText='truncated';assert.equal(inspectEditor({scope}).checks.rendered_body,false);
const output=JSON.stringify(inspectEditor({scope}));
assert(!output.includes('synthetic-target')&&!output.includes('合成同名')&&!output.includes('truncated'));
window.router.currentRoute.value.name='Home';
assert.equal(inspectEditor({scope}).diagnostics.route_kind,'home');
window.router.currentRoute.value.name='private-dynamic-route';
const unknownRoute=inspectEditor({scope});assert.equal(unknownRoute.diagnostics.route_kind,'other');
assert(!JSON.stringify(unknownRoute).includes('private-dynamic-route'));
"""
    result = subprocess.run(['node', '-e', script], cwd=ROOT, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_blocked_diagnostics_never_copy_dynamic_values_or_grant_network_permission():
    script = r"""
const assert=require('node:assert/strict');
const {allowRequest,classifyBlockedRequest}=require('./scripts/huawei-matrix-browser.cjs');
const secret='private-dynamic-synthetic-marker';
const known=classifyBlockedRequest('POST','https://cloud.huawei.com/html/setCookieValue?value='+secret,'xhr');
assert.deepEqual(known,{method:'POST',resourceType:'xhr',officialHost:true,pathClass:'/html/setCookieValue'});
assert(!allowRequest('POST','https://cloud.huawei.com/html/setCookieValue','xhr',{},new Set()));
const results=[known,
 classifyBlockedRequest('GET','https://cloud.huawei.com/notepad/note/'+secret+'?token='+secret,'xhr'),
 classifyBlockedRequest('GET','https://cloud.huawei.com/'+secret+'.js?token='+secret,'script'),
 classifyBlockedRequest('GET','https://'+secret+'.invalid/html/getHomeData?token='+secret,'xhr'),
 classifyBlockedRequest(secret,'https://cloud.huawei.com/'+secret,secret),
 classifyBlockedRequest('GET','not-a-url:'+secret,'xhr')];
assert.equal(results[1].pathClass,'other_path');
assert.equal(results[2].pathClass,'static_asset');
assert.equal(results[3].officialHost,false);assert.equal(results[3].pathClass,'other_path');
assert.equal(results[4].method,'other');assert.equal(results[4].resourceType,'other');
assert(!JSON.stringify(results).includes(secret));
assert(!JSON.stringify(results).includes('https://'));
"""
    result = subprocess.run(['node', '-e', script], cwd=ROOT, capture_output=True, text=True, timeout=8)
    assert result.returncode == 0, result.stderr


def test_unsettled_navigation_is_observed_and_total_timeout_closes_own_browser():
    """A rejected auth bootstrap may strand Vue's guard but must not strand the lab worker."""
    script = r"""
const assert=require('node:assert/strict');
const {requestRoute,inspectEditor,bounded,runBatch}=require('./scripts/huawei-matrix-browser.cjs');
(async()=>{
 const never=new Promise(()=>{});
 global.window={router:{replace:()=>never,isReady:async()=>{},hasRoute:()=>true}};
 assert.equal(await bounded(()=>requestRoute({group_id:'123',fixtureId:'synthetic'}),100),undefined);
 let observations=0,closes=0;
 const scope={scopeVersion:1,scopeLabel:'honor-huawei-BD19:0',fixtureId:'synthetic',group_id:'123',
  title:'笔记互迁验收 · 超时测试',group_name:'synthetic',expectedText:'body',expectedRuns:[{text:'body'}],
  image_specs:[{width:640,height:240},{width:640,height:240}]};
 const page={goto:async()=>{},waitForFunction:async()=>{},
  evaluate:async(fn,arg)=>{
   if(fn===inspectEditor){observations++;return never}
   return fn(arg);
  }};
 const browser={newContext:async()=>({route:async()=>{},addCookies:async()=>{},newPage:async()=>page}),
  close:async()=>{closes++}};
 const cookies=[{name:'synthetic',value:'synthetic-test-value'}];
 const result=await runBatch({scopes:[scope],cookies},{chromium:{launch:async()=>browser},timeoutMs:25});
 assert.equal(observations,1,'Pending navigation must not prevent entering the identity observer.');
 assert.equal(closes,1,'Only the browser created by this run is closed, once.');
 assert.equal(cookies.length,0);
 assert.equal(result.timed_out,true);
 assert.equal(result.cleanup_completed,true);
 assert.equal(result.status,'needs_review');
 assert.equal(result.checked,0);assert.equal(result.cloud_writes,0);assert.equal(result.formal_acceptance,false);
 assert(!JSON.stringify(result).includes('synthetic-test-value'));
})().catch(error=>{console.error(error);process.exitCode=1});
"""
    result = subprocess.run(['node', '-e', script], cwd=ROOT, capture_output=True, text=True, timeout=8)
    assert result.returncode == 0, result.stderr


def test_initial_home_navigation_and_note_registration_finish_before_exact_selection():
    """A mounted router must not let target selection race Home's asynchronous setup."""
    script = r"""
const assert=require('node:assert/strict');
const {prepareRouter,requestRoute}=require('./scripts/huawei-matrix-browser.cjs');
(async()=>{
 let initialized=false,registered=false,replaces=0,ready;
 global.window={router:{isReady:()=>new Promise(resolve=>ready=()=>{initialized=true;resolve()}),
  hasRoute:name=>name==='note'&&registered,replace:()=>{replaces++;return Promise.resolve()}}};
 const page={evaluate:fn=>Promise.resolve().then(fn),waitForFunction:async(fn,arg,options)=>{
  assert.equal(arg,null);assert(options.timeout>0);
  const until=Date.now()+options.timeout;
  while(!fn()){
   if(Date.now()>=until){const error=new Error('test timeout');error.name='TimeoutError';throw error}
   await new Promise(resolve=>setTimeout(resolve,1));
  }
 }};
 const selected=prepareRouter(page,100).then(()=>requestRoute({fixtureId:'synthetic',group_id:'123'}));
 await new Promise(resolve=>setTimeout(resolve,5));assert.equal(replaces,0);assert.equal(initialized,false);
 ready();await new Promise(resolve=>setTimeout(resolve,5));assert.equal(replaces,0);
 registered=true;await selected;assert.equal(replaces,1);
 // An initialized Home without the note module must remain blocked.
 window.router.isReady=async()=>{};registered=false;
 await assert.rejects(prepareRouter(page,10).then(()=>requestRoute({})),{name:'TimeoutError'});
 assert.equal(replaces,1);
 // A permanently pending initial navigation is independently bounded too.
 window.router.isReady=()=>new Promise(()=>{});registered=true;
 await assert.rejects(prepareRouter(page,10).then(()=>requestRoute({})),{name:'TimeoutError'});
 assert.equal(replaces,1);
})().catch(error=>{console.error(error);process.exitCode=1});
"""
    result = subprocess.run(['node', '-e', script], cwd=ROOT, capture_output=True, text=True, timeout=8)
    assert result.returncode == 0, result.stderr


def test_native_reads_all_lines_with_real_codemirror_early_return_semantics():
    """Returning Array.push's count must not stop proof collection after the first line."""
    script = r"""
const assert=require('node:assert/strict');
const {inspectEditor}=require('./scripts/huawei-matrix-browser.cjs');
const rows=['alpha','beta','gamma'].map(text=>({lineType:'Text',text}));
const scope={fixtureId:'synthetic',group_id:'123',title:'synthetic title',expectedText:'alpha\nbeta\ngamma',
 expectedRuns:rows.map(row=>({text:row.text,bold:true}))};
const editorNode={},pre=rows.map(row=>({text:row.text}));
const cm={doc:{eachLine(fn){for(const row of rows)if(fn(row))return true}}};
const detail={noteDetail:{guid:'synthetic'},noteContent:{tag_id:'123'},noteTitle:scope.title,
 $el:{isConnected:true,contains:node=>node===editorNode},$refs:{editor:{Editor:cm,
 getValue:()=>({simpleLines:rows.map(row=>({lineType:'Text',content:row.text}))})}}};
global.window={router:{currentRoute:{value:{name:'note',params:{itemId:'synthetic',categoryId:'123',kind:'note'}}}}};
global.document={querySelector:selector=>selector==='#app'
 ?{_vnode:{component:{type:{name:'note-detail'},proxy:detail}}}:editorNode,
 querySelectorAll:()=>pre,createTreeWalker:element=>{let done=false;return {nextNode(){
  if(done)return null;done=true;return {textContent:element.text,parentElement:element};
 }}}};
global.NodeFilter={SHOW_TEXT:4};
global.getComputedStyle=()=>({fontWeight:'700',fontStyle:'normal',textDecorationLine:'none',
 backgroundColor:'rgba(0, 0, 0, 0)',fontFamily:'sans-serif'});
const result=inspectEditor({scope});
assert.equal(result.diagnostics.lines,3);assert.equal(result.diagnostics.renderedLines,3);
assert(Object.values(result.checks).every(Boolean));
rows[2].text='changed';assert.equal(inspectEditor({scope}).checks.body,false);
rows[2].text='gamma';
// The official edit loader moves precisely the serialized title to its own editor.
rows[0].text=scope.title;pre[0].text=scope.title;
const bodyText=rows.map(row=>row.text).join('\n'),bodyRuns=rows.map(row=>({text:row.text,bold:true}));
scope.expectedText=scope.title+'\n'+bodyText;
scope.expectedRuns=[{text:scope.title},...bodyRuns];
scope.editedTitleProjection={firstBlockKind:'paragraph',firstBlockText:scope.title,
 titleRuns:[{text:scope.title}],expectedText:bodyText,expectedRuns:bodyRuns};
detail.noteContent.data5={data2:'edit',data1:scope.title};
let displayedTitle=scope.title;const originalQuery=document.querySelector;
document.querySelector=s=>s==='#note_title_editor .CodeMirror'
 ?{CodeMirror:{getValue:()=>displayedTitle}}:originalQuery(s);
assert(Object.values(inspectEditor({scope}).checks).every(Boolean));
assert.equal(inspectEditor({scope}).diagnostics.edited_title_projection,true);
rows[0].text='';assert.equal(inspectEditor({scope}).checks.body,false,
 'The repeated first body paragraph must not be discarded as another title.');rows[0].text=scope.title;
detail.noteContent.data5.data2='auto';
assert.equal(inspectEditor({scope}).diagnostics.edited_title_projection,false);
assert.equal(inspectEditor({scope}).checks.body,false);
detail.noteContent.data5.data2='edit';detail.noteContent.data5.data1='wrong title';
assert.equal(inspectEditor({scope}).diagnostics.edited_title_projection,false);
assert.equal(inspectEditor({scope}).checks.body,false);
detail.noteContent.data5.data1=scope.title;displayedTitle='wrong visible title';
assert.equal(inspectEditor({scope}).checks.title,false);
assert.equal(inspectEditor({scope}).diagnostics.edited_title_projection,false);
displayedTitle=scope.title;scope.editedTitleProjection.firstBlockKind='heading';
assert.equal(inspectEditor({scope}).diagnostics.edited_title_projection,false);
assert.equal(inspectEditor({scope}).checks.body,false);
"""
    result = subprocess.run(['node', '-e', script], cwd=ROOT, capture_output=True, text=True, timeout=8)
    assert result.returncode == 0, result.stderr
