<<<<<<< HEAD
# Jira Transition SLA Dashboard

Replay Jira changelogs to calculate L3 and Dev ownership time for every assignment.

## What it reports

- Every L3 and Dev assignment interval
- Assigned To and Assigned By
- Jira field/tag, previous and new values
- Individual assignment duration
- Repeated assignments aggregated per person
- L3 pickup SLA
- Total L3 time per ticket
- Total Dev time per ticket
- End-to-end resolution time
- Ticket, person, and transition CSV reports

The implementation uses Jira's paginated issue changelog endpoint rather than relying only on `expand=changelog`, so long histories are not silently truncated.

## Setup

```bash
python -m venv venv
# Windows
venv\Scripts\activate
# macOS/Linux
source venv/bin/activate

pip install -r requirements.txt
copy .env.example .env
```

Set:

```env
JIRA_BASE_URL=https://lambdatest.atlassian.net
JIRA_USER=your-email@example.com
JIRA_TOKEN=your-api-token
```

Never commit `.env`.

## Configure tickets or JQL

Edit `config.yaml`:

```yaml
ticket_keys:
  - TE-9976
  - TE-112
  - TTN-22457

jql: ""

l3_field_names:
  - "L3 triager"

dev_field_names:
  - "assignee"

whole_minutes: true
```

Use either explicit `ticket_keys` or JQL. JQL is useful for date/project based reporting.

## Run

```bash
python main.py
```

Outputs are written to `output/`:

- `jira_transition_sla_report.csv`
- `jira_person_sla_summary.csv`
- `jira_ticket_sla_summary.csv`

## Important ownership model

`L3 triager` is treated as the L3 ownership field and `assignee` as Dev ownership by default. The Jira changelog author's display name is captured as `Assigned By`.

Every assignment is retained as an individual interval. If the same person receives a ticket twice, both intervals are preserved and then summed in the person summary.

## Jira API

The changelog is fetched with `GET /rest/api/3/issue/{issueIdOrKey}/changelog` using pagination. Atlassian documents this endpoint as a paginated collection of issue changelogs. See the official Jira REST API documentation for the endpoint and pagination behavior.

## Security

Use a Jira API token with only the permissions required to read the relevant issues. Credentials are loaded from environment variables and never written to reports.
=======
# jira-transition-sla-dashboard
>>>>>>> 56f695f3f2184b1b7a9950710fd2719e86942bd9
