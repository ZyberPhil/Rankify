DELETE FROM team_members
WHERE rowid NOT IN (
    SELECT MIN(rowid)
    FROM team_members
    GROUP BY user_id
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_team_members_user_unique
ON team_members(user_id);
