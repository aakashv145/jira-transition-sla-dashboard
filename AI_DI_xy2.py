import sqlite3
import time
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
import requests

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None


def _load_env_fallback(env_path):
    """Minimal .env loader used only when python-dotenv is unavailable."""
    if not env_path.exists():
        return
    import os
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('\"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value

import pandas as pd
import re
import html
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None


# ============================================================
# PATHS / PAGE CONFIG
# ============================================================

ROOT = Path(__file__).resolve().parent

# Load the same .env used by the Jira transition collector.  The previous
# dashboard could silently skip the live Jira lookup when these variables were
# only present in .env, which caused stale DB values for reporter/priority.
if load_dotenv is not None:
    load_dotenv(ROOT / ".env", override=False)
else:
    _load_env_fallback(ROOT / ".env")

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
        /* Keep all app controls below Streamlit's fixed top toolbar/Deploy area. */
        .stAppViewContainer .main .block-container,
        .main .block-container {
            padding-top: 5.5rem !important;
            padding-bottom: 2rem;
        }

        /* Extra safety for older Streamlit DOM versions. */
        [data-testid="stAppViewContainer"] .main .block-container {
            padding-top: 5.5rem !important;
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

        /* Explicit snapshot cards prevent browser/Streamlit ellipsizing. */
        .snapshot-card-value {
            color: #172b4d;
            font-size: 1.42rem;
            font-weight: 700;
            margin-top: 0.25rem;
            line-height: 1.18;
            white-space: normal !important;
            overflow-wrap: anywhere;
            word-break: break-word;
            min-height: 3.0rem;
            display: flex;
            align-items: center;
        }
        .snapshot-card-label {
            color: #5e6c84;
            font-size: 0.82rem;
        }
        .snapshot-card {
            border-top: 3px solid #0c66e4;
            border-radius: 7px;
            background: #ffffff;
            padding: 0.8rem 0.9rem;
            min-height: 112px;
            box-shadow: 0 1px 2px rgba(9,30,66,.08);
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

DB_TIMEOUT_SECONDS = 60
DB_RETRIES = 8


def get_connection(read_only=True):
    """Open a SQLite connection optimized for dashboard reads."""
    if not DB.exists():
        raise FileNotFoundError(f"Database not found: {DB}")

    if read_only:
        uri = f"file:{DB.as_posix()}?mode=ro"
        conn = sqlite3.connect(
            uri,
            uri=True,
            timeout=DB_TIMEOUT_SECONDS,
            check_same_thread=False,
        )
    else:
        conn = sqlite3.connect(
            DB,
            timeout=DB_TIMEOUT_SECONDS,
            check_same_thread=False,
        )

    conn.execute(
        f"PRAGMA busy_timeout = {DB_TIMEOUT_SECONDS * 1000}"
    )

    if read_only:
        conn.execute("PRAGMA query_only = ON")

    return conn


def table_exists(table_name, conn=None):
    """Check whether a table exists, with retry protection."""
    own_connection = conn is None

    if own_connection:
        conn = get_connection(read_only=True)

    try:
        for attempt in range(DB_RETRIES):
            try:
                result = conn.execute(
                    """
                    SELECT 1
                    FROM sqlite_master
                    WHERE type='table'
                      AND name=?
                    LIMIT 1
                    """,
                    (table_name,),
                ).fetchone()

                return result is not None

            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower():
                    raise

                if attempt == DB_RETRIES - 1:
                    return False

                time.sleep(min(2 ** attempt, 8))

        return False

    finally:
        if own_connection and conn is not None:
            conn.close()


def table_columns(table_name):
    for attempt in range(DB_RETRIES):
        conn = None

        try:
            conn = get_connection(read_only=True)

            rows = conn.execute(
                f'PRAGMA table_info("{table_name}")'
            ).fetchall()

            return [row[1] for row in rows]

        except sqlite3.OperationalError as exc:

            if "no such table" in str(exc).lower():
                return []

            if "locked" not in str(exc).lower():
                raise

            if attempt == DB_RETRIES - 1:
                return []

            time.sleep(min(2 ** attempt, 8))

        finally:
            if conn is not None:
                conn.close()

    return []


def load_table(table_name):
    """
    Read a SQLite table safely while main.py/scheduler may be writing.

    The dashboard deliberately avoids doing a separate sqlite_master query
    before SELECT. This reduces the chance of failing on a transient schema
    lock.

    If a table was successfully loaded earlier in this Streamlit session,
    that last good copy is returned if the DB remains locked.
    """
    cache_key = f"_db_cache_{table_name}"
    last_error = None

    for attempt in range(DB_RETRIES):
        conn = None

        try:
            conn = get_connection(read_only=True)

            # Direct SELECT: fewer lock points than table_exists() + SELECT.
            df = pd.read_sql_query(
                f'SELECT * FROM "{table_name}"',
                conn,
            )

            # Keep a last-known-good copy for transient lock situations.
            try:
                st.session_state[cache_key] = df.copy()
            except Exception:
                pass

            return df

        except (sqlite3.OperationalError, pd.errors.DatabaseError) as exc:

            # pandas.read_sql_query() wraps sqlite3.OperationalError
            # inside pandas.errors.DatabaseError. The previous version
            # only caught sqlite3.OperationalError, so a locked database
            # could still terminate the Streamlit app.
            last_error = exc
            message = str(exc).lower()

            if "no such table" in message:
                return pd.DataFrame()

            if "database is locked" not in message and "database table is locked" not in message:
                raise

            if attempt < DB_RETRIES - 1:
                # Exponential backoff: 1, 2, 4, 8, 8, 8, 8 seconds.
                # SQLite's busy_timeout also applies on each connection.
                time.sleep(min(2 ** attempt, 8))

        finally:

            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    # Return the last successful snapshot instead of crashing Streamlit.
    try:
        cached = st.session_state.get(cache_key)

        if cached is not None:
            st.warning(
                f"SQLite is temporarily locked while reading "
                f"'{table_name}'. Showing the last successfully "
                f"loaded data; it will refresh automatically after "
                f"the writer releases the database."
            )
            return cached.copy()

    except Exception:
        pass

    # No previous snapshot exists. Fail with a useful message.
    if last_error is not None:
        raise RuntimeError(
            f"SQLite database is locked while reading '{table_name}'. "
            f"The Jira/SLA updater is currently holding a write transaction. "
            f"The dashboard retried the read several times but no previous "
            f"snapshot was available. Let the Jira updater finish its "
            f"transaction and refresh Streamlit. If this happens repeatedly, "
            f"enable SQLite WAL mode in the writer (main.py/database.py) "
            f"so dashboard reads can run while Jira data is being updated."
        ) from last_error

    return pd.DataFrame()


# ============================================================
# PERSON -> ROLE MAPPING
# ============================================================
# Keep the authoritative role mapping here.  The dashboard must
# not trust the role stored on the latest transition because a
# person can have historical transition rows with an incorrect
# or stale role.
#
# Add more people here as required. Matching is case-insensitive.
PERSON_ROLE_MAP = {
    "aakash verma": "L3",
    "anshul rawat": "L3",
    "kartikay sharma": "DEV",
}


def _norm_person(value):
    """Normalize a Jira person name for reliable identity comparison."""
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


def canonical_person_role(person, existing_roles=None, reporter=None):
    """
    Resolve the SLA role from identity.

    Business rule: the Jira reporter is NOT a DEV or L3 owner.  Reporter
    identity is checked before the static role map and before historical role
    evidence so a bad historical row can never turn the reporter into DEV.
    """
    name = str(person or "").strip()
    if not name or name.upper() in {"UNASSIGNED", "NONE", "NAN"}:
        return ""

    reporter_name = str(reporter or "").strip()
    if reporter_name and _norm_person(name) == _norm_person(reporter_name):
        return "REPORTER"

    mapped = PERSON_ROLE_MAP.get(_norm_person(name))
    if mapped:
        return mapped

    if existing_roles:
        roles = [
            str(r).strip().upper()
            for r in existing_roles
            if str(r or "").strip().upper() in {"L3", "DEV"}
        ]
        if roles:
            return str(pd.Series(roles).value_counts().index[0])

    return ""


_JIRA_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9_]+-\d+)\b", re.I)


def extract_jira_key(value):
    """
    Return the canonical upper-case Jira key inside `value`, or "".

    Tolerates values like "te-25300", " TE-25300 ", full Jira browse URLs and
    dashboard URLs such as "http://localhost:8505/?ticket=TE-25300".
    """
    text = str(value or "").strip()
    if not text:
        return ""
    match = _JIRA_KEY_RE.search(text)
    return match.group(1).upper() if match else ""


def normalize_status(value):
    return (
        str(value or "")
        .strip()
        .lower()
        .replace("’", "'")
    )


def infer_stage(row):
    """
    Derive workflow Stage independently from Role and State.

    Role answers WHO owns the SLA interval (L3 or DEV).
    Stage answers WHERE the Jira is in the workflow.
    State remains the underlying transition/state value.

    This intentionally prevents a state such as DEV_ASSIGNED from
    automatically turning an L3 owner into DEV.
    """
    role = str(row.get("role") or "").strip().upper()
    state = str(row.get("state") or "").strip().upper()
    status = normalize_status(row.get("status", ""))
    owner = str(row.get("assigned_to") or row.get("Owner") or "UNASSIGNED").strip()

    if owner.upper() in {"", "UNASSIGNED", "NONE", "NAN"}:
        if state == "L3_WAITING" or "pending rca" in status or "rca pending" in status:
            return "L3 / Triage"
        if (
            state == "DEV_WAITING"
            or "triag" in status
            or "ready for dev" in status
            or "ready for development" in status
        ):
            return "Waiting for Dev"
        return "Unassigned"

    # Owner identity is authoritative for Role and therefore for the
    # ownership stage. An L3 must never be displayed as Development merely
    # because the stored transition state is DEV_ASSIGNED.
    if role == "L3":
        return "L3 / Triage"

    if role == "DEV":
        return "Development"

    if role == "REPORTER":
        return "Reporter"

    if state == "L3_WAITING":
        return "L3 / Triage"

    if state == "DEV_WAITING":
        return "Waiting for Dev"

    return "Other"


def apply_person_roles(df):
    """Normalize role for every transition using person identity."""
    if df.empty:
        return df

    result = df.copy()
    if "assigned_to" not in result.columns:
        result["assigned_to"] = "UNASSIGNED"
    if "role" not in result.columns:
        result["role"] = ""

    # Build historical role evidence once per person.
    evidence = {}
    for person, person_df in result.groupby("assigned_to", dropna=False):
        evidence[str(person).strip().lower()] = person_df["role"].tolist()

    if "reporter" in result.columns:
        result["role"] = result.apply(
            lambda row: canonical_person_role(
                row.get("assigned_to"),
                evidence.get(str(row.get("assigned_to") or "").strip().lower(), []),
                reporter=row.get("reporter"),
            ),
            axis=1,
        )
    else:
        result["role"] = result["assigned_to"].apply(
            lambda person: canonical_person_role(
                person,
                evidence.get(str(person).strip().lower(), []),
            )
        )

    return result


@st.cache_data(ttl=REFRESH_INTERVAL, show_spinner=False)
def load_transitions():
    df = load_table("transitions")

    if df.empty:
        return df

    if "person" in df.columns and "assigned_to" not in df.columns:
        df = df.rename(columns={"person": "assigned_to"})

    if "assigned_to" not in df.columns:
        df["assigned_to"] = "UNASSIGNED"

    df["assigned_to"] = (
        df["assigned_to"]
        .fillna("UNASSIGNED")
        .astype(str)
        .replace({
            "": "UNASSIGNED",
            "None": "UNASSIGNED",
            "nan": "UNASSIGNED",
        })
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
        ).fillna(df["duration_minutes"] * 60)
    else:
        df["duration_seconds"] = df["duration_minutes"] * 60

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

    if "state" not in df.columns:
        df["state"] = ""

    if "waiting_type" not in df.columns:
        df["waiting_type"] = ""

    # Canonicalize roles BEFORE deriving state so an L3 such as
    # Aakash Verma cannot be incorrectly classified as DEV_WAITING/DEV_ASSIGNED.
    df = apply_person_roles(df)

    def infer_state(row):
        state = str(row.get("state") or "").strip()
        if state:
            return state

        owner = str(row.get("assigned_to") or "UNASSIGNED")
        role = str(row.get("role") or "").upper()

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

    df["state"] = df.apply(infer_state, axis=1)

    def infer_waiting(row):
        waiting = str(row.get("waiting_type") or "").strip()
        if waiting:
            return waiting

        if row["state"] == "L3_WAITING":
            return "L3_WAITING"
        if row["state"] == "DEV_WAITING":
            return "DEV_WAITING"
        if row["state"] == "UNASSIGNED":
            return "UNASSIGNED"
        return ""

    df["waiting_type"] = df.apply(infer_waiting, axis=1)

    # Stage is deliberately independent from State. This keeps an L3 owner
    # such as Anshul Rawat in the L3/Triage stage even if the historical
    # transition row contains DEV_ASSIGNED.
    df["stage"] = df.apply(infer_stage, axis=1)

    waiting_states = {"L3_WAITING", "DEV_WAITING", "UNASSIGNED"}
    df.loc[df["state"].isin(waiting_states), "assigned_to"] = "UNASSIGNED"

    return df


@st.cache_data(ttl=REFRESH_INTERVAL, show_spinner=False)
def load_ticket_summary():
    df = load_table("ticket_summary")

    if df.empty:
        return df

    if "ticket" in df.columns:
        cleaned = df["ticket"].apply(extract_jira_key)
        df["ticket"] = cleaned.where(cleaned != "", df["ticket"].astype(str).str.strip().str.upper())

    for column in [
        "ticket",
        "created",
        "l3_pickup_sla",
        "total_l3_time",
        "total_dev_time",
        "status",
        "resolution",
        "priority",
        "bug_severity",
        "issue_urgency",
        "issue_impact",
        "reporter",
        "l3_pickup_sla",
        "total_l3_time",
        "total_dev_time",
    ]:
        if column not in df.columns:
            df[column] = ""

    return df


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


def dashboard_base_url():
    """
    Return the current Streamlit dashboard base URL.

    The table links intentionally point back to this dashboard with
    ?ticket=<JIRA>, so clicking a Jira opens the existing transition
    SLA detail page rather than the external Jira application.
    """
    try:
        headers = st.context.headers
        host = headers.get("Host")

        if host:
            proto = headers.get(
                "X-Forwarded-Proto",
                "http",
            ).split(",")[0].strip()

            if proto not in {"http", "https"}:
                proto = "http"

            return f"{proto}://{host}".rstrip("/")

    except Exception:
        pass

    return ""


def transition_dashboard_url(ticket):
    ticket = str(ticket or "").strip()

    base = dashboard_base_url()

    if base:
        return (
            f"{base}/?ticket={quote(ticket, safe='')}"
        )

    # Relative URL is the safest option for local Streamlit and mounted
    # deployments when request headers are unavailable.
    return f"?ticket={quote(ticket, safe='')}"


def jira_url(ticket):
    """
    External Jira URL helper retained for places that need the
    actual Jira application URL.
    """
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

if not DB.exists():
    st.error(
        "sla_dashboard.db was not found. Run the Jira refresh first."
    )
    st.stop()


import sys as _sys
_script_t0 = time.perf_counter()

def _stage(msg):
    print(f"[dashboard +{time.perf_counter() - _script_t0:6.1f}s] {msg}", file=_sys.stderr, flush=True)

_stage("script start; loading transitions from DB")
transitions = load_transitions()
_stage(f"transitions loaded: {len(transitions):,} rows; loading ticket summary")
ticket_summary = load_ticket_summary()
_stage(f"ticket summary loaded: {len(ticket_summary):,} rows")

# Defensive normalization for databases created by older dashboard versions.
if not transitions.empty:
    if "stage" not in transitions.columns:
        transitions["stage"] = transitions.apply(infer_stage, axis=1)
    if "Stage" not in transitions.columns:
        transitions["Stage"] = transitions["stage"]


if transitions.empty:
    st.warning(
        "No transition SLA data is available yet. "
        "Run main.py or scheduler.py to populate the database."
    )
    st.stop()


# ============================================================
# SIDEBAR WORKLOAD NAVIGATION
# ============================================================

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
                width="stretch",
        ):
            navigate_to_ticket(
                person_ticket
            )


st.sidebar.checkbox(
    "Live Jira refresh on overview",
    value=True,
    key="overview_live_jira",
    help="ON (default): status, resolution, assignee, reporter and severity are bulk-fetched from Jira for every ticket in the selected date range. OFF: use the values stored in sla_dashboard.db (may be stale).",
)

if st.sidebar.button(
        "🏠 Overview",
        width="stretch",
):
    clear_ticket_view()


# ============================================================
# TICKET CURRENT STATUS / TRANSITION HISTORY HELPERS
# ============================================================

def get_ticket_current_status(ticket_summary, ticket, fallback=""):
    """Use ticket_summary as the source of truth for the Jira's current status."""
    if ticket_summary is None or ticket_summary.empty:
        return str(fallback or "").strip()

    if "ticket" not in ticket_summary.columns or "status" not in ticket_summary.columns:
        return str(fallback or "").strip()

    rows = ticket_summary[
        ticket_summary["ticket"].astype(str).str.strip().str.upper()
        == str(ticket).strip().upper()
        ]

    if rows.empty:
        return str(fallback or "").strip()

    # ticket_summary is the current Jira snapshot. Prefer the last row if
    # duplicate records exist.
    value = rows.iloc[-1]["status"]
    return str(value or fallback or "").strip()


def current_state_for_status(owner, status, fallback_state="UNKNOWN", existing_roles=None, reporter=None):
    """Derive the current ownership/waiting state from the live Jira assignee.

    The live Jira assignee is authoritative for the current state.  When the
    person's role is not in PERSON_ROLE_MAP, use historical transition role
    evidence for that person before falling back to the previous state.
    """
    owner_n = str(owner or "UNASSIGNED").strip()
    status_n = normalize_status(status)

    if owner_n.upper() in {"", "UNASSIGNED", "NONE", "NAN"}:
        if "pending rca" in status_n or "rca pending" in status_n:
            return "L3_WAITING"
        if "triag" in status_n or "ready for dev" in status_n or "ready for development" in status_n:
            return "DEV_WAITING"
        return "UNASSIGNED"

    role = canonical_person_role(owner_n, existing_roles=existing_roles, reporter=reporter)
    if role == "REPORTER":
        return "REPORTER_ASSIGNED"
    if role == "L3":
        return "L3_ASSIGNED"
    if role == "DEV":
        return "DEV_ASSIGNED"

    return str(fallback_state or "ASSIGNED")


