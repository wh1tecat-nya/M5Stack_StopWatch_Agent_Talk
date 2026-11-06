CREATE TABLE subscription (
    slot INTEGER PRIMARY KEY CHECK (slot = 1),
    id TEXT NOT NULL,
    url TEXT NOT NULL,
    secret TEXT NOT NULL,
    expires INTEGER NOT NULL,
    verified INTEGER NOT NULL,
    old_secret TEXT,
    rotate_until INTEGER
);

CREATE TABLE turns (
    id TEXT PRIMARY KEY,
    subscription_id TEXT NOT NULL,
    created INTEGER NOT NULL,
    expires INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'delivering', 'waiting', 'ready', 'failed')),
    lease_until INTEGER,
    attempts INTEGER NOT NULL DEFAULT 0,
    answer TEXT
);
CREATE INDEX turns_expires ON turns(expires);
