from fastapi import FastAPI
import sqlite3


app = FastAPI()


def query(sql):

    conn = sqlite3.connect(
        "sla_dashboard.db"
    )

    cursor = conn.cursor()

    cursor.execute(sql)

    data = cursor.fetchall()

    conn.close()

    return data



@app.get("/tickets")
def tickets():

    return query("""
    SELECT *
    FROM jira_sla
    ORDER BY duration_minutes DESC
    """)



@app.get("/developers")
def developers():

    return query("""
    SELECT
    person,
    SUM(duration_minutes)
    FROM jira_sla
    WHERE role='DEV'
    GROUP BY person
    """)



@app.get("/l3")
def l3():

    return query("""
    SELECT
    person,
    SUM(duration_minutes)
    FROM jira_sla
    WHERE role='L3'
    GROUP BY person
    """)