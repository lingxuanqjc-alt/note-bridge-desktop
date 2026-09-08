"""Historical rendering scopes must not become new writes or broad account reads."""
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from note_bridge.errors import BridgeError
from note_bridge.models import Block, NoteDocument, Span
from note_bridge.providers.huawei_groups import folder_key
from note_bridge.receipt_identity import FIELDS, identity_key, receipt_key
from note_bridge.storage import Store

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import huawei_legacy_native_scope as scope  # noqa: E402
from xiaomi_browser_scope import _module  # noqa: E402


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), 'utf-8')


def note(platform, account, identifier, title='笔记互迁验收 · 合成范围'):
    return NoteDocument(platform=platform, account_id=account, source_id=identifier, title=title,
        source_folder_id='synthetic-folder', source_folder_name='synthetic-group',
        blocks=[Block(spans=[Span(text='笔记互迁验收 · 合成范围')]), Block(spans=[Span(text='synthetic body')])])


@pytest.fixture
def empty_case(tmp_path, monkeypatch):
    base = tmp_path / '.private'
    target_account, source_account = 'a' * 64, 'b' * 64
    source = note('meizu', source_account, 'synthetic-source', '')
    actual = note('huawei', target_account, 'synthetic-target')
    identity = dict(zip(FIELDS, ('meizu', source_account, source.source_id, source.fingerprint(),
                                 'huawei', target_account), strict=True))
    key = identity_key(identity)
    direct = dict(kind='independent-cloud-write-smoke', id='fixture-20260906-meizu-empty-X1',
        armed=False, target='meizu', expected_account=source_account, with_images=False,
        with_group=True, content_case='empty_title', title=actual.title)
    build = _module(ROOT, 'live-fixture-job').fixture
    seed = build(direct)
    seed_key = receipt_key(seed, SimpleNamespace(spec=SimpleNamespace(id='meizu'), account_id=source_account))
    origin = dict(platform='meizu', account=source_account, source_id=source.source_id,
                  fingerprint=source.fingerprint(), manifest='meizu-empty-X1-job.json')
    job = dict(kind='cloud-matrix-batch', id='matrix-20260906-meizu-huawei-AA5', target='huawei',
        expected_account=target_account, armed=False, sources=[dict(title=actual.title,
            with_images=False, with_group=True, from_cloud_fixture=origin)])
    old = [note('huawei', target_account, 'original-' + str(i)) for i in range(12)]
    baseline = dict(target={n.source_id: n.fingerprint() for n in old}, source={source.source_id: source.fingerprint()})
    original = dict(batch_id=job['id'], target='huawei', formal_acceptance=False, before_count=12,
        status='api_verified', original_target_notes_unchanged=True, only_confirmed_additions=True,
        items=[dict(source_id=source.source_id, source_fingerprint=source.fingerprint(), status='api_verified',
            receipt_status='confirmed', content_verified=True, group_verified=True,
            source_title_empty=True, target_title_empty=False)], issues=[])
    put(base / 'checkpoints/meizu-empty-X1-job.json', direct)
    put(base / 'checkpoints/meizu-huawei-AA5-job.json', job)
    put(base / 'checkpoints' / (job['id'] + '-before.json'), baseline)
    put(base / 'checkpoints/matrix-20260907-xiaomi-huawei-BC3-before.json',
        {'target': {**baseline['target'], actual.source_id: actual.fingerprint(), 'another-original': 'e' * 64}})
    put(base / 'evidence' / (job['id'] + '.json'), original)
    store = Store(base / 'session-lab/notes.sqlite')
    store.replace_snapshot('meizu', source_account, [source], True)
    store.replace_snapshot('huawei', target_account, old + [actual], True)
    store.save_receipt(seed_key, 'confirmed', [source.source_id])
    store.save_receipt(key, 'confirmed', [actual.source_id])
    store.save_receipt(folder_key(source, target_account), 'confirmed', [actual.source_folder_id])
    monkeypatch.setattr(scope, '_module', lambda root, name: _module(ROOT, name))
    return SimpleNamespace(root=tmp_path, base=base, store=store, source=source, actual=actual,
        account=target_account, identity=identity, key=key, seed_key=seed_key, job=job, baseline=baseline,
        selected=dict(manifest='meizu-huawei-AA5-job.json', index=0))


