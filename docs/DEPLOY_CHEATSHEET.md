# Deploy Cheat Sheet

Quick reference for all deployment operations.

---

## Standard deploy

```powershell
cd "G:\My Drive\GIT_ROOT\Playground"
& "$env:USERPROFILE\.fly\bin\fly.exe" deploy --remote-only
```

⚠️ **Always use `--remote-only` on Windows.** Without it, Fly looks for a local Docker daemon which doesn't exist and hangs indefinitely.

---

## Force rebuild (skip cache)

```powershell
& "$env:USERPROFILE\.fly\bin\fly.exe" deploy --remote-only --no-cache
```

---

## Check deploy status

```powershell
& "$env:USERPROFILE\.fly\bin\fly.exe" status --app oraclereadinesssrc-dzxnqq
```

---

## Watch live logs

```powershell
& "$env:USERPROFILE\.fly\bin\fly.exe" logs --app oraclereadinesssrc-dzxnqq
```

---

## Get the READINESS_TOKEN

```powershell
& "$env:USERPROFILE\.fly\bin\fly.exe" ssh console `
    --app oraclereadinesssrc-dzxnqq `
    -C "printenv READINESS_TOKEN"
```

---

## Set a new secret

```powershell
& "$env:USERPROFILE\.fly\bin\fly.exe" secrets set `
    --app oraclereadinesssrc-dzxnqq `
    READINESS_TOKEN=<new-value>
```

---

## SSH into the container

```powershell
& "$env:USERPROFILE\.fly\bin\fly.exe" ssh console --app oraclereadinesssrc-dzxnqq
```

---

## Upload a diagnostic script to /data

```powershell
# Open SFTP shell
& "$env:USERPROFILE\.fly\bin\fly.exe" sftp shell --app oraclereadinesssrc-dzxnqq
```

In the SFTP prompt:
```
put check_db.py /data/check_db.py
exit
```

Then SSH in and run: `python /data/check_db.py`

---

## Quick DB row count (one-liner)

```powershell
& "$env:USERPROFILE\.fly\bin\fly.exe" ssh console `
    --app oraclereadinesssrc-dzxnqq `
    -C "python -c \"import sqlite3; db=sqlite3.connect('/data/readiness.db'); [print(t, db.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]) for t in ['features','feature_details']]\""
```

---

## Health check URLs

```bash
curl https://oraclereadinesssrc-dzxnqq.fly.dev/health
curl https://oraclereadinesssrc-dzxnqq.fly.dev/framer-metadata
curl "https://oraclereadinesssrc-dzxnqq.fly.dev/api/ica/features.csv?release=26C" | head -3
```

---

## Common deploy failures

| Symptom | Fix |
|---|---|
| Deploy hangs at "building image" | Add `--remote-only` |
| `TOML parse error` | Check `fly.toml` — `force_https` must be boolean `true` not string `"true"` |
| `volume not found` | `fly volumes list --app oraclereadinesssrc-dzxnqq` — check name matches fly.toml |
| App crashes on start | `fly logs` — look for Python traceback |
| 403 on /mcp after deploy | Token changed — `fly secrets show` and update Bob config |
