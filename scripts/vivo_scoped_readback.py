"""Read only receipt-authorized test IDs; never replace a whole-account snapshot.

The caller must derive exact_ids from consumed synthetic manifests and confirmed
receipts. This helper neither discovers IDs nor treats matching titles as scope.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field

from note_bridge.errors import BridgeError
from note_bridge.models import NoteDocument, account_fingerprint
from note_bridge.providers.vivo import DIVIDER_DOWNGRADE_WARNING, VivoProvider, parse_entry
from note_bridge.richtext import DEFAULT_LAYOUT_WARNING
from note_bridge.tasks import TaskContext


@dataclass(frozen=True)
class ScopedReadback:
    account_id: str
    exact_ids: tuple[str, ...]
    notes: list[NoteDocument]
    complete: bool
    # Attachment IDs are resource GUIDs; upload receipts contain resourceKey/metaId.
    resource_keys: dict[str, dict[str, str]] = field(default_factory=dict)


def verify_account(provider: VivoProvider) -> str:
    """Use the production probe's identity rules without its statistics request.

    A changed or unconfirmed account closes this provider and its RAM session.
    The caller must create a new provider after any failed verification.
    """
    previous = provider.account_id
    try:
        if provider.spec.id != "vivo":
            raise BridgeError("invalid_read_scope", "限定读取需要 vivo 会话。")
        session = provider._json("GET", "/account/getUserCookie")
        if not isinstance(session, dict):
            raise BridgeError("login_incomplete", "vivo 会话尚未建立。")
        openid = session.get("vivo_account_cookie_iqoo_openid")
        token = session.get("vivo_account_cookie_iqoo_vivotoken")
        if not isinstance(openid, str) or not openid or not isinstance(token, str) or not token:
            raise BridgeError("login_incomplete", "请在 vivo 官方窗口完成登录并进入笔记。")
        provider._openid = openid
        provider.transport.session.headers.update({"token": token, "openId": openid, "source": "1"})
        user = provider._json("POST", "/account/getUserInfo", {"openId": openid})
        if not isinstance(user, dict) or not user.get("userId"):
            raise BridgeError("login_incomplete", "vivo 账号身份未能确认。")
        account = account_fingerprint(provider.spec.id, str(user["userId"]))
        if previous and account != previous:
            raise BridgeError("account_changed", "vivo 账号发生变化，限定读取已停止。")
        provider.account_id = account
        return account
    except Exception:
        provider.close()
        raise


def read_notes(provider: VivoProvider, exact_ids: Iterable[str], context: TaskContext) -> ScopedReadback:
    """Return acquisition coverage only for exact_ids, including an empty scope.

    No probe, statistics, full note listing or Store snapshot write occurs here.
    Transport/ownership/attachment failures raise; content warnings make the
    scoped result incomplete except for the existing verified layout conversions.
    """
    if isinstance(exact_ids, (str, bytes)):
        raise BridgeError("invalid_read_scope", "限定读取需要明确的笔记标识集合。")
    try:
        identities = tuple(exact_ids)
    except TypeError:
        raise BridgeError("invalid_read_scope", "限定读取需要明确的笔记标识集合。") from None
    if (any(not isinstance(identity, str) or not identity or identity.strip() != identity for identity in identities)
            or len(set(identities)) != len(identities)):
        raise BridgeError("invalid_read_scope", "限定读取的笔记标识无效或重复。")
    account = provider.account_id
    if provider.spec.id != "vivo" or not isinstance(account, str) or not account:
        raise BridgeError("login_required", "请先确认 vivo 账号，再读取指定测试笔记。")
    context.check_cancel()
    if not identities:
        return ScopedReadback(account, identities, [], True)
    kwargs = {"check_cancel": context.check_cancel, "wait": context.cancelled.wait}

    def check_account():
        context.check_cancel()
        if provider.account_id != account:
            raise BridgeError("account_changed", "vivo 账号发生变化，限定读取已停止。")

    check_account()
    context.update(total=len(identities), stage="正在读取指定测试笔记的分组。")
    folders = provider._json("POST", "/noteBook/getList", {"maxEntries": 100000}, **kwargs)
    if (not isinstance(folders, list) or len(folders) >= 100000
            or any(not isinstance(row, dict) or not isinstance(row.get("guid"), str) or not row["guid"]
                   for row in folders)):
        raise BridgeError("protocol_changed", "vivo 分组列表结构无法识别或未完整返回。")
    groups = {row["guid"]: row.get("nameNew") or row.get("name", "") for row in folders}
    if len(groups) != len(folders):
        raise BridgeError("group_mismatch", "vivo 返回了重复的分组标识，限定读取已停止。")
    notes, complete, resource_keys = [], True, {}
    for identity in identities:
        check_account()
        detail = provider._json("POST", "/note/getIncludeItem/v2",
                                {"guid": identity, "syncProtocolVersion": 200}, encrypted=True, **kwargs)
        check_account()
        if not isinstance(detail, dict) or detail.get("guid") != identity:
            raise BridgeError("detail_mismatch", "vivo 详情与指定测试笔记不一致。")
        # Validate account, active state, type and encryption before the body or any asset is read.
        parse_entry(detail, "", account, groups)
        folder = detail.get("noteBookGuid")
        # The official note client adds guid="0" (uncategorized) locally; getList
        # need not return it. Preserve the original ID/name just as parse_entry
        # does. Functional views "-1"/"-2" are not alternate default notebook IDs.
        if folder and folder != "0" and folder not in groups:
            raise BridgeError("group_mismatch", "指定测试笔记的分组未在当前目录中确认。")
        resources = detail.get("resources")
        if resources is None:
            resources = []
        if not isinstance(resources, list):
            raise BridgeError("protocol_changed", "vivo 测试笔记的附件列表结构无法识别。")
        # Check every resource before downloading any, including resources not referenced by the body.
        assets = [provider._attachment(resource, identity, context) for resource in resources]
        if len({asset.id for asset in assets}) != len(assets):
            raise BridgeError("attachment_mismatch", "vivo 测试笔记返回了重复附件标识。")
        if any(not isinstance(resource.get("resourceKey"), str) or not resource["resourceKey"] for resource in resources):
            raise BridgeError("attachment_mismatch", "vivo 测试笔记的原件资源标识缺失。")
        resource_keys[identity] = {asset.id: resource["resourceKey"]
                                  for asset, resource in zip(assets, resources, strict=True)}
        check_account()
        body = provider._json("POST", "/note/getContent/v2", {"guid": identity}, encrypted=True, **kwargs)
        if not isinstance(body, str):
            raise BridgeError("protocol_changed", "vivo 测试笔记正文不是预期的富文本结构。")
        for resource, asset in zip(resources, assets, strict=True):
            check_account()
            provider._download_resource(resource, asset, context)
            if not asset.local_path or not asset.sha256 or asset.size <= 0:
                raise BridgeError("attachment_incomplete", "指定测试笔记的附件原件未完整获取。")
        check_account()
        note = parse_entry(detail, body, account, groups, assets)
        allowed = {DEFAULT_LAYOUT_WARNING, DIVIDER_DOWNGRADE_WARNING}
        complete = complete and set(note.warnings) <= allowed
        for warning in note.warnings:
            context.issue("source_warning", warning, identity)
        notes.append(note)
        context.update(completed=len(notes), succeeded=len(notes), stage="正在核对指定测试笔记。")
    check_account()
    if not complete:
        context.issue("incomplete_scoped_read", "指定测试范围存在尚未完整获取的内容，请核对逐条提示。")
    return ScopedReadback(account, identities, notes, complete, resource_keys)