def format_human_datetime(value):
    """Format timeline timestamps in Indian Standard Time (IST)."""
    dt = safe_datetime(value)
    if pd.isna(dt):
        return ""

    # Jira timestamps are stored in UTC. Display them in IST (UTC+05:30).
    if getattr(dt, "tzinfo", None) is not None:
        dt = dt.tz_convert("Asia/Kolkata")
    else:
        dt = dt.tz_localize("UTC").tz_convert("Asia/Kolkata")

    return dt.strftime("%d %b %Y, %I:%M:%S %p IST")


def infer_timeline_status(row):
    """
    Resolve the Jira workflow status represented by one historical SLA interval.

    Ownership and Jira status are deliberately kept separate:
      L3       -> Pending RCA
      REPORTER  -> Awaiting Customer Response
      DEV       -> Triaged

    An UNASSIGNED interval does not automatically mean the Jira status is
    ``Unassigned``. If the source row contains a real Jira status, preserve it;
    otherwise inherit the nearest workflow status from the chronological
    transition sequence. The sequence normalizer in build_transition_timeline
    handles the latter case.
    """
    state = str(row.get("State") or row.get("state") or "").strip().upper()
    role = str(row.get("Role") or row.get("role") or "").strip().upper()
    existing = str(row.get("status") or "").strip()
    existing_n = normalize_status(existing)

    if role == "L3" or state in {"L3_ASSIGNED", "L3_WAITING"}:
        return "Pending RCA"

    if role == "REPORTER" or state == "REPORTER_ASSIGNED":
        return "Awaiting Customer Response"

    if role == "DEV" or state in {"DEV_ASSIGNED", "DEV_WAITING"}:
        return "Triaged"

    # Do not let a generic assignment state overwrite a real Jira workflow
    # status. This is especially important for an unassigned Jira that is
    # already Triaged and waiting for the next developer assignment.
    if existing_n not in {"", "unassigned", "unknown", "none", "nan"}:
        return existing

    return existing


def normalize_timeline_statuses(timeline):
    """
    Make the historical Jira status sequence chronological and deterministic.

    This is the critical fix for the detail graph: a released/UNASSIGNED
    ownership row must not replace the actual Jira workflow status.
    """
    if timeline.empty:
        return timeline

    result = timeline.sort_values("Start", na_position="last").reset_index(drop=True).copy()
    result["status"] = result.apply(infer_timeline_status, axis=1)

    generic = {"", "unassigned", "unknown", "none", "nan"}

    # First pass: resolve generic rows from their nearest known workflow state.
    for i in range(len(result)):
        current = normalize_status(result.at[i, "status"])
        if current not in generic:
            continue

        # Prefer the next concrete ownership role because an UNASSIGNED row
        # immediately before a DEV assignment belongs to the Triaged phase.
        next_status = ""
        for j in range(i + 1, len(result)):
            candidate = str(result.at[j, "status"] or "").strip()
            if normalize_status(candidate) not in generic:
                next_status = candidate
                break

        if next_status:
            result.at[i, "status"] = next_status
            continue

        # Otherwise inherit the most recent concrete workflow status.
        for j in range(i - 1, -1, -1):
            candidate = str(result.at[j, "status"] or "").strip()
            if normalize_status(candidate) not in generic:
                result.at[i, "status"] = candidate
                break

    return result


def build_transition_timeline(ticket_df, current_status, live_current_owner=None):
    """Build ownership, waiting and current-status transition history."""
    timeline = ticket_df.copy()

    timeline["Start"] = timeline["assigned_at"].apply(safe_datetime)
    timeline["End"] = timeline["released_at"].apply(safe_datetime)
    timeline["Duration"] = timeline["duration_minutes"].apply(format_minutes)
    timeline["Owner"] = timeline["assigned_to"].fillna("UNASSIGNED")

    # Reporter is issue-level Jira metadata, not an ownership role.  Keep it
    # available on every transition row so the transition view clearly
    # distinguishes the Jira reporter from the person who owned the SLA
    # interval.
    reporter_name = str(ticket_df.attrs.get("reporter", "") or "").strip()
    timeline["Reporter"] = reporter_name

    # Always resolve Role from the owner identity first. The historical role
    # stored on a transition row is only fallback evidence.
    timeline["Role"] = timeline.apply(
        lambda row: canonical_person_role(
            row.get("Owner"),
            existing_roles=[row.get("role", "")],
            reporter=reporter_name,
        ),
        axis=1,
    )

    timeline["State"] = timeline["state"].fillna("")
    timeline["Waiting Type"] = timeline["waiting_type"].fillna("")

    # Stage is independent from Role/State and is derived after canonical Role.
    timeline["stage"] = timeline.apply(
        lambda row: infer_stage({
            "assigned_to": row.get("Owner"),
            "role": row.get("Role"),
            "state": row.get("State"),
            "status": row.get("status", ""),
        }),
        axis=1,
    )
    timeline["Stage"] = timeline["stage"]

    # IMPORTANT: historical rows must show the Jira status during that
    # ownership interval, not the latest/current ticket status copied into
    # the transitions snapshot. Normalize the entire sequence chronologically
    # so an UNASSIGNED ownership row does not become a fake Jira status.
    timeline = normalize_timeline_statuses(timeline)

    timeline["Event"] = "Ownership / waiting interval"

    if timeline.empty:
        return timeline

    # A previous dashboard run may have persisted a synthetic
    # "Current state" UNASSIGNED/L3_WAITING row into the source data. If the
    # live Jira is currently assigned, that row is stale and must never appear
    # in the historical timeline. Historical ownership rows are preserved.
    live_owner = str(live_current_owner or "UNASSIGNED").strip()
    if live_owner.upper() not in {"", "UNASSIGNED", "NONE", "NAN"}:
        synthetic_mask = (
            timeline["Owner"].astype(str).str.strip().str.upper().eq("UNASSIGNED")
            & timeline["assigned_by"].fillna("").astype(str).str.strip().str.lower().eq("current state")
        )
        timeline = timeline.loc[~synthetic_mask].copy()
        if timeline.empty:
            return timeline

    current_status = str(current_status or "").strip()
    status_rows = timeline[
        timeline["status"].fillna("").astype(str).str.strip() != ""
        ]
    known_status = (
        str(status_rows.iloc[-1]["status"]).strip()
        if not status_rows.empty else ""
    )

    timeline = timeline.sort_values("Start", na_position="last").reset_index(drop=True)
    latest_row = timeline.iloc[-1]
    latest_end = safe_datetime(latest_row.get("End"))

    # The historical timeline may end because an SLA ownership interval was
    # released. That does NOT mean the Jira is currently unassigned. When a
    # live Jira assignee is available, use it as the current-owner truth.
    if live_current_owner is not None:
        current_owner = str(live_current_owner or "UNASSIGNED").strip()
    else:
        current_owner = str(latest_row.get("Owner") or "UNASSIGNED").strip()
        if pd.notna(latest_end):
            current_owner = "UNASSIGNED"

    # Latest known activity is the best available status-change anchor when
    # Jira changelog timestamps are not stored in the SQLite database.
    activity_times = []
    for col in ["End", "Start"]:
        vals = timeline[col].dropna()
        if not vals.empty:
            activity_times.append(vals.max())
    event_time = max(activity_times) if activity_times else pd.NaT

    # Resolve the current owner's historical role evidence locally.
    # This function has its own scope, so it cannot rely on the
    # owner_role_evidence variable created by the ticket-detail renderer.
    owner_role_evidence = []
    if (
        not ticket_df.empty
        and "assigned_to" in ticket_df.columns
        and "role" in ticket_df.columns
        and current_owner
    ):
        owner_role_evidence = ticket_df.loc[
            ticket_df["assigned_to"]
            .fillna("")
            .astype(str)
            .str.strip()
            .str.lower()
            == current_owner.strip().lower(),
            "role",
        ].dropna().astype(str).tolist()

    if current_status and normalize_status(current_status) != normalize_status(known_status):
        event_state = current_state_for_status(
            current_owner, current_status, fallback_state="UNKNOWN", reporter=reporter_name
        )
        timeline = pd.concat([
            timeline,
            pd.DataFrame([{
                "Role": canonical_person_role(current_owner, existing_roles=owner_role_evidence, reporter=reporter_name),
                "Owner": current_owner,
                "State": event_state,
                "Stage": infer_stage({
                    "assigned_to": current_owner,
                    "role": canonical_person_role(current_owner, existing_roles=owner_role_evidence, reporter=reporter_name),
                    "state": event_state,
                    "status": current_status,
                }),
                "Waiting Type": (
                    "DEV_WAITING" if event_state == "DEV_WAITING"
                    else "L3_WAITING" if event_state == "L3_WAITING"
                    else "UNASSIGNED"
                ),
                "Start": event_time,
                "End": pd.NaT,
                "Duration": "0s",
                "assigned_by": "Status transition",
                # This is a synthetic CURRENT status event, so unlike
                # historical ownership rows it must use the live Jira status.
                "status": current_status,
                "Event": f"Status changed: {known_status or 'Unknown'} → {current_status}",
            }]),
        ], ignore_index=True)

    # Only create a synthetic active waiting interval when the LIVE Jira
    # assignee is actually unassigned. A released historical interval is not
    # sufficient evidence that the Jira is currently unassigned.
    if current_owner.upper() == "UNASSIGNED" and pd.notna(latest_end):
        waiting_state = current_state_for_status(
            "UNASSIGNED", current_status, fallback_state="UNASSIGNED"
        )
        now = pd.Timestamp.now(tz="UTC")
        wait_start = latest_end
        if wait_start.tzinfo is None:
            wait_start = wait_start.tz_localize("UTC")
        live_minutes = max(
            0.0, (now - wait_start).total_seconds() / 60.0
        )

        timeline = pd.concat([
            timeline,
            pd.DataFrame([{
                "Role": "",
                "Owner": "UNASSIGNED",
                "State": waiting_state,
                "Stage": infer_stage({
                    "assigned_to": "UNASSIGNED",
                    "role": "",
                    "state": waiting_state,
                    "status": current_status,
                }),
                "Waiting Type": waiting_state,
                "Start": wait_start,
                "End": now,
                "Duration": format_minutes(live_minutes),
                "assigned_by": "Current state",
                "status": current_status,
                "Event": "Current waiting for next owner",
            }]),
        ], ignore_index=True)

    return timeline.sort_values("Start", na_position="last").reset_index(drop=True)


# ============================================================
# TICKET TRANSITION SLA DETAIL PAGE
# ============================================================

def is_resolved_jira(status, resolution=None):
    """True when the Jira is closed out (Done / Closed / Won't Do / Resolved / Deployed)."""
    return is_final_status(status) or is_final_status(resolution)


def build_sla_health_snapshot(timeline, current_status, current_priority, current_resolution=None):
    """
    Build deterministic SLA-health facts before any AI explanation.

    A resolved Jira (status or resolution in FINAL_STATUSES) is reported as
    RESOLVED with risk 0.  It is never shown as BREACHED / AT RISK, because
    there is nothing left to act on.  The historical over/under-target figure
    is still surfaced in the risk factor for reporting.
    """
    if timeline is None or timeline.empty:
        return {}

    work = timeline.copy()
    work["duration_minutes"] = pd.to_numeric(
        work.get("duration_minutes", 0), errors="coerce"
    ).fillna(0)
    work["Jira Status"] = work.get("status", "").fillna("").astype(str).str.strip()
    work["Jira Status"] = work["Jira Status"].replace(
        {"": "Unknown", "nan": "Unknown", "None": "Unknown"}
    )

    status_totals = (
        work.groupby("Jira Status", as_index=False)["duration_minutes"]
        .sum()
        .sort_values("duration_minutes", ascending=False)
    )

    # SLA clock pauses while awaiting the customer, or while the ticket is
    # parked "Under Observation" with the team.
    customer_wait_mask = work["Jira Status"].apply(is_sla_hold_status)
    customer_wait_minutes = float(work.loc[customer_wait_mask, "duration_minutes"].sum())
    work = work[~customer_wait_mask]
    status_totals = status_totals[~status_totals["Jira Status"].apply(is_sla_hold_status)]

    total_minutes = float(work["duration_minutes"].sum())
    target_hours = priority_target_hours(current_priority)
    total_hours = total_minutes / 60.0
    breach_hours = max(0.0, total_hours - target_hours)

    # Deterministic risk score. AI explains this score; it does not invent it.
    score = 0
    if target_hours > 0:
        ratio = total_hours / target_hours
        if ratio >= 1.0:
            score += 70
        elif ratio >= 0.85:
            score += 50
        elif ratio >= 0.70:
            score += 30
        elif ratio >= 0.50:
            score += 15

    normalized_status = normalize_status(current_status)
    if "pending rca" in normalized_status:
        score += 20

    if not status_totals.empty:
        longest = float(status_totals.iloc[0]["duration_minutes"])
        if total_minutes and longest / total_minutes >= 0.70:
            score += 10

    score = min(100, int(score))
    resolved = is_resolved_jira(current_status, current_resolution)
    currently_on_hold = is_sla_hold_status(current_status)
    if resolved:
        health = "RESOLVED"
        score = 0
    elif currently_on_hold:
        # Ball is not with the team right now (awaiting customer / under
        # observation) — never surface this as an active breach that needs
        # action, regardless of time accrued before it went on hold.
        health = "AT RISK" if score >= 40 else "HEALTHY"
    elif score >= 70:
        health = "BREACHED"
    elif score >= 40:
        health = "AT RISK"
    else:
        health = "HEALTHY"

    # Human-readable deterministic risk drivers. These are intentionally based
    # only on Jira/SLA data and are safe to show directly on the dashboard.
    risk_drivers = []
    if target_hours > 0:
        utilization = (total_hours / target_hours) * 100.0
        if utilization >= 100:
            risk_drivers.append(f"SLA target exceeded by {breach_hours:.1f}h")
        elif utilization >= 85:
            risk_drivers.append(f"SLA utilization {utilization:.0f}%")
        elif utilization >= 70:
            risk_drivers.append(f"SLA utilization {utilization:.0f}%")

    if "pending rca" in normalized_status:
        risk_drivers.append("Pending RCA")
    elif is_customer_wait_status(current_status):
        risk_drivers.insert(0, "SLA paused — awaiting customer")
    elif is_observation_status(current_status):
        risk_drivers.insert(0, "SLA paused — under observation")

    if not status_totals.empty and total_minutes:
        longest = float(status_totals.iloc[0]["duration_minutes"])
        concentration = (longest / total_minutes) * 100.0
        if concentration >= 70:
            longest_status = str(status_totals.iloc[0]["Jira Status"])
            risk_drivers.append(f"{concentration:.0f}% of SLA in {longest_status}")

    if not risk_drivers:
        risk_drivers.append("Within SLA target")

    if resolved:
        closed_as = str(current_resolution or current_status or "Done").strip()
        if target_hours > 0 and breach_hours > 0:
            risk_drivers = [f"Resolved ({closed_as})", f"closed {breach_hours:.1f}h over SLA target"]
        else:
            risk_drivers = [f"Resolved ({closed_as})", "closed within SLA target"]

    return {
        "health": health,
        "risk_score": score,
        "risk_factor": " • ".join(risk_drivers[:2]),
        "current_status": str(current_status or "Unknown"),
        "priority": str(current_priority or "Unknown"),
        "total_sla": format_minutes(total_minutes),
        "customer_wait": format_minutes(customer_wait_minutes),
        "customer_wait_minutes": customer_wait_minutes,
        "sla_paused": currently_on_hold,
        "target": format_hours_value(target_hours),
        "breach": format_hours_value(breach_hours),
        "status_durations": [
            {
                "status": str(row["Jira Status"]),
                "duration": format_minutes(float(row["duration_minutes"])),
            }
            for _, row in status_totals.iterrows()
        ],
    }


def generate_ai_sla_health(snapshot):
    """Ask the model for a concise, structured explanation of SLA health."""
    import os

    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return None, "OPENAI_API_KEY is not configured."
    if OpenAI is None:
        return None, "The OpenAI Python package is not installed. Run: pip install openai"

    client = OpenAI(api_key=api_key)
    schema = {
        "type": "object",
        "properties": {
            "health": {"type": "string", "enum": ["HEALTHY", "AT RISK", "BREACHED", "RESOLVED"]},
            "summary": {"type": "string"},
            "risks": {"type": "array", "items": {"type": "string"}},
            "recommended_actions": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["health", "summary", "risks", "recommended_actions"],
        "additionalProperties": False,
    }

    response = client.responses.create(
        model=os.getenv("OPENAI_SLA_MODEL", "gpt-5.6-luna"),
        instructions=(
            "You are an SLA operations analyst. Analyze only the supplied Jira SLA facts. "
            "Do not invent missing events, people, timestamps, or SLA targets. "
            "The risk_score and deterministic health are calculated by the application; "
            "use them as the source of truth. Give concise operational recommendations."
        ),
        input=json.dumps(snapshot, ensure_ascii=False),
        text={
            "format": {
                "type": "json_schema",
                "name": "sla_health",
                "strict": True,
                "schema": schema,
            },
            "verbosity": "low",
        },
    )

    try:
        parsed = json.loads(response.output_text)
    except Exception as exc:
        return None, f"AI response could not be parsed: {exc}"

    return parsed, None


def render_ticket_transition_page(ticket, transitions, ticket_summary):
    """
    Render a Jira-specific Transition SLA page.

    Navigation is driven by the URL query parameter:
        ?ticket=TE-26070

    This is intentionally rendered before the overview so both:
      - clicking a Jira in the Active Jira table, and
      - using the "Open SLA" text field
    open the same detail view instead of returning to the overview.
    """
    ticket = str(ticket or "").strip().upper()

    if not ticket:
        return

    # Pull every transition/ownership record for this Jira.
    if transitions.empty or "ticket" not in transitions.columns:
        ticket_df = pd.DataFrame()
    else:
        ticket_df = transitions[
            transitions["ticket"].astype(str).str.strip().str.upper() == ticket
            ].copy()

    # Current Jira status MUST come from ticket_summary when available.
    fallback_status = ""
    if not ticket_df.empty and "status" in ticket_df.columns:
        vals = ticket_df["status"].dropna().astype(str).str.strip()
        if not vals.empty:
            fallback_status = vals.iloc[-1]

    current_status = get_ticket_current_status(
        ticket_summary,
        ticket,
        fallback=fallback_status,
    )

    # Current owner must match Jira's live assignee. Transition history is
    # retained for SLA calculations, but released_at only means that the
    # historical SLA ownership interval ended; it does NOT prove that Jira
    # is currently unassigned.
    current_owner = "UNASSIGNED"
    jira_assignee_verified = False

    # Prefer the current Jira snapshot first when it already contains an
    # assignee. This keeps the detail page consistent with the overview.
    summary_rows = pd.DataFrame()
    if not ticket_summary.empty and "ticket" in ticket_summary.columns:
        summary_rows = ticket_summary[
            ticket_summary["ticket"].astype(str).str.strip().str.upper() == ticket
        ].copy()
        if not summary_rows.empty and "assignee" in summary_rows.columns:
            cached_assignee = str(summary_rows.iloc[-1].get("assignee") or "").strip()
            if cached_assignee and cached_assignee.upper() not in {
                "UNASSIGNED", "NONE", "NAN"
            }:
                current_owner = cached_assignee

    current_priority = "—"
    current_reporter = ""
    if not ticket_summary.empty and "ticket" in ticket_summary.columns:
        reporter_rows = ticket_summary[
            ticket_summary["ticket"].astype(str).str.strip().str.upper() == ticket
        ]
        if not reporter_rows.empty and "reporter" in reporter_rows.columns:
            value = str(reporter_rows.iloc[-1].get("reporter") or "").strip()
            if value:
                current_reporter = value

    if not current_reporter and not ticket_df.empty and "reporter" in ticket_df.columns:
        values = ticket_df["reporter"].dropna().astype(str).str.strip()
        values = values[values != ""]
        if not values.empty:
            current_reporter = values.iloc[-1]

    if not ticket_summary.empty and "ticket" in ticket_summary.columns:
        priority_rows = ticket_summary[
            ticket_summary["ticket"].astype(str).str.strip().str.upper() == ticket
        ]
        if not priority_rows.empty:
            severity_value = str(priority_rows.iloc[-1].get("bug_severity") or "").strip() if "bug_severity" in priority_rows.columns else ""
            value = severity_value or (str(priority_rows.iloc[-1].get("priority") or "").strip() if "priority" in priority_rows.columns else "")
            if value:
                match = re.search(r"\bsev(?:erity)?\s*[-_ ]?([1-4])\b", value, re.I)
                current_priority = f"Sev {match.group(1)}" if match else value

    # Current owner must match Jira's live assignee.

    # Refresh the single ticket from Jira. If Jira successfully returned the
    # issue, an empty assignee is a real UNASSIGNED value and must not be
    # replaced by a historical transition owner.
    current_resolution = ""
    if not ticket_summary.empty and "ticket" in ticket_summary.columns and "resolution" in ticket_summary.columns:
        _res_rows = ticket_summary[
            ticket_summary["ticket"].astype(str).str.strip().str.upper() == ticket
        ]
        if not _res_rows.empty:
            current_resolution = str(_res_rows.iloc[-1].get("resolution") or "").strip()

    live_ticket = pd.DataFrame([{"ticket": ticket}])
    live_ticket = refresh_current_jira_fields(live_ticket)
    if not live_ticket.empty:
        live_status = str(live_ticket.iloc[0].get("status") or "").strip()
        if live_status:
            current_status = live_status
        live_resolution = str(live_ticket.iloc[0].get("resolution") or "").strip()
        if live_resolution:
            current_resolution = live_resolution

        live_priority = str(live_ticket.iloc[0].get("priority") or "").strip()
        live_severity = str(live_ticket.iloc[0].get("bug_severity") or "").strip()
        if live_severity:
            current_priority = live_severity
        elif live_priority:
            current_priority = live_priority

        live_reporter = str(live_ticket.iloc[0].get("reporter") or "").strip()
        if live_reporter:
            current_reporter = live_reporter

        jira_assignee_verified = bool(live_ticket.iloc[0].get("assignee_verified", False))
        if jira_assignee_verified:
            live_assignee = str(live_ticket.iloc[0].get("assignee") or "").strip()
            current_owner = (
                live_assignee
                if live_assignee and live_assignee.upper() not in {"NONE", "NAN"}
                else "UNASSIGNED"
            )
        elif current_owner == "UNASSIGNED" and "assignee" in live_ticket.columns:
            live_assignee = str(live_ticket.iloc[0].get("assignee") or "").strip()
            if live_assignee and live_assignee.upper() not in {
                "UNASSIGNED", "NONE", "NAN"
            }:
                current_owner = live_assignee

    # Never hide a live-Jira failure. The dashboard may still render cached
    # transition data, but the user can immediately see why reporter/priority
    # could not be refreshed from Jira.
    if not live_ticket.empty:
        live_error = str(live_ticket.iloc[0].get("jira_refresh_error") or "").strip()
        if live_error:
            st.warning(f"Live Jira fields could not be refreshed: {live_error}")

    # Fallback when neither Jira nor the current ticket snapshot provides an
    # assignee. Do not use a historical row if Jira explicitly confirmed that
    # the issue is currently unassigned.
    if (
        current_owner == "UNASSIGNED"
        and not jira_assignee_verified
        and not ticket_df.empty
    ):
        ordered = ticket_df.copy()
        if "assigned_at" in ordered.columns:
            ordered["_assigned_at"] = ordered["assigned_at"].apply(safe_datetime)
            ordered = ordered.sort_values("_assigned_at", na_position="last")
        else:
            ordered = ordered.reset_index(drop=True)

        latest = ordered.iloc[-1]
        assigned_to = str(latest.get("assigned_to") or "").strip()
        if assigned_to and assigned_to.upper() not in {"UNASSIGNED", "NONE", "NAN"}:
            current_owner = assigned_to

    # Current State must follow the live Jira assignee, not the last
    # historical SLA row.  Use the owner's historical role evidence when
    # the person is not explicitly present in PERSON_ROLE_MAP.
    owner_role_evidence = []
    if not ticket_df.empty and "assigned_to" in ticket_df.columns and "role" in ticket_df.columns:
        owner_role_evidence = ticket_df.loc[
            ticket_df["assigned_to"].fillna("").astype(str).str.strip().str.lower()
            == current_owner.strip().lower(),
            "role",
        ].tolist()

    current_state = current_state_for_status(
        current_owner,
        current_status,
        fallback_state="UNASSIGNED",
        existing_roles=owner_role_evidence,
        reporter=current_reporter,
    )

    # Preserve the live Jira reporter as issue-level metadata on the timeline.
    # Reporter and assignee are intentionally separate concepts: a reporter can
    # also have been an assignee historically, but that must not make the
    # reporter's identity replace the actual SLA owner.
    ticket_df.attrs["reporter"] = current_reporter

    # Build the historical timeline. Pass the live Jira owner so a released
    # historical interval cannot create a false UNASSIGNED/L3_WAITING row when
    # the Jira is currently assigned.
    timeline = build_transition_timeline(
        ticket_df,
        current_status,
        live_current_owner=current_owner,
    )

    # Final reporter guard for the detail table: historical DB rows can carry
    # a stale DEV role for a person who is actually the Jira reporter.
    if not timeline.empty and current_reporter:
        reporter_candidates = {_norm_person(current_reporter)}
        if not live_ticket.empty:
            raw_aliases = str(live_ticket.iloc[0].get("reporter_aliases") or "")
            reporter_candidates.update(
                _norm_person(x) for x in raw_aliases.split("|") if str(x).strip()
            )
        owner_norm = timeline["Owner"].fillna("").astype(str).map(_norm_person)
        reporter_mask = owner_norm.isin(reporter_candidates)
        timeline.loc[reporter_mask, "Role"] = "REPORTER"
        timeline.loc[reporter_mask, "Stage"] = "Reporter"
        timeline.loc[reporter_mask, "State"] = "REPORTER_ASSIGNED"
        timeline.loc[reporter_mask, "status"] = timeline.loc[reporter_mask, "status"].fillna("")
        timeline.loc[reporter_mask, "Reporter"] = current_reporter

    # Keep the detail page task-focused: navigation + metrics, no page title.
    _, action_col = st.columns([8, 1])

    with action_col:
        if st.button(
                "← Overview",
                width="stretch",
                key=f"overview_{ticket}",
        ):
            clear_ticket_view()

    # Current snapshot.
    current_role = canonical_person_role(
        current_owner,
        existing_roles=owner_role_evidence,
        reporter=current_reporter,
    )
    if not live_ticket.empty:
        raw_aliases = str(live_ticket.iloc[0].get("reporter_aliases") or "")
        reporter_candidates = {_norm_person(current_reporter)} if current_reporter else set()
        reporter_candidates.update(
            _norm_person(x) for x in raw_aliases.split("|") if str(x).strip()
        )
        if _norm_person(current_owner) in reporter_candidates:
            current_role = "REPORTER"
    current_stage = infer_stage({
        "assigned_to": current_owner,
        "role": current_role,
        "state": current_state,
        "status": current_status,
    })

    if current_reporter:
        st.caption(f"Reporter: **{current_reporter}**")

    issue_urgency = str(live_ticket.iloc[0].get("issue_urgency") or "").strip() if not live_ticket.empty else ""
    issue_impact = str(live_ticket.iloc[0].get("issue_impact") or "").strip() if not live_ticket.empty else ""
    if issue_urgency or issue_impact:
        parts = []
        if current_priority != "—": parts.append(f"Severity: **{current_priority}**")
        if issue_urgency: parts.append(f"Issue Urgency: **{issue_urgency}**")
        if issue_impact: parts.append(f"Issue Impact: **{issue_impact}**")
        st.caption(" • ".join(parts))

    # ============================================================
    # EXECUTIVE SUMMARY — ALWAYS FIRST
    # ============================================================
    # The first screen must answer the operational questions immediately:
    #   1. Is the Jira healthy / breached?
    #   2. What is the risk score?
    #   3. How much SLA time has been consumed?
    #   4. Which Jira statuses consumed that time?
    # The detailed transition audit is intentionally rendered later.

    total_minutes = 0.0
    if not timeline.empty and "duration_minutes" in timeline.columns:
        total_minutes = float(
            pd.to_numeric(timeline["duration_minutes"], errors="coerce")
            .fillna(0)
            .sum()
        )

    chart_df = pd.DataFrame()
    if not timeline.empty:
        chart_df = timeline.copy()
        chart_df["Start"] = chart_df["Start"].apply(safe_datetime)
        chart_df["End"] = chart_df["End"].apply(safe_datetime)
        chart_df["Duration Minutes"] = pd.to_numeric(
            chart_df.get("duration_minutes", 0), errors="coerce"
        ).fillna(0)
        chart_df["Owner"] = chart_df.get("Owner", "UNASSIGNED").fillna("UNASSIGNED").astype(str)
        chart_df["Role"] = chart_df.get("Role", "").fillna("").astype(str)
        chart_df["State"] = chart_df.get("State", "UNKNOWN").fillna("UNKNOWN").astype(str)

        if "status" not in chart_df.columns:
            chart_df["status"] = "Unknown"
        chart_df = normalize_timeline_statuses(chart_df)
        chart_df["Jira Status"] = (
            chart_df["status"]
            .fillna("Unknown")
            .astype(str)
            .str.strip()
            .replace({"": "Unknown", "nan": "Unknown", "None": "Unknown"})
        )

        # Open intervals need an end timestamp for Plotly. Do not convert the
        # Jira status to UNASSIGNED merely because the owner is currently empty.
        open_mask = chart_df["End"].isna() & chart_df["Start"].notna()
        if open_mask.any():
            now_utc = pd.Timestamp.now(tz="UTC")
            for idx in chart_df.index[open_mask]:
                start_value = chart_df.at[idx, "Start"]
                duration_value = float(chart_df.at[idx, "Duration Minutes"] or 0)
                calculated_end = start_value + pd.Timedelta(minutes=duration_value)
                chart_df.at[idx, "End"] = max(start_value, min(now_utc, calculated_end))

        chart_df = chart_df[
            chart_df["Start"].notna() & chart_df["End"].notna()
        ].copy()

    if not chart_df.empty:
        health_snapshot = build_sla_health_snapshot(
            chart_df, current_status, current_priority, current_resolution
        )
    else:
        health_snapshot = {
            "health": "UNKNOWN",
            "risk_score": 0,
            "total_sla": format_minutes(total_minutes),
            "target": "—",
            "current_status": current_status or "Unknown",
            "risk_factor": "Insufficient timestamped SLA data",
        }

    # ------------------------------------------------------------
    # 1. JIRA HEALTH — TOP PRIORITY
    # ------------------------------------------------------------
    st.markdown("## SLA Health Overview")
    h1, h2, h3, h4, h5 = st.columns(5)
    h1.metric("Jira Health", health_snapshot.get("health", "UNKNOWN"))
    h2.metric("Risk Score", f"{health_snapshot.get('risk_score', 0)}/100")
    h3.metric("Total SLA", health_snapshot.get("total_sla", format_minutes(total_minutes)))
    h4.metric("SLA Target", health_snapshot.get("target", "—"))
    h5.metric("Current Status", health_snapshot.get("current_status", current_status or "Unknown"))

    risk_factor = health_snapshot.get("risk_factor", "")
    if risk_factor:
        st.caption(f"Primary risk factor: **{risk_factor}**")
    if float(health_snapshot.get("customer_wait_minutes", 0) or 0) > 0:
        st.caption(
            f"On-hold time excluded from Total SLA: **{health_snapshot.get('customer_wait')}** "
            "(SLA clock is paused while awaiting customer response or under observation)."
        )

    health_value = str(health_snapshot.get("health", "UNKNOWN")).upper()
    if health_value == "RESOLVED":
        st.info(
            f"JIRA RESOLVED  •  no active SLA  •  "
            f"Current Jira status: {health_snapshot.get('current_status', current_status or 'Unknown')}"
            + (f"  •  Resolution: {current_resolution}" if current_resolution else "")
        )
    elif health_value == "BREACHED":
        st.error(
            f"SLA BREACHED  •  Risk {health_snapshot.get('risk_score', 0)}/100  •  "
            f"Current Jira status: {health_snapshot.get('current_status', current_status or 'Unknown')}"
        )
    elif health_value == "AT RISK":
        st.warning(
            f"SLA AT RISK  •  Risk {health_snapshot.get('risk_score', 0)}/100  •  "
            f"Current Jira status: {health_snapshot.get('current_status', current_status or 'Unknown')}"
        )
    elif health_value == "HEALTHY":
        st.success(
            f"SLA HEALTHY  •  Risk {health_snapshot.get('risk_score', 0)}/100  •  "
            f"Current Jira status: {health_snapshot.get('current_status', current_status or 'Unknown')}"
        )

    # ------------------------------------------------------------
    # 2. TOTAL SLA BY JIRA STATUS — CHRONOLOGICAL
    # ------------------------------------------------------------
    if not chart_df.empty:
        status_totals = (
            chart_df.groupby("Jira Status", as_index=False)["Duration Minutes"]
            .sum()
        )
        first_seen = (
            chart_df.groupby("Jira Status", as_index=False)["Start"]
            .min()
            .rename(columns={"Start": "First Seen"})
        )
        status_totals = (
            status_totals.merge(first_seen, on="Jira Status", how="left")
            .sort_values("First Seen")
            .reset_index(drop=True)
        )
        status_totals["Duration"] = status_totals["Duration Minutes"].apply(
            lambda x: format_minutes(float(x))
        )
        status_totals["Cumulative Minutes"] = status_totals["Duration Minutes"].cumsum()
        # Cumulative SLA elapsed when this Jira status phase ends.
        status_totals["Total SLA at Time"] = status_totals["Cumulative Minutes"].apply(
            lambda x: format_minutes(float(x))
        )
        status_totals["Bar Label"] = status_totals.apply(
            lambda r: f"{r['Jira Status']} • {r['Duration']}", axis=1
        )
        status_totals["Cumulative Label"] = status_totals["Cumulative Minutes"].apply(
            lambda x: format_minutes(float(x))
        )

        st.markdown("### Total SLA Time by Jira Status")
        st.caption(
            "Bars show total time spent in each Jira status. The line shows cumulative SLA, "
            "in the order the statuses first occurred."
        )

        fig_total = go.Figure()
        fig_total.add_bar(
            x=status_totals["Jira Status"],
            y=status_totals["Duration Minutes"],
            text=status_totals["Bar Label"],
            textposition="inside",
            insidetextanchor="middle",
            customdata=status_totals[["Duration", "First Seen"]],
            name="Status Duration",
            hovertemplate=(
                "<b>%{x}</b><br>"
                "Duration: %{customdata[0]}<br>"
                "First Seen: %{customdata[1]}<extra></extra>"
            ),
        )
        fig_total.add_scatter(
            x=status_totals["Jira Status"],
            y=status_totals["Cumulative Minutes"],
            mode="lines+markers+text",
            text=status_totals["Cumulative Label"],
            textposition="top center",
            name="Cumulative SLA",
            yaxis="y2",
            hovertemplate="<b>%{x}</b><br>Cumulative SLA: %{text}<extra></extra>",
        )
        fig_total.update_layout(
            height=max(390, 170 + len(status_totals) * 55),
            margin=dict(l=10, r=70, t=30, b=20),
            xaxis_title="Jira Status",
            yaxis=dict(title="Status Duration (minutes)"),
            yaxis2=dict(
                title="Cumulative SLA (minutes)",
                overlaying="y",
                side="right",
                showgrid=False,
            ),
            legend_title="Metric",
            uniformtext_minsize=9,
            uniformtext_mode="hide",
        )
        st.plotly_chart(
            fig_total,
            width="stretch",
            key=f"sla_total_status_chart_top_v4_{ticket}",
        )

        # Compact duration table belongs immediately under the graph.
        # Include cumulative SLA so the user can see the total SLA at the end
        # of each Jira-status phase.
        audit = status_totals[
            ["Jira Status", "Duration", "Total SLA at Time", "First Seen"]
        ].copy()
        audit["First Seen"] = audit["First Seen"].apply(format_human_datetime)
        st.dataframe(
            audit,
            width="stretch",
            hide_index=True,
            column_config={
                "Jira Status": st.column_config.TextColumn("Jira Status", width="large"),
                "Duration": st.column_config.TextColumn("Duration", width="medium"),
                "Total SLA at Time": st.column_config.TextColumn(
                    "Total SLA at Time",
                    help="Cumulative SLA elapsed from the first recorded transition through this status.",
                    width="medium",
                ),
                "First Seen": st.column_config.TextColumn("First Seen", width="large"),
            },
        )
    else:
        st.info(f"No transition history is available for {ticket}.")

    # ------------------------------------------------------------
    # 3. CURRENT JIRA SNAPSHOT CARDS
    # ------------------------------------------------------------
    st.markdown("### Current Jira Snapshot")

    # st.metric may ellipsize long values such as full owner names. Render
    # these as explicit HTML cards so names/statuses wrap instead of being cut.
    snapshot_items = [
        ("Priority", current_priority or "—"),
        ("Current Status", current_status or "Unknown"),
        ("Current Owner", current_owner or "UNASSIGNED"),
        ("Current Role", current_role or "Unknown"),
        ("Current Stage", current_stage or "Unknown"),
        ("Current State", current_state or "UNKNOWN"),
        ("Total SLA", format_minutes(total_minutes)),
    ]
    snapshot_cols = st.columns(7)
    for col, (label, value) in zip(snapshot_cols, snapshot_items):
        safe_label = html.escape(str(label))
        safe_value = html.escape(str(value))
        col.markdown(
            f"""<div class=\"snapshot-card\">
                <div class=\"snapshot-card-label\">{safe_label}</div>
                <div class=\"snapshot-card-value\">{safe_value}</div>
            </div>""",
            unsafe_allow_html=True,
        )

    # ------------------------------------------------------------
    # 4. DETAILED TRANSITION AUDIT — SECONDARY / ELABORATE VIEW
    # ------------------------------------------------------------
    st.markdown("### Detailed Transition Audit")
    st.caption(
        "Complete Jira ownership and status history. Reporter is kept separate from DEV/L3 "
        "ownership and is shown explicitly whenever the reporter held the SLA interval."
    )
    display_columns = [
        "Role", "Stage", "Owner", "Reporter", "State", "Waiting Type",
        "Start", "End", "Duration", "assigned_by", "status",
    ]
    available = [col for col in display_columns if col in timeline.columns]
    detail = timeline[available].copy()
    for col in ["Start", "End"]:
        if col in detail.columns:
            detail[col] = detail[col].apply(format_human_datetime)
    detail_config = {}
    for col_name in detail.columns:
        if col_name in {"Owner", "Reporter", "assigned_by", "Stage", "State", "Waiting Type", "status"}:
            detail_config[col_name] = st.column_config.TextColumn(col_name, width="large")
        elif col_name in {"Start", "End"}:
            detail_config[col_name] = st.column_config.TextColumn(col_name, width="large")
        elif col_name == "Duration":
            detail_config[col_name] = st.column_config.TextColumn(col_name, width="medium")

    st.dataframe(
        detail,
        width="stretch",
        hide_index=True,
        column_config=detail_config,
    )

    # ------------------------------------------------------------
    # 5. AI ANALYSIS — OPTIONAL / COLLAPSED
    # ------------------------------------------------------------
    with st.expander("🤖 AI SLA Analysis", expanded=False):
        st.caption(
            "The risk score is calculated locally from Jira SLA data. AI is used only "
            "to explain the risk and suggest operational actions."
        )
        if st.button(
            "Analyze SLA Health with AI",
            key=f"ai_sla_health_{ticket}",
            type="primary",
        ):
            with st.spinner("Analyzing SLA transition health…"):
                ai_result, ai_error = generate_ai_sla_health(health_snapshot)
            if ai_error:
                st.warning(ai_error)
            elif ai_result:
                st.markdown(
                    f"**{ai_result.get('health', health_snapshot.get('health'))}** — "
                    f"{ai_result.get('summary', '')}"
                )
                if ai_result.get("risks"):
                    st.markdown("**Key risks**")
                    for item in ai_result["risks"]:
                        st.markdown(f"- {item}")
                if ai_result.get("recommended_actions"):
                    st.markdown("**Recommended actions**")
                    for item in ai_result["recommended_actions"]:
                        st.markdown(f"- {item}")

    # ------------------------------------------------------------
    # 6. CHRONOLOGICAL SLA OWNERSHIP TIMELINE — DETAILED VISUAL
    # ------------------------------------------------------------
    if not chart_df.empty:
        # IMPORTANT: the Y axis is a SEQUENCE of SLA intervals, not a unique
        # person list.  A Jira can move Aakash -> Reporter -> UNASSIGNED ->
        # Kartikay, and the graph must preserve exactly that chronology.
        graph = chart_df.copy().sort_values(
            ["Start", "End"], na_position="last"
        ).reset_index(drop=True)

        graph["Stage"] = graph.get("Stage", "").fillna("Other").astype(str)
        graph["Owner"] = graph["Owner"].fillna("UNASSIGNED").astype(str).str.strip()
        graph["Role"] = graph["Role"].fillna("").astype(str).str.strip()
        graph["Jira Status"] = (
            graph["Jira Status"]
            .fillna("Unknown")
            .astype(str)
            .str.strip()
            .replace({"": "Unknown", "nan": "Unknown", "None": "Unknown"})
        )
        graph["Duration Minutes"] = pd.to_numeric(
            graph["Duration Minutes"], errors="coerce"
        ).fillna(0.0)
        graph["Duration Label"] = graph["Duration Minutes"].apply(
            lambda x: format_minutes(float(x))
        )

        # Consecutive rows belonging to the same owner/role/status are one
        # ownership phase. This removes duplicate split rows such as the two
        # consecutive Aakash L3 intervals while retaining real hand-offs.
        phase_rows = []
        for _, row in graph.iterrows():
            if pd.isna(row["Start"]) or pd.isna(row["End"]):
                continue

            key = (row["Owner"], row["Role"], row["Jira Status"])
            if phase_rows:
                prev = phase_rows[-1]
                same_phase = prev["_key"] == key
                contiguous = (
                    pd.notna(prev["End"])
                    and abs((row["Start"] - prev["End"]).total_seconds()) <= 1.0
                )
                if same_phase and contiguous:
                    prev["End"] = max(prev["End"], row["End"])
                    prev["Duration Minutes"] += float(row["Duration Minutes"])
                    prev["Duration Label"] = format_minutes(prev["Duration Minutes"])
                    continue

            item = row.to_dict()
            item["_key"] = key
            phase_rows.append(item)

        graph = pd.DataFrame(phase_rows)

        if not graph.empty:
            # Zero-second assignment records are transition events, not SLA
            # intervals. They stay in the detailed audit table but should not
            # create a misleading 1-pixel owner row in the SLA duration graph.
            graph = graph[graph["Duration Minutes"] > 0].copy()
            graph = graph.sort_values("Start").reset_index(drop=True)

        if not graph.empty:
            # Give EVERY phase a unique category. Using only Owner/Role makes
            # Plotly collapse repeated owners and destroys chronology.
            graph["Sequence"] = range(1, len(graph) + 1)
            graph["Timeline Label"] = graph.apply(
                lambda r: (
                    f"{int(r['Sequence']):02d}  {r['Owner']} • "
                    f"{r['Role'] or 'Unassigned'}"
                ),
                axis=1,
            )
            graph["Bar Label"] = graph.apply(
                lambda r: f"{r['Jira Status']} • {r['Duration Label']}",
                axis=1,
            )

            # Explicit chronological order: first interval is at the top,
            # next hand-off below it, and so on.
            category_order = graph["Timeline Label"].tolist()

            st.markdown("### Chronological SLA Transition")
            st.caption(
                "Each bar is one real SLA ownership phase, ordered by its start time. "
                "Zero-second assignment events remain in the audit table but are excluded "
                "from the duration bars."
            )

            fig_timeline = px.timeline(
                graph,
                x_start="Start",
                x_end="End",
                y="Timeline Label",
                color="Jira Status",
                text="Bar Label",
                category_orders={"Timeline Label": category_order},
                hover_data={
                    "Role": True,
                    "Stage": True,
                    "Owner": True,
                    "Jira Status": True,
                    "State": True,
                    "Start": True,
                    "End": True,
                    "Duration Label": True,
                    "Timeline Label": False,
                    "Sequence": True,
                },
            )
            fig_timeline.update_traces(
                textposition="inside",
                insidetextanchor="middle",
                cliponaxis=False,
                textfont=dict(size=12),
            )
            fig_timeline.update_yaxes(
                categoryorder="array",
                categoryarray=category_order[::-1],
                title="Chronological Owner / Role",
            )
            fig_timeline.update_xaxes(title="Chronological Time")
            fig_timeline.update_layout(
                height=max(360, 125 + len(graph) * 82),
                margin=dict(l=10, r=10, t=30, b=20),
                legend_title="Jira Status",
                hoverlabel=dict(align="left"),
                uniformtext_minsize=9,
                uniformtext_mode="hide",
            )
            st.plotly_chart(
                fig_timeline,
                width="stretch",
                key=f"sla_timeline_chart_v3_{ticket}",
            )

            # Small hand-off summary makes the expected flow explicit and
            # prevents the visual from being interpreted as a simple owner
            # aggregation.
            handoff = graph[[
                "Sequence", "Owner", "Role", "Jira Status",
                "Duration Label", "Start", "End"
            ]].copy()
            handoff["Phase"] = handoff["Sequence"].apply(lambda x: f"{int(x):02d}")
            handoff = handoff[[
                "Phase", "Owner", "Role", "Jira Status",
                "Duration Label", "Start", "End"
            ]]
            handoff = handoff.rename(columns={"Duration Label": "Duration"})
            handoff["Start"] = handoff["Start"].apply(format_human_datetime)
            handoff["End"] = handoff["End"].apply(format_human_datetime)
            st.dataframe(handoff, width="stretch", hide_index=True)

    # Explicit current-state note makes the unassigned waiting period obvious.
    if (
        current_owner.upper() == "UNASSIGNED"
        and normalize_status(current_status) in {
            "triaged", "ready for dev", "ready for development"
        }
    ):
        st.info(
            f"{ticket} is currently **UNASSIGNED** and in **{current_status}**. "
            "The historical Jira status graph remains unchanged; only the current "
            "ownership state is UNASSIGNED."
        )

    # Keep the user on the detail page until they explicitly choose Overview.
    st.stop()


# Jira statuses/resolutions excluded from the Active Jira view.
FINAL_STATUSES = {
    "done",
    "deployed",
    "won't do",
    "won’t do",
    "wont do",
    "closed",
    "resolved",
}

# Issue types excluded from the Active Jira queue.
EXCLUDED_ACTIVE_ISSUE_TYPES = {"ltpm", "story"}


def normalize_issue_type(value):
    return (
        str(value or "")
        .strip()
        .lower()
        .replace("_", " ")
        .replace("-", " ")
    )


def is_excluded_active_issue_type(value):
    return normalize_issue_type(value) in EXCLUDED_ACTIVE_ISSUE_TYPES


def is_final_status(value):
    return normalize_status(value) in {
        normalize_status(v) for v in FINAL_STATUSES
    }


# Jira statuses where the ball is with the customer.  Time spent here PAUSES
# the SLA clock: it is excluded from Total SLA, breach and risk everywhere.
CUSTOMER_WAIT_MARKERS = (
    "awaiting customer",
    "waiting for customer",
    "waiting on customer",
    "customer response",
    "pending customer",
    "customer wait",
)

# Jira statuses where the ticket is parked with the team purely to watch
# something play out (e.g. re-running/observing automation executions) —
# there is no outstanding action, so like customer wait, time spent here
# PAUSES the SLA clock and the ticket is never surfaced as an active breach.
OBSERVATION_MARKERS = (
    "under observation",
)

# Union of every "hold" status — the SLA clock is paused and the ticket is
# never displayed as BREACHED while it is in one of these statuses.
SLA_HOLD_MARKERS = CUSTOMER_WAIT_MARKERS + OBSERVATION_MARKERS


def is_customer_wait_status(value):
    status = normalize_status(value)
    return any(marker in status for marker in CUSTOMER_WAIT_MARKERS)


def is_observation_status(value):
    status = normalize_status(value)
    return any(marker in status for marker in OBSERVATION_MARKERS)


def is_sla_hold_status(value):
    """True when no one owes an SLA-clock action right now — ball is with
    the customer, or the ticket is parked for observation."""
    status = normalize_status(value)
    return any(marker in status for marker in SLA_HOLD_MARKERS)


def current_transition_rows(transitions, reporter_map=None):
    """Return one latest transition row per Jira."""
    if transitions.empty:
        return pd.DataFrame(
            columns=[
                "ticket",
                "current_owner",
                "current_state",
                "current_role",
                "transition_status",
            ]
        )

    rows = []

    data = transitions.copy()
    data["sort_date"] = data["assigned_at"].apply(safe_datetime)

    for ticket, ticket_df in data.groupby("ticket", dropna=True):
        ticket_df = ticket_df.sort_values(
            "sort_date",
            na_position="first",
        )
        last = ticket_df.iloc[-1]

        owner = last.get("assigned_to") or "UNASSIGNED"
        ticket_key = str(ticket).strip().upper()
        reporter = str((reporter_map or {}).get(ticket_key, "") or "").strip()
        role = (
            canonical_person_role(
                owner,
                existing_roles=[last.get("role", "")],
                reporter=reporter,
            )
            or str(last.get("role") or "").upper()
        )

        rows.append(
            {
                "ticket": str(ticket),
                "current_owner": owner,
                "current_state": last.get("state") or "UNKNOWN",
                "current_role": role,
                "transition_status": last.get("status") or "",
            }
        )

    return pd.DataFrame(rows)


def classify_active_ticket(status, state, current_owner=None):
    """Classify the current active Jira into the dashboard queues."""
    status_n = normalize_status(status)
    state_n = str(state or "").upper()
    owner_n = str(current_owner or "UNASSIGNED").strip().upper()

    # Pending RCA has priority over the generic unassigned bucket.
    if state_n == "L3_WAITING":
        return "Pending RCA"

    if "pending rca" in status_n or "rca pending" in status_n:
        return "Pending RCA"

    if state_n == "L3_ASSIGNED":
        return "Triaged"

    if "triag" in status_n:
        return "Triaged"

    if owner_n in {"", "UNASSIGNED", "NONE", "NAN"}:
        return "Unassigned"

    if state_n == "DEV_WAITING":
        return "Ready for Dev"

    if state_n == "DEV_ASSIGNED":
        return "In Progress"

    if "ready for dev" in status_n or "ready for development" in status_n:
        return "Ready for Dev"

    if "in progress" in status_n or "development" in status_n:
        return "In Progress"

    return "Other Active"



def refresh_current_jira_fields(df, progress=None):
    """
    Fetch authoritative CURRENT Jira values.

    Sources:
      reporter       -> Jira fields.reporter
      assignee       -> Jira fields.assignee
      status         -> Jira fields.status
      Bug Severity   -> Jira custom field named "Bug Severity"
      Issue Urgency  -> Jira custom field named "Issue Urgency"
      Issue Impact   -> Jira custom field named "Issue Impact"

    Bug Severity is the dashboard Priority for this SLA dashboard.  Jira's
    standard Priority field is only a fallback.
    """
    if df.empty or "ticket" not in df.columns:
        return df

    import os

    # .env is loaded at startup, but read the values here as well so this
    # function remains safe when imported/tested independently.
    if load_dotenv is not None:
        load_dotenv(ROOT / ".env", override=False)
    else:
        _load_env_fallback(ROOT / ".env")

    base_url = os.getenv("JIRA_BASE_URL", "").strip().rstrip("/")
    jira_user = os.getenv("JIRA_USER", "").strip()
    jira_token = os.getenv("JIRA_TOKEN", "").strip()

    import sys
    _t_start = time.perf_counter()

    def log(msg):
        line = f"[jira-refresh +{time.perf_counter() - _t_start:6.1f}s] {msg}"
        print(line, file=sys.stderr, flush=True)
        if progress is not None:
            try:
                progress(msg)
            except Exception:
                pass

    log(f"start: {len(df)} rows, base_url={'set' if base_url else 'MISSING'}, creds={'set' if (jira_user and jira_token) else 'MISSING'}")

    result = df.copy()
    for column, default in [
        ("assignee", ""),
        ("reporter", ""),
        ("reporter_aliases", ""),
        ("assignee_verified", False),
        ("jira_verified", False),
        ("priority", ""),
        ("bug_severity", ""),
        ("issue_urgency", ""),
        ("issue_impact", ""),
        ("status", ""),
        ("resolution", ""),
        ("issue_type", ""),
        ("created", ""),
        ("updated", ""),
    ]:
        if column not in result.columns:
            result[column] = default

    if not base_url or not jira_user or not jira_token:
        # Do NOT silently pretend that cached data is live Jira data.
        result["jira_refresh_error"] = (
            "Jira credentials unavailable. Expected JIRA_BASE_URL, "
            "JIRA_USER and JIRA_TOKEN in .env or environment."
        )
        return result

    session = requests.Session()
    session.auth = (jira_user, jira_token)
    session.headers.update({
        "Accept": "application/json",
        "Content-Type": "application/json",
    })

    def clean(value):
        if value is None:
            return ""
        if isinstance(value, dict):
            # Prefer human-readable Jira values over IDs.
            for key in (
                "value", "displayName", "name", "label", "text", "string",
                "emailAddress", "id", "accountId",
            ):
                candidate = value.get(key)
                if candidate not in (None, ""):
                    if isinstance(candidate, (dict, list)):
                        nested = clean(candidate)
                        if nested:
                            return nested
                    else:
                        return str(candidate).strip()
            for key in ("option", "selected", "fieldValue", "content"):
                candidate = value.get(key)
                if candidate not in (None, ""):
                    nested = clean(candidate)
                    if nested:
                        return nested
            return ""
        if isinstance(value, list):
            return ", ".join(v for v in (clean(x) for x in value) if v)
        return str(value).strip()

    def person_aliases(value):
        if not isinstance(value, dict):
            text = clean(value)
            return [text] if text else []
        aliases = []
        for key in ("displayName", "name", "accountId", "emailAddress", "key"):
            val = str(value.get(key) or "").strip()
            if val and val not in aliases:
                aliases.append(val)
        return aliases

    def norm_field_name(value):
        return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())

    def normalize_severity(value):
        text = clean(value)
        match = re.search(
            r"\bsev(?:erity)?\s*[-_ ]?([1-4])\b",
            text,
            re.I,
        )
        return f"Sev {match.group(1)}" if match else ""

    def value_contains_severity(value):
        # Recursive fallback for custom fields returned as nested Jira option
        # objects rather than a simple string.
        if isinstance(value, dict):
            for child in value.values():
                found = normalize_severity(child)
                if found:
                    return found
                found = value_contains_severity(child)
                if found:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = value_contains_severity(child)
                if found:
                    return found
        else:
            return normalize_severity(value)
        return ""

    def jira_get(path, params=None):
        last_error = None
        # v3 is preferred; v2 is retained for Server/DC compatibility.
        for version in ("3", "2"):
            try:
                response = session.get(
                    f"{base_url}/rest/api/{version}/{path.lstrip('/')}",
                    params=params,
                    timeout=30,
                )
                response.raise_for_status()
                return response.json()
            except Exception as exc:
                last_error = exc
        raise last_error or RuntimeError("Jira request failed")

    # Resolve field IDs once.  Jira custom field IDs are installation-specific,
    # so hard-coding a customfield_xxxxx is intentionally avoided.
    field_map = {}
    try:
        log("fetching /field metadata")
        metadata = jira_get("field")
        log("field metadata ok")
        items = metadata if isinstance(metadata, list) else metadata.get("values", [])
        for item in items:
            fid = str(item.get("id") or "").strip()
            fname = str(item.get("name") or "").strip()
            if fid and fname:
                field_map[norm_field_name(fname)] = fid
    except Exception:
        field_map = {}

    wanted_ids = {
        "bugseverity": field_map.get("bugseverity", ""),
        "issueurgency": field_map.get("issueurgency", ""),
        "issueimpact": field_map.get("issueimpact", ""),
    }

    def apply_issue(index, issue):
        """Write the live Jira values from one issue payload into `result`."""
        if True:
            fields = issue.get("fields") or {}
            names_map = issue.get("names") or {}

            status_obj = fields.get("status") or {}
            resolution_obj = fields.get("resolution") or {}
            issue_type_obj = fields.get("issuetype") or {}
            assignee_obj = fields.get("assignee") or {}
            reporter_obj = fields.get("reporter") or {}
            priority_obj = fields.get("priority") or {}

            # Some Jira responses expose names only in the issue-level map;
            # combine it with the global /field metadata.
            id_to_name = dict(field_map)
            for fid, fname in names_map.items():
                id_to_name[norm_field_name(fname)] = str(fid).strip()

            def named_value(name):
                fid = id_to_name.get(norm_field_name(name), "")
                if fid:
                    val = clean(fields.get(fid))
                    if val:
                        return val
                return ""

            bug_severity = named_value("Bug Severity")
            issue_urgency = named_value("Issue Urgency")
            issue_impact = named_value("Issue Impact")

            # Direct ID fallback if the field map was available.
            if not bug_severity and wanted_ids["bugseverity"]:
                bug_severity = clean(fields.get(wanted_ids["bugseverity"]))
            if not issue_urgency and wanted_ids["issueurgency"]:
                issue_urgency = clean(fields.get(wanted_ids["issueurgency"]))
            if not issue_impact and wanted_ids["issueimpact"]:
                issue_impact = clean(fields.get(wanted_ids["issueimpact"]))

            # Last-resort severity discovery. Sev 1..4 is distinctive enough to
            # safely identify Bug Severity even when Jira omits field metadata.
            normalized_bug_severity = normalize_severity(bug_severity)
            if not normalized_bug_severity:
                for fid, raw_value in fields.items():
                    if str(fid).lower() in {
                        "priority", "status", "resolution", "issuetype",
                    }:
                        continue
                    found = value_contains_severity(raw_value)
                    if found:
                        normalized_bug_severity = found
                        break

            standard_priority = clean(
                priority_obj.get("name")
                or priority_obj.get("value")
                or priority_obj.get("id")
            )
            live_priority = normalized_bug_severity or standard_priority

            aliases = person_aliases(reporter_obj)
            live_reporter = (
                str(reporter_obj.get("displayName") or "").strip()
                or str(reporter_obj.get("name") or "").strip()
                or str(reporter_obj.get("accountId") or "").strip()
                or str(reporter_obj.get("emailAddress") or "").strip()
            )
            live_assignee = (
                str(assignee_obj.get("displayName") or "").strip()
                or str(assignee_obj.get("name") or "").strip()
                or str(assignee_obj.get("accountId") or "").strip()
            )

            result.at[index, "status"] = clean(status_obj.get("name"))
            result.at[index, "resolution"] = clean(resolution_obj.get("name"))
            result.at[index, "issue_type"] = clean(issue_type_obj.get("name"))
            result.at[index, "assignee"] = live_assignee
            result.at[index, "reporter"] = live_reporter
            result.at[index, "reporter_aliases"] = " | ".join(aliases)
            result.at[index, "assignee_verified"] = True
            result.at[index, "jira_verified"] = True
            result.at[index, "priority"] = live_priority
            result.at[index, "bug_severity"] = normalized_bug_severity
            result.at[index, "issue_urgency"] = clean(issue_urgency)
            result.at[index, "issue_impact"] = clean(issue_impact)
            result.at[index, "created"] = clean(fields.get("created"))
            result.at[index, "updated"] = clean(fields.get("updated"))
            result.at[index, "jira_refresh_error"] = ""

    # ------------------------------------------------------------------
    # Fetch strategy
    #
    # The home dashboard passes ~2k+ tickets.  One GET /issue/{key} per ticket
    # (each with a 30s timeout and a v3->v2 retry) took tens of minutes and
    # left the overview page blank while the script was still "running".
    #
    # Fix: pull issues in bulk with JQL `key in (...)` (100 keys per request,
    # ~25 requests for 2.3k tickets) and only fall back to per-issue GET for
    # keys the search did not return.  The per-ticket detail page still goes
    # through the same function, so it behaves exactly as before for 1 key.
    # ------------------------------------------------------------------
    key_to_index = {}
    for index, row in result.iterrows():
        key = extract_jira_key(row.get("ticket", ""))
        if key:
            key_to_index.setdefault(key, index)

    fetched = {}

    def jira_search_batch(keys):
        """Return {KEY: issue} for a batch of keys, trying the search endpoints in order."""
        jql = "key in (" + ",".join(f'"{k}"' for k in keys) + ")"
        body_v3 = {
            "jql": jql,
            "maxResults": len(keys),
            "fields": ["*all"],
            "expand": "names",
        }
        body_v2 = dict(body_v3, expand=["names"])
        attempts = [
            ("POST", "3/search/jql", body_v3),        # Jira Cloud (current)
            ("POST", "3/search", body_v3),            # Jira Cloud (legacy)
            ("POST", "2/search", body_v2),            # Jira Server / DC
        ]
        last_error = None
        for method, path, payload in attempts:
            try:
                response = session.request(
                    method,
                    f"{base_url}/rest/api/{path}",
                    json=payload,
                    timeout=60,
                )
                response.raise_for_status()
                data = response.json()
                names_map = data.get("names") or {}
                out = {}
                for issue in data.get("issues") or []:
                    if names_map and not issue.get("names"):
                        issue["names"] = names_map
                    out[str(issue.get("key") or "").strip().upper()] = issue
                return out
            except Exception as exc:
                last_error = exc
        raise last_error or RuntimeError("Jira search failed")

    all_keys = list(key_to_index.keys())
    if len(all_keys) > 1:
        batch_size = 100
        n_batches = (len(all_keys) + batch_size - 1) // batch_size
        for b, start in enumerate(range(0, len(all_keys), batch_size), 1):
            batch = all_keys[start:start + batch_size]
            log(f"bulk search batch {b}/{n_batches} ({len(batch)} keys)")
            try:
                got = jira_search_batch(batch)
                fetched.update(got)
                log(f"batch {b} returned {len(got)} issues")
            except Exception as exc:
                log(f"batch {b} FAILED: {exc}")

    # Hard cap on per-issue fallbacks.  If the bulk search endpoint is not
    # reachable we surface that as a per-row error instead of silently making
    # thousands of sequential requests again.
    fallback_budget = 50 if len(all_keys) > 1 else len(all_keys)
    fallback_used = 0

    log(f"applying {len(fetched)} fetched issues; up to {fallback_budget} per-issue fallbacks")
    for key_upper, index in key_to_index.items():
        try:
            issue = fetched.get(key_upper)
            if issue is None:
                if fallback_used >= fallback_budget:
                    result.at[index, "jira_refresh_error"] = (
                        "Not returned by bulk JQL search and per-issue fallback budget exhausted"
                    )
                    continue
                fallback_used += 1
                issue = jira_get(
                    f"issue/{quote(key_upper, safe='')}",
                    params={"fields": "*all", "expand": "names"},
                )
            apply_issue(index, issue)
        except Exception as exc:
            result.at[index, "jira_refresh_error"] = str(exc)
            continue

    log("done")
    return result


OVERVIEW_REFRESH_TTL_SECONDS = 600


def refresh_current_jira_fields_cached(df, cache_key, _progress=None):
    """
    Session-scoped cache for the consolidated overview refresh.

    Implemented with st.session_state instead of @st.cache_data on purpose:
    the progress callback writes to Streamlit elements created outside this
    function, which @st.cache_data cannot replay on a cache hit
    (CacheReplayClosureError).  Keyed on the set of ticket keys; entries
    expire after OVERVIEW_REFRESH_TTL_SECONDS.  The Refresh button clears it.
    """
    store = st.session_state.setdefault("_overview_refresh_cache", {})
    entry = store.get(cache_key)
    now = time.time()
    if entry and (now - entry["at"]) < OVERVIEW_REFRESH_TTL_SECONDS:
        if _progress is not None:
            _progress(f"using cached Jira refresh from {int(now - entry['at'])}s ago")
        return entry["df"].copy()

    result = refresh_current_jira_fields(df, progress=_progress)

    # Keep the cache small: one entry per distinct ticket set, drop stale ones.
    for k in [k for k, v in store.items() if (now - v["at"]) >= OVERVIEW_REFRESH_TTL_SECONDS]:
        store.pop(k, None)
    store[cache_key] = {"at": now, "df": result.copy()}
    return result


