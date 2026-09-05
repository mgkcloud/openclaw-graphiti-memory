from contextlib import contextmanager
import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('sync', Path(__file__).parents[1] / 'scripts/graphiti-sync-sessions.py')
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)


@contextmanager
def connect(path):
    db = sqlite3.connect(path)
    try:
        with db: yield db
    finally: db.close()


def fixture(path):
    with connect(path) as db:
        db.executescript('''
        CREATE TABLE transcript_events(session_id TEXT,seq INTEGER,event_json TEXT,created_at INTEGER);
        CREATE TABLE session_transcript_active_events(session_id TEXT,event_seq INTEGER);
        CREATE TABLE memory_session_tombstones(session_id TEXT);
        CREATE TABLE session_transcript_index_state(needs_rebuild INTEGER);
        ''')
        for seq, ident in enumerate(['old', 'new', 'removed']):
            event = {'type': 'message', 'id': ident, 'timestamp': '2026-09-05T00:00:00Z',
                     'message': {'role': 'user', 'content': 'Synthetic sync verification: the test observatory is called Copper Finch.'}}
            db.execute('INSERT INTO transcript_events VALUES (?,?,?,?)', ('s',seq,json.dumps(event),seq))
            if ident != 'removed':
                db.execute('INSERT INTO session_transcript_active_events VALUES (?,?)', ('s',seq))


class Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / 'agent.sqlite'
        fixture(self.db)
        self.state = self.root / 'state.json'
        self.state.write_text(json.dumps({'synced_messages': {'old': 'preserved'}, 'last_sync': None}))
        self.patcher = patch.object(sync, 'SYNC_STATE_FILE', self.state)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_readonly_active_and_dedup(self):
        before = self.db.read_bytes()
        rows = list(sync.candidates(sync.iter_entries(self.db), sync.load_sync_state()))
        self.assertEqual([x[0] for x in rows], ['new'])
        self.assertEqual(before, self.db.read_bytes())

    def test_dry_run_no_network_or_state_write(self):
        before = self.state.read_bytes()
        with patch.object(sync, 'api', side_effect=AssertionError('network')):
            sync.sync_sessions(self.db, True)
        self.assertEqual(before, self.state.read_bytes())

    def test_async_confirm_and_no_replay(self):
        with patch.object(sync, 'api', return_value=(202, {})) as api:
            sync.sync_sessions(self.db, limit=1)
            self.assertEqual(api.call_count, 1)
        state = sync.load_sync_state()
        self.assertNotIn('new', state['synced_messages'])
        name = state['pending_messages']['new']['name']
        with patch.object(sync, 'api', return_value=(200, [{'name': name}])) as api:
            sync.sync_sessions(self.db)
            self.assertEqual(api.call_count, 1)
        self.assertEqual(sync.load_sync_state()['synced_messages']['old'], 'preserved')
        self.assertIn('new', sync.load_sync_state()['synced_messages'])

    def test_ambiguous_failure_blocks_retry(self):
        with patch.object(sync, 'api', side_effect=TimeoutError):
            with self.assertRaises(TimeoutError): sync.sync_sessions(self.db)
        with patch.object(sync, 'api', return_value=(200, [])) as api:
            with self.assertRaises(RuntimeError): sync.sync_sessions(self.db)
            self.assertEqual(api.call_count, 1)

    def test_only_id_does_not_submit_other_messages(self):
        with patch.object(sync, 'api', side_effect=AssertionError('unexpected POST')):
            sync.sync_sessions(self.db, only_id='not-present')
        self.assertEqual(sync.load_sync_state()['pending_messages'], {})

    def test_corrupt_state_fails_closed(self):
        self.state.write_text('{')
        with self.assertRaises(RuntimeError): sync.load_sync_state()

    def test_migration_boundary_uses_source_not_run_time(self):
        entries = list(sync.iter_entries(self.db))
        entries[0]['timestamp'] = '2026-09-04T10:00:00Z'
        entries[1]['timestamp'] = '2026-09-03T10:00:00Z'
        state = sync.load_sync_state()
        state['last_sync'] = '2026-09-05T12:00:00'
        sync.migration_boundary(entries, state)
        self.assertEqual(state['sqlite_resume_after'], entries[0]['timestamp'])
        self.assertEqual(list(sync.candidates(entries, state)), [])

    def test_unmapped_legacy_state_stops_migration(self):
        with self.assertRaises(RuntimeError):
            sync.migration_boundary([], sync.load_sync_state())

    def test_no_tool_reasoning_or_failed_completion(self):
        entry = {'type': 'message', 'id': 'x', 'message': {'role': 'assistant',
                 'stopReason': 'error', 'content': [{'type': 'text','text': 'Failed partial response'}]}}
        self.assertEqual(list(sync.candidates([entry], {'synced_messages': {}})), [])
        self.assertEqual(sync.extract_text_content([{'type':'thinking','thinking':'private reasoning'}]), '')

    def test_tombstones_and_stale_index(self):
        with connect(self.db) as db:
            db.execute("INSERT INTO memory_session_tombstones VALUES ('s')")
        self.assertEqual(list(sync.iter_entries(self.db)), [])
        with connect(self.db) as db:
            db.execute('INSERT INTO session_transcript_index_state VALUES (1)')
        with self.assertRaises(RuntimeError): list(sync.iter_entries(self.db))


if __name__ == '__main__': unittest.main()
