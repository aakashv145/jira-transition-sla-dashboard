import csv
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml
from dotenv import load_dotenv

from database import (
    initialize_db,
    save_transition,
    save_ticket_summary,
    clear_ticket_transitions,
)


# ============================================================
# PATHS / CONFIGURATION
# ============================================================

ROOT = Path(__file__).resolve().parent

OUTPUT = ROOT / "output"
OUTPUT.mkdir(exist_ok=True)

load_dotenv(ROOT / ".env")


BASE_URL = os.getenv(
    "JIRA_BASE_URL",
    ""
).rstrip("/")

USER = os.getenv("JIRA_USER")
TOKEN = os.getenv("JIRA_TOKEN")


if not BASE_URL or not USER or not TOKEN:

    raise SystemExit(
        "Missing Jira credentials. "
        "Set JIRA_BASE_URL, JIRA_USER and JIRA_TOKEN in .env"
    )


CONFIG_FILE = ROOT / "config.yaml"


if not CONFIG_FILE.exists():

    raise SystemExit(
        f"Configuration file not found: {CONFIG_FILE}"
    )


with open(
        CONFIG_FILE,
        encoding="utf-8"
) as f:

    CONFIG = yaml.safe_load(f) or {}


# ============================================================
# SLA CONFIGURATION
# ============================================================

L3_FIELDS = {
    str(x).strip().lower()
    for x in CONFIG.get(
        "l3_field_names",
        [
            "L3 triager"
        ]
    )
}


DEV_FIELDS = {
    str(x).strip().lower()
    for x in CONFIG.get(
        "dev_field_names",
        [
            "assignee"
        ]
    )
}


FINAL_STATUSES = {
    str(x).strip().lower()
    for x in CONFIG.get(
        "final_statuses",
        [
            "Done",
            "Deployed",
            "Won't Do",
            "Closed",
            "Resolved"
        ]
    )
}


# ============================================================
# HTTP SESSION
# ============================================================

session = requests.Session()

session.auth = (
    USER,
    TOKEN
)

