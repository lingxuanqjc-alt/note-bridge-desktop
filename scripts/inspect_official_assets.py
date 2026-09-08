"""Read public, unauthenticated vendor pages; cache only under .reference.

Never pass account cookies, authentication headers or a browser profile to this tool.
The output is protocol research, not a working-connector acceptance result.
"""

import concurrent.futures
import hashlib
import json
import re
import urllib.parse
import urllib.request
from pathlib import Path

PAGES = {
    "xiaomi": "https://i.mi.com/",
    "oppo": "https://cloud.oppo.com/",
    "vivo": "https://yun.vivo.com.cn/",
    "huawei": "https://cloud.huawei.com/",
    "honor": "https://cloud.honor.com/",
    "meizu": "https://cloud.flyme.cn/",
    "wps": "https://note.wps.cn/",
}


def inspect(item):
    platform, url = item
    root = Path(__file__).resolve().parents[1] / ".reference" / "official" / platform
    root.mkdir(parents=True, exist_ok=True)
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=25) as response:
            body = response.read(5_000_000)
            final_url = response.url
        (root / "index.html").write_bytes(body)
        text = body.decode("utf-8", "replace")
        assets = sorted(set(urllib.parse.urljoin(final_url, src) for src in re.findall(
            r'<script[^>]*\bsrc=["\']([^"\']+)', text, re.I
        )))
        result = {"platform": platform, "url": final_url, "sha256": hashlib.sha256(body).hexdigest(), "scripts": assets}
        (root / "manifest.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result
    except Exception as exc:
        return {"platform": platform, "error_type": type(exc).__name__}


if __name__ == "__main__":
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        for result in pool.map(inspect, PAGES.items()):
            print(json.dumps(result, ensure_ascii=False), flush=True)
