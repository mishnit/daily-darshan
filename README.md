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
├── docs/images/                # canonical + source images (committed to Git/Pages)
│
├── tests/                      # pytest unit tests + fakes
└── .github/workflows/
    ├── image.yml               # daily image fetch (08:31 IST target)
    ├── delivery.yml            # renewal + delivery after successful Pages publication
    ├── pages.yml               # manual page regeneration
    └── deploy-pages.yml        # publish docs/ once after a successful image workflow
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
python scheduler.py cleanup      # retain the latest 30 days of operational logs
python scheduler.py all          # cleanup + image + expiry + renewal + delivery
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
| `daily_image_rotation` | Weekday-to-source mapping. Multiple sources on a day are all downloaded; the largest valid result becomes canonical. |
| `temple_sources` | Named temple page URLs and `enabled` flags used by the weekday rotation. |
| `image_sources` / `image_source_config` | Legacy generic source fallback used only when no enabled named temple sources are configured. |
| `image_validation` | `min_width`, `min_height`, `allowed_formats` for `ImageValidator`. |
| `paths` | Relative paths to the CSV files and `images/` directory. |
| `schedule` | Image cron hint (documentation; the actual cron lives in `image.yml`). Pages publication and delivery are event-driven. |
| `renewal.reminder_days` | Days-before-expiry to send reminders, e.g. `[3, 2, 1]`. |
| `renewal.whatsapp_number` | Digits-only WhatsApp destination used by the near-expiry page CTA. |
| `persistence` | Webhook durability. `mode`: `github_api` (snapshot reads and atomic Git Data API commits — needs `GITHUB_TOKEN`+`GITHUB_REPO`) or `local` (no sync; dev only). `branch`: repo branch to sync against. |
| `delivery` | Delivery mode + message settings. `mode`: `utility_template` (send a parameterized utility template linking to a per-subscriber page) or `image` (send the image inline). Also controls template language, page/image URLs, retries, 30-day operational-log retention, image retention and page-retention grace. |

### Common config changes

- **Change a price or plan length** — edit `plans.<plan>.amount` / `.days`. No code change.
- **Add a new plan** — add a `plans` entry; it becomes selectable in the webhook automatically.
- **Change the weekday rotation** — edit `daily_image_rotation.<weekday>`. When a weekday
  lists multiple sources, the job stores every valid candidate and selects the largest.
