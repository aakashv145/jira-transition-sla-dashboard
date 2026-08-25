from apscheduler.schedulers.blocking import BlockingScheduler
from main import main


scheduler = BlockingScheduler()


@scheduler.scheduled_job(
    "interval",
    minutes=5
)
def refresh():

    print(
        "Refreshing Jira SLA data..."
    )

    main()



if __name__ == "__main__":

    main()

    scheduler.start()