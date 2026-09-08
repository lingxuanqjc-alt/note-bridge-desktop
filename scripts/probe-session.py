"""Lab worker: accepts a live session over stdin, never via files or command-line arguments."""
import importlib.util
import json
import sys
from collections import Counter
from contextlib import ExitStack
from http.cookies import SimpleCookie
from pathlib import Path

from lab_platform_lock import PlatformLocks, foreground_scope, verify_inputs
from lab_store import LabStore as Store
from oppo_markup_repair_dispatch import run_armed_job as run_markup_repair
from oppo_markup_repair_dispatch import selected as markup_repair_selected
from parallel_read_dispatch import dispatch
from session_discovery import discover

from note_bridge.errors import BridgeError
from note_bridge.models import PlatformId
from note_bridge.operations import fetch_snapshot
from note_bridge.paths import AppPaths
from note_bridge.providers.factory import create_provider
from note_bridge.tasks import TaskRunner


def main(diagnostics):
    request = json.load(sys.stdin)
    platform = PlatformId(request["platform"])
    action = request["action"]
    if action not in ("probe", "fetch"):
        raise ValueError("unsupported operation")
    if platform == PlatformId.VIVO and action == "fetch":
        raise BridgeError("synthetic_scope_required", "vivo 验收只读取确认回执绑定的工具样例，已停止全账号读取。")
    jars = []
    for cookie in request.pop("cookies"):
        jar = SimpleCookie()
        jar[cookie["name"]] = cookie["value"]
        jar[cookie["name"]]["domain"] = cookie["domain"]
        jar[cookie["name"]]["path"] = cookie["path"]
        jar[cookie["name"]]["secure"] = cookie["secure"]
        jars.append(jar)
    paths = AppPaths(Path(__file__).resolve().parents[1] / ".private" / "session-lab")
    paths.prepare()
    root = Path(__file__).resolve().parents[1]
    if action == 'probe' and not markup_repair_selected(platform, root):
        background = dispatch(platform, jars, root)
        if background is not None:
            jars.clear()
            return background
    scope, inputs = foreground_scope(root, platform, action)
    with PlatformLocks(root, scope):
        verify_inputs(inputs)
        return foreground(platform, action, jars, paths, diagnostics, inputs=inputs)


def foreground(platform, action, jars, paths, diagnostics, *, inputs):
    if action == "probe":
        # Explicit lab-only intent, consumed once. The native login API has no write-job path.
        repair_result = run_markup_repair(platform, jars, Path(__file__).resolve().parents[1], inputs=inputs)
        if repair_result is not None:
            jars.clear()
            return repair_result
        matrix_path = Path(__file__).with_name("live-matrix-job.py")
        matrix_spec = importlib.util.spec_from_file_location("live_matrix_job", matrix_path)
        matrix_module = importlib.util.module_from_spec(matrix_spec)
        matrix_spec.loader.exec_module(matrix_module)
        matrix_result = matrix_module.run_armed_job(platform, jars, Path(__file__).resolve().parents[1])
        if matrix_result is not None:
            jars.clear()
            return matrix_result
        module_path = Path(__file__).with_name("live-fixture-job.py")
        module_spec = importlib.util.spec_from_file_location("live_fixture_job", module_path)
        module = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(module)
        fixture_result = module.run_armed_job(platform, jars, Path(__file__).resolve().parents[1])
        if fixture_result is not None:
            jars.clear()
            # The full protocol evidence remains in the private fixture file.
            keys = ("kind", "platform", "fixture_id", "formal_acceptance", "status", "receipt_confirmed",
                    "acknowledged_writes", "before_count", "after_count", "issue_codes", "image_bytes_verified",
                    "expected_images", "restored_images", "group_mapping_verified", "group_write_requests",
                    "content_and_style_verified", "comparison_policy", "original_notes_unchanged")
            return {key: fixture_result[key] for key in keys if key in fixture_result}
        discovery = discover(platform, jars, Path(__file__).resolve().parents[1])
        if discovery is not None:
            jars.clear()
            return discovery
    provider = create_provider(platform, jars, paths.resources)
    jars.clear()
    with ExitStack() as observers:
        try:
            if platform == PlatformId.HUAWEI and action == "fetch":
                from huawei_session_diagnostics import record_huawei_fetch
                observers.enter_context(record_huawei_fetch(provider.transport,
                    Path(__file__).resolve().parents[1], diagnostics))
            provider.probe()
            if action == "probe":
                result = {"platform": platform, "operation": action, "status": "authenticated_read_probe_passed"}
                if platform == PlatformId.XIAOMI:
                    # Use only this provider's fresh in-memory official GET evidence.
                    # Do not load the historical lab upload-contract fallback here.
                    try:
                        provider.require_unencrypted_mode()
                        result["unencrypted_write_mode_confirmed"] = True
                    except BridgeError as error:
                        result["unencrypted_write_mode_confirmed"] = False
                        result["write_mode_code"] = error.code
                return result
            store = Store(paths.database)
            runner = TaskRunner(store)
            runner.start("fetch", lambda ctx: fetch_snapshot(provider, store, ctx))
            runner.join()
            report = runner.current()
            result = {"platform": platform, "operation": action, "status": report.status, "task_id": report.id,
                    "completed": report.completed, "total": report.total, "succeeded": report.succeeded,
                    "issue_counts": dict(Counter(issue.code for issue in report.issues))}
            if (report.status in ("succeeded", "partial") and not ({i.code for i in report.issues} - {"source_warning"})
                    and store.snapshot(platform, provider.account_id).get("complete")):
                from fetch_scope import write_fetch_scope
                result["scope_file"] = write_fetch_scope(Path(__file__).resolve().parents[1], store, platform,
                                                        provider.account_id, report)
            return result
        finally:
            provider.close()


diagnostics = {}
try:
    result = main(diagnostics)
except BridgeError as error:
    result = {"status": "blocked", "code": error.code, "message": error.message}
except Exception as error:
    result = {"status": "failed", "code": type(error).__name__}
result.update(diagnostics)
print(json.dumps(result, ensure_ascii=True))
