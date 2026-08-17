import csv
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "output"
OUTPUT.mkdir(exist_ok=True)
load_dotenv(ROOT / ".env")

BASE_URL = os.getenv("JIRA_BASE_URL", "").rstrip("/")
USER = os.getenv("JIRA_USER")
TOKEN = os.getenv("JIRA_TOKEN")

if not BASE_URL or not USER or not TOKEN:
    raise SystemExit("Set JIRA_BASE_URL, JIRA_USER and JIRA_TOKEN in .env")

with open(ROOT / "config.yaml", encoding="utf-8") as f:
    CONFIG = yaml.safe_load(f) or {}

L3_FIELDS = {x.lower() for x in CONFIG.get("l3_field_names", ["L3 triager"])}
DEV_FIELDS = {x.lower() for x in CONFIG.get("dev_field_names", ["assignee"])}

session = requests.Session()
session.auth = (USER, TOKEN)
session.headers.update({"Accept": "application/json", "Content-Type": "application/json"})


def parse_dt(value):
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def clean_dt(value):
    return value.replace(second=0, microsecond=0)


def minutes_between(start, end):
    if not start or not end:
        return 0
    return max(0, int((clean_dt(end) - clean_dt(start)).total_seconds() // 60))


def duration(minutes):
    minutes = max(0, int(minutes))
    return f"{minutes // 60}hrs {minutes % 60} minutes" if minutes >= 60 else f"{minutes} minutes"


def get_issue(key):
    url = f"{BASE_URL}/rest/api/3/issue/{key}"
    r = session.get(url, params={"fields": "*all"}, timeout=60)
    r.raise_for_status()
    return r.json()


def get_changelog(key):
    """Fetch every changelog page; never rely on expand=changelog."""
    url = f"{BASE_URL}/rest/api/3/issue/{key}/changelog"
    start = 0
    histories = []
    while True:
        r = session.get(url, params={"startAt": start, "maxResults": 100}, timeout=60)
        r.raise_for_status()
        page = r.json()
        values = page.get("values", page.get("histories", []))
        histories.extend(values)
        if page.get("isLast", True) or not values:
            break
        start += len(values)
        if page.get("total") is not None and start >= page["total"]:
            break
    return sorted(histories, key=lambda x: x["created"])


def jql_keys(jql):
    if not jql:
        return []
    url = f"{BASE_URL}/rest/api/3/search"
    start = 0
    keys = []
    while True:
        r = session.get(url, params={"jql": jql, "startAt": start, "maxResults": 100, "fields": "key"}, timeout=60)
        r.raise_for_status()
        data = r.json()
        issues = data.get("issues", [])
        keys.extend(x["key"] for x in issues)
        if start + len(issues) >= data.get("total", len(keys)) or not issues:
            break
        start += len(issues)
    return keys


def author_name(history):
    return (history.get("author") or {}).get("displayName", "") or ""


def make_transition(issue, role, field, item, history, assigned_to, assigned_by, number, start, end, status):
    mins = minutes_between(start, end)
    return {
        "ticket": issue["key"],
        "project": (issue.get("fields", {}).get("project") or {}).get("key", ""),
        "issue_type": (issue.get("fields", {}).get("issuetype") or {}).get("name", ""),
        "priority": (issue.get("fields", {}).get("priority") or {}).get("name", ""),
        "labels": ", ".join(issue.get("fields", {}).get("labels") or []),
        "role": role,
        "jira_field": field,
        "transition_type": f"{role} Assignment",
        "assignment_no": number,
        "previous_value": item.get("fromString", "") or "",
        "assigned_to": assigned_to or "",
        "assigned_by": assigned_by or "",
        "new_value": item.get("toString", "") or "",
        "assigned_at": start.isoformat() if start else "",
        "released_at": end.isoformat() if end else "",
        "duration_minutes": mins,
        "duration": duration(mins),
        "status": status or "",
    }


def analyze(issue, histories):
    created = parse_dt(issue["fields"]["created"])
    resolution = parse_dt(issue["fields"].get("resolutiondate"))
    end_of_ownership = resolution or datetime.now(timezone.utc)

    active = {"L3": None, "DEV": None}
    counters = defaultdict(int)
    rows = []
    status = issue["fields"].get("status", {}).get("name", "")

    for h in histories:
        ts = parse_dt(h["created"])
        for item in h.get("items", []):
            field = (item.get("field") or "").lower()

            if field == "status":
                status = item.get("toString") or status
                continue

            role = "L3" if field in L3_FIELDS else "DEV" if field in DEV_FIELDS else None
            if not role:
                continue

            old = item.get("fromString") or ""
            new = item.get("toString") or ""
            current = active[role]

            if current:
                rows.append(make_transition(
                    issue, role, current["field"], current["item"], current["history"],
                    current["user"], current["assigned_by"], current["number"],
                    current["start"], ts, status
                ))

            if new:
                counters[role] += 1
                active[role] = {
                    "user": new,
                    "assigned_by": author_name(h),
                    "start": ts,
                    "field": item.get("field", ""),
                    "item": item,
                    "history": h,
                    "number": counters[role],
                }
            else:
                active[role] = None

    for role, current in active.items():
        if current:
            rows.append(make_transition(
                issue, role, current["field"], current["item"], current["history"],
                current["user"], current["assigned_by"], current["number"],
                current["start"], end_of_ownership, status
            ))

    return rows, created, resolution


def summarize_ticket(issue, rows, created, resolution):
    l3 = [r for r in rows if r["role"] == "L3"]
    dev = [r for r in rows if r["role"] == "DEV"]
    first = min(l3, key=lambda x: x["assigned_at"]) if l3 else None
    pickup = minutes_between(created, parse_dt(first["assigned_at"])) if first else None
    total_resolution = minutes_between(created, resolution) if resolution else None

    return [
        issue["key"], issue["fields"]["created"],
        first["assigned_at"] if first else "Not Picked Up",
        duration(pickup) if pickup is not None else "Not Picked Up",
        duration(sum(x["duration_minutes"] for x in l3)),
        duration(sum(x["duration_minutes"] for x in dev)),
        duration(total_resolution) if total_resolution is not None else "Open",
        len(l3), len(dev),
        issue["fields"].get("status", {}).get("name", ""),
        issue["fields"].get("resolution", {}).get("name", "") if issue["fields"].get("resolution") else "",
    ]


def write_csv(path, headers, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        writer.writerows(rows)


def main():
    keys = list(dict.fromkeys(CONFIG.get("ticket_keys", []) + jql_keys(CONFIG.get("jql", ""))))
    all_rows = []
    ticket_rows = []

    for key in keys:
        print(f"Processing {key}...")
        issue = get_issue(key)
        histories = get_changelog(key)
        rows, created, resolution = analyze(issue, histories)
        all_rows.extend(rows)
        ticket_rows.append(summarize_ticket(issue, rows, created, resolution))

    write_csv(
        OUTPUT / "jira_transition_sla_report.csv",
        ["Ticket", "Project", "Issue Type", "Priority", "Labels", "Role", "Jira Field",
         "Transition Type", "Assignment #", "Previous Value", "Assigned To", "Assigned By",
         "New Value", "Assigned At", "Released At", "Duration Minutes", "Duration", "Status"],
        [[r[k] for k in ["ticket","project","issue_type","priority","labels","role","jira_field",
          "transition_type","assignment_no","previous_value","assigned_to","assigned_by",
          "new_value","assigned_at","released_at","duration_minutes","duration","status"]] for r in all_rows]
    )

    aggregate = defaultdict(lambda: {"tickets": set(), "assignments": 0, "minutes": 0, "first": None, "last": None})
    for r in all_rows:
        key = (r["ticket"], r["role"], r["assigned_to"])
        a = aggregate[key]
        a["tickets"].add(r["ticket"])
        a["assignments"] += 1
        a["minutes"] += r["duration_minutes"]
        a["first"] = min(a["first"], r["assigned_at"]) if a["first"] else r["assigned_at"]
        a["last"] = max(a["last"], r["released_at"]) if a["last"] else r["released_at"]

    person_rows = []
    for (ticket, role, person), a in sorted(aggregate.items()):
        person_rows.append([ticket, role, person, a["assignments"], duration(a["minutes"]), a["first"], a["last"]])

    write_csv(
        OUTPUT / "jira_person_sla_summary.csv",
        ["Ticket", "Role", "Person", "Assignments", "Total Time", "First Assignment", "Last Release"],
        person_rows
    )

    write_csv(
        OUTPUT / "jira_ticket_sla_summary.csv",
        ["Ticket", "Created", "First L3 Pickup", "L3 Pickup SLA", "Total L3 Time",
         "Total Dev Time", "Total Resolution Time", "L3 Assignments", "Dev Assignments",
         "Status", "Resolution"],
        ticket_rows
    )

    print(f"Reports written to {OUTPUT}")


if __name__ == "__main__":
    main()
