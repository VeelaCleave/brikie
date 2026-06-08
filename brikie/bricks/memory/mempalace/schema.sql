-- MemPalace Schema — Spatial/Temporal Knowledge Graph
-- Phase 3.2: Tripartite Memory Architecture

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- Entities: Core nodes in the knowledge graph
CREATE TABLE IF NOT EXISTS entities (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    entity_type TEXT NOT NULL CHECK(entity_type IN ('person', 'project', 'tool', 'concept', 'decision', 'milestone')),
    session_id TEXT,
    description TEXT,
    created_at TEXT DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now', 'utc')),
    valid_from TEXT,
    valid_to TEXT
);

-- Triples: RDF-style relationships between entities
CREATE TABLE IF NOT EXISTS triples (
    id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL,
    predicate TEXT NOT NULL,
    object_id TEXT NOT NULL,
    valid_from TEXT,
    valid_to TEXT,
    confidence REAL DEFAULT 0.8,
    source_message_index INTEGER,
    created_at TEXT DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now', 'utc'))
);

-- Spatial Map: Maps entities to the MemPalace spatial hierarchy
CREATE TABLE IF NOT EXISTS spatial_map (
    id TEXT PRIMARY KEY,
    entity_id TEXT NOT NULL,
    wing TEXT,
    room TEXT,
    hall TEXT,
    tunnel TEXT,
    drawer TEXT,
    distance REAL DEFAULT 1.0,
    created_at TEXT DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now', 'utc'))
);

-- Spatial Regions: Hierarchical containers (wings, rooms, halls, tunnels, drawers)
CREATE TABLE IF NOT EXISTS spatial_regions (
    id TEXT PRIMARY KEY,
    region_type TEXT NOT NULL CHECK(region_type IN ('wing', 'room', 'hall', 'tunnel', 'drawer')),
    name TEXT NOT NULL,
    parent_id TEXT,
    description TEXT,
    session_id TEXT,
    chroma_collection TEXT,
    chroma_document_id TEXT,
    created_at TEXT DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now', 'utc'))
);

-- Indexes for efficient queries
CREATE INDEX IF NOT EXISTS idx_entities_name ON entities(name);
CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(entity_type);
CREATE INDEX IF NOT EXISTS idx_entities_session ON entities(session_id);
CREATE INDEX IF NOT EXISTS idx_triples_subject ON triples(subject_id);
CREATE INDEX IF NOT EXISTS idx_triples_predicate ON triples(predicate);
CREATE INDEX IF NOT EXISTS idx_triples_object ON triples(object_id);
CREATE INDEX IF NOT EXISTS idx_triples_valid_from ON triples(valid_from);
CREATE INDEX IF NOT EXISTS idx_triples_valid_to ON triples(valid_to);
CREATE INDEX IF NOT EXISTS idx_spatial_map_entity ON spatial_map(entity_id);
CREATE INDEX IF NOT EXISTS idx_spatial_map_wing ON spatial_map(wing);
CREATE INDEX IF NOT EXISTS idx_spatial_map_room ON spatial_map(room);
CREATE INDEX IF NOT EXISTS idx_spatial_regions_type ON spatial_regions(region_type);
