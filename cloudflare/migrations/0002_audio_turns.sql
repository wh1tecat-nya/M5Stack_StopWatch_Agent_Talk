ALTER TABLE turns ADD COLUMN audio_phase TEXT;
ALTER TABLE turns ADD COLUMN recording BLOB;
ALTER TABLE turns ADD COLUMN transcript TEXT;
ALTER TABLE turns ADD COLUMN audio_lease INTEGER NOT NULL DEFAULT 0;
ALTER TABLE turns ADD COLUMN audio_attempts INTEGER NOT NULL DEFAULT 0;

CREATE INDEX turns_audio_queue ON turns(audio_phase, status, created)
  WHERE audio_phase IS NOT NULL;
