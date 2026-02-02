# Fixing `bad object refs/remotes/origin/main` and "did not send all necessary objects"

## Cause

- A **loose** remote-tracking ref (e.g. `.git/refs/remotes/origin/main`) pointed to a commit the remote no longer has or didn’t send.
- Git uses that ref and tries to load that object → "bad object" and "did not send all necessary objects".

## Fix applied

1. **Remove the bad ref file** (if you see "main 2" in the error):
   ```bash
   rm ".git/refs/remotes/origin/main 2"
   ```
2. **Optionally remove a bad loose `main` ref** so Git uses the packed ref:
   ```bash
   rm .git/refs/remotes/origin/main
   ```
3. **Refresh from remote**:
   ```bash
   git fetch origin
   git pull --tags origin main
   ```

## If it happens again

Run from the repo root:

```bash
rm -f ".git/refs/remotes/origin/main 2" .git/refs/remotes/origin/main
git fetch origin
git pull --tags origin main
```

If another branch shows the same error, replace `main` with that branch name in both the `rm` path and the `git pull` command.
