"""Native checks must retain linked-resource ownership without opening a writable store."""
import importlib
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))


@pytest.mark.parametrize('name', ['xiaomi_browser_scope', 'honor_matrix_scope', 'meizu_matrix_scope'])
def test_resource_receipts_are_named_rows_and_database_remains_readonly(tmp_path, name):
    path = tmp_path / 'scope.sqlite'
    with sqlite3.connect(path) as seed:
        seed.execute('CREATE TABLE resource_receipts(receipt_key,resource_id,status)')
        seed.executemany('INSERT INTO resource_receipts VALUES (?,?,?)',
                         [('owned', 'image', 'linked'), ('other', 'unrelated', 'linked')])
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
        db.execute('PRAGMA query_only=ON')
        store = importlib.import_module(name)._ReadOnlyStore(db)
        assert store.resource_receipts('owned') == [{'resource_id': 'image', 'status': 'linked'}]
        assert store.resource_receipts('missing') == []
        with pytest.raises(sqlite3.OperationalError, match='readonly'):
            with store.connection() as same_connection:
                same_connection.execute('DELETE FROM resource_receipts')
