"""Follow Huawei's browser CSRF source and response rotation in memory."""

from urllib.parse import quote, urlsplit

from .transport import Transport, host_matches


class HuaweiTransport(Transport):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._csrf = None

    def _share_cookies(self, path):
        url = urlsplit(self.origin + path)
        request_path = url.path or "/"
        for cookie in self.session.cookies:
            if cookie.name != "shareToken" or cookie.is_expired() or (cookie.secure and url.scheme != "https"):
                continue
            domain = cookie.domain.lstrip(".")
            if url.hostname != domain and not (
                cookie.domain_initial_dot and host_matches(url.hostname or "", (domain,))
            ):
                continue
            cookie_path = cookie.path or "/"
            if request_path != cookie_path and not (
                request_path.startswith(cookie_path)
                and (cookie_path.endswith("/") or request_path[len(cookie_path):].startswith("/"))
            ):
                continue
            yield cookie

    def _share_token(self, path):
        values = {cookie.value for cookie in self._share_cookies(path)}
        return next(iter(values)) if len(values) == 1 else None

    def _sync_share_token(self, path, value):
        cookies = list(self._share_cookies(path))
        if cookies:
            # Keep the imported domain/path keys instead of adding a conflicting duplicate.
            for cookie in cookies:
                cookie.value = value
        else:
            self.session.cookies.set("shareToken", value, domain="." + urlsplit(self.origin).hostname,
                                     path="/", secure=True)

    def request(self, method, path, **kwargs):
        headers = dict(kwargs.pop("headers", {}))
        attachment_get = method.upper() == "GET" and path.startswith("/proxy/v1/download/")
        if attachment_get:
            # The official blob loader uses bare XHR, outside Axios CSRF interceptors.
            headers = {key: value for key, value in headers.items() if key.lower() != "csrftoken"}
        else:
            if self._csrf:
                self._sync_share_token(path, self._csrf)
            csrf = self._share_token(path)
            if csrf:
                headers["CSRFToken"] = csrf
        before = self._share_token(path)
        response = super().request(method, path, headers=headers, **kwargs)
        value = response.headers.get("CSRFToken") if not attachment_get else None
        if value:
            # Official q5 applies encodeURIComponent; Ri returns the cookie without decoding.
            self._csrf = quote(value, safe="~()*!.'-_")
            self._sync_share_token(path, self._csrf)
        elif (current := self._share_token(path)) and current != before:
            # A cookie-only server rotation is already in browser cookie representation.
            self._csrf = current
        return response

    def close(self):
        self._csrf = None
        super().close()
