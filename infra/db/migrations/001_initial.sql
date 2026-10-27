CREATE TABLE IF NOT EXISTS reviews(
  id TEXT PRIMARY KEY,
  case_id TEXT NOT NULL,
  draft TEXT NOT NULL,
  hash TEXT NOT NULL,
  status TEXT NOT NULL,
  reviewer TEXT
);

CREATE TABLE IF NOT EXISTS records(
  id TEXT PRIMARY KEY,
  case_id TEXT NOT NULL UNIQUE,
  draft TEXT NOT NULL,
  review_id TEXT NOT NULL,
  dataset_version TEXT
);

CREATE TABLE IF NOT EXISTS keys(
  key TEXT PRIMARY KEY,
  hash TEXT NOT NULL,
  response TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS calls(
  case_id TEXT NOT NULL,
  tool TEXT NOT NULL,
  n INTEGER NOT NULL,
  PRIMARY KEY(case_id, tool)
);

CREATE TABLE IF NOT EXISTS audit(
  seq BIGSERIAL PRIMARY KEY,
  at TEXT NOT NULL,
  event TEXT NOT NULL,
  case_id TEXT,
  detail TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workbench_state(
  id INTEGER PRIMARY KEY CHECK (id = 1),
  document TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS reviews_status_idx ON reviews(status);

CREATE INDEX IF NOT EXISTS audit_case_idx ON audit(case_id);
