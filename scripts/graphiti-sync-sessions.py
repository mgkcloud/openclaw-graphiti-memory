#!/usr/bin/env python3
"""
Sync Clawdbot session messages to Graphiti knowledge graph.
Runs periodically to keep Graphiti updated with conversation history.
"""

import argparse
import sqlite3
import hashlib
from contextlib import contextmanager, closing
import json
import os
import sys
from datetime import datetime
from pathlib import Path
import urllib.request
import urllib.error

GRAPHITI_URL = os.environ.get("GRAPHITI_URL", "http://localhost:8001")
SESSIONS_DIR = Path(os.environ.get("OPENCLAW_SESSIONS_DIR", Path.home() / ".openclaw/agents/main/sessions"))
SYNC_STATE_FILE = Path(os.environ.get("GRAPHITI_SYNC_STATE_FILE", Path.home() / ".openclaw/graphiti-sync-state.json"))
MAX_MESSAGES_PER_RUN = 50

def load_sync_state():
    """Load the sync state tracking which messages have been synced."""
    if SYNC_STATE_FILE.exists():
        try:
            return json.loads(SYNC_STATE_FILE.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            raise RuntimeError("Invalid sync state; refusing to replay history") from exc
    return {"synced_messages": {}, "last_sync": None}

def save_sync_state(state):
    """Save the sync state."""
    SYNC_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = SYNC_STATE_FILE.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, SYNC_STATE_FILE)

def check_graphiti():
    """Check if Graphiti is available."""
    try:
        req = urllib.request.Request(f"{GRAPHITI_URL}/healthcheck")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except:
        return False

