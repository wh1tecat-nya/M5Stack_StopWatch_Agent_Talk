CREATE TABLE conversation_history (
    id TEXT PRIMARY KEY,
    created INTEGER NOT NULL,
    transcript TEXT,
    answer TEXT,
    status TEXT NOT NULL,
    answered INTEGER
);
CREATE INDEX conversation_history_created ON conversation_history(created DESC, id DESC);

INSERT INTO conversation_history (id, created, transcript, answer, status, answered)
SELECT id, created, transcript, answer, status,
    CASE WHEN answer IS NOT NULL THEN unixepoch('now') * 1000 ELSE NULL END
FROM turns WHERE audio_phase IS NOT NULL AND (transcript IS NOT NULL OR answer IS NOT NULL);

CREATE TRIGGER archive_audio_turn
AFTER UPDATE OF transcript, answer, status ON turns
WHEN NEW.audio_phase IS NOT NULL
BEGIN
    INSERT INTO conversation_history (id, created, transcript, answer, status, answered)
    VALUES (NEW.id, NEW.created, NEW.transcript, NEW.answer, NEW.status,
        CASE WHEN NEW.answer IS NOT NULL THEN unixepoch('now') * 1000 ELSE NULL END)
    ON CONFLICT(id) DO UPDATE SET
        transcript = COALESCE(excluded.transcript, conversation_history.transcript),
        answer = COALESCE(excluded.answer, conversation_history.answer),
        status = excluded.status,
        answered = COALESCE(conversation_history.answered, excluded.answered);
END;
