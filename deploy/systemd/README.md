# systemd deployment

Runs both scripts as **one-shot containers on a timer** — the smart pattern for
a lightweight, idempotent job like this: each run starts fresh (crash-isolated),
nothing sits in RAM between runs, and the two scripts get independent schedules.

| Unit | Schedule | Runs |
|------|----------|------|
| `steam-notifier.{service,timer}` | daily 09:00 | sale change alerts (`notifier.py`) |
| `steam-availability.{service,timer}` | every 20 min | the availability watcher (`availability.py`) |

Both call `docker compose run --rm <service>`, so the [docker-compose.yml](../../docker-compose.yml)
config (env from `.env`, persistent `state` volume) is reused as-is.

## Prerequisites

1. **Docker + the Compose plugin** installed.
2. **`.env` filled in** (`cp .env.example .env` and edit). The services read it.
3. The unit runs as **`User=vuksan`**, so that user must be able to talk to
   Docker without sudo:
   ```sh
   sudo usermod -aG docker vuksan      # then log out/in once
   ```
   (Or set `User=root` in the `.service` files and skip this.)
4. **Build the image once** so the first timer run doesn't wait on a build:
   ```sh
   cd /home/vuksan/steam-sale-notifier
   docker compose build
   ```

## Adjust before installing

The unit files assume:
- checkout at `/home/vuksan/steam-sale-notifier`
- user `vuksan`
- the `docker` binary at `/usr/bin/docker` (check with `which docker`)

If any differ, edit the `.service` files (`WorkingDirectory`, `User`,
`ExecStart` path) accordingly.

## Install

```sh
# from the repo root
sudo cp deploy/systemd/steam-*.service deploy/systemd/steam-*.timer /etc/systemd/system/
sudo systemctl daemon-reload

# enable + start the *timers* (not the services)
sudo systemctl enable --now steam-notifier.timer
sudo systemctl enable --now steam-availability.timer
```

## Verify

```sh
systemctl list-timers 'steam-*'        # next/last fire times
journalctl -u steam-notifier.service   # output of the digest runs
journalctl -u steam-availability.service

# trigger a run right now without waiting for the timer:
sudo systemctl start steam-notifier.service
```

## Change a schedule

Edit the `OnCalendar=` line in the `.timer` file, then:
```sh
sudo cp deploy/systemd/steam-notifier.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart steam-notifier.timer
```
`OnCalendar` examples: `daily`, `hourly`, `*-*-* 09,18:00:00` (09:00 & 18:00),
`Mon *-*-* 08:00:00` (Mondays 08:00). Test an expression with
`systemd-analyze calendar 'daily'`.

## Uninstall

```sh
sudo systemctl disable --now steam-notifier.timer steam-availability.timer
sudo rm /etc/systemd/system/steam-*.{service,timer}
sudo systemctl daemon-reload
```
