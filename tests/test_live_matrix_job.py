import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from note_bridge.errors import BridgeError
from note_bridge.models import Block, NoteDocument, PlatformId, Span
from note_bridge.providers.base import CreatedNote, Snapshot

scripts = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(scripts))
spec = importlib.util.spec_from_file_location("matrix_job_test", scripts / "live-matrix-job.py")
matrix = importlib.util.module_from_spec(spec)
spec.loader.exec_module(matrix)


def source(identifier="a"):
    return NoteDocument(platform=PlatformId.HONOR, account_id="source", source_id=identifier,
                        title="笔记互迁验收 · " + identifier, blocks=[Block(spans=[Span(text="中文😀", bold=True)])])


def test_different_platform_jobs_cannot_bypass_the_single_pending_write_guard(tmp_path):
    private = tmp_path / ".private"
    private.mkdir()
    (private / "lab-matrix-job.json").write_text(json.dumps({"target": "wps", "armed": True}), "utf-8")
    (private / "lab-write-job.json").write_text(json.dumps({"target": "meizu", "armed": True}), "utf-8")
    with pytest.raises(BridgeError) as error:
        matrix.run_armed_job("meizu", [], tmp_path)
    assert error.value.code == "conflicting_write_jobs"


@pytest.mark.parametrize("mode", ["duplicate", "mixed", "oppo"])
def test_whole_batch_scope_is_checked_before_provider_or_write(monkeypatch, mode):
    notes = [source(), source("b")]
    if mode == "duplicate":
        notes[1] = notes[0]
    elif mode == "mixed":
        notes[1].account_id = "different"
    else:
        for note in notes:
            note.platform = PlatformId.OPPO
    queue = iter(notes)
    monkeypatch.setattr(matrix, "select_source", lambda *args: next(queue))
    with pytest.raises(BridgeError):
        matrix.select_batch({"target": "meizu", "sources": [{}, {}]}, None, None, None)


def test_comparison_cannot_hide_a_changed_cell_or_lost_style():
    note = source()
    note.blocks.append(Block(kind="table", rows=[["x", ""], ["", "y"]]))
    restored = note.model_copy(deep=True)
    assert matrix.compare_note(note, restored)
    restored.blocks[-1].rows = [["x", "y"], ["", ""]]
    assert not matrix.compare_note(note, restored)


def test_wps_loss_requires_exact_report_and_preserves_remaining_style():
    note = source()
    note.blocks[0].spans[0].strike = True
    target = note.model_copy(deep=True)
    target.platform = PlatformId.WPS
    target.blocks[0].spans[0].strike = False
    loss = "WPS 便签不保留删除线、高亮、行内代码或超链接样式，已保留文字。"
    assert not matrix.compare_note(note, target)
    assert matrix.compare_note(note, target, [loss])
    target.blocks[0].spans[0].bold = False
    assert not matrix.compare_note(note, target, [loss]), "Reported strike loss cannot excuse unrelated bold loss."
    restored = note.model_copy(deep=True)
    restored.blocks[0].spans[0].bold = False
    assert not matrix.compare_note(note, restored)


@pytest.mark.parametrize("platform,loss", [
    ("meizu", "表格转换为制表符分隔文本。"),
    ("wps", "WPS 表格已按行和制表符转换为文字。"),
    ("honor", "荣耀表格、代码块、引用或分隔线已转换为普通段落。"),
    ("xiaomi", "表格转换为制表符分隔文本。"),
])
def test_table_downgrade_keeps_empty_column_coordinates(platform, loss):
    note = source()
    note.blocks = [Block(kind="table", rows=[["x", "", "y"], ["", "z", ""]])]
    target = note.model_copy(deep=True)
    target.platform = platform
    target.blocks = ([Block(spans=[Span(text="x\t\ty")]), Block(spans=[Span(text="\tz\t")])]
                     if platform in ("wps", "xiaomi") else [Block(spans=[Span(text="x\t\ty\n\tz\t")])])
    warnings = [loss]
    if platform == "wps":
        warnings.append("WPS 区块内换行已转换为独立段落，跨行样式需核对。")
    assert not matrix.compare_note(note, target)
    assert matrix.compare_note(note, target, warnings)
    target.blocks[0].spans[0].text = target.blocks[0].spans[0].text.replace("\t\t", "\t")
    assert not matrix.compare_note(note, target, warnings), "A reported table downgrade cannot excuse shifted columns."


