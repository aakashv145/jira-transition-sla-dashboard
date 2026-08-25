import requests
import os


JIRA_URL = os.getenv("JIRA_URL")
USER = os.getenv("JIRA_USER")
TOKEN = os.getenv("JIRA_TOKEN")


def get_active_issues():

    jql = """
    project = TE
    AND statusCategory != Done
    ORDER BY updated DESC
    """

    url = f"{JIRA_URL}/rest/api/3/search"

    payload = {
        "jql": jql,
        "maxResults": 500,
        "fields": [
            "summary",
            "status",
            "assignee",
            "created",
            "updated"
        ]
    }

    response = requests.post(
        url,
        json=payload,
        auth=(USER,TOKEN)
    )

    response.raise_for_status()

    return response.json()["issues"]