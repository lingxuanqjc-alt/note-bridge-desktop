from pathlib import Path

from ..models import PlatformId
from .base import SPECS, PendingProvider, Provider
from .honor import HonorProvider
from .huawei import HuaweiProvider
from .huawei_transport import HuaweiTransport
from .meizu import MeizuProvider
from .oppo import OppoProvider
from .transport import Transport
from .vivo import VivoProvider
from .wps import WpsProvider
from .xiaomi import XiaomiProvider


def create_provider(platform: PlatformId, cookies, resources: Path) -> Provider:
    if platform == PlatformId.XIAOMI:
        return XiaomiProvider(Transport("https://i.mi.com", SPECS[platform].domains, cookies), resources)
    if platform == PlatformId.MEIZU:
        return MeizuProvider(Transport("https://notes.flyme.cn", SPECS[platform].domains, cookies), resources)
    if platform == PlatformId.HONOR:
        return HonorProvider(
            Transport("https://cloud.honor.com", SPECS[platform].domains, cookies), resources
        )
    if platform == PlatformId.WPS:
        return WpsProvider(
            Transport("https://note-api.wps.cn", SPECS[platform].domains, cookies),
            Transport("https://account.wps.cn", SPECS[platform].domains, cookies),
            resources,
            Transport("https://drive.wps.cn", SPECS[platform].domains, cookies),
        )
    if platform == PlatformId.VIVO:
        return VivoProvider(Transport("https://pc.vivo.com.cn", SPECS[platform].domains, cookies), resources)
    if platform == PlatformId.OPPO:
        return OppoProvider(
            Transport("https://owork-api-cn.oppo.com", SPECS[platform].domains, cookies), resources
        )
    if platform == PlatformId.HUAWEI:
        return HuaweiProvider(
            HuaweiTransport("https://cloud.huawei.com", SPECS[platform].domains, cookies), resources
        )
    return PendingProvider(SPECS[platform])