def test_empty_scope_uses_exact_archived_ancestry_without_context_backfill_or_broad_cache(empty_case, monkeypatch):
    case = empty_case
    with case.store.connection() as db:
        before = list(db.iterdump())
    monkeypatch.setattr(Store, 'notes', lambda *args: pytest.fail('Broad account body read'))
    original_cached = scope.cached
    calls = []

    def cached(db, platform, account, source_id):
        assert platform != 'vivo', 'Synthetic ancestry must not read private Vivo content.'
        calls.append((platform, source_id))
        return original_cached(db, platform, account, source_id)

    monkeypatch.setattr(scope, 'cached', cached)
    result = scope.browser_target(case.selected, case.root, case.account)
    assert result['image_specs'] == [] and result['fixtureId'] == case.actual.source_id
    assert len([p for p, _ in calls if p == 'meizu']) == 1
    with case.store.connection() as db:
        assert list(db.iterdump()) == before, 'Native scope selection must not create receipt contexts or mutate state.'


@pytest.mark.parametrize('selected', [
    dict(manifest='meizu-huawei-AA2-job.json', index=3),
    dict(manifest='xiaomi-huawei-BC3-job.json', index=3),
    dict(manifest='meizu-huawei-AA5-job.json', index=True),
    dict(manifest='vivo-huawei-W4-job.json', index=0),
    dict(manifest='../private.json', index=0),
])
def test_duplicate_empty_and_unattempted_entries_never_enter_native_scope(tmp_path, selected):
    with pytest.raises(ValueError, match='scope_unverified'):
        scope.browser_target(selected, tmp_path, 'synthetic')


@pytest.mark.parametrize('change', ['wrong-account', 'ancestor-uncertain', 'target-uncertain',
    'context-conflict', 'source-body-changed', 'target-body-changed', 'original-body-changed', 'extra-resource'])
def test_uncertain_or_changed_identity_cannot_be_reported_as_verified(empty_case, change):
    case = empty_case
    if change == 'ancestor-uncertain':
        case.store.save_receipt(case.seed_key, 'uncertain')
    elif change == 'target-uncertain':
        case.store.save_receipt(case.key, 'uncertain')
    elif change == 'context-conflict':
        with case.store.connection() as db:
            wrong = {**case.identity, 'source_account': 'c' * 64}
            db.execute('INSERT INTO receipt_contexts VALUES (?,?,?,?,?,?,?)',
                       (case.key, *(wrong[k] for k in FIELDS)))
    elif change.endswith('body-changed'):
        n = case.source if change.startswith('source') else case.actual
        if change.startswith('original'):
            n = note('huawei', case.account, 'original-0')
        n = n.model_copy(deep=True)
        n.blocks.append(Block(spans=[Span(text='unexpected edit')]))
        with case.store.connection() as db:
            db.execute('UPDATE notes SET document=? WHERE platform=? AND account=? AND source_id=?',
                       (n.model_dump_json(), n.platform, n.account_id, n.source_id))
    elif change == 'extra-resource':
        with case.store.connection() as db:
            db.execute('INSERT INTO resource_receipts VALUES (?,?,?)', (case.key, 'unexpected-image', 'linked'))
    with pytest.raises((ValueError, BridgeError)) as error:
        scope.browser_target(case.selected, case.root, 'wrong' if change == 'wrong-account' else case.account)
    assert getattr(error.value, 'code', None) == 'synthetic_receipt_unconfirmed' or 'scope_unverified' in str(error.value)


@pytest.mark.parametrize('change', [None, 'sha', 'target-fingerprint', 'missing-repair-receipt'])
def test_recovered_aa2_requires_original_hashes_exact_version_and_confirmed_repair(tmp_path, change):
    source = note('meizu', 'b' * 64, 'source')
    actual = note('huawei', 'a' * 64, 'target-2')
    key = 'd' * 64
    base = tmp_path / '.private'
    paths = [base / 'checkpoints/meizu-huawei-AA2-job.json', base / 'evidence/original.json',
             base / 'checkpoints/baseline.json']
    job = {'id': 'matrix-20260906-meizu-huawei-AA2'}
    baseline = {'target': {'old': '0' * 64}}
    recovery = {'kind': 'huawei-AA2-scoped-recovery', 'mode': 'repair', 'status': 'verified_with_degradation',
        'creates': 0, 'uploads': 0, 'updates': 1, 'existing_images': 2, 'missing_images': 0,
        'content_verified': True, 'original_other_notes_unchanged': True, 'warnings': []}
    recovery_before = {'fingerprints': {'old': '0' * 64, **{'target-' + str(i): '1' * 64 for i in range(3)}}}
    original = {'status': 'needs_review'}
    for path, data in zip(paths, (job, original, baseline), strict=True):
        put(path, data)
    repair_path = base / 'evidence/huawei-AA2-recovery-repair.json'
    before_path = base / 'checkpoints/huawei-AA2-recovery-before.json'
    put(repair_path, recovery)
    put(before_path, recovery_before)
    items = [dict(entry_index=i, source=scope._identity(source), receipt_key=key,
                  target={**scope._identity(actual), 'source_id': 'target-' + str(i)}) for i in range(3)]
    proof = dict(kind='huawei-recovered-source-lineage', batch_id=job['id'], formal_acceptance=False,
        cloud_writes=0, items=items, fresh_readback_task_id='synthetic-task',
        file_sha256={k: hashlib.sha256(p.read_bytes()).hexdigest() for k, p in zip(
            ('manifest', 'original_evidence', 'baseline', 'recovery_evidence', 'recovery_baseline'),
            [*paths, repair_path, before_path], strict=True)})
    if change == 'sha':
        proof['file_sha256']['baseline'] = 'f' * 64
    elif change == 'target-fingerprint':
        proof['items'][2]['target']['fingerprint'] = 'f' * 64
    put(base / 'evidence/huawei-AA2-source-lineage.json', proof)

    def read(relative):
        path = base / relative
        paths.append(path)
        return json.loads(path.read_text('utf-8'))

    store = SimpleNamespace(task=lambda _: SimpleNamespace(operation='fetch', status='succeeded', issues=[]),
        receipt=lambda _: None if change == 'missing-repair-receipt' else dict(status='confirmed', remote_ids=[actual.source_id]))
    args = ('meizu-huawei-AA2-job.json', 2, job, original, baseline, source, actual, key, store, read, paths)
    if change is None:
        assert scope._proof(*args) == []
    else:
        with pytest.raises(ValueError, match='scope_unverified'):
            scope._proof(*args)