@st.cache_data(ttl=REFRESH_INTERVAL, show_spinner=False)
def add_dashboard_sla_health(df, transitions):
    """
    Add deterministic Jira SLA Health + Risk Score to the overview dataset.

    IMPORTANT:
    - Health is calculated from the same SLA rules used by the ticket detail page.
    - AI is NOT called for every Jira on the dashboard. This keeps the overview
      fast, deterministic, and free of per-row API cost.
    - Reporter time is already excluded by build_sla_health_snapshot().
    """
    result = df.copy()
    result["Risk Score"] = 0
    result["Jira Health"] = "UNKNOWN"
    result["Risk Factor"] = "No SLA data"

    if result.empty:
        return result

    transition_map = {}
    if transitions is not None and not transitions.empty and "ticket" in transitions.columns:
        for key, rows in transitions.groupby("ticket", dropna=False):
            transition_map[str(key).strip().upper()] = rows.copy()

    for idx, row in result.iterrows():
        ticket_key = str(row.get("ticket", "")).strip().upper()
        timeline = transition_map.get(ticket_key, pd.DataFrame())

        # If no transition history exists, use the consolidated SLA total so
        # the dashboard can still provide a useful deterministic health state.
        if timeline.empty:
            total_minutes = float(pd.to_numeric(row.get("total_minutes", 0), errors="coerce") or 0)
            timeline = pd.DataFrame([{
                "status": row.get("status", "Unknown"),
                "duration_minutes": total_minutes,
            }])

        snapshot = build_sla_health_snapshot(
            timeline,
            row.get("status", "Unknown"),
            row.get("Priority", row.get("priority", "Unknown")),
            row.get("resolution", ""),
        )

        result.at[idx, "Risk Score"] = int(snapshot.get("risk_score", 0))
        result.at[idx, "Jira Health"] = snapshot.get("health", "UNKNOWN")
        result.at[idx, "Risk Factor"] = snapshot.get("risk_factor", "No SLA data")

    return result


def add_active_sla_details(df, transitions):
    """
    Add compact SLA details to the home-page Active Jira dataset.

    Priority comes from the live Jira snapshot when available.
    L3/DEV SLA values are derived from transition ownership intervals,
    with ticket_summary values used as a fallback.
    """
    result = df.copy()

    for column in ["priority", "l3_pickup_sla", "total_l3_time", "total_dev_time"]:
        if column not in result.columns:
            result[column] = ""

    if transitions is not None and not transitions.empty:
        work = transitions.copy()
        work["duration_minutes"] = pd.to_numeric(
            work.get("duration_minutes", 0),
            errors="coerce",
        ).fillna(0)
        work["role_norm"] = work.get("role", "").astype(str).str.upper()
        # REPORTER intervals are informational only and never contribute to L3/DEV SLA.
        work.loc[work["role_norm"].eq("REPORTER"), "duration_minutes"] = 0

        role_totals = (
            work.groupby(["ticket", "role_norm"], as_index=False)["duration_minutes"]
            .sum()
            .pivot(index="ticket", columns="role_norm", values="duration_minutes")
            .reset_index()
        )

        if not role_totals.empty:
            role_totals.columns.name = None
            if "L3" in role_totals.columns:
                l3_map = role_totals.set_index("ticket")["L3"]
                result["total_l3_time"] = result["ticket"].map(l3_map).fillna(
                    pd.to_numeric(result["total_l3_time"], errors="coerce")
                )
            if "DEV" in role_totals.columns:
                dev_map = role_totals.set_index("ticket")["DEV"]
                result["total_dev_time"] = result["ticket"].map(dev_map).fillna(
                    pd.to_numeric(result["total_dev_time"], errors="coerce")
                )

    result["total_l3_time"] = pd.to_numeric(
        result["total_l3_time"], errors="coerce"
    ).fillna(0)
    result["total_dev_time"] = pd.to_numeric(
        result["total_dev_time"], errors="coerce"
    ).fillna(0)

    # Total SLA is the complete transition ownership SLA, already calculated
    # by get_active_tickets. Keep it authoritative.
    result["L3 SLA"] = result["total_l3_time"].apply(format_minutes)
    result["DEV SLA"] = result["total_dev_time"].apply(format_minutes)
    result["Total SLA"] = pd.to_numeric(
        result.get("total_minutes", 0), errors="coerce"
    ).fillna(0).apply(format_minutes)

    result["Priority"] = (
        result["priority"]
        .fillna("")
        .astype(str)
        .str.strip()
        .replace({"": "—", "nan": "—", "None": "—"})
    )

    return result


def get_active_tickets(summary, transitions, start_date=None, end_date=None):
    """
    Return active Jira tickets for the selected date window.

    Active Jira is intentionally restricted to:
      - Pending RCA
      - Triaged
      - Ready for Dev
      - In Progress

    Final Jira statuses/resolutions are always excluded.
    """
    if summary.empty:
        return pd.DataFrame()

    df = summary.copy()

    # Refresh current Jira fields before deciding whether a ticket is active.
    # This fixes stale SQLite rows such as TE-26265 being stored as "Open"
    # while Jira currently shows "Won't Do".
    df = refresh_current_jira_fields(df)

    for col in ["ticket", "status", "resolution"]:
        if col not in df.columns:
            df[col] = ""

    df["ticket"] = df["ticket"].fillna("").astype(str).str.strip()
    df["status"] = df["status"].fillna("").astype(str).str.strip()
    df["resolution"] = df["resolution"].fillna("").astype(str).str.strip()

    df = df[df["ticket"] != ""].copy()
    df = df.drop_duplicates(subset=["ticket"], keep="last")

    # Resolution is authoritative when present. Status is also checked,
    # but the final current-state check below is repeated after merging
    # the latest transition record. This prevents a stale ticket_summary
    # status such as "Open" from keeping a Jira active when its latest
    # transition status is already "Won't Do".
    df = df[
        ~df["status"].apply(is_final_status)
        & ~df["resolution"].apply(is_final_status)
        ].copy()

    # Do not display LTPM or Story issue types in Active Jira.
    issue_type_column = next(
        (
            column for column in
            ["issue_type", "issuetype", "issueType", "type"]
            if column in df.columns
        ),
        None,
    )
    if issue_type_column:
        df = df[
            ~df[issue_type_column].apply(is_excluded_active_issue_type)
        ].copy()


    if df.empty:
        return df

    current = current_transition_rows(transitions)

    if not current.empty:
        df = df.merge(current, on="ticket", how="left")
    else:
        df["current_owner"] = "UNASSIGNED"
        df["current_state"] = "UNKNOWN"
        df["current_role"] = ""
        df["transition_status"] = ""

    # IMPORTANT: current_owner must match the Jira's actual current assignee.
    # Transition history is used for SLA history, but a released transition
    # row must not override a Jira that is currently assigned.
    if "assignee" in df.columns:
        live_assignee = (
            df["assignee"]
            .fillna("")
            .astype(str)
            .str.strip()
        )
        if "assignee_verified" in df.columns:
            verified = df["assignee_verified"].fillna(False).astype(bool)
            # Jira is authoritative when the issue was successfully returned,
            # including the case where assignee is empty (genuinely unassigned).
            df.loc[verified, "current_owner"] = live_assignee[verified].replace(
                {"": "UNASSIGNED"}
            )
        else:
            has_live_assignee = ~live_assignee.str.upper().isin(
                {"", "UNASSIGNED", "NONE", "NAN"}
            )
            df.loc[has_live_assignee, "current_owner"] = live_assignee[has_live_assignee]

    df["current_owner"] = (
        df["current_owner"]
        .fillna("UNASSIGNED")
        .replace({"": "UNASSIGNED", "nan": "UNASSIGNED", "None": "UNASSIGNED"})
    )
    df["current_role"] = df["current_role"].fillna("")

    # Reporter is an issue identity, not an SLA ownership role. If the reporter
    # ever appears as an assignee in historical/cached data, keep them labeled
    # REPORTER so their time is not classified as DEV or L3.
    if "reporter" in df.columns:
        df["current_role"] = df.apply(
            lambda r: canonical_person_role(
                r.get("current_owner"),
                reporter=r.get("reporter"),
            ) or str(r.get("current_role") or ""),
            axis=1,
        )

    # Recalculate the current state after replacing the historical owner
    # with Jira's live assignee. This prevents a released L3_WAITING/
    # UNASSIGNED transition row from overriding a Jira that is currently
    # assigned to a developer.
    transition_role_evidence = {}
    if not transitions.empty and "ticket" in transitions.columns:
        for ticket_key, ticket_rows in transitions.groupby("ticket", dropna=False):
            transition_role_evidence[str(ticket_key).strip().upper()] = ticket_rows

    def _live_current_state(row):
        ticket_key = str(row.get("ticket", "")).strip().upper()
        owner = str(row.get("current_owner") or "UNASSIGNED").strip()
        rows_for_ticket = transition_role_evidence.get(ticket_key)
        roles = []
        if rows_for_ticket is not None and not rows_for_ticket.empty and "assigned_to" in rows_for_ticket.columns and "role" in rows_for_ticket.columns:
            roles = rows_for_ticket.loc[
                rows_for_ticket["assigned_to"].fillna("").astype(str).str.strip().str.lower()
                == owner.lower(),
                "role",
            ].tolist()
        return current_state_for_status(
            owner,
            row.get("status", ""),
            fallback_state=row.get("current_state") or "UNKNOWN",
            existing_roles=roles,
            reporter=row.get("reporter", ""),
        )

    df["current_state"] = df.apply(_live_current_state, axis=1)

    # Final-state protection:
    # Jira 26265 was observed with a stale summary status of "Open" while
    # the Jira had already reached "Won't Do". The latest transition status
    # is checked here as well, so final Jira states cannot appear in Active.
    if "transition_status" in df.columns:
        df = df[
            ~df["transition_status"].apply(is_final_status)
        ].copy()

    if df.empty:
        return df

    df["Queue"] = df.apply(
        lambda r: classify_active_ticket(
            r.get("status"),
            r.get("current_state"),
            r.get("current_owner"),
        ),
        axis=1,
    )

    # Active Jira is intentionally restricted to the four
    # transition-SLA queues below. Open, Backlog, generic
    # Unassigned and Other Active tickets are not displayed.
    allowed_active_queues = {
        "Pending RCA",
        "Triaged",
        "Ready for Dev",
        "In Progress",
    }
    df = df[df["Queue"].isin(allowed_active_queues)].copy()

    if df.empty:
        return df

    # --------------------------------------------------------
    # Date used by the Active Jira time filter. Prefer the Jira
    # created date; fall back to the latest transition timestamp.
    # --------------------------------------------------------
    date_columns = [
        "created",
        "created_at",
        "updated",
        "updated_at",
        "last_updated",
    ]

    date_column = next(
        (c for c in date_columns if c in df.columns),
        None,
    )

    if date_column is not None:
        df["Active Date"] = df[date_column].apply(safe_datetime)
    else:
        transition_dates = pd.DataFrame()
        if not transitions.empty and "ticket" in transitions.columns:
            transition_dates = transitions.copy()
            transition_dates["_transition_date"] = transition_dates[
                "assigned_at"
            ].apply(safe_datetime)
            transition_dates = (
                transition_dates.groupby("ticket", as_index=False)["_transition_date"]
                .max()
            )
            df = df.merge(transition_dates, on="ticket", how="left")
            df["Active Date"] = df["_transition_date"]
            df.drop(columns=["_transition_date"], inplace=True, errors="ignore")
        else:
            df["Active Date"] = pd.NaT

    # Apply selected time window only when a usable date exists.
    if start_date is not None and end_date is not None:
        start_ts = pd.Timestamp(start_date)
        end_ts = pd.Timestamp(end_date)
        if start_ts.tzinfo is None:
            start_ts = start_ts.tz_localize("UTC")
        if end_ts.tzinfo is None:
            end_ts = end_ts.tz_localize("UTC")

        dated = df["Active Date"].notna()
        df = df[
            (~dated)
            | ((df["Active Date"] >= start_ts) & (df["Active Date"] < end_ts))
            ].copy()

    # Total accumulated transition time for this Jira.
    if not transitions.empty:
        totals = (
            transitions.groupby("ticket", as_index=False)["duration_minutes"]
            .sum()
            .rename(columns={"duration_minutes": "total_minutes"})
        )
        df = df.merge(totals, on="ticket", how="left")
    else:
        df["total_minutes"] = 0

    df["total_minutes"] = pd.to_numeric(
        df["total_minutes"],
        errors="coerce",
    ).fillna(0)

    df["Total SLA"] = df["total_minutes"].apply(format_minutes)

    # Most recently created/updated/active Jira first.
    # Queue is only the secondary grouping; never sort primarily by Jira key.
    return df.sort_values(
        ["Active Date", "Queue", "ticket"],
        ascending=[False, True, True],
        na_position="last",
    ).reset_index(drop=True)


