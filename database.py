import sqlite3
from pathlib import Path


# =====================================================
# DATABASE PATH
# =====================================================

BASE_DIR = Path(__file__).resolve().parent

DB_NAME = BASE_DIR / "sla_dashboard.db"


# =====================================================
# CONNECTION
# =====================================================

def get_connection():

    return sqlite3.connect(
        DB_NAME
    )


# =====================================================
# INITIALIZE DATABASE
# =====================================================

def initialize_db():

    conn = get_connection()

    cursor = conn.cursor()

    # -------------------------------------------------
    # Transition level SLA data
    # -------------------------------------------------

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS transitions
        (
            ticket TEXT,
            project TEXT,
            role TEXT,
            person TEXT,
            assigned_by TEXT,
            assigned_at TEXT,
            released_at TEXT,
            duration_minutes INTEGER,
            duration TEXT,
            status TEXT,
            updated_at DATETIME
                DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # -------------------------------------------------
    # Ticket level SLA data
    # -------------------------------------------------

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS ticket_summary
        (
            ticket TEXT,
            created TEXT,
            l3_pickup_sla TEXT,
            total_l3_time TEXT,
            total_dev_time TEXT,
            status TEXT,
            resolution TEXT,
            updated_at DATETIME
                DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    conn.commit()

    conn.close()


# =====================================================
# SAVE TRANSITION
# =====================================================

def save_transition(row):

    conn = get_connection()

    cursor = conn.cursor()

    cursor.execute(
        """
        INSERT INTO transitions
        (
            ticket,
            project,
            role,
            person,
            assigned_by,
            assigned_at,
            released_at,
            duration_minutes,
            duration,
            status
        )
        VALUES
        (
            :ticket,
            :project,
            :role,
            :assigned_to,
            :assigned_by,
            :assigned_at,
            :released_at,
            :duration_minutes,
            :duration,
            :status
        )
        """,
        row
    )

    conn.commit()

    conn.close()


# =====================================================
# SAVE TICKET SUMMARY
# =====================================================

def save_ticket_summary(
        ticket,
        created,
        l3_pickup_sla,
        total_l3_time,
        total_dev_time,
        status,
        resolution
):

    conn = get_connection()

    cursor = conn.cursor()

    # Delete old summary for this ticket.
    # This is important because scheduler.py runs
    # main() every 5 minutes.

    cursor.execute(
        """
        DELETE FROM ticket_summary
        WHERE ticket = ?
        """,
        (
            ticket,
        )
    )

    cursor.execute(
        """
        INSERT INTO ticket_summary
        (
            ticket,
            created,
            l3_pickup_sla,
            total_l3_time,
            total_dev_time,
            status,
            resolution
        )
        VALUES
        (
            ?,
            ?,
            ?,
            ?,
            ?,
            ?,
            ?
        )
        """,
        (
            ticket,
            created,
            l3_pickup_sla,
            total_l3_time,
            total_dev_time,
            status,
            resolution
        )
    )

    conn.commit()

    conn.close()


# =====================================================
# CLEAR OLD TRANSITIONS FOR TICKET
# =====================================================

def clear_ticket_transitions(ticket):

    conn = get_connection()

    cursor = conn.cursor()

    cursor.execute(
        """
        DELETE FROM transitions
        WHERE ticket = ?
        """,
        (
            ticket,
        )
    )

    conn.commit()

    conn.close()