"""Explicitly scoped background lab reads; credentials travel only over stdin."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import queue
import re
import sqlite3
import subprocess
import sys
import threading
import uuid
from collections import Counter
from contextlib import closing, redirect_stdout
from http.cookies import SimpleCookie
from pathlib import Path
from types import SimpleNamespace

from lab_platform_lock import PlatformLocks, atomic_json, file_lock
from lab_store import LabStore as Store

from note_bridge.errors import BridgeError
from note_bridge.paths import AppPaths
from note_bridge.providers.factory import create_provider
from note_bridge.tasks import TaskRunner

OPERATIONS = {'wps_fetch': 'wps', 'meizu_BD12_native': 'meizu', 'vivo_BD11_capture': 'vivo',
              'huawei_scoped_source_capture': 'huawei', 'huawei_xiaomi_BD13_native': 'xiaomi',
              'huawei_wps_BD14_native': 'wps', 'huawei_meizu_BD16_native': 'meizu',
              'huawei_BD13_BD17_source_post': 'huawei'}
HUAWEI_NATIVE = {'huawei_xiaomi_BD13_native': ('xiaomi', 'BD13'), 'huawei_wps_BD14_native': ('wps', 'BD14'),
                'huawei_meizu_BD16_native': ('meizu', 'BD16')}
BD12 = 'wps-meizu-BD12-job.json'
BD16 = 'huawei-meizu-BD16-job.json'
TERMINAL = frozenset(('completed', 'failed', 'blocked', 'cancelled', 'needs_review'))


def require(condition, code='invalid_parallel_read'):
    if not condition:
        raise BridgeError(code, '后台只读任务的操作、回执或精确范围未确认。')


def module(root, relative, name):
    spec = importlib.util.spec_from_file_location(name, root / relative)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def cookie_rows(jars):
    return [{'name': m.key, 'value': m.value, 'domain': m['domain'], 'path': m['path'] or '/',
             'secure': bool(m['secure'])} for jar in jars for m in jar.values()]


def cookie_jars(rows):
    jars = []
    for row in rows:
        require(set(row) == {'name', 'value', 'domain', 'path', 'secure'})
        jar = SimpleCookie()
        jar[row['name']] = row['value']
        for key in ('domain', 'path', 'secure'):
            jar[row['name']][key] = row[key]
        jars.append(jar)
    return jars


def audit(root, operation):
    """Local exact receipt binding. No discovery intents or cloud reads here."""
    from xiaomi_browser_scope import _ReadOnlyStore

    database = root / '.private/session-lab/notes.sqlite'
    with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True, timeout=15)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        return _audit(root, operation, _ReadOnlyStore(db))


def _audit(root, operation, store):
    require(operation in OPERATIONS)
    if operation in HUAWEI_NATIVE:
        return _audit_huawei_native(root, operation, store)
    if operation == 'huawei_BD13_BD17_source_post':
        from huawei_source_post import OUTPUT
        from huawei_source_post import audit as source_post_audit

        require(not (root / OUTPUT).exists(), 'source_post_already_recorded')
        return source_post_audit(root, store)
    if operation == 'huawei_scoped_source_capture':
        from huawei_scoped_source import bindings

        references, _ = bindings(store, root)
        return json.loads(json.dumps(references))
    if operation == 'vivo_BD11_capture':
        capture = module(root, '.private/checkpoints/capture-vivo-BD11.py', 'parallel_BD11')
        # JSON normalizes the tuple exact_ids before the parent/child comparison.
        return json.loads(json.dumps(capture.inspect(root, store)))
    payload = (root / '.private/checkpoints' / BD12).read_bytes()
    job = json.loads(payload)
    require(job.get('kind') == 'cloud-matrix-batch' and job.get('id') == 'matrix-20260907-wps-meizu-BD12'
            and job.get('armed') is False and job.get('target') == 'meizu'
            and job.get('source_policy') == 'direct_seed_only' and len(job.get('sources', [])) == 2)
    origins = [entry.get('from_cloud_fixture', {}) for entry in job['sources']]
    require([origin.get('manifest') for origin in origins] == ['wps-group-J2-job.json', 'wps-complex-BD1-job.json']
            and all(origin.get('platform') == 'wps' and 'batch_manifest' not in origin for origin in origins)
            and origins[0].get('account') == origins[1].get('account'))
    fixture = module(root, 'scripts/live-fixture-job.py', 'parallel_fixture')
    matrix = module(root, 'scripts/live-matrix-job.py', 'parallel_matrix')
    bindings = [matrix.confirmed_tool_binding(BD12, index, store, root, fixture.fixture) for index in (0, 1)]
    require(all(binding['platform'] == 'meizu' and binding['account'] == job['expected_account']
                and len(binding['lineage']) == 1 and not binding['lineage'][0]['lineage'] for binding in bindings))
    evidence_path = root / '.private/evidence' / (job['id'] + '.json')
    evidence_raw = evidence_path.read_bytes()
    evidence = json.loads(evidence_raw)
    require(evidence.get('batch_id') == job['id'] and evidence.get('status') == 'api_verified'
            and evidence.get('original_target_notes_unchanged') is True
            and evidence.get('only_confirmed_additions') is True and len(evidence.get('items', [])) == 2)
    for origin, item in zip(origins, evidence['items'], strict=True):
        require(item.get('source_id') == origin.get('source_id') and item.get('source_fingerprint') == origin.get('fingerprint')
                and item.get('receipt_status') == 'confirmed' and item.get('status') == 'api_verified'
                and item.get('content_verified') is True and item.get('group_verified') is True)
    return {'manifest': BD12, 'manifest_sha256': hashlib.sha256(payload).hexdigest(),
            'evidence_sha256': hashlib.sha256(evidence_raw).hexdigest(), 'bindings': bindings,
            'account_id': origins[0]['account'] if operation == 'wps_fetch' else job['expected_account']}


def _audit_huawei_native(root, operation, store):
    from huawei_scoped_source import MANIFESTS, verify_source

    from note_bridge.operations import receipt_key

    target, label = HUAWEI_NATIVE[operation]
    name, identifier = f'huawei-{target}-{label}-job.json', f'matrix-20260907-huawei-{target}-{label}'
    raw = (root / '.private/checkpoints' / name).read_bytes()
    old_raw = (root / '.private/evidence' / (identifier + '.json')).read_bytes()
    job, evidence = json.loads(raw), json.loads(old_raw)
    require(job.get('kind') == 'cloud-matrix-batch' and job.get('id') == identifier and job.get('target') == target
            and job.get('armed') is False and job.get('source_policy') == 'direct_seed_only' and len(job.get('sources', [])) == 2)
    origins = [entry.get('from_cloud_fixture', {}) for entry in job['sources']]
    require(tuple(o.get('manifest') for o in origins) == MANIFESTS
            and all(o.get('platform') == 'huawei' and 'batch_manifest' not in o for o in origins)
            and origins[0].get('scoped_proof') == origins[1].get('scoped_proof'))
    reference = origins[0].get('scoped_proof')
    require(evidence.get('batch_id') == identifier and evidence.get('source') == 'huawei' and evidence.get('target') == target
            and evidence.get('status') == 'api_verified' and evidence.get('formal_acceptance') is False
            and evidence.get('original_target_notes_unchanged') is True and evidence.get('only_confirmed_additions') is True
            and evidence.get('source_capture') == reference and evidence.get('whole_source_account_snapshot') is False
            and len(evidence.get('items', [])) == 2)
    sources = verify_source(reference, store, root)
    matrix = module(root, 'scripts/live-matrix-job.py', 'parallel_huawei_native_matrix')
    destination = SimpleNamespace(spec=SimpleNamespace(id=target), account_id=job['expected_account'])
    cached = {n.source_id: n for n in store.notes(target, destination.account_id)}
    result = []
    for entry, origin, source, item in zip(job['sources'], origins, sources, evidence['items'], strict=True):
        require((origin.get('account'), origin.get('source_id'), origin.get('fingerprint'), entry.get('title')) ==
                (source.account_id, source.source_id, source.fingerprint(), source.display_title)
                and entry.get('with_images') is True and entry.get('with_group') is True
                and item.get('source_id') == source.source_id and item.get('source_fingerprint') == source.fingerprint()
                and item.get('status') == 'api_verified' and item.get('receipt_status') == 'confirmed'
                and item.get('content_verified') is True and item.get('group_verified') is True)
        key = receipt_key(source, destination)
        receipt = store.receipt(key)
        require(receipt and receipt['status'] == 'confirmed' and len(receipt['remote_ids']) == 1)
        identity = receipt['remote_ids'][0]
        require(isinstance(identity, str) and re.fullmatch(r'\d{1,30}' if target == 'xiaomi' else r'[0-9a-f]{32}', identity))
        actual = cached.get(identity)
        warnings = [i['message'] for i in evidence.get('issues', []) if i.get('note_id') == source.source_id and i.get('code') == 'format_downgrade']
        require(actual is not None and actual.platform == target and actual.account_id == destination.account_id
                and len(actual.attachments) == 2 and matrix.compare_note(source, actual, warnings))
        groups = __import__('note_bridge.providers.' + target + '_groups', fromlist=['folder_key'])
        require(store.receipt(groups.folder_key(source, destination.account_id)) ==
                {'status': 'confirmed', 'remote_ids': [actual.source_folder_id]})
        # Only existing fixture labels enter the browser result. Derive expected
        # marks from the verified target, retaining Huawei's already-reported losses.
        styles = {}
        for text in ('加粗', '斜体', '下划线', '组合样式 😀'):
            flags = [{mark: bool(getattr(span, mark)) for mark in ('bold', 'italic', 'underline', 'strike', 'highlight')}
                     for block in actual.blocks for span in block.spans if text in span.text]
            if flags:
                require(all(value == flags[0] for value in flags))
                styles[text] = flags[0]
        require(all(text in styles for text in ('加粗', '斜体', '下划线')))
        result.append({'fixture_id': identity, 'receipt_key': key, 'target_fingerprint': actual.fingerprint(), 'styles': styles})
    require(len({item['fixture_id'] for item in result}) == 2)
    return {'manifest': name, 'manifest_sha256': hashlib.sha256(raw).hexdigest(), 'evidence_sha256': hashlib.sha256(old_raw).hexdigest(),
            'account_id': destination.account_id, 'source_capture': reference, 'items': result}


def state(directory, status, **fields):
    atomic_json(directory / 'state.json', {'job_id': directory.name, 'status': status, **fields})


def dispatch(platform, jars, root):
    """Called before the foreground probe router. Consume one explicit intent."""
    path = root / '.private/lab-parallel-read.json'
    if not path.exists():
        return None
    intent = json.loads(path.read_text('utf-8'))
    if intent.get('armed') is not True or intent.get('platform') != platform:
        return None
    with file_lock(root / '.private/parallel-read/intent.lock'):
        # The unlocked read only avoids unrelated contention; consumption still
        # depends on the latest matching intent under the original one-use lock.
        intent = json.loads(path.read_text('utf-8'))
        if intent.get('armed') is not True or intent.get('platform') != platform:
            return None
        intent['armed'] = False
        atomic_json(path, intent)  # Even malformed matched requests cannot remain armed.
        require(set(intent) == {'kind', 'platform', 'operation', 'armed'}
                and intent['kind'] == 'parallel-lab-read'
                and OPERATIONS.get(intent['operation']) == platform)
        job_id = uuid.uuid4().hex
        directory = root / '.private/parallel-read/jobs' / job_id
        directory.mkdir(parents=True)
        manifest = {'kind': intent['kind'], 'job_id': job_id, 'platform': str(platform),
                    'operation': intent['operation'], 'cloud_writes': 0, 'formal_acceptance': False,
                    'receipt_audit': audit(root, intent['operation'])}
        atomic_json(directory / 'manifest.json', manifest)
        state(directory, 'starting')
        command = [sys.executable, str(root / 'scripts/parallel_read_dispatch.py'), '--worker', job_id]
        try:
            child = subprocess.Popen(command, cwd=root, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True, encoding='utf-8',
                                     creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            child.stdin.write(json.dumps({'cookies': cookie_rows(jars)}))
            child.stdin.close()
            responses = queue.Queue(maxsize=1)
            def receive_ready():
                try:
                    responses.put(child.stdout.readline())
                finally:
                    child.stdout.close()

            threading.Thread(target=receive_ready, daemon=True).start()
            try:
                ready = json.loads(responses.get(timeout=12))
                require(ready == {'job_id': job_id, 'status': 'locked'}, 'parallel_handshake_failed')
            except (queue.Empty, ValueError, BridgeError):
                # Do not kill/relaunch an uncertain child or erase its ownership record.
                return {'kind': 'parallel-lab-read', 'job_id': job_id, 'platform': str(platform),
                        'status': 'dispatch_needs_review', 'state_file': (directory / 'state.json').relative_to(root).as_posix()}
        except (OSError, ValueError):
            state(directory, 'failed', code='spawn_or_pipe_failed')
            raise BridgeError('parallel_spawn_failed', '后台进程未确认启动，请核对该任务状态，不能自动重派。') from None
        return {'kind': 'parallel-lab-read', 'job_id': job_id, 'platform': str(platform), 'status': 'dispatched',
                'state_file': (directory / 'state.json').relative_to(root).as_posix()}


def _wps(root, jars, binding):
    from fetch_scope import write_fetch_scope

    from note_bridge.operations import fetch_snapshot

    paths = AppPaths(root / '.private/session-lab')
    store = Store(paths.database)
    provider = create_provider('wps', jars, paths.resources)
    try:
        require(provider.probe() == binding['account_id'], 'account_changed')
        runner = TaskRunner(store)
        runner.start('fetch', lambda context: fetch_snapshot(provider, store, context))
        runner.join()
        report = runner.current()
        result = {'platform': 'wps', 'operation': 'fetch', 'status': report.status, 'task_id': report.id,
                  'completed': report.completed, 'total': report.total, 'succeeded': report.succeeded,
                  'issue_counts': dict(Counter(issue.code for issue in report.issues))}
        if (report.status in ('succeeded', 'partial')
                and not ({issue.code for issue in report.issues} - {'source_warning'})):
            result['scope_file'] = write_fetch_scope(root, store, 'wps', provider.account_id, report)
        return result
    finally:
        provider.close()


def _meizu(root, jars, binding, locks, manifest=BD12):
    from meizu_matrix_scope import browser_target

    provider = create_provider('meizu', jars, root / '.private/session-lab/resources')
    try:
        require(provider.probe() == binding['account_id'], 'account_changed')
        scopes = [browser_target({'manifest': manifest, 'index': index}, root, provider.account_id) for index in (0, 1)]
    finally:
        provider.close()
    # No timeout kill: Node's bounded browser operations close their browser in
    # finally. An abnormal Node exit leaves a review marker for its process tree.
    locks.poisoned = True
    process = subprocess.run(['node', str(root / 'scripts/meizu-matrix-browser.cjs')],
                             input=json.dumps({'scopes': scopes, 'cookies': cookie_rows(jars)}),
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding='utf-8',
                             creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    if process.returncode != 0:
        locks.poisoned = True
        raise BridgeError('native_process_exit', '官网只读核对进程异常结束，需要核对其子进程是否关闭。')
    try:
        report = json.loads(process.stdout)
        require(report.get('kind') == 'meizu-matrix-native-batch' and report.get('cloud_writes') == 0
                and report.get('formal_acceptance') is False and report.get('status') in ('verified', 'needs_review', 'blocked'))
        if report['status'] == 'verified':
            require(report.get('total') == report.get('checked') == 2 and report.get('remaining') == 0
                    and len(report.get('items', [])) == 2)
            for index, (item, scope) in enumerate(zip(report['items'], scopes, strict=True)):
                require(item.get('index') == index and item.get('status') == 'content_images_styles_verified'
                        and all(item.get(key) is True for key in ('identity_matched', 'title_matched',
                                    'group_identity_matched', 'groupSelected', 'bodyTextMatches'))
                        and len(item.get('images', [])) == scope['expected_images']
                        and all(all(image.get(key) is True for key in ('identityMatched', 'decoded', 'visible', 'ratioMatches'))
                                for image in item['images'])
                        and len(item.get('styles', [])) == len(scope['expected_styles'])
                        and all(style.get('verified') is True for style in item['styles'])
                        and all(len(item.get(key, [])) == len(scope[key]) and all(value is True for value in item[key])
                                for key in ('headings', 'todos', 'lists')))
    except (ValueError, BridgeError):
        locks.poisoned = True
        raise BridgeError('native_result_invalid', '官网核对结果无法确认，未标记成功。') from None
    locks.poisoned = False
    # The existing native helper emits only booleans/counts and whitelisted diagnostics.
    report['fixture_scopes'] = [{'manifest': manifest, 'index': index} for index in (0, 1)]
    return report


def _huawei_native_checks(target, report, expected):
    checks = {'read_only': type(report.get('cloud_writes')) is int and report['cloud_writes'] == 0 and report.get('formal_acceptance') is False}
    if target == 'xiaomi':
        def bound(proof):
            fields = ('currentTree', 'noteIdentityMatches', 'loadedCurrentNote', 'widgetOwnsDom',
                      'widgetReady', 'dataReceived', 'visible', 'matched')
            rows = [r for r in (proof or {}).get('candidates', []) if r.get('matched') is True]
            return (proof or {}).get('matched') == 1 and len(rows) == 1 and all(rows[0].get(k) is True for k in fields)
        frames = [f for f in report.get('frames', []) if f.get('fixtureTitleInEditor') is True]
        checks.update(report=report.get('kind') == 'xiaomi-desktop-readonly-discovery' and report.get('status') == 'inspected'
                      and report.get('navigation') == 'loaded',
                      requested_id=report.get('scopeVersion') == 2 and report.get('requestedFixtureId') == expected['fixture_id'],
                      list_identity=report.get('fixtureOpened') is True and bool(report.get('locatorProofs'))
                      and all(p.get('matched') == 1 for p in report.get('locatorProofs', [])),
                      unique_editor=len(frames) == 1 and sum(bound(f.get('editorProof')) for f in report.get('frames', [])) == 1)
        frame = frames[0] if len(frames) == 1 else {}
        checks['editor_identity'] = (frame.get('host') == 'i.mi.com' and frame.get('path') == '/note/h5'
                                    and bound(frame.get('editorProof')) and bound(frame.get('editorProofAfter')))
        images, samples = frame.get('decodedImages', []), frame.get('styleSamples', {})
        checks['images'] = len(images) == 2 and all(all(i.get(k) is True for k in ('insideEditor', 'complete', 'visible'))
                                                     and i.get('width') == 640 and i.get('height') == 240 for i in images)
    else:
        checks.update(report=report.get('kind') == 'wps-matrix-native' and report.get('status') == 'content_images_verified',
                      editor_identity=report.get('identity_matched') is True, group=report.get('groupSelected') is True,
                      body=report.get('bodyTextMatches') is True)
        images, samples = report.get('images', []), report.get('samples', {})
        checks['images'] = len(images) == 2 and all(i.get('decoded') is True and i.get('visible') is True
            and isinstance(i.get('ratio'), (int, float)) and abs(i['ratio'] - 640 / 240) < .03 for i in images)
    for label, flags in expected['styles'].items():
        observed = samples.get(label, [])
        checks['style:' + label] = bool(observed) and all(all(row.get(mark) is value for mark, value in flags.items()) for row in observed)
    return checks


def _huawei_native(root, directory, jars, binding, operation, locks):
    target, _ = HUAWEI_NATIVE[operation]
    provider = create_provider(target, jars, root / '.private/session-lab/resources')
    try:
        require(provider.probe() == binding['account_id'], 'account_changed')
    finally:
        provider.close()
    scopes = []
    if target == 'xiaomi':
        from xiaomi_browser_scope import browser_target

        scopes = [browser_target({'manifest': binding['manifest'], 'index': i}, root, binding['account_id']) for i in (0, 1)]
        requests = [{'mode': 'desktop-scoped-fixture', **scope} for scope in scopes]
    else:
        from xiaomi_browser_scope import _ReadOnlyStore

        # Reuse WPS's existing resolver with a private module instance and a
        # read-only transaction; do not initialize a write-capable Store here.
        resolver = module(root, 'scripts/vivo_matrix_scope.py', 'parallel_BD14_wps_scope')
        with closing(sqlite3.connect((root / '.private/session-lab/notes.sqlite').resolve().as_uri() + '?mode=ro', uri=True)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            resolver.Store = lambda *_: _ReadOnlyStore(db)
            scopes = [resolver.browser_target({'manifest': binding['manifest'], 'index': i}, root, binding['account_id'], platform='wps') for i in (0, 1)]
        requests = scopes
    require([s['fixtureId'] for s in scopes] == [item['fixture_id'] for item in binding['items']])
    items = []
    for index, (request, expected) in enumerate(zip(requests, binding['items'], strict=True)):
        locks.poisoned = True
        script = 'xiaomi-editor-discovery.cjs' if target == 'xiaomi' else 'wps-matrix-browser.cjs'
        result = subprocess.run(['node', str(root / 'scripts' / script)], input=json.dumps({**request, 'cookies': cookie_rows(jars)}),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding='utf-8',
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        require(result.returncode == 0, 'native_process_exit')
        try:
            report = json.loads(result.stdout)
            require(isinstance(report, dict), 'native_result_invalid')
            checks = _huawei_native_checks(target, report, expected)
        except (ValueError, TypeError, KeyError, AttributeError):
            raise BridgeError('native_result_invalid', '官网核对结果无法确认，未标记成功。') from None
        locks.poisoned = False  # Existing Node helpers close their browser before a normal exit.
        item = {'index': index, 'fixture_scope': {'manifest': binding['manifest'], 'index': index},
                'fixture_id': expected['fixture_id'], 'status': 'verified' if all(checks.values()) else 'needs_review',
                'checks': checks, 'report': report}
        atomic_json(directory / f'native-{index}.json', item)
        items.append(item)
        if item['status'] != 'verified':
            break
    return {'kind': 'huawei-fixed-target-native', 'platform': target, 'formal_acceptance': False, 'cloud_writes': 0,
            'status': 'verified' if len(items) == 2 and all(i['status'] == 'verified' for i in items) else 'needs_review',
            'total': 2, 'checked': len(items), 'remaining': 2 - len(items), 'items': items,
            'comparison_scope': 'exact editor identity; known title/image/style samples' if target == 'xiaomi'
                else 'exact editor identity; full ordered body excluding whitespace; image count/decoding and known styles',
            'limitations': ['API evidence binds original images and group receipts. No phone, full-layout or additional-format claims.']}


def execute(root, directory, manifest, jars, locks):
    operation, binding = manifest['operation'], manifest['receipt_audit']
    require(audit(root, operation) == binding, 'parallel_scope_changed')
    if operation == 'huawei_meizu_BD16_native':
        return _meizu(root, jars, binding, locks, manifest=BD16)
    if operation in HUAWEI_NATIVE:
        return _huawei_native(root, directory, jars, binding, operation, locks)
    if operation == 'huawei_BD13_BD17_source_post':
        from huawei_source_post import run

        return run(jars, root)
    if operation == 'wps_fetch':
        return _wps(root, jars, binding)
    if operation == 'meizu_BD12_native':
        return _meizu(root, jars, binding, locks)
    if operation == 'vivo_BD11_capture':
        capture = module(root, '.private/checkpoints/capture-vivo-BD11.py', 'parallel_BD11')
        request = {'platform': 'vivo', 'operation': operation, 'armed': True}
        control = directory / 'capture-control.json'
        atomic_json(control, request)
        return capture.run(jars, root, request, control_path=control)
    if operation == 'huawei_scoped_source_capture':
        from huawei_scoped_source import capture

        return capture(jars, root)
    raise BridgeError('invalid_parallel_read', '不支持该后台操作。')


def worker(root, job_id, input_stream=sys.stdin, ready_stream=sys.stdout):
    require(re.fullmatch(r'[0-9a-f]{32}', job_id) is not None)
    directory = root / '.private/parallel-read/jobs' / job_id
    # This is a single consumption, not a restartable queue. A second invocation
    # must neither read cloud data again nor overwrite the original result/state.
    with file_lock(directory / 'execution.lock'):
        if json.loads((directory / 'state.json').read_text('utf-8')).get('status') != 'starting':
            return
        _worker(root, directory, job_id, input_stream, ready_stream)


def _worker(root, directory, job_id, input_stream, ready_stream):
    jars = []
    try:
        manifest = json.loads((directory / 'manifest.json').read_text('utf-8'))
        require(set(manifest) == {'kind', 'job_id', 'platform', 'operation', 'cloud_writes', 'formal_acceptance', 'receipt_audit'}
                and manifest['kind'] == 'parallel-lab-read' and manifest['job_id'] == job_id
                and manifest['cloud_writes'] == 0 and manifest['formal_acceptance'] is False
                and manifest['operation'] in OPERATIONS
                and OPERATIONS[manifest['operation']] == manifest['platform'])
        payload = json.load(input_stream)
        require(set(payload) == {'cookies'} and isinstance(payload['cookies'], list))
        jars = cookie_jars(payload.pop('cookies'))
        with PlatformLocks(root, [manifest['platform']], job_id) as locks:
            state(directory, 'running', platform=manifest['platform'], operation=manifest['operation'], pid=os.getpid())
            print(json.dumps({'job_id': job_id, 'status': 'locked'}), file=ready_stream, flush=True)
            if (directory / 'cancel-requested.json').exists():
                state(directory, 'cancelled', cloud_reads_started=False)
                return
            with open(os.devnull, 'w') as quiet, redirect_stdout(quiet):
                report = execute(root, directory, manifest, jars, locks)
            atomic_json(directory / 'result.json', report)
            successful = report.get('status') in ('verified', 'succeeded', 'partial')
            if manifest['operation'] == 'wps_fetch':
                successful = successful and bool(report.get('scope_file'))
            elif manifest['operation'] == 'huawei_scoped_source_capture':
                successful = (report.get('status') == 'captured' and report.get('scope_complete') is True
                              and report.get('whole_account_snapshot') is False)
            elif manifest['operation'] == 'huawei_meizu_BD16_native':
                successful = (report.get('kind') == 'meizu-matrix-native-batch' and report.get('status') == 'verified'
                    and report.get('formal_acceptance') is False and type(report.get('cloud_writes')) is int
                    and report['cloud_writes'] == 0 and report.get('total') == report.get('checked') == 2
                    and report.get('remaining') == 0 and len(report.get('items', [])) == 2
                    and all(item.get('status') == 'content_images_styles_verified' for item in report['items'])
                    and report.get('fixture_scopes') == [{'manifest': BD16, 'index': index} for index in (0, 1)])
            elif manifest['operation'] in HUAWEI_NATIVE:
                successful = (report.get('kind') == 'huawei-fixed-target-native' and report.get('platform') == manifest['platform']
                    and report.get('formal_acceptance') is False and type(report.get('cloud_writes')) is int and report['cloud_writes'] == 0
                    and report.get('status') == 'verified' and report.get('total') == report.get('checked') == 2
                    and report.get('remaining') == 0 and len(report.get('items', [])) == 2
                    and all(i.get('index') == index and i.get('status') == 'verified' and i.get('checks')
                            and all(v is True for v in i['checks'].values()) for index, i in enumerate(report['items'])))
            elif manifest['operation'] == 'huawei_BD13_BD17_source_post':
                successful = (report.get('kind') == manifest['operation'] and report.get('status') == 'verified'
                    and report.get('scope_complete') is True and report.get('whole_account_snapshot') is False
                    and (report.get('covered_batches'), report.get('source_notes'), report.get('source_images')) == (5, 2, 4)
                    and all(type(report.get(k)) is int and report[k] == 0 for k in ('cloud_writes', 'cache_writes', 'receipt_writes')))
            state(directory, 'completed' if successful else 'needs_review', result_file='result.json',
                  cancel_requested=(directory / 'cancel-requested.json').exists(),
                  result_status=report.get('status'), cloud_writes=0)
    except BridgeError as error:
        state(directory, 'blocked', code=error.code)
    except Exception as error:
        state(directory, 'failed', code=type(error).__name__)
    finally:
        jars.clear()


def request_cancel(root, job_id):
    require(re.fullmatch(r'[0-9a-f]{32}', job_id) is not None)
    directory = root / '.private/parallel-read/jobs' / job_id
    current = json.loads((directory / 'state.json').read_text('utf-8'))
    if current['status'] not in TERMINAL:
        atomic_json(directory / 'cancel-requested.json', {'requested': True})
    return {'job_id': job_id, 'status': current['status'], 'cancel_requested': current['status'] not in TERMINAL}


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--worker':
        worker(Path(__file__).resolve().parents[1], sys.argv[2])
    else:
        raise SystemExit(2)