def month_bounds(month_value):
    start = pd.Timestamp(
        year=month_value.year,
        month=month_value.month,
        day=1,
        tz="UTC",
    )

    if month_value.month == 12:
        end = pd.Timestamp(
            year=month_value.year + 1,
            month=1,
            day=1,
            tz="UTC",
        )
    else:
        end = pd.Timestamp(
            year=month_value.year,
            month=month_value.month + 1,
            day=1,
            tz="UTC",
        )

    return start, end


def person_month_data(transitions, person, role, month_value):
    """Return Jira history and daily workload for one person/month."""
    if transitions.empty or not person:
        return pd.DataFrame(), pd.DataFrame()

    start_month, end_month = month_bounds(month_value)
    now = pd.Timestamp.now(tz="UTC")

    df = transitions[
        (transitions["assigned_to"].astype(str) == str(person))
        & (transitions["role"].astype(str).str.upper() == role.upper())
        ].copy()

    if df.empty:
        return pd.DataFrame(), pd.DataFrame()

    df["Start"] = df["assigned_at"].apply(safe_datetime)
    df["End"] = df["released_at"].apply(safe_datetime).fillna(now)

    # Only intervals that overlap the selected month.
    month_df = df[
        df["Start"].notna()
        & df["End"].notna()
        & (df["Start"] < end_month)
        & (df["End"] >= start_month)
        ].copy()

    if month_df.empty:
        return pd.DataFrame(), pd.DataFrame()

    # Jira history for the month.
    history = (
        month_df.groupby("ticket", as_index=False)
        .agg(
            First_Assigned=("Start", "min"),
            Last_Released=("End", "max"),
            Transitions=("ticket", "size"),
            Total_Minutes=("duration_minutes", "sum"),
        )
        .sort_values("First_Assigned")
    )

    history["SLA"] = history["Total_Minutes"].apply(format_minutes)

    # Split each ownership interval across calendar days.
    daily_rows = []

    for _, row in month_df.iterrows():
        interval_start = max(row["Start"], start_month)
        interval_end = min(row["End"], end_month)

        if pd.isna(interval_start) or pd.isna(interval_end) or interval_end <= interval_start:
            continue

        day = interval_start.normalize()

        while day < interval_end:
            day_start = max(day, interval_start)
            day_end = min(day + pd.Timedelta(days=1), interval_end)
            hours = (day_end - day_start).total_seconds() / 3600.0

            if hours > 0:
                daily_rows.append(
                    {
                        "Date": day.date(),
                        "Jira": row["ticket"],
                        "Hours": hours,
                    }
                )

            day += pd.Timedelta(days=1)

    daily = pd.DataFrame(daily_rows)

    if not daily.empty:
        daily = (
            daily.groupby(["Date", "Jira"], as_index=False)["Hours"]
            .sum()
            .sort_values(["Date", "Jira"])
        )
        daily["Hours"] = daily["Hours"].round(2)

    return history, daily


# ============================================================
# SLA TARGET HELPERS — MUST BE DEFINED BEFORE ANY PAGE RENDER
# ============================================================
# These helpers are used by the ticket detail / AI SLA Health page.
# Keep them above the query-parameter render because Streamlit executes
# this module top-to-bottom on every rerun.
OVERALL_SLA_TARGET_HOURS = 72
PRIORITY_SLA_TARGET_HOURS = {
    "sev 1": 24,
    "sev 2": 48,
    "sev 3": 72,
    "sev 4": 72,
    "critical": 24,
    "highest": 24,
    "high": 48,
    "medium": 72,
    "low": 120,
}

def priority_target_hours(value):
    key = normalize_status(value)
    return float(PRIORITY_SLA_TARGET_HOURS.get(key, OVERALL_SLA_TARGET_HOURS))

def severity_bucket(value):
    """Bucket a Priority value into Sev 1-4 (or Other/Unknown) for the
    severity tabs shared by the Recent SLA Breaches and Jira SLA Health
    views."""
    match = re.search(r"\bsev(?:erity)?\s*[-_ ]?([1-4])\b", str(value or ""), re.I)
    return f"Sev {match.group(1)}" if match else "Other / Unknown"

def format_hours_value(hours):
    try:
        hours = float(hours or 0)
    except (TypeError, ValueError):
        hours = 0.0
    return format_minutes(hours * 60.0)


# ============================================================
# QUERY-PARAMETER NAVIGATION
# ============================================================

# Both the clickable Jira table links and the direct Jira lookup use
# ?ticket=<JIRA>. Render that ticket-specific page before the overview.
#
# This was missing previously: the URL changed successfully, but the
# application never consumed the "ticket" query parameter, so Streamlit
# simply rendered the home/overview page again.
requested_ticket = st.query_params.get("ticket", "")

if isinstance(requested_ticket, list):
    requested_ticket = requested_ticket[0] if requested_ticket else ""

requested_ticket = str(requested_ticket or "").strip().upper()

if requested_ticket:
    render_ticket_transition_page(
        requested_ticket,
        transitions,
        ticket_summary,
    )


# ============================================================
# ============================================================
# CONSOLIDATED SLA OVERVIEW
# ============================================================
# The Overview is intentionally an executive/operational dashboard.
# Jira-specific ownership/timeline data remains available by clicking a
# Jira key, which opens the existing ?ticket=<JIRA> detail page.

def _date_column(df):
    for column in [
        "created",
        "created_at",
        "updated",
        "updated_at",
        "last_updated",
    ]:
        if column in df.columns:
            return column
    return None


def ticket_dashboard_dates(summary, transitions):
    """
    One timestamp per Jira for date filtering on the overview.

    Preference order:
      1. ticket_summary.created (Jira created date) when it parses
      2. earliest transition assigned_at for the ticket (first time it was seen)
    """
    dates = pd.Series(pd.NaT, index=summary.index, dtype="datetime64[ns, UTC]")
    date_col = _date_column(summary)
    if date_col:
        dates = pd.to_datetime(summary[date_col], errors="coerce", utc=True)

    if transitions is not None and not transitions.empty and "assigned_at" in transitions.columns:
        first_seen = (
            transitions.assign(
                _key=transitions["ticket"].astype(str).str.strip().str.upper(),
                _at=pd.to_datetime(transitions["assigned_at"], errors="coerce", utc=True),
            )
            .dropna(subset=["_at"])
            .groupby("_key")["_at"].min()
        )
        keys = summary["ticket"].astype(str).str.strip().str.upper()
        fallback = keys.map(first_seen)
        dates = dates.fillna(fallback)

    return dates


@st.cache_data(ttl=REFRESH_INTERVAL, show_spinner=False)
def build_consolidated_dataset(summary, transitions, start=None, end=None, live_jira=False, _progress=None):
    """
    Build one row per Jira for the consolidated overview.

    `start`/`end` (tz-aware UTC) restrict the dataset to Jira created (or first
    seen) inside the window BEFORE any live Jira refresh, so the overview only
    fetches the tickets it will actually display.  Tickets with no usable date
    are excluded from a windowed overview.

    Cached on (summary, transitions, start, end, live_jira) content — NOT on
    `_progress`, which is a UI callback whose identity changes every rerun
    and would otherwise defeat caching (leading underscore tells
    st.cache_data to skip hashing it). `live_jira` is passed explicitly
    instead of read from st.session_state inside the function, so toggling
    it correctly busts the cache instead of silently returning a stale
    result.
    """
    if summary.empty:
        return pd.DataFrame()

    summary = summary.copy()
    summary["Dashboard Date"] = ticket_dashboard_dates(summary, transitions)
    if start is not None or end is not None:
        mask = summary["Dashboard Date"].notna()
        if start is not None:
            mask &= summary["Dashboard Date"] >= start
        if end is not None:
            mask &= summary["Dashboard Date"] < end
        summary = summary[mask].copy()
        if _progress is not None:
            _progress(f"{len(summary):,} Jira in selected date window")
        if summary.empty:
            return pd.DataFrame()

    if live_jira:
        ticket_keys = tuple(sorted(
            str(k).strip().upper() for k in summary.get("ticket", pd.Series(dtype=str)).dropna()
        ))
        df = refresh_current_jira_fields_cached(summary.copy(), ticket_keys, _progress=_progress)
        if _progress is not None:
            _progress("Jira refresh done; consolidating dataset")
    else:
        # Overview uses the values already in sla_dashboard.db (same as AI_v2).
        # The ticket detail page still does a live Jira lookup for one issue.
        df = summary.copy()
        if _progress is not None:
            _progress("live Jira refresh OFF for overview; using DB values")

    if "ticket" not in df.columns:
        return pd.DataFrame()

    for column, default in [
        ("status", ""),
        ("resolution", ""),
        ("priority", ""),
        ("issue_type", ""),
        ("assignee", ""),
    ]:
        if column not in df.columns:
            df[column] = default

    df["ticket"] = df["ticket"].fillna("").astype(str).str.strip()
    df = df[df["ticket"] != ""].drop_duplicates("ticket", keep="last").copy()

    # Current transition state is useful as a fallback, but Jira's live
    # assignee/status/priority values remain authoritative when available.
    reporter_map = {}
    if "reporter" in df.columns:
        reporter_map = {
            str(row.get("ticket") or "").strip().upper(): str(row.get("reporter") or "").strip()
            for _, row in df.iterrows()
            if str(row.get("ticket") or "").strip()
        }

    current = current_transition_rows(transitions, reporter_map=reporter_map)
    if not current.empty:
        df = df.merge(current, on="ticket", how="left")
    else:
        df["current_owner"] = "UNASSIGNED"
        df["current_state"] = "UNKNOWN"
        df["current_role"] = ""
        df["transition_status"] = ""

    df["current_owner"] = df["current_owner"].fillna("UNASSIGNED").astype(str).str.strip()

    # The Jira reporter is issue metadata, not an SLA owner. Re-resolve the
    # current role after applying the live assignee so a stale historical DEV
    # role cannot classify the reporter as DEV.
    if "reporter" in df.columns:
        df["current_role"] = df.apply(
            lambda row: canonical_person_role(
                row.get("current_owner"),
                existing_roles=[row.get("current_role", "")],
                reporter=row.get("reporter"),
            ) or ("" if pd.isna(row.get("current_role")) else str(row.get("current_role"))),
            axis=1,
        )

    live_assignee = df["assignee"].fillna("").astype(str).str.strip()
    if "assignee_verified" in df.columns:
        verified = df["assignee_verified"].fillna(False).astype(bool)
        df.loc[verified, "current_owner"] = live_assignee[verified].replace({"": "UNASSIGNED"})
    else:
        has_assignee = ~live_assignee.str.upper().isin({"", "UNASSIGNED", "NONE", "NAN"})
        df.loc[has_assignee, "current_owner"] = live_assignee[has_assignee]

    # Re-resolve role AFTER applying Jira's live assignee. This ordering is
    # important: the transition snapshot may say DEV for an old owner while
    # Jira currently has a different developer assigned.
    if "reporter" in df.columns:
        df["current_role"] = df.apply(
            lambda row: canonical_person_role(
                row.get("current_owner"),
                existing_roles=[row.get("current_role", "")],
                reporter=row.get("reporter"),
            ) or ("" if pd.isna(row.get("current_role")) else str(row.get("current_role"))),
            axis=1,
        )

    # Safety net: collapse any stray NaN/"nan"/"None" text left over from a
    # merge into a clean blank, so the Role column never prints "nan".
    df["current_role"] = (
        df["current_role"].astype(str).str.strip()
        .replace({"nan": "", "None": "", "NaN": "", "<NA>": ""})
    )

    # Resolve current state from the live owner/status.
    evidence = {}
    if not transitions.empty:
        for key, rows in transitions.groupby("ticket", dropna=False):
            evidence[str(key).strip().upper()] = rows

    def live_state(row):
        key = str(row.get("ticket", "")).strip().upper()
        owner = str(row.get("current_owner") or "UNASSIGNED").strip()
        rows = evidence.get(key)
        roles = []
        if rows is not None and not rows.empty:
            roles = rows.loc[
                rows["assigned_to"].fillna("").astype(str).str.strip().str.lower() == owner.lower(),
                "role",
            ].tolist()
        return current_state_for_status(
            owner,
            row.get("status", ""),
            fallback_state=row.get("current_state") or "UNKNOWN",
            existing_roles=roles,
            reporter=row.get("reporter", ""),
        )

    df["current_state"] = df.apply(live_state, axis=1)
    df["Queue"] = df.apply(
        lambda row: classify_active_ticket(
            row.get("status"),
            row.get("current_state"),
            row.get("current_owner"),
        ),
        axis=1,
    )

    # Transition totals: total, L3 and DEV ownership time per Jira.
    if not transitions.empty:
        work = transitions.copy()
        work["duration_minutes"] = pd.to_numeric(
            work.get("duration_minutes", 0), errors="coerce"
        ).fillna(0)

        if reporter_map and "assigned_to" in work.columns:
            def _safe_role(row):
                owner = str(row.get("assigned_to") or "").strip()
                key = str(row.get("ticket") or "").strip().upper()
                reporter = str(reporter_map.get(key, "") or "").strip()
                return canonical_person_role(owner, existing_roles=[row.get("role", "")], reporter=reporter) or str(row.get("role") or "").upper()
            work["role"] = work.apply(_safe_role, axis=1)

        work.loc[work["role"].astype(str).str.upper().eq("REPORTER"), "duration_minutes"] = 0

        # Customer-wait / under-observation intervals pause the SLA clock.
        cw_mask = work["status"].apply(is_sla_hold_status)
        customer_wait = (
            work[cw_mask].groupby("ticket", as_index=False)["duration_minutes"].sum()
            .rename(columns={"duration_minutes": "customer_wait_minutes"})
        )
        work.loc[cw_mask, "duration_minutes"] = 0

        totals = work.groupby("ticket", as_index=False)["duration_minutes"].sum()
        totals = totals.rename(columns={"duration_minutes": "total_minutes"})
        df = df.merge(totals, on="ticket", how="left")
        df = df.merge(customer_wait, on="ticket", how="left")

        role_totals = (
            work.assign(role_norm=work.get("role", "").astype(str).str.upper())
            .groupby(["ticket", "role_norm"], as_index=False)["duration_minutes"]
            .sum()
            .pivot(index="ticket", columns="role_norm", values="duration_minutes")
            .reset_index()
        )
        role_totals.columns.name = None
        if "L3" in role_totals.columns:
            df = df.merge(
                role_totals[["ticket", "L3"]].rename(columns={"L3": "l3_minutes"}),
                on="ticket", how="left"
            )
        else:
            df["l3_minutes"] = 0
        if "DEV" in role_totals.columns:
            df = df.merge(
                role_totals[["ticket", "DEV"]].rename(columns={"DEV": "dev_minutes"}),
                on="ticket", how="left"
            )
        else:
            df["dev_minutes"] = 0

        # Team is optional in the source DB. If it exists on transitions,
        # attach the latest non-empty value per Jira for the global filter.
        if "team" in work.columns:
            team_map = (
                work.assign(team_clean=work["team"].fillna("").astype(str).str.strip())
                .query("team_clean != ''")
                .groupby("ticket", as_index=False)["team_clean"]
                .last()
                .rename(columns={"team_clean": "team_from_transitions"})
            )
            if not team_map.empty:
                df = df.merge(team_map, on="ticket", how="left")
                if "team" not in df.columns:
                    df["team"] = df["team_from_transitions"]
                else:
                    df["team"] = df["team"].fillna(df["team_from_transitions"])
                df.drop(columns=["team_from_transitions"], inplace=True, errors="ignore")
    else:
        df["total_minutes"] = 0
        df["l3_minutes"] = 0
        df["dev_minutes"] = 0

    if "customer_wait_minutes" not in df.columns:
        df["customer_wait_minutes"] = 0
    for column in ["total_minutes", "l3_minutes", "dev_minutes", "customer_wait_minutes"]:
        df[column] = pd.to_numeric(df[column], errors="coerce").fillna(0)
    df["Customer Wait"] = df["customer_wait_minutes"].apply(format_minutes)
    df["SLA Paused"] = df["status"].apply(is_sla_hold_status)

    if "Dashboard Date" not in df.columns:
        df["Dashboard Date"] = ticket_dashboard_dates(df, transitions)

    df["Priority"] = (
        df["priority"].fillna("").astype(str).str.strip()
        .replace({"": "Unknown", "nan": "Unknown", "None": "Unknown"})
    )
    df["Target Hours"] = df["Priority"].apply(priority_target_hours)
    df["SLA Hours"] = df["total_minutes"] / 60.0
    final_mask = df["status"].apply(is_final_status) | df["resolution"].apply(is_final_status)
    df["Lifecycle"] = final_mask.map({True: "Completed", False: "Open"})

    # "Over target" is the historical fact (used for compliance %).
    # "SLA Breached" is the actionable flag: only OPEN Jira can be breached.
    # A Done / Closed / Won't Do Jira is never displayed as breached, and
    # neither is a Jira currently on hold (awaiting customer / under
    # observation) — the team owes no action while the ball is elsewhere.
    df["Over Target"] = df["SLA Hours"] > df["Target Hours"]
    df["SLA Breached"] = df["Over Target"] & ~final_mask & ~df["SLA Paused"]
    df["SLA Compliance"] = ~df["Over Target"]
    df["Breach Hours"] = (df["SLA Hours"] - df["Target Hours"]).clip(lower=0)
    df["Resolution Time"] = df["total_minutes"].apply(format_minutes)
    df["L3 Time"] = df["l3_minutes"].apply(format_minutes)
    df["DEV Time"] = df["dev_minutes"].apply(format_minutes)
    df["Breach Duration"] = df["Breach Hours"].apply(format_hours_value)

    # Recent SLA breach information.
    df["Breach Reason"] = df.apply(
        lambda row: (
            f"{row['Priority']} exceeded {row['Target Hours']:.0f}h target"
            if bool(row["SLA Breached"]) else ""
        ),
        axis=1,
    )

    return df


