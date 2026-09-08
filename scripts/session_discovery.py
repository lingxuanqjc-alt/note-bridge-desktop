"""Read-only protocol discovery. Output contains structure, never response values or credentials."""
import json
import re
import uuid
from pathlib import Path
from urllib.parse import parse_qsl, urljoin, urlsplit

from lxml import html

from note_bridge.providers.base import SPECS
from note_bridge.providers.transport import Transport


def shape(value, depth=0):
    if depth > 10:
        return type(value).__name__
    if isinstance(value, dict):
        return {key if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,49}", key) else "<dynamic-key>": shape(item, depth + 1)
                for key, item in list(value.items())[:100]}
    if isinstance(value, list):
        return {"count": len(value), "samples": [shape(item, depth + 1) for item in value[:2]]}
    if isinstance(value, str) and value.startswith(("{", "[")):
        try:
            return {"encoded_json": shape(json.loads(value), depth + 1)}
        except ValueError:
            pass
    return type(value).__name__


def discover(platform, jars, root: Path):
    job_path = root / ".private/lab-discovery.json"
    if not job_path.exists():
        return None
    job = json.loads(job_path.read_text("utf-8"))
    if job.get("platform") != platform or job.get("operation") == "none":
        return None
    fixed_sources = {
        "oppo_fixed_source_capture": ("oppo", "oppo_scoped_source"),
        "vivo_fixed_source_capture": ("vivo", "vivo_fixed_source"),
        "huawei_BI4_target_readback": ("huawei", "huawei_bi4_readback"),
    }
    if job.get("operation") in fixed_sources:
        from importlib import import_module

        expected_platform, module_name = fixed_sources[job["operation"]]
        if platform != expected_platform or set(job) != {"operation", "platform"}:
            raise ValueError("invalid_fixed_source_scope")
        return import_module(module_name).capture(jars, root)
    if job.get("operation") == "oppo_target_native":
        from oppo_native_run import run

        if platform != "oppo" or set(job) not in (
                {"operation", "platform", "fixture_scopes"},
                {"operation", "platform", "fixture_scopes", "repair_references"}):
            raise ValueError("invalid_oppo_native_scope")
        if "repair_references" in job:
            return run(jars, root, job["fixture_scopes"], repair_references=job["repair_references"])
        return run(jars, root, job["fixture_scopes"])
    if job.get("operation") == "oppo_BI2_BI12_source_post":
        from oppo_source_post import run

        if platform != "oppo" or set(job) != {"operation", "platform", "bi4_reference"}:
            raise ValueError("invalid_oppo_source_post_scope")
        return run(jars, root, job["bi4_reference"])
    if job.get("operation") == "huawei_target_native" and platform == "huawei":
        import subprocess

        from huawei_native_scope import browser_target

        from note_bridge.providers.factory import create_provider

        requests = job.get("fixture_scopes", [job.get("fixture_scope", {})])
        if not isinstance(requests, list) or not 1 <= len(requests) <= 4:
            raise ValueError("huawei_native_scope_unverified")
        provider = create_provider(platform, jars, root / ".private/session-lab/resources")
        try:
            account = provider.probe()
            scopes = [browser_target(request, root, account) for request in requests]
        finally:
            provider.close()
        if len({scope["fixtureId"] for scope in scopes}) != len(scopes):
            raise ValueError("huawei_native_scope_unverified")
        cookies = [{"name": m.key, "value": m.value, "domain": m["domain"],
                    "path": m["path"] or "/", "secure": bool(m["secure"])} for jar in jars for m in jar.values()]
        try:
            result = subprocess.run(["node", str(root / "scripts/huawei-matrix-browser.cjs")],
                input=json.dumps({"scopes": scopes, "cookies": cookies}), text=True, encoding="utf-8",
                capture_output=True, timeout=None)
        finally:
            cookies.clear()
        try:
            report = json.loads(result.stdout)
        except ValueError:
            report = {"kind": "huawei-exact-target-native", "status": "blocked", "code": "browser_result", "cloud_writes": 0,
                      "formal_acceptance": False}
        report["fixture_scopes"] = requests
        report["local_evidence_bindings"] = [{"scope": scope["scopeLabel"], "target_fingerprint": scope["target_fingerprint"],
                                              "evidence_sha256": scope["evidence_sha256"]} for scope in scopes]
        output = root / ".private/evidence" / ("huawei-target-native-" + uuid.uuid4().hex + ".json")
        with output.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
        return {**report, "evidence": output.relative_to(root).as_posix()}
    if job.get("operation") == "vivo_BD11_capture" and platform == "vivo":
        import importlib.util

        spec = importlib.util.spec_from_file_location("vivo_BD11_capture", root / ".private/checkpoints/capture-vivo-BD11.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.run(jars, root, job)
    if job.get("operation") == "vivo_source_scope" and platform == "vivo":
        import importlib.util

        spec = importlib.util.spec_from_file_location("vivo_source_scope", root / "scripts/live-matrix-job.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.run_vivo_source_scope(jars, root, job)
    if job.get("operation") == "vivo_AK1_empty_paragraph" and platform == "vivo":
        import hashlib
        from types import SimpleNamespace

        from note_bridge.operations import receipt_key
        from note_bridge.providers.factory import create_provider
        from note_bridge.storage import Store

        manifest = json.loads((root / ".private/checkpoints/wps-vivo-AK1-job.json").read_text("utf-8"))
        assert manifest["armed"] is False and manifest["id"] == "matrix-20260907-wps-vivo-AK1"
        source_scope = manifest["sources"][0]["from_cloud_fixture"]
        store = Store(root / ".private/session-lab/notes.sqlite")
        source = next(n for n in store.notes("wps", source_scope["account"]) if n.source_id == source_scope["source_id"])
        assert source.fingerprint() == source_scope["fingerprint"]
        target = SimpleNamespace(spec=SimpleNamespace(id="vivo"), account_id=manifest["expected_account"])
        receipt = store.receipt(receipt_key(source, target))
        assert receipt["status"] == "confirmed" and len(receipt["remote_ids"]) == 1
        provider = create_provider(platform, jars, root / ".private/session-lab/resources")
        try:
            assert provider.probe() == manifest["expected_account"]
            body = provider._json("POST", "/note/getContent/v2", {"guid": receipt["remote_ids"][0]}, encrypted=True)
            assert isinstance(body, str)
            tree = html.fragment_fromstring(body, create_parent="div")
            blank = [p for p in tree.iter("p") if len(p) == 1 and p[0].tag == "br"
                     and not (p.text or "").strip() and not (p[0].tail or "").strip()]
            report = {"kind": "vivo-AK1-empty-paragraph-inspection", "formal_acceptance": False,
                      "cloud_writes": 0, "raw_body_sha256": hashlib.sha256(body.encode()).hexdigest(),
                      "explicit_blank_paragraphs": len(blank)}
            if job.get("mode") in ("reparse", "cache"):
                import importlib.util

                from note_bridge.providers.vivo import parse_entry

                module_spec = importlib.util.spec_from_file_location("AK1_matrix_check", root / "scripts/live-matrix-job.py")
                matrix = importlib.util.module_from_spec(module_spec)
                module_spec.loader.exec_module(matrix)
                old = json.loads((root / ".private/evidence/vivo-AK1-empty-paragraph-inspection.json").read_text("utf-8"))
                assert old["raw_body_sha256"] == report["raw_body_sha256"]
                cached = next(n for n in store.notes("vivo", manifest["expected_account"]) if n.source_id == receipt["remote_ids"][0])
                detail = provider._json("POST", "/note/getIncludeItem/v2",
                    {"guid": cached.source_id, "syncProtocolVersion": 200}, encrypted=True)
                assert detail["guid"] == cached.source_id
                assert {a["guid"] for a in detail["resources"]} == {a.id for a in cached.attachments}
                restored = parse_entry(detail, body, manifest["expected_account"],
                                       {cached.source_folder_id: cached.source_folder_name}, cached.attachments)
                report.update(kind="vivo-AK1-empty-paragraph-reparse", content_verified=matrix.compare_note(source, restored),
                              source_fingerprint=source.fingerprint(), cached_fingerprint=cached.fingerprint(),
                              restored_fingerprint=restored.fingerprint(), raw_body_unchanged=True,
                              group_unchanged=restored.source_folder_id == cached.source_folder_id)
                if job.get("mode") == "cache":
                    output = root / ".private/evidence/vivo-AK1-cache-reparse.json"
                    assert not output.exists() and report["content_verified"] and report["group_unchanged"]
                    prior = json.loads((root / ".private/evidence/vivo-AK1-empty-paragraph-reparse.json").read_text("utf-8"))
                    assert prior["cached_fingerprint"] == cached.fingerprint()
                    assert prior["restored_fingerprint"] == restored.fingerprint()
                    notes = store.notes("vivo", manifest["expected_account"])
                    assert len(notes) == 552 and store.snapshot("vivo", manifest["expected_account"])["complete"]
                    before = {n.source_id: n.fingerprint() for n in notes}
                    (root / ".private/checkpoints/vivo-AK1-before-cache-reparse.json").write_text(json.dumps(before), "utf-8")
                    store.replace_snapshot("vivo", manifest["expected_account"],
                        [restored if n.source_id == restored.source_id else n for n in notes], True)
                    after = store.notes("vivo", manifest["expected_account"])
                    assert len(after) == 552 and all(n.fingerprint() == before[n.source_id]
                                                   for n in after if n.source_id != restored.source_id)
                    report.update(kind="vivo-AK1-cache-reparse", other_cached_notes_unchanged=551,
                                  cached_note_corrected=True)
                    output.write_text(json.dumps(report, indent=2), "utf-8")
                    return report
                (root / ".private/evidence/vivo-AK1-empty-paragraph-reparse.json").write_text(json.dumps(report, indent=2), "utf-8")
                return report
            (root / ".private/evidence/vivo-AK1-empty-paragraph-inspection.json").write_text(json.dumps(report, indent=2), "utf-8")
            return report
        finally:
            provider.close()
    if job.get("operation") == platform + "_matrix_browser" and platform in ("vivo", "wps", "honor", "meizu"):
        import subprocess

        if platform == "honor":
            from honor_matrix_scope import browser_target
        elif platform == "meizu":
            from meizu_matrix_scope import browser_target
        else:
            from vivo_matrix_scope import browser_target

        from note_bridge.providers.factory import create_provider

        provider = create_provider(platform, jars, root / ".private/session-lab/resources")
        try:
            account = provider.probe()
            if platform in ("honor", "meizu") and "fixture_scopes" in job:
                scopes = job["fixture_scopes"]
                if not isinstance(scopes, list) or not 1 <= len(scopes) <= 30:
                    raise ValueError("unverified_fixture_scope")
                scope = {"scopes": [browser_target(item, root, account, platform=platform) for item in scopes]}
            else:
                scope = browser_target(job.get("fixture_scope", {}), root, account, platform=platform)
        finally:
            provider.close()
        cookies = [{"name": morsel.key, "value": morsel.value, "domain": morsel["domain"],
                    "path": morsel["path"] or "/", "secure": bool(morsel["secure"])}
                   for jar in jars for morsel in jar.values()]
        result = subprocess.run(["node", str(root / "scripts" / (platform + "-matrix-browser.cjs"))],
                                input=json.dumps({**scope, "cookies": cookies}), text=True,
                                encoding="utf-8", capture_output=True,
                                # Honor's batch owns browser shutdown in its Node finally;
                                # killing that parent can strand its Chrome descendants.
                                timeout=None if platform == "honor" and "fixture_scopes" in job else 100)
        cookies.clear()
        try:
            report = json.loads(result.stdout)
        except ValueError:
            report = {"kind": platform + "-matrix-native", "status": "blocked", "code": "browser_result"}
        scope_key = "fixture_scopes" if platform in ("honor", "meizu") and "fixture_scopes" in job else "fixture_scope"
        report[scope_key] = job[scope_key]
        (root / ".private/evidence" / (platform + "-matrix-native-latest.json")).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
        return report
    if job.get("operation") == "xiaomi_complex_repair" and platform == "xiaomi":
        import importlib.util

        spec = importlib.util.spec_from_file_location("xiaomi_complex_repair", root / "scripts/repair-xiaomi-complex.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.run(jars, root)
    if job.get("operation") == "xiaomi_native_repair" and platform == "xiaomi":
        import importlib.util

        spec = importlib.util.spec_from_file_location("xiaomi_native_repair", root / "scripts/repair-xiaomi-native.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if job.get("mode") == "reconcile":
            return module.reconcile(jars, root, job.get("label", "AF"))
        return module.run(jars, root)
    if job.get("operation") == "xiaomi_repair_inspect" and platform == "xiaomi":
        import importlib.util

        spec = importlib.util.spec_from_file_location("xiaomi_repair_inspect", root / "scripts/inspect-xiaomi-repair.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.run(jars, root)
    if job.get("operation") == "xiaomi_committed_file_response" and platform == "xiaomi":
        import importlib.util

        from note_bridge.operations import receipt_key
        from note_bridge.providers.factory import create_provider
        from note_bridge.storage import Store

        intent = json.loads((root / ".private/lab-write-job.json").read_text("utf-8"))
        if intent.get("id") != "fixture-20260907-xiaomi-image-AC5" or intent.get("armed") is not False:
            raise ValueError("Readonly file inspection requires the consumed AC5 intent")
        spec = importlib.util.spec_from_file_location("xiaomi_fixture_inspection", root / "scripts/live-fixture-job.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        provider = create_provider(platform, jars, root / ".private/session-lab/resources")
        try:
            if provider.probe() != intent["expected_account"]:
                raise ValueError("Account scope differs")
            store = Store(root / ".private/session-lab/notes.sqlite")
            rows = store.resource_receipts(receipt_key(module.fixture(intent), provider))
            files = [row["resource_id"].removeprefix("xiaomi-file/") for row in rows
                     if row["resource_id"].startswith("xiaomi-file/")]
            if len(files) != 1:
                raise ValueError("Expected one committed file receipt")
            with provider.transport.session.get("https://i.mi.com/file/full",
                    params={"type": "note_img", "fileid": files[0]}, allow_redirects=False,
                    timeout=(10, 35), stream=True) as response:
                location = urlsplit(response.headers.get("Location", ""))
                report = {"kind": "xiaomi-committed-file-response", "formal_acceptance": False,
                          "cloud_writes": 0, "account_probe_passed": True, "status_code": response.status_code,
                          "content_type": response.headers.get("Content-Type", "").split(";")[0],
                          "redirect_host": location.hostname, "redirect_scheme": location.scheme,
                          "redirect_path_length": len(location.path), "redirect_has_query": bool(location.query)}
                if job.get("verify_original") is True:
                    from threading import Event
                    from types import SimpleNamespace

                    expected = module.fixture(intent).attachments[0]
                    actual = expected.model_copy(update={"id": files[0], "local_path": None})
                    provider._download(actual, SimpleNamespace(check_cancel=lambda: None, cancelled=Event()))
                    report["original_bytes_verified"] = actual.size == expected.size and actual.sha256 == expected.sha256
                    report["bytes"] = actual.size
                (root / ".private/evidence/xiaomi-AC5-file-response.json").write_text(json.dumps(report, indent=2), "utf-8")
                return report
        finally:
            provider.close()
    if job.get("operation") == "huawei_cookie_scope" and platform == "huawei":
        from note_bridge.providers.factory import create_provider
        from note_bridge.providers.transport import host_matches

        provider = create_provider(platform, jars, root / ".private/session-lab/resources")
        try:
            identity = [c for c in provider.transport.session.cookies if c.name == "userId"]
            applicable = [c for c in identity if host_matches("cloud.huawei.com", (c.domain.lstrip("."),))]
            return {"kind": "huawei-cookie-scope-counts", "cloud_writes": 0,
                    "identity_cookie_count": len(identity), "distinct_identity_count": len({c.value for c in identity}),
                    "applicable_identity_count": len(applicable), "applicable_distinct_count": len({c.value for c in applicable}),
                    "scopes": [{"domain": c.domain, "path_length": len(c.path)} for c in identity]}
        finally:
            provider.close()
    if job.get("operation") == "huawei_identity_bootstrap" and platform == "huawei":
        from contextlib import ExitStack

        from huawei_session_diagnostics import observe_huawei_session

        from note_bridge.errors import BridgeError
        from note_bridge.providers.factory import create_provider

        provider = create_provider(platform, jars, root / ".private/session-lab/resources")
        identities, results = [], []
        observers = ExitStack()
        try:
            events = (observers.enter_context(observe_huawei_session(provider.transport, request_shape=True))
                      if job.get("diagnostics") is True else None)
            # Official app 17.0.0.300 obtains userid from common/home before
            # initializing its userId header. No cookie-based probe runs here.
            cookies = [c for c in provider.transport.session.cookies if c.name == "userId"]
            cookie_values = {c.value for c in cookies}
            cookie_scope = {"identity_cookie_count": len(cookies), "distinct_identity_count": len(cookie_values),
                            "case_variant_count": sum(c.name.lower() == "userid" and c.name != "userId"
                                for c in provider.transport.session.cookies)}
            if events is not None:
                imported = list(provider.transport.session.cookies)
                cookie_scope["imported_name_counts"] = {name: sum(c.name == name for c in imported) for name in (
                    "userId", "userid", "shareToken", "CSRFToken", "JSESSIONID", "isLogin", "token", "loginID")}
                cookie_scope["imported_domain_counts"] = {domain: sum(
                    c.domain.lower().lstrip(".") == domain or c.domain.lower().endswith("." + domain)
                    for c in imported) for domain in ("huawei.com", "hicloud.com", "huaweicloud.com")}
            for path in ("/html/getCommonParam", "/html/getHomeData"):
                row = {"endpoint": path}
                try:
                    headers = {**provider._files.headers(), "Content-Type": "application/json;charset=utf-8"}
                    payload = provider.transport.json("POST", path, json={"traceId": headers["x-hw-trace-id"]},
                                                      headers=headers, write=False)
                    code, login = payload.get("code"), payload.get("isLogin")
                    identity = payload.get("userid")
                    identity = identity if isinstance(identity, str) and identity else None
                    identities.append(identity)
                    row.update(code=code if type(code) is int and -999999 <= code <= 999999 else None,
                               isLogin=login if type(login) in (str, int) and login in (0, 1, "0", "1") else None,
                               userid_present=identity is not None,
                               matches_unique_identity_cookie=(identity in cookie_values)
                                   if identity and len(cookie_values) == 1 else None)
                    results.append(row)
                except BridgeError as error:
                    safe_codes = {"session_expired", "request_forbidden", "rate_limited", "network_error",
                                  "server_error", "protocol_changed", "request_rejected"}
                    results.append({**row, "error": error.code if error.code in safe_codes else "request_failed"})
                    break
                except Exception:
                    results.append({**row, "error": "internal_error"})
                    break
            report = {"kind": "huawei-identity-bootstrap", "formal_acceptance": False, "cloud_writes": 0,
                      "account_identity_assigned": False, "cookie_scope": cookie_scope, "results": results,
                      "response_userids_equal": identities[0] == identities[1]
                          if len(identities) == 2 and all(identities) else None}
            if events is not None:
                report["events"] = events
            return report
        finally:
            identities.clear()
            observers.close()
            provider.close()
    if job.get("operation") == "xiaomi_desktop_readonly" and platform == "xiaomi":
        import base64
        import io
        import subprocess
        import time

        from PIL import Image

        from note_bridge.providers.xiaomi_files import upload_metadata
        from note_bridge.providers.factory import create_provider

        scope_provider = create_provider(platform, jars, root / ".private/session-lab/resources")
        try:
            account_id = scope_provider.probe()
        finally:
            scope_provider.close()
        fixture_bytes = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAgAAAAICAIAAABLbSncAAAAFElEQVR4nGNMibrDgA0wYRUdtBIAQ7sBqlteL2kAAAAASUVORK5CYII=")
        with Image.open(io.BytesIO(fixture_bytes)) as fixture_image:
            fixture_image.verify()
        cookies = [{"name": morsel.key, "value": morsel.value, "domain": morsel["domain"],
                    "path": morsel["path"] or "/", "secure": bool(morsel["secure"])}
                   for jar in jars for morsel in jar.values()]
        browser_scope = {}
        if job.get("mode") == "desktop-scoped-fixture":
            from xiaomi_browser_scope import browser_target
            browser_scope = browser_target(job.get("fixture_scope", {}), root, account_id)
        try:
            result = subprocess.run(["node", str(root / "scripts/xiaomi-editor-discovery.cjs")],
                input=json.dumps({"cookies": cookies, "mode": job.get("mode", "desktop-root"),
                                  **browser_scope,
                                  "expectedUploadMetadata": upload_metadata(fixture_bytes, "note-bridge-contract.png", "image/png")}), text=True,
                encoding="utf-8", capture_output=True, timeout=65)
            report = json.loads(result.stdout)
            if (job.get("mode") == "desktop-upload-contract" and report.get("status") == "inspected"
                    and report.get("uploadContracts")
                    and all(row.get("independentMetadataMatches") and not row.get("encryptionPresent")
                            for row in report["uploadContracts"])):
                proof = {"account_id": account_id, "kind": "xiaomi-nonencrypted-upload-scope",
                         "formal_acceptance": False, "cloud_writes": 0,
                         "metadata_verified": True, "verified_at": time.time()}
                (root / ".private/evidence/xiaomi-upload-account-scope.json").write_text(json.dumps(proof), "utf-8")
                report["account_scope_verified"] = True
            return report
        finally:
            cookies.clear()
    if job.get("operation") == "huawei_BD2_header_aba" and platform == "huawei":
        import importlib.util

        if set(job) != {"platform", "operation"}:
            raise ValueError("Unexpected header diagnostic scope")
        spec = importlib.util.spec_from_file_location("huawei_BD2_header_aba",
            root / ".private/checkpoints/huawei-BD2-header-aba.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.run(jars, root)
    if job.get("operation") == "huawei_BD2_exact_readonly" and platform == "huawei":
        import importlib.util

        spec = importlib.util.spec_from_file_location("huawei_BD2_exact_readonly",
            root / ".private/checkpoints/huawei-BD2-exact-readonly.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.run(jars, root)
    if job.get("operation") == "huawei_BC3_empty_paragraph" and platform == "huawei":
        from huawei_bc3_empty_paragraph import run
        return run(jars, root)
    if job.get("operation") == "huawei_matrix_recover" and platform == "huawei":
        import importlib.util
        spec = importlib.util.spec_from_file_location("huawei_matrix_recovery", root / "scripts/recover-huawei-matrix.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.run(jars, root)
    if job.get("operation") == "xiaomi_group_shapes" and platform == "xiaomi":
        from note_bridge.providers.factory import create_provider
        from note_bridge.providers.xiaomi_groups import folder_listing
        provider = create_provider(platform, jars, root / ".private/session-lab/resources")
        try:
            provider.probe()
            page = provider._page()
            rows = folder_listing(provider)
            return {"platform": platform, "operation": job["operation"],
                    "folders": shape(page.get("folders")), "active_folder_count": len(rows),
                    "statuses": sorted({str(r.get("status")) for r in page.get("folders", [])})}
        finally:
            provider.close()
    if job.get("operation") == "honor_group_shapes" and platform == "honor":
        from note_bridge.providers.factory import create_provider
        provider = create_provider(platform, jars, root / ".private/session-lab/resources")
        try:
            provider.probe()
            rows = provider._json("POST", "notepad/note/initDataBase/getFolderList")
            return {"platform": platform, "operation": job["operation"], "folders": shape(rows),
                    "types": sorted({r.get("type") for r in rows}),
                    "delete_flags": sorted({str(r.get("delete_flag")) for r in rows})}
        finally:
            provider.close()
    if job.get("operation") == "vivo_group_shapes" and platform == "vivo":
        from note_bridge.providers.factory import create_provider
        provider = create_provider(platform, jars, root / ".private/session-lab/resources")
        try:
            provider.probe()
            rows = provider._json("POST", "/noteBook/getList", {"maxEntries": 100000})
            return {"platform": platform, "operation": job["operation"], "books": shape(rows),
                    "deletion_values": sorted({r.get("deleted") for r in rows}),
                    "root_count": sum(r.get("parentGuid") in (None, "-1") for r in rows)}
        finally:
            provider.close()
    if job.get("operation") == "meizu_group_shapes" and platform == "meizu":
        from note_bridge.providers.factory import create_provider
        provider = create_provider(platform, jars, root / ".private/session-lab/resources")
        try:
            provider.probe()
            tags = provider._json("gettags")
            notes = provider._json("getnotegroups", params={"start": 0, "length": 200, "groupUuid": "-1"})
            return {"platform": platform, "operation": job["operation"], "tags": shape(tags),
                    "notes": shape(notes), "default_tag_ids": [t.get("id") for t in tags.get("data", [])
                        if str(t.get("id")) in ("-1", "0", "-2", "-3")],
                    "group_status_types": sorted({type(r.get("groupStatus")).__name__ for r in notes.get("content", [])})}
        finally:
            provider.close()
    if job.get("operation") == "huawei_group_review" and platform == "huawei":
        import importlib.util
        spec = importlib.util.spec_from_file_location("huawei_group_review", root / "scripts/huawei-group-review.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.run(jars, root)
    if job.get("operation") == "huawei_native_save" and platform == "huawei":
        import importlib.util
        spec = importlib.util.spec_from_file_location("huawei_native_job", root / "scripts/huawei-native-job.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.run(jars, root)
    if job.get("operation") == "huawei_fixture_complete" and platform == "huawei":
        import importlib.util
        spec = importlib.util.spec_from_file_location("huawei_fixture_completion", root / "scripts/repair-huawei-fixture.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.run(jars, root)
    if job.get("operation") == "wps_file_protocol" and platform == "wps":
        from note_bridge.providers.factory import create_provider
        provider = create_provider(platform, jars, root / ".private/session-lab/resources")
        try:
            provider.probe()
            data = provider.transport.json("POST", "/s3/requestdownload", json={},
                headers={"Origin": "https://note.wps.cn", "Referer": "https://note.wps.cn/"})
            return {"platform": platform, "operation": job["operation"], "download_shape": shape(data)}
        finally:
            provider.close()
    if job.get("operation") == "huawei_image_protocol" and platform == "huawei":
        import secrets
        import time
        from note_bridge.providers.factory import create_provider
        provider = create_provider(platform, jars, root / ".private/session-lab/resources")
        try:
            provider.probe()
            result = {"platform": platform, "operation": job["operation"]}
            original_request = provider.transport.session.request
            def observe_response(method, url, **options):
                response = original_request(method, url, **options)
                token = response.headers.get("CSRFToken")
                result.setdefault("response_contract", []).append({"status": response.status_code,
                    "csrf_header_present": bool(token),
                    "csrf_matches_cookie": bool(token) and token == provider.transport.cookie("CSRFToken")})
                return response
            provider.transport.session.request = observe_response
            headers = {"Origin": "https://cloud.huawei.com", "Referer": "https://cloud.huawei.com/home",
                       "Content-Type": "application/json;charset=utf-8"}
            csrf = provider.transport.cookie("CSRFToken")
            if csrf:
                headers["CSRFToken"] = csrf
            for name, path, data in [("switch", "/html/getAboutGet", {"moduleType": "notepad"}),
                                     ("common", "/html/getCommonParam", {})]:
                trace = "00001_02_" + str(int(time.time())) + "_" + "".join(str(1 + secrets.randbelow(9)) for _ in range(8))
                response = provider.transport.json("POST", path, json={**data, "traceId": trace},
                    headers=headers)
                result[name + "_shape"] = shape(response)
                description = str(response.get("info", ""))
                result[name + "_recognized_flags"] = [word for word in
                    ("CSRF", "csrf", "token", "登录", "参数", "失败", "鉴权", "非法", "trace", "error") if word in description]
                result[name + "_flags"] = {key: response[key] for key in
                    ("code", "dataSwitch", "dataVersion", "cutOverStatus", "fileProxyGrayStrategyVersionValue")
                    if key in response and (type(response[key]) is int or
                        isinstance(response[key], str) and re.fullmatch(r"(?:dataVersion=)?[0-9.]{1,25}", response[key]))}
            return result
        finally:
            provider.close()
    if job.get("operation") == "huawei_fixture_review" and platform == "huawei":
        import importlib.util
        from note_bridge.operations import receipt_key
        from note_bridge.providers.factory import create_provider
        from note_bridge.storage import Store
        manifest = json.loads((root / ".private/lab-write-job.json").read_text("utf-8"))
        if manifest.get("armed") or manifest.get("target") != platform:
            raise ValueError("wrong_fixture_scope")
        spec = importlib.util.spec_from_file_location("huawei_fixture", root / "scripts/live-fixture-job.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        provider = create_provider(platform, jars, root / ".private/session-lab/resources")
        try:
            if provider.probe() != manifest["expected_account"]:
                raise ValueError("account_changed")
            receipt = Store(root / ".private/session-lab/notes.sqlite").receipt(receipt_key(module.fixture(manifest), provider))
            listing, rows = provider._listing()
            found = [r for r in rows if r["guid"] in receipt["remote_ids"]]
            if len(found) != 1:
                report = {"operation": job["operation"], "matches": len(found), "total": len(rows)}
                report["listing_statuses"] = sorted({r.get("status") for r in rows if type(r.get("status")) is int})
                report["discard_count"] = len(listing.get("rspInfo", {}).get("discardList") or [])
                try:
                    detail = provider._json("note/query", {"guid": receipt["remote_ids"][0], "kind": "note", "ctagNoteInfo": listing.get("ctagNoteInfo", ""), "startCursor": listing.get("startCursor", "")})["rspInfo"]
                    data = json.loads(detail["data"])
                    report.update(detail_status=detail.get("status"), delete_flag=data.get("content", {}).get("delete_flag"),
                                  title_matches=data.get("content", {}).get("title") == manifest["title"], direct_detail_shape=shape(detail))
                    report["kind"] = detail.get("kind") if detail.get("kind") in ("note", "newnote", "task") else "other"
                    specific = provider._json("simplenote/query", {"noteGuids": receipt["remote_ids"][0], "newNoteGuids": "", "taskGuids": "", "status": 0})
                    report["specific_listing_shape"] = shape(specific)
                    # Official service U() queries sync using these four current cursors; no note payload is sent.
                    original_json = provider.transport.json
                    def observe_sync(method, path, **options):
                        response = original_json(method, path, **options)
                        if path == "/notepad/sync":
                            report["sync_shape"] = shape(response)
                            report["sync_success"] = response.get("Result", {}).get("code") == "0"
                        return response
                    provider.transport.json = observe_sync
                    try:
                        provider._json("sync", {key: listing.get(key, "") for key in
                            ("ctagNoteInfo", "ctagTaskInfo", "ctagNoteTag", "startCursor")})
                    except Exception as error:
                        report["sync_adapter_error"] = getattr(error, "code", type(error).__name__)
                    finally:
                        provider.transport.json = original_json
                    _, refreshed_rows = provider._listing()
                    report["after_sync_matches"] = sum(r.get("guid") in receipt["remote_ids"] for r in refreshed_rows)
                    report["after_sync_total"] = len(refreshed_rows)
                    def record_contract(record):
                        body = json.loads(record["data"])
                        content = body.get("content", {})
                        return {"body_guid_matches_envelope": body.get("guid") == record.get("guid"),
                            "body_guid_is_client_id": str(body.get("guid", "")).startswith("newNote"),
                            "note_guid_matches_envelope": record.get("noteGuid") == record.get("guid"),
                            "prefix_matches_uuid": content.get("prefix_uuid") == record.get("uuid"),
                            "unstruct_matches_luid": content.get("unstruct_uuid") == record.get("luid"),
                            "expire_time_zero": record.get("expireTime") == 0,
                            "recycle_time_zero": record.get("recycleTime") == 0,
                            "content_shape": shape(content)}
                    report["fixture_contract"] = record_contract(detail)
                    if rows:
                        normal = provider._json("note/query", {"guid": rows[0]["guid"], "kind": "note",
                            "ctagNoteInfo": listing.get("ctagNoteInfo", ""), "startCursor": listing.get("startCursor", "")})["rspInfo"]
                        report["visible_record_contract"] = record_contract(normal)
                except Exception as error:
                    report["direct_query_error"] = getattr(error, "code", type(error).__name__)
                (root / ".private/evidence" / (manifest["id"] + "-review.json")).write_text(json.dumps(report, indent=2), "utf-8")
                return report
            row = found[0]
            detail = provider._json("note/query", {"guid": row["guid"], "kind": "note", "ctagNoteInfo": listing.get("ctagNoteInfo", ""), "startCursor": listing.get("startCursor", "")})["rspInfo"]
            data = json.loads(detail["data"])
            report = {"operation": job["operation"], "total": len(rows), "matches": 1,
                "etag_matches_listing": detail.get("etag") == row.get("etag"),
                "data_guid_matches_cloud": data.get("guid") == row["guid"],
                "detail_guid_matches_cloud": detail.get("guid") == row["guid"],
                "image_count": len(detail.get("attachments") or []),
                "detail_shape": shape(detail)}
            (root / ".private/evidence" / (manifest["id"] + "-review.json")).write_text(json.dumps(report, indent=2), "utf-8")
            return report
        finally:
            provider.close()
    if job.get("operation") == "honor_fixture_review" and platform == "honor":
        import hashlib
        import importlib.util
        from note_bridge.operations import receipt_key
        from note_bridge.providers.factory import create_provider
        from note_bridge.storage import Store
        manifest = json.loads((root / ".private/lab-write-job.json").read_text("utf-8"))
        if manifest.get("armed") or manifest.get("target") != platform or not manifest.get("with_images"):
            raise ValueError("wrong_fixture_scope")
        spec = importlib.util.spec_from_file_location("honor_fixture_check", root / "scripts/live-fixture-job.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        provider = create_provider(platform, jars, root / ".private/session-lab/resources")
        try:
            if provider.probe() != manifest["expected_account"]:
                raise ValueError("account_changed")
            store = Store(root / ".private/session-lab/notes.sqlite")
            receipt = store.receipt(receipt_key(module.fixture(manifest), provider))
            if not receipt or len(receipt["remote_ids"]) != 1:
                raise ValueError("no_single_prepared_remote_id")
            details = provider._json("POST", "notepad/noteDetail", json={"noteIds": receipt["remote_ids"]})
            result = {"platform": platform, "operation": job["operation"], "receipt_status": receipt["status"],
                      "detail_shape": shape(details), "resources": []}
            for row in store.resource_receipts(receipt_key(module.fixture(manifest), provider)):
                with provider._files._transport().session.get(
                    provider._files._transport().origin + "/portal/notepad/file/singleFileDownstream",
                    params={"cloudPath": "/sync/notepad/" + row["resource_id"]},
                    headers=provider._files._headers(), timeout=(10, 35), allow_redirects=False,
                ) as response:
                    result["resources"].append({"receipt_state": row["status"], "http_status": response.status_code,
                        "bytes": len(response.content), "sha256": hashlib.sha256(response.content).hexdigest()})
            (root / ".private/evidence" / (manifest["id"] + "-review.json")).write_text(json.dumps(result, indent=2), "utf-8")
            return result
        finally:
            provider.close()
    if job.get("operation") == "honor_file_config" and platform == "honor":
        from note_bridge.providers.factory import create_provider
        provider = create_provider(platform, jars, root / ".private/session-lab/resources")
        try:
            provider.probe()
            data = provider._json("GET", "config/web")
            origins = {}
            def inspect(value, trail=""):
                if isinstance(value, dict):
                    for key, item in value.items():
                        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,49}", key):
                            inspect(item, trail + "/" + key)
                elif isinstance(value, str) and value.startswith("https://"):
                    parsed = urlsplit(value)
                    if parsed.hostname and parsed.hostname.endswith(("honor.com", "hihonor.com")):
                        origins[trail] = parsed.scheme + "://" + parsed.hostname
            inspect(data)
            return {"platform": platform, "operation": job["operation"], "shape": shape(data), "public_origins": origins}
        finally:
            provider.close()
    if job.get("operation") == "huawei_page_contract" and platform == "huawei":
        import subprocess
        from note_bridge.providers.factory import create_provider
        manifest = json.loads((root / ".private/lab-write-job.json").read_text("utf-8"))
        if manifest.get("armed") or manifest.get("target") != platform:
            raise ValueError("wrong_fixture_scope")
        provider = create_provider(platform, jars, root / ".private/session-lab/resources")
        try:
            if provider.probe() != manifest["expected_account"]:
                raise ValueError("account_changed")
        finally:
            provider.close()
        cookies = [{"name": morsel.key, "value": morsel.value, "domain": morsel["domain"],
                    "path": morsel["path"] or "/", "secure": bool(morsel["secure"])}
                   for jar in jars for morsel in jar.values()]
        result = subprocess.run(["node", str(root / "scripts/huawei-page-contract.cjs")],
            input=json.dumps({"cookies": cookies}), text=True, encoding="utf-8", capture_output=True, timeout=85)
        cookies.clear()
        report = json.loads(result.stdout)
        (root / ".private/evidence/huawei-page-contract.json").write_text(json.dumps(report, indent=2), "utf-8")
        return report
    if job.get("operation") == platform + "_fixture_browser" and platform in ("vivo", "honor", "meizu", "oppo", "wps", "huawei"):
        import subprocess
        from note_bridge.providers.factory import create_provider
        manifest = json.loads((root / ".private/lab-write-job.json").read_text("utf-8"))
        evidence = json.loads((root / ".private/evidence" / (manifest["id"] + ".json")).read_text("utf-8"))
        scope_verified = evidence.get("status") in ("verified", "verified_with_degradation")
        if manifest["id"] == "fixture-20260906-honor-wps-R2" and platform == "wps":
            from note_bridge.storage import Store

            review = json.loads((root / ".private/evidence/honor-wps-R2-semantic-review.json").read_text("utf-8"))
            store = Store(root / ".private/session-lab/notes.sqlite")
            matches = [note for note in store.notes(platform, manifest["expected_account"])
                       if note.display_title == manifest["title"] and note.fingerprint() == review.get("target_fingerprint")]
            scope_verified = (review.get("fixture_id") == manifest["id"] and review.get("status") == "verified_with_degradation"
                              and review.get("source_unchanged") is True and review.get("content_and_style_verified") is True
                              and evidence.get("receipt_confirmed") is True and evidence.get("image_bytes_verified") is True
                              and evidence.get("group_mapping_verified") is True and len(matches) == 1)
        if (manifest.get("armed") or manifest.get("target") != platform
                or not scope_verified):
            raise ValueError("unverified_fixture_scope")
        provider = create_provider(platform, jars, root / ".private/session-lab/resources")
        try:
            if provider.probe() != manifest["expected_account"]:
                raise ValueError("different_target_account")
        finally:
            provider.close()
        cookies = [{"name": morsel.key, "value": morsel.value, "domain": morsel["domain"],
                    "path": morsel["path"] or "/", "secure": bool(morsel["secure"])}
                   for jar in jars for morsel in jar.values()]
        result = subprocess.run(["node", str(root / "scripts" / (platform + "-fixture-browser.cjs"))],
                                input=json.dumps({"cookies": cookies, "title": manifest["title"], "expected_images": 2,
                                    "group_name": ("笔记互迁分组验收 N" if manifest["id"] == "fixture-20260906-honor-wps-R2" else {"wps": "笔记互迁分组验收 J", "huawei": "笔记互迁分组验收 K", "meizu": "笔记互迁分组验收 L", "vivo": "笔记互迁分组验收 M", "honor": "笔记互迁分组验收 N"}.get(platform)
                                                   if manifest.get("with_group") else None)}),
                                text=True, encoding="utf-8", capture_output=True, timeout=85)
        cookies.clear()
        try:
            report = json.loads(result.stdout)
        except ValueError:
            report = {"kind": "official-" + platform + "-fixture-browser", "status": "blocked", "code": "browser_result"}
        report["fixture_id"] = manifest["id"]
        (root / ".private/evidence" / (platform + "-image-browser.json")).write_text(json.dumps(report, indent=2), "utf-8")
        return report
    if job.get("operation") == "vivo_failed_image_check" and platform == "vivo":
        import importlib.util
        from note_bridge.errors import BridgeError
        from note_bridge.operations import receipt_key
        from note_bridge.providers.factory import create_provider
        from note_bridge.storage import Store
        manifest = json.loads((root / ".private/lab-write-job.json").read_text("utf-8"))
        if manifest.get("armed") or manifest.get("target") != platform or not manifest.get("with_images"):
            raise ValueError("wrong_fixture_scope")
        module_spec = importlib.util.spec_from_file_location("image_fixture_check", root / "scripts/live-fixture-job.py")
        module = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(module)
        provider = create_provider(platform, jars, root / ".private/session-lab/resources")
        try:
            if provider.probe() != manifest["expected_account"]:
                raise ValueError("different_target_account")
            store = Store(root / ".private/session-lab/notes.sqlite")
            key = receipt_key(module.fixture(manifest), provider)
            receipt = store.receipt(key)
            resources = store.resource_receipts(key)
            result = {"platform": platform, "operation": job["operation"], "receipt_status": receipt["status"],
                      "total_notes": provider._statistics()["totalNotes"]}
            domains = provider.transport.json("POST", "/clouddisk-api/api/suite/web/meta/fileDomain.do",
                                              json={"metaIds": [item["resource_id"] for item in resources]})
            result["file_domain_shape"] = shape(domains)
            result["file_domain_code"] = domains.get("code") if type(domains.get("code")) is int else "other"
            result["file_origins"] = []
            for row in domains.get("data", {}).get("fileDomain", []):
                value = urlsplit(row.get("domain", ""))
                result["file_origins"].append({"scheme": value.scheme, "host": value.hostname,
                                               "port": value.port, "path_empty": value.path in ("", "/"),
                                               "query_present": bool(value.query)})
            evidence = json.loads((root / ".private/evidence" / (manifest["id"] + ".json")).read_text("utf-8"))
            origins = [step["upload_origin"]["host"] for step in evidence.get("file_steps", []) if step.get("upload_origin")]
            if origins and resources:
                from note_bridge.providers.vivo_files import file_origin
                from urllib.parse import quote
                sts = provider.transport.json("POST", "/clouddisk-api/api/suite/web/meta/getStsToken.do", json={"tokenType": 1})
                origin = file_origin("https://" + origins[0])
                file_transport = Transport(origin, (urlsplit(origin).hostname,))
                result["object_read_checks"] = []
                try:
                    for mode in ("source", "thumb"):
                        endpoint = "/api/file/webdisk/" + mode + "/" + quote(resources[0]["resource_id"], safe="")
                        if mode == "thumb":
                            endpoint += "/w50h50"
                        with file_transport.session.get(origin + endpoint,
                                params={"stsToken": sts["data"]["stsToken"], "openId": provider._openid},
                                timeout=(10, 20), allow_redirects=False, stream=True) as response:
                            check = {"mode": mode, "http_status": response.status_code,
                                     "content_type": response.headers.get("Content-Type", "")}
                            if "json" in check["content_type"]:
                                payload = response.json()
                                check["code"] = payload.get("code") if type(payload.get("code")) is int else "other"
                                check["shape"] = shape(payload)
                            result["object_read_checks"].append(check)
                finally:
                    file_transport.close()
            try:
                detail = provider._json("POST", "/note/getIncludeItem/v2",
                                        {"guid": receipt["remote_ids"][0], "syncProtocolVersion": 200}, encrypted=True)
                result["prepared_note_exists"] = isinstance(detail, dict) and detail.get("guid") == receipt["remote_ids"][0]
            except BridgeError as error:
                result["prepared_note_check"] = error.code
            return result
        finally:
            provider.close()
    if job.get("operation") == "wps_failed_create_check" and platform == "wps":
        import importlib.util
        from note_bridge.operations import receipt_key
        from note_bridge.providers.factory import create_provider
        from note_bridge.storage import Store
        manifest = json.loads((root / ".private/lab-write-job.json").read_text("utf-8"))
        if manifest.get("armed") or manifest.get("target") != platform:
            raise ValueError("wrong_fixture_scope")
        module_spec = importlib.util.spec_from_file_location("fixture_check", root / "scripts/live-fixture-job.py")
        module = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(module)
        provider = create_provider(platform, jars, root / ".private/session-lab/resources")
        try:
            if provider.probe() != manifest["expected_account"]:
                raise ValueError("different_target_account")
            store = Store(root / ".private/session-lab/notes.sqlite")
            receipt = store.receipt(receipt_key(module.fixture(manifest), provider))
            if not receipt or len(receipt["remote_ids"]) != 1:
                raise ValueError("no_single_prepared_remote_id")
            guid = receipt["remote_ids"][0]
            result = {"platform": platform, "operation": job["operation"], "receipt_status": receipt["status"]}
            for key, transport, method, path, options in [
                ("drive", provider.drive, "GET", "/api/v3/files/wpsnote_file_id", {"params": {"noteid": guid}}),
                ("note", provider.transport, "POST", "/notesvr/get/noteinfo", {"json": {"noteIds": [guid]}}),
            ]:
                with transport.session.request(method, transport.origin + path, timeout=(10, 35),
                                               allow_redirects=False,
                                               headers={"Origin": "https://note.wps.cn", "Referer": "https://note.wps.cn/"},
                                               **options) as response:
                    try:
                        data = response.json()
                        result[key] = {"http_status": response.status_code, "shape": shape(data),
                                       "file_exists": bool(data.get("fileid")) if key == "drive" else None}
                        message = str(data.get("msg", "")).lower() + str(data.get("result", "")).lower()
                        result[key]["recognized_error_flags"] = [word for word in ("csrf", "login", "permission", "referer", "origin", "notfound", "not_found") if word in message]
                    except ValueError:
                        result[key] = {"http_status": response.status_code, "json": False}
            return result
        finally:
            provider.close()
    if job.get("operation") == "wps_drive_shapes" and platform == "wps":
        transport = Transport("https://drive.wps.cn", SPECS[platform].domains, jars)
        try:
            data = transport.json("GET", "/api/v3/groups/special")
            return {"platform": platform, "operation": job["operation"], "group_shape": shape(data),
                    "has_group_identity": isinstance(data.get("id"), (int, str)) and bool(data["id"])}
        finally:
            transport.close()
    if job.get("operation") == "oppo_read_shapes" and platform == "oppo":
        from note_bridge.providers.oppo import OppoProvider
        provider = OppoProvider(Transport("https://owork-api-cn.oppo.com", SPECS[platform].domains, jars),
                                root / ".private/session-lab/resources")
        diagnostic = []
        original_json = provider.transport.json
        def diagnostic_json(method, path, **kwargs):
            result = original_json(method, path, **kwargs)
            code = result.get("code")
            diagnostic.append({"path": path, "code": code if type(code) is int else "non_numeric",
                               "shape": shape(result)})
            return result
        provider.transport.json = diagnostic_json
        try:
            provider.probe()
            listing = provider._json("/web/note/v2/list", {
                "status": 0, "queryFields": [], "pageNo": 1, "pageSize": 50,
                "recordType": "hypertext_item_info", "sortFieldList": [
                    {"field": "topTime", "sortType": "desc"}, {"field": "updateTime", "sortType": "desc"}],
            })
            groups = provider._json("/web/note/v2/group-list-new")
            handwritten = provider._json("/web/paint_note/v2/list", {"status": 0, "pageNo": 1, "pageSize": 50,
                                         "recordType": "paint_note_item_info", "queryFields": [
                                             {"field": "sysVersion", "comparatorType": "greater_than", "value": "0"}],
                                         "sortFieldList": [{"field": "sysVersion", "sortType": "desc"}]})
            result = {"platform": platform, "operation": job["operation"], "listing_shape": shape(listing),
                      "groups_shape": shape(groups), "handwritten_shape": shape(handwritten)}
            if isinstance(listing, dict) and listing.get("records"):
                row = listing["records"][0]
                detail = provider._json("/web/note/v2/info", {"recordId": row["recordId"], "version": "", "status": row.get("status", 0)},
                                        params={"recordId": row["recordId"], "version": ""})
                result["detail_shape"] = shape(detail)
                if isinstance(detail, dict):
                    for key in ("content", "noteContent", "body", "rawText"):
                        if isinstance(detail.get(key), str):
                            tree = html.fragment_fromstring(detail[key] or "", create_parent="div")
                            result[key + "_tags"] = sorted({element.tag for element in tree.iter()
                                                             if isinstance(element.tag, str)})
            return result
        except Exception as error:
            return {"platform": platform, "operation": job["operation"], "status": "discovery_incomplete",
                    "code": type(error).__name__, "diagnostic": diagnostic}
        finally:
            provider.close()
    if job.get("operation") == "vivo_read_shapes" and platform == "vivo":
        from note_bridge.providers.vivo import VivoProvider
        provider = VivoProvider(Transport("https://pc.vivo.com.cn", SPECS[platform].domains, jars),
                                root / ".private/session-lab/resources")
        try:
            provider.probe()
            stats = provider._json("POST", "/statistics/note", {"type": 0, "encryptType": None})
            listing = provider._json("POST", "/note/getAllNote/v2", {
                "maxEntries": 50, "pageNum": 1, "sortField": 0, "syncProtocolVersion": 200,
            }, encrypted=True)
            result = {"platform": platform, "operation": job["operation"], "stats_shape": shape(stats),
                      "listing_shape": shape(listing)}
            if isinstance(listing, dict) and listing.get("notes"):
                note = listing["notes"][0]
                body = provider._json("POST", "/note/getContent/v2", {"guid": note["guid"]}, encrypted=True)
                result["body_shape"] = shape(body)
                if isinstance(body, str):
                    tree = html.fragment_fromstring(body or "", create_parent="div")
                    result["body_tags"] = sorted({element.tag for element in tree.iter()
                                                   if isinstance(element.tag, str)})
                result["note_types"] = sorted({row.get("type") for row in listing["notes"]
                                                 if isinstance(row.get("type"), int)})
            return result
        finally:
            provider.close()
    if job.get("operation") in ("vivo_custom_shapes", "vivo_refresh_custom") and platform == "vivo":
        from collections import Counter
        from note_bridge.providers.vivo import VivoProvider
        from note_bridge.paths import AppPaths
        from note_bridge.storage import Store
        paths = AppPaths(root / ".private/session-lab")
        provider = VivoProvider(Transport("https://pc.vivo.com.cn", SPECS[platform].domains, jars), paths.resources)
        try:
            provider.probe()
            store = Store(paths.database)
            notes = store.notes(platform, provider.account_id)
            samples = [n for n in notes
                       if any("专有" in w for w in n.warnings)]
            tags, children = Counter(), Counter()
            refreshed = {}
            for sample in samples:
                body = provider._json("POST", "/note/getContent/v2", {"guid": sample.source_id}, encrypted=True)
                if job["operation"] == "vivo_refresh_custom":
                    from note_bridge.providers.vivo import parse_entry
                    detail = provider._json("POST", "/note/getIncludeItem/v2",
                                            {"guid": sample.source_id, "syncProtocolVersion": 200}, encrypted=True)
                    note = parse_entry(detail, body, provider.account_id,
                                       {sample.source_folder_id: sample.source_folder_name}, sample.attachments)
                    if note.updated_at != sample.updated_at or note.source_id != sample.source_id:
                        raise ValueError("source_changed_during_parser_recheck")
                    refreshed[sample.source_id] = note
                tree = html.fragment_fromstring(body or "", create_parent="div")
                for element in tree.iter():
                    if isinstance(element.tag, str) and element.tag.startswith("vnote-"):
                        tags[element.tag + ":" + ",".join(sorted(element.attrib))] += 1
                        for child in element.iterdescendants():
                            if isinstance(child.tag, str):
                                flags = {k: v for k, v in child.attrib.items()
                                         if k in ("checked", "check", "state", "status", "done", "data-checked", "type")
                                         and v in ("0", "1", "2", "true", "false", "checked", "unchecked")}
                                children[child.tag + ":" + ",".join(sorted(child.attrib)) + ":" + json.dumps(flags)] += 1
            if refreshed:
                original_account = provider.account_id
                if provider.probe() != original_account:
                    raise ValueError("account_changed_during_parser_recheck")
                updated = [refreshed.get(n.source_id, n) for n in notes]
                store.replace_snapshot(platform, original_account, updated, all(not n.warnings for n in updated))
            return {"platform": platform, "operation": job["operation"], "notes": len(samples),
                    "element_shapes": dict(tags), "children_shapes": dict(children),
                    "cache_reparsed": len(refreshed),
                    "todo_blocks": sum(b.kind == "todo" for n in refreshed.values() for b in n.blocks)}
        finally:
            provider.close()
    if job.get("operation") in ("vivo_media_shapes", "vivo_media_download", "vivo_image_markup") and platform == "vivo":
        from note_bridge.providers.vivo import VivoProvider
        from note_bridge.paths import AppPaths
        from note_bridge.storage import Store
        paths = AppPaths(root / ".private/session-lab")
        provider = VivoProvider(Transport("https://pc.vivo.com.cn", SPECS[platform].domains, jars), paths.resources)
        try:
            provider.probe()
            notes = Store(paths.database).notes(platform, provider.account_id)
            sample = next(n for n in notes if n.attachments or any("媒体" in w or "附件" in w for w in n.warnings))
            detail = provider._json("POST", "/note/getIncludeItem/v2", {"guid": sample.source_id,
                                     "syncProtocolVersion": 200}, encrypted=True)
            body = provider._json("POST", "/note/getContent/v2", {"guid": sample.source_id}, encrypted=True)
            tree = html.fragment_fromstring(body or "", create_parent="div")
            media = tree.xpath(".//img|.//audio|.//video|.//source")
            if job["operation"] == "vivo_image_markup":
                from collections import Counter
                elements = []
                for element in tree.xpath(".//vnote-image")[:3]:
                    attrs = {}
                    for key, value in element.attrib.items():
                        if key in ("width", "height", "size") and re.fullmatch(r"[0-9.% ,x]+", value):
                            attrs[key] = value
                        elif key == "filename":
                            attrs[key] = {"extension": Path(value).suffix,
                                          "resource_guid_matches": sum(r.get("guid", "") in value for r in detail.get("resources", []))}
                        else:
                            attrs[key] = "present"
                    elements.append({"tag": element.tag, "attrs": attrs, "children": dict(Counter(c.tag for c in element))})
                return {"platform": platform, "operation": job["operation"], "images": elements}
            if job["operation"] == "vivo_media_download":
                from note_bridge.providers.vivo import parse_entry
                from note_bridge.tasks import TaskRunner
                from collections import Counter
                assets = []
                def work(context):
                    context.update(total=len(detail.get("resources", [])))
                    for resource in detail.get("resources", []):
                        attachment = provider._attachment(resource, sample.source_id, context)
                        provider._download_resource(resource, attachment, context)
                        assets.append(attachment)
                        context.update(completed=len(assets), succeeded=len(assets))
                from lab_store import LabStore

                runner = TaskRunner(LabStore(paths.database))
                runner.start("attachment_probe", work)
                runner.join()
                report = runner.current()
                note = parse_entry(detail, body, provider.account_id, {}, assets)
                return {"platform": platform, "operation": job["operation"], "status": report.status,
                        "downloaded": report.succeeded, "total": report.total,
                        "file_session_established": bool(provider._sts),
                        "linked": sum(b.kind == "attachment" for b in note.blocks),
                        "bytes": sum(a.size for a in assets),
                        "issue_counts": dict(Counter(i.code for i in report.issues))}
            return {"platform": platform, "operation": job["operation"], "detail_shape": shape(detail),
                    "media_elements": [{"tag": n.tag, "attributes": sorted(n.attrib)} for n in media],
                    "all_element_shapes": sorted({n.tag + ":" + ",".join(sorted(n.attrib)) for n in tree.iter()
                                                  if isinstance(n.tag, str)}),
                    "resource_types": [{"category": r.get("category"), "resType": r.get("resType"),
                                         "mime": r.get("mime"), "bytes": r.get("resourceSize"),
                                         "guid_in_body": r["guid"] in body,
                                         "key_in_body": bool(r.get("resourceKey")) and r["resourceKey"] in body,
                                         "domain_host": urlsplit(r.get("domainAddr", "")).hostname}
                                        for r in detail.get("resources", [])]}
        finally:
            provider.close()
    if job.get("operation") not in ("page_assets", "huawei_read_shapes", "static_assets", "huawei_note_assets") or platform not in ("honor", "huawei", "oppo"):
        raise ValueError("unsupported_discovery")
    origin, path = {"honor": ("https://cloud.honor.com", "/portal/notepad"),
                    "huawei": ("https://cloud.huawei.com", "/home"),
                    "oppo": ("https://owork-api-cn.oppo.com", "/note/")}[platform]
    transport = Transport(origin, SPECS[platform].domains, jars)
    try:
        if job["operation"] == "huawei_note_assets" and platform == "huawei":
            directory = root / ".reference/official/huawei/application"
            runtime = next(directory.glob("runtime.*.js")).read_text("utf-8")
            hashes = dict(re.findall(r'(\d+):"([0-9a-f]{8,40})"', runtime))
            # Exact imports of the current official notepad route and detail component.
            chunks = (70504, 39470, 31637, 56241, 99393, 26985, 51588, 5316, 73414,
                      14106, 81300, 98833, 85578, 83396)
            results = []
            for chunk in chunks:
                if str(chunk) not in hashes:
                    results.append({"chunk": chunk, "status": "not_in_runtime_map"})
                    continue
                name = f"{chunk}.{hashes[str(chunk)]}.17.0.0.300.js"
                with transport.session.get(origin + "/static/js/" + name,
                                           timeout=(10, 35), allow_redirects=False) as response:
                    results.append({"chunk": chunk, "status": response.status_code})
                    if response.status_code == 200 and "javascript" in response.headers.get("Content-Type", ""):
                        (directory / name).write_bytes(response.content)
            return {"platform": platform, "operation": job["operation"], "results": results}
        if job["operation"] == "huawei_read_shapes" and platform == "huawei":
            results = []
            for endpoint, data in (("/notepad/notetag/query", {"index": 0}),
                                   ("/notepad/simplenote/query", {"index": 0, "status": 0, "guids": ""})):
                with transport.session.post(origin + endpoint, json={**data, "traceId": uuid.uuid4().hex},
                                            timeout=(10, 30), allow_redirects=False) as response:
                    item = {"endpoint": endpoint, "http_status": response.status_code,
                            "content_type": response.headers.get("Content-Type", "").split(";")[0]}
                    if 300 <= response.status_code < 400:
                        redirect = urlsplit(urljoin(origin, response.headers.get("Location", "")))
                        item.update({"redirect_host": redirect.hostname, "redirect_path": redirect.path})
                    try:
                        payload = response.json()
                        item["shape"] = shape(payload)
                        code = str(payload.get("Result", {}).get("code", "unknown"))
                        item["result_code"] = code if re.fullmatch(r"\d{1,10}", code) else "unclassified"
                    except (ValueError, AttributeError):
                        pass
                    results.append(item)
            rows = payload.get("rspInfo", {}).get("noteList", [])
            if rows:
                row = rows[0]
                body = transport.json("POST", "/notepad/note/query", json={
                    "ctagNoteInfo": payload.get("ctagNoteInfo", ""),
                    "startCursor": payload.get("startCursor", ""), "guid": row["guid"],
                    "kind": row["kind"], "traceId": uuid.uuid4().hex,
                })
                results.append({"endpoint": "/notepad/note/query", "shape": shape(body)})
                response_entry = body.get("rspInfo", {})
                decoded = json.loads(response_entry.get("data", "{}"))
                results[-1]["identifier_checks"] = {
                    "response_matches_request": response_entry.get("guid") == row["guid"],
                    "body_guid_empty": not bool(decoded.get("guid")),
                    **{"body_matches_" + key: decoded.get("guid") == response_entry.get(key)
                       for key in ("guid", "noteGuid", "uuid", "luid")},
                }
            return {"platform": platform, "operation": job["operation"], "results": results,
                    "cookie_names": sorted({cookie.name if re.fullmatch(r"[A-Za-z_]{1,40}", cookie.name)
                                             else "<dynamic-cookie-name>" for cookie in transport.session.cookies})}
        with transport.session.get(origin + path, timeout=(10, 30), allow_redirects=False) as response:
            if 300 <= response.status_code < 400:
                redirect = urlsplit(urljoin(origin + path, response.headers.get("Location", "")))
                return {"platform": platform, "operation": "page_assets", "status": "redirect",
                        "http_status": response.status_code, "host": redirect.hostname, "path": redirect.path}
            response.raise_for_status()
            markup = response.text
        assets = []
        sources = html.fromstring(markup).xpath("//script/@src")
        static_results = []
        for source in sources:
            url = urlsplit(urljoin(origin + path, source))
            # Only public static asset addresses may leave the worker.
            if url.scheme == "https" and url.path.endswith(".js"):
                assets.append(url._replace(query="", fragment="").geturl())
                if job["operation"] == "static_assets" and url.hostname == urlsplit(origin).hostname:
                    with transport.session.get(url.geturl(), timeout=(10, 35), allow_redirects=False) as script:
                        static_results.append({"name": url.path.rsplit("/", 1)[-1], "status": script.status_code,
                                               "query_fields": sorted(dict(parse_qsl(url.query)))})
                        if script.status_code == 200 and "javascript" in script.headers.get("Content-Type", ""):
                            destination = root / ".reference/official" / platform / "application" / url.path.rsplit("/", 1)[-1]
                            destination.parent.mkdir(parents=True, exist_ok=True)
                            destination.write_bytes(script.content)
        destination = root / ".private/protocol" / platform / "public-assets.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(assets, indent=2), encoding="utf-8")
        return {"platform": platform, "operation": "page_assets", "status": "inspected", "public_assets": assets,
                "meta_names": re.findall(r"<meta[^>]+name=['\"]([A-Za-z_-]{1,40})['\"]", markup),
                "static_results": static_results}
    finally:
        transport.close()