def extract_text_content(content):
    """Extract text from message content."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = []
        for item in content:
            if isinstance(item, dict) and item.get('type') == 'text':
                texts.append(item.get('text', ''))
            elif isinstance(item, str):
                texts.append(item)
        return ' '.join(texts)
    return ''

def should_sync_message(content):
    """Determine if a message should be synced."""
    if not content or len(content) < 10:
        return False
    
    # Skip system/internal messages
    skip_patterns = [
        '[Signal', '[Slack', 'HEARTBEAT', 'NO_REPLY', 
        '✅ New session', 'System:', '[message_id:'
    ]
    for pattern in skip_patterns:
        if pattern in content:
            return False
    
    return True


def iter_entries(database=None):
    """Read canonical active SQLite events, or legacy JSONL; never write source."""
    if database and database.exists():
        with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)) as db:
            db.execute('PRAGMA query_only=ON')
            # Refuse stale active-branch projections rather than sync removed branches.
            if db.execute('SELECT 1 FROM session_transcript_index_state WHERE needs_rebuild != 0 LIMIT 1').fetchone():
                raise RuntimeError('Transcript active index needs rebuild; source left unchanged')
            query = '''SELECT e.event_json FROM transcript_events e
                JOIN session_transcript_active_events a
                ON a.session_id=e.session_id AND a.event_seq=e.seq
                LEFT JOIN memory_session_tombstones t ON t.session_id=e.session_id
                WHERE t.session_id IS NULL
                ORDER BY e.created_at,e.session_id,e.seq'''
            for (raw,) in db.execute(query):
                yield json.loads(raw)
        return
    for path in sorted(SESSIONS_DIR.glob('*.jsonl')):
        if '.trajectory.' in path.name or '.checkpoint.' in path.name:
            continue
        for line in path.read_text(encoding='utf-8').splitlines():
            if line.strip():
                yield json.loads(line)


def candidates(entries, state):
    seen = set(state['synced_messages']) | set(state.get('pending_messages', {}))
    for entry in entries:
        ident = entry.get('id')
        message = entry.get('message', {})
        if entry.get('type') != 'message' or not ident or ident in seen:
            continue
        boundary = state.get('sqlite_resume_after')
        if boundary and parse_timestamp(entry.get('timestamp')) < parse_timestamp(boundary):
            continue
        role = message.get('role')
        content = extract_text_content(message.get('content'))
        if role not in ('user', 'assistant') or not should_sync_message(content):
            continue
        # Never ingest tool calls, reasoning, or failed assistant completions.
        if message.get('stopReason') in ('error', 'aborted', 'toolUse'):
            continue
        seen.add(ident)
        yield ident, role, content, entry.get('timestamp')


def parse_timestamp(value):
    if not value:
        raise RuntimeError('Missing transcript timestamp; refusing ambiguous migration')
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        raise RuntimeError('Transcript timestamp must include timezone')
    return stamp


def migration_boundary(entries, state):
    """Use source timestamps, NOT last_sync (which also advances on empty runs)."""
    if 'sqlite_resume_after' in state:
        return
    timestamps = [entry['timestamp'] for entry in entries
                  if entry.get('id') in state['synced_messages'] and entry.get('timestamp')]
    if state['synced_messages'] and not timestamps:
        raise RuntimeError('Cannot map legacy synced IDs to SQLite; manual migration review required')
    if timestamps:
        state['sqlite_resume_after'] = max(timestamps, key=parse_timestamp)


def api(path, payload=None):
    data = None if payload is None else json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(GRAPHITI_URL + path, data=data,
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=60) as response:
        return response.status, json.load(response)


@contextmanager
def state_lock():
    # OS releases advisory lock on crash; never delete/recreate a held lock inode.
    path = SYNC_STATE_FILE.with_suffix('.lock')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        handle.seek(0)
        if os.name == 'nt':
            import msvcrt
            if path.stat().st_size == 0:
                handle.write(b'0'); handle.flush(); handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def sync_sessions(database, dry_run=False, limit=50, group='clawdbot-main', only_id=None):
    state = load_sync_state()
    if not isinstance(state.get('synced_messages'), dict):
        raise RuntimeError('Invalid deduplication state')
    if database and database.exists():
        if sqlite3.sqlite_version_info < (3, 37, 0):
            raise RuntimeError('SQLite >= 3.37 required; run with a current Python (for example Python 3.12)')
        migration_boundary(iter_entries(database), state)
    if dry_run:
        count = sum(1 for _ in candidates(iter_entries(database), state))
        print(json.dumps({'eligible_unsent': count, 'limit': limit, 'writes': 0}))
        return 0
    pending = state.setdefault('pending_messages', {})
    if pending:
        _, episodes = api('/episodes/' + group + '?last_n=10000')
        names = {e.get('name') for e in episodes}
        for ident, receipt in list(pending.items()):
            if receipt['group'] == group and receipt['name'] in names:
                state['synced_messages'][ident] = datetime.now().isoformat()
                del pending[ident]
        save_sync_state(state)
        if pending:
            raise RuntimeError('Unconfirmed submissions retained; no automatic replay. Inspect Graphiti worker.')
    submitted = 0
    for ident, role, content, timestamp in candidates(iter_entries(database), state):
        if only_id and ident != only_id:
            continue
        if submitted >= limit:
            break
        name = 'openclaw-sync-' + hashlib.sha256(ident.encode()).hexdigest()
        # Persist BEFORE POST: an ambiguous timeout/crash must never cause replay.
        pending[ident] = {'name': name, 'group': group, 'submitted_at': datetime.now().isoformat()}
        save_sync_state(state)
        code, _ = api('/messages', {'group_id': group, 'messages': [{
            'name': name, 'role_type': role, 'role': 'User' if role == 'user' else 'Agent',
            'content': content, 'timestamp': timestamp}]})
        if code not in (200, 202):
            raise RuntimeError('Graphiti did not accept message')
        submitted += 1
    state['last_sync'] = datetime.now().isoformat()
    save_sync_state(state)
    print(json.dumps({'submitted': submitted, 'pending_confirmation': len(pending),
                      'confirmed_total': len(state['synced_messages'])}))
    return submitted


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, default=Path(os.environ.get(
        'OPENCLAW_AGENT_DB', SESSIONS_DIR.parent / 'agent/openclaw-agent.sqlite')))
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--limit', type=int, default=MAX_MESSAGES_PER_RUN)
    parser.add_argument('--group', default='clawdbot-main')
    parser.add_argument('--only-id', help='Submit only this source message ID for controlled verification')
    args = parser.parse_args()
    try:
        if args.limit < 0:
            raise ValueError('--limit must be non-negative')
        if args.dry_run:
            sync_sessions(args.database, True, args.limit, args.group)
        else:
            with state_lock():
                sync_sessions(args.database, False, args.limit, args.group, args.only_id)
    except Exception as exc:
        print('Graphiti sync failed: ' + type(exc).__name__ + ': ' + str(exc), file=sys.stderr)
        sys.exit(1)
