# Steam sale notifier — zero external dependencies, so a slim Python base is all
# we need. The scripts are one-shot (run once and exit), intended to be invoked
# on a schedule by the host (cron, a Kubernetes CronJob, etc.).

FROM python:3.12-slim

# Don't buffer stdout/stderr so logs show up immediately, and don't write .pyc.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Copy the application. .dockerignore keeps .env, state files and git out.
COPY . .

# Run as a non-root user. Create the state dir (mount point for the `state`
# volume) up front so it's owned by `app` — otherwise Docker initialises the
# volume mount point as root and the one-shot scripts can't write their state.
RUN useradd --create-home --uid 1000 app \
    && mkdir -p /app/data \
    && chown -R app:app /app
USER app

# Configuration comes from the environment. Pass it at run time, e.g.:
#
#   # one-shot, env from your .env file:
#   docker build -t steam-sale-notifier .
#   docker run --rm --env-file .env steam-sale-notifier
#
#   # persist the "what was on sale last run" state across runs:
#   docker run --rm --env-file .env -v steam-state:/app steam-sale-notifier
#
#   # run the availability watcher instead of the sale digest:
#   docker run --rm --env-file .env steam-sale-notifier python availability.py
#
#   # preview without posting to Slack:
#   docker run --rm --env-file .env steam-sale-notifier python notifier.py --dry-run
CMD ["python", "notifier.py"]
