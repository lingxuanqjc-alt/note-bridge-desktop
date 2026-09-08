"""An explicitly armed, one-use cloud fixture test for the already-running session lab.

The lab predates write commands. Its fresh Python worker can consume this reviewed local
intent while receiving an existing session through the pipe. Ordinary probes never write.
This file is not part of the native application; production write gates stay closed.
"""

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from lab_store import LabStore as Store

from note_bridge.errors import BridgeError
from note_bridge.models import Attachment, Block, NoteDocument, PlatformId, Span, TaskStatus
from note_bridge.operations import fetch_snapshot, migrate, receipt_key
from note_bridge.paths import AppPaths
from note_bridge.providers.factory import create_provider
from note_bridge.tasks import TaskRunner


def write_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), "utf-8")
    os.replace(temporary, path)


def fixture(job):
    note = NoteDocument(
        platform=PlatformId.MEIZU if job["target"] == "xiaomi" else PlatformId.XIAOMI,
        account_id="synthetic-fixture-source", source_id=job["id"],
        title=job["title"], created_at=datetime(2026, 8, 15, 8, tzinfo=timezone.utc),
        updated_at=datetime(2026, 8, 16, 9, tzinfo=timezone.utc),
        blocks=[Block(spans=[Span(text="中文 English 😀 "), Span(text="加粗", bold=True),
                            Span(text=" / "), Span(text="斜体", italic=True),
                            Span(text=" / "), Span(text="下划线", underline=True)]),
                Block(kind="todo", checked=True, spans=[Span(text="已完成验收项")]),
                Block(kind="todo", spans=[Span(text="未完成验收项")]),
                Block(kind="list", ordered=True, spans=[Span(text="第一项")]),
                Block(kind="list", ordered=True, spans=[Span(text="第二项")])],
    )
    if job.get("with_group") is True:
        suffix = {"wps": "J", "oppo": "I", "huawei": "K", "meizu": "L", "vivo": "M", "honor": "N", "xiaomi": "P"}[job["target"]]
        note.source_folder_id = "synthetic-" + job["target"] + "-folder-" + suffix
        note.source_folder_name = "笔记互迁分组验收 " + suffix
    if job.get("with_images") is True:
        from PIL import Image, ImageDraw
        resources = Path(__file__).resolve().parents[1] / ".private/session-lab/resources"
        directory = resources / "fixtures"
        directory.mkdir(parents=True, exist_ok=True)
        for index, extension in enumerate(("png", "jpg")):
            path = directory / ("vivo-upload-synthetic-" + str(index) + "." + extension)
            if not path.exists():
                picture = Image.new("RGB", (640, 240), (245, 245, 255))
                draw = ImageDraw.Draw(picture)
                draw.rectangle((20, 20, 160, 220), fill=(112, 73, 220))
                draw.rectangle((180, 20, 320, 220), fill=(40, 160, 100))
                draw.text((350, 90), "NOTE BRIDGE TEST " + str(index + 1), fill=(20, 20, 20))
                picture.save(path)
            content = path.read_bytes()
            asset = Attachment(id="synthetic-image-" + str(index), name=path.name, kind="image",
                               mime="image/png" if extension == "png" else "image/jpeg",
                               local_path=path.relative_to(resources).as_posix(), size=len(content),
                               sha256=hashlib.sha256(content).hexdigest())
            note.attachments.append(asset)
            note.blocks.insert(1 + index * 3, Block(kind="attachment", attachment_id=asset.id))
    if job.get("with_long_body") is True:
        note.blocks.append(Block(spans=[Span(text="长正文完整性 English 😀 " * 800)]))
    if job.get("content_case") in ("rich_styles", "rich_styles_plain", "complex"):
        note.blocks.extend([
            Block(kind="heading", level=2, spans=[Span(text="二级标题 · 富文本验收")]),
            Block(spans=[Span(text="删除线", strike=True), Span(text=" / "), Span(text="高亮", highlight=True),
                         Span(text=" / "), Span(text="链接文字", link="https://example.com/note-bridge")]),
            Block(spans=[Span(text="组合样式 😀", bold=True, italic=True, underline=True, strike=True, highlight=True),
                         Span(text="普通文字 <script>仅作为文本</script> & 中文" if job["content_case"] == "rich_styles" else "普通文字 & 中文")]),
        ])
    if job.get("content_case") in ("table", "complex"):
        note.blocks.append(Block(kind="table", rows=[
            ["列A", "列B", "列C"], ["中文😀", "", "A | B"], ["", "数字 42", "& < >"],
        ]))
    if job.get("content_case") == "complex":
        note.blocks.extend([
            Block(kind="quote", spans=[Span(text="引用内容 中文 English 😀")]),
            Block(kind="code", spans=[Span(text='first = "中文😀"\n\nsecond = 42')]),
            Block(kind="divider"),
            Block(spans=[Span(text="长正文完整性 English 😀 " * 800)]),
        ])
    if job.get("content_case") == "empty_title":
        note.title = ""
        note.blocks.insert(0, Block(spans=[Span(text=job["title"])]))
    return note


