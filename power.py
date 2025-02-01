import subprocess
import os
import datetime
import sqlite3
from prettytable import PrettyTable
import psutil
import re

log_db = "~/power.db"

app_slice_regex = re.compile(r".*app\.slice/(.+)-[0-9]+\.scope")

def adapt_datetime(ts):
    return ts.strftime('%Y-%m-%d %H:%M:%S.%f')

def convert_datetime(s):
    return datetime.datetime.strptime(s.decode('utf-8'), '%Y-%m-%d %H:%M:%S.%f')

sqlite3.register_adapter(datetime.datetime, adapt_datetime)
sqlite3.register_converter('timestamp', convert_datetime)

conn = sqlite3.connect(os.path.expanduser(log_db), detect_types=sqlite3.PARSE_DECLTYPES)
c = conn.cursor()

# Create the database if it doesn't exist
c.execute("CREATE TABLE IF NOT EXISTS power (time TIMESTAMP, app TEXT, power REAL, pid INTEGER)")

def is_subprocess(pid, my_pid, children):
    if pid == my_pid:
        return True

    for child in children:
        if pid == child.pid:
            return True
    return False

def process_exists(pid):
    return psutil.pid_exists(pid)


def get_cpu_percent():
    # Get the current power draw for each app
    power = subprocess.check_output(["ps", "-eo", "pid,pcpu", "--no-header"]).decode("utf-8")
    process = psutil.Process()
    children = process.children(recursive=True)
    lines = power.split("\n")
    apps = {}
    for line in lines:
        if line == "":
            continue

        sections = [section for section in line.split(' ') if section != ""]
        pid = sections[0]
        pcpu = float(sections[-1])
        # If the pid is a subprocess of the current process, ignore it
        if is_subprocess(int(pid), process.pid, children) or not process_exists(int(pid)):
            continue
        app_process = psutil.Process(int(pid))

        # Get the cgroup of the app_process
        with open(f"/proc/{app_process.pid}/cgroup") as f:
            cgroup = f.read().strip()

        if cgroup.endswith('flatpak-session-helper.service'):
            # Handle apps wrapped by the flatpak-session-helper
            roots = ['systemd', 'bwrap']
            parent = app_process.parent()
            while parent is not None and parent.name() not in roots:
                app_process = parent
                parent = app_process.parent()
            app = app_process.name()
        elif 'app.slice' not in cgroup:
            app = 'System'
        else:
            match = app_slice_regex.match(cgroup)
            if match:
                # TODO: Get user displayable name based on app type
                app = match.group(1).replace('app-flatpak-', '').replace('app-gnome-', '').split('\\')[0]
            else:
                app = 'System'
        if app in apps:
            apps[app] += pcpu
        else:
            apps[app] = pcpu
    return apps

cpu = get_cpu_percent()

# c.execute("DELETE FROM power")
# conn.commit()

# Log the power usage
for app in cpu:
    if cpu[app] < 0.01:
        continue
    c.execute("INSERT INTO power VALUES (?, ?, ?)", (datetime.datetime.now(), app, cpu[app]))
conn.commit()

# Print the power usage from the last hour
start_time = datetime.datetime.now() - datetime.timedelta(hours=1)
c.execute("SELECT app, power FROM power WHERE time > ?", (start_time,))
power = c.fetchall()

apps = {}
for app in power:
    if app[0] in apps:
        apps[app[0]] += float(app[1])
    else:
        apps[app[0]] = float(app[1])

total = sum(apps.values())

for app in apps:
    apps[app] = apps[app] / total * 100

power = sorted(apps.items(), key=lambda x: x[1], reverse=True)

# Delete readings older than 10 hours
c.execute("DELETE FROM power WHERE time < ?", (datetime.datetime.now() - datetime.timedelta(hours=10),))
conn.commit()

# Create a PrettyTable object
table = PrettyTable()
table.field_names = ["App", "Power Usage (%)", "Active"]

# Add rows to the table
for app in power:
    table.add_row([app[0], f"{app[1]:.2f}", "Yes" if app[0] in cpu else "No"])

# Print the table
print("Power usage in the last hour:")
print(table)

conn.close()
