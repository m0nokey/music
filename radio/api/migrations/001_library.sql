CREATE TABLE IF NOT EXISTS tracks (
    filename text PRIMARY KEY,
    storage_backend text NOT NULL DEFAULT 'local' CHECK (storage_backend IN ('local', 's3')),
    storage_key text NOT NULL,
    artist text NOT NULL DEFAULT '',
    title text NOT NULL DEFAULT '',
    duration_seconds double precision,
    file_size_bytes bigint NOT NULL DEFAULT 0 CHECK (file_size_bytes >= 0),
    file_mtime_ns bigint NOT NULL DEFAULT 0,
    content_sha256 char(64),
    metadata_source text NOT NULL DEFAULT 'auto' CHECK (metadata_source IN ('auto', 'manual')),
    is_available boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS tracks_content_sha256_idx ON tracks (content_sha256);
CREATE INDEX IF NOT EXISTS tracks_display_idx ON tracks (lower(artist), lower(title));

CREATE TABLE IF NOT EXISTS playlists (
    slot smallint PRIMARY KEY CHECK (slot BETWEEN 1 AND 8),
    name text NOT NULL,
    ascii_art text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS playlist_tracks (
    playlist_slot smallint NOT NULL REFERENCES playlists(slot) ON DELETE CASCADE,
    track_filename text NOT NULL REFERENCES tracks(filename) ON DELETE CASCADE,
    position integer NOT NULL CHECK (position >= 0),
    added_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (playlist_slot, track_filename),
    UNIQUE (playlist_slot, position)
);

CREATE TABLE IF NOT EXISTS live_queue (
    id bigserial PRIMARY KEY,
    track_filename text NOT NULL REFERENCES tracks(filename) ON DELETE CASCADE,
    position bigint NOT NULL UNIQUE,
    requested_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS playback_settings (
    id smallint PRIMARY KEY CHECK (id = 1),
    live_playing boolean NOT NULL DEFAULT true,
    repeat_mode text NOT NULL DEFAULT 'off' CHECK (repeat_mode IN ('off', 'one', 'all')),
    shuffle boolean NOT NULL DEFAULT false,
    updated_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO playlists (slot, name)
VALUES
    (1, 'PLAYLIST 01'), (2, 'PLAYLIST 02'),
    (3, 'PLAYLIST 03'), (4, 'PLAYLIST 04'),
    (5, 'PLAYLIST 05'), (6, 'PLAYLIST 06'),
    (7, 'PLAYLIST 07'), (8, 'PLAYLIST 08')
ON CONFLICT (slot) DO NOTHING;

INSERT INTO playback_settings (id) VALUES (1)
ON CONFLICT (id) DO NOTHING;