# ------------------------------------------------------------
# Header / actions
# ------------------------------------------------------------
header_left, header_right = st.columns([5.8, 1.8])
with header_left:
    st.markdown('<div class="app-title">Transition SLA Dashboard</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="app-subtitle">Track Jira transitions, ownership time & SLA performance</div>',
        unsafe_allow_html=True,
    )
with header_right:
    action_a, action_b = st.columns(2)
    with action_a:
        if st.button("↻ Refresh", width="stretch", key="overview_refresh"):
            st.cache_data.clear()  # force a fresh live Jira pull
            st.session_state.pop("_overview_refresh_cache", None)
            st.session_state.last_refresh = time.time()
            st.rerun()
    with action_b:
        export_placeholder = True

st.divider()

# ------------------------------------------------------------
# Direct Jira lookup — retained as a compact drill-down control.
# ------------------------------------------------------------
with st.expander("Open a specific Jira SLA", expanded=False):
    with st.form("jira_lookup_form_v2", clear_on_submit=False):
        jira_col, jira_button_col = st.columns([5, 1])
        with jira_col:
            global_ticket_search = st.text_input(
                "Jira key",
                placeholder="Enter Jira key, e.g. TE-25312",
                label_visibility="collapsed",
                key="global_ticket_search_v2",
            )
        with jira_button_col:
            open_jira = st.form_submit_button("Open SLA →", type="primary", width="stretch")
    if open_jira:
        ticket = global_ticket_search.strip().upper()
        if ticket:
            navigate_to_ticket(ticket)
        else:
            st.warning("Enter a Jira key first.")


# ------------------------------------------------------------
# Global filters — deliberately close to the reference dashboard.
# ------------------------------------------------------------
filter_cols = st.columns([1.45, 1.0, 1.0, 1.0, 1.15, 1.15])
with filter_cols[0]:
    overview_range = st.selectbox(
        "Date range",
        ["Last 30 Days", "Last 7 Days", "Last 3 Months", "Last 6 Months", "Last 1 Year", "Custom"],
        index=0,
        key="overview_range",
    )
with filter_cols[1]:
    priority_filter = st.selectbox("Priority", ["All", "Sev 1", "Sev 2", "Sev 3", "Sev 4", "Other"], key="overview_priority")
with filter_cols[2]:
    if not ticket_summary.empty and "project" in ticket_summary.columns:
        project_values = sorted({
            str(x).strip()
            for x in ticket_summary["project"].dropna()
            if str(x).strip()
        })
    else:
        project_values = []
    project_filter = st.selectbox("Project", ["All"] + project_values, key="overview_project")
with filter_cols[3]:
    team_values = sorted({str(x).strip() for x in transitions.get("team", pd.Series(dtype=str)).dropna() if str(x).strip()}) if not transitions.empty and "team" in transitions.columns else []
    team_filter = st.selectbox("Team", ["All"] + team_values, key="overview_team")
with filter_cols[4]:
    assignee_type = st.selectbox("Assignee Type", ["All", "L3", "Developer", "Unassigned"], key="overview_assignee_type")
with filter_cols[5]:
    status_filter = st.selectbox("Status", ["All", "Open", "Completed", "Pending RCA", "Triaged", "Ready for Dev", "In Progress"], key="overview_status")

custom_dates = None
if overview_range == "Custom":
    custom_dates = st.date_input(
        "Custom date range",
        value=(datetime.now().date() - pd.Timedelta(days=30), datetime.now().date()),
        key="overview_custom_dates",
    )

# Round to the minute (floor) instead of using full microsecond precision.
# build_consolidated_dataset() is cached on (summary, transitions, start,
# end, live_jira) — a raw pd.Timestamp.now() here changes on every single
# rerun (down to the microsecond), so the cache key never repeats and every
# rerun was still paying the full ~145s rebuild cost even after caching was
# added. Flooring to the minute means reruns within the same minute share
# an identical cache key and actually hit the cache.
now_utc = pd.Timestamp.now(tz="UTC").floor("min")
if overview_range == "Last 7 Days":
    overview_start = now_utc - pd.Timedelta(days=7)
elif overview_range == "Last 3 Months":
    overview_start = now_utc - pd.DateOffset(months=3)
elif overview_range == "Last 6 Months":
    overview_start = now_utc - pd.DateOffset(months=6)
elif overview_range == "Last 1 Year":
    overview_start = now_utc - pd.DateOffset(years=1)
elif overview_range == "Custom" and custom_dates and len(custom_dates) == 2:
    overview_start = pd.Timestamp(custom_dates[0], tz="UTC")
    now_utc = pd.Timestamp(custom_dates[1], tz="UTC") + pd.Timedelta(days=1)
else:
    overview_start = now_utc - pd.Timedelta(days=30)
overview_end = now_utc

_t0 = time.perf_counter()
_status = st.status("Loading overview…", expanded=True)
_status_line = _status.empty()

def _progress(msg):
    _status_line.write(f"{time.perf_counter() - _t0:5.1f}s — {msg}")

_stage("filters rendered; starting build_consolidated_dataset")
_progress(f"tickets in DB: {len(ticket_summary):,}, transitions: {len(transitions):,}")
overview = build_consolidated_dataset(
    ticket_summary, transitions, start=overview_start, end=overview_end,
    live_jira=st.session_state.get("overview_live_jira", False), _progress=_progress,
)
st.session_state["diag_build_seconds"] = time.perf_counter() - _t0
_progress("dataset ready")
_stage(f"consolidated dataset ready: {len(overview):,} rows")

if overview.empty:
    st.info(
        f"No Jira created between {overview_start.strftime('%d %b %Y')} and "
        f"{overview_end.strftime('%d %b %Y')}. Widen the date range to see more."
    )
    st.stop()

# Some rows arrive with a full dashboard/Jira URL in "ticket" instead of a
# bare key (e.g. "http://localhost:8501/?ticket=TE-25300"), which then shows
# up unlinked/raw in the Jira column. extract_jira_key() already knows how
# to pull the key out of that; apply it once here so every downstream table
# and link is guaranteed a clean TE-XXXXX value.
overview["ticket"] = overview["ticket"].apply(
    lambda v: extract_jira_key(v) or str(v or "").strip().upper()
)
overview = overview[overview["ticket"] != ""].drop_duplicates(subset=["ticket"], keep="last").copy()

# Window already applied inside build_consolidated_dataset (before the Jira
# refresh); this is only a guard in case the dataset was served from cache.
overview = overview[
    overview["Dashboard Date"].notna()
    & (overview["Dashboard Date"] >= overview_start)
    & (overview["Dashboard Date"] < overview_end)
].copy()

# LTPM / Story issue types are engineering work items, not customer-reported
# issues — they are never shown as Active Jira or as an SLA breach anywhere
# on the dashboard (e.g. TE-24382, a Story, must not appear as breached).
_overview_issue_type_col = next(
    (c for c in ["issue_type", "issuetype", "issueType", "type"] if c in overview.columns),
    None,
)
if _overview_issue_type_col:
    overview = overview[
        ~overview[_overview_issue_type_col].apply(is_excluded_active_issue_type)
    ].copy()

st.caption(
    f"Showing Jira created between **{overview_start.strftime('%d %b %Y')}** and "
    f"**{(overview_end - pd.Timedelta(seconds=1)).strftime('%d %b %Y')}** — {len(overview):,} of "
    f"{len(ticket_summary):,} in the database."
)

if priority_filter != "All":
    if priority_filter == "Other":
        overview = overview[~overview["Priority"].str.lower().isin({"sev 1", "sev 2", "sev 3", "sev 4"})].copy()
    else:
        overview = overview[overview["Priority"].str.lower() == priority_filter.lower()].copy()

if project_filter != "All" and "project" in overview.columns:
    overview = overview[overview["project"].astype(str).str.strip() == project_filter].copy()

if team_filter != "All" and "team" in overview.columns:
    overview = overview[overview["team"].astype(str).str.strip() == team_filter].copy()

if assignee_type != "All":
    if assignee_type == "L3":
        overview = overview[overview["current_role"].astype(str).str.upper() == "L3"].copy()
    elif assignee_type == "Developer":
        overview = overview[overview["current_role"].astype(str).str.upper() == "DEV"].copy()
    else:
        overview = overview[overview["current_owner"].astype(str).str.upper().isin({"", "UNASSIGNED", "NONE", "NAN"})].copy()

if status_filter != "All":
    if status_filter in {"Open", "Completed"}:
        overview = overview[overview["Lifecycle"] == status_filter].copy()
    else:
        overview = overview[overview["Queue"] == status_filter].copy()

if overview.empty:
    st.info("No Jira records match the selected filters.")
    st.stop()

# SLA Health is calculated after filters so the dashboard KPIs always reflect
# exactly the Jira records currently visible to the user.
_t1 = time.perf_counter()
_progress(f"computing SLA health for {len(overview):,} rows")
overview = add_dashboard_sla_health(overview, transitions)
st.session_state["diag_health_seconds"] = time.perf_counter() - _t1
_progress("SLA health done")

# Single source of truth: "SLA Breached" must always agree with "Jira
# Health". These used to be two independent calculations and could show
# different totals for the same filtered dataset (e.g. the KPI card said
# 298 while the Jira SLA Health section said 392). Jira Health already
# accounts for paused (customer wait / under observation) tickets and
# resolved tickets, so every breach-based view on the dashboard — the KPI
# card, the Recent SLA Breaches tab, the charts, and the owner tables —
# now derives from this one flag.
overview["SLA Breached"] = overview["Jira Health"] == "BREACHED"
overview["SLA Compliance"] = overview["Jira Health"] != "BREACHED"
_stage("SLA health done; rendering charts")
_status.update(label=f"Overview loaded in {time.perf_counter() - _t0:.1f}s", state="complete", expanded=False)

with st.expander("Diagnostics (load timing / Jira refresh)", expanded=False):
    _err_col = overview["jira_refresh_error"] if "jira_refresh_error" in overview.columns else pd.Series(dtype=str)
    _errs = _err_col.fillna("").astype(str).str.strip()
    _err_count = int((_errs != "").sum())
    d1, d2, d3, d4 = st.columns(4)
    d1.metric("Build dataset (s)", f"{st.session_state.get('diag_build_seconds', 0):.1f}")
    d2.metric("SLA health calc (s)", f"{st.session_state.get('diag_health_seconds', 0):.1f}")
    d3.metric("Jira-verified rows", f"{int(overview.get('jira_verified', pd.Series(dtype=bool)).fillna(False).astype(bool).sum()):,}")
    d4.metric("Rows with refresh error", f"{_err_count:,}")
    if _err_count:
        st.write("Top refresh errors:")
        st.dataframe(_errs[_errs != ""].value_counts().head(10).rename_axis("error").reset_index(name="rows"), hide_index=True, width="stretch")

if st.session_state.get("overview_live_jira", False) and "jira_verified" in overview.columns:
    _unverified = int((~overview["jira_verified"].fillna(False).astype(bool)).sum())
    if _unverified:
        st.warning(
            f"{_unverified:,} of {len(overview):,} Jira could not be refreshed from Jira and are "
            "showing stored values (see Diagnostics for the error)."
        )

# ------------------------------------------------------------
# KPI ROW
# ------------------------------------------------------------
total_jira = overview["ticket"].nunique()
open_count = int((overview["Lifecycle"] == "Open").sum())
completed_count = int((overview["Lifecycle"] == "Completed").sum())
breached_count = int(overview["SLA Breached"].sum())
compliance = 100.0 * float(overview["SLA Compliance"].mean()) if len(overview) else 0.0
resolved_for_time = overview[overview["Lifecycle"] == "Completed"]
if resolved_for_time.empty:
    resolved_for_time = overview
avg_resolution = float(resolved_for_time["SLA Hours"].mean()) if len(resolved_for_time) else 0.0
median_resolution = float(resolved_for_time["SLA Hours"].median()) if len(resolved_for_time) else 0.0
p90_resolution = float(resolved_for_time["SLA Hours"].quantile(0.90)) if len(resolved_for_time) else 0.0

health_counts = overview["Jira Health"].value_counts()
healthy_count = int(health_counts.get("HEALTHY", 0))
at_risk_count = int(health_counts.get("AT RISK", 0))
health_breached_count = int(health_counts.get("BREACHED", 0))
health_resolved_count = int(health_counts.get("RESOLVED", 0))
avg_risk = float(pd.to_numeric(overview["Risk Score"], errors="coerce").fillna(0).mean())

k1, k2, k3, k4, k5, k6, k7, k8 = st.columns(8)
k1.metric("Total Jira", f"{total_jira:,}")
k2.metric("Open Jira", f"{open_count:,}")
k3.metric("Resolved Jira", f"{completed_count:,}")
k4.metric("SLA Compliance", f"{compliance:.1f}%")
k5.metric("Breached SLA", f"{breached_count:,}", help="Open Jira over their SLA target. Resolved Jira are excluded; time spent awaiting customer response is not counted.")
k6.metric("Avg Resolution Time", format_hours_value(avg_resolution), help=f"Median: {format_hours_value(median_resolution)} • P90: {format_hours_value(p90_resolution)}")
k7.metric("Jira Health", f"{healthy_count} Healthy / {at_risk_count} At Risk", help=f"{health_resolved_count} resolved (not counted)")
k8.metric("Avg Risk", f"{avg_risk:.0f}/100", help=f"{health_breached_count} Jira currently classified as BREACHED")


# ------------------------------------------------------------
# TOP-LEVEL TABS — Recent SLA Breaches is the primary, first-glance view;
# everything else lives under Full Analytics.
# ------------------------------------------------------------
_top_tabs = st.tabs(["🚨 Recent SLA Breaches", "📊 Full Analytics"])

with _top_tabs[0]:
    st.subheader("Recent SLA Breaches")
    st.caption(
        "Every open Jira currently over its SLA target. Time awaiting customer "
        "response or under observation is excluded, so this list only shows "
        "breaches the team can act on right now."
    )

    def _render_breach_table(df):
        if df.empty:
            st.success("No SLA breaches in this view.")
            return
        display = df[[
            "ticket", "status", "Priority", "Queue", "current_owner",
            "Breach Duration", "SLA Hours", "Target Hours", "Breach Reason"
        ]].copy()
        display["ticket"] = display["ticket"].astype(str).apply(transition_dashboard_url)
        display["SLA Hours"] = display["SLA Hours"].apply(format_hours_value)
        display["Target Hours"] = display["Target Hours"].apply(format_hours_value)
        display = display.rename(columns={
            "ticket": "Jira", "status": "Status", "Priority": "Priority", "Queue": "Breached At",
            "current_owner": "Owner", "Breach Duration": "Time Breached", "SLA Hours": "Total SLA",
            "Target Hours": "SLA Target", "Breach Reason": "Reason",
        })
        st.dataframe(
            display,
            width="stretch",
            hide_index=True,
            column_config={
                "Jira": st.column_config.LinkColumn("Jira", display_text=r"(TE-[0-9]+)"),
            },
            height=520,
        )

    _breach_full = overview[overview["SLA Breached"]].copy().sort_values("SLA Hours", ascending=False)
    _breach_full["_sev"] = _breach_full["Priority"].apply(severity_bucket)

    _breach_sev_order = ["Sev 1", "Sev 2", "Sev 3", "Sev 4", "Other / Unknown"]
    _breach_sev_groups = {sev: _breach_full[_breach_full["_sev"] == sev] for sev in _breach_sev_order}
    _breach_sev_groups = {sev: grp for sev, grp in _breach_sev_groups.items() if not grp.empty}

    _breach_tab_labels = [f"All ({len(_breach_full):,})"] + [
        f"{sev} ({len(grp):,})" for sev, grp in _breach_sev_groups.items()
    ]
    _breach_tabs = st.tabs(_breach_tab_labels)

    with _breach_tabs[0]:
        st.metric("Breached Jira", f"{len(_breach_full):,}")
        _render_breach_table(_breach_full)

    for _tab, (_sev, _grp) in zip(_breach_tabs[1:], _breach_sev_groups.items()):
        with _tab:
            _target = _grp["Target Hours"].mode()
            _target = format_hours_value(_target.iloc[0]) if not _target.empty else "—"
            m1, m2 = st.columns(2)
            m1.metric("Breached Jira", f"{len(_grp):,}")
            m2.metric("SLA Target", _target)
            _render_breach_table(_grp)

