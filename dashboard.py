import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st


# ============================================================
# PATHS / PAGE CONFIG
# ============================================================

ROOT = Path(__file__).resolve().parent
DB = ROOT / "sla_dashboard.db"

st.set_page_config(
    page_title="Jira Transition SLA Dashboard",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

REFRESH_INTERVAL = 300


# ============================================================
# LIGHTWEIGHT UI THEME
# ============================================================

st.markdown(
    """
    <style>
        .block-container {
            padding-top: 1.1rem;
            padding-bottom: 2rem;
        }

        .app-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 0.35rem 0 0.75rem 0;
            border-bottom: 1px solid #dfe1e6;
            margin-bottom: 1rem;
        }

        .app-title {
            font-size: 1.65rem;
            font-weight: 700;
            color: #172b4d;
        }

        .app-subtitle {
            color: #5e6c84;
            font-size: 0.86rem;
        }

        .section-card {
            border: 1px solid #dfe1e6;
            border-radius: 8px;
            padding: 1rem;
            background: #ffffff;
            box-shadow: 0 1px 2px rgba(9,30,66,.08);
        }

        .metric-card {
            border-top: 3px solid #0c66e4;
            border-radius: 7px;
            background: #ffffff;
            padding: 0.85rem 1rem;
            min-height: 105px;
            box-shadow: 0 1px 2px rgba(9,30,66,.08);
        }

        .metric-label {
            color: #5e6c84;
            font-size: 0.82rem;
        }

        .metric-value {
            color: #172b4d;
            font-size: 1.55rem;
            font-weight: 700;
            margin-top: 0.2rem;
        }

        .metric-help {
            color: #6b778c;
            font-size: 0.75rem;
        }

        .status-pill {
            display: inline-block;
            padding: 3px 9px;
            border-radius: 12px;
            font-size: 0.76rem;
            font-weight: 600;
            background: #deebff;
            color: #0747a6;
        }

        .waiting-pill {
            background: #fff0b3;
            color: #7a5c00;
        }

        .assigned-pill {
            background: #e3fcef;
            color: #006644;
        }

        .danger-pill {
            background: #ffebe6;
            color: #bf2600;
        }

        div[data-testid="stMetric"] {
            background: white;
            border-top: 3px solid #0c66e4;
            border-radius: 7px;
            padding: 0.6rem 0.8rem;
            box-shadow: 0 1px 2px rgba(9,30,66,.08);
        }

        .ticket-link {
            color: #0c66e4;
            font-weight: 650;
        }

        .small-muted {
            color: #6b778c;
            font-size: 0.78rem;
        }
    </style>
    """,
    unsafe_allow_html=True,
)


# ============================================================
# DATABASE
# ============================================================

def get_connection():
    return sqlite3.connect(DB)


def table_exists(table_name):
    if not DB.exists():
        return False

    conn = get_connection()

    try:
        result = conn.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type='table'
              AND name=?
            """,
            (table_name,),
        ).fetchone()
    finally:
        conn.close()

    return result is not None


def table_columns(table_name):
    if not table_exists(table_name):
        return []

    conn = get_connection()

    try:
        rows = conn.execute(
            f"PRAGMA table_info({table_name})"
        ).fetchall()
    finally:
        conn.close()

    return [row[1] for row in rows]


def load_table(table_name):
    if not table_exists(table_name):
        return pd.DataFrame()

    conn = get_connection()

    try:
        return pd.read_sql(
            f"SELECT * FROM {table_name}",
            conn,
        )
    finally:
        conn.close()


def load_transitions():
    df = load_table("transitions")

    if df.empty:
        return df

    # Backward compatibility with the original schema.
    if "person" in df.columns and "assigned_to" not in df.columns:
        df = df.rename(
            columns={
                "person": "assigned_to"
            }
        )

    if "assigned_to" not in df.columns:
        df["assigned_to"] = "UNASSIGNED"

    df["assigned_to"] = (
        df["assigned_to"]
        .fillna("UNASSIGNED")
        .astype(str)
        .replace(
            {
                "": "UNASSIGNED",
                "None": "UNASSIGNED",
                "nan": "UNASSIGNED",
            }
        )
    )

    if "role" not in df.columns:
        df["role"] = ""

    if "duration_minutes" not in df.columns:
        df["duration_minutes"] = 0

    df["duration_minutes"] = pd.to_numeric(
        df["duration_minutes"],
        errors="coerce",
    ).fillna(0)

    if "duration_seconds" in df.columns:
        df["duration_seconds"] = pd.to_numeric(
            df["duration_seconds"],
            errors="coerce",
        ).fillna(
            df["duration_minutes"] * 60
        )
    else:
        df["duration_seconds"] = (
            df["duration_minutes"] * 60
        )

    for column in [
        "ticket",
        "project",
        "assigned_by",
        "assigned_at",
        "released_at",
        "duration",
        "status",
    ]:
        if column not in df.columns:
            df[column] = ""

    # New refined state-machine columns.
    if "state" not in df.columns:
        df["state"] = ""

    if "waiting_type" not in df.columns:
        df["waiting_type"] = ""

    # Infer state for databases created before the refined engine.
    def infer_state(row):
        state = str(row.get("state") or "").strip()

        if state:
            return state

        owner = str(
            row.get("assigned_to")
            or "UNASSIGNED"
        )

        role = str(
            row.get("role")
            or ""
        ).upper()

        if owner == "UNASSIGNED":
            if role == "L3":
                return "L3_WAITING"
            if role == "DEV":
                return "DEV_WAITING"
            return "UNASSIGNED"

        if role == "L3":
            return "L3_ASSIGNED"

        if role == "DEV":
            return "DEV_ASSIGNED"

        return "ASSIGNED"

    df["state"] = df.apply(
        infer_state,
        axis=1,
    )

    def infer_waiting(row):
        waiting = str(
            row.get("waiting_type") or ""
        ).strip()

        if waiting:
            return waiting

        state = row["state"]

        if state == "L3_WAITING":
            return "L3_WAITING"

        if state == "DEV_WAITING":
            return "DEV_WAITING"

        if state == "UNASSIGNED":
            return "UNASSIGNED"

        return ""

    df["waiting_type"] = df.apply(
        infer_waiting,
        axis=1,
    )

    # Critical UI invariant:
    # waiting rows must never display the previous engineer.
    waiting_states = {
        "L3_WAITING",
        "DEV_WAITING",
        "UNASSIGNED",
    }

    df.loc[
        df["state"].isin(waiting_states),
        "assigned_to",
    ] = "UNASSIGNED"

    return df


def load_ticket_summary():
    df = load_table("ticket_summary")

    if df.empty:
        return df

    for column in [
        "ticket",
        "created",
        "l3_pickup_sla",
        "total_l3_time",
        "total_dev_time",
        "status",
        "resolution",
    ]:
        if column not in df.columns:
            df[column] = ""

    return df


# ============================================================
# DATA HELPERS
# ============================================================

def minutes_value(df, role=None, state=None):
    data = df

    if role is not None:
        data = data[
            data["role"].astype(str).str.upper()
            == role.upper()
        ]

    if state is not None:
        data = data[
            data["state"] == state
        ]

    return float(
        pd.to_numeric(
            data["duration_minutes"],
            errors="coerce",
        ).fillna(0).sum()
    )


def format_duration(minutes=None, seconds=None):
    """
    Human-readable SLA duration.

    Examples:
        30 seconds  -> 30s
        90 seconds  -> 1m 30s
        45 minutes  -> 45m
        90 minutes  -> 1h 30m
        480 minutes -> 8h
        1500 minutes -> 1d 1h
    """

    try:
        if seconds is not None:
            total_seconds = float(seconds or 0)
        else:
            total_seconds = float(minutes or 0) * 60.0
    except (TypeError, ValueError):
        total_seconds = 0.0

    total_seconds = max(0.0, total_seconds)

    if total_seconds < 60:
        return f"{int(round(total_seconds))}s"

    total_minutes = int(total_seconds // 60)

    if total_minutes < 60:
        return f"{total_minutes}m"

    total_hours = total_minutes // 60
    remaining_minutes = total_minutes % 60

    if total_hours < 24:
        if remaining_minutes:
            return f"{total_hours}h {remaining_minutes}m"
        return f"{total_hours}h"

    days = total_hours // 24
    remaining_hours = total_hours % 24

    if remaining_hours:
        return f"{days}d {remaining_hours}h"

    return f"{days}d"


def format_minutes(minutes):
    """Backward-compatible wrapper for older dashboard calls."""
    return format_duration(minutes=minutes)

def seconds_to_minutes(seconds):
    try:
        return float(seconds or 0) / 60.0
    except (TypeError, ValueError):
        return 0.0


def safe_datetime(value):
    if pd.isna(value) or value in ("", None):
        return pd.NaT

    return pd.to_datetime(
        value,
        errors="coerce",
        utc=True,
    )


def ticket_list(df):
    if df.empty:
        return []

    return sorted(
        {
            str(x)
            for x in df["ticket"].dropna()
            if str(x).strip()
        }
    )


def people_for_role(df, role):
    if df.empty:
        return []

    result = df[
        (
            df["role"]
            .astype(str)
            .str.upper()
            == role.upper()
        )
        &
        (
            df["assigned_to"]
            != "UNASSIGNED"
        )
    ]["assigned_to"].dropna()

    return sorted(
        {
            str(x)
            for x in result
            if str(x).strip()
        }
    )


def jira_url(ticket):
    # Dashboard can display the ticket as an internal selection.
    # If JIRA_BASE_URL exists in Streamlit environment, use it.
    import os

    base = os.getenv(
        "JIRA_BASE_URL",
        ""
    ).rstrip("/")

    if base:
        return f"{base}/browse/{ticket}"

    return ""


def navigate_to_ticket(ticket):
    st.query_params["ticket"] = ticket
    st.rerun()


def clear_ticket_view():
    if "ticket" in st.query_params:
        del st.query_params["ticket"]

    st.rerun()


# ============================================================
# HEADER
# ============================================================

st.markdown(
    """
    <div class="app-header">
        <div>
            <div class="app-title">L3 / Developer Transition SLA Dashboard</div>
            <div class="app-subtitle">
                Jira ownership, waiting time and transition SLA analysis
            </div>
        </div>
    </div>
    """,
    unsafe_allow_html=True,
)


if not DB.exists():
    st.error(
        "sla_dashboard.db was not found. Run the Jira refresh first."
    )
    st.stop()


transitions = load_transitions()
ticket_summary = load_ticket_summary()


if transitions.empty:
    st.warning(
        "No transition SLA data is available yet. "
        "Run main.py or scheduler.py to populate the database."
    )
    st.stop()


# ============================================================
# SIDEBAR SEARCH / NAVIGATION
# ============================================================

st.sidebar.markdown("### 🔎 Find Jira")

ticket_options = ticket_list(transitions)

search_ticket = st.sidebar.text_input(
    "Search Jira",
    placeholder="e.g. TE-25312",
)

if search_ticket.strip():
    query = search_ticket.strip().lower()

    matching_tickets = [
        ticket
        for ticket in ticket_options
        if query in ticket.lower()
    ]
else:
    matching_tickets = ticket_options[:50]


if matching_tickets:

    selected_ticket = st.sidebar.selectbox(
        "Matching Jira",
        matching_tickets,
        key="matching_ticket",
    )

    if st.sidebar.button(
        "Open SLA Dashboard →",
        use_container_width=True,
        type="primary",
    ):
        navigate_to_ticket(
            selected_ticket
        )

else:

    st.sidebar.info(
        "No Jira matched your search."
    )


st.sidebar.divider()


st.sidebar.markdown("### 👤 Find L3 / Developer")


people_role = st.sidebar.radio(
    "Person type",
    [
        "L3",
        "DEV",
    ],
    horizontal=True,
)


people = people_for_role(
    transitions,
    people_role,
)


selected_person = st.sidebar.selectbox(
    f"{people_role} person",
    ["All"] + people,
)


if selected_person != "All":

    person_tickets = sorted(
        transitions[
            (
                transitions["role"]
                .astype(str)
                .str.upper()
                == people_role.upper()
            )
            &
            (
                transitions["assigned_to"]
                == selected_person
            )
        ]["ticket"]
        .dropna()
        .unique()
        .tolist()
    )

    st.sidebar.caption(
        f"{len(person_tickets)} Jira(s) handled by "
        f"{selected_person}"
    )

    if person_tickets:

        person_ticket = st.sidebar.selectbox(
            "Select Jira",
            person_tickets,
            key="person_ticket",
        )

        if st.sidebar.button(
            f"Open {person_ticket} SLA →",
            use_container_width=True,
        ):
            navigate_to_ticket(
                person_ticket
            )


st.sidebar.divider()

if st.sidebar.button(
    "🏠 Overview",
    use_container_width=True,
):
    clear_ticket_view()


# ============================================================
# TICKET DETAIL PAGE
# ============================================================

requested_ticket = st.query_params.get(
    "ticket"
)


if requested_ticket:

    requested_ticket = str(
        requested_ticket
    ).strip()

    ticket_df = transitions[
        transitions["ticket"].astype(str)
        == requested_ticket
    ].copy()

    if ticket_df.empty:

        st.error(
            f"No transition data found for {requested_ticket}."
        )

        if st.button("← Back to Overview"):
            clear_ticket_view()

        st.stop()


    # --------------------------------------------------------
    # Ticket header
    # --------------------------------------------------------

    header_left, header_right = st.columns(
        [5, 1]
    )

    with header_left:

        st.markdown(
            f"## 🎫 {requested_ticket}"
        )

        project = (
            str(
                ticket_df["project"].iloc[0]
            )
            if "project" in ticket_df.columns
            else ""
        )

        st.caption(
            f"Project: {project or 'N/A'} • "
            f"Transition SLA Analysis"
        )

    with header_right:

        if st.button(
            "← Back",
            use_container_width=True,
        ):
            clear_ticket_view()


    # --------------------------------------------------------
    # Ticket metrics
    # --------------------------------------------------------

    l3_active = minutes_value(
        ticket_df,
        state="L3_ASSIGNED",
    )

    l3_waiting = minutes_value(
        ticket_df,
        state="L3_WAITING",
    )

    dev_active = minutes_value(
        ticket_df,
        state="DEV_ASSIGNED",
    )

    dev_waiting = minutes_value(
        ticket_df,
        state="DEV_WAITING",
    )

    generic_waiting = minutes_value(
        ticket_df,
        state="UNASSIGNED",
    )

    total_elapsed = (
        l3_active
        + l3_waiting
        + dev_active
        + dev_waiting
        + generic_waiting
    )


    k1, k2, k3, k4, k5 = st.columns(5)

    k1.metric(
        "L3 Active",
        format_duration(l3_active),
    )

    k2.metric(
        "L3 Waiting",
        format_duration(l3_waiting),
    )

    k3.metric(
        "DEV Active",
        format_duration(dev_active),
    )

    k4.metric(
        "DEV Waiting",
        format_duration(dev_waiting),
    )

    k5.metric(
        "Total SLA",
        format_duration(total_elapsed),
    )


    # --------------------------------------------------------
    # Current owner/state
    # --------------------------------------------------------

    last_row = (
        ticket_df
        .sort_values(
            "assigned_at",
            na_position="first",
        )
        .iloc[-1]
    )

    current_owner = (
        last_row["assigned_to"]
        or "UNASSIGNED"
    )

    current_state = (
        last_row["state"]
        or "UNKNOWN"
    )

    status = (
        last_row["status"]
        or ""
    )

    if current_owner == "UNASSIGNED":

        st.warning(
            f"⏳ Current state: {current_state} — "
            f"ticket is currently unassigned."
        )

    else:

        st.success(
            f"Current owner: {current_owner} "
            f"({current_state})"
        )


    # --------------------------------------------------------
    # SLA composition pie
    # --------------------------------------------------------

    left, right = st.columns(
        [1, 1]
    )

    with left:

        st.subheader(
            "SLA Time Composition"
        )

        composition = pd.DataFrame(
            {
                "State": [
                    "L3 Active",
                    "L3 Waiting",
                    "DEV Active",
                    "DEV Waiting",
                    "Unassigned",
                ],
                "Minutes": [
                    l3_active,
                    l3_waiting,
                    dev_active,
                    dev_waiting,
                    generic_waiting,
                ],
            }
        )

        composition = composition[
            composition["Minutes"] > 0
        ]

        if not composition.empty:

            fig = px.pie(
                composition,
                names="State",
                values="Minutes",
                hole=0.48,
            )

            fig.update_layout(
                margin=dict(
                    l=10,
                    r=10,
                    t=20,
                    b=10,
                ),
                legend_title="",
            )

            st.plotly_chart(
                fig,
                use_container_width=True,
            )

        else:

            st.info(
                "No measurable transition duration."
            )


    # --------------------------------------------------------
    # Engineer contribution
    # --------------------------------------------------------

    with right:

        st.subheader(
            "Ownership Contribution"
        )

        owners = ticket_df[
            ticket_df["assigned_to"] != "UNASSIGNED"
        ].copy()

        if not owners.empty:

            owner_chart = (
                owners
                .groupby(
                    [
                        "role",
                        "assigned_to",
                    ],
                    as_index=False,
                )["duration_minutes"]
                .sum()
                .sort_values(
                    "duration_minutes",
                    ascending=False,
                )
            )

            fig = px.bar(
                owner_chart,
                x="assigned_to",
                y="duration_minutes",
                color="role",
                text="duration_minutes",
            )

            fig.update_layout(
                xaxis_title="Owner",
                yaxis_title="Minutes",
                margin=dict(
                    l=10,
                    r=10,
                    t=20,
                    b=10,
                ),
            )

            st.plotly_chart(
                fig,
                use_container_width=True,
            )

        else:

            st.info(
                "No assigned ownership intervals."
            )


    # --------------------------------------------------------
    # Transition timeline
    # --------------------------------------------------------

    st.subheader(
        "🔄 Transition Timeline"
    )

    timeline = ticket_df.copy()

    timeline["Start"] = timeline[
        "assigned_at"
    ].apply(safe_datetime)

    timeline["End"] = timeline[
        "released_at"
    ].apply(safe_datetime)

    timeline["Duration"] = timeline[
        "duration_minutes"
    ].apply(format_minutes)

    timeline["Owner"] = timeline[
        "assigned_to"
    ]

    timeline["State"] = timeline[
        "state"
    ]

    timeline["Role"] = timeline[
        "role"
    ]

    timeline["Waiting Type"] = timeline[
        "waiting_type"
    ]

    display_columns = [
        "Role",
        "Owner",
        "State",
        "Waiting Type",
        "Start",
        "End",
        "Duration",
        "assigned_by",
        "status",
    ]

    display_columns = [
        c for c in display_columns
        if c in timeline.columns
    ]

    st.dataframe(
        timeline[
            display_columns
        ],
        use_container_width=True,
        hide_index=True,
    )


    # --------------------------------------------------------
    # Gantt-style transition chart
    # --------------------------------------------------------

    st.subheader(
        "⏱ Ownership / Waiting Timeline"
    )

    gantt = timeline[
        timeline["Start"].notna()
        & timeline["End"].notna()
    ].copy()

    if not gantt.empty:

        gantt["Label"] = (
            gantt["Role"].astype(str)
            + " • "
            + gantt["Owner"].astype(str)
        )

        fig = px.timeline(
            gantt,
            x_start="Start",
            x_end="End",
            y="Label",
            color="State",
            hover_data=[
                "Duration",
                "Waiting Type",
                "assigned_by",
                "status",
            ],
        )

        fig.update_yaxes(
            autorange="reversed"
        )

        fig.update_layout(
            height=max(
                320,
                80 * len(gantt),
            ),
            margin=dict(
                l=10,
                r=10,
                t=20,
                b=10,
            ),
        )

        st.plotly_chart(
            fig,
            use_container_width=True,
        )


    # --------------------------------------------------------
    # Waiting analysis
    # --------------------------------------------------------

    st.subheader(
        "⏳ Waiting Analysis"
    )

    w1, w2, w3 = st.columns(3)

    w1.metric(
        "L3 Waiting",
        format_duration(l3_waiting),
        help=(
            "Time after an L3 explicitly releases the "
            "ticket to UNASSIGNED until the next owner."
        ),
    )

    w2.metric(
        "DEV Waiting",
        format_duration(dev_waiting),
        help=(
            "Time after a DEV explicitly releases the "
            "ticket to UNASSIGNED until the next owner."
        ),
    )

    w3.metric(
        "Generic Unassigned",
        format_duration(generic_waiting),
        help=(
            "Unassigned intervals whose origin could not "
            "be attributed to L3 or DEV."
        ),
    )


    # --------------------------------------------------------
    # Assignment counts
    # --------------------------------------------------------

    st.subheader(
        "Handoffs & Assignments"
    )

    l3_assignments = len(
        ticket_df[
            ticket_df["state"]
            == "L3_ASSIGNED"
        ]
    )

    dev_assignments = len(
        ticket_df[
            ticket_df["state"]
            == "DEV_ASSIGNED"
        ]
    )

    l3_wait_events = len(
        ticket_df[
            ticket_df["state"]
            == "L3_WAITING"
        ]
    )

    dev_wait_events = len(
        ticket_df[
            ticket_df["state"]
            == "DEV_WAITING"
        ]
    )

    a1, a2, a3, a4 = st.columns(4)

    a1.metric(
        "L3 Assignments",
        l3_assignments,
    )

    a2.metric(
        "DEV Assignments",
        dev_assignments,
    )

    a3.metric(
        "L3 Waiting Events",
        l3_wait_events,
    )

    a4.metric(
        "DEV Waiting Events",
        dev_wait_events,
    )


    # --------------------------------------------------------
    # Jira link if configured
    # --------------------------------------------------------

    external_url = jira_url(
        requested_ticket
    )

    if external_url:

        st.link_button(
            "Open Jira Ticket ↗",
            external_url,
        )


    st.caption(
        f"Ticket status: {status or 'Unknown'} • "
        f"Last dashboard refresh: "
        f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )

    st.stop()


# ============================================================
# OVERVIEW PAGE
# ============================================================

st.subheader(
    "L3 / Developer Workload Overview"
)

st.caption(
    "Search a Jira to open its transition SLA dashboard, "
    "or select an L3 / DEV to see the Jira tickets handled by them."
)


# ============================================================
# TOP SEARCH BAR
# ============================================================

search_col, role_col, refresh_col = st.columns(
    [4, 2, 1]
)

with search_col:

    global_ticket_search = st.text_input(
        "Search Jira",
        placeholder="Search by Jira key, e.g. TE-25312",
        label_visibility="collapsed",
    )

with role_col:

    overview_role = st.selectbox(
        "Owner type",
        [
            "All",
            "L3",
            "DEV",
        ],
        label_visibility="collapsed",
    )

with refresh_col:

    if st.button(
        "Refresh",
        use_container_width=True,
    ):
        st.rerun()


# ============================================================
# FILTERED OVERVIEW
# ============================================================

overview = transitions.copy()

if global_ticket_search.strip():

    q = global_ticket_search.strip().lower()

    overview = overview[
        overview["ticket"]
        .astype(str)
        .str.lower()
        .str.contains(
            q,
            na=False,
        )
    ]


if overview_role != "All":

    overview = overview[
        overview["role"]
        .astype(str)
        .str.upper()
        == overview_role
    ]


# ============================================================
# OVERVIEW KPIs
# ============================================================

total_tickets = overview[
    "ticket"
].nunique()

l3_active_total = minutes_value(
    overview,
    state="L3_ASSIGNED",
)

l3_waiting_total = minutes_value(
    overview,
    state="L3_WAITING",
)

dev_active_total = minutes_value(
    overview,
    state="DEV_ASSIGNED",
)

dev_waiting_total = minutes_value(
    overview,
    state="DEV_WAITING",
)


k1, k2, k3, k4, k5 = st.columns(5)

k1.metric(
    "Jira Tickets",
    total_tickets,
)

k2.metric(
    "L3 Active",
    format_duration(l3_active_total),
)

k3.metric(
    "L3 Waiting",
    format_duration(l3_waiting_total),
)

k4.metric(
    "DEV Active",
    format_duration(dev_active_total),
)

k5.metric(
    "DEV Waiting",
    format_duration(dev_waiting_total),
)


# ============================================================
# CURRENT L3 OWNERS
# ============================================================

left, right = st.columns(
    [1, 1]
)

with left:

    st.subheader(
        "L3 Current Tickets"
    )

    current_l3 = overview[
        overview["state"]
        == "L3_ASSIGNED"
    ].copy()

    if not current_l3.empty:

        l3_current = (
            current_l3
            .groupby(
                "assigned_to",
                as_index=False,
            )["ticket"]
            .nunique()
            .rename(
                columns={
                    "ticket": "Tickets"
                }
            )
            .sort_values(
                "Tickets",
                ascending=False,
            )
        )

        fig = px.pie(
            l3_current,
            names="assigned_to",
            values="Tickets",
            hole=0.48,
        )

        fig.update_layout(
            margin=dict(
                l=10,
                r=10,
                t=20,
                b=10,
            ),
            legend_title="L3",
        )

        st.plotly_chart(
            fig,
            use_container_width=True,
        )

    else:

        st.info(
            "No current L3-owned tickets."
        )


with right:

    st.subheader(
        "Jiras Awaiting L3"
    )

    waiting_l3 = overview[
        overview["state"]
        == "L3_WAITING"
    ].copy()

    if not waiting_l3.empty:

        waiting_l3 = (
            waiting_l3
            .sort_values(
                "assigned_at",
                ascending=False,
            )
            .drop_duplicates(
                "ticket"
            )
        )

        waiting_l3["Waiting"] = (
            waiting_l3["duration_minutes"]
            .apply(format_minutes)
        )

        st.dataframe(
            waiting_l3[
                [
                    "ticket",
                    "project",
                    "Waiting",
                    "assigned_at",
                    "released_at",
                ]
            ],
            use_container_width=True,
            hide_index=True,
        )

    else:

        st.info(
            "No Jira currently in L3 waiting state."
        )


# ============================================================
# PERSON WORKLOAD
# ============================================================

st.subheader(
    "👥 L3 / Developer Workload"
)

assigned = overview[
    overview["assigned_to"] != "UNASSIGNED"
].copy()

if not assigned.empty:

    person_summary = (
        assigned
        .groupby(
            [
                "role",
                "assigned_to",
            ],
            as_index=False,
        )["duration_minutes"]
        .sum()
        .sort_values(
            "duration_minutes",
            ascending=False,
        )
    )

    fig = px.bar(
        person_summary,
        x="assigned_to",
        y="duration_minutes",
        color="role",
        text="duration_minutes",
    )

    fig.update_layout(
        xaxis_title="Engineer",
        yaxis_title="SLA Minutes",
        margin=dict(
            l=10,
            r=10,
            t=20,
            b=10,
        ),
    )

    st.plotly_chart(
        fig,
        use_container_width=True,
    )


# ============================================================
# SEARCH RESULTS / OPEN TICKET
# ============================================================

st.subheader(
    "🎫 Jira Tickets"
)

if overview.empty:

    st.info(
        "No Jira matches the selected filters."
    )

else:

    ticket_rows = (
        overview
        .groupby(
            "ticket",
            as_index=False,
        )
        .agg(
            total_minutes=(
                "duration_minutes",
                "sum",
            ),
            transitions=(
                "ticket",
                "size",
            ),
        )
        .sort_values(
            "ticket"
        )
    )

    # Current state/owner for each ticket.
    current_rows = []

    for ticket in ticket_rows["ticket"]:

        rows = overview[
            overview["ticket"] == ticket
        ].copy()

        rows = rows.sort_values(
            "assigned_at",
            na_position="first",
        )

        last = rows.iloc[-1]

        current_rows.append(
            {
                "ticket": ticket,
                "current_owner": (
                    last["assigned_to"]
                    or "UNASSIGNED"
                ),
                "state": last["state"],
                "status": last["status"],
            }
        )

    current_df = pd.DataFrame(
        current_rows
    )

    ticket_rows = ticket_rows.merge(
        current_df,
        on="ticket",
        how="left",
    )

    ticket_rows["Total SLA"] = (
        ticket_rows["total_minutes"]
        .apply(format_minutes)
    )

    st.dataframe(
        ticket_rows[
            [
                "ticket",
                "current_owner",
                "state",
                "status",
                "Total SLA",
                "transitions",
            ]
        ],
        use_container_width=True,
        hide_index=True,
    )


    st.caption(
        "Use the search box above to narrow the list, "
        "then select a Jira from the sidebar to open its SLA dashboard."
    )


# ============================================================
# AUTO REFRESH
# ============================================================

if "last_refresh" not in st.session_state:
    st.session_state.last_refresh = time.time()

if (
    time.time()
    - st.session_state.last_refresh
    > REFRESH_INTERVAL
):

    st.session_state.last_refresh = time.time()
    st.rerun()


st.divider()

st.caption(
    "Data source: Jira Cloud → main.py → sla_dashboard.db • "
    "Dashboard refresh interval: 5 minutes"
)
