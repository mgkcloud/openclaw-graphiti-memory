# SQLite transcript sync

OpenClaw releases that migrate JSONL transcripts into `openclaw-agent.sqlite`
need a SQLite reader. An advancing `last_sync` timestamp does not prove ingestion.

## Usage

Use Python with SQLite >= 3.37 (Python 3.12 works). On Windows, explicitly select
the interpreter: another application may put an older Python first on PATH.

```sh
python scripts/graphiti-sync-sessions.py --dry-run
python scripts/graphiti-sync-sessions.py --limit 1 --only-id SOURCE_MESSAGE_ID
# After the worker finishes, reconcile receipts without sending more:
python scripts/graphiti-sync-sessions.py --limit 0
```

Environment overrides: `OPENCLAW_SESSIONS_DIR`, `OPENCLAW_AGENT_DB`,
`GRAPHITI_SYNC_STATE_FILE`, `GRAPHITI_URL`. Default paths use `.openclaw`.
Legacy `.clawdbot` installations must set the session and state overrides.
Point to the existing state file; never start with an empty state during migration.

SQLite is opened read-only, with query-only enabled. Only active transcript events
are read; tombstoned sessions are excluded. A stale active projection fails closed.
JSONL remains supported when no SQLite database exists. Malformed source/state
fails closed. No source tables, embeddings, Graphiti models or indexes are changed.

Original message IDs remain the deduplication keys. On the first SQLite run, the
latest source timestamp matching an already-synced ID becomes `sqlite_resume_after`.
Older unsent messages are not silently replayed or marked synced. If no old IDs
match, migration stops for review. Dry-run does not save this boundary or contact
Graphiti. There is no rolling 24-hour filter that could silently drop a backlog.

## Delivery confirmation and failure handling

The sender persists a pending receipt atomically **before** POST, with an episode
name derived from the original message ID. HTTP 202 is only acceptance. On a later
run, `/episodes/{group}?last_n=10000` must contain the name before the message is
marked synced. Unconfirmed receipts stop new submissions and are never retried
automatically. This is deliberately conservative: a crash before POST can also
require manual reconciliation. The API is not assumed to provide idempotency.

Do not clear pending receipts on timeout. Inspect worker logs and episode storage.
After a confirmed failed, non-persisted job, an operator can back up state and
explicitly reconcile that receipt. If more than 10,000 episodes have passed, find
the receipt outside that lookup window before deciding anything failed.
An episode appearing proves episode persistence, not perfect extraction of every
fact; verify retrieval too. Healthcheck success does not prove the worker's LLM
account has credit. Existing searches can work while ingestion fails with quota.

An OS advisory lock prevents overlapping writers using this script. Do not run the
old writer alongside it. Rollout: wait for scheduled sync to finish, back up its
script/state and obtain a consistent source SQLite backup, dry-run, test in a
separate group/state, then replace the scheduled script and explicitly pin the
working interpreter. Keep unrelated file-sync configuration unchanged.

## Preservation and validation

No deletion/reset/reindex endpoint is used. New production episodes can naturally
add facts or change Graphiti temporal relationships; this is normal ingestion,
not a guarantee of zero graph changes. Before rollout, back up Neo4j independently:
an episodes export or the Graphiti file cache is **not** a complete graph backup.

Run `python -m unittest discover -s tests -v`. Test a synthetic SQLite transcript
with `--group` and a separate state file, confirm persistence, test search, and
run again to confirm no duplicate. Compare existing episode properties and cached
vectors before/after, not only counts. Never include private snapshots in a PR.

## Validation from the upgrade investigation

- Reader dry-run successfully finds post-migration backlog without writes.
- Consistent SQLite snapshot passed integrity check.
- 1,220 existing conversation episodes: no missing or changed records after test.
- All 2,803 cached embeddings: identical content checksum before/after.
- Existing deduplication entries unchanged; known-memory search response identical.
- Initial isolated POST failed in the worker with `429 credit_balance_exhausted`.
  After billing was restored, a fresh isolated test persisted, was retrieved by
  semantic search, and a second sync submitted zero duplicates.
- A real source message then passed the deployed reader, ingestion, receipt
  confirmation and semantic retrieval end-to-end.
- Full logical graph-property comparison around that one production submission:
  3,477 prior nodes and 9,148 prior relationships all unchanged, none removed;
  two nodes and three relationships added. This is a point-in-time check, not
  a guarantee that future ingestion never updates existing facts.
- Legacy dedup records all preserved. The scheduled reader was replaced and its
  wrapper pinned to the installed Python 3.12; the ten-minute schedule re-enabled.
  Backlog processing is separate from the single-message verification above.
