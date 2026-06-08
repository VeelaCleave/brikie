PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    soul_id TEXT DEFAULT 'default',
    max_context_tokens INTEGER NOT NULL DEFAULT 4096,
    tail_length INTEGER NOT NULL DEFAULT 5,
    total_messages INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    last_compaction_index INTEGER NOT NULL DEFAULT -1,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now', 'utc')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now', 'utc'))
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    "index" INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    tool_call_id TEXT,
    token_count INTEGER NOT NULL DEFAULT 0,
    is_compacted INTEGER NOT NULL DEFAULT 0,
    compacted_at TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now', 'utc')),
    UNIQUE(session_id, "index")
);

CREATE TABLE IF NOT EXISTS dag_nodes (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    depth INTEGER NOT NULL DEFAULT 0,
    start_index INTEGER NOT NULL,
    end_index INTEGER NOT NULL,
    content TEXT NOT NULL,
    token_count INTEGER NOT NULL DEFAULT 0,
    compaction_cost INTEGER NOT NULL DEFAULT 0,
    parent_id TEXT REFERENCES dag_nodes(id),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now', 'utc'))
);

CREATE TABLE IF NOT EXISTS token_budgets (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    active_context_tokens INTEGER NOT NULL DEFAULT 0,
    summary_tokens INTEGER NOT NULL DEFAULT 0,
    tail_tokens INTEGER NOT NULL DEFAULT 0,
    max_budget INTEGER NOT NULL DEFAULT 4096,
    trigger_threshold INTEGER NOT NULL DEFAULT 3200
);

CREATE TABLE IF NOT EXISTS compaction_log (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    node_id TEXT NOT NULL REFERENCES dag_nodes(id),
    depth INTEGER NOT NULL DEFAULT 0,
    messages_compacted INTEGER NOT NULL,
    tokens_before INTEGER NOT NULL,
    tokens_after INTEGER NOT NULL,
    tokens_saved INTEGER NOT NULL,
    compaction_cost INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_session_index ON messages(session_id, "index");
CREATE INDEX IF NOT EXISTS idx_messages_compacted ON messages(session_id, is_compacted);
CREATE INDEX IF NOT EXISTS idx_dag_nodes_session_depth ON dag_nodes(session_id, depth);
CREATE INDEX IF NOT EXISTS idx_dag_nodes_parent ON dag_nodes(parent_id);
CREATE INDEX IF NOT EXISTS idx_budgets_session ON token_budgets(session_id);
CREATE INDEX IF NOT EXISTS idx_compaction_log_session ON compaction_log(session_id);
