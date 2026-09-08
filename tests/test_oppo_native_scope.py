"""Native acceptance requires the exact consumed fixture and its current editor."""
import hashlib
import importlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from note_bridge.errors import BridgeError
from note_bridge.exporter import stamp
from note_bridge.models import Block, PlatformId, Span
from note_bridge.operations import receipt_key
from note_bridge.providers.oppo_groups import folder_key
from note_bridge.receipt_identity import migration_identity
from note_bridge.storage import Store

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
helper = importlib.import_module('oppo_native_scope')


def setup(tmp_path, monkeypatch, source_platform=None):
    from test_fixed_oppo_sources import oppo
    from test_fixed_oppo_sources import setup as fixture_setup

    real_notes = Store.notes
    ctx = fixture_setup(tmp_path, monkeypatch, oppo)
    monkeypatch.setattr(Store, 'notes', lambda store, platform, account:
        pytest.fail('Native OPPO scope must never read private Vivo cache') if platform == 'vivo'
        else real_notes(store, platform, account))
    real_module = helper._module
    monkeypatch.setattr(helper, '_module', lambda _, name: real_module(ROOT, name))
    if source_platform:
        names = {'wps': ('wps-group-J2-job.json', 'wps-complex-BD1-job.json'),
                 'xiaomi': ('xiaomi-group-P2-job.json', 'xiaomi-image-AC6-job.json')}[source_platform]
        entries, sources = [], []
        for index, name in enumerate(names):
            original = dict(kind='independent-cloud-write-smoke', id='fixture-20260907-' + name.removesuffix('-job.json'),
                target=source_platform, armed=False, expected_account='c' * 64, title='笔记互迁验收 · 原始样例 ' + str(index),
                with_images=not (source_platform == 'xiaomi' and index == 0), with_group=True)
            write(ctx.private / 'checkpoints' / name, original)
            write(ctx.private / 'evidence' / (original['id'] + '.json'), {'status': 'verified'})
            seed = helper.source_fixture(tmp_path, original)
            source = seed.model_copy(deep=True, update={'platform': PlatformId(source_platform), 'account_id': 'c' * 64,
                'source_id': str(index + 1) * 32})
            source.blocks.append(Block(spans=[Span(text=stamp(seed))]))
            target = SimpleNamespace(spec=SimpleNamespace(id=source_platform), account_id=source.account_id)
            ctx.store.save_receipt(receipt_key(seed, target), 'confirmed', [source.source_id])
            sources.append(source)
            entries.append(dict(title=source.display_title, with_images=bool(source.attachments), with_group=True,
                from_cloud_fixture=dict(platform=source_platform, manifest=name, account=source.account_id,
                    source_id=source.source_id, fingerprint=source.fingerprint())))
        ctx.store.replace_snapshot(source_platform, sources[0].account_id, sources, True)
        name = source_platform + '-oppo-BI1-job.json'
        identifier = 'matrix-20260907-' + name.removesuffix('-job.json')
        matrix = dict(kind='cloud-matrix-batch', id=identifier, target='oppo', armed=False,
            expected_account=ctx.account, source_policy='direct_seed_only', oppo_scope='OR1_fixed_direct_12', sources=entries)
        write(ctx.private / 'checkpoints' / name, matrix)
        write(ctx.private / 'checkpoints' / (identifier + '-consumed-job.json'), matrix)  # Real matrix archives have no timestamp.
        write(ctx.private / 'evidence' / (identifier + '.json'), dict(kind=matrix['kind'], batch_id=identifier,
            source=source_platform, target='oppo', formal_acceptance=False, status='api_verified',
            oppo_scope=matrix['oppo_scope'], original_target_notes_unchanged=True, only_confirmed_additions=True,
            items=[dict(source_id=s.source_id, source_fingerprint=s.fingerprint(), status='api_verified',
                receipt_status='confirmed', content_verified=True, group_verified=True) for s in sources]))
        ctx.selected = {'manifest': name, 'index': 0}
    else:
        sources = [ctx.seeds[1]]
        ctx.selected = {'manifest': helper.OR1, 'index': None}
    restored, keys = [], []
    for index, source in enumerate(sources):
        target = SimpleNamespace(spec=SimpleNamespace(id='oppo'), account_id=ctx.account)
        key = receipt_key(source, target)
        identity = 'synthetic-native-' + str(index)
        actual = source.model_copy(deep=True, update={'platform': PlatformId.OPPO, 'account_id': ctx.account,
            'source_id': identity, 'source_folder_id': 'synthetic-target-group'})
        remap = {}
        for i, asset in enumerate(actual.attachments):
            remap[asset.id] = 'target-attach-' + str(i)
            asset.id, asset.name = remap[asset.id], f'target-file-{index}-{i}'
        for block in actual.blocks:
            if block.kind == 'attachment':
                block.attachment_id = remap[block.attachment_id]
        actual.blocks.append(Block(spans=[Span(text=stamp(source))]))
        with ctx.store.connection() as db:
            db.execute('DELETE FROM resource_receipts WHERE receipt_key=?', (key,))
        ctx.store.save_receipt(key, 'sending', [identity])
        for i, asset in enumerate(actual.attachments):
            ctx.store.save_resource_receipt(key, asset.name, 'uploaded')
            ctx.store.save_resource_receipt(key, f'prepare/{index * 2 + i:032x}', 'uploaded')
        ctx.store.save_receipt(key, 'confirmed', [identity])
        ctx.store.bind_receipt_context(key, migration_identity(source, target))
        ctx.store.save_receipt(folder_key(source, ctx.account), 'confirmed', [actual.source_folder_id])
        restored.append(actual)
        keys.append(key)
    ctx.store.replace_snapshot('oppo', ctx.account, restored, True)
    ctx.restored, ctx.keys = restored, keys
    return ctx


