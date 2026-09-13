# Runbooks

Operational procedures for Manifold on the NAS. Everything here assumes the container's
`/data` is `/mnt/user/appdata/manifold` on Unraid and that `MANIFOLD_MASTER_KEY` lives in a
password manager. Nothing in these steps prints a credential value.

## Backups

Snapshots are consistent copies of `manifold.db` written with SQLite's online backup API,
so a snapshot never blocks a tool call. They live in `/data/backups` as
`manifold-<UTC stamp>-<reason>.db`. The newest 14 are kept; older ones are deleted when a
new one is written. Reasons:

| Reason | When |
|---|---|
| `daily` | Two minutes after boot, then every 24 hours |
| `manual` | The Snapshot now button on Settings |
| `pre-migration` | Before a schema migration on boot |
| `pre-restore` | Before a restore replaces the live database |
| `pre-rotation` | Before a master key rotation |

Credentials inside a snapshot are still encrypted with the master key in force when it was
taken. A snapshot without the matching key holds no usable credentials.

Settings lists the snapshots with download links. Point Unraid's appdata backup at
`/mnt/user/appdata/manifold/backups` for an off-box copy; the files are safe to copy while the
gateway runs. Do not copy the live `manifold.db` from a running container, it can be
mid-write.

## Restore

Two steps, both on the Settings page under Restore.

1. Upload and validate. The file is checked for integrity, for being a Manifold database,
   for a schema version this build can run, and its key check row must decrypt under the
   current `MANIFOLD_MASTER_KEY`. The report shows schema version and row counts for the
   live database beside the snapshot, with a warning for every table where they differ.
   Audit rows will always differ, that is expected.
2. Confirm restore, typed. The live database is snapshotted as `pre-restore`, the validated
   file is staged as `manifold.db.restore`, and the gateway exits. Docker restarts it and
   the staged file is swapped in before the database opens.

Reload Settings after about ten seconds. The Backups list shows the `pre-restore` snapshot;
if the restore was a mistake, download it and restore it the same way.

Connectors in claude.ai keep working as long as the snapshot contains their OAuth clients
and tokens. A snapshot older than the token lifetime means claude.ai refreshes on its next
call; a snapshot from before the connector was registered means reconnecting it.

If the gateway does not come back, use the shell path:

```bash
docker stop manifold
cp /mnt/user/appdata/manifold/backups/<snapshot>.db /mnt/user/appdata/manifold/manifold.db.restore
docker start manifold
```

The staged file is applied on boot exactly as the UI path is. Never edit `manifold.db`
in place while the container runs.

## Master key rotation

Rotation re-encrypts every stored credential and the key check row under a new key, inside
one transaction, after a `pre-rotation` snapshot. It refuses to run if the current key is
wrong, if the new key equals the current one, or if the snapshot fails.

1. Generate the new key and store it in the password manager before anything else:
   ```bash
   head -c 32 /dev/urandom | base64
   ```
2. Run the rotation inside the running container. The current key comes from the
   container's environment; only the new one is passed on the command line.
   ```bash
   docker exec manifold python -m manifold rotate-key --new-key '<new base64 key>'
   ```
   It prints how many credentials were rotated. Credentials created between this step and
   the restart in step 4 would be written under the old key, so do the next two steps
   straight away.
3. In the Unraid Docker UI, edit the Manifold container and set `MANIFOLD_MASTER_KEY` to
   the new value.
4. Apply. Unraid recreates the container; the log should show `manifold started` and no
   `MANIFOLD_MASTER_KEY does not match` line. Open Credentials and Test connection on one
   credential to prove decryption.
5. Take a Snapshot now. Every snapshot before this point is encrypted under the old key,
   so keep the old key in the password manager until those have aged out of the 14.

If step 4 fails with a key mismatch, the environment value is wrong. Fix it and apply again;
nothing else is needed because the rotation already committed.

## Restart policy

The restart button and the restore confirm both exit the process and rely on Docker
starting it again. The Unraid template carries `--restart=unless-stopped` in Extra
Parameters. A container created from an older template needs that added by hand, or a
restart leaves it stopped until started from the Docker page.

## Audit log size

The audit log is pruned daily in batches of 500 rows: first anything older than the
retention setting, then the oldest rows past the row cap. Both settings and a Prune now
button are on Settings. The Audit page shows the row count, the oldest row and, when the
cap fired on the last prune, how many rows it removed. Rows going by cap means something is
calling tools faster than retention alone would allow; check the Audit page for the
toolset and tool doing it. Space is reclaimed by incremental vacuum, never a full one.

## Unraid Connect and the BigInt overflow

The Unraid GraphQL API returns some byte counts as 32-bit Int, and `vars.mdResyncSize`
overflows on large arrays. Manifold drops the overflowing leaf at runtime, retries, and
shows it as a note on the toolset card. The Unraid Connect plugin ships the API build with
the BigInt fix; once the NAS runs it, the note disappears on its own and the workaround
can be retired.
