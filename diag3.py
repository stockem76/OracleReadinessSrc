import os, subprocess

# Find what is holding port 8080
try:
    with open('/proc/net/tcp') as f:
        lines = f.readlines()
    # Port 8080 in hex = 1F90
    for line in lines:
        if '1F90' in line.upper():
            print('tcp entry:', line.strip())
except Exception as e:
    print('tcp read error:', e)

# Find all python processes and their ports
pids = [d for d in os.listdir('/proc') if d.isdigit()]
python_pids = []
for pid in pids:
    try:
        with open(f'/proc/{pid}/cmdline', 'rb') as f:
            cmd = f.read().replace(b'\x00', b' ').decode(errors='replace').strip()
        if 'python' in cmd.lower():
            python_pids.append((pid, cmd[:120]))
    except:
        pass

print('Python processes:')
for pid, cmd in python_pids:
    print(f'  PID {pid}: {cmd}')

# Check which image is running
try:
    with open('/proc/1/cmdline', 'rb') as f:
        cmd = f.read().replace(b'\x00', b' ').decode(errors='replace')
    print(f'PID 1: {cmd}')
except Exception as e:
    print(f'PID 1 error: {e}')

# Check server.py modification time to confirm new image
import os.path
mtime = os.path.getmtime('/app/server.py')
import datetime
print(f'server.py mtime: {datetime.datetime.utcfromtimestamp(mtime)}')

# Check if the new _FRAMER_PROJECT_URL is in the deployed code
with open('/app/server.py') as f:
    content = f.read()
if '_FRAMER_PROJECT_URL' in content:
    print('_FRAMER_PROJECT_URL: FOUND in /app/server.py (new image deployed)')
else:
    print('_FRAMER_PROJECT_URL: NOT FOUND (old image still running)')