def write(path, data):
    path.write_text(json.dumps(data), 'utf-8')


@pytest.mark.parametrize('source_platform', [None, 'wps', 'xiaomi'])
def test_scope_recomputes_exact_lineage_assets_and_actual_style_requirements(tmp_path, monkeypatch, source_platform):
    ctx = setup(tmp_path, monkeypatch, source_platform)
    before = (ctx.private / 'session-lab/notes.sqlite').read_bytes()
    result = helper.browser_target(ctx.selected, tmp_path, ctx.account)
    assert result['fixtureId'] == ctx.restored[0].source_id and result['target_fingerprint'] == ctx.restored[0].fingerprint()
    assert result['expectedText'] == ctx.restored[0].plain_text and result['receipt_key'] == ctx.keys[0]
    assert len(result['image_specs']) == (0 if source_platform == 'xiaomi' else 2)
    if source_platform:
        assert result['headings'] == [], 'Basic original seeds must not invent comprehensive style requirements.'
    assert (ctx.private / 'session-lab/notes.sqlite').read_bytes() == before
    assert result['evidence_sha256']


@pytest.mark.parametrize('tamper', ['account', 'unconsumed', 'unknown', 'context', 'api_pending', 'body', 'group', 'resource', 'image'])
def test_scope_refuses_unverified_or_changed_content_without_cloud_reads(tmp_path, monkeypatch, tamper):
    ctx = setup(tmp_path, monkeypatch, 'wps')
    if tamper == 'account':
        ctx.account = 'd' * 64
    elif tamper in ('unconsumed', 'api_pending'):
        name = 'matrix-20260907-wps-oppo-BI1'
        path = ctx.private / ('checkpoints/' + name + '-consumed-job.json' if tamper == 'unconsumed' else 'evidence/' + name + '.json')
        value = json.loads(path.read_text('utf-8'))
        value['armed' if tamper == 'unconsumed' else 'status'] = True if tamper == 'unconsumed' else 'needs_review'
        write(path, value)
    elif tamper == 'unknown':
        ctx.store.save_receipt(ctx.keys[0], 'uncertain')
    elif tamper == 'context':
        with ctx.store.connection() as db:
            db.execute('DELETE FROM receipt_contexts WHERE key=?', (ctx.keys[0],))
    elif tamper in ('body', 'group'):
        if tamper == 'body':
            ctx.restored[0].blocks = [Block(spans=[Span(text='same title but unrelated body')])]
        else:
            ctx.restored[0].source_folder_id = 'unrelated-group'
        ctx.store.replace_snapshot('oppo', ctx.account, ctx.restored, True)
    elif tamper == 'resource':
        with ctx.store.connection() as db:
            db.execute('UPDATE resource_receipts SET status=? WHERE receipt_key=?', ('uncertain', ctx.keys[0]))
    else:
        (ctx.private / 'session-lab/resources' / ctx.restored[0].attachments[0].local_path).write_bytes(b'changed')
    with pytest.raises((ValueError, BridgeError)):
        helper.browser_target(ctx.selected, tmp_path, ctx.account)