def test_vivo_cache_is_never_a_legal_fallback():
    with pytest.raises(ValueError, match='scope_unverified'):
        scope.cached(None, 'vivo', 'synthetic', 'synthetic')


def test_shared_dispatch_preserves_exact_legacy_whitelist(empty_case):
    from huawei_native_scope import browser_target

    result = browser_target(empty_case.selected, empty_case.root, empty_case.account)
    assert result['scopeLabel'] == 'meizu-huawei-AA5:0' and result['image_specs'] == []


def test_browser_zero_images_is_only_aa5_and_never_waits_for_two_images():
    script = r"""
const assert=require('node:assert/strict');
const {validScope,runBatch,inspectEditor}=require('./scripts/huawei-matrix-browser.cjs');
(async()=>{
 const scope={scopeVersion:1,scopeLabel:'meizu-huawei-AA5:0',fixtureId:'synthetic',group_id:'123',
  title:'笔记互迁验收 · 合成范围',group_name:'synthetic',expectedText:'body',expectedRuns:[{text:'body'}],image_specs:[]};
 const double=[{width:640,height:240},{width:640,height:240}];
 assert(validScope(scope));assert(!validScope({...scope,image_specs:double}));
 for(const label of ['xiaomi-huawei-BC3:0','xiaomi-huawei-BC3:1','xiaomi-huawei-BC3:2',
   'meizu-huawei-AA2:0','meizu-huawei-AA2:1','meizu-huawei-AA2:2',
   'wps-huawei-BD18:0','wps-huawei-BD20:0','honor-huawei-BD19:0','honor-huawei-BD19:1']){
  assert(validScope({...scope,scopeLabel:label,image_specs:double}));
  assert(!validScope({...scope,scopeLabel:label}));
 }
 for(const label of ['meizu-huawei-AA2:3','xiaomi-huawei-BC3:3','vivo-huawei-W4:0'])
  assert(!validScope({...scope,scopeLabel:label,image_specs:double}));
 let closes=0,waits=0;
 const page={goto:async()=>{},waitForFunction:async(fn)=>{
  assert(!String(fn).includes('nodes.length'),'A zero-image note must not wait for two image elements.');waits++;
 },evaluate:async(fn)=>fn===inspectEditor?{checks:{route:true,detail:true,body:true,group:true,styles:true}}:undefined,
 locator:()=>({count:async()=>0,evaluateAll:async()=>[]})};
 const browser={newContext:async()=>({route:async()=>{},addCookies:async()=>{},newPage:async()=>page}),
  close:async()=>{closes++}};
 const runtime={chromium:{launch:async()=>browser},timeoutMs:500};
 const result=await runBatch({scopes:[scope],cookies:[{name:'synthetic',value:'synthetic'}]},runtime);
 assert.equal(result.status,'verified');assert.equal(result.items[0].images,0);
 assert.equal(result.items[0].decoded,0);assert.equal(waits,2);assert.equal(closes,1);
 await assert.rejects(()=>runBatch({scopes:Array(5).fill(scope),cookies:[{name:'synthetic',value:'synthetic'}]},runtime),/scope/);
 assert.equal(closes,1,'Maximum batch size must remain four.');
})().catch(error=>{console.error(error);process.exitCode=1});
"""
    result = subprocess.run(['node', '-e', script], cwd=ROOT, capture_output=True, text=True, timeout=8)
    assert result.returncode == 0, result.stderr