def install_empty_title_seed(provider, job, evidence):
    """Create one labelled body with an actually empty cloud title, lab only."""
    if job.get("target") != "meizu" or job.get("with_images"):
        raise BridgeError("invalid_fixture_job", "空标题来源先仅对魅族无附件样例开放。")
    original = provider._json

    def request(path, **kwargs):
        if path == "updatenote":
            data = kwargs.get("data", {})
            if not kwargs.get("write") or "uuid" in data or data.get("title") != job["title"]:
                raise BridgeError("invalid_fixture_job", "空标题样例不允许更新任何既有笔记。")
            kwargs = {**kwargs, "data": {**data, "title": ""}}
            evidence["empty_title_requested"] = True
        return original(path, **kwargs)

    provider._json = request


def compare_complex_readback(source, restored, warnings):
    """Require an explicit loss policy; do not fall back to the basic smoke check."""
    import importlib.util

    from matrix_expectations import expected_blocks
    if expected_blocks(source, restored.platform, warnings) is None:
        return False
    spec = importlib.util.spec_from_file_location("fixture_matrix_comparison", Path(__file__).with_name("live-matrix-job.py"))
    matrix = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(matrix)
    return matrix.compare_note(source, restored, warnings)


def run_armed_job(platform, jars, root: Path):
    path = root / ".private/lab-write-job.json"
    if not path.exists():
        return None
    job = json.loads(path.read_text("utf-8"))
    if job.get("armed") is not True or job.get("target") != platform:
        return None
    if (job.get("kind") != "independent-cloud-write-smoke" or platform not in ("meizu", "xiaomi", "vivo", "oppo", "wps", "huawei", "honor")
            or not re.fullmatch(r"fixture-\d{8}-[A-Za-z0-9-]{1,30}", job.get("id", ""))
            or not job.get("title", "").startswith("笔记互迁验收 · ")
            or not re.fullmatch(r"[0-9a-f]{64}", job.get("expected_account", ""))):
        raise BridgeError("invalid_fixture_job", "测试写入任务未通过范围检查，未执行写入。")
    if job.get("with_images") and platform not in ("vivo", "honor", "meizu", "oppo", "wps", "huawei", "xiaomi"):
        raise BridgeError("invalid_fixture_job", "图片测试仅对本次已审阅的图片上传实现开放。")
    if job.get("with_long_body") and platform != "wps":
        raise BridgeError("invalid_fixture_job", "本次长正文测试仅对 WPS 开放。")
    if job.get("content_case") not in (None, "rich_styles", "rich_styles_plain", "table", "complex", "empty_title"):
        raise BridgeError("invalid_fixture_job", "内容样例未通过范围检查，未执行写入。")
    oppo_complex_seed = (platform == "oppo" and job.get("id") == "fixture-20260907-oppo-complex-OR1"
                         and job.get("title") == "笔记互迁验收 · OPPO综合 OR1"
                         and job.get("with_images") is True and job.get("with_group") is True)
    if job.get("content_case") == "complex" and (
            (platform not in ("vivo", "wps", "huawei", "honor", "meizu", "xiaomi") and not oppo_complex_seed)
            or job.get("from_cloud_fixture") is not None):
        raise BridgeError("invalid_fixture_job", "综合种子仅允许在已确认范围内直接创建，OPPO 仅开放固定 OR1，不能复用迁入笔记。")
    if job.get("content_case") == "empty_title" and (platform != "meizu" or job.get("with_images")):
        raise BridgeError("invalid_fixture_job", "空标题来源先仅对魅族无附件样例开放。")
    if job.get("with_group") and platform not in ("oppo", "wps", "huawei", "meizu", "vivo", "honor", "xiaomi"):
        raise BridgeError("invalid_fixture_job", "本次分组测试仅对已实现分组的平台开放。")
    # Consume before even probing. A crash cannot leave an armed write for a later ordinary probe.
    job["armed"] = False
    job["consumed_at"] = datetime.now(timezone.utc).isoformat()
    write_json(path, job)
    paths = AppPaths(root / ".private/session-lab")
    store = Store(paths.database)
    runner = TaskRunner(store)
    provider = create_provider(PlatformId(platform), jars, paths.resources)
    evidence = {"kind": job["kind"], "platform": platform, "fixture_id": job["id"],
                "formal_acceptance": False, "status": "not_started"}
    evidence_path = root / ".private/evidence" / (job["id"] + ".json")
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    if platform == "xiaomi" and job.get("with_images"):
        from urllib.parse import urlsplit

        from session_discovery import shape
        original_xiaomi_json = provider._json

        def observe_xiaomi_upload_json(method, path, **kwargs):
            result = original_xiaomi_json(method, path, **kwargs)
            if path in ("/file/v2/user/request_upload_file", "/file/v2/user/commit"):
                evidence.setdefault("upload_response_shapes", []).append({"path": path, "shape": shape(result)})
                storage = result.get("storage") or {}
                kss = storage.get("kss") or {}
                evidence["upload_origins"] = [{"scheme": urlsplit(url).scheme, "host": urlsplit(url).hostname,
                                               "port": urlsplit(url).port, "path_length": len(urlsplit(url).path),
                                               "has_query": bool(urlsplit(url).query), "has_fragment": bool(urlsplit(url).fragment)}
                                              for url in kss.get("node_urls", []) if isinstance(url, str)]
                write_json(evidence_path, evidence)
            return result

        provider._json = observe_xiaomi_upload_json
    if platform == "huawei" and job.get("with_images"):
        from session_discovery import shape
        original_huawei_request = provider.transport.session.request
        def observe_huawei_request(method, url, **kwargs):
            response = original_huawei_request(method, url, **kwargs)
            if method == "POST" and any(part in url for part in ("/note/create", "/note/update", "/upload/", "UploadAttachmentProcess")):
                entry = {"http_status": response.status_code, "csrf_rotated": bool(response.headers.get("CSRFToken"))}
                try:
                    payload = response.json()
                    entry["shape"] = shape(payload)
                    code = payload.get("code", payload.get("Result", {}).get("code"))
                    entry["code"] = code if str(code).lstrip("-").isdigit() else "other"
                except (ValueError, AttributeError):
                    entry["json"] = False
                evidence.setdefault("write_http", []).append(entry)
            return response
        provider.transport.session.request = observe_huawei_request
        original_huawei_json = provider.transport.json
        def observe_huawei_json(method, path, **kwargs):
            step = "upload" if path.startswith("/proxy/v1/upload/") else path
            try:
                result = original_huawei_json(method, path, **kwargs)
            except BridgeError as error:
                evidence.setdefault("protocol_steps", []).append({"step": step, "failure_code": error.code})
                raise
            code = result.get("code", result.get("Result", {}).get("code"))
            evidence.setdefault("protocol_steps", []).append({"step": step, "shape": shape(result),
                "code": code if isinstance(code, (str, int)) and str(code).lstrip("-").isdigit() else "other"})
            return result
        provider.transport.json = observe_huawei_json
    if platform in ("meizu", "oppo") and job.get("with_images"):
        from session_discovery import shape
        original_meizu_json = provider.transport.json
        def observe_meizu_json(method, path, **kwargs):
            result = original_meizu_json(method, path, **kwargs)
            if kwargs.get("write"):
                evidence.setdefault("write_steps", []).append({"path": path, "shape": shape(result),
                    "code": result.get("returnCode", result.get("code")) if type(result.get("returnCode", result.get("code"))) is int else "other"})
            return result
        provider.transport.json = observe_meizu_json
    if platform == "honor" and job.get("with_images"):
        from session_discovery import shape

        from note_bridge.providers import honor_files
        original_metadata_json = provider.transport.json
        def observe_honor_metadata(method, path, **kwargs):
            result = original_metadata_json(method, path, **kwargs)
            if "/notepad/file/" in path:
                evidence.setdefault("metadata_steps", []).append({"step": path, "shape": shape(result),
                    "code": result.get("code") if type(result.get("code")) is int else "other"})
            return result
        provider.transport.json = observe_honor_metadata
        original_honor_request = provider.transport.session.request
        def observe_honor_request(method, url, **kwargs):
            response = original_honor_request(method, url, **kwargs)
            if method == "POST" and url.endswith("/notepad/noteSave"):
                item = {"step": "noteSave", "http_status": response.status_code}
                try:
                    payload = response.json()
                    item["shape"] = shape(payload)
                    if type(payload.get("code")) is int:
                        item["code"] = payload["code"]
                    description = str(payload.get("message", "")) + str(payload.get("desc", ""))
                    item["field_paths"] = re.findall(r'\["([A-Za-z_]{1,40})"\]', description)
                    item["recognized_fields"] = [field for field in (
                        "attachments", "attachment_type", "attach_type", "parent_uuid", "filename", "mimetype",
                        "hash", "size", "guid", "unstruct_guid", "first_attach_uuid", "null", "Integer", "String"
                    ) if field in description]
                except ValueError:
                    item["json"] = False
                evidence.setdefault("note_http", []).append(item)
            return response
        provider.transport.session.request = observe_honor_request
        original_transport = honor_files.Transport
        class ObservedHonorTransport(original_transport):
            def json(self, method, path, **kwargs):
                try:
                    result = super().json(method, path, **kwargs)
                except BridgeError as error:
                    evidence.setdefault("file_steps", []).append({"step": path, "failure_code": error.code})
                    raise
                evidence.setdefault("file_steps", []).append({"step": path, "shape": shape(result),
                    "code": result.get("code") if type(result.get("code")) is int else "other"})
                return result
        honor_files.Transport = ObservedHonorTransport
    if platform == "vivo" and job.get("with_images"):
        from session_discovery import shape

        from note_bridge.providers import vivo_files
        original_json = provider.transport.json
        def observe_metadata(method, path, **kwargs):
            result = original_json(method, path, **kwargs)
            if path.startswith("/clouddisk-api/"):
                trace = {"step": path.rsplit("/", 1)[-1], "shape": shape(result),
                         "code": result.get("code") if type(result.get("code")) is int else "other"}
                evidence.setdefault("file_steps", []).append(trace)
                if path.endswith("/preUpload.do") and isinstance(result.get("data"), dict):
                    from urllib.parse import urlsplit
                    value = urlsplit(result["data"].get("uploadUrl", ""))
                    trace["upload_origin"] = {"scheme": value.scheme, "host": value.hostname,
                                              "port": value.port, "path_empty": value.path in ("", "/"),
                                              "query_present": bool(value.query)}
            return result
        provider.transport.json = observe_metadata
        original_file_transport = vivo_files.Transport
        class ObservedFileTransport(original_file_transport):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                original_request = self.session.request
                def observe_request(method, url, **options):
                    try:
                        response = original_request(method, url, **options)
                        trace = {"http_status": response.status_code,
                                 "content_type": response.headers.get("Content-Type", "")}
                        try:
                            body = response.json()
                            trace["body_shape"] = shape(body)
                            if isinstance(body, dict) and type(body.get("code")) is int:
                                trace["body_code"] = body["code"]
                        except ValueError:
                            trace["json"] = False
                        evidence.setdefault("file_http", []).append(trace)
                        return response
                    except Exception as error:
                        evidence.setdefault("file_http", []).append({"failure_type": type(error).__name__})
                        raise
                self.session.request = observe_request

            def json(self, method, path, **kwargs):
                try:
                    result = super().json(method, path, **kwargs)
                except BridgeError as error:
                    evidence.setdefault("file_steps", []).append({"step": path.rsplit("/", 1)[-1], "failure_code": error.code})
                    raise
                evidence.setdefault("file_steps", []).append({"step": path.rsplit("/", 1)[-1],
                    "shape": shape(result), "code": result.get("code") if type(result.get("code")) is int else "other"})
                return result
        vivo_files.Transport = ObservedFileTransport
    if platform == "wps":
        original_request = provider.drive.session.request
        def observe_drive(method, url, **kwargs):
            response = original_request(method, url, **kwargs)
            if method == "POST":
                trace = {"step": "create_cloud_file", "http_status": response.status_code}
                try:
                    payload = response.json()
                    words = str(payload.get("result", "")).lower() + str(payload.get("msg", "")).lower()
                    trace["recognized_error_flags"] = [word for word in ("csrf", "login", "permission", "notfound", "file", "space", "quota", "referer") if word in words]
                except (ValueError, AttributeError):
                    trace["non_json"] = True
                evidence.setdefault("write_steps", []).append(trace)
            return response
        provider.drive.session.request = observe_drive
    if platform == "wps" and job.get("with_group"):
        original_group_json = provider._json
        evidence["group_write_requests"] = 0
        def observe_group_json(method, path, **kwargs):
            if method == "POST" and path == "set/notegroup":
                evidence["group_write_requests"] += 1
            return original_group_json(method, path, **kwargs)
        provider._json = observe_group_json
    if platform == "huawei" and job.get("with_group"):
        original_group_json = provider._json
        evidence["group_write_requests"] = 0
        def observe_huawei_group_json(path, data, **kwargs):
            if path == "notetag/create":
                evidence["group_write_requests"] += 1
            return original_group_json(path, data, **kwargs)
        provider._json = observe_huawei_group_json
    if platform == "meizu" and job.get("with_group"):
        original_group_json = provider._json
        evidence["group_write_requests"] = 0
        def observe_meizu_group_json(path, **kwargs):
            if path == "addNodeTag":
                evidence["group_write_requests"] += 1
            return original_group_json(path, **kwargs)
        provider._json = observe_meizu_group_json
    if platform == "vivo" and job.get("with_group"):
        original_group_json = provider._json
        evidence["group_write_requests"] = 0
        def observe_vivo_group_json(method, path, data=None, **kwargs):
            if path == "/noteBook/create":
                evidence["group_write_requests"] += 1
            return original_group_json(method, path, data, **kwargs)
        provider._json = observe_vivo_group_json
    if platform == "honor" and job.get("with_group"):
        original_group_json = provider._json
        evidence["group_write_requests"] = 0
        def observe_honor_group_json(method, path, **kwargs):
            if path == "notepad/note/folder/update":
                evidence["group_write_requests"] += 1
            return original_group_json(method, path, **kwargs)
        provider._json = observe_honor_group_json
    if platform == "xiaomi" and job.get("with_group"):
        original_group_json = provider._json
        evidence["group_write_requests"] = 0
        def observe_xiaomi_group_json(method, path, **kwargs):
            if path == "/note/folder" and kwargs.get("write"):
                evidence["group_write_requests"] += 1
            return original_group_json(method, path, **kwargs)
        provider._json = observe_xiaomi_group_json
    if job.get("content_case") == "empty_title":
        install_empty_title_seed(provider, job, evidence)
    try:
        if provider.probe() != job["expected_account"]:
            raise BridgeError("account_changed", "目标账号与本次测试范围不一致，未执行写入。")
        if platform == "xiaomi":
            try:
                provider.require_unencrypted_mode()
            except BridgeError:
                scope_path = root / ".private/evidence/xiaomi-upload-account-scope.json"
                scope = json.loads(scope_path.read_text("utf-8")) if scope_path.exists() else {}
                provider.confirm_upload_contract(scope)
            evidence["unencrypted_account_scope_verified"] = True
        if platform == "vivo":
            # Its large, style-degraded snapshot is separately checked. This is a scoped create/readback smoke.
            before_count = provider._statistics()["totalNotes"]
            before = {}
        else:
            runner.start("fixture_before", lambda ctx: fetch_snapshot(provider, store, ctx))
            runner.join()
            if runner.current().status != TaskStatus.SUCCEEDED:
                raise BridgeError("fixture_before_incomplete", "目标读取不完整，未执行测试写入。")
            before = {n.source_id: n.fingerprint() for n in store.notes(platform, provider.account_id)}
            before_count = len(before)
        write_json(root / ".private/checkpoints" / (job["id"] + "-before.json"),
                   {"target": before, "count": before_count, "full_fingerprints": platform != "vivo"})
        if job.get("from_cloud_fixture") is not None:
            from cloud_fixture_source import select_source
            note = select_source(job, store, root, fixture)
            evidence["direction_source"] = {"platform": note.platform, "fingerprint": note.fingerprint(),
                "origin": "confirmed cloud fixture readback", "source_post_readback": "pending"}
        else:
            note = fixture(job)
        # Only this lab instance may exercise the candidate implementation.
        provider.write_supported = True
        provider.images_supported = job.get("with_images") is True
        runner.start("migrate", lambda ctx: migrate([note], provider, store, ctx))
        runner.join()
        written = runner.current()
        receipt = store.receipt(receipt_key(note, provider))
        evidence.update(status=written.status, acknowledged_writes=written.succeeded,
                        issue_codes=sorted({i.code for i in written.issues}))
        evidence["cloud_resource_receipt_states"] = [row["status"] for row in store.resource_receipts(receipt_key(note, provider))]
        if not receipt or receipt["status"] != "confirmed":
            return evidence
        evidence["receipt_confirmed"] = True
        evidence["status"] = "readback_pending"
        if platform == "vivo":
            from note_bridge.providers.vivo import parse_entry
            guid = receipt["remote_ids"][0]
            detail = provider._json("POST", "/note/getIncludeItem/v2",
                                    {"guid": guid, "syncProtocolVersion": 200}, encrypted=True)
            if not isinstance(detail, dict) or detail.get("guid") != guid:
                raise BridgeError("detail_mismatch", "新建笔记的云端标识无法核对。")
            body = provider._json("POST", "/note/getContent/v2", {"guid": guid}, encrypted=True)
            assets = []
            def readback_assets(context):
                for resource in detail.get("resources") or []:
                    asset = provider._attachment(resource, guid, context)
                    provider._download_resource(resource, asset, context)
                    assets.append(asset)
            runner.start("fixture_assets_readback", readback_assets)
            runner.join()
            if runner.current().status != TaskStatus.SUCCEEDED:
                return evidence
            books = provider._json("POST", "/noteBook/getList", {"maxEntries": 100000})
            restored = parse_entry(detail, body, provider.account_id,
                {r["guid"]: r.get("nameNew") or r.get("name", "") for r in books}, assets)
            after = {guid: restored}
            after_count = provider._statistics()["totalNotes"]
            preserved = None
            evidence["existing_note_content_scope"] = "not_rechecked_in_this_scoped_write_test"
            old_notes = [n for n in store.notes(platform, provider.account_id) if n.source_id != guid]
            store.replace_snapshot(platform, provider.account_id, old_notes + [restored], False)
        else:
            runner.start("fixture_readback", lambda ctx: fetch_snapshot(provider, store, ctx))
            runner.join()
            if runner.current().status != TaskStatus.SUCCEEDED:
                return evidence
            after = {n.source_id: n for n in store.notes(platform, provider.account_id)}
            after_count = len(after)
            preserved = all(key in after and after[key].fingerprint() == digest for key, digest in before.items())
        matches = [after[key] for key in receipt["remote_ids"] if key in after]
        content_verified = len(matches) == 1 and matches[0].display_title == note.display_title
        if job.get("content_case") == "empty_title":
            evidence["raw_title_empty"] = len(matches) == 1 and matches[0].title == ""
            content_verified = content_verified and evidence["raw_title_empty"]
        if content_verified and job.get("with_group"):
            if platform == "wps":
                from note_bridge.providers.wps_groups import folder_key
            elif platform == "huawei":
                from note_bridge.providers.huawei_groups import folder_key
            elif platform == "meizu":
                from note_bridge.providers.meizu_groups import folder_key
            elif platform == "vivo":
                from note_bridge.providers.vivo_groups import folder_key
            elif platform == "honor":
                from note_bridge.providers.honor_groups import folder_key
            elif platform == "xiaomi":
                from note_bridge.providers.xiaomi_groups import folder_key
            else:
                from note_bridge.providers.oppo_groups import folder_key
            group_receipt = store.receipt(folder_key(note, provider.account_id))
            group_verified = (group_receipt is not None and group_receipt["status"] == "confirmed"
                              and group_receipt["remote_ids"] == [matches[0].source_folder_id]
                              and matches[0].source_folder_name == note.source_folder_name)
            evidence["group_mapping_verified"] = group_verified
            content_verified = content_verified and group_verified
        if content_verified and job.get("content_case") == "complex":
            warnings = [issue.message for issue in written.issues
                        if issue.code == "format_downgrade" and issue.note_id == note.source_id]
            images_verified = (len(note.attachments) == len(matches[0].attachments)
                               and all(asset.sha256 and sum(
                                   (item.kind, item.size, item.sha256) == (asset.kind, asset.size, asset.sha256)
                                   for item in matches[0].attachments) == 1 for asset in note.attachments))
            evidence.update(image_bytes_verified=bool(images_verified), expected_images=len(note.attachments),
                            restored_images=len(matches[0].attachments), comparison_policy="strict_matrix_expectations")
            content_verified = bool(images_verified) and compare_complex_readback(note, matches[0], warnings)
        elif content_verified:
            expected = [b.model_copy(deep=True) for b in note.blocks]
            if note.attachments:
                image_map = {}
                for asset in note.attachments:
                    matching = [item for item in matches[0].attachments
                                if item.sha256 == asset.sha256 and item.size == asset.size and item.kind == asset.kind]
                    if len(matching) == 1:
                        image_map[asset.id] = matching[0].id
                images_verified = len(image_map) == len(note.attachments) == len(matches[0].attachments)
                evidence.update(image_bytes_verified=images_verified, expected_images=len(note.attachments),
                                restored_images=len(matches[0].attachments))
                content_verified = content_verified and images_verified
                for block in expected:
                    if block.kind == "attachment":
                        block.attachment_id = image_map.get(block.attachment_id)
            if platform == "huawei":
                ordinal = 0
                for block in expected:
                    if block.kind == "list":
                        ordinal += 1
                        prefix = f"{ordinal}. " if block.ordered else "• "
                        block.kind, block.ordered = "paragraph", False
                        if block.spans:
                            block.spans[0].text = prefix + block.spans[0].text
                        content_verified = content_verified and any(i.code == "format_downgrade" for i in written.issues)
                    else:
                        ordinal = 0
            from migration_content_check import contains_content
            paragraph_breaks = platform == "wps" and any(
                issue.code == "format_downgrade" and issue.message == "WPS 区块内换行已转换为独立段落，跨行样式需核对。"
                for issue in written.issues)
            content_verified = content_verified and contains_content(matches[0].blocks, expected, paragraph_breaks=paragraph_breaks)
            content_verified = content_verified and "2026-08-15" in matches[0].plain_text
        passed = "verified_with_degradation" if written.issues else "verified"
        accepted = preserved is not False and content_verified and after_count == before_count + written.succeeded
        evidence.update(status=passed if accepted else "needs_review",
                        before_count=before_count, after_count=after_count, original_notes_unchanged=preserved,
                        content_and_style_verified=content_verified)
        return evidence
    finally:
        provider.close()
        write_json(evidence_path, evidence)
