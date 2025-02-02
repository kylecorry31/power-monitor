import subprocess
import os
import datetime
import sqlite3
from prettytable import PrettyTable
import psutil
import re

log_db = "~/power.db"

app_slice_regex = re.compile(r".*app\.slice/(app-.+)-[0-9]+\.scope")
discharging_regex = re.compile(r".*state:\s*discharging", re.DOTALL)
percent_regex = re.compile(r".*percentage:\s*([0-9.]+)%.*", re.DOTALL)
energy_regex = re.compile(r".*energy:\s*([0-9.]+)\s*Wh.*", re.DOTALL)
energy_full_regex = re.compile(r".*energy-full:\s*([0-9.]+)\s*Wh.*", re.DOTALL)

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
c.execute("CREATE TABLE IF NOT EXISTS battery (time TIMESTAMP, charging BOOLEAN, percent INTEGER, energy REAL, energy_full REAL)")

def is_subprocess(pid, my_pid, children):
    if pid == my_pid:
        return True

    for child in children:
        if pid == child.pid:
            return True
    return False

def process_exists(pid):
    return psutil.pid_exists(pid)

def get_battery_info():
    # TODO: Determine the correct battery
    output = subprocess.check_output(["upower", "-i", "/org/freedesktop/UPower/devices/battery_BAT1"]).decode("utf-8")
    discharging = discharging_regex.match(output) is not None
    percent = percent_regex.match(output)
    if percent is not None:
        percent = int(percent.group(1))
    energy = energy_regex.match(output)
    if energy is not None:
        energy = float(energy.group(1))
    energy_full = energy_full_regex.match(output)
    if energy_full is not None:
        energy_full = float(energy_full.group(1))
    return {"discharging": discharging, "percent": percent, "energy": energy, "energy_full": energy_full}

def get_cpu_percent():
    # Get the current power draw for each app
    power = subprocess.check_output(["ps", "-eo", "pid,pcpu", "--no-header"]).decode("utf-8")
    process = psutil.Process()
    children = process.children(recursive=True)
    lines = power.split("\n")
    apps = {}
    flatpak_map = {}

    # List all flatpak apps
    try:
        flatpak_list = subprocess.check_output(["flatpak", "list"]).decode("utf-8")
        flatpak_apps = flatpak_list.split("\n")
        for app in flatpak_apps:
            if app == "":
                continue
            app_name = app.split("\t")[0]
            app_id = app.split("\t")[1]
            flatpak_map[app_id] = app_name
    except:
        pass

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
            # TODO: Improve detection
            if app == 'flatpak-session-helper' or app == 'p11-kit-server':
                app = 'System'
        else:
            match = app_slice_regex.match(cgroup)
            if match:
                app = match.group(1)

                if app.startswith('app-flatpak-'):
                    app = app.replace('app-flatpak-', '')
                    if app in flatpak_map:
                        app = flatpak_map[app]
                else:
                    # TODO: Get user displayable name for gnome apps
                    app = match.group(1).replace('app-gnome-', '').split('\\')[0]
            else:
                app = 'System'
        if app in apps:
            apps[app] += pcpu
        else:
            apps[app] = pcpu
    return apps

battery = get_battery_info()
cpu = get_cpu_percent()

now = datetime.datetime.now()

# Log the battery usage
c.execute("INSERT INTO battery VALUES (?, ?, ?, ?, ?)", (now, not battery["discharging"], battery["percent"], battery["energy"], battery["energy_full"]))
conn.commit()

# c.execute("DELETE FROM power")
# c.execute("DELETE FROM battery")
# conn.commit()

# Log the power usage
for app in cpu:
    if cpu[app] < 0.01:
        continue
    c.execute("INSERT INTO power VALUES (?, ?, ?)", (now, app, cpu[app]))
conn.commit()

# Print the power usage from the last hour
start_time = now - datetime.timedelta(hours=1)
last_battery_stats = None
# TODO: Add option to specify start time (ignore charging times)
if battery["discharging"]:
    last_charging_reading = c.execute("SELECT time FROM battery WHERE charging = 1 ORDER BY time DESC LIMIT 1").fetchone()
    if last_charging_reading is not None:
        start_time = max(start_time, last_charging_reading[0])
    first_discharging_reading = c.execute("SELECT time, percent, energy FROM battery WHERE charging = 0 AND time >= ? ORDER BY time ASC LIMIT 1", (start_time,)).fetchone()
    if first_discharging_reading is not None:
        last_battery_stats = {
            "percent": first_discharging_reading[1],
            "energy": first_discharging_reading[2]
        }
        start_time = max(start_time, first_discharging_reading[0])
c.execute("SELECT app, power FROM power WHERE time >= ?", (start_time,))
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
c.execute("DELETE FROM power WHERE time < ?", (now - datetime.timedelta(hours=10),))
c.execute("DELETE FROM battery WHERE time < ?", (now - datetime.timedelta(hours=10),))
conn.commit()

# Calculate battery status
percent_delta = 0
energy_delta = 0
if  last_battery_stats is not None:
    percent_delta = battery["percent"] - last_battery_stats["percent"]
    energy_delta = battery["energy"] - last_battery_stats["energy"]

# Create a PrettyTable object
table = PrettyTable()
table.field_names = ["App", "CPU (%)", "Energy (Wh)", "Battery (%)", "Active"]

# Add rows to the table
for app in power:
    energy = -energy_delta * app[1] / 100
    battery = -percent_delta * app[1] / 100
    table.add_row([app[0], f"{app[1]:.2f}", f"{energy:.2f}", f"{battery:.2f}", "Yes" if app[0] in cpu else "No"])

# Print the table
print(f"Power usage since {start_time}")
print(table)

conn.close()
