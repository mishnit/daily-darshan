# Daily Darshan

A minimal, near-zero-infrastructure platform that delivers a daily "darshan" image to
WhatsApp subscribers. It uses **GitHub** as source control + persistence + image storage,
**CSV** files as the datastore, **GitHub Actions** as the scheduler, and a small
**serverless FastAPI** app for WhatsApp webhook interaction.

Built to Tech Doc v2.0 using a lightweight **Domain-Driven Design / Clean Architecture**
approach.

---

## Table of Contents

1. [Architecture & Separation of Concerns](#architecture--separation-of-concerns)
2. [Repository Layout](#repository-layout)
3. [Local Development](#local-development)
4. [Configuration](#configuration)
5. [Secrets](#secrets)
6. [Deployment](#deployment)
7. [Subscriber Conversation Flow](#subscriber-conversation-flow)
8. [Scheduled Jobs](#scheduled-jobs)
9. [Admin Operations](#admin-operations)
10. [Testing](#testing)
11. [Code Cleanliness & Maintainability](#code-cleanliness--maintainability)
12. [Extending the System](#extending-the-system)
13. [Cost Model](#cost-model)

---

## Architecture & Separation of Concerns

The codebase is organized into concentric layers. **Dependencies point inward only** —
the domain knows nothing about infrastructure, and business logic depends on abstract
*ports*, never on concrete adapters.

```
┌──────────────────────────────────────────────────────────────┐
│  Entry points        main.py (webhook)   scheduler.py (jobs)   │
│                      config.py (composition root / wiring)     │
├──────────────────────────────────────────────────────────────┤
│  Adapters            WhatsApp · GitHub · Image sources         │  infrastructure
│  (implement ports)   CSV repositories                          │
├──────────────────────────────────────────────────────────────┤
│  Ports (interfaces)  application/ports/*                        │  boundaries
├──────────────────────────────────────────────────────────────┤
│  Application         PaymentService · SubscriberService ·       │  use cases
│  services            DeliveryService · ImageService ·           │
│                      RenewalReminderService                     │
├──────────────────────────────────────────────────────────────┤
│  Domain              Subscriber · Payment · Image · enums       │  business rules
└──────────────────────────────────────────────────────────────┘
```

| Layer | Package | Responsibility | May depend on |
|-------|---------|----------------|---------------|
| **Domain** | `domain/` | Pure business objects and rules (state machine, reference-id format, UPI intent, eligibility, image validation rules). No I/O. | stdlib only |
| **Ports** | `application/ports/` | Abstract interfaces (`ABC`) for persistence, WhatsApp, image sources, GitHub. | `domain` |
| **Application** | `application/` | Use-case orchestration. Coordinates domain objects through ports. | `domain`, `application.ports` |
| **Adapters** | `adapters/`, `repositories/` | Concrete implementations of ports: Meta WhatsApp HTTP, GitHub (git CLI / REST), image-source scrapers, CSV persistence. | `domain`, `application.ports` |
| **Composition root** | `config.py` | The single place adapters are bound to ports (`Container`). | everything |
| **Entry points** | `main.py`, `scheduler.py` | Thin I/O shells (HTTP webhook, CLI). Contain no business logic. | `config`, `application` |

**Why this matters:** business logic (`application/` + `domain/`) is fully unit-testable
with in-memory fakes and has zero knowledge of WhatsApp, GitHub, HTTP, or CSV. Swapping
CSV for a database, or Meta for another WhatsApp provider, means writing one new adapter —
no changes to the core.

---

## Repository Layout

```
daily-darshan/
├── main.py                     # FastAPI WhatsApp webhook (serverless entry point)
├── scheduler.py                # CLI entry point for GitHub Actions jobs
├── config.py                   # Composition root: loads config.json, wires Container
├── config.json                 # Non-secret configuration
│
├── domain/                     # Business objects & rules (no I/O)
│   ├── enums.py                #   statuses, reminder types, domain errors
│   ├── subscriber.py           #   Subscriber lifecycle state machine
│   ├── payment.py              #   Payment, reference-id, UPI-intent, UTR rules
│   └── image.py                #   Image value object + canonical path
│
├── application/                # Use-case services
│   ├── ports/                  #   interfaces the core depends on
│   │   ├── repositories.py
│   │   ├── whatsapp.py
│   │   └── storage.py
│   ├── payment_service.py
│   ├── subscriber_service.py
│   ├── delivery_service.py
│   ├── image_service.py        #   ImageCollector + ImageService
│   └── renewal_reminder_service.py
│
├── repositories/               # CSV implementations of repository ports
│   ├── csv_repository.py       #   generic atomic CSV primitive
│   ├── subscriber_repository.py
│   ├── payment_repository.py
│   ├── sentlog_repository.py
│   ├── renewal_repository.py
│   └── log_repository.py
│
├── adapters/                   # External-system adapters
│   ├── whatsapp.py             #   Meta WhatsApp Cloud API client
│   ├── github.py               #   LocalGitRepository + GitHubApiRepository
│   └── image_sources/
│       ├── http_source.py      #   shared download base
│       ├── temple_source.py
│       ├── rss_source.py
│       ├── website_source.py
│       └── validator.py        #   ImageValidator (Pillow)
│
├── csv/                        # Datastore (committed to Git)
│   ├── subscribers.csv
│   ├── payments.csv
│   ├── sentlog.csv
│   ├── renewals.csv
│   └── logs.csv
├── images/                     # images/YYYY-MM-DD.jpg (committed to Git)
│
├── tests/                      # pytest unit tests + fakes
└── .github/workflows/
    ├── image.yml               # daily image fetch (08:00 IST)
    └── delivery.yml            # renewal reminders + daily delivery
```

---

## Local Development

Requires **Python 3.11+**.

```bash
cd daily-darshan

# create an isolated environment
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# install runtime + test dependencies
pip install -r requirements.txt pytest

# run the test suite
pytest -q

# run the webhook locally
uvicorn main:app --reload --port 8000

# run a scheduled job locally (uses config.json + env vars)
python scheduler.py image        # fetch & store today's image
python scheduler.py delivery     # deliver to eligible subscribers
python scheduler.py renewal      # send renewal reminders
python scheduler.py all          # renewal + image + delivery
python scheduler.py delivery --date 2026-08-19   # override the date
```

> The scheduler commits CSV/image changes via git. Run it inside a checked-out repo with
> a configured git author (GitHub Actions does this automatically — see below).

---

## Configuration

All **non-secret** settings live in `config.json`. Nothing here is confidential, so it is
safe to commit. Load order: `DAILY_DARSHAN_CONFIG` env var → `config.json` (default).

| Key | Purpose |
|-----|---------|
| `plans` | Plan catalog: `{ "<plan>": { "amount": <int>, "days": <int> } }`. Drives pricing, UPI amount, and subscription length. |
| `upi` | `payee_vpa`, `payee_name`, `currency` used to build the UPI intent string. |
| `image_sources` | Ordered list of source keys defining **fallback priority** (e.g. `["temple","rss","website"]`). |
| `image_source_config` | Per-source settings (URLs). Only sources present here **and** listed in `image_sources` are wired. |
| `image_validation` | `min_width`, `min_height`, `allowed_formats` for `ImageValidator`. |
| `paths` | Relative paths to the CSV files and `images/` directory. |
| `schedule` | Cron hints (documentation; actual cron lives in the workflow YAML). |
| `renewal.reminder_days` | Days-before-expiry to send reminders, e.g. `[3, 1]`. |
| `persistence` | Webhook durability. `mode`: `github_api` (webhook syncs CSVs to the shared repo via Contents API — needs `GITHUB_TOKEN`+`GITHUB_REPO`) or `local` (no sync; dev only). `branch`: repo branch to sync against. |
| `delivery` | Delivery mode + message settings. `mode`: `utility_template` (send a parameterized utility template linking to a per-subscriber page) or `image` (send the image inline). `template_name`, `template_lang`, `page_base_url` (base for the per-subscriber URL), `pages_dir` (GitHub Pages source dir), `image_public_base` (public base URL for images), `caption` (`{date}` placeholder, image mode), `max_send_retries`. |

### Common config changes

- **Change a price or plan length** — edit `plans.<plan>.amount` / `.days`. No code change.
- **Add a new plan** — add a `plans` entry; it becomes selectable in the webhook automatically.
- **Reorder / disable image sources** — edit the `image_sources` array. Remove a key to
  disable it; reorder to change fallback priority.
- **Point a source at a real site** — edit `image_source_config.<source>.*` URLs.
- **Change reminder cadence** — edit `renewal.reminder_days` (only `3` and `1` are mapped
  to reminder types today; see [Extending](#extending-the-system) to add more).
- **Change delivery caption** — edit `delivery.caption`.
- **Switch delivery mode** — set `delivery.mode`:
  - `utility_template` — sends an approved WhatsApp **utility template** whose `{{2}}` is a
    per-subscriber page URL (`page_base_url/<subscription_id>`); the darshan image lives on a
    static GitHub Pages page. ~7× cheaper than marketing **if Meta classifies the template as
    Utility** (not guaranteed — see the caveat in [DEPLOYMENT.md](./DEPLOYMENT.md)). Requires
    `template_name`, `page_base_url`, `pages_dir`, `image_public_base` and GitHub Pages enabled.
  - `image` — sends the image inline (Meta media upload, private-repo safe). Higher engagement,
    billed as Marketing.
- **Adjust the schedule** — edit the `cron` in `.github/workflows/*.yml` (the source of
  truth), and optionally mirror it in `config.json.schedule` for documentation.

After changing `config.json`, run `pytest -q` and commit. No redeploy of the scheduler is
needed — GitHub Actions checks out the latest `config.json` on every run. The **webhook**
process caches config at startup, so redeploy/restart it to pick up changes.

---

## Secrets

Secrets are **never** stored in `config.json` or committed to Git. They are read from
environment variables (Tech Doc §19).

| Variable | Used by | Notes |
|----------|---------|-------|
| `WHATSAPP_ACCESS_TOKEN` | WhatsApp adapter | Meta Cloud API token. |
| `WHATSAPP_PHONE_NUMBER_ID` | WhatsApp adapter | Meta phone-number id. |
| `WEBHOOK_VERIFY_TOKEN` | `main.py` GET `/webhook` | Meta webhook verification handshake. |
| `WHATSAPP_APP_SECRET` | `main.py` POST `/webhook` | Meta app secret; verifies `X-Hub-Signature-256` on inbound webhooks. If unset, signature checks are skipped (dev only) — **set it in production**. |
| `GITHUB_TOKEN` | webhook durable persistence + `GitHubApiRepository` | Contents-API reads/writes so the webhook shares state with the scheduler (**required in production** with `persistence.mode=github_api`). In Actions, the built-in token + `contents: write` suffices. |
| `GITHUB_REPO` | webhook persistence + scheduler | `owner/repo`. Used for the webhook's Contents-API sync and to build the public raw image URL. Auto-set in Actions via `${{ github.repository }}`; **set explicitly on the webhook host**. |
| `DAILY_DARSHAN_CONFIG` | `config.py` | Optional path override for `config.json`. |

- **GitHub Actions:** add secrets under *Settings → Secrets and variables → Actions*.
- **Serverless host:** set them as environment variables in the platform dashboard.
- Locally, export them in your shell or use a `.env` (already git-ignored) — do **not** commit it.

---

## Deployment

Two independent deployables:

### 1. Webhook API (`main.py`)

A stateless FastAPI app. Deploy to any free-tier serverless/host that runs Python (Vercel,
Render, Fly, Cloudflare with a Python runtime, etc.).

```bash
# production run
uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}
```

**Ready-made deploy files (in this repo):**

| File | Use |
|------|-----|
| `Dockerfile` | Portable container image (Python 3.11-slim, non-root user, single uvicorn worker). Works on Fly, Railway, Render, ECS, etc. |
| `.dockerignore` | Keeps the build context small (excludes `.git`, `.venv`, tests, caches). |
| `render.yaml` | Render Blueprint: free web service, `numInstances: 1`, health check on `/health`, secrets declared with `sync: false` (set values in the dashboard). |
| `fly.toml` | Fly.io config: `min_machines_running = 1`, HTTPS forced, `/health` check, `sin` region. |

Build/run the container locally:

```bash
docker build -t daily-darshan-webhook .
docker run -p 8000:8000 --env-file .env daily-darshan-webhook
# verify: curl localhost:8000/health  ->  {"status":"ok"}
```

Deploy with Fly:

```bash
fly launch --no-deploy      # first time only; keep the generated app name in fly.toml
fly secrets set WHATSAPP_ACCESS_TOKEN=... WHATSAPP_PHONE_NUMBER_ID=... \
                WEBHOOK_VERIFY_TOKEN=... GITHUB_TOKEN=... GITHUB_REPO=owner/repo
fly deploy
```

> **Run a single instance.** Persistence is CSV-in-Git; multiple webhook instances would
> risk concurrent Git writes. `render.yaml` and `fly.toml` are both pinned to one instance.

Endpoints:
- `GET /health` — liveness probe.
- `GET /webhook` — Meta verification handshake (`hub.mode`/`hub.verify_token`/`hub.challenge`).
- `POST /webhook` — inbound WhatsApp messages.

Configure the endpoint URL + `WEBHOOK_VERIFY_TOKEN` in the Meta app dashboard, and set the
WhatsApp secrets as environment variables on the host.

> **Webhook durability & shared state (important).** The webhook runs on an ephemeral,
> single-instance host and writes CSVs to local disk. To make those writes **durable and
> visible to the scheduler/admin**, it syncs with the shared GitHub repo via the Contents
> API: it **pulls** the latest tracked CSVs before handling a message and **pushes** them
> after. This is enabled only when `persistence.mode = "github_api"` (in `config.json`) **and**
> both `GITHUB_TOKEN` and `GITHUB_REPO` are set in the environment. If not configured, the
> webhook writes local-only (fine for dev, **but on an ephemeral host those writes are lost on
> restart and never reach the scheduler** — so set the token + repo in production). The
> scheduler itself commits via `git` directly, so it does not need this sync.
>
> **`/health` is a real readiness probe.** It returns **200** `{"status":"ok"}` when the app
> initialized and the store is readable; **503** `{"status":"unhealthy"}` (with a reason) if
> the container failed to build, or `{"status":"degraded"}` if the store is unreadable. It also
> reports whether durable persistence and signature verification are enabled. A bad config no
> longer crashes the process — the app starts and `/health` reports the failure so the platform
> can react. The webhook always returns **200** to Meta (even on a malformed body or a
> per-message handler error) so Meta does not hammer retries; failures are logged and isolated
> per message.
>
> **Ack-fast, process async.** After verifying the signature and parsing the body, the webhook
> **returns `200 {"status":"accepted"}` immediately** and does the slow work — the repo
> pull/push and WhatsApp API calls — in a **background task** off the response path. This keeps
> Meta's webhook latency near-instant regardless of how slow the downstream calls are, so Meta
> never times out and retries. (Forged/unsigned and malformed requests are still rejected
> synchronously before the ack.) Note: background tasks run in-process — if the instance is
> killed mid-task the in-flight message is dropped; Meta's own retry and the `message.id`
> dedupe mitigate this, and a durable queue is the next step for higher guarantees.

Steps:
  1. Push the repo to GitHub.
  2. Render → New → Web Service, connect the repo.
  3. Build command: pip install -r requirements.txt
  4. Start command: uvicorn main:app --host 0.0.0.0 --port $PORT
  5. Add environment variables (secrets, per the README): WHATSAPP_ACCESS_TOKEN, WHATSAPP_PHONE_NUMBER_ID, WEBHOOK_VERIFY_TOKEN, WHATSAPP_APP_SECRET, and GITHUB_TOKEN + GITHUB_REPO (required for durable persistence).
  6. Take the assigned HTTPS URL and register .../webhook in the Meta app dashboard with the same WEBHOOK_VERIFY_TOKEN.
  
Container route (portable across Fly/Railway/Render/ECS)
  
  FROM python:3.11-slim
  WORKDIR /app
  COPY requirements.txt .
  RUN pip install --no-cache-dir -r requirements.txt
  COPY . .
  CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
  
Production considerations
  
  - Concurrency: start with 1–2 workers; the webhook is I/O-light. -w $(($(nproc)*2+1)) is a common formula if you scale up.
  - HTTPS: required by Meta. The managed hosts terminate TLS for you.
  - Secrets: set via the platform's env-var UI, never committed (already git-ignored).
  - Health check: point the platform's health check at GET /health (already implemented).
  - Statelessness caveat for this app: persistence is CSV-in-Git, and the webhook caches config.json at startup. If you scale to multiple instances, concurrent Git writes from several webhook processes can conflict (the Tech Doc's "avoid concurrent writers" note). For the MVP, run a single instance for the webhook and let GitHub Actions handle the scheduled writes.

### 2. Scheduled jobs (`scheduler.py`)

Run entirely by **GitHub Actions** — no server required. The workflows check out the repo,
install deps, run tests, execute the job, and commit results back.

To enable:
1. Push this repository to GitHub.
2. Add the Actions secrets listed above.
3. Ensure workflow permissions allow writes: the YAMLs already declare `permissions: contents: write`. Also confirm *Settings → Actions → General → Workflow permissions* is set to **Read and write**.
4. The workflows run on their cron schedules and can be triggered manually via
   **workflow_dispatch** (Actions tab → *Run workflow*) for recovery/testing.

---

## Subscriber Conversation Flow

The webhook (`main.py`) runs a small **CTA-driven** conversation. All selections are made by
**tapping interactive buttons / list options** (WhatsApp interactive messages), so the bot
never guesses intent from free text. **Free text is accepted only for the user's name and the
12-digit UTR; the phone number is implicit (the message sender).**

```
User: (any message, e.g. "hi")
Bot:  🙏 Welcome to Daily Darshan! What would you like to do?
      [ Subscribe ]  [ Renew ]                         ← reply buttons (CTA ids)
User: (taps Subscribe)
Bot:  Choose your Daily Darshan plan:                   ← list message
      • Monthly   — ₹49 · 30 days
      • Quarterly — ₹129 · 90 days
      • Yearly    — ₹449 · 365 days
User: (taps Quarterly)
Bot:  🙏 What name should we greet you by?               ← asked only if no name yet
User: Deep                                              ← free text (name)
Bot:  Namaste Deep! Plan: quarterly
      Amount: ₹129
      Pay via UPI: upi://pay?...
      Reference: DD2608190001
      After paying, reply with your 12-digit UTR.
User: 123456789012                                      ← free text (UTR)
Bot:  Thanks! We received your UTR. Your subscription
      activates once an admin verifies the payment.
```

Returning subscriber:

```
User: (taps Renew)
Bot:  Namaste Deep! Renewing your quarterly plan.        ← existing plan, no name prompt
      Amount: ₹129
      Pay via UPI: upi://pay?...
      Reference: DD2608190002
      After paying, reply with your 12-digit UTR.
```

Details:
- **Selections are buttons, not typed commands.** Inbound taps arrive as interactive
  `button_reply`/`list_reply` **ids**; routing is on stable ids: `CTA_SUBSCRIBE` → plan list,
  `CTA_RENEW` → renew, `PLAN_<plan>` → chosen plan. Typing a plan word (e.g. "how much is
  yearly?") **never** starts a subscription — it just re-shows the menu. This removes a class
  of accidental-signup / wrong-plan bugs from free-text parsing.
- **Free text is limited to name and UTR.** When the bot is awaiting a name, the next text is
  stored as the name (a 12-digit value is treated as a UTR, never a name; a blank re-prompts).
  A 12-digit message is recorded as the UTR against the latest pending payment. Any other
  typed text shows the CTA menu.
- **Name capture is explicit** (WhatsApp profile name is unreliable). If the inbound webhook
  already carries a profile name, the prompt is skipped and that name is used.
- **RENEW is distinct from SUBSCRIBE.** Tapping Renew uses the subscriber's **existing plan**
  (not the default), greets by stored name, no name prompt. On admin verification, renewal
  **extends from the current expiry date** (not from today) so remaining days are never lost
  (Tech Doc §29). Renew from an unknown mobile falls back to the plan list.
- The awaiting-name state is a flag on the subscriber row (`subscribers.csv`), so it survives
  across webhook calls without server-side session state.
- Duplicate/re-delivered webhooks are deduped on the WhatsApp `message.id`, so re-taps/re-sends
  don't re-prompt or create duplicate payments.
- Activation remains admin-verified out-of-band (see Admin Operations); the name/plan captured
  here is what later fills the daily utility template and the per-subscriber page greeting.

> **WhatsApp note:** interactive buttons/list messages are free-form inside the 24-hour
> user-initiated window. To send the initial menu to a user who hasn't messaged in 24h, use an
> approved template with buttons; within the window (the normal case, since the user just
> messaged) the free-form interactive menu is used.

---

## Scheduled Jobs

| Workflow | Schedule (UTC) | Local time | Does |
|----------|----------------|------------|------|
| `image.yml` | `30 2 * * *` | 08:00 IST | Fetch → validate → store `images/YYYY-MM-DD.jpg`, then **(re)generate every subscriber's page** → commit. Image storage is idempotent (skips if a valid image already exists), but pages are regenerated **every run** so newly-signed-up subscribers get a page. |
| `delivery.yml` | `0 3 * * *` | 08:30 IST | **Expire lapsed subscriptions** (ACTIVE past `end_date` → `EXPIRED`), send renewal reminders, then deliver today's darshan to eligible subscribers → update CSVs → commit. |

**Idempotency** (safe to re-run):
- Delivery keys on `date + mobile` in `sentlog.csv` (only `SENT` rows block re-send).
- Renewal reminders key on `mobile + reminder_type + expiry_date` in `renewals.csv`.
- The **expiry sweep** only transitions `ACTIVE` subscribers whose `end_date` has passed; an
  already-`EXPIRED` subscriber is skipped, so re-runs are safe. `PAUSED` (intentional hold)
  and `CANCELLED` (terminal) are never auto-expired.
- Image collection skips replacement when a valid dated image already exists — **but page
  generation still runs**, so a subscriber who joined after the image was stored still gets
  their page on the next image run.

**Subscription expiry.** Eligibility is date-gated (an expired subscriber is excluded from
delivery/reminders regardless of stored status), but the `delivery.yml` workflow also runs an
**expiry sweep** (`scheduler.py expiry`) before renewal/delivery that flips the stored status
`ACTIVE → EXPIRED` once `end_date` has passed, keeping reports and admin views truthful. A
subscriber expiring exactly today (`end_date == today`) is still active — expiry applies from
the day after. Renewal reactivates an `EXPIRED` subscriber (`EXPIRED → ACTIVE`, extending
dates).

**Page timing (utility-template mode).** Each subscriber's page lives at
`docs/<subscription_id>/index.html` and is the target of the utility-template link. Pages are
produced in two places so a subscriber's branded URL is never a 404 when they receive it:
1. The daily **image job** regenerates all pages every run (even if the image already exists).
2. **Activation** (`admin.py verify --activate`) renders that subscriber's page immediately,
   so a mid-day signup gets a working URL without waiting for the next image job.
> Note: GitHub Pages takes ~1 minute to publish a commit, so a page is reachable shortly
> after the commit that creates it, not instantaneously.

**Fault tolerance:** image sources are tried in priority order; a failing source falls
through to the next. WhatsApp sends use bounded retries; a failure for one subscriber does
not stop the batch. Git pushes retry once via `pull --rebase` and never force-push.

### Coordination between the two machines

The webhook (Render) and the scheduler/admin (GitHub Actions) never talk to each other
directly. The **GitHub repo `main` branch is the shared source of truth**; both sides read
and write the same CSVs there:

- **Webhook** uses the GitHub **Contents API** (`GitHubApiRepository` via `RepoSync`): it
  **pulls** the tracked CSVs before handling a message and **pushes** them after.
- **Scheduler/admin** uses the **git CLI** on the checked-out repo (`LocalGitRepository`):
  it commits + pushes (retry once via `pull --rebase`, never force-push).

Because both write CSVs on `main`, two mechanisms keep them from clobbering each other:

1. **Writer separation + safe expiry.** The webhook and scheduler mostly write different
   files. The one true overlap is `subscribers.csv` (webhook opt-in vs. the nightly expiry
   sweep). `sweep_expired` therefore **re-reads each subscriber row fresh right before
   flipping status** and only changes the status field, so a subscriber the webhook added or
   updated concurrently is preserved rather than overwritten by a stale snapshot. `logs.csv`
   is **append-only**, so log rows from both sides merge without row-level conflicts.

2. **Defer-push quiet window.** During the nightly job window the webhook **defers its
   pushes** so it never writes on top of an in-flight scheduler commit. The window is
   configured in `config.json` under `persistence.quiet_window_utc` (default `02:25`–`03:10`
   UTC, bracketing the 02:30 image and 03:00 delivery jobs). While inside the window, webhook
   writes stay on local disk and are **flushed by the first push after the window closes**;
   pulls are always allowed so the webhook keeps reading fresh state.

> This is coordination by convention (staggered timing + single-writer + rebase-retry), not a
> transactional database. It suits the low write volume of a darshan service. At higher write
> rates, move state to a real datastore (SQLite on a persistent volume, or a hosted DB).

---

## Admin Operations

Payment verification is intentionally **out-of-band** — a submitted UTR is *not* proof of
payment (Tech Doc §6/§15).

- **Verify a payment (recommended, one step):** after confirming the real UPI transaction,
  run the admin CLI to mark the payment `SUCCESS` and activate the subscriber:
  ```bash
  python admin.py list-pending                          # see what's awaiting verification
  python admin.py verify DD2608190001 --activate --commit
  ```
  Omit `--commit` to review CSVs first; omit `--activate` to only verify. Use
  `python admin.py reject DD2608190001` for a non-matching payment. See
  [DEPLOYMENT.md](./DEPLOYMENT.md#step-by-step-approval) for the full runbook.
- **Verify a payment (manual):** open `csv/payments.csv`, find the row by
  `reference_id`/`utr`, confirm the actual UPI transaction, set `status` to `SUCCESS`,
  commit. Then activate the subscriber via the subscriber service.
- **Override the daily image:** replace `images/YYYY-MM-DD.jpg` and commit.
- Git history serves as the audit trail for all of the above.

---

## Testing

```bash
pytest -q          # all tests
pytest tests/test_renewal.py -q   # a single file
```

- **48 unit tests** cover Tech Doc §20 and §30: reference-id generation, UPI intent, UTR
  validation, subscriber state transitions, eligibility, image validation, source
  fallback, duplicate-delivery prevention, retry logic, and renewal-reminder rules.
- Tests use **in-memory fakes** (`tests/conftest.py`: `FakeWhatsApp`, `FakeSource`) and
  **real CSV repositories in a temp directory**, so they run fast with no network or
  external services.
- Image-validation tests auto-skip if Pillow is unavailable.

Both GitHub Actions workflows run `pytest` before executing their job, so a failing test
blocks image collection / delivery.

---

## Code Cleanliness & Maintainability

Principles this codebase follows:

- **Dependency inversion via ports.** Business logic imports only `domain` and
  `application.ports`. Concrete adapters (`adapters/`, `repositories/`) implement those
  ports and are wired in exactly one place: `config.py`'s `Container`. To trace how a
  dependency is satisfied, look in the composition root.
- **Single Responsibility per module.** Each service owns one use case; each adapter wraps
  one external system; each repository persists one entity. Files are small and focused
  (most under ~150 lines).
- **Pure domain layer.** `domain/` has no I/O and no framework imports, making rules
  trivial to test and reason about. Entities expose `from_row` / `to_row` so persistence
  mapping lives with the entity, not scattered across repositories.
- **Thin entry points.** `main.py` and `scheduler.py` only translate I/O (HTTP / CLI) into
  service calls; they contain no business rules. This keeps the framework replaceable.
- **Explicit boundaries and types.** Ports are `ABC`s; results use small dataclasses
  (`WhatsAppResult`, `DeliveryReport`, `ReminderReport`) instead of loose tuples/dicts.
- **Safe persistence.** `CSVRepository` writes atomically (temp file + `os.replace`) to
  reduce corruption risk; the GitHub adapter never force-pushes and retries on conflict.
- **Collision-free reference ids.** Payment reference ids (`DD` + `YYMMDD` + 4-digit
  sequence) are allocated safely even under concurrent writers: `CSVRepository.append_unique`
  runs the read-check-append cycle under an exclusive OS file lock (`flock`), and
  `PaymentService.create_payment` retries with a fresh sequence on `DuplicateKeyError`.
  Reads tolerate a mid-`os.replace` snapshot, so no crash or duplicate id is possible.
  Verified with a 6-process stress test (120 concurrent creates → 120 unique ids).
- **Constructor injection, no globals.** Services receive their collaborators as
  constructor arguments — no hidden singletons — which is what makes the fakes in tests
  possible.
- **Idempotency and structured logging** are first-class (`logs.csv` events, dedupe keys),
  which keeps operations debuggable and re-runnable.

### Conventions for contributors

- Never import an `adapters.*` module from `domain/` or `application/`. Depend on a port.
- Add new wiring only in `config.py`.
- Keep secrets in environment variables; keep tunables in `config.json`.
- Add/extend tests alongside any new rule or use case; run `pytest -q` before committing.
- Match existing style: type hints, `from __future__ import annotations`, dataclasses for
  value/result objects, docstrings referencing the relevant Tech Doc section.

---

## Extending the System

- **New image source:** subclass `HttpImageSource` (implement `resolve_url`), register it
  in `config.py._build_sources`, and add its config block + `image_sources` entry.
- **New persistence backend (e.g. SQLite/DynamoDB):** implement the repository ports in a
  new adapter and swap them in `Container`. No service/domain changes.
- **New WhatsApp provider:** implement `WhatsAppClientPort` and bind it in `Container`.
- **More reminder offsets:** extend `ReminderType.for_days_remaining` (and its mapping) in
  `domain/enums.py`, then add `reminder_days` values in `config.json`.
- **New scheduled job:** add a function in `scheduler.py` and a workflow YAML; keep the
  business logic in an application service.

---

## Cost Model

Target infrastructure cost is **₹0** while chosen services stay within free tiers: GitHub
(repo + Actions), a free-tier serverless host for the webhook. Potential paid items:
WhatsApp Business/API messaging charges, an optional custom domain, and any usage beyond
free-tier limits. Re-check provider free-tier limits before production.