- **Enable, disable or repoint a temple** — edit `temple_sources.<source>`.
- **Change reminder cadence** — edit `renewal.reminder_days` (`3`, `2`, and `1` are mapped
  to reminder types today; see [Extending](#extending-the-system) to add more).
- **Change delivery caption** — edit `delivery.caption`.
- **Change operational-log retention** — edit `delivery.log_retention_days` (currently `30`).
- **Switch delivery mode** — set `delivery.mode`:
  - `utility_template` — sends an approved WhatsApp **utility template** whose dynamic URL
    button receives the subscription ID and resolves to `page_base_url/<subscription_id>`; the darshan image lives on a
    static GitHub Pages page. Billing depends on Meta's assigned category and current rate;
    verify both in WhatsApp Manager (see [DEPLOYMENT.md](./DEPLOYMENT.md)). Requires
    `template_name`, `page_base_url`, `pages_dir`, `image_public_base` and GitHub Pages enabled.
  - `image` — sends the image inline (Meta media upload, private-repo safe). Higher engagement,
    subject to Meta's current category and pricing rules.
- **Adjust the schedule** — edit the `cron` in `.github/workflows/image.yml` (the source of
  truth), and optionally mirror it in `config.json.schedule` for documentation. A successful
  image run publishes Pages once; delivery starts only after that publication succeeds.

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
| `WHATSAPP_APP_SECRET` | `main.py` POST `/webhook` | Meta app secret; verifies `X-Hub-Signature-256` on inbound webhooks. Local mode can omit it; `github_api` production mode fails closed when it is absent. |
| `GITHUB_TOKEN` | webhook durable persistence + `GitHubApiRepository` | Snapshot reads and atomic Git commits; requires Contents read/write (**required in production** with `persistence.mode=github_api`). In Actions, the built-in token + `contents: write` suffices. |
| `GITHUB_REPO` | webhook persistence + scheduler | `owner/repo`. Used for webhook GitHub sync and public raw image URLs. Auto-set in Actions via `${{ github.repository }}`; **set explicitly on the webhook host**. |
| `GPG_PRIVATE_KEY` | GitHub Actions scheduler | **Required GitHub Actions secret** containing the ASCII-armored private key used to sign scheduler commits. Workflows fail rather than create unsigned commits if it is unavailable. Add the matching public key to the GitHub account so commits are marked Verified. |
| `GPG_PASSPHRASE` | GitHub Actions scheduler | **Required GitHub Actions secret** that unlocks `GPG_PRIVATE_KEY` through non-interactive loopback/preset pinentry. The workflow performs a signing check before scheduled work. |
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
> visible to the scheduler/admin**, it reads CSVs at one immutable Git commit and uses the
> Git Data API to publish an atomic multi-file commit. It **pulls** before handling and **pushes**
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
> can react. Processing, persistence and reply failures return **503** to request redelivery;
> successful messages within a partially failed batch remain deduplicated. Invalid signatures
> return **403**; malformed JSON is acknowledged and ignored without executing actions.
>
> **Persist before acknowledgement.** Processing runs in a worker thread under a cross-process
> local state lock, but HTTP **200** is returned only after the GitHub commit succeeds. A process
> failure before commit leaves the request unacknowledged. This trades latency for durability;
> slow GitHub/WhatsApp calls can cause redelivery. Interactive replies are not exactly-once.
> A durable queue remains the recommended upgrade for higher throughput. Production fails
> closed when persistence is unavailable. If a legacy quiet window is enabled, it returns 503
> before handling rather than accepting ephemeral deferred writes.

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
3. Ensure workflow permissions allow writes: the scheduler YAMLs declare `permissions: contents: write`. Also confirm *Settings → Actions → General → Workflow permissions* is set to **Read and write**.
4. The image workflow runs on its cron schedule. A successful image run automatically triggers
   one Pages deployment, which triggers delivery only after publication succeeds. Image, Pages
   deployment, and delivery also support **workflow_dispatch** for recovery/testing.
5. Under *Settings → Pages → Build and deployment → Source*, select **GitHub Actions**. The
   `Deploy Daily Darshan Pages` workflow then publishes `docs/` exactly once after each successful
   `Daily Image` workflow, instead of the legacy branch publisher rebuilding on every commit.

---

## Subscriber Conversation Flow

The webhook (`main.py`) runs a small **CTA-driven** conversation. All selections are made by
**tapping interactive buttons / list options** (WhatsApp interactive messages), so the bot
never guesses intent from free text. **Free text is accepted only for the user's name and the
12-digit UTR; the phone number is implicit (the message sender).**

```
User: Radhe Radhe                                       ← inbound greeting
Bot:  🙏 Welcome to Daily Darshan! What would you like to do?
      [ Subscribe ]  [ Renew ]  [ Stop messages ]      ← reply buttons (CTA ids)
User: (taps Subscribe)
Bot:  Choose your Daily Darshan plan:                   ← list message
      • Starter — ₹9 · 3 days
      • Weekly  — ₹69 · 30 days
      • Monthly — ₹199 · 90 days
      • Yearly  — ₹699 · 365 days
User: (taps Monthly)
Bot:  🙏 What name should we greet you by?               ← asked only if no name yet
User: Deep                                              ← free text (name)
Bot:  By continuing, you agree to receive daily darshan
      and occasional subscription updates. Reply STOP anytime.
      [ I agree ]  [ No thanks ]                        ← explicit consent
User: (taps I agree)
Bot:  Radhe Radhe Deep Ji! Plan: monthly
      Amount: ₹199
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
Bot:  Radhe Radhe Deep Ji! Renewing your monthly plan.   ← existing plan, no name prompt
      Amount: ₹199
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
- **Consent gates payment.** A new or previously opted-out customer must tap `I agree`
  before the UPI instruction is created. `No thanks`, `STOP`, `UNSUBSCRIBE`, or `CANCEL`
  revokes delivery consent and confirms the opt-out.
- The awaiting-name state is a flag on the subscriber row (`subscribers.csv`), so it survives
  across webhook calls without server-side session state.
- Re-delivered webhooks are deduped on WhatsApp `message.id`. A fresh tap has a new ID and is
  a new action. Restart words such as `Radhe Radhe`, `RENEW` and `MENU` are not stored as names.
- Failed conversational replies roll back that message's state. **STOP and received UTR are
  exceptions:** the customer instruction is retained and `reply_retries.csv` stores only its
  failed acknowledgement. Redelivery retries that reply without reapplying the instruction.
- Meta delivery-status callbacks reconcile an initially accepted template send. A later `failed`
  status changes matching renewal/delivery ledger rows to `FAILED`, reopening the daily slot.
  `message_statuses.csv` retains callbacks that arrive before the ledger. Positive delivered/read
  evidence wins over delayed failure callbacks; `SENT` alone means API acceptance, not delivery.
- Activation remains admin-verified out-of-band (see Admin Operations); the name/plan captured
  here is what later fills the daily utility template and the per-subscriber page greeting.
- The subscriber page explicitly confirms that the subscription is active and welcomes the user.
  WhatsApp uses a separate approved `daily_darshan_welcome` activation template. It never
  consumes the daily renewal/delivery contact slot. Daily delivery and renewal continue using
  `daily_darshan_delivery_update`.
  Admin verification queues a welcome in `csv/welcomes.csv` rather than sending immediately.
  Commit/push and publish the page first; the delivery workflow drains the welcome outbox.
  Repeated verification reuses the same payment-keyed task. Opted-out recipients are cancelled.
  Production webhook replies likewise use a durable `csv/reply_outbox.csv` before sending.
  Both outboxes retain ambiguous attempts for reconciliation instead of blindly resending.
  Customers can send CONTINUE, STATUS or RESEND to recover their current step without
  creating another payment or extending a subscription. Repeated recovery requests have
  a 30-second cooldown. BACK from name capture returns to plans; other steps return to menu.
  The `Retry WhatsApp Replies` workflow wakes Render every five minutes to retry eligible
  outbox entries. Conversation versions and subscriber/payment fingerprints cancel stale
  instructions, and a 23-hour expiry protects the reply window. See DEPLOYMENT.md for the
  required WEBHOOK_BASE_URL variable and WHATSAPP_APP_SECRET repository secret.
  Both renewal and delivery check the public page's subscription ID, date and expiry metadata
  before sending. Missing, legacy or stale pages must be regenerated and deployed first.

> **WhatsApp note:** interactive buttons/list messages are free-form inside the 24-hour
> user-initiated window. To send the initial menu to a user who hasn't messaged in 24h, use an
> approved template with buttons; within the window (the normal case, since the user just
> messaged) the free-form interactive menu is used.

The current configuration uses `daily_darshan_delivery_update` with language `en` for scheduled
delivery and renewal reminders. Activation uses the separate `daily_darshan_welcome` template;
both templates send the customer name as body `{{1}}`
and the subscription ID as dynamic URL-button `{{1}}`; configure that button URL as
`https://vipseva.com/{{1}}`. The renewal send deliberately uses the same delivery-status copy
and does not include the expiry date.

Subscriber pages show a **Renew on WhatsApp** CTA from the largest configured
`renewal.reminder_days` value through the post-expiry page grace period. The link opens
`renewal.whatsapp_number` with `RENEW` prefilled; use international digits without `+`.

New daily and source-candidate image filenames use a random UUID prefix, and subscriber pages
reference that persisted opaque filename. Image, page-repair, and delivery runs rediscover and
reuse the same name for the date. A fresh image run migrates an existing date-only canonical
image and removes its predictable legacy canonical and candidate aliases.

The delivery workflow queues overlapping runs and sends renewal reminders before the daily
darshan message. When at least one reminder is sent, it waits five minutes before delivery by
default. If no reminder is sent, delivery starts immediately. Set the GitHub Actions repository
variable `WHATSAPP_MESSAGE_GAP_SECONDS` to another non-negative whole number to change the
workflow-wide pause; `0` proceeds directly to delivery.

Renewal and delivery share the `sentlog.csv` daily contact ledger. A successful renewal reminder
uses that subscriber's one WhatsApp contact slot for the date, so the later delivery phase skips
only that subscriber while continuing for other eligible subscribers. A failed reminder does not
consume the slot, allowing delivery to proceed. This per-subscriber rule applies across scheduled
and manual reruns: at most one successful renewal-or-delivery message is attempted per subscriber
per date after its successful send has been persisted.

---

## Scheduled Jobs

| Workflow | Schedule (UTC) | Local time | Does |
|----------|----------------|------------|------|
| `image.yml` | `1 3 * * *` | 08:31 IST target | Test → verify GPG signing → prune operational logs → fetch all configured sources, store the largest valid canonical image, regenerate pages, expire lapsed subscribers, prune inactive pages and old images, then commit. Historical backfill misses warn and continue; today's image is mandatory. |
| `deploy-pages.yml` | After successful `Daily Image` completion; manual on demand | After image preparation | Publish the current default branch's `docs/` exactly once. A failed/cancelled or non-default-branch image run fails this gate and cannot trigger delivery. |
| `delivery.yml` | After successful `Deploy Daily Darshan Pages`; manual on demand | After publication | Validate WhatsApp secrets → test → verify GPG signing → prune logs → run an idempotent expiry safety sweep → send renewal reminders → deliver today's published personalized page link → signed commits. |
| `pages.yml` | Manual only | On demand | Regenerate all pages from today's stored canonical image without fetching remote images. |

GitHub cron schedules are targets rather than exact start-time guarantees and may be delayed
under runner load. Workflow YAML is authoritative; `config.json.schedule` is informational.
Delivery has no cron of its own. The normal scheduled/manual image chain is image preparation →
one Pages deployment → delivery. Direct manual delivery does not redeploy an unchanged site.
Manual page regeneration does not publish by itself; after verification, manually run **Deploy
Daily Darshan Pages**, which publishes once and then starts delivery.

### End-to-end production journey

1. A customer sends **Radhe Radhe**. Render verifies and deduplicates the webhook, advances the
   CTA/name/consent/payment conversation, and persists subscriber, payment and processed-message
   CSV changes to `main` through an atomic Git Data API commit before HTTP 200.
2. An administrator verifies the UTR and activates or renews the subscriber. This commits the
   subscriber page, but the commit itself does not publish Pages in Actions-based mode.
3. At 08:31 IST (target time), **Daily Image** fetches every source configured for the weekday,
   stores UUID-prefixed candidates, chooses the largest valid image, regenerates subscriber pages,
   expires lapsed subscriptions and applies retention cleanup. Today's valid image is mandatory;
   historical backfill misses only warn.
4. A successful default-branch image run starts **Deploy Daily Darshan Pages**. Publication occurs
   once from the current `main` checkout. A failed Pages deployment stops the automatic chain.
5. Successful publication starts **Daily Delivery**. For each eligible subscriber it attempts a
   renewal reminder first, otherwise the delivery-status template. A persisted successful send in
   `sentlog.csv` blocks every later scheduled or manual contact for that subscriber on that date.
   A failed reminder does not consume the slot, so delivery may still be attempted.
6. Render commits, admin commits, pull-request merges and other pushes to `main` still run CI tests,
   but they do not publish Pages. For an immediate mid-day activation, run **Deploy Daily Darshan
   Pages** manually; for a new image plus the complete chain, run **Daily Image** manually on `main`.

Direct **Daily Delivery** runs never rebuild or deploy the site and should be used only after the
current page is public. Successful **Regenerate Daily Pages** runs on the default branch automatically
trigger Pages deployment, followed by Daily Delivery with daily-send safeguards. Previously published pages remain viewable until a later deployment
replaces or prunes them.

**Idempotency** (safe to re-run):
- Renewal and delivery share `date + mobile` reservations in `sentlog.csv`. The scheduler commits
  `PENDING` before calling WhatsApp and commits the outcome afterward, per subscriber.
  `SENT` (accepted), `DELIVERED`, `PENDING` and `UNKNOWN` block the daily slot. A proven failure
  allows a retry; an ambiguous timeout, crash or failed outcome push does not.
  Never automatically clear `PENDING`/`UNKNOWN`: reconcile provider evidence first. This chooses
  duplicate prevention over guaranteed delivery when the external result cannot be established.
- Renewal reminder history additionally keys on `mobile + reminder_type + expiry_date` in
  `renewals.csv`. Existing successful renewal history is backfilled into the daily ledger on a
  rerun so rollout-day duplicates remain blocked.
- `logs.csv` and `sentlog.csv` retain the inclusive latest 30 calendar days. Cleanup is
  idempotent and creates no commit when nothing is old enough to remove.
- The **expiry sweep** only transitions `ACTIVE` subscribers whose `end_date` has passed; an
  already-`EXPIRED` subscriber is skipped, so re-runs are safe. `PAUSED` (intentional hold)
  and `CANCELLED` (terminal) are never auto-expired.
- Image collection fetches the configured weekday sources on every run, stores every valid
  source candidate, and selects the largest as the canonical dated image. Page generation
  also runs every time, so a subscriber added later still receives a refreshed page.

**Subscription expiry.** Eligibility is date-gated (an expired subscriber is excluded from
delivery/reminders regardless of stored status). The image workflow runs the primary **expiry
sweep** before publication, and `delivery.yml` repeats it as an idempotent manual-run safety check.
The sweep flips the stored status
`ACTIVE → EXPIRED` once `end_date` has passed, keeping reports and admin views truthful. A
subscriber expiring exactly today (`end_date == today`) is still active — expiry applies from
the day after. Renewal reactivates an `EXPIRED` subscriber (`EXPIRED → ACTIVE`, extending
dates).

**Page timing (utility-template mode).** Each subscriber's page lives at
`docs/<subscription_id>/index.html` and is the target of the utility-template link. Pages are
produced in two places so a subscriber's branded URL is never a 404 when they receive it:
1. The daily **image job** regenerates all pages every run (even if the image already exists).
2. **Activation** (`admin.py verify --activate`) renders and optionally commits that subscriber's
   page. A commit alone does not publish under Actions-based Pages. To make a mid-day page live,
   manually run **Deploy Daily Darshan Pages** after activation; successful publication then
   triggers delivery.
> A page becomes reachable after the Pages deployment succeeds, not merely after its Git commit.

**Fault tolerance:** image sources are tried in priority order; a failing source falls
through to the next. WhatsApp sends use bounded retries; a failure for one subscriber does
not stop the batch. Git pushes retry once via `pull --rebase` and never force-push.

### Coordination between the two machines

The webhook (Render) and the scheduler/admin (GitHub Actions) never talk to each other
directly. The **GitHub repo `main` branch is the shared source of truth**; both sides read
and write the same CSVs there:

- **Webhook** uses the GitHub **Git Data API** (`GitHubApiRepository` via `RepoSync`): it
  **pulls** the tracked CSVs before handling a message and **pushes** them after.
- **Scheduler/admin** uses the **git CLI** on the checked-out repo (`LocalGitRepository`):
  it commits + pushes (retry once via `pull --rebase`, never force-push).

Because both write CSVs on `main`, two mechanisms reduce clobbering risk:

1. **Writer separation + safe expiry.** The webhook and scheduler overlap on `subscribers.csv`
   (webhook opt-in vs. the nightly expiry sweep), while asynchronous Meta status callbacks also
   reconcile `sentlog.csv` and `renewals.csv`. `sweep_expired` therefore **re-reads each subscriber row fresh right before
   flipping status** and only changes the status field, so a subscriber the webhook added or
   updated concurrently is preserved rather than overwritten by a stale snapshot. Normal
   `logs.csv` writes are append-only; scheduled cleanup atomically removes rows outside the
   30-day window.

2. **Optimistic conflict handling.** The webhook now pushes immediately; the quiet window is
   disabled because event-driven/manual workflows cannot be safely bracketed by a fixed clock
   window and deferred writes on Render's ephemeral disk can be lost. GitHub API writes reject a
   stale snapshot: one tree commit contains all webhook CSV changes and a non-force branch update
   rejects a concurrent advance. The handler restores its local snapshot and requests redelivery.
   Scheduler pushes pull/rebase once and fail visibly rather than force-pushing.

> GitHub state publication is atomic, but GitHub and WhatsApp are not a distributed transaction.
> Local locking assumes one Render instance/shared filesystem; retain that deployment model.
> At higher write
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
  Repeating the same reference does not extend dates again: `applied_payment_refs` is stored
  atomically with subscriber dates. `activation_state` records payment application progress.
  Legacy verified payments without markers require reconciliation before reapplication; see
  [release and recovery checklist](./DEPLOYMENT.md#safety-changes-release-and-recovery-checklist).
- Prefer the CLI over manual payment/subscriber edits so entitlement markers remain consistent.
- **Override the daily image:** replace `docs/images/YYYY-MM-DD.jpg` and commit.
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

## Secret Key Rotation
- When rotating WHATSAPP_ACCESS_TOKEN, update in both: Render and GitHub Actions
- When rotating WEBHOOK_VERIFY_TOKEN, update both Render and Meta Developer Dashboard → WhatsApp webhook verification
- When rotating GITHUB_TOKEN, update Render only. GitHub Actions uses its automatically generated per-run token.
