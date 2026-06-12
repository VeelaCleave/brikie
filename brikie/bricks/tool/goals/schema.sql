PRAGMA journal_mode = WAL;

-- A high-level goal the agent works toward over hours/days, across
-- sessions. Status: active | paused | done | abandoned.
CREATE TABLE IF NOT EXISTS goals (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'paused', 'done', 'abandoned')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now', 'utc')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now', 'utc'))
);

-- Subtasks link back to their parent goal for traceability. Ordered by
-- position within the goal. Status: pending | active | done | blocked.
CREATE TABLE IF NOT EXISTS subtasks (
    id TEXT PRIMARY KEY,
    goal_id TEXT NOT NULL REFERENCES goals(id),
    position INTEGER NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'active', 'done', 'blocked')),
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now', 'utc')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now', 'utc')),
    UNIQUE (goal_id, position)
);

-- Append-only progress log per goal — the durable trail a supervising
-- agent (or a resumed session) reads to pick up without losing state.
CREATE TABLE IF NOT EXISTS goal_events (
    id TEXT PRIMARY KEY,
    goal_id TEXT NOT NULL REFERENCES goals(id),
    kind TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now', 'utc'))
);

CREATE INDEX IF NOT EXISTS idx_subtasks_goal ON subtasks(goal_id);
CREATE INDEX IF NOT EXISTS idx_goal_events_goal ON goal_events(goal_id);
