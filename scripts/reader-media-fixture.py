"""Generate synthetic offline media exports; never use authenticated snapshots."""

import hashlib
import importlib.util
import json
import math
import struct
import sys
import wave
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

from note_bridge.exporter import Exporter, package_export
from note_bridge.models import Attachment, Block, NoteDocument, PlatformId, Span


def main():
    root = Path(__file__).resolve().parents[1]
    out = Path(sys.argv[1]).resolve()
    assert out.is_relative_to(root / '.private')
    resources = out / 'resources'
    assert (resources / 'sample.webm').is_file()
    with wave.open(str(resources / 'sample.wav'), 'wb') as audio:
        audio.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
        audio.writeframes(b''.join(struct.pack('<h', int(3000 * math.sin(2 * math.pi * 440 * i / 8000)))
                                   for i in range(9600)))
    Image.new('RGB', (160, 90), '#7a6fe8').save(resources / 'sample.png')
    attachments = []
    for kind, suffix, mime in [('audio', 'wav', 'audio/wav'), ('video', 'webm', 'video/webm'),
                               ('image', 'png', 'image/png')]:
        path = resources / ('sample.' + suffix)
        attachments.append(Attachment(id=kind, name=f'中文 空格😀.{suffix}', local_path=path.name,
                                      kind=kind, mime=mime, size=path.stat().st_size,
                                      sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
    first = NoteDocument(platform=PlatformId.XIAOMI, account_id='synthetic-media', source_id='one',
                         title='音视频一', created_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
                         blocks=[Block(spans=[Span(text='alpha beta')]),
                                 *[Block(kind='attachment', attachment_id=a.id) for a in attachments]],
                         attachments=attachments)
    second = first.model_copy(deep=True)
    second.source_id, second.title = 'two', '音视频二'
    spec = importlib.util.spec_from_file_location('verify_exports', root / 'scripts/verify-live-exports.py')
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    checks, readers = [], []
    for format in ('txt', 'md', 'html', 'docx'):
        for multi in (False, True):
            result = Exporter(resources).export([first, second], out, format, multi)
            archive_path = package_export(result.path, out)
            relocated = out / '搬到另一处' / (format + ('-multi' if multi else '-single'))
            with zipfile.ZipFile(archive_path) as archive:
                archive.extractall(relocated)
            files = sorted(relocated.glob('*.' + format))
            verifier.verify(files, [first, second], format, multi)
            checks.append({'format': format, 'multi': multi, 'status': 'verified'})
            if format == 'html':
                readers += [{'path': str(path), 'multi': multi} for path in files]
    print(json.dumps({'checks': checks, 'readers': readers}))


if __name__ == '__main__':
    main()
