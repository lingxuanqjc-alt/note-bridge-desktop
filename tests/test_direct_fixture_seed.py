"""Direct complex seeds need strict readback and must not recycle cloud transfers."""
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from note_bridge.errors import BridgeError
from note_bridge.models import Attachment, Block, NoteDocument, PlatformId, Span
from note_bridge.providers.base import SPECS, CreatedNote, Snapshot

scripts = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(scripts))
expected_blocks = importlib.import_module("matrix_expectations").expected_blocks

spec = importlib.util.spec_from_file_location("direct_fixture_test", scripts / "live-fixture-job.py")
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


def job_for(platform):
    return {"kind": "independent-cloud-write-smoke", "id": "fixture-20260907-direct-test",
            "target": platform, "expected_account": "a" * 64, "title": "笔记互迁验收 · 同名固定种子",
            "content_case": "complex", "armed": True}


def job_path(tmp_path, job):
    private = tmp_path / ".private"
    (private / "checkpoints").mkdir(parents=True)
    path = private / "lab-write-job.json"
    path.write_text(json.dumps(job), "utf-8")
    return path


@pytest.mark.parametrize("platform", ["wps", "huawei", "honor", "meizu", "xiaomi", "vivo"])
def test_direct_complex_whitelist_consumes_intent_before_any_account_request(tmp_path, monkeypatch, platform):
    path = job_path(tmp_path, job_for(platform))

    def stop_at_provider(*args):
        assert json.loads(path.read_text("utf-8"))["armed"] is False
        raise RuntimeError("Stopped before any network request")

    monkeypatch.setattr(fixture, "create_provider", stop_at_provider)
    with pytest.raises(RuntimeError, match="Stopped before"):
        fixture.run_armed_job(platform, [], tmp_path)


@pytest.mark.parametrize("mode", ["oppo", "derived"])
def test_complex_cannot_enable_deferred_platform_or_reuse_cloud_content(tmp_path, monkeypatch, mode):
    platform = "oppo" if mode == "oppo" else "wps"
    job = job_for(platform)
    if mode == "derived":
        job["from_cloud_fixture"] = {"manifest": "prior-transfer-job.json"}
    job_path(tmp_path, job)
    monkeypatch.setattr(fixture, "create_provider", lambda *a: pytest.fail("Scope must reject before provider creation"))
    with pytest.raises(BridgeError) as error:
        fixture.run_armed_job(platform, [], tmp_path)
    assert error.value.code == "invalid_fixture_job"


def oppo_complex_job():
    return {**job_for('oppo'), 'id': 'fixture-20260907-oppo-complex-OR1',
            'title': '笔记互迁验收 · OPPO综合 OR1', 'with_images': True, 'with_group': True}


@pytest.mark.parametrize('change', [None, {'id': 'fixture-20260906-oppo-group-I1'},
    {'title': '笔记互迁验收 · 其他笔记'}, {'with_images': False}, {'with_group': False},
    {'from_cloud_fixture': {'manifest': 'already-migrated-job.json'}}])
def test_oppo_resume_allows_only_the_new_direct_grouped_two_image_seed(tmp_path, monkeypatch, change):
    job = {**oppo_complex_job(), **(change or {})}
    path = job_path(tmp_path, job)

    def stop_at_provider(*args):
        assert change is None, 'Any scope change must be rejected before account or cloud access.'
        assert json.loads(path.read_text('utf8'))['armed'] is False
        raise RuntimeError('Stopped before network')

    monkeypatch.setattr(fixture, 'create_provider', stop_at_provider)
    with pytest.raises(RuntimeError if change is None else BridgeError):
        fixture.run_armed_job('oppo', [], tmp_path)


