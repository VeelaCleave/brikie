-- LLM Wiki Schema — Markdown-backed wiki store
-- Phase 3.2: Tripartite Memory Architecture

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- Pages: Core wiki pages (identified by slug-based ID)
CREATE TABLE IF NOT EXISTS pages (
    id TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT DEFAULT 'draft' CHECK(status IN ('draft', 'review', 'published', 'archived')),
    line_count INTEGER DEFAULT 0,
    source TEXT DEFAULT 'manual',
    created_at TEXT DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now', 'utc')),
    updated_at TEXT DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now', 'utc'))
);

-- Links: Relationships between pages and external references
CREATE TABLE IF NOT EXISTS links (
    source_page_id TEXT NOT NULL,
    target_page_id TEXT NOT NULL,
    link_type TEXT DEFAULT 'wiki' CHECK(link_type IN ('wiki', 'external', 'heading')),
    PRIMARY KEY(source_page_id, target_page_id)
);

-- Tags: Tag assignments to pages
CREATE TABLE IF NOT EXISTS tags (
    page_id TEXT NOT NULL,
    tag TEXT NOT NULL,
    PRIMARY KEY(page_id, tag)
);

-- Indexes for efficient queries
CREATE INDEX IF NOT EXISTS idx_pages_status ON pages(status);
CREATE INDEX IF NOT EXISTS idx_pages_title ON pages(title);
CREATE INDEX IF NOT EXISTS idx_pages_source ON pages(source);
CREATE INDEX IF NOT EXISTS idx_links_source ON links(source_page_id);
CREATE INDEX IF NOT EXISTS idx_links_target ON links(target_page_id);
CREATE INDEX IF NOT EXISTS idx_tags_page ON tags(page_id);
CREATE INDEX IF NOT EXISTS idx_tags_tag ON tags(tag);
