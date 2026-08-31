# ping-scheduler

**Decide when your session window opens, instead of letting the first casual prompt of the day decide for you.**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/)

Subscription plans that meter usage in rolling sessions start that window on your
first request. If you open a terminal at 08:30 to check one thing, the window is
spent by early afternoon — whether or not that is when you needed it.

`ping-scheduler` fires a trivial command at times you choose, so the window opens
on your schedule. By default that command is `claude -p "ok"`, which runs the
[Claude Code](https://www.claude.com/product/claude-code) CLI, but the command is
configurable and the scheduler itself knows nothing about what it runs.

One page, seven columns, a switch per day and one for everything:

- **Weekly schedule.** Each day has its own list of `HH:MM` times and its own
  toggle. Times are added and removed without touching anything else.
- **Global switch.** Turning it off deletes every trigger; turning it on rebuilds
  them. It starts off deliberately, so a freshly created volume never begins
  firing on a configuration nobody has reviewed.
- **Manual ping.** A button to confirm the mechanism still works. It runs even
  when the scheduler is paused.
- **History.** Recent runs with status, exit code, duration and an excerpt of the
  output — enough to notice something broke without reading container logs.

Configuration changes rebuild the triggers in place: **the container never needs
restarting.**

## How it works

```
FastAPI + APScheduler (in process)
        │
        │  at the scheduled time
        ▼
subprocess:  claude -p "ok"        ← configurable via PING_COMMAND
        │
        ▼
SQLite in the data volume
   (timestamp, weekday, success, exit code, output excerpt)
```

The scheduler does not sync incrementally: every change drops all jobs and
recreates them from the database. With seven days of a few times each the cost is
irrelevant, and it eliminates an entire class of stale-state bugs.

## Quick start

```bash
git clone https://github.com/avesperinas/ping-scheduler
cd ping-scheduler
cp .env.example .env      # fill in credentials and your token
docker compose up -d
```

Then open <http://127.0.0.1:8009>, turn the global switch on, and add a time.

To authenticate the CLI, generate a token with `claude setup-token` and put it in
`.env` as `CLAUDE_CODE_OAUTH_TOKEN`. Without one the app still runs, but every
ping fails and the interface says so.

## Configuration

Everything is an environment variable. None of it is persisted to the database.

| Variable | Default | What it does |
|---|---|---|
| `PING_AUTH_USER` | — | Basic Auth user. Without it, the app denies everything. |
| `PING_AUTH_PASSWORD` | — | Basic Auth password. |
| `CLAUDE_CODE_OAUTH_TOKEN` | — | Your subscription token. Passed **only** to the subprocess: never stored, never returned by the API, never logged. |
| `PING_COMMAND` | `claude -p "ok"` | The command each ping runs. Split with `shlex` and executed **without a shell**: no pipes, no redirection. |
| `PING_TIMEOUT_SECONDS` | `120` | A ping that does not finish is killed and recorded as failed. |
| `SCHEDULER_TIMEZONE` | `UTC` | The times you configure are local to this zone (IANA name). |
| `HISTORY_LIMIT` | `200` | How many runs are kept. |
| `OUTPUT_EXCERPT_CHARS` | `2000` | How much output is stored per run. |

`PING_COMMAND` is configurable on purpose: if the CLI's authentication mechanism
changes, the command can be adjusted without rebuilding the image.

### About the token

- Read from the environment and injected **only** into the subprocess environment.
- Not written to SQLite, not returned by the API, not written to the logs: at
  startup only `present` or `MISSING` is recorded.
- Before a ping's output is stored it passes through `runner.redact()`, which
  replaces the token with `***` in case a future CLI version ever printed it in
  an error message.

## Disk usage

Both bounded, but for different reasons:

- **The database prunes itself.** Every write deletes everything older than the
  `HISTORY_LIMIT`-th newest row, so the table never exceeds that many rows. With
  the default limits the file settles around 400 KB. SQLite reuses freed pages
  rather than shrinking the file, so it plateaus — no `VACUUM` needed.
- **Container logs do not, by default.** The page polls the history every 30
  seconds per open tab and uvicorn logs every request, which adds up to roughly
  150 MB per year per open tab. The bundled `docker-compose.yml` caps this with a
  `json-file` rotation block; keep it if you write your own.

## Security

Basic Auth over HTTPS is the minimum worth having in front of this app: whoever
gets in can change when your window is consumed and fire pings at will. Put it
behind a reverse proxy with TLS, and behind a real identity proxy if you have one.

**It fails closed.** Without `PING_AUTH_USER` and `PING_AUTH_PASSWORD` the app
answers 503 to everything. If the secret never reaches the container, a useless
app beats an open one.

`/api/health` is the only public route. It has to be: if it demanded credentials,
the container healthcheck would fail and a healthcheck-gated rollout would roll
back in a loop.

Known limitations, stated plainly:

- **`POST /api/ping` is vulnerable to CSRF.** It takes no request body, so a
  cross-origin form submission is a CORS-"simple" request that skips the
  preflight, and the browser attaches cached Basic Auth credentials. A page you
  visit while logged in can fire a ping. The other mutating routes are protected
  incidentally: `PUT`, `DELETE` and the JSON-bodied `POST` all require a
  preflight that nothing answers.
- **The whole container environment is passed to the ping subprocess**, and
  `redact()` strips only the token. If a future CLI version dumped its
  environment on error, `PING_AUTH_PASSWORD` could land in the stored excerpt.
- **No rate limiting on Basic Auth.** There is nothing here to slow down a
  brute-force attempt; put it behind something that does.

Anyone who can set environment variables on the container can run an arbitrary
binary inside it via `PING_COMMAND`. That is by design, and is no more than
container access already implies.

## Development

```bash
make run-app     # http://127.0.0.1:8009 (login: dev / dev)
make app-logs
make app-stop
```

Without Docker:

```bash
uv venv && uv pip install -r requirements.txt
ln -s ../frontend backend/static          # the Dockerfile does this as a copy
PING_AUTH_USER=dev PING_AUTH_PASSWORD=dev \
  PING_COMMAND='echo ok' \
  .venv/bin/uvicorn backend.main:app --reload
```

The frontend is plain HTML, CSS and JavaScript: no build step, no dependencies,
no `node_modules`. The Dockerfile copies it as-is into `backend/static/`.

The image bundles Node 22 and a version-pinned CLI (`CLAUDE_CODE_VERSION` in the
`Dockerfile`); bumping it is a deliberate commit, not a silent rebuild.

## Disclaimer

This is an independent personal project. It is **not affiliated with, endorsed
by, or sponsored by Anthropic.** "Claude" and "Claude Code" are trademarks of
Anthropic PBC; they appear here only to describe, accurately and in plain text,
what the default command runs.

This tool does not modify, bundle or redistribute the Claude Code CLI — it
installs the official npm package and invokes it unmodified — and it never
collects or intermediates anyone's credentials: you supply your own token, it
stays in your own container, and it is passed only to the subprocess.

Your use of the CLI remains subject to
[Anthropic's Usage Policy](https://www.anthropic.com/legal/aup) and Terms of
Service, under which advertised plan limits "assume ordinary, individual usage".
Scheduling automated invocations is your decision and your responsibility; see
[Anthropic's legal and compliance documentation](https://code.claude.com/docs/en/legal-and-compliance)
and satisfy yourself that your usage fits your own agreement before deploying it.

## License

[MIT](LICENSE)
