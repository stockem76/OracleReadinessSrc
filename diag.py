import socket, subprocess, os, sys

# 1. Check port 8080
s = socket.socket()
r = s.connect_ex(('127.0.0.1', 8080))
s.close()
print(f"port 8080: {'OPEN' if r == 0 else 'CLOSED (error '+str(r)+')'}")

# 2. Try to import server and catch any import error
try:
    import importlib.util
    spec = importlib.util.spec_from_file_location("server", "/app/server.py")
    # just compile, don't execute
    with open("/app/server.py") as f:
        compile(f.read(), "server.py", "exec")
    print("server.py: compiles OK")
except Exception as e:
    print(f"server.py compile error: {e}")

# 3. Check env vars
token = os.environ.get("READINESS_TOKEN", "")
print(f"READINESS_TOKEN set: {bool(token)}")
print(f"READINESS_DATA_DIR: {os.environ.get('READINESS_DATA_DIR', '/data')}")

# 4. Check /data exists and db
data_dir = os.environ.get("READINESS_DATA_DIR", "/data")
db_path = os.path.join(data_dir, "readiness.db")
print(f"DB exists: {os.path.exists(db_path)}")
if os.path.exists(db_path):
    import sqlite3
    db = sqlite3.connect(db_path)
    for t in ["features", "feature_details"]:
        try:
            c = db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            print(f"  {t}: {c} rows")
        except Exception as e:
            print(f"  {t}: ERROR {e}")

# 5. Check if server process is running
try:
    with open("/proc/1/cmdline", "rb") as f:
        cmdline = f.read().replace(b'\x00', b' ').decode(errors='replace')
    print(f"PID 1 cmdline: {cmdline}")
except Exception as e:
    print(f"PID 1: {e}")

# 6. List running python processes via /proc
pids = [d for d in os.listdir('/proc') if d.isdigit()]
for pid in pids:
    try:
        with open(f'/proc/{pid}/cmdline', 'rb') as f:
            cmd = f.read().replace(b'\x00', b' ').decode(errors='replace').strip()
        if 'python' in cmd.lower() or 'server' in cmd.lower():
            print(f"  proc {pid}: {cmd[:120]}")
    except:
        pass

print("diag done")