with _top_tabs[1]:
    # ------------------------------------------------------------
    # SLA HEALTH OVERVIEW
    # ------------------------------------------------------------
    st.subheader("Jira SLA Health")
    st.caption("Risk score is deterministic (0–100). Higher scores indicate greater SLA risk; Jira Health is derived from the same SLA rules used on the ticket detail page.")

    h1, h2, h3, h4 = st.columns(4)
    h1.metric("Healthy", f"{healthy_count}")
    h2.metric("At Risk", f"{at_risk_count}")
    h3.metric("Breached", f"{health_breached_count}")
    h4.metric("Resolved", f"{health_resolved_count}", help="Done / Closed / Won't Do — no active SLA")

    health_table = overview[["ticket", "Priority", "status", "current_owner", "Jira Health", "Risk Score", "Risk Factor", "SLA Hours", "Target Hours"]].copy()
    health_table["SLA Hours"] = health_table["SLA Hours"].apply(format_hours_value)
    health_table["Target Hours"] = health_table["Target Hours"].apply(format_hours_value)
    health_table["ticket"] = health_table["ticket"].astype(str).apply(transition_dashboard_url)
    health_table = health_table.rename(columns={
        "ticket": "Jira",
        "status": "Current Status",
        "current_owner": "Owner",
        "Jira Health": "Health",
        "Risk Score": "Risk / 100",
        "Risk Factor": "Primary Risk Factor",
        "SLA Hours": "Total SLA",
        "Target Hours": "SLA Target",
    })

    # Highest-risk Jira first. This makes the dashboard operational rather than
    # just informational.
    health_table = health_table.sort_values("Risk / 100", ascending=False)

    _health_col_config = {
        "Jira": st.column_config.LinkColumn("Jira", display_text=r"(TE-[0-9]+)"),
        "Risk / 100": st.column_config.ProgressColumn("Risk / 100", min_value=0, max_value=100, format="%d"),
    }

    health_table["_sev"] = health_table["Priority"].apply(severity_bucket)

    # Operational view: only Jira that need action are listed by default.
    _health_scope = st.radio(
        "Show",
        ["Breached only", "Breached + At Risk", "All open", "Everything (incl. resolved)"],
        horizontal=True,
        key="health_table_scope",
        label_visibility="collapsed",
    )
    _scope_sets = {
        "Breached only": {"BREACHED"},
        "Breached + At Risk": {"BREACHED", "AT RISK"},
        "All open": {"BREACHED", "AT RISK", "HEALTHY", "UNKNOWN"},
    }
    if _health_scope in _scope_sets:
        health_table = health_table[health_table["Health"].isin(_scope_sets[_health_scope])]

    _sev_order = ["Sev 1", "Sev 2", "Sev 3", "Sev 4", "Other / Unknown"]
    _sev_groups = {sev: health_table[health_table["_sev"] == sev] for sev in _sev_order}
    _sev_groups = {sev: grp for sev, grp in _sev_groups.items() if not grp.empty}

    _tab_labels = [f"All ({len(health_table):,})"] + [
        f"{sev} ({len(grp):,}"
        + (f" • {int((grp['Health'] == 'BREACHED').sum())} breached" if (grp['Health'] == 'BREACHED').any() else "")
        + ")"
        for sev, grp in _sev_groups.items()
    ]
    _tabs = st.tabs(_tab_labels)

    if health_table.empty:
        st.success("No Jira match this view — nothing breached in the selected filters.")

    with _tabs[0]:
        st.dataframe(
            health_table.drop(columns=["_sev"]),
            width="stretch", hide_index=True, column_config=_health_col_config, height=360,
        )

    for tab, (sev, grp) in zip(_tabs[1:], _sev_groups.items()):
        with tab:
            target = grp["SLA Target"].mode().iloc[0] if not grp["SLA Target"].mode().empty else "—"
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Jira", f"{len(grp):,}")
            m2.metric("Breached", f"{int((grp['Health'] == 'BREACHED').sum()):,}")
            m3.metric("At Risk", f"{int((grp['Health'] == 'AT RISK').sum()):,}")
            m4.metric("SLA Target", target)
            st.dataframe(
                grp.drop(columns=["_sev", "Priority"]),
                width="stretch", hide_index=True, column_config=_health_col_config, height=360,
            )

    # ------------------------------------------------------------
    # ROW 1 — SLA PERFORMANCE
    # ------------------------------------------------------------

    # ------------------------------------------------------------
    # ROW 1 — SLA PERFORMANCE
    # ------------------------------------------------------------
    left, middle, right = st.columns([1.15, 1.15, 1.0])

    with left:
        st.subheader("SLA Compliance by Priority")
        priority_df = (
            overview.groupby("Priority", as_index=False)
            .agg(
                Total=("ticket", "nunique"),
                Within_SLA=("SLA Compliance", "sum"),
            )
        )
        priority_df["Within %"] = 100 * priority_df["Within_SLA"] / priority_df["Total"].replace(0, 1)
        priority_df["Breached %"] = 100 - priority_df["Within %"]
        priority_df = priority_df.sort_values("Priority")
        fig = go.Figure()
        fig.add_bar(y=priority_df["Priority"], x=priority_df["Within %"], orientation="h", name="Within SLA", text=priority_df["Within %"].round(0).astype(int).astype(str) + "%")
        fig.add_bar(y=priority_df["Priority"], x=priority_df["Breached %"], orientation="h", name="Breached", text=priority_df["Breached %"].round(0).astype(int).astype(str) + "%")
        fig.update_layout(barmode="stack", height=285, margin=dict(l=10, r=10, t=10, b=10), xaxis_title="Percent", yaxis_title="", xaxis=dict(range=[0, 100]))
        fig.update_traces(textposition="inside")
        st.plotly_chart(fig, width="stretch", key="overview_priority_compliance")

    with middle:
        st.subheader("Average Resolution Time by Priority")
        st.caption("Resolved Jira only — matches the \"Avg Resolution Time\" KPI above.")
        # Use the same scope as the top-level "Avg Resolution Time" KPI
        # (Lifecycle == "Completed"). The old version averaged "SLA Hours"
        # across ALL Jira including still-open ones, whose SLA clock keeps
        # climbing — that silently inflated this "resolution time" with
        # tickets that haven't actually been resolved yet, and disagreed
        # with the KPI card showing the same label.
        _priority_order = overview["Priority"].drop_duplicates()
        avg_priority = (
            resolved_for_time.groupby("Priority", as_index=False)
            .agg(
                Average=("SLA Hours", "mean"),
                P90=("SLA Hours", lambda x: x.quantile(0.90)),
            )
        )
        avg_priority = (
            pd.DataFrame({"Priority": _priority_order})
            .merge(avg_priority, on="Priority", how="left")
        )
        # Do not assign a fixed column list here. Pandas groupby/agg may retain
        # additional grouping metadata depending on the input frame. Select the
        # required columns explicitly to avoid Length mismatch errors.
        avg_priority["Median/Avg"] = pd.to_numeric(
            avg_priority.get("Average", pd.Series(dtype=float)), errors="coerce"
        ).fillna(0)
        avg_priority["P90"] = pd.to_numeric(
            avg_priority.get("P90", pd.Series(dtype=float)), errors="coerce"
        ).fillna(0)
        if not (overview["Lifecycle"] == "Completed").any():
            st.info("No resolved Jira in the selected filters yet — showing all Jira as a fallback.")
        fig = go.Figure()
        fig.add_bar(x=avg_priority["Priority"], y=avg_priority["Median/Avg"], name="Average", text=avg_priority["Median/Avg"].round(1).astype(str) + "h")
        fig.add_bar(x=avg_priority["Priority"], y=avg_priority["P90"], name="P90", text=avg_priority["P90"].round(1).astype(str) + "h")
        fig.update_layout(height=285, margin=dict(l=10, r=10, t=10, b=10), yaxis_title="Hours", xaxis_title="")
        fig.update_traces(textposition="outside")
        st.plotly_chart(fig, width="stretch", key="overview_resolution_priority")

    with right:
        st.subheader("SLA Breaches by Status")
        breach_df = overview[overview["SLA Breached"]].copy()
        if breach_df.empty:
            st.success("No SLA breaches in the selected period.")
        else:
            status_counts = breach_df.groupby("Queue", as_index=False)["ticket"].nunique().rename(columns={"ticket": "Tickets"})
            fig = px.pie(status_counts, names="Queue", values="Tickets", hole=.55)
            fig.update_layout(height=285, margin=dict(l=5, r=5, t=10, b=10), showlegend=True)
            fig.add_annotation(text=f"{breached_count}<br><span style='font-size:11px'>Total</span>", x=.5, y=.5, showarrow=False)
            st.plotly_chart(fig, width="stretch", key="overview_breach_status")

    # ------------------------------------------------------------
    # ROW 2 — TRANSITION / OWNERSHIP
    # ------------------------------------------------------------
    # "Time Spent in Each Status (Average)" used to live here as a standalone
    # bar chart, but it was showing the exact same per-status average already
    # in the "Avg Time" column of the Transition SLA table below — a strict
    # subset of that table's info (which also has P90, target and SLA %) —
    # so it was dropped as a duplicate rather than kept for its own sake.
    middle, right = st.columns([1.4, 1.0])

    with middle:
        st.subheader("SLA Age by Current Status (Average | P90)")
        st.caption(
            "\"Avg/P90 Time\" is total elapsed SLA time (since creation) for "
            "Jira currently sitting in each status — not the duration of that "
            "one transition step. A high number here means tickets are aging "
            "badly while stuck in that status, not that the step itself is slow."
        )
        transition_rows = []
        for queue, group in overview.groupby("Queue"):
            vals = group["SLA Hours"]
            transition_rows.append({
                "Status": queue,
                "Avg Age": format_hours_value(vals.mean()),
                "P90 Age": format_hours_value(vals.quantile(.90)),
                "SLA Target": format_hours_value(overview[overview["Queue"] == queue]["Target Hours"].median()),
                "SLA %": f"{100 * group['SLA Compliance'].mean():.0f}%",
            })
        transition_display = pd.DataFrame(transition_rows)
        st.dataframe(transition_display, width="stretch", hide_index=True, height=300)

    with right:
        st.subheader("Ownership Time Breakdown (Average)")
        ownership = pd.DataFrame({
            "Category": ["L3", "Developers", "Other (counted)", "On Hold (Customer Wait / Observation)"],
            "Hours": [
                overview["l3_minutes"].mean()/60,
                overview["dev_minutes"].mean()/60,
                max(0, overview["SLA Hours"].mean() - overview["l3_minutes"].mean()/60 - overview["dev_minutes"].mean()/60),
                overview["customer_wait_minutes"].mean()/60,
            ],
        })
        ownership = ownership[ownership["Hours"] > 0]
        fig = px.pie(ownership, names="Category", values="Hours", hole=.55)
        fig.update_layout(height=300, margin=dict(l=5, r=5, t=10, b=10))
        fig.add_annotation(text=f"{format_hours_value(overview['SLA Hours'].mean())}<br><span style='font-size:11px'>Avg Total</span>", x=.5, y=.5, showarrow=False)
        st.plotly_chart(fig, width="stretch", key="overview_ownership")

    # ------------------------------------------------------------
    # ROW 3 — AGING + OWNERS
    # ------------------------------------------------------------
    # "Top L3 Owners" used to sit here as a full column, but it always reads
    # "No L3 ownership data available" — canonical_person_role() only
    # recognizes 2 hardcoded L3 names (PERSON_ROLE_MAP), so almost no
    # currently-open ticket resolves to role == "L3" even though L3 work is
    # clearly happening (see Ownership Time Breakdown). A widget that never
    # has data trains people to ignore it, so it's pulled out of the main
    # grid until the roster is current — see the expander below instead.
    left, right = st.columns([1.0, 1.3])

    with left:
        st.subheader("Open Jira Aging")
        open_df = overview[overview["Lifecycle"] == "Open"].copy()
        if open_df.empty:
            st.info("No open Jira in the selected period.")
        else:
            open_df["Age Hours"] = (pd.Timestamp.now(tz="UTC") - open_df["Dashboard Date"]).dt.total_seconds() / 3600
            bins = [-1, 4, 12, 24, 48, 72, float("inf")]
            labels = ["0–4h", "4–12h", "12–24h", "24–48h", "48–72h", "72h+"]
            open_df["Aging"] = pd.cut(open_df["Age Hours"].fillna(0), bins=bins, labels=labels)
            aging = open_df.groupby("Aging", observed=False)["ticket"].nunique().reindex(labels, fill_value=0).reset_index()
            aging.columns = ["Aging", "Tickets"]
            fig = px.bar(aging, x="Aging", y="Tickets", text="Tickets")
            fig.update_traces(textposition="outside")
            fig.update_layout(height=280, margin=dict(l=10, r=10, t=10, b=10), xaxis_title="", yaxis_title="Jira")
            st.plotly_chart(fig, width="stretch", key="overview_aging")

    with right:
        st.subheader("Top Developer Owners")
        dev = overview[overview["current_role"].astype(str).str.upper() == "DEV"].copy()
        if dev.empty:
            st.info("No developer ownership data available.")
        else:
            owner_table = dev.groupby("current_owner", as_index=False).agg(
                Jira_Handled=("ticket", "nunique"),
                Avg_Ownership_Hours=("dev_minutes", lambda x: x.sum() / max(1, x.count()) / 60),
                SLA_Compliance=("SLA Compliance", "mean"),
            ).sort_values("Avg_Ownership_Hours", ascending=False).head(8)
            owner_table["Avg Ownership"] = owner_table["Avg_Ownership_Hours"].apply(format_hours_value)
            owner_table["SLA %"] = (owner_table["SLA_Compliance"].fillna(0) * 100).round(0).astype(int).astype(str) + "%"
            st.dataframe(owner_table[["current_owner", "Jira_Handled", "Avg Ownership", "SLA %"]].rename(columns={"current_owner":"Developer", "Jira_Handled":"Jira Handled"}), width="stretch", hide_index=True, height=280)

    with st.expander("Top L3 Owners (needs roster fix — currently unreliable)"):
        st.caption(
            "canonical_person_role() only recognizes L3 triagers listed in "
            "PERSON_ROLE_MAP (currently just 2 names). Anyone else falls "
            "back to historical transition data, which is often blank, so "
            "this table under-reports real L3 ownership. Add the current L3 "
            "roster to PERSON_ROLE_MAP to fix it properly."
        )
        l3 = overview[overview["current_role"].astype(str).str.upper() == "L3"].copy()
        if l3.empty:
            st.info("No L3 ownership data available.")
        else:
            owner_table = l3.groupby("current_owner", as_index=False).agg(
                Jira_Handled=("ticket", "nunique"),
                Avg_Ownership_Hours=("l3_minutes", lambda x: x.sum() / max(1, x.count()) / 60),
                SLA_Compliance=("SLA Compliance", "mean"),
            ).sort_values("Avg_Ownership_Hours", ascending=False).head(8)
            owner_table["Avg Ownership"] = owner_table["Avg_Ownership_Hours"].apply(format_hours_value)
            owner_table["SLA %"] = (owner_table["SLA_Compliance"].fillna(0) * 100).round(0).astype(int).astype(str) + "%"
            st.dataframe(owner_table[["current_owner", "Jira_Handled", "Avg Ownership", "SLA %"]].rename(columns={"current_owner":"L3 Triager", "Jira_Handled":"Jira Handled"}), width="stretch", hide_index=True, height=280)

    # ------------------------------------------------------------
    # ROW 4 — HANDOFFS
    # ------------------------------------------------------------
    st.subheader("Handoff Analysis")
    if transitions.empty:
        st.info("No transition data available.")
    else:
        handoff = transitions.groupby("ticket")["assigned_to"].nunique().reset_index(name="Owners")
        handoff["Bucket"] = pd.cut(handoff["Owners"], bins=[0,1,2,3,float("inf")], labels=["1 Owner", "2 Owners", "3 Owners", "4+ Owners"])
        handoff_counts = handoff.groupby("Bucket", observed=False)["ticket"].nunique().reindex(["1 Owner","2 Owners","3 Owners","4+ Owners"], fill_value=0).reset_index()
        handoff_counts.columns = ["Owners", "Jira"]
        fig = px.pie(handoff_counts, names="Owners", values="Jira", hole=.5)
        fig.update_layout(height=280, margin=dict(l=5, r=5, t=10, b=10))
        st.plotly_chart(fig, width="stretch", key="overview_handoffs")

    # ------------------------------------------------------------
    # Active Jira drill-down
    # ------------------------------------------------------------
    st.subheader("Jira SLA Details")
    active_overview = overview[overview["Lifecycle"] == "Open"].copy()
    if not active_overview.empty:
        active_table = active_overview[[
            "ticket", "Priority", "reporter", "status", "Queue", "current_owner", "current_role", "Jira Health", "Risk Score", "Risk Factor", "L3 Time", "DEV Time", "Resolution Time"
        ]].copy()
        active_table["ticket"] = active_table["ticket"].astype(str).apply(transition_dashboard_url)
        active_table = active_table.rename(columns={
            "ticket":"Jira", "reporter":"Reporter", "status":"Current Status", "current_owner":"Current Owner", "current_role":"Role", "Jira Health":"Health", "Risk Score":"Risk / 100", "Risk Factor":"Primary Risk Factor", "L3 Time":"L3 SLA", "DEV Time":"DEV SLA", "Resolution Time":"Total SLA"
        })
        st.dataframe(
            active_table,
            width="stretch",
            hide_index=True,
            column_config={
                "Jira": st.column_config.LinkColumn("Jira", display_text=r"(TE-[0-9]+)"),
                "Risk / 100": st.column_config.ProgressColumn("Risk / 100", min_value=0, max_value=100, format="%d"),
            },
        )
    else:
        st.info("No open Jira records for the selected filters.")

st.caption(
    f"All times are in calendar time (IST) • SLA targets are configured in the dashboard • "
    f"Data as of {datetime.now().strftime('%d %b %Y, %I:%M %p IST')}"
)

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