def test_wps_address_report_requires_exact_url_and_preserves_label_style():
    note = source()
    note.blocks[0].spans[0].link = "https://example.org/source"
    target = note.model_copy(deep=True)
    target.platform = PlatformId.WPS
    target.blocks[0].spans[0].link = None
    report = ["WPS 超链接已转换为链接文字及明文地址，原链接样式未保留。"]
    assert not matrix.compare_note(note, target, report), "Reporting a URL is not evidence that it survived."
    target.blocks[0].spans.append(Span(text=" (https://example.org/source)"))
    assert matrix.compare_note(note, target, report)
    target.blocks[0].spans[-1].text = " (https://example.org/wrong)"
    assert not matrix.compare_note(note, target, report)
    target.blocks[0].spans[-1].text = " (https://example.org/source)"
    target.blocks[0].spans[0].bold = False
    assert not matrix.compare_note(note, target, report)


def test_huawei_numbered_text_must_preserve_order_and_bold():
    note = source()
    note.blocks = [Block(kind="list", ordered=True, spans=[Span(text="first", bold=True)]),
                   Block(kind="list", ordered=True, spans=[Span(text="second")])]
    target = note.model_copy(deep=True)
    target.platform = PlatformId.HUAWEI
    target.blocks = [Block(spans=[Span(text="1. "), Span(text="first", bold=True)]), Block(spans=[Span(text="2. second")])]
    loss = ["华为列表已转换为带编号或符号的普通段落。"]
    assert not matrix.compare_note(note, target)
    assert matrix.compare_note(note, target, loss)
    target.blocks.reverse()
    assert not matrix.compare_note(note, target, loss)


def test_batch_reads_target_only_twice_and_consumed_job_never_repeats(tmp_path, monkeypatch):
    private = tmp_path / ".private"
    (private / "evidence").mkdir(parents=True)
    (tmp_path / "scripts").mkdir()
    # Use the real fixture loader; source selection is independently covered by its contract tests.
    (tmp_path / "scripts/live-fixture-job.py").write_bytes((scripts / "live-fixture-job.py").read_bytes())
    notes = [source(), source("b")]
    monkeypatch.setattr(matrix, "select_batch", lambda *args: notes)
    account = "a" * 64
    job = {"id": "matrix-20260906-test", "kind": "cloud-matrix-batch", "target": "meizu",
           "expected_account": account, "sources": [{}, {}], "armed": True, "source_policy": "direct_seed_only"}
    path = private / "lab-matrix-job.json"
    path.write_text(json.dumps(job), "utf-8")
    rows = [source("original").model_copy(update={"platform": PlatformId.MEIZU, "account_id": account})]
    calls = []

    def fetch(context):
        calls.append("read")
        return Snapshot(list(rows), True)

    def create(note, context):
        assert json.loads(path.read_text("utf-8"))["armed"] is False
        baseline = json.loads((private / "checkpoints/matrix-20260906-test-before.json").read_text("utf-8"))
        assert list(baseline["target"]) == ["original"], "A later network failure must not erase the pre-write comparison baseline."
        calls.append("write")
        identifier = "target-" + note.source_id
        rows.append(note.model_copy(update={"platform": PlatformId.MEIZU, "account_id": account, "source_id": identifier}))
        return CreatedNote([identifier])

    provider = SimpleNamespace(spec=SimpleNamespace(id="meizu"), account_id=account, probe=lambda: account,
                               preflight=lambda notes: None, fetch=fetch, create=create, close=lambda: None)
    monkeypatch.setattr(matrix, "create_provider", lambda *args: provider)
    report = matrix.run_armed_job("meizu", [], tmp_path)
    assert report["status"] == "api_verified" and report["original_target_notes_unchanged"]
    assert report["formal_acceptance"] is False and report["official_rendering"] == "pending"
    assert calls == ["read", "write", "write", "read"]
    assert matrix.run_armed_job("meizu", [], tmp_path) is None
    assert len(rows) == 3


