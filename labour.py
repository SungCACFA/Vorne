import requests

f = "start_time,team_size,labor,earned_labor"
devices = [
    ("10.15.22.19", "FR8"),
    ("10.15.22.83", "AreaB"),
    ("10.15.22.95", "AreaD"),
    ("10.15.22.119", "AreaE"),
]

for ip, name in devices:
    r = requests.get(
        f"http://{ip}/api/v0/channels/shift/events",
        params={"fields": f},
        timeout=30,
    )
    rows = r.json()["data"]["events"]
    crew = sum(1 for x in rows if (x[1] or 0) > 0)
    lab = sum(1 for x in rows if (x[2] or 0) > 0)
    earned = sum(1 for x in rows if (x[3] or 0) > 0)
    print(f"{name:6} {len(rows):4} shifts | crew size: {crew:4} | labour: {lab:4} | earned labour: {earned:4}")
