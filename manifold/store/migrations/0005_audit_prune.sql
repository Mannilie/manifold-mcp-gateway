-- Phase 6: remember when the audit log was last pruned and why, for the dashboard.
CREATE TABLE audit_prune_log (
    id          INTEGER PRIMARY KEY,
    ts          TEXT NOT NULL,
    by_age      INTEGER NOT NULL,
    by_cap      INTEGER NOT NULL,
    rows_after  INTEGER NOT NULL
);