def test_legacy_armed_batch_is_consumed_without_new_provider_or_writes(tmp_path, monkeypatch):
    private = tmp_path / ".private"
    private.mkdir()
    path = private / "lab-matrix-job.json"
    path.write_text(json.dumps({"kind": "cloud-matrix-batch", "target": "wps", "armed": True}), "utf-8")
    monkeypatch.setattr(matrix, "create_provider", lambda *a: pytest.fail("Legacy jobs cannot write"))
    with pytest.raises(BridgeError) as error:
        matrix.run_armed_job("wps", [], tmp_path)
    assert error.value.code == "invalid_fixture_source"
    assert json.loads(path.read_text("utf-8"))["armed"] is False
    assert matrix.run_armed_job("wps", [], tmp_path) is None


@pytest.mark.parametrize("mode", ["batch-reference", "hidden-transfer", "wrong-account", "wrong-platform"])
def test_direct_seed_policy_rejects_transfer_ancestry_before_selecting_any_note(tmp_path, monkeypatch, mode):
    directory = tmp_path / ".private/checkpoints"
    directory.mkdir(parents=True)
    original = {"kind": "independent-cloud-write-smoke", "target": "vivo", "expected_account": "account"}
    ref = {"manifest": "original-job.json", "platform": "vivo", "account": "account"}
    if mode == "batch-reference":
        ref = {**ref, "batch_manifest": "old-transfer-job.json"}
    elif mode == "hidden-transfer":
        original["from_cloud_fixture"] = {"manifest": "earlier-source-job.json"}
    elif mode == "wrong-account":
        original["expected_account"] = "other"
    else:
        original["target"] = "honor"
    (directory / "original-job.json").write_text(json.dumps(original), "utf-8")
    monkeypatch.setattr(matrix, "select_source", lambda *a: pytest.fail("Ancestry rejected before note selection"))
    with pytest.raises(BridgeError) as error:
        matrix.select_batch({"target": "wps", "source_policy": "direct_seed_only",
                             "sources": [{"from_cloud_fixture": ref}]}, None, tmp_path, None)
    assert error.value.code == "invalid_fixture_source"


@pytest.mark.parametrize("mode", ["valid", "missing", "expired", "different-account", "unverified"])
def test_xiaomi_image_batch_needs_fresh_account_bound_nonencrypted_evidence(tmp_path, monkeypatch, mode):
    from note_bridge.models import Attachment, account_fingerprint
    from note_bridge.providers.xiaomi import XiaomiProvider

    monkeypatch.setattr("note_bridge.providers.xiaomi.time.time", lambda: 1000)
    monkeypatch.setattr("note_bridge.providers.xiaomi.time.monotonic", lambda: 1000)
    cookies = {"userId": "synthetic-user", "serviceToken": "synthetic-session"}
    transport = SimpleNamespace(cookie=cookies.get, session=object(),
                                json=lambda *a, **k: pytest.fail("Contract validation must remain local"))
    provider = XiaomiProvider(transport, tmp_path)
    provider.account_id = account_fingerprint(PlatformId.XIAOMI, cookies["userId"])
    note = source()
    note.attachments = [Attachment(id="image", name="image.png", kind="image")]
    evidence = {"kind": "xiaomi-nonencrypted-upload-scope", "account_id": provider.account_id,
                "metadata_verified": mode != "unverified", "verified_at": 999,
                "formal_acceptance": False, "cloud_writes": 0}
    if mode == "expired":
        evidence["verified_at"] = 399
    elif mode == "different-account":
        evidence["account_id"] = "someone-else"
    if mode != "missing":
        path = tmp_path / ".private/evidence/xiaomi-upload-account-scope.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(evidence), "utf-8")
    if mode == "valid":
        matrix.configure_candidate(provider, [note], tmp_path)
        assert provider.write_supported and provider.images_supported
        provider.require_unencrypted_mode()
        cookies["serviceToken"] = "different-synthetic-session"
        with pytest.raises(BridgeError) as error:
            provider.require_unencrypted_mode()
        assert error.value.code == "account_changed", "A lab contract cannot authorize a later backend session."
    else:
        with pytest.raises(BridgeError):
            matrix.configure_candidate(provider, [note], tmp_path)
        assert provider.write_supported and provider.images_supported
        with pytest.raises(BridgeError):
            provider.require_unencrypted_mode()


