# Section 4.10 — Infrastructure redundancy demo

This is the "if one instance goes down, the site keeps running" half of
Section 4.10 (the other half — retries, circuit breakers, and the mail
outage / friendly-error-page demos — lives entirely inside the app itself
and needs no extra setup; see the README and `_devtools/test_smoke.py`'s
"4.10 Fault tolerance" section).

This part runs **three separate GymFlow instances** behind an **nginx load
balancer**, all sharing one database, so that killing any single instance
does not take the site down. It is a local demo (Docker + Docker Compose)
for showing the professor infrastructure-level fault tolerance — it is not
how the graded PythonAnywhere deployment is set up, and it doesn't need to
be for the CRUD/algorithm/ML/AI-agent features to be graded.

## Prerequisites

- Docker Desktop installed and running (on Windows/WSL: Docker Desktop with
  the WSL2 backend enabled — installer at docker.com if you don't have it).
- Nothing else already listening on port 8080 on your machine.

## Running it

From the project folder:

```
docker compose up --build
```

This builds the GymFlow image once and starts:

- `app1`, `app2`, `app3` — three independent copies of the whole Flask app
  (each running under gunicorn, 2 worker processes), sharing one SQLite
  database file and one uploads folder through Docker volumes.
- `nginx` — a load balancer in front of them, listening on **port 8080**.

Open **http://localhost:8080** (not :5000 — that would bypass the load
balancer and talk to a single instance directly). Log in with the default
administrator (`admin@gymflow.local` / `Admin123!`) same as always.

## Demoing the failover live

1. With `docker compose up` running, open http://localhost:8080 and log in.
   Refresh the dashboard a few times — every response carries an
   `X-Served-By` response header naming which instance answered
   (`app1`/`app2`/`app3`); check it in your browser's dev tools Network tab
   to see the load balancer spreading requests across all three.
2. In another terminal, kill one instance to simulate it crashing:
   ```
   docker compose stop app2
   ```
3. Keep using the site (refresh the dashboard, book a class, whatever).
   Nothing breaks — you stay logged in (the session cookie is valid on
   every instance because they share the same `GYMFLOW_SECRET_KEY`) and
   every request still succeeds, now only ever served by `app1`/`app3`
   (check the `X-Served-By` header again). nginx detected `app2` failing
   and stopped routing to it automatically (`nginx/nginx.conf`'s
   `max_fails`/`fail_timeout` + `proxy_next_upstream`).
4. Bring it back:
   ```
   docker compose start app2
   ```
   Within a few seconds it starts showing up in `X-Served-By` again —
   nginx retries a backend it previously marked down once its
   `fail_timeout` window elapses.
5. To simulate a *harder* crash (not just stopped, but gone), use
   `docker compose kill app2` instead of `stop`, then `docker compose up -d
   app2` to bring a fresh instance back.

To confirm this isn't just the load balancer quietly hiding a broken
instance, hit an instance directly while it's still running:
`curl http://localhost:8080/health` goes through nginx; there's no direct
port mapping to an individual instance in this compose file by design (that
would defeat the point of only ever talking to the load balancer) — instead
watch `docker compose logs -f app2` in a separate terminal while you stop
and start it, so you can see it actually go down and come back.

## Design notes (worth knowing if asked about it)

- **Passive health checking.** Open-source nginx doesn't ship active health
  checks (polling `/health` on its own); that's an nginx-plus feature. This
  demo uses the standard passive approach instead: `max_fails=1
  fail_timeout=10s` on each upstream server means "after 1 failed request,
  stop sending this server traffic for 10 seconds, then try it again."
  `proxy_next_upstream` makes nginx retry a *single* failed request against
  a different backend transparently, so a visitor never even sees the
  failure that triggered the mark-down.
- **Shared SQLite via a Docker volume.** All three instances point at the
  same database file (`GYMFLOW_DATABASE_URL=sqlite:////shared/gymflow.db`,
  a named volume). This is fine for this demo (light traffic, mostly one
  person clicking around) but is a real limitation, not a production
  pattern — SQLite serializes writes across processes and can raise
  "database is locked" under real concurrent write load. A production
  multi-instance deployment would point `GYMFLOW_DATABASE_URL` at a real
  database server (e.g. Postgres) instead, which needs **zero application
  code changes** since `gymflow/config.py` already reads that URL from the
  environment.
- **`_seed_admin()` had a real race condition here**, found while building
  this demo: several instances booting against the same empty database at
  once could each see "no administrator yet" and all try to insert one,
  and the losers crashed on a UNIQUE-constraint error instead of just
  no-op'ing. Fixed in `gymflow/__init__.py` by catching that and rolling
  back instead of letting the worker die — see the comment there. This is
  exactly the kind of bug that infrastructure-level redundancy testing is
  supposed to surface, so it's worth mentioning if asked how this was
  verified.
- **Shared `GYMFLOW_SECRET_KEY` across instances** is what makes a login
  session portable between them — Flask's session cookie is just signed
  with that key, so any instance that knows it can validate a cookie
  another instance issued. If it were random per-instance, a load-balanced
  visitor would get logged out every time the balancer switched them to a
  different backend.
