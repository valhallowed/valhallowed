-- The web map's one read of the data layer (B6): each world's players in its newest session (by
-- when intake took it) who have not left, and where they were last seen. Such a player has a
-- position there, and their latest presence row in that session is a join, is missing (a player
-- whose name cleaned to nothing sends positions and no join), or is a leave older than their
-- latest position.
--
-- The view does not check how recent that position is. A live player was also seen within three
-- position intervals (docs/architecture.md § Data layer); the web map makes that check in its own
-- query, on received_at, the database's clock (web-map/src/server/players.ts).
--
-- No id is a column, neither a player's nor a session's: a platform id never leaves the data layer
-- through the web map. A player shows by name, null until intake takes their join.
--
-- A world is one lookup in session_newest, and each player one in position_latest and
-- presence_latest, never a scan of a session's positions.
--
-- The view is the web map's whole privilege, so it is a security barrier: a condition the reader
-- adds runs only on the rows the view lets through, and cannot see where a player who left was.

CREATE VIEW live_player WITH (security_barrier) AS
SELECT w.name AS world, p.name, pos.x, pos.y, pos.z, pos.at, pos.received_at
FROM world w
CROSS JOIN LATERAL (
    SELECT s.id FROM session s WHERE s.world_id = w.id ORDER BY s.received_at DESC LIMIT 1
) newest
CROSS JOIN player p
CROSS JOIN LATERAL (
    SELECT x, y, z, at, received_at FROM position
    WHERE session_id = newest.id AND player_id = p.id
    ORDER BY at DESC LIMIT 1
) pos
LEFT JOIN LATERAL (
    SELECT joined, at FROM presence
    WHERE session_id = newest.id AND player_id = p.id
    ORDER BY at DESC LIMIT 1
) pres ON true
WHERE coalesce(pres.joined, true) OR pres.at < pos.at;
