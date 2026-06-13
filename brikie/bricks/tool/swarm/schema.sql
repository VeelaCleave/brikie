PRAGMA journal_mode = WAL;

-- One swarm dispatch: the coordinator fanned N scoped tasks out to N
-- ephemeral sub-agents and collected their reports. Persisted purely for
-- observability — every delegation is auditable after the fact, even
-- though the sub-agents' contexts are discarded (clean GC).
-- Status: running | done.
CREATE TABLE IF NOT EXISTS swarm_runs (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'done')),
    goal TEXT NOT NULL DEFAULT '',
    task_count INTEGER NOT NULL DEFAULT 0,
    ok_count INTEGER NOT NULL DEFAULT 0,
    summary TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now', 'utc')),
    finished_at TEXT
);

-- One sub-agent within a run: its role, the task it was given, and the
-- report it returned. The discarded context is gone, but the outcome is
-- kept here for traceability.
CREATE TABLE IF NOT EXISTS swarm_tasks (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES swarm_runs(id),
    position INTEGER NOT NULL,
    role TEXT NOT NULL,
    task TEXT NOT NULL,
    ok INTEGER NOT NULL DEFAULT 0,
    report TEXT NOT NULL DEFAULT '',
    steps INTEGER NOT NULL DEFAULT 0,
    tool_calls INTEGER NOT NULL DEFAULT 0,
    tools_used TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now', 'utc'))
);

CREATE INDEX IF NOT EXISTS idx_swarm_tasks_run ON swarm_tasks(run_id);