@pytest.mark.parametrize('selected', [{'manifest': helper.OR1, 'index': 0}, {'manifest': '../private.json', 'index': 0},
    {'manifest': 'wps-oppo-OTHER-job.json', 'index': 0}, {'manifest': 'wps-oppo-BI1-job.json', 'index': True}])
def test_scope_rejects_unrelated_paths_and_indices_before_opening_database(tmp_path, selected):
    with pytest.raises(ValueError):
        helper.browser_target(selected, tmp_path, 'a' * 64)


def test_native_request_guard_and_editor_checks_are_independent_of_title():
    script = r"""
const assert=require('node:assert/strict');
const {validScope,allowRequest,routeClass,inspectEditor}=require('./scripts/oppo-matrix-browser.cjs');
const scope={scopeVersion:1,scopeLabel:'wps-oppo-BI1:0',fixtureId:'selected-id',group_id:'selected-group',group_name:'synthetic',
 title:'笔记互迁验收 · 同名',expectedText:'中文 English 😀',expectedRuns:[{text:'中文 English 😀',bold:true}],
 headings:[],todos:[],lists:[],quotes:[],codes:[],tables:[],dividers:0,
 image_specs:[{id:'a',cloud_id:'cloud-a',width:640,height:240},{id:'b',cloud_id:'cloud-b',width:640,height:240}]};
assert(validScope(scope));assert(!validScope({...scope,image_specs:[]}));
assert(validScope({...scope,scopeLabel:'xiaomi-oppo-BI5:0',image_specs:[]}));
const ids=new Set(['selected-id']),files=new Set(['cloud-a','cloud-b']),payload={key:'synthetic',iv:'synthetic',encryptContent:'synthetic'};
const allowed=(method,path,body=payload,type='xhr')=>allowRequest(method,'https://owork-api-cn.oppo.com'+path,type,body,ids,files);
const prefix='/owork-server/web/note/v2/';
assert(allowed('POST',prefix+'info?recordId=selected-id&version='));
assert(!allowed('POST',prefix+'info?recordId=private-neighbour&version='));
assert(allowed('GET',prefix+'file-download/cloud-a?OCLOUD_ACCESS_TOKEN=RAM_ONLY_SECRET'));
assert(!allowed('GET',prefix+'file-download/private-file'));
const thumbnail=prefix+'file-thumbnail/cloud-a?x-kit-process='+encodeURIComponent('image/format,webp/quality,Q_85');
assert(allowed('GET',thumbnail));assert.equal(routeClass('https://owork-api-cn.oppo.com'+thumbnail),'selected_thumbnail');
assert(!allowed('GET',thumbnail.replace('cloud-a','private-file')));
assert(!allowed('GET',thumbnail+'&unexpected=1'));assert(!allowed('GET',thumbnail+'&x-kit-process=anything'));
assert(!allowed('GET',thumbnail.replace('Q_85','Q_10')));assert(!allowed('GET',prefix+'file-thumbnail/cloud-a'));
assert(!allowed('POST',thumbnail));
for(const method of ['GET','POST','PUT','DELETE'])for(const path of ['add','update','delete','prepare-file-upload','big-file-part-merge'])
 assert(!allowed(method,prefix+path));
assert(!allowRequest('GET','https://id.heytap.com/login','document',null,ids,files));
assert(!allowRequest('GET','https://unrelated.invalid/image.png','image',null,ids,files));
assert(!allowed('POST',prefix+'unknown'));
let rendered=scope.expectedText,model=scope.expectedText,weight='700';
const rect=()=>({width:640,height:240});
const titleNode={tagName:'H1',innerText:scope.title,contains:n=>n===titleText};
const titleText={parentElement:titleNode,textContent:scope.title};
const titleModel={type:{name:'heading'},attrs:{level:1},nodeSize:30,textContent:scope.title};
const node={isConnected:true,getBoundingClientRect:rect,parentElement:null,firstElementChild:titleNode,
 querySelectorAll:s=>s==='img'?images:s==='h1,h2,h3,h4,h5,h6'?[titleNode]:[]};
titleNode.parentElement=node;
const span={parentElement:node,tagName:'SPAN',closest:()=>null};
const images=scope.image_specs.map(spec=>{
 const image={isConnected:true,complete:true,getBoundingClientRect:rect,naturalWidth:640,naturalHeight:240,decode:async()=>{},parentElement:null,closest:()=>wrapper};
 const wrapper={parentElement:node,contains:el=>el===image,__vue__:{$props:{node:{type:{name:'image'},attrs:{attachId:spec.id}}}}};
 wrapper.__vue__.$el=wrapper;
 image.parentElement=wrapper;return image;
});
const detail={recordId:scope.fixtureId,groupGuid:scope.group_id,rawTitle:scope.title,status:0};
const parent={currentNote:{...detail},$refs:{},$parent:null};
const owner={noteDetail:{...detail},groupName:scope.group_name,$el:{contains:el=>el===node},$parent:parent,
 editor:{view:{dom:node},state:{doc:{firstChild:titleModel,content:{size:70},textBetween:(from)=>{
  assert.equal(from,titleModel.nodeSize,'Only the separately bound title node may be excluded.');return model;}}}}};
parent.$refs.richEditorVueRef=owner;node.parentElement={__vue__:owner,parentElement:null};
global.document={querySelectorAll:()=>[node],createTreeWalker:()=>{let index=0;return {nextNode:()=>
 [titleText,{parentElement:span,textContent:rendered},null][index++]}}};
global.NodeFilter={SHOW_TEXT:4};global.getComputedStyle=()=>({fontWeight:weight,fontStyle:'normal',
 textDecorationLine:'none',backgroundColor:'transparent',fontFamily:'sans-serif'});
(async()=>{
 const good=await inspectEditor(scope);assert(Object.values(good.checks).every(Boolean));
 const version=JSON.parse(process.argv[1]);
 scope.expected_version_sha256=version.sha256;owner.noteDetail.version=version.value;parent.currentNote.version=version.value;
 const repaired=await inspectEditor(scope);assert.equal(repaired.checks.identity,true);assert.equal(repaired.diagnostics.versionMatched,true);
 owner.noteDetail.version='changed';assert.equal((await inspectEditor(scope)).checks.identity,false);
 owner.noteDetail.version=version.value;parent.currentNote.version='changed';assert.equal((await inspectEditor(scope)).checks.identity,false);
 delete scope.expected_version_sha256;delete owner.noteDetail.version;delete parent.currentNote.version;
 assert.equal(good.diagnostics.titleDOMTag,'H1');assert.equal(good.diagnostics.titleModelType,'heading');
 assert.equal(good.diagnostics.titleModelLevel,1);assert.equal(good.diagnostics.titleProjectionMatched,true);
 titleNode.tagName='P';titleModel.type.name='paragraph';titleModel.attrs={};
 const plainTitle=await inspectEditor(scope);assert(Object.values(plainTitle.checks).every(Boolean));
 assert.equal(plainTitle.diagnostics.titleDOMTag,'P');assert.equal(plainTitle.diagnostics.titleModelType,'paragraph');
 assert.equal(plainTitle.diagnostics.titleModelLevel,null);
 titleNode.tagName='DIV';
 const officialParagraph=await inspectEditor(scope);assert(Object.values(officialParagraph.checks).every(Boolean));
 assert.equal(officialParagraph.diagnostics.titleDOMTag,'DIV');
 titleNode.tagName='SECTION';assert.equal((await inspectEditor(scope)).checks.title,false);
 titleNode.tagName='DIV';
 titleNode.innerText=scope.title+' body prefix';titleModel.textContent=titleNode.innerText;
 assert.equal((await inspectEditor(scope)).checks.title,false,'A title prefix must not discard any body content.');
 titleNode.innerText=scope.title;titleModel.textContent=scope.title;
 titleModel.type.name='heading';titleModel.attrs={level:1};
 assert.equal((await inspectEditor(scope)).checks.title,false,'DOM and model projections must agree.');
 titleNode.tagName='H1';
 titleNode.innerText='wrong heading';assert.equal((await inspectEditor(scope)).checks.title,false);
 titleNode.innerText=scope.title;titleModel.textContent='wrong model title';assert.equal((await inspectEditor(scope)).checks.title,false);
 titleModel.textContent=scope.title;titleModel.attrs.level=2;assert.equal((await inspectEditor(scope)).checks.title,false);
 titleModel.attrs.level=1;
 parent.currentNote.recordId='same-title-neighbour';assert.equal((await inspectEditor(scope)).checks.identity,false);
 parent.currentNote.recordId=scope.fixtureId;owner.noteDetail.groupGuid='wrong';assert.equal((await inspectEditor(scope)).checks.group,false);
 owner.noteDetail.groupGuid=scope.group_id;weight='400';assert.equal((await inspectEditor(scope)).checks.styles,false);
 weight='700';rendered='truncated';assert.equal((await inspectEditor(scope)).checks.renderedText,false);
 rendered=scope.expectedText;model='reordered body';assert.equal((await inspectEditor(scope)).checks.modelText,false);
 model=scope.expectedText;images[1].parentElement.__vue__.$props.node.attrs.attachId='a';assert.equal((await inspectEditor(scope)).checks.images,false);
 const output=JSON.stringify(await inspectEditor(scope));assert(!output.includes('selected-id')&&!output.includes('同名')&&!output.includes('中文'));
})().catch(error=>{console.error(error);process.exitCode=1});
"""
    version = '修后-\x7f-😀-"-\\'
    encoded = json.dumps(version, ensure_ascii=True, sort_keys=True).encode()
    result = subprocess.run(['node', '-e', script, json.dumps({'value': version, 'sha256': hashlib.sha256(encoded).hexdigest()})],
                            cwd=ROOT, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_official_list_entry_and_sanitized_metadata_diagnostics_do_not_expand_note_scope():
    script = r"""
const assert=require('node:assert/strict');
const {ENTRY,allowRequest,blockedRequest,locateRow,pageDiagnostics}=require('./scripts/oppo-matrix-browser.cjs');
const ids=new Set(['selected-id']),files=new Set(['selected-file']);
const allowed=(url,type='document',method='GET')=>allowRequest(method,url,type,null,ids,files);
assert.equal(ENTRY,'https://cloud.oppo.com/owork/mapp/sticky-notes');
assert(allowed(ENTRY));assert(!allowed(ENTRY+'?id=private'));
assert(!allowed(ENTRY.replace('sticky-notes','file')));
assert(allowed('https://cloud.oppo.com/owork/static/build/main.js','script'));
assert(allowed('https://owork-api-cn.oppo.com/note/','fetch'));
assert(allowed('https://owork-api-cn.oppo.com/note/index.html','fetch'));
assert(allowed('https://owork-api-cn.oppo.com/note/navigator.html','fetch'));
assert(!allowed('https://owork-api-cn.oppo.com/note/navigator.html?private=value','fetch'));
assert(!allowed('https://owork-api-cn.oppo.com/note/navigator.html','fetch','POST'));
assert(allowed('https://owork-api-cn.oppo.com/note/static/build/main.js','script'));
assert(allowed('https://owork-api-cn.oppo.com/note/static/build/main.js','fetch'));
assert(allowed('https://owork-api-cn.oppo.com/note/static/build/main.css','fetch'));
assert(!allowed('https://owork-api-cn.oppo.com/note/static/build/private.json','fetch'));
assert(!allowed('https://owork-api-cn.oppo.com/note/static/build/main.js?private=value','fetch'));
assert(!allowed('https://owork-api-cn.oppo.com/file/index.html','fetch'));
assert(!allowed('https://owork-api-cn.oppo.com/note/','fetch','POST'));
assert(!allowed('https://owork-api-cn.oppo.com/note/?id=private','fetch'));
assert(!allowed('https://owork-api-cn.oppo.com/note/private','fetch'));
assert(allowed('https://id.heytap.com/packages/account_web_sdk/index.umd.js','script'));
assert(!allowed('https://id.heytap.com/packages/account_web_sdk/index.umd.js?session=private','script'));
assert(!allowed('https://id.heytap.com/login','document'));
assert(!allowed('https://owork-api-cn.oppo.com/owork-server/web/note/v2/file-download/private','image'));
const blocked=url=>blockedRequest({url:()=>url,method:()=> 'GET',resourceType:()=> 'fetch'},'other',false);
const safe=blocked('https://owork-api-cn.oppo.com/note/?credential=synthetic-secret');
assert.equal(safe.pathFingerprint,require('node:crypto').createHash('sha256').update('/note/').digest('hex'));
assert.equal(safe.pathFingerprint,blocked('https://owork-api-cn.oppo.com/note/?different=value').pathFingerprint);
assert.equal(safe.hasQuery,true);assert.equal(safe.hostClass,'api');assert.equal(safe.resourceType,'fetch');
assert(!JSON.stringify(safe).includes('synthetic-secret')&&!JSON.stringify(safe).includes('/note/'));
const scope={fixtureId:'selected-id',group_id:'selected-group',title:'synthetic title'};
const visible={isConnected:true,getBoundingClientRect:()=>({width:100,height:20})};
const option={recordId:scope.fixtureId,groupGuid:scope.group_id,rawTitle:scope.title,status:0};
const child={},row={...visible,removeAttribute(){},setAttribute(k,v){this.mark=[k,v]},contains:n=>n===child,querySelectorAll:()=>[child]};
child.__vue__={$props:{optionData:option},$el:child};
const list={...visible,parentElement:null};
const metadata=Array.from({length:17},(_,i)=>({recordId:i===12?scope.fixtureId:'private-'+i,
 get rawTitle(){throw Error('Unselected titles are unnecessary for diagnostics')},
 get content(){throw Error('Diagnostic must never read unrelated bodies')}}));
const owner={dataListNotes:{list:metadata,pageNo:2,pageSize:30,totalCount:17,hasMore:false},loading:false,
 $refs:{scrollContainer:{contains:n=>n===list}}};
list.__vue__=owner;
global.document={readyState:'complete',getElementById:()=>({}),querySelectorAll:s=>s==='.list-container ul.list'?[list]:s==='li.list-item'?[row]:[]};
global.location={hostname:'cloud.oppo.com',pathname:'/owork/mapp/sticky-notes'};
assert.equal(locateRow(scope).matches,1);
let diagnostic=pageDiagnostics(scope);
assert.equal(diagnostic.pathClass,'note_list');assert.equal(diagnostic.loadedRows,17);
assert.equal(diagnostic.pageSize,30);assert.equal(diagnostic.selectedInLoadedList,true);
assert.equal(diagnostic.listOwners,1);assert.equal(diagnostic.hasMore,false);
assert(!JSON.stringify(diagnostic).includes('selected-id')&&!JSON.stringify(diagnostic).includes('private-'));
global.location.pathname='/note/';assert.equal(pageDiagnostics(scope).pathClass,'note_share');
global.location.pathname='/unexpected/private';assert.equal(pageDiagnostics(scope).pathClass,'other');
owner.dataListNotes.list=[];assert.equal(pageDiagnostics(scope).selectedInLoadedList,false);
option.groupGuid='wrong';assert.equal(locateRow(scope).wrongRow,true);
"""
    result = subprocess.run(['node', '-e', script], cwd=ROOT, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('tamper', [None, 'top_label_only', 'wrong_index', 'wrong_image', 'missing_body'])
def test_dispatcher_cannot_accept_a_forged_native_success_label(tamper):
    scopes = [{'image_specs': [{}, {}]}]
    item = {'index': 0, 'status': 'content_images_styles_verified',
        'checks': dict.fromkeys(helper.NATIVE_CHECKS, True),
        'structures': dict.fromkeys(('headings', 'todos', 'lists', 'quotes', 'codes', 'tables', 'dividers'), True),
        'images': [{'scopeIndex': i, **dict.fromkeys(('identityMatched', 'decoded', 'visible', 'dimensionsMatched'), True)} for i in range(2)]}
    report = {'kind': 'oppo-matrix-native-batch', 'formal_acceptance': False, 'cloud_writes': 0,
              'status': 'verified', 'total': 1, 'checked': 1, 'remaining': 0, 'items': [item]}
    if tamper == 'top_label_only':
        report['items'] = []
    elif tamper == 'wrong_index':
        item['index'] = 1
    elif tamper == 'wrong_image':
        item['images'][1]['scopeIndex'] = 0
    elif tamper == 'missing_body':
        item['checks']['renderedText'] = False
    if tamper:
        with pytest.raises(ValueError):
            helper.validate_report(report, scopes)
    else:
        assert helper.validate_report(report, scopes)


def test_official_image_alb_only_reads_exact_receipted_files():
    script = r"""
const assert=require('node:assert/strict');
const {allowRequest,blockedRequest}=require('./scripts/oppo-matrix-browser.cjs');
const origin='https://owork-alb-cn.oppo.com',prefix='/owork-server/web/note/v2/';
const ids=new Set(['selected-id']),files=new Set(['selected-file']);
const allowed=(method,path,base=origin)=>allowRequest(method,base+path,'image',null,ids,files);
const image=prefix+'file-download/selected-file';
const thumb=prefix+'file-thumbnail/selected-file?x-kit-process=image/format,webp/quality,Q_85';
const listThumb=prefix+'file-thumbnail/selected-file?x-kit-process=image/resize,h_42/quality,Q_60/format,webp';
for(const method of ['GET','HEAD']){
 assert(allowed(method,image));assert(allowed(method,thumb));assert(allowed(method,listThumb));
 assert(!allowed(method,image+'?fileName=private'));
 assert(!allowed(method,thumb+'&x-kit-process=image/format,png'));
 assert(!allowed(method,thumb+'&extra=value'));
 assert(!allowed(method,thumb.replace('Q_85','Q_100')));
 assert(!allowed(method,listThumb.replace('h_42','h_43')));
 assert(!allowed(method,listThumb.replace('Q_60','Q_61')));
 assert(!allowed(method,listThumb+'&extra=value'));
 assert(!allowed(method,listThumb+'&x-kit-process=image/format,webp/quality,Q_85'));
 assert(!allowed(method,prefix+'file-thumbnail/selected-file'));
 for(const path of [image,thumb,listThumb]){
  assert(!allowed(method,path.replace('selected-file','private-file')));
  assert(!allowed(method,path.replace('selected-file','selected-file/extra')));
  for(const base of ['http://owork-alb-cn.oppo.com','https://owork-alb-cn.oppo.com:444',
   'https://owork-alb-cn.oppo.com.evil.invalid','https://owork-file-cn.oppo.com',
   'https://user:password@owork-alb-cn.oppo.com'])assert(!allowed(method,path,base));
 }
 for(const path of [prefix+'info?recordId=selected-id',prefix+'list',prefix+'group-list-new',
  '/owork-server/web/account/v1/userInfo','/note/index.html'])assert(!allowed(method,path));
}
for(const method of ['POST','PUT','PATCH','DELETE','OPTIONS']){
 assert(!allowed(method,image));assert(!allowed(method,thumb));assert(!allowed(method,listThumb));
 assert(!allowRequest(method,origin+prefix+'info?recordId=selected-id','fetch',
  {encryptContent:'synthetic',iv:'synthetic',key:'synthetic'},ids,files));
}
const diagnostic=blockedRequest({url:()=>origin+image+'?token=synthetic-secret',method:()=> 'GET',
 resourceType:()=> 'image'},'selected_image',false);
assert.equal(diagnostic.officialHost,true);assert.equal(diagnostic.hostClass,'file_alb');
assert(!JSON.stringify(diagnostic).includes('synthetic-secret'));
assert(!JSON.stringify(diagnostic).includes('selected-file'));
"""
    result = subprocess.run(['node', '-e', script], cwd=ROOT, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
