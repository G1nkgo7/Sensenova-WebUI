"""SQLite layer for pptagent_static_web_demo Studio (stdlib only).

One DB file at studio/data/studio.db. Open a fresh connection per request via
connect(); FastAPI runs sync route fns in a threadpool so short sqlite calls are
fine at this scale. ``STUDIO_SQLITE_JOURNAL_MODE`` can select a filesystem-safe
journal mode for deployments whose data directory lives on shared storage.
"""
import os
import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("STUDIO_DATA_DIR", BASE_DIR / "data")).expanduser()
DB_PATH = DATA_DIR / "studio.db"
WORKSPACES_DIR = DATA_DIR / "workspaces"
SQLITE_JOURNAL_MODE = os.environ.get("STUDIO_SQLITE_JOURNAL_MODE", "WAL").upper()
if SQLITE_JOURNAL_MODE not in {"DELETE", "TRUNCATE", "PERSIST", "MEMORY", "WAL", "OFF"}:
    SQLITE_JOURNAL_MODE = "WAL"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  username      TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  display_name  TEXT,
  role          TEXT NOT NULL DEFAULT 'user',   -- 'user' | 'admin'
  is_active     INTEGER NOT NULL DEFAULT 1,
  created_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
  token       TEXT PRIMARY KEY,
  user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at  INTEGER NOT NULL,
  expires_at  INTEGER NOT NULL,
  ip          TEXT,
  user_agent  TEXT
);

CREATE TABLE IF NOT EXISTS conversations (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  title      TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  role            TEXT NOT NULL,    -- 'user' | 'assistant' | 'system'
  content         TEXT,
  deck_id         INTEGER,          -- assistant msg may reference a generated deck
  created_at      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS decks (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  conversation_id INTEGER REFERENCES conversations(id) ON DELETE SET NULL,
  batch_id        INTEGER,
  batch_index     INTEGER,
  parent_deck_id  INTEGER REFERENCES decks(id) ON DELETE SET NULL,
  revision_no     INTEGER NOT NULL DEFAULT 0,
  revision_instruction TEXT,
  title           TEXT,
  seed_json       TEXT,             -- {query, lang, slide_count, ...}
  status          TEXT NOT NULL DEFAULT 'queued',  -- waiting|queued|running|completed|failed|canceled
  model           TEXT,                            -- 模型选择(engine.MODELS 的 key)
  pipeline        TEXT,                            -- pipeline 版本(engine.PIPELINES 的 key)
  skill_version   TEXT,                            -- skill 版本(engine.SKILLS 的 key)
  run_dir         TEXT,
  slide_count     INTEGER,
  error           TEXT,
  created_at      INTEGER NOT NULL,
  started_at      INTEGER,
  finished_at     INTEGER
);

CREATE TABLE IF NOT EXISTS batches (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name            TEXT,
  source_name     TEXT,
  model           TEXT,
  pipeline        TEXT,
  skill_version   TEXT,
  total_count     INTEGER NOT NULL,
  created_at      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS deck_usage (
  deck_id          INTEGER PRIMARY KEY REFERENCES decks(id) ON DELETE CASCADE,
  user_id          INTEGER NOT NULL,
  model            TEXT,
  input_tokens     INTEGER DEFAULT 0,
  output_tokens    INTEGER DEFAULT 0,
  image_count      INTEGER DEFAULT 0,
  web_search_count INTEGER DEFAULT 0,
  est_cost_usd     REAL DEFAULT 0,
  duration_s       REAL DEFAULT 0,
  n_slides         INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS custom_models (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  display_name  TEXT NOT NULL,
  model_id      TEXT NOT NULL,
  base_url      TEXT NOT NULL,
  api_key_enc   TEXT,
  vision_enabled INTEGER NOT NULL DEFAULT 1,
  is_active     INTEGER NOT NULL DEFAULT 1,
  created_at    INTEGER NOT NULL,
  updated_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS history_preferences (
  user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  item_kind   TEXT NOT NULL,                         -- 'static' | 'dynamic'
  item_id     TEXT NOT NULL,
  pinned      INTEGER NOT NULL DEFAULT 0,
  updated_at  INTEGER NOT NULL,
  PRIMARY KEY (user_id, item_kind, item_id)
);

CREATE INDEX IF NOT EXISTS idx_conv_user  ON conversations(user_id);
CREATE INDEX IF NOT EXISTS idx_msg_conv   ON messages(conversation_id);
CREATE INDEX IF NOT EXISTS idx_decks_user ON decks(user_id);
CREATE INDEX IF NOT EXISTS idx_batches_user ON batches(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_custom_models_user_active
  ON custom_models(user_id, is_active, id);
CREATE INDEX IF NOT EXISTS idx_history_preferences_user
  ON history_preferences(user_id, pinned, updated_at);
"""


def connect():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    WORKSPACES_DIR.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: FastAPI 的同步依赖 setup/teardown 可能在
    # 线程池的不同线程执行;连接是每请求独享的,放开该检查是安全的。
    con = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    con.row_factory = sqlite3.Row
    # SQLite WAL relies on a reliable shared-memory (-shm) implementation.
    # AFS does not provide those semantics, so CCI selects DELETE mode.
    con.execute(f"PRAGMA journal_mode={SQLITE_JOURNAL_MODE}")
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=5000")
    return con


def init_db():
    con = connect()
    try:
        con.executescript(SCHEMA)
        # 老库迁移:新增列只做 additive migration,不重建表。
        cols = [r[1] for r in con.execute("PRAGMA table_info(decks)")]
        if "model" not in cols:
            con.execute("ALTER TABLE decks ADD COLUMN model TEXT")
        if "pipeline" not in cols:
            con.execute("ALTER TABLE decks ADD COLUMN pipeline TEXT")
        if "skill_version" not in cols:
            con.execute("ALTER TABLE decks ADD COLUMN skill_version TEXT")
        if "batch_id" not in cols:
            con.execute("ALTER TABLE decks ADD COLUMN batch_id INTEGER")
        if "batch_index" not in cols:
            con.execute("ALTER TABLE decks ADD COLUMN batch_index INTEGER")
        if "parent_deck_id" not in cols:
            con.execute(
                "ALTER TABLE decks ADD COLUMN parent_deck_id INTEGER "
                "REFERENCES decks(id) ON DELETE SET NULL"
            )
        if "revision_no" not in cols:
            con.execute("ALTER TABLE decks ADD COLUMN revision_no INTEGER NOT NULL DEFAULT 0")
        if "revision_instruction" not in cols:
            con.execute("ALTER TABLE decks ADD COLUMN revision_instruction TEXT")
        con.execute("CREATE INDEX IF NOT EXISTS idx_decks_batch ON decks(batch_id, batch_index)")
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_decks_parent "
            "ON decks(parent_deck_id, revision_no)"
        )
        con.commit()
    finally:
        con.close()
