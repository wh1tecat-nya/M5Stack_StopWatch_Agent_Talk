CREATE TABLE ui_bridge (
    slot INTEGER PRIMARY KEY CHECK (slot = 1),
    session_id TEXT NOT NULL,
    expires INTEGER NOT NULL
);
