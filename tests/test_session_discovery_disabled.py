"""Disabling lab discovery must allow the normal account probe without any discovery requests."""
import importlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
discovery = importlib.import_module('session_discovery')


@pytest.mark.parametrize('platform', ['oppo', 'xiaomi', 'huawei', 'honor', 'meizu', 'vivo', 'wps'])
def test_explicitly_disabled_discovery_returns_none_before_any_network_or_private_read(tmp_path, monkeypatch, platform):
    folder = tmp_path / '.private'
    folder.mkdir()
    (folder / 'lab-discovery.json').write_text(json.dumps({'platform': platform, 'operation': 'none'}), 'utf-8')
    monkeypatch.setattr(discovery, 'Transport', lambda *_: pytest.fail('Disabled discovery must make no request'))
    assert discovery.discover(platform, ['RAM_ONLY_SECRET'], tmp_path) is None
    assert sorted(path.name for path in folder.iterdir()) == ['lab-discovery.json']


def test_unknown_operation_remains_an_error_not_silently_disabled(tmp_path, monkeypatch):
    folder = tmp_path / '.private'
    folder.mkdir()
    (folder / 'lab-discovery.json').write_text(json.dumps({'platform': 'oppo', 'operation': 'unknown'}), 'utf-8')
    monkeypatch.setattr(discovery, 'Transport', lambda *_: pytest.fail('Unknown discovery must not send a request'))
    with pytest.raises(ValueError, match='^unsupported_discovery$'):
        discovery.discover('oppo', [], tmp_path)
