-- Telemetry from the plugin (B2), as intake takes it. A world is known by its save name, a
-- player by their platform id; a name is the player's own choice, so it is stored, never a key.

CREATE TABLE world (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name text NOT NULL UNIQUE
);

-- One load of a world. The plugin names it; intake fences it to the world that first sent it.
CREATE TABLE session (
    id uuid PRIMARY KEY,
    world_id bigint NOT NULL REFERENCES world,
    started_at timestamptz,
    received_at timestamptz NOT NULL DEFAULT now()
);
-- A world's newest session, where its live players are.
CREATE INDEX session_newest ON session (world_id, received_at DESC);

CREATE TABLE player (
    id text PRIMARY KEY,
    -- Unknown until intake takes the player's join.
    name text,
    seen_at timestamptz NOT NULL
);

CREATE TABLE presence (
    session_id uuid NOT NULL REFERENCES session,
    player_id text NOT NULL REFERENCES player,
    joined boolean NOT NULL,
    at timestamptz NOT NULL,
    received_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX presence_latest ON presence (session_id, player_id, at DESC);

CREATE TABLE position (
    session_id uuid NOT NULL REFERENCES session,
    player_id text NOT NULL REFERENCES player,
    at timestamptz NOT NULL,
    x double precision NOT NULL,
    y double precision NOT NULL,
    z double precision NOT NULL,
    received_at timestamptz NOT NULL DEFAULT now()
);
-- Each player's latest position in a session.
CREATE INDEX position_latest ON position (session_id, player_id, at DESC);