def test_oppo_complex_real_encoder_preserves_group_images_and_every_content_block(tmp_path, monkeypatch):
    from threading import Event

    from note_bridge.providers.oppo import OppoProvider, parse_entry
    from note_bridge.providers.oppo_groups import folder_key
    from note_bridge.storage import Store

    with monkeypatch.context() as patch:
        patch.setattr(fixture, '__file__', str(tmp_path / 'scripts/live-fixture-job.py'))
        source = fixture.fixture(oppo_complex_job())
    resources = tmp_path / '.private/session-lab/resources'
    provider = OppoProvider(SimpleNamespace(session=SimpleNamespace(headers={})), resources)
    provider.account_id = 'synthetic-oppo-account'
    assert provider.write_supported and provider.images_supported
    provider.preflight([source])
    store = Store(tmp_path / 'synthetic.sqlite')
    context = SimpleNamespace(store=store, check_cancel=lambda: None, cancelled=Event())
    groups, added, uploads = [], [], []

    def request(path, data=None, **kwargs):
        if path.endswith('group-list-new'):
            return list(groups)
        assert kwargs['write'] is True
        if path.endswith('add-group'):
            groups.append({'groupGuid': 'synthetic-group', 'groupName': data['groupName']})
            return groups[-1]
        assert path.endswith('/add') and 'recordId' not in data and 'version' not in data
        added.append(data)
        return {'recordId': 'synthetic-new-note'}

    def upload(asset, ctx):
        uploads.append(asset.id)
        return {'id': 'cloud-' + asset.id, 'type': 0, 'url': '/synthetic-blob', 'checkPayload': 'synthetic'}

    provider._json = request
    provider._files.upload = upload
    created = provider.create(source, context)
    assets = [asset.model_copy(update={'id': 'cloud-' + asset.id}) for asset in source.attachments]
    restored = parse_entry({**added[0], 'recordId': created.remote_ids[0], 'status': 0},
                           provider.account_id, {'synthetic-group': source.source_folder_name}, assets)
    assert len(groups) == len(added) == 1 and len(uploads) == len(set(uploads)) == 2
    assert store.receipt(folder_key(source, provider.account_id)) == {
        'status': 'confirmed', 'remote_ids': ['synthetic-group']}
    assert restored.source_folder_id == added[0]['groupGuid'] == 'synthetic-group'
    assert {'table', 'code', 'quote', 'divider', 'heading', 'todo', 'list', 'attachment'} <= {b.kind for b in restored.blocks}
    assert len(restored.plain_text) > 10000 and not restored.warnings
    # API/export code semantics remain intact while the native display loss is explicit.
    assert created.warnings == ["OPPO 官网将代码块显示为普通文字；代码正文及原始标记仍保留，可导出到本地。"]
    assert fixture.compare_complex_readback(source, restored, created.warnings)
    damaged = restored.model_copy(deep=True)
    next(b for b in damaged.blocks if b.kind == 'table').rows[1][1] = 'lost empty cell'
    assert not fixture.compare_complex_readback(source, damaged, created.warnings)
    assert OppoProvider.write_supported and OppoProvider.images_supported


@pytest.mark.parametrize("platform", ["wps", "huawei"])
def test_same_title_complex_seed_reuses_original_group_without_mutating_original(platform):
    old_job = {**job_for(platform), "id": "fixture-20260907-original", "with_group": True, "content_case": None}
    original = fixture.fixture(old_job)
    new = fixture.fixture({**old_job, "id": "fixture-20260907-complex", "content_case": "complex"})
    assert original.display_title == new.display_title
    assert original.source_id != new.source_id
    assert (original.platform, original.account_id, original.source_folder_id, original.source_folder_name) == (
        new.platform, new.account_id, new.source_folder_id, new.source_folder_name)
    assert len(new.plain_text) > 10000 and {"table", "code", "quote", "divider", "heading"}.issubset({b.kind for b in new.blocks})
    assert not any(b.kind == "table" for b in original.blocks)


def test_complex_never_uses_matrix_fallback_when_a_required_loss_policy_is_missing():
    note = NoteDocument(platform=PlatformId.XIAOMI, account_id="synthetic", source_id="direct",
                        title="笔记互迁验收 · 严格比较", blocks=[Block(spans=[Span(text="value", strike=True)])])
    restored = note.model_copy(update={"platform": PlatformId.WPS})
    assert expected_blocks(note, "wps", []) is None
    assert not fixture.compare_complex_readback(note, restored, [])


