from http.cookies import SimpleCookie

import pytest
import requests

from note_bridge.providers.transport import Transport


@pytest.mark.parametrize("domain", [".huawei.com", ".cloud.huawei.com"])
def test_server_rotation_replaces_imported_domain_cookie(domain):
    jar = SimpleCookie()
    jar["session"] = "synthetic-old"
    jar["session"]["domain"] = domain
    jar["session"]["path"] = "/"
    transport = Transport("https://cloud.huawei.com", ("huawei.com",), [jar])
    try:
        transport.session.cookies.set_cookie(requests.cookies.create_cookie(
            "session", "synthetic-new", domain=domain, path="/"))
        prepared = transport.session.prepare_request(requests.Request("GET", "https://cloud.huawei.com/notepad/query"))
        assert len(transport.session.cookies) == 1, "Domain normalization must not leave stale credentials alongside the server replacement."
        assert transport.cookie("session") == "synthetic-new"
        assert prepared.headers["Cookie"] == "session=synthetic-new"
    finally:
        transport.close()


def test_rotation_does_not_merge_different_cookie_paths():
    transport = Transport("https://cloud.huawei.com", ("huawei.com",))
    try:
        for path, value in [("/", "synthetic-root"), ("/notepad", "synthetic-scoped")]:
            transport.session.cookies.set_cookie(requests.cookies.create_cookie("session", value, domain=".huawei.com", path=path))
        assert len(transport.session.cookies) == 2 and transport.cookie("session") is None
    finally:
        transport.close()