def test_xiaomi_reported_paragraph_conversion_cannot_hide_lost_bold_or_blank_lines():
    note = source()
    note.blocks = [Block(kind="code", spans=[Span(text="line1\n\nline2", bold=True)])]
    restored = note.model_copy(deep=True)
    restored.platform = PlatformId.XIAOMI
    restored.blocks = [Block(spans=[Span(text="line1", bold=True)]), Block(),
                       Block(spans=[Span(text="line2", bold=True)])]
    warnings = ["部分段落样式转换为普通文本。", "小米区块内换行已转换为独立段落，跨行样式需核对。"]
    assert not matrix.compare_note(note, restored)
    assert matrix.compare_note(note, restored, warnings)
    restored.blocks.pop(1)
    assert not matrix.compare_note(note, restored, warnings)


def test_xiaomi_link_roundtrip_cannot_certify_unchecked_native_link_card_layout():
    note = source()
    note.blocks[0].spans[0].link = "https://example.com/"
    restored = note.model_copy(deep=True)
    restored.platform = PlatformId.XIAOMI
    assert not matrix.compare_note(note, restored,
        ["小米超链接使用独立链接卡片，原段落内位置和样式需核对。"])


def test_xiaomi_inline_code_loss_requires_report_without_excusing_other_marks():
    note = source()
    note.blocks = [Block(spans=[Span(text="code", code=True, bold=True)])]
    restored = note.model_copy(deep=True)
    restored.platform = PlatformId.XIAOMI
    restored.blocks[0].spans[0].code = False
    warning = "小米行内代码已保留文本，等宽字体未保留。"
    assert not matrix.compare_note(note, restored)
    assert matrix.compare_note(note, restored, [warning])
    restored.blocks[0].spans[0].bold = False
    assert not matrix.compare_note(note, restored, [warning])


def test_xiaomi_native_link_card_preserves_url_and_surrounding_marks_with_reported_layout():
    note = source()
    note.blocks = [Block(spans=[Span(text="before", bold=True), Span(text="card", link="https://example.com/", italic=True),
                               Span(text="after")])]
    restored = note.model_copy(deep=True)
    restored.platform = PlatformId.XIAOMI
    restored.blocks = [Block(spans=[Span(text="before", bold=True), Span(text="after")]),
                       Block(spans=[Span(text="card", link="https://example.com/")])]
    warning = "小米超链接使用独立链接卡片，原段落内位置和样式需核对。"
    assert not matrix.compare_note(note, restored)
    assert matrix.compare_note(note, restored, [warning])
    restored.blocks[1].spans[0].link = "https://wrong.example/"
    assert not matrix.compare_note(note, restored, [warning])
    restored.blocks[1].spans[0].link = "https://example.com/"
    restored.blocks[0].spans[0].bold = False
    assert not matrix.compare_note(note, restored, [warning])
