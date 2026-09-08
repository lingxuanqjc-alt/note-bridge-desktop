"""Bounded read retries and one-shot writes, with credentials confined to memory."""

from __future__ import annotations

from collections.abc import Callable
from http.cookiejar import Cookie
from urllib.parse import urlparse

import requests

from ..errors import BridgeError, WriteUncertain


def host_matches(host: str, domains: tuple[str, ...]) -> bool:
    host = host.lower().rstrip(".")
    return any(host == domain or host.endswith("." + domain) for domain in domains)


class Transport:
    def __init__(self, origin: str, domains: tuple[str, ...], cookies=(), session=None):
        self.origin, self.domains = origin.rstrip("/"), domains
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36",
                "Referer": origin + "/",
            }
        )
        for jar in cookies:
            for morsel in jar.values():
                # CookieJar keys include the leading dot. Preserve browser domain
                # cookies so a server Set-Cookie rotation replaces the same entry.
                domain = morsel["domain"] or urlparse(origin).hostname
                if host_matches(domain.lstrip("."), domains):
                    self.session.cookies.set_cookie(
                        Cookie(
                            version=0,
                            name=morsel.key,
                            value=morsel.value,
                            port=None,
                            port_specified=False,
                            domain=domain,
                            domain_specified=bool(morsel["domain"]),
                            domain_initial_dot=domain.startswith("."),
                            path=morsel["path"] or "/",
                            path_specified=True,
                            secure=bool(morsel["secure"]),
                            expires=None,
                            discard=True,
                            comment=None,
                            comment_url=None,
                            rest={},
                            rfc2109=False,
                        )
                    )

    def cookie(self, name: str) -> str | None:
        values = {item.value for item in self.session.cookies if item.name == name}
        return next(iter(values)) if len(values) == 1 else None

    def request(
        self,
        method: str,
        path: str,
        *,
        write: bool = False,
        return_redirects: bool = False,
        check_cancel: Callable = lambda: None,
        wait: Callable[[float], None] | None = None,
        **kwargs,
    ):
        if not path.startswith("/") or path.startswith("//"):
            raise BridgeError("unsafe_url", "平台请求地址不正确。")
        if return_redirects and (write or method not in ("GET", "HEAD")):
            raise ValueError("Redirect inspection is restricted to read-only requests")
        attempts = 1 if write else 3
        for attempt in range(attempts):
            check_cancel()
            try:
                response = self.session.request(
                    method, self.origin + path, timeout=(10, 35), allow_redirects=False, **kwargs
                )
            except requests.RequestException:
                if write:
                    raise WriteUncertain() from None
                if attempt + 1 == attempts:
                    raise BridgeError("network_error", "网络请求失败，请检查网络连接后重试。") from None
                if wait:
                    wait(min(2**attempt, 4))
                continue
            if response.status_code == 403:
                response.close()
                raise BridgeError("request_forbidden", "平台拒绝访问此请求（HTTP 403），请核对账号权限或官网提示；这不一定是登录失效。")
            if return_redirects and 300 <= response.status_code < 400:
                return response
            if response.status_code == 401 or 300 <= response.status_code < 400:
                response.close()
                raise BridgeError("session_expired", "登录已失效或需要二次验证，请重新登录。")
            if response.status_code == 429:
                response.close()
                if write or attempt + 1 == attempts:
                    raise BridgeError("rate_limited", "平台请求过于频繁，请稍后再试。")
                if wait:
                    wait(min(2 ** (attempt + 1), 8))
                continue
            if response.status_code >= 500:
                response.close()
                if write:
                    raise WriteUncertain()
                if attempt + 1 < attempts:
                    if wait:
                        wait(2**attempt)
                    continue
                raise BridgeError("server_error", "平台服务暂时不可用，请稍后再试。")
            if response.status_code >= 400:
                response.close()
                raise BridgeError("request_rejected", "平台拒绝了请求，请检查账号权限和云空间。")
            return response
        raise BridgeError("network_error", "网络请求未完成。")

    def json(self, method: str, path: str, **kwargs) -> dict:
        with self.request(method, path, **kwargs) as response:
            try:
                payload = response.json()
            except ValueError:
                if kwargs.get("write"):
                    raise WriteUncertain() from None
                raise BridgeError(
                    "protocol_changed", "平台返回格式发生变化，已停止处理以免遗漏笔记。"
                ) from None
        if not isinstance(payload, dict):
            if kwargs.get("write"):
                raise WriteUncertain()
            raise BridgeError("protocol_changed", "平台返回结构无法识别。")
        return payload

    def close(self):
        self.session.cookies.clear()
        self.session.close()
