"""Prepare read-only Xiaomi selectors for explicitly reviewed fixed batches."""
import argparse
import hashlib
import json
from pathlib import Path

from xiaomi_browser_scope import browser_target

REVIEWED_BATCHES = {
    'wps-xiaomi-BD4-job.json': (
        'matrix-20260907-wps-xiaomi-BD4', 'wps', (
            ('wps-group-J2-job.json', '笔记互迁验收 · WPS分组 J2', True),
            ('wps-complex-BD1-job.json', '笔记互迁验收 · WPS分组 J2', True),
        ),
    ),
    'honor-xiaomi-BD7-job.json': (
        'matrix-20260907-honor-xiaomi-BD7', 'honor', (
            ('honor-group-N2-job.json', '笔记互迁验收 · 荣耀分组 N2', True),
            ('honor-complex-BD5-job.json', '笔记互迁验收 · 荣耀分组 N2', True),
        ),
    ),
    'meizu-xiaomi-BD8-job.json': (
        'matrix-20260907-meizu-xiaomi-BD8', 'meizu', (
            ('meizu-group-L2-job.json', '笔记互迁验收 · 魅族分组 L2', True),
            ('meizu-complex-BD6-job.json', '笔记互迁验收 · 魅族分组 L2', True),
            ('meizu-empty-X1-job.json', '笔记互迁验收 · 魅族空标题 X1', False),
        ),
    ),
}


def prepare_selectors(root, name):
    """Resolve the approved manifest only; never start a worker or write a receipt."""
    if name not in REVIEWED_BATCHES:
        raise ValueError('unreviewed_browser_batch')
    batch_id, platform, originals = REVIEWED_BATCHES[name]
    payload = (root / '.private/checkpoints' / name).read_bytes()
    manifest = json.loads(payload)
    sources = manifest.get('sources')
    if (manifest.get('id') != batch_id or manifest.get('source_policy') != 'direct_seed_only'
            or not isinstance(sources, list) or len(sources) != len(originals)):
        raise ValueError('unreviewed_browser_batch')
    for entry, (original, title, images) in zip(sources, originals, strict=True):
        origin = entry.get('from_cloud_fixture', {})
        if (entry.get('title') != title or entry.get('with_images') is not images
                or entry.get('with_group') is not True or origin.get('platform') != platform
                or origin.get('manifest') != original or 'batch_manifest' in origin):
            raise ValueError('unreviewed_browser_batch')
    scopes = [{'manifest': name, 'index': index} for index in range(len(originals))]
    selected = [browser_target(scope, root, manifest.get('expected_account')) for scope in scopes]
    if (len({item['fixtureId'] for item in selected}) != len(originals)
            or any(item['fixtureTitle'] != original[1] for item, original in zip(selected, originals, strict=True))):
        raise ValueError('unverified_fixture_scope')
    return {
        'kind': 'xiaomi-scoped-discovery-preparation', 'formal_acceptance': False,
        'cloud_requests': 0, 'cloud_writes': 0, 'activated': False,
        'manifest_sha256': hashlib.sha256(payload).hexdigest(), 'resolved': selected,
        'requests': [{'armed': False, 'platform': 'xiaomi', 'operation': 'xiaomi_desktop_readonly',
                      'mode': 'desktop-scoped-fixture', 'fixture_scope': scope} for scope in scopes],
        'limitations': [
            'Only confirmed receipts and local cache evidence have been checked; no browser was started.',
            'The existing discovery checks known structure/styles/images, not a complete source body comparison.',
            'Requests remain single-item to preserve existing discovery behavior.',
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', choices=tuple(REVIEWED_BATCHES))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    report = prepare_selectors(root, args.manifest)
    path = root / '.private/checkpoints' / args.manifest.replace('-job.json', '-native-selectors.json')
    with path.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    print(json.dumps({'status': 'prepared', 'count': len(report['resolved']),
                      'file': path.relative_to(root).as_posix(), 'cloud_requests': 0}))


if __name__ == '__main__':
    main()