@pytest.mark.parametrize("platform", ["wps", "huawei"])
@pytest.mark.parametrize("fault", [None, "missing-warning", "lost-text", "changed-image"])
def test_complex_single_uses_strict_policy_and_preserves_baseline_and_receipt(
    tmp_path, monkeypatch, platform, fault,
):
    job = {**job_for(platform), "with_images": True, "with_group": True}
    path = job_path(tmp_path, job)
    note = fixture.fixture({**job, "with_images": False})
    for index in range(2):
        digest = hashlib.sha256(f"image-{index}".encode()).hexdigest()
        asset = Attachment(id=f"asset-{index}", name=f"image-{index}.png", kind="image", mime="image/png",
                           size=7, sha256=digest, local_path=f"synthetic-{index}.png")
        note.attachments.append(asset)
        note.blocks.insert(index * 2 + 1, Block(kind="attachment", attachment_id=asset.id))
    monkeypatch.setattr(fixture, "fixture", lambda _: note)
    warnings = ({
        "wps": ["WPS 便签不保留删除线、高亮、行内代码或超链接样式，已保留文字。",
                "WPS 表格已按行和制表符转换为文字。", "WPS 引用或代码区块已转换为普通文字。",
                "WPS 分隔线已转换为文字。", "WPS 区块内换行已转换为独立段落，跨行样式需核对。"],
        "huawei": ["华为删除线、高亮、代码字体或链接已保留文本，样式未保留。",
                   "华为列表已转换为带编号或符号的普通段落。", "华为标题层级、引用、代码块、表格或分隔线已转换为普通段落。"],
    })[platform]
    original = NoteDocument(platform=platform, account_id=job["expected_account"], source_id="original",
                            title="Existing original", blocks=[Block(spans=[Span(text="Leave unchanged")])])
    rows, calls = [original], []

    def fetch(context):
        calls.append("read")
        return Snapshot(list(rows), True)

    def create(source, context):
        assert json.loads(path.read_text("utf-8"))["armed"] is False
        calls.append("create")
        blocks, _ = expected_blocks(source, platform, warnings)
        restored = source.model_copy(deep=True)
        restored.platform, restored.account_id, restored.source_id = platform, job["expected_account"], "new-direct"
        restored.blocks = blocks + [Block(spans=[Span(text="2026-08-15 / 2026-08-16")])]
        restored.source_folder_id = "reused-original-folder"
        if fault == "lost-text":
            restored.blocks[-2].spans[0].text = "Truncated long body"
        elif fault == "changed-image":
            restored.attachments[0].sha256 = "0" * 64
        group_module = __import__("note_bridge.providers." + platform + "_groups", fromlist=["folder_key"])
        context.store.save_receipt(group_module.folder_key(source, job["expected_account"]), "confirmed", [restored.source_folder_id])
        rows.append(restored)
        return CreatedNote([restored.source_id], [] if fault == "missing-warning" else warnings)

    session = SimpleNamespace(request=lambda *a, **kw: None)
    provider = SimpleNamespace(spec=SPECS[PlatformId(platform)], account_id=job["expected_account"],
                               probe=lambda: job["expected_account"], preflight=lambda notes: None,
                               fetch=fetch, create=create, close=lambda: None,
                               transport=SimpleNamespace(session=session, json=lambda *a, **kw: {}),
                               drive=SimpleNamespace(session=SimpleNamespace(request=lambda *a, **kw: None)),
                               _json=lambda *a, **kw: {})
    monkeypatch.setattr(fixture, "create_provider", lambda *a: provider)
    report = fixture.run_armed_job(platform, [], tmp_path)
    assert calls == ["read", "create", "read"] and report["receipt_confirmed"]
    assert report["original_notes_unchanged"] and report["comparison_policy"] == "strict_matrix_expectations"
    assert report["content_and_style_verified"] is (fault is None)
    assert report["status"] == ("verified_with_degradation" if fault is None else "needs_review")
    assert fixture.run_armed_job(platform, [], tmp_path) is None
