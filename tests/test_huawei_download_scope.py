"""A download preparation belongs to exactly one note and one uninterrupted scope."""
import hashlib
import io
import json
import threading
from types import SimpleNamespace
from urllib.parse import unquote

import pytest
from PIL import Image

from note_bridge.errors import BridgeError, Cancelled
from note_bridge.models import Attachment
from note_bridge.providers.huawei import HuaweiProvider


def setup(tmp_path):
    stream = io.BytesIO()
    Image.new('RGB', (5, 3), 'purple').save(stream, 'PNG')
    data = stream.getvalue()
    calls, failures = [], []
    provider = HuaweiProvider(SimpleNamespace(cookie=lambda name: None), tmp_path)
    provider.account_id = 'synthetic-account'
    provider._files.config = 'synthetic-version'

    def prepare(method, path, **kwargs):
        assert path == '/proxyserver/driveFileProxy/preProcess' and method == 'POST'
        calls.append(('prepare', None))
        if failures and failures[0] == 'prepare':
            failures.pop(0)
            return {'code': 'rejected'}
        return {'code': '0'}

    def download(method, path, **kwargs):
        route = unquote(path)
        assert method == 'GET'
        calls.append(('download', route.split('/record/')[1].split('/')[0]))
        if failures and failures[0] == 'download':
            failures.pop(0)
            raise BridgeError('network_error', 'Synthetic interrupted read')
        return SimpleNamespace(iter_content=lambda _: [data], close=lambda: None)

    provider.transport.json, provider.transport.request = prepare, download
    context = SimpleNamespace(check_cancel=lambda: None, cancelled=threading.Event(), update=lambda **kwargs: None,
                              issue=lambda *args: pytest.fail('Synthetic complete notes must not lose content'))
    metadata = {'assetId': 'synthetic-asset', 'versionId': 'synthetic-revision',
                'usage': 'synthetic.png', 'resourceLength': len(data)}
    asset = Attachment(id='synthetic-asset', name='synthetic.png', kind='image', size=len(data),
                       sha256=hashlib.sha256(data).hexdigest())
    return SimpleNamespace(provider=provider, calls=calls, failures=failures, context=context,
                           metadata=metadata, asset=asset, data=data)


def test_one_note_prepares_once_but_individual_downloads_stay_independent(tmp_path):
    ctx = setup(tmp_path)
    files = ctx.provider._files
    with files.note_download_scope('note-A') as read:
        for _ in range(2):
            actual = read(ctx.asset.model_copy(), ctx.metadata, ctx.context, expected=ctx.asset)
            assert actual.sha256 == ctx.asset.sha256 and actual.size == len(ctx.data)
    assert ctx.calls == [('prepare', None), ('download', 'note-A'), ('download', 'note-A')]
    for _ in range(2):
        files.download(ctx.asset.model_copy(), 'note-A', ctx.metadata, ctx.context, expected=ctx.asset)
    assert sum(kind == 'prepare' for kind, _ in ctx.calls) == 3, 'Upload readback callers retain one preparation per independent check'


def test_note_scope_cannot_be_reused_for_other_note_or_after_exit(tmp_path):
    ctx = setup(tmp_path)
    with ctx.provider._files.note_download_scope('note-A') as read:
        read(ctx.asset.model_copy(), ctx.metadata, ctx.context)
        with pytest.raises(BridgeError) as error:
            with ctx.provider._files.note_download_scope('note-B'):
                pytest.fail('Overlapping note preparations are not allowed')
        assert error.value.code == 'attachment_scope_active'
    with pytest.raises(BridgeError) as error:
        read(ctx.asset.model_copy(), ctx.metadata, ctx.context)
    assert error.value.code == 'attachment_scope_closed'
    with ctx.provider._files.note_download_scope('note-B') as other:
        other(ctx.asset.model_copy(), ctx.metadata, ctx.context)
    assert ctx.calls == [('prepare', None), ('download', 'note-A'), ('prepare', None), ('download', 'note-B')]


@pytest.mark.parametrize('failure', ['prepare', 'download'])
def test_failure_invalidates_scope_even_if_caught_and_next_attempt_prepares_fresh(tmp_path, failure):
    ctx = setup(tmp_path)
    ctx.failures.append(failure)
    with ctx.provider._files.note_download_scope('note-A') as read:
        with pytest.raises(BridgeError):
            read(ctx.asset.model_copy(), ctx.metadata, ctx.context)
        before = list(ctx.calls)
        with pytest.raises(BridgeError) as error:
            read(ctx.asset.model_copy(), ctx.metadata, ctx.context)
        assert error.value.code == 'attachment_scope_closed' and ctx.calls == before
    with ctx.provider._files.note_download_scope('note-A') as retried:
        retried(ctx.asset.model_copy(), ctx.metadata, ctx.context)
    assert sum(kind == 'prepare' for kind, _ in ctx.calls) == 2, 'A later attempt must not reuse failed authentication preparation'


def test_empty_scope_does_not_prepare_and_cancellation_cannot_start_download(tmp_path):
    ctx = setup(tmp_path)
    with ctx.provider._files.note_download_scope('empty-note'):
        pass
    assert not ctx.calls

    def cancel():
        raise Cancelled()

    ctx.context.check_cancel = cancel
    with ctx.provider._files.note_download_scope('note-A') as read:
        with pytest.raises(Cancelled):
            read(ctx.asset.model_copy(), ctx.metadata, ctx.context)
        with pytest.raises(BridgeError) as error:
            read(ctx.asset.model_copy(), ctx.metadata, ctx.context)
        assert error.value.code == 'attachment_scope_closed'
    assert not ctx.calls


def test_account_change_invalidates_preparation_before_second_image(tmp_path):
    ctx = setup(tmp_path)
    with ctx.provider._files.note_download_scope('note-A') as read:
        read(ctx.asset.model_copy(), ctx.metadata, ctx.context)
        ctx.provider.account_id = 'other-account'
        with pytest.raises(BridgeError) as error:
            read(ctx.asset.model_copy(), ctx.metadata, ctx.context)
        assert error.value.code == 'account_changed'
    assert len(ctx.calls) == 2


def test_two_complete_fetches_prepare_each_note_anew_and_preserve_all_images(tmp_path):
    ctx = setup(tmp_path)
    provider = ctx.provider
    rows = [{'guid': name, 'kind': 'note', 'etag': 'stable'} for name in ('note-A', 'note-B')]
    provider._listing = lambda **kwargs: ({}, rows)
    provider.probe = lambda: provider.account_id

    def metadata(path, payload, **kwargs):
        if path == 'notetag/query':
            return {'rspInfo': {'noteList': []}}
        assert path == 'note/query'
        attachments = [{**ctx.metadata, 'assetId': f'image-{index}', 'usage': f'image-{index}.png'} for index in (0, 1)]
        markup = '<note>' + ''.join(f'<element type="Attachment">/images/{a["usage"]}</element>' for a in attachments) + '</note>'
        return {'rspInfo': {'guid': payload['guid'], 'kind': 'note', 'attachments': attachments,
                            'data': json.dumps({'content': {'html_content': markup}})}}

    provider._json = metadata
    for _ in range(2):
        snapshot = provider.fetch(ctx.context)
        assert snapshot.complete and len(snapshot.notes) == 2
        assert all(len(note.attachments) == 2 and all(a.sha256 == ctx.asset.sha256 for a in note.attachments)
                   for note in snapshot.notes)
    assert sum(kind == 'prepare' for kind, _ in ctx.calls) == 4, 'Preparation cannot escape its note into the next fetch'
    assert sum(kind == 'download' for kind, _ in ctx.calls) == 8