session.headers.update(
    {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
)


# ============================================================
# DATE HELPERS
# ============================================================

def parse_dt(value):

    if not value:
        return None

    if isinstance(
            value,
            datetime
    ):
        return value

    return datetime.fromisoformat(
        str(value).replace(
            "Z",
            "+00:00"
        )
    )


def clean_dt(value):

    if not value:
        return None

    return value.replace(
        second=0,
        microsecond=0
    )


def minutes_between(
        start,
        end
):

    if not start or not end:
        return 0

    start = clean_dt(start)
    end = clean_dt(end)

    if end < start:
        return 0

    return int(
        (
                end - start
        ).total_seconds()
        // 60
    )


def format_duration(minutes):

    minutes = max(
        0,
        int(minutes or 0)
    )

    hours = minutes // 60
    mins = minutes % 60

    if hours:

        return (
            f"{hours}hrs "
            f"{mins} minutes"
        )

    return (
        f"{mins} minutes"
    )


# ============================================================
# JIRA API
# ============================================================

def get_issue(
        issue_key
):

    url = (
        f"{BASE_URL}"
        f"/rest/api/3/issue/"
        f"{issue_key}"
    )

    response = session.get(
        url,
        params={
            "fields": "*all"
        },
        timeout=60
    )

    response.raise_for_status()

    return response.json()


def get_changelog(
        issue_key
):

    url = (
        f"{BASE_URL}"
        f"/rest/api/3/issue/"
        f"{issue_key}"
        "/changelog"
    )

    start_at = 0

    histories = []

    while True:

        response = session.get(
            url,
            params={
                "startAt": start_at,
                "maxResults": 100
            },
            timeout=60
        )

        response.raise_for_status()

        data = response.json()

        values = data.get(
            "values",
            []
        )

        if not values:
            break

        histories.extend(
            values
        )

        if data.get(
                "isLast",
                False
        ):
            break

        start_at += len(
            values
        )

        total = data.get(
            "total"
        )

        if (
                total is not None
                and start_at >= total
        ):
            break

    return sorted(
        histories,
        key=lambda x:
        parse_dt(
            x["created"]
        )
    )


def get_keys_from_jql(
        jql
):

    if not jql:
        return []

    url = (
        f"{BASE_URL}"
        "/rest/api/3/search/jql"
    )

    keys = []

    next_token = None

    while True:

        payload = {
            "jql": jql,
            "maxResults": 100,
            "fields": [
                "key"
            ]
        }

        if next_token:

            payload[
                "nextPageToken"
            ] = next_token

        response = session.post(
            url,
            json=payload,
            timeout=60
        )

        if response.status_code != 200:

            print(
                "JQL Search Failed"
            )

            print(
                response.text
            )

        response.raise_for_status()

        data = response.json()

        for issue in data.get(
                "issues",
                []
        ):

            keys.append(
                issue["key"]
            )

        next_token = data.get(
            "nextPageToken"
        )

        if not next_token:
            break

    return keys


# ============================================================
# JIRA CHANGELOG HELPERS
# ============================================================

def get_author_name(
        history
):

    author = (
            history.get(
                "author"
            )
            or {}
    )

    return (
            author.get(
                "displayName"
            )
            or author.get(
        "emailAddress"
    )
            or author.get(
        "accountId"
    )
            or ""
    )


def get_issue_context(
        issue
):

    fields = issue.get(
        "fields",
        {}
    )

    project = (
            fields.get(
                "project"
            )
            or {}
    )

    issue_type = (
            fields.get(
                "issuetype"
            )
            or {}
    )

    priority = (
            fields.get(
                "priority"
            )
            or {}
    )

    resolution = fields.get(
        "resolution"
    )

    status = (
            fields.get(
                "status"
            )
            or {}
    )

    return {

        "project":
            project.get(
                "key",
                ""
            ),

        "issue_type":
            issue_type.get(
                "name",
                ""
            ),

        "priority":
            priority.get(
                "name",
                ""
            ),

        "labels":
            ", ".join(
                fields.get(
                    "labels"
                )
                or []
            ),

        "status":
            status.get(
                "name",
                ""
            ),

        "resolution":
            resolution.get(
                "name",
                ""
            )
            if resolution
            else ""
    }


# ============================================================
# BUILD TRANSITION ROW
# ============================================================

def classify_waiting_type(role, owner):
    """Classify an explicit no-owner interval."""
    if owner != "UNASSIGNED":
        return ""

    if role == "L3":
        return "L3_WAITING"

    if role == "DEV":
        return "DEV_WAITING"

    return "UNASSIGNED"


def build_transition_row(
        issue,
        role,
        field,
        item,
        assigned_to,
        assigned_by,
        assignment_number,
        start,
        end,
        status,
    waiting_type=""
):

    context = get_issue_context(
        issue
    )

    duration_minutes = minutes_between(
        start,
        end
    )

    return {

        "ticket":
            issue["key"],

        "project":
            context["project"],

        "role":
            role,

        "jira_field":
            field,

        "assignment_no":
            assignment_number,

        "previous_value":
            item.get(
                "fromString",
                ""
            )
            or "",

        "assigned_to":
            assigned_to
            or "",

        "assigned_by":
            assigned_by
            or "",

        "new_value":
            item.get(
                "toString",
                ""
            )
            or "",

        "assigned_at":
            (
                start.isoformat()
                if start
                else ""
            ),

        "released_at":
            (
                end.isoformat()
                if end
                else ""
            ),

        "duration_minutes":
            duration_minutes,

        "duration":
            format_duration(
                duration_minutes
            ),

        "state":
            (
                waiting_type
                if waiting_type
                else (
                    "L3_ASSIGNED"
                    if role == "L3"
                    and assigned_to != "UNASSIGNED"
                    else (
                        "DEV_ASSIGNED"
                        if role == "DEV"
                        and assigned_to != "UNASSIGNED"
                        else "UNASSIGNED"
                    )
                )
            ),

        "waiting_type":
            waiting_type or "",

        "status":
            (
                "Unassigned"
                if assigned_to == "UNASSIGNED"
                else (status or "Assigned")
            )
    }


# ============================================================
# SLA OWNERSHIP ENGINE
# ============================================================

def classify_waiting_type(role, owner):
    """Classify an explicit no-owner interval."""
    if owner != "UNASSIGNED":
        return ""

    if role == "L3":
        return "L3_WAITING"

    if role == "DEV":
        return "DEV_WAITING"

    return "UNASSIGNED"


def analyze_issue(
    issue,
    histories
):

    fields = issue.get(
        "fields",
        {}
    )

    created = parse_dt(
        fields.get("created")
    )

    resolution_date = parse_dt(
        fields.get("resolutiondate")
    )

    ownership_end = (
        resolution_date
        or datetime.now(timezone.utc)
    )

    current_l3 = None
    current_assignee = None
    current_status = (
        (
            fields.get("status")
            or {}
        ).get(
            "name",
            ""
        )
    )

    # Explicit state-machine variables.
    active_role = None
    active_owner = None
    active_state = None
    active_start = None
    active_assigned_by = None
    active_field = None
    active_item = None
    active_waiting_type = ""

    assignment_counter = defaultdict(int)
    rows = []

    def start_state(
        role,
        owner,
        field,
        item,
        history,
        start_time
    ):
        nonlocal active_role
        nonlocal active_owner
        nonlocal active_state
        nonlocal active_start
        nonlocal active_assigned_by
        nonlocal active_field
        nonlocal active_item
        nonlocal active_waiting_type

        if not owner:
            owner = "UNASSIGNED"

        active_role = role
        active_owner = owner
        active_start = start_time
        active_assigned_by = get_author_name(history)
        active_field = field
        active_item = item

        if owner == "UNASSIGNED":
            active_waiting_type = classify_waiting_type(
                role,
                owner
            )
            active_state = (
                active_waiting_type
                or "UNASSIGNED"
            )
        elif role == "L3":
            active_waiting_type = ""
            active_state = "L3_ASSIGNED"
        elif role == "DEV":
            active_waiting_type = ""
            active_state = "DEV_ASSIGNED"
        else:
            active_waiting_type = ""
            active_state = "ASSIGNED"

        assignment_counter[role] += 1

    def close_state(
        end_time,
        status
    ):
        nonlocal active_role
        nonlocal active_owner
        nonlocal active_state
        nonlocal active_start
        nonlocal active_assigned_by
        nonlocal active_field
        nonlocal active_item
        nonlocal active_waiting_type

        if active_owner is None:
            return

        row = build_transition_row(
            issue,
            active_role,
            active_field,
            active_item or {},
            active_owner,
            active_assigned_by,
            assignment_counter[active_role],
            active_start,
            end_time,
            status,
            active_waiting_type,
            active_state
        )

        # Hard invariant: waiting states never carry the previous person.
        if active_state in (
            "L3_WAITING",
            "DEV_WAITING",
            "UNASSIGNED"
        ):
            row["assigned_to"] = "UNASSIGNED"
            row["status"] = "Unassigned"
            row["waiting_type"] = active_state
            row["state"] = active_state

        rows.append(row)

        active_role = None
        active_owner = None
        active_state = None
        active_start = None
        active_assigned_by = None
        active_field = None
        active_item = None
        active_waiting_type = ""

    def enter_waiting(
        waiting_role,
        field,
        item,
        history,
        change_time
    ):
        # Always close the real previous owner first.
        if active_owner is not None:
            close_state(
                change_time,
                current_status
            )

        start_state(
            waiting_role,
            "UNASSIGNED",
            field,
            item,
            history,
            change_time
        )

    def enter_owner(
        role,
        owner,
        field,
        item,
        history,
        change_time
    ):
        if not owner:
            return

        if (
            active_owner == owner
            and active_role == role
        ):
            return

        if active_owner is not None:
            close_state(
                change_time,
                current_status
            )

        start_state(
            role,
            owner,
            field,
            item,
            history,
            change_time
        )

    # Replay Jira changelog chronologically.
    for history in histories:

        change_time = parse_dt(
            history["created"]
        )

        for item in history.get(
            "items",
            []
        ):

            field_original = (
                item.get("field")
                or ""
            )

            field = (
                field_original
                .lower()
                .strip()
            )

            # ------------------------------------------------
            # STATUS
            # ------------------------------------------------

            if field == "status":

                current_status = (
                    item.get("toString")
                    or current_status
                )

                if (
                    current_status.lower()
                    in FINAL_STATUSES
                ):
                    if active_owner is not None:
                        close_state(
                            change_time,
                            current_status
                        )

                continue

            # ------------------------------------------------
            # L3
            # ------------------------------------------------

            if field in L3_FIELDS:

                new_l3 = (
                    item.get("toString")
                    or ""
                ).strip()

                # Explicit L3 -> UNASSIGNED.
                if not new_l3:

                    previous_owner = active_owner

                    enter_waiting(
                        "L3",
                        field_original,
                        item,
                        history,
                        change_time
                    )

                    current_l3 = None

                    print(
                        f"    L3 UNASSIGNED: "
                        f"{previous_owner or 'UNKNOWN'} -> "
                        f"UNASSIGNED at "
                        f"{change_time.isoformat()}"
                    )

                    continue

                # UNASSIGNED -> L3 or L3 reassignment.
                current_l3 = new_l3

                enter_owner(
                    "L3",
                    new_l3,
                    field_original,
                    item,
                    history,
                    change_time
                )

                continue

            # ------------------------------------------------
            # DEV / ASSIGNEE
            # ------------------------------------------------

            if field in DEV_FIELDS:

                new_assignee = (
                    item.get("toString")
                    or ""
                ).strip()

                # DEV assigned.
                if new_assignee:

                    current_assignee = new_assignee

                    enter_owner(
                        "DEV",
                        new_assignee,
                        field_original,
                        item,
                        history,
                        change_time
                    )

                    continue

                # DEV -> UNASSIGNED.
                previous_owner = active_owner

                enter_waiting(
                    "DEV",
                    field_original,
                    item,
                    history,
                    change_time
                )

                current_assignee = None

                print(
                    f"    DEV UNASSIGNED: "
                    f"{previous_owner or 'UNKNOWN'} -> "
                    f"UNASSIGNED at "
                    f"{change_time.isoformat()}"
                )

                continue

    # Close current state at resolution or now.
    if active_owner is not None:
        close_state(
            ownership_end,
            current_status
        )

    return (
        rows,
        created,
        resolution_date
    )


# ============================================================
# TRANSITION ROW BUILDER
# ============================================================

def build_transition_row(
    issue,
    role,
    field,
    item,
    assigned_to,
    assigned_by,
    assignment_number,
    start,
    end,
    status,
    waiting_type="",
    state=""
):

    context = get_issue_context(
        issue
    )

    duration_minutes = minutes_between(
        start,
        end
    )

    if assigned_to == "UNASSIGNED":

        state = (
            waiting_type
            or state
            or "UNASSIGNED"
        )

        transition_status = "Unassigned"

    else:

        if not state:

            if role == "L3":
                state = "L3_ASSIGNED"

            elif role == "DEV":
                state = "DEV_ASSIGNED"

            else:
                state = "ASSIGNED"

        transition_status = (
            status
            or "Assigned"
        )

    return {

        "ticket":
            issue["key"],

        "project":
            context["project"],

        "role":
            role,

        "jira_field":
            field,

        "assignment_no":
            assignment_number,

        "previous_value":
            item.get(
                "fromString",
                ""
            ) or "",

        "assigned_to":
            assigned_to
            or "UNASSIGNED",

        "assigned_by":
            assigned_by
            or "",

        "new_value":
            item.get(
                "toString",
                ""
            ) or "",

        "assigned_at":
            (
                start.isoformat()
                if start
                else ""
            ),

        "released_at":
            (
                end.isoformat()
                if end
                else ""
            ),

        "duration_minutes":
            duration_minutes,

        "duration":
            format_duration(
                duration_minutes
            ),

        "state":
            state,

        "waiting_type":
            waiting_type,

        "status":
            transition_status
    }


# ============================================================
# CSV WRITER
# ============================================================

def write_csv(
        filename,
        headers,
        rows
):

    with open(
            filename,
            "w",
            newline="",
            encoding="utf-8-sig"
    ) as file:

        writer = csv.writer(
            file
        )

        writer.writerow(
            headers
        )

        writer.writerows(
            rows
        )


# ============================================================
# PERSON SUMMARY
# ============================================================

def build_person_summary(
        rows
):

    summary = defaultdict(
        int
    )

    for row in rows:

        key = (
            row["ticket"],
            row["role"],
            row["assigned_to"]
        )

        summary[key] += (
            row["duration_minutes"]
        )


    output = []

    for (
            ticket,
            role,
            person
    ), minutes in summary.items():

        output.append(
            [
                ticket,
                role,
                person,
                format_duration(
                    minutes
                )
            ]
        )


    return output


# ============================================================
# TICKET SUMMARY
# ============================================================

def build_ticket_summary(
        issue,
        rows,
        created,
        resolution
):

    # Real L3 engineer time only.
    # UNASSIGNED waiting time is tracked separately.
    l3_rows = [
        row
        for row in rows
        if (
            row["role"] == "L3"
            and row["assigned_to"] != "UNASSIGNED"
        )
    ]

    l3_waiting_rows = [
        row
        for row in rows
        if row.get("state") == "L3_WAITING"
    ]

    dev_waiting_rows = [
        row
        for row in rows
        if row.get("state") == "DEV_WAITING"
    ]

    unassigned_rows = [
        row
        for row in rows
        if (
            row["assigned_to"] == "UNASSIGNED"
            and row.get("state") == "UNASSIGNED"
        )
    ]

    dev_rows = [
        row
        for row in rows
        if row["role"] == "DEV"
    ]


    total_l3 = sum(
        row["duration_minutes"]
        for row in l3_rows
    )

    total_l3_waiting = sum(
        row["duration_minutes"]
        for row in l3_waiting_rows
    )

    total_dev_waiting = sum(
        row["duration_minutes"]
        for row in dev_waiting_rows
    )

    total_unassigned = sum(
        row["duration_minutes"]
        for row in unassigned_rows
    )

    total_dev = sum(
        row["duration_minutes"]
        for row in dev_rows
    )

    total_sla = (
        total_l3
        + total_unassigned
        + total_dev
    )


    # --------------------------------------------------------
    # FIRST L3 PICKUP
    # --------------------------------------------------------

    l3_pickup_sla = "Not Picked Up"


    if (
            l3_rows
            and created
    ):

        first_l3 = min(
            l3_rows,
            key=lambda row:
            parse_dt(
                row["assigned_at"]
            )
        )


        first_l3_time = parse_dt(
            first_l3[
                "assigned_at"
            ]
        )


        pickup_minutes = minutes_between(
            created,
            first_l3_time
        )


        l3_pickup_sla = format_duration(
            pickup_minutes
        )


    fields = issue.get(
        "fields",
        {}
    )


    status = (
        (
                fields.get(
                    "status"
                )
                or {}
        ).get(
            "name",
            ""
        )
    )


    resolution_value = (
        resolution.isoformat()
        if resolution
        else ""
    )


    return {

        "ticket":
            issue["key"],

        "created":
            (
                created.isoformat()
                if created
                else ""
            ),

        "l3_pickup_sla":
            l3_pickup_sla,

        "total_l3_time":
            format_duration(
                total_l3
            ),

        "total_dev_time":
            format_duration(
                total_dev
            ),

        "total_l3_waiting_time":
            format_duration(
                total_l3_waiting
            ),

        "total_dev_waiting_time":
            format_duration(
                total_dev_waiting
            ),

        "total_unassigned_time":
            format_duration(
                total_unassigned
            ),

        "total_sla_time":
            format_duration(
                total_sla
            ),

        "total_unassigned_minutes":
            total_unassigned,

        "total_sla_minutes":
            total_sla,

        "status":
            status,

        "resolution":
            resolution_value,

        "total_l3_minutes":
            total_l3,

        "total_dev_minutes":
            total_dev
    }


# ============================================================
# MAIN
# ============================================================

def main():

    print()
    print(
        "=============================================="
    )
    print(
        "      JIRA TRANSITION SLA REFRESH"
    )
    print(
        "=============================================="
    )
    print()


    # --------------------------------------------------------
    # Initialize database
    # --------------------------------------------------------

    initialize_db()


    # --------------------------------------------------------
    # Get ticket keys
    # --------------------------------------------------------

    ticket_keys = CONFIG.get(
        "ticket_keys",
        []
    )


    jql = CONFIG.get(
        "jql",
        ""
    )


    jql_keys = get_keys_from_jql(
        jql
    )


    keys = list(
        dict.fromkeys(
            ticket_keys
            +
            jql_keys
        )
    )


    if not keys:

        print(
            "No Jira tickets found."
        )

        return


    print(
        f"Tickets found: {len(keys)}"
    )

    print()


    # --------------------------------------------------------
    # Global output collections
    # --------------------------------------------------------

    all_rows = []

    ticket_summary_rows = []


    # ========================================================
    # PROCESS TICKETS
    # ========================================================

    for ticket in keys:

        print(
            f"Processing {ticket}"
        )


        try:

            # ------------------------------------------------
            # Jira issue
            # ------------------------------------------------

            issue = get_issue(
                ticket
            )


            # ------------------------------------------------
            # Changelog
            # ------------------------------------------------

            histories = get_changelog(
                ticket
            )


            print(
                f"  Changelog entries : "
                f"{len(histories)}"
            )


            # ------------------------------------------------
            # SLA analysis
            # ------------------------------------------------

            rows, created, resolution = (
                analyze_issue(
                    issue,
                    histories
                )
            )


            print(
                f"  SLA rows generated: "
                f"{len(rows)}"
            )


            # ------------------------------------------------
            # Clear previous data
            #
            # Scheduler runs every 5 minutes.
            # This prevents duplicate transitions.
            # ------------------------------------------------

            # Rebuild this ticket completely from the Jira
            # changelog. This removes old incorrect owner rows
            # and prevents scheduler duplicates.
            clear_ticket_transitions(
                ticket
            )


            # ------------------------------------------------
            # Save transition rows
            # ------------------------------------------------

            for row in rows:

                # Final safety invariant: no waiting interval may
                # contain the previous person's name.
                if row.get("state") in (
                    "L3_WAITING",
                    "DEV_WAITING",
                    "UNASSIGNED"
                ):
                    row["assigned_to"] = "UNASSIGNED"
                    row["status"] = "Unassigned"
                    row["waiting_type"] = (
                        row.get("state")
                        or "UNASSIGNED"
                    )

                print(
                    f"  SLA -> "
                    f"{row['role']} | "
                    f"{row['assigned_to']} | "
                    f"{row['duration']} | "
                    f"{row['status']}"
                )


                save_transition(
                    row
                )


            # ------------------------------------------------
            # Ticket summary
            # ------------------------------------------------

            summary = build_ticket_summary(
                issue,
                rows,
                created,
                resolution
            )


            save_ticket_summary(

                ticket=summary[
                    "ticket"
                ],

                created=summary[
                    "created"
                ],

                l3_pickup_sla=summary[
                    "l3_pickup_sla"
                ],

                total_l3_time=summary[
                    "total_l3_time"
                ],

                total_dev_time=summary[
                    "total_dev_time"
                ],

                status=summary[
                    "status"
                ],

                resolution=summary[
                    "resolution"
                ]
            )


            # ------------------------------------------------
            # Add to CSV collection
            # ------------------------------------------------

            all_rows.extend(
                rows
            )


            ticket_summary_rows.append(
                summary
            )


            # ------------------------------------------------
            # Console summary
            # ------------------------------------------------

            print(
                f"  L3 Pickup SLA    : "
                f"{summary['l3_pickup_sla']}"
            )


            print(
                f"  Total L3 Time    : "
                f"{summary['total_l3_time']}"
            )


            print(
                f"  Total DEV Time   : "
                f"{summary['total_dev_time']}"
            )


            print(
                f"  L3 Waiting Time  : "
                f"{summary['total_l3_waiting_time']}"
            )

            print(
                f"  DEV Waiting Time : "
                f"{summary['total_dev_waiting_time']}"
            )

            print(
                f"  Unassigned Time  : "
                f"{summary['total_unassigned_time']}"
            )


            print(
                f"  Total SLA Time   : "
                f"{summary['total_sla_time']}"
            )


            print(
                f"  Status           : "
                f"{summary['status']}"
            )


            print(
                f"  Saved transitions: "
                f"{len(rows)}"
            )


            print()


        except Exception as exc:

            print(
                f"  ERROR processing "
                f"{ticket}: {exc}"
            )

            print()

            # Continue with next ticket.
            continue


    # ========================================================
    # CSV 1 - TRANSITION REPORT
    # ========================================================

    write_csv(

        OUTPUT /
        "jira_transition_sla_report.csv",

        [
            "Ticket",
            "Project",
            "Role",
            "Assigned To",
            "Assigned By",
            "Assigned At",
            "Released At",
            "Duration Minutes",
            "Duration",
            "State",
            "Waiting Type",
            "Status"
        ],

        [
            [
                row["ticket"],
                row["project"],
                row["role"],
                row["assigned_to"],
                row["assigned_by"],
                row["assigned_at"],
                row["released_at"],
                row["duration_minutes"],
                row["duration"],
                row.get("state", ""),
                row.get("waiting_type", ""),
                row["status"]
            ]

            for row in all_rows
        ]
    )


    # ========================================================
    # CSV 2 - PERSON SUMMARY
    # ========================================================

    write_csv(

        OUTPUT /
        "jira_person_sla_summary.csv",

        [
            "Ticket",
            "Role",
            "Person",
            "Total Time"
        ],

        build_person_summary(
            all_rows
        )
    )


    # ========================================================
    # CSV 3 - TICKET SUMMARY
    # ========================================================

    write_csv(

        OUTPUT /
        "jira_ticket_sla_summary.csv",

        [
            "Ticket",
            "Created",
            "First L3 Pickup",
            "L3 Pickup SLA",
            "Total L3 Time",
            "L3 Waiting Time",
            "DEV Waiting Time",
            "Total Unassigned Time",
            "Total Dev Time",
            "Total SLA Time",
            "Resolution Time",
            "L3 Assignments",
            "Dev Assignments",
            "Status"
        ],

        [
            [
                summary["ticket"],
                summary["created"],
                "",
                summary["l3_pickup_sla"],
                summary["total_l3_time"],
                summary["total_l3_waiting_time"],
                summary["total_dev_waiting_time"],
                summary["total_unassigned_time"],
                summary["total_dev_time"],
                summary["total_sla_time"],
                (
                        summary["resolution"]
                        or "Open"
                ),
                len([
                    row
                    for row in all_rows
                    if (
                            row["ticket"]
                            == summary["ticket"]
                            and
                            row["role"]
                            == "L3"
                    )
                ]),
                len([
                    row
                    for row in all_rows
                    if (
                            row["ticket"]
                            == summary["ticket"]
                            and
                            row["role"]
                            == "DEV"
                    )
                ]),
                summary["status"]
            ]

            for summary
            in ticket_summary_rows
        ]
    )


    # ========================================================
    # FINAL OUTPUT
    # ========================================================

    print()
    print(
        "=============================================="
    )
    print(
        "       JIRA SLA REFRESH COMPLETED"
    )
    print(
        "=============================================="
    )
    print()


    print(
        f"Tickets processed : "
        f"{len(keys)}"
    )


    print(
        f"Transitions saved : "
        f"{len(all_rows)}"
    )


    print(
        f"Ticket summaries  : "
        f"{len(ticket_summary_rows)}"
    )


    print()


    print(
        "Database:"
    )


    print(
        " - sla_dashboard.db"
    )


    print()


    print(
        "Reports:"
    )


    print(
        " - output/jira_transition_sla_report.csv"
    )


    print(
        " - output/jira_person_sla_summary.csv"
    )


    print(
        " - output/jira_ticket_sla_summary.csv"
    )


    print()


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    main()