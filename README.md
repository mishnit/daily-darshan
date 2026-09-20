# Daily Darshan

## CSV consistency and webhook latency

Render enables an explicitly best-effort webhook queue. Signature-verified payloads
are put into a bounded in-memory queue and acknowledged immediately; overload is also
acknowledged and dropped. One actor mutates indexed in-memory repository state in batches
and submits immutable replies without waiting for transport. A persistent 320-thread,
5,000-job bounded sender pool calls WhatsApp and returns outcomes through a control queue
to the same state writer. Memory state is serialized to CSV
before Git snapshots are attempted every 15 minutes. The actor resumes queued work after
each snapshot. No RepoSync,
CSV lock, state lock, Git operation or Meta request runs in the HTTP request path.

This mode deliberately sacrifices durability: queued and locally processed events can
be lost on restart, spin-down, deployment, queue overflow or failed Git export. It is
selected by `WEBHOOK_BEST_EFFORT_QUEUE=true` and `WEBHOOK_SINGLE_WRITER=true` in
`render.yaml`. Without those flags the legacy synchronous durable mode remains available.
Render logs emit queue depth, drops, failures, batch latency, oldest-event latency, and
snapshot duration every ten seconds while traffic is being processed.
Ordinary payment refreshes are coalesced to at most once per 15 seconds; critical commands
always refresh immediately. Best-effort message-ID dedupe is capped at 120,000 recent rows,
and reconciled Meta status callbacks are consumed to bound memory under sustained traffic.
Every user-initiated webhook contributes to 10-second processing and send-completion
avg/p50/p95/p99 aggregates. Correlated per-invocation logs are sampled at 0.1% by default
(`WEBHOOK_METRICS_SAMPLE_RATE=0.001`) to avoid making logging itself a throughput bottleneck;
Meta status-only callbacks are excluded.
`WEBHOOK_METRICS_ENABLED=false` disables metric collection and correlation while preserving
operational webhook errors. `WEBHOOK_LOGGING_ENABLED=false` disables the complete
`daily_darshan.webhook` logger, including errors, queue/snapshot diagnostics, metrics, and
sampled invocation logs; both settings are read at process startup.

The actor has two ordered lanes. Ordinary customer events remain memory-backed until the
15-minute snapshot. Authorized `ADMIN`/`ADM_*`, `UTR_CONFIRM_*`/`UTR_EDIT_*`, and rejected-payment
`CTA_PAYMENT_REVIEW` events use
a critical durability lane: the latest remote payment ledger is merged by `reference_id`, the command
is applied and committed immediately, and only then is its WhatsApp response sent. A
same-field payment conflict blocks the critical command; ordinary refreshes treat the
committed remote payment value as authoritative. Both lanes share one state writer, so an
immediate critical commit and a scheduled snapshot cannot overlap.

Before either kind of commit, shared business CSVs are three-way merged by their domain
keys: subscriber mobile; delivery date/mobile; renewal mobile/type/expiry; welcome payment
reference; and image/request IDs. Delivery states merge monotonically (`QUEUED/PENDING` →
`SENT` → `DELIVERED/READ`), payment references are unioned, and operational logs are an
append-only idempotent union. Conflicting mutable fields block critical commits; the
ordinary snapshot accepts the already committed remote value. A Git baseline advances
only after its semantic merge succeeds.

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
| `daily_image_rotation` | Weekday-to-source mapping. Store all valid candidates and ask the admin to preview and approve one source. |
| `admin.require_image_approval` | Enabled in production. Blocks pages, deployment and customer messages until today's image is approved. |
| `admin.image_preview_base` | HTTPS repository content base used for WhatsApp image previews before Pages deployment. Must be publicly reachable by Meta. |
| `temple_sources` | Named temple page URLs and `enabled` flags used by the weekday rotation. |
| `image_sources` / `image_source_config` | Legacy generic source fallback used only when no enabled named temple sources are configured. |
| `image_validation` | `min_width`, `min_height`, `allowed_formats` for `ImageValidator`. |
| `paths` | Relative paths to the CSV files and `images/` directory. |
| `schedule` | Image cron hint (documentation; the actual cron lives in `image.yml`). Pages publication and delivery are event-driven. |
| `renewal.reminder_days` | Days-before-expiry to send reminders, e.g. `[3, 2, 1]`. Does not change the fixed three-day renewal eligibility or page CTA window. |
| `renewal.whatsapp_number` | Digits-only WhatsApp destination used by the near-expiry page CTA. |
| `delivery.karma_api_url` | Public Render endpoint used after a successful native share handoff to award one daily Karma point. |
| `persistence` | Webhook durability. `mode`: `github_api` (snapshot reads and atomic Git Data API commits — needs `GITHUB_TOKEN`+`GITHUB_REPO`) or `local` (no sync; dev only). `branch`: repo branch to sync against. |
| `delivery` | Delivery mode + message settings. `mode`: `utility_template` (send a parameterized utility template linking to a per-subscriber page) or `image` (send the image inline). Also controls template language, page/image URLs, retries, 30-day operational-log retention, image retention and page-retention grace. |

### Common config changes

- **Change a price or plan length** — edit `plans.<plan>.amount` / `.days`. No code change.
- **Add a new plan** — add a `plans` entry; it becomes selectable in the webhook automatically.
- **Change the weekday rotation** — edit `daily_image_rotation.<weekday>`. When a weekday
  lists multiple sources, the job stores every valid candidate for admin selection.
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

> **Webhook persistence mode (important).** Render uses a bounded best-effort in-memory queue
> and attempts one batched Git snapshot every 15 minutes. It does not pull/push per request.
> Acknowledgement means queued (or intentionally dropped at overload), not durably processed.
> The scheduler continues to commit through git directly.
>
> **`/health` is a real readiness probe.** It returns **200** `{"status":"ok"}` when the app
> initialized and the store is readable; **503** `{"status":"unhealthy"}` (with a reason) if
> the container failed to build, or `{"status":"degraded"}` if the store is unreadable. It also
> reports the selected webhook mode and signature verification. A bad config no longer crashes
> the process. In best-effort mode valid webhook payloads receive HTTP 200 even when dropped;
> failures are logged instead of requesting Meta redelivery. Invalid signatures
> return **403**; malformed JSON is acknowledged and ignored without executing actions.
>
> **Best-effort acknowledgement.** HTTP 200 is returned before conversation processing,
> reply sending or Git persistence. One actor owns state mutation, so Render does not use CSV
> or state locks in this mode. This is not exactly-once or lossless; a durable queue remains the
> required upgrade when losing consent, payment or conversation events is unacceptable.

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
4. Run **Daily Image** manually or through your external scheduler. The current YAML has no cron.
   The admin selects an image on WhatsApp before pages are regenerated, deployed and delivered.
   All recovery workflows also support **workflow_dispatch** and enforce the same approval gates.
5. Under *Settings → Pages → Build and deployment → Source*, select **GitHub Actions**. The
   `Deploy Daily Darshan Pages` publishes only after the approved image is rendered. An image
   collection run awaiting approval may start a gated workflow but does not deploy an artifact.

---

## Subscriber Conversation Flow

The webhook (`main.py`) runs a small **CTA-driven** conversation. All selections are made by
**tapping interactive buttons / list options** (WhatsApp interactive messages), so the bot
never guesses intent from free text. **Free text is accepted only for the user's name and the
12-digit UTR; the phone number is implicit (the message sender).**

```
User: Radhe Radhe                                       ← inbound greeting
Bot:  🙏 Radhe Radhe! Choose an option below.
      [ Open menu ] → View plans
User: (taps View plans)
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
      After paying, reply with your payment reference and 12-digit UTR.
      Example: *UTR DD2608190001 123456789012*
User: UTR DD2608190001 123456789012                     ← free text (UTR)
Bot:  Please check your UTR 123456789012 for payment DD2608190001.
      [ Confirm UTR ]  [ Change UTR ]
      This UTR has not been submitted for review yet.
User: (taps Confirm UTR)
Bot:  Your latest UTR 123456789012 for payment DD2608190001 has been recorded.
      It replaced the previous UTR (if any) and is now awaiting admin verification.
      We aim to review it within 24 hours. You do not need to pay again.
      Please send MENU to check payment status.
```

Returning subscriber (Upgrade outside the renewal window, Renew near expiry or after expiry):

UTR confirmation applies to first subscriptions, renewals, extensions and corrections.
Before confirmation, the number is saved only as a recoverable conversation draft in
`conversations.csv` (`utr_draft`, `utr_reference`, `utr_confirmation`). It is not written
to `payments.csv.utr` or submitted for admin review. Subscription status and paid dates
stay unchanged. The payment remains an unpaid/pending checkout until confirmation.
`Change UTR` invalidates the old confirmation and requests a corrected reference-qualified
UTR. Sending a new UTR also replaces the draft. `Payment instructions`/`Payment status`
resumes an outstanding confirmation. Only the latest confirmation for the same sender
is accepted, and payment ownership/status are checked again when it is tapped.
Previously confirmed UTR evidence stays in place until a correction is confirmed.
These are interactive replies; no new Meta template or secret is required.

```
User: (taps Upgrade while on Monthly, expiry beyond 3 days)
Bot:  Choose a larger Daily Darshan plan.
User: (selects Yearly)
Bot:  Radhe Radhe Deep Ji! Renewing your yearly plan.   ← stored name reused
      Amount: ₹699
      Pay via UPI: upi://pay?...
      Reference: DD2608190002
      After paying, reply with your payment reference and 12-digit UTR.
      Example: *UTR DD2608190002 123456789012*
```

### Menu state examples and recovery

The menu is rebuilt from the current subscriber and payment rows on every recognized command.
The following examples describe the exact plan actions and safe recovery from an unexpected input:

| State | User sends or taps | Expected menu/list | Unexpected input recovery |
|---|---|---|---|
| New user | `Hi`, `Hello`, `Radhe Radhe`, `MENU`, `PAYMENT` | `View plans`; selecting it lists Starter, Weekly, Monthly and Yearly | A question or plan name reopens the menu; it never creates a payment |
| Active Starter, expiry beyond 3 days | `MENU` → `Upgrade` | Weekly, Monthly and Yearly only | `RENEW` reopens the same menu; a stale smaller-plan CTA is rejected |
| Active Weekly, expiry beyond 3 days | `MENU` → `Upgrade` | Monthly and Yearly only | `PAYMENT` opens the current checkout status/instructions without replacing it |
| Active Monthly, expiry within 3 days | `MENU` → `Renew` | Monthly and Yearly | Invalid UTR leaves checkout unchanged and asks for `UTR <reference> <12 digits>` |
| Active Yearly, expiry within 3 days | `MENU` → `Renew` | Yearly only; description says “Renew your current plan” | Old navigation CTAs reopen eligible plans; stale lower-plan selections are rejected |
| Active Yearly, expiry beyond 3 days | `MENU` | Subscription status only | `RENEW` reopens the menu but cannot create an unavailable upgrade |
| Expired subscriber | `MENU` or `RENEW` | `Subscription status` + `Renew`; all configured plans | A stale CTA returns to the current menu; no entitlement changes before admin approval |

For any state, `Hi`, `Hello`, `Radhe Radhe`, `MENU`, `RENEW`, `SUBSCRIBE`, `PAYMENT`, `PAYMENT STATUS`
and `PAYMENT INSTRUCTIONS` return to navigation. A valid name is captured only while the name
prompt is active; a valid UTR is processed only against an open payment. `STOP`, `UNSUBSCRIBE` and
`CANCEL` remain case-insensitive opt-out commands, and `STATUS` returns status directly.

Details:

- New users see View plans, not Subscription status. Incomplete signup returns to the
  missing name or consent step when the user sends MENU / Radhe Radhe.
- If a subscriber record is missing but an old PENDING payment without UTR remains,
  show View plans and require name/consent again. Menu navigation preserves the payment;
  selecting the same plan reuses its reference. Rejected and approved payments remain
  status-only until resolved, even without a subscriber record.
- Active users see Subscription status (including their current plan). Outside the three-day
  renewal window they see Upgrade, whose list contains only plans strictly larger than their
  current plan; subscribers already on the largest plan see no plan CTA. Inside the three-day
  window they see Renew, whose list contains their current plan plus larger plans (including
  Yearly subscribers renewing Yearly). A Yearly subscriber's row is described as
  “Renew your current plan”; plans with larger choices use “Renew or choose a larger plan.”
  Expired users see Subscription status and Renew, with all configured plans available. Active opted-out users additionally see Resume messages: explicit
  consent restores delivery without another payment.
- The renewal window is fixed at **0–3 calendar days remaining in Asia/Kolkata**, including
  expiry day. At four days remaining, same-plan checkout is unavailable. Reminder cadence
  does not change this rule. Upgrades to strictly larger plans are available at any time,
  including through the Renew list during the renewal window. Plans are ordered by configured
  duration (`days`), then price (`amount`) for equal durations.
- Unpaid PENDING checkouts for lower plans or same-plan renewals outside the window become
  SUPERSEDED when the subscriber returns. Payment instructions, consent recovery, retries,
  and old plan buttons recheck eligibility. A confirmed UTR remains available for review;
  someone who paid using older instructions can still submit its reference-qualified UTR.
  Expired subscribers can choose any configured plan. Approval of recorded payments and
  preservation of already-paid days continue to use the existing admin flow.
- Applied payment references are excluded from unpaid checkout actions. If a manual recovery left
  the subscriber plan label stale, menu and status eligibility use the largest plan proven by the
  subscriber's applied payment references. This prevents an old WhatsApp CTA from reopening plans
  below or above the wrong entitlement; old messages remain visible, but every click is revalidated.
- Help, Stop messages, Continue, Resend and Back are not menu options. Typed STOP and the
  consent disclosure's No thanks button still revoke consent without removing paid days.
- An unpaid checkout shows Payment instructions and Change plan for new users, Renew for existing
  subscribers, and Upgrade/Renew for active users according to the renewal window. After UTR submission, Payment status and the
  applicable plan action remain available. Status repeats the reference-qualified
  UTR format so the customer can identify the checkout actually paid. Choosing another plan
  creates a new checkout; a customer who already paid must confirm the older paid-against
  reference as `UTR <reference> <12-digit UTR>` and must not pay again.
- Sending a reference-qualified UTR again creates a correction draft. Only tapping Confirm UTR
  replaces the previously stored UTR. The acknowledgement names the latest UTR and payment reference,
  confirms that it is awaiting admin verification within 24 hours, and tells the user to send
  MENU for payment status; it never activates the subscription without administrator approval.
- If administrators approve multiple genuine payments, every payment reference is applied
  once and contributes its purchased days. The subscriber retains the longest approved plan
  as the active plan, so approving a smaller payment later cannot downgrade the plan label.
- Payment instructions/Payment status opens the current checkout or review details.
  STATUS reports current plan, entitlement, consent and any pending payment separately. Send MENU for
  available actions. Existing subscribers retain Subscription status while paying or renewing.
- Rejected payments show Payment status and require administrator resolution before another
  checkout. Approval without activation says activation is being completed. After activation,
  page publication remains awaiting confirmation until the welcome worker verifies publication.
  Publication is independent of whether Meta accepts/delivers the welcome message.
- **Hi / Hello / Radhe Radhe / MENU never resets signup or payment.** During name entry the
  missing-name prompt is shown again; during consent the disclosure is shown; while awaiting
  UTR the existing checkout options are shown. A confirmed UTR stays under review. Navigation
  is never saved as a name and never creates a replacement payment.
- **Changed-plan payment matching:** after a checkout has been superseded, a bare UTR is
  ambiguous and is not recorded. Send `UTR <original-reference> <12-digit-UTR>`, using the
  reference from the instructions actually paid against, e.g. `UTR DD2609130001 123456789012`.
  The original plan/reference is restored for review and other unpaid pending checkouts are
  superseded. References belonging to another sender or already verified/rejected are refused.
  An existing payment under review cannot be displaced. Admin still checks actual amount/proof.
- Payment instructions never bypass missing name or consent. Numeric/payment-looking names
  and questions are rejected with a name prompt; valid names remain title-cased.
- An active opted-out user always has Resume messages, even during payment review. Its
  separate consent action changes consent only, not checkout, UTR or entitlement.
- Rejected users can select Request review. This records `PAYMENT_REVIEW_REQUESTED` in the
  operational log through the immediate critical Git lane and acknowledges the request only after
  that commit; it does not automatically notify an admin or
  approve payment. Administrators inspect `list-rejected` and logs. A new checkout is unlocked
  only after `reopen-payment <reference> --no-payment-confirmed --commit`, or the original
  payment is verified after proof review. The explicit flag must never be used if payment occurred.
- A WhatsApp admin rejection immediately sends the customer the configured
  `messages.payment_rejected` response. `{reference_id}`, `{release_days}`, and `{release_date}`
  are supported placeholders. Payment status reuses exactly the same text so the proactive notice
  and later menu response cannot disagree.
- Delivery cleanup changes a payment from `FAILED` to `SUPERSEDED` after three full calendar days
  from `rejected_at`, controlled by `delivery.failed_payment_release_days`. This releases the menu
  so the customer can renew or upgrade again without deleting the old reference, UTR, timestamp, or
  audit history. Legacy rows use their `PAYMENT_REJECTED` log timestamp and remain blocked when no
  trustworthy rejection timestamp exists.
- As soon as one approved payment is applied, every other `PENDING` or `FAILED` checkout for that
  customer becomes `SUPERSEDED`. This immediately removes competing reviews from the customer menu
  without deleting their UTR evidence. Those rows remain in the admin review queue and can still be
  verified later when bank evidence proves that a second payment also occurred.
- A newly verified purchase can reactivate a CANCELLED subscriber once, starting a fresh term
  from the UTR-confirmation IST date without silently restoring consent. Old cancelled/paused/expired
  activation welcomes are cancelled rather than announcing an active subscription.
- UTR text may be 12 digits or `UTR: 123456789012`. Image/document captions in that format
  are accepted; screenshots without a valid UTR caption prompt the user to send it as text.
  No OCR or automatic payment approval is performed.
- **Selections are buttons, not typed commands.** Inbound taps arrive as interactive
  `button_reply`/`list_reply` **ids**; routing is on stable ids: `CTA_SUBSCRIBE` → plan list,
  `CTA_RENEW` → renew, `PLAN_<plan>` → chosen plan. Typing a plan word (e.g. "how much is
  yearly?") **never** starts a subscription — it just re-shows the menu. This removes a class
  of accidental-signup / wrong-plan bugs from free-text parsing.
- **Free text is limited to name and UTR.** When the bot is awaiting a name, the next text is
  stored as the name (a 12-digit value is treated as a UTR, never a name; a blank re-prompts).
  A 12-digit message creates a draft for the latest pending payment and requests confirmation. Any other
  typed text shows the CTA menu.
- **Name capture is explicit** (WhatsApp profile name is unreliable). If the inbound webhook
  already carries a profile name, the prompt is skipped and that name is used.
- **Renewal offers the current plan plus larger plans inside the three-day window, and all
  configured plans after expiry**, reuses the stored name and gates payment instructions on
  consent. An upgrade outside the window offers only larger plans. A returning user's selected plan is stored in the pending payment; their paid
  plan and dates change only on admin approval. On admin verification, renewal
  **extends from the current expiry date** (not from today) so remaining days are never lost
  (Tech Doc §29). Renew from an unknown mobile falls back to the plan list.
- **Consent gates payment.** A new or previously opted-out customer must tap `I agree`
  before the UPI instruction is sent. A pending checkout may already exist, but does not grant
  entitlement. `No thanks`, `STOP`, `UNSUBSCRIBE`, or `CANCEL` revokes delivery consent,
  not paid days or a refund. Active users can explicitly resume; expired users renew.
- The awaiting-name state is a flag on the subscriber row (`subscribers.csv`), so it survives
  across webhook calls without server-side session state.
- Re-delivered webhooks are deduped on WhatsApp `message.id`. A fresh tap has a new ID and is
  a new action. Restart words such as `Radhe Radhe`, `RENEW` and `MENU` are not stored as names.
- In production (`github_api` persistence), conversational state and reply-outbox entries
  are committed before sending. A failed commit restores local state and sends nothing.
  Failed sends retain committed state and are retried only when safe and still relevant.
  The direct-send/non-production path uses rollback, with STOP/UTR acknowledgement retries
  retained in `reply_outbox.csv`; it is not the production delivery ordering.
- Meta delivery-status callbacks reconcile an initially accepted template send. A later `failed`
  status changes matching welcome/renewal/delivery ledger rows to `FAILED`, reopening the daily slot.
  New rows in `welcomes.csv`, `reply_outbox.csv`, and
  `message_statuses.csv` include an immutable creation `timestamp`, in UTC formatted
  as `2026-09-11T11:01:06.270845` (six fractional digits, no timezone suffix).
  Status updates and duplicate callbacks preserve it. Legacy headers upgrade on
  the next write; historical rows remain blank rather than being backdated.
  `message_statuses.csv` retains callbacks that arrive before the ledger. Positive delivered/read
  evidence wins over delayed failure callbacks; `SENT` alone means API acceptance, not delivery.
- Activation remains admin-verified out-of-band (see Admin Operations); the name/plan captured
  here is what later fills the daily utility template and the per-subscriber page greeting.
- The subscriber page explicitly confirms that the subscription is active through the stored
  `subscribers.csv` expiry date.
  Welcome, renewal and delivery use `dailydarshan_subscription_status`, while their audit records
  remain in separate CSV ledgers. A welcome consumes the same date+mobile contact slot, ensuring
  at most one of those three messages reaches a subscriber per day.
  Admin verification queues a welcome in `csv/welcomes.csv` rather than sending immediately.
  The welcome worker also creates any missing task for an ACTIVE subscriber's semicolon-separated
  `applied_payment_refs`, so a careful manual CSV activation remains recoverable and idempotent.
  The subscriber's existing `subscription_id` is preserved across activation and renewal, keeping
  one stable `docs/<subscription_id>/index.html` page for that mobile number.
  Page generation validates the complete subscriber snapshot before writing: every ACTIVE mobile
  must have exactly one ACTIVE row and one non-shared `subscription_id`. Conflicting active rows,
  missing IDs or an ID assigned to different mobiles fail the workflow before any personalised
  page is overwritten. If a historical non-active row exists for the same mobile, only its
  canonical ACTIVE row is rendered.
  Commit/push and publish the page first; the delivery workflow drains the welcome outbox.
  Repeated verification reuses the same payment-keyed task. Opted-out recipients are cancelled.
  Production webhook replies likewise use a durable `csv/reply_outbox.csv` before sending.
  Both outboxes retain ambiguous attempts for reconciliation instead of blindly resending.
  Subscriber pages encourage sharing through “Share Darshan with family & friends on whatsapp”.
  On supported HTTPS browsers the native share sheet receives the actual image file and
  a VIP Seva referral caption, never the subscriber page URL. Users select WhatsApp.
- A completed native share handoff can award one Karma point per subscription per IST date through
  `delivery.karma_api_url`. The static page displays the persisted total and updates it optimistically
  after the API accepts a new daily event. This proves the browser handed content to a share target,
  not that a recipient opened or read it.
  Download-image, copy-caption and explicitly labelled link-only fallbacks remain available.
  WhatsApp/browser versions may omit the caption when sharing a file; it can be copied manually.
  This does not prevent someone copying their personal URL from the address bar. After changing
  the renderer, regenerate subscriber pages and deploy Pages to publish the new share controls.
  Customers can send MENU and select Payment instructions or Payment status without
  creating another payment or extending a subscription. Continue, Resend and Back are not
  shown or advertised; legacy commands/buttons remain accepted for older messages.
  The Retry WhatsApp Replies workflow has been removed: no timer drains old replies.
  An operator can explicitly invoke the signed Render recovery endpoint for eligible
  outbox entries. Conversation versions and subscriber/payment fingerprints cancel stale
  instructions, and a 23-hour expiry protects the reply window. Confirmed failures are
  cancelled after the initial attempt plus three retries. Fresh MENU messages remain usable.
  Both renewal and delivery check the public page's subscription ID, date and expiry metadata
  before sending. Missing, legacy or stale pages must be regenerated and deployed first.
  Daily delivery additionally verifies the actual image URL on the published page has today's
  dated filename. Regenerated pages using an earlier image remain viewable but cannot pass
  this daily-delivery check. Welcome and renewal retain their separate publication rules.
  Queued status replies include welcome/publication state in their freshness fingerprint,
  preventing a delayed preparation message after publication has been confirmed.

### Welcome and daily-message coordination

| Event | Message and ordering | Retry / daily-slot rule |
| --- | --- | --- |
| UTR received | Conversational acknowledgement, awaiting admin verification | Does not activate or consume the daily slot |
| Payment approved, not activated | Payment status says activation is being completed | No welcome yet |
| Activation or renewal applied, publication unconfirmed | Payment status says page preparation/publication awaits confirmation | Payment-keyed welcome stays queued |
| Published page verified | `dailydarshan_subscription_status`, language `en` | Welcome takes the subscriber's shared daily contact slot |
| Daily renewal reminder due in 3, 2 or 1 days | `dailydarshan_subscription_status`, language `en` | Shares the subscriber/date reservation with daily delivery |
| Daily delivery eligible | Same delivery-update template, personalised page button | Skips if renewal/delivery already holds that day's slot |

The workflow runs welcome, renewal reminder, then daily delivery. The first accepted or uncertain
send consumes the shared daily slot, so later phases skip that subscriber. A definitively failed
welcome releases the slot so renewal or delivery can provide a same-day fallback. If an earlier
renewal/delivery already used today's slot, a queued welcome waits for the next delivery run/day.
Publication confirmation does not mean a welcome was delivered.

Definitively failed renewal attempts allow delivery fallback; PENDING/UNKNOWN attempts hold
the slot pending reconciliation. API acceptance is not delivery confirmation. Typed STOP
blocks business-initiated messages but preserves entitlement; restoring consent does not
automatically revive an already-cancelled welcome task.

Regression coverage: `tests/test_product_journey.py`, `tests/test_welcome_outbox.py`,
`tests/test_conversation_recovery.py`, `tests/test_audit_regressions.py`,
`tests/test_delivery.py` and `tests/test_renewal.py`. Run the full suite before merge and
verify the exact PR head in CI; local tests do not verify live Meta/Render delivery.

> **WhatsApp note:** interactive buttons/list messages are free-form inside the 24-hour
> user-initiated window. To send the initial menu to a user who hasn't messaged in 24h, use an
> approved template with buttons; within the window (the normal case, since the user just
> messaged) the free-form interactive menu is used.

The current configuration uses `dailydarshan_subscription_status` with language `en` for welcome,
scheduled delivery and renewal reminders. All three sends use the customer name as body `{{1}}`
and subscription status as body `{{2}}`. The one dynamic URL button is labelled
**Daily Darshan Subscription**, with URL `https://vipseva.com/{{1}}`; its independently
numbered parameter receives only the subscription ID.

Approved body:

> Radhe Radhe {{1}} Ji,
>
> Your Daily Darshan delivery status has been updated as {{2}}.
> Please check subscription status in personalised link below on VIPSeva.com.

Welcome uses **Activated**. Delivery and renewal use **Active** beyond three days,
**Expiring in 3 days**, **Expiring in 2 days**, **Expiring in 1 day**, or **Expiring today**,
based on the scheduler's IST business date. Welcome retains activation wording even near expiry;
the personalised page still shows the expiry. The formatter supports **Expired**, but these
automatic jobs continue to exclude expired subscribers; this change adds no expired-user campaign.
Renewal reminders run on configured days 3, 2 and 1; expiry-day delivery uses Expiring today.

All three paths share the existing date+mobile reservation. Normal workflow priority is welcome,
renewal, delivery. Other execution orders and reruns still allow at most one accepted or uncertain
send that day. Confirmed failures release the slot; PENDING/UNKNOWN outcomes block retries until
reconciled. Provider acceptance is not proof of delivery.

This approved template requires today's public image as its header, enforced by the sender even
if its header setting is omitted. All three production header settings are `image`. Missing images
fail without sending. Legacy one-body-variable templates remain compatible for rollback;
their media headers are controlled independently in `config.json`. Keep
`delivery.template_header`, `delivery.welcome_template_header` and
`renewal.template_header` set to `none` for templates without a header. After Meta approves a
template with a dynamic **Image** header, set the applicable template name and change only its
header setting to `image`. The sender then prepends today's validated, publicly deployed canonical
image as the Meta header component while retaining the existing body-name and URL-button values.
An image-header send fails closed when today's image is missing; a welcome's failed reservation
is released. Run `pytest tests/test_subscription_template.py -q` for payload and daily-limit tests.

Subscriber pages show a **Renew on WhatsApp** CTA from three days before expiry
through the post-expiry page grace period, independently of `renewal.reminder_days`. The link opens
`renewal.whatsapp_number` with `RENEW` prefilled; use international digits without `+`.

New daily and source-candidate image filenames use a random UUID prefix, and subscriber pages
reference that persisted opaque filename. Image, page-repair, and delivery runs rediscover and
reuse the same name for the date. A fresh image run migrates an existing date-only canonical
image and removes its predictable legacy canonical and candidate aliases.

The delivery workflow queues overlapping repository writers and sends renewal reminders before
the daily darshan phase. It does not sleep between phases: the shared `sentlog.csv` reservation
already guarantees that the same subscriber receives at most one welcome, renewal or delivery
attempt per day. Removing the workflow-wide pause frees the runner without weakening that rule.

Welcome, renewal and delivery share the `sentlog.csv` daily contact ledger while retaining their
separate welcome/renewal audit CSVs. A successful welcome or renewal uses that subscriber's one
WhatsApp contact slot for the date, so later phases skip only that subscriber while continuing for
others. A definitive failure releases the slot, allowing the next phase to proceed. This rule
applies across scheduled and manual reruns: at most one accepted or uncertain business-initiated
template is attempted per subscriber per date.

---

## Scheduled Jobs

| Workflow | Schedule (UTC) | Local time | Does |
|----------|----------------|------------|------|
| `image.yml` | Manual / external scheduler | On demand | Store valid source candidates, then invite admin to reply ADMIN for visual selection. No automatic highest-resolution selection. |
| `payment-utr-alert.yml` | Manual only | On demand | Alert admin about confirmed UTRs awaiting review and today's checkouts missing a UTR. Uses `daily_darshan_ops_alert`. |
| `pages.yml` | Push to `csv/pipeline_requests.csv` on main; manual | After admin approval | Check today's approval, copy only approved bytes to canonical image, regenerate pages and record the approval stamp. |
| `deploy-pages.yml` | Successful Daily Image or Regenerate Daily Pages; manual | After rendering | Deploy only if approval, canonical bytes and rendered stamp agree. Collection-only completion skips deployment. |
| `delivery.yml` | Successful deployment; every 30 minutes; manual | After publication | Require today's approval and live public stamp, then run welcome, renewal and delivery with the shared daily contact limit. Scheduled recovery safely retries confirmed failures; ambiguous sends remain held for callback reconciliation. |

With `admin.require_image_approval=true`, no previous-date fallback or manual source override
can bypass today's admin decision. Jobs exit/skip while waiting; no runner sleeps waiting for
the admin. The admin's committed decision creates a durable publication request, triggering
page regeneration automatically. A failed workflow can be rerun after correcting its cause.
Successful page regeneration triggers deployment, then delivery. The existing once-per-day
ledger still prevents repeat customer messages.

The former automatic largest-image selection and previous-date page fallback remain available
only when image approval is explicitly disabled in configuration. Production enables approval.

### End-to-end production journey

1. Customer sees the value proposition if no welcome row exists, chooses a plan, supplies a
   name and consent, pays, and sends **UTR Txn_Ref_ID UTR_ID**. Payment responses highlight the
   real example using WhatsApp bold: `*UTR DD2609160001 123456789012*`.
2. Customer confirms the displayed UTR. Only then does `payments.csv.utr` become reviewable.
   `utr_confirmed_at` records the confirmation timestamp with the IST timezone.
3. Admin `919535507255` sends **ADMIN** to business sender `916361699109`, selects
   **Review payments**, checks the UTR/amount against bank records, and taps **Approve payment**.
   The webhook atomically commits payment, subscription, queued welcome and publication request.
   Rejection adds no entitlement. A changed UTR invalidates an old approval button.
4. Activation uses `start_date = confirmation date (IST)` and
   `end_date = max(previous expiry, confirmation date) + purchased days` for renewals/extensions.
   Example: confirmation Sep 16, existing expiry Sep 20, 30-day purchase, admin approval Sep 18:
   start Sep 16, end Oct 20. For an already expired subscriber, end is Oct 16. Legacy records
   without a confirmation timestamp use the approval date; no historical date is invented.
5. **Daily Image** collects candidates. Admin receives an ops alert, replies **ADMIN**, selects
   **Select daily image**, previews a source, and taps **Approve image** (or **Other sources**).
   Even a single available source requires approval. An older day's preview cannot be approved.
6. The approval commit updates `csv/pipeline_requests.csv`, triggering **Regenerate Daily Pages**.
   It validates approved image bytes and renders pages from fresh main, retrying bounded Git
   collisions. **Deploy Daily Darshan Pages** publishes the matching artifact.
7. **Daily Delivery** verifies the live approval stamp and personalized pages before sending.
   Welcome, renewal and delivery retain separate audit records and share the daily contact slot.
   No approval means no generation/deployment/customer send; already published pages remain viewable.

Setup: add `WHATSAPP_ADMIN_NUMBERS=919535507255` to **Render environment** and **GitHub Actions
repository variables** before rollout. GitHub image/payment-alert jobs use the existing WhatsApp
access-token and phone-number-ID secrets. Render still uses its existing GitHub PAT with Contents
read/write to main. This PAT's publication-request commit triggers Actions; the automatic Actions
token does not trigger another push workflow. No new Meta template is needed: the existing
`daily_darshan_ops_alert` tells the admin to reply ADMIN, opening the conversation for interactive
reviews and image previews. The template's URL button remains a workflow-run link.

New operational schemas (existing headers migrate on write):
- `payments.csv`: adds `utr_confirmed_at` and `rejected_at`.
- `conversations.csv`: adds draft and admin decision fields; only opaque, sender-bound buttons
  for the current snapshot can approve.
- `image_reviews.csv`: `id,date,generation,source,path,sha256,status,approved_by,approved_at`.
- `pipeline_requests.csv`: `id,reason,created_at`; payment-reference/image-generation keys
  make repeat approvals idempotent. This is a trigger/audit ledger, not a queue to delete.

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
- Before approval, image reruns replace the pending batch and invalidate stale previews.
  After approval, reruns preserve the chosen source. Regenerate pages to publish later activations.

**Subscription expiry.** Eligibility is date-gated (an expired subscriber is excluded from
delivery/reminders regardless of stored status). Approved page publication runs the primary **expiry
sweep** before publication, and `delivery.yml` repeats it as an idempotent manual-run safety check.
The sweep flips the stored status
`ACTIVE → EXPIRED` once `end_date` has passed, keeping reports and admin views truthful. A
subscriber expiring exactly today (`end_date == today`) is still active — expiry applies from
the day after. Renewal reactivates an `EXPIRED` subscriber (`EXPIRED → ACTIVE`, extending
dates).

**Page timing (utility-template mode).** Each subscriber's page lives at
`docs/<subscription_id>/index.html` and is the target of the utility-template link. Pages are
produced in two places so a subscriber's branded URL is never a 404 when they receive it:
1. **Regenerate Daily Pages** renders all pages after today's source is approved.
2. **Activation**, through WhatsApp or `admin.py verify --activate --commit`, queues a publication
   request when image approval is enabled. It never renders a page before source approval.
   If a CLI commit uses credentials that suppress push workflows, manually run **Regenerate
   Daily Pages**. Successful regeneration triggers deployment and then delivery.
> A page becomes reachable after the Pages deployment succeeds, not merely after its Git commit.

**Fault tolerance:** image sources are tried in priority order; a failing source falls
through to the next. WhatsApp sends use bounded retries; a failure for one subscriber does
not stop the batch. Git pushes make at most five attempts on branch-advance
rejections, fetching and rebasing between attempts, and never force-push.

### Coordination between the two machines

The webhook (Render) and the scheduler/admin (GitHub Actions) never talk to each other
directly. The **GitHub repo `main` branch is the shared source of truth**; both sides read
and write the same CSVs there:

- **Webhook** uses the GitHub **Git Data API** (`GitHubApiRepository` via `RepoSync`): it
  **pulls** the tracked CSVs before handling a message and **pushes** them after.
- **Scheduler/admin** uses the **git CLI** on the checked-out repo (`LocalGitRepository`):
  it commits + pushes (up to five attempts on branch-advance rejection, never force-push).

Because both write CSVs on `main`, two mechanisms reduce clobbering risk:

1. **Writer separation + safe expiry.** The webhook and scheduler overlap on `subscribers.csv`
   (webhook opt-in vs. the nightly expiry sweep), while asynchronous Meta status callbacks also
   reconcile `sentlog.csv` and `renewals.csv`. `sweep_expired` therefore **re-reads each subscriber row fresh right before
   flipping status** and only changes the status field, so a subscriber the webhook added or
   updated concurrently is preserved rather than overwritten by a stale snapshot. Normal
   `logs.csv` writes are append-only; scheduled cleanup atomically removes rows outside the
   30-day window.

2. **Optimistic conflict handling.** The webhook pushes immediately. Event-driven/manual workflows
   cannot be safely bracketed by a fixed clock window, and deferred writes on Render's ephemeral
   disk can be lost. GitHub API writes reject a
   stale snapshot: one tree commit contains all webhook CSV changes and a non-force branch update
   rejects a concurrent advance. The handler restores its local snapshot and requests redelivery.
   Scheduler pushes fetch/rebase with at most five push attempts. Concurrent
   append-only `csv/logs.csv` additions preserve both writers' events. Other
   conflicts abort recovery and fail visibly without overwriting business data.
   Individual Git commands time out after 60 seconds.

   Daily Image uses a stronger publication transaction: fetch `main`, download
   candidates once, then generate pages and expiry changes in a disposable
   checkout. A rejected push discards that checkout and rebuilds from the newest
   CSV/config snapshot using the cached candidates. There are at most five push
   attempts with short randomized delays. No stale page commit is rebased.
   Image audit events are retained as `image-audit` workflow artifacts for 30 days,
   separate from Git publication. Image no longer runs CSV log cleanup; existing
   maintenance/delivery cleanup remains responsible for CSV retention. Exhausted
   retries fail Daily Image and activate the existing Daily Darshan Ops Alerts
   workflow (its WhatsApp secrets and approved template must be configured).

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
- **Manual subscription activation:** use this recovery path only after independently verifying the
  payment. Update the subscriber's existing row in `csv/subscribers.csv` as follows:
  1. Set `status` to `ACTIVE` and enter the verified plan, start date and end date.
  2. Preserve the existing `subscription_id`; never create a second subscriber row or page ID for
     the same mobile number.
  3. Append the verified payment reference to the semicolon-separated `applied_payment_refs` field.
     Do not remove references that have already been applied.
  4. Commit and push `csv/subscribers.csv` to `main`, then manually run **Regenerate Daily Pages**.
  5. Wait for the automatically chained **Deploy Daily Darshan Pages** and **Daily Delivery**
     workflows. During Daily Delivery's welcome phase, the worker creates any missing
     payment-keyed `csv/welcomes.csv` row as `QUEUED`, verifies the public subscriber page and
     sends the welcome if that subscriber's daily contact slot is available. The regeneration
     workflow itself does not create or send the welcome.

  If today's contact slot was already used by a welcome, renewal or delivery, the welcome remains
  `QUEUED` for a later eligible run. Check `csv/welcomes.csv`, `csv/sentlog.csv` and the Daily
  Delivery logs before retrying; do not blindly resend `PENDING` or `UNKNOWN` attempts.
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

The `Tests` CI workflow runs for pull requests and code/configuration pushes to `main`.
Template payload tests follow the template selected in `config.json`, including
its one- or two-variable body and optional image header. The subscription-status
template is also tested explicitly for safe future switches. Shared daily-limit,
retry and consent checks always run; only an image-header-specific check is skipped
when the selected template has no image header. Existing test definitions are retained.
CSV-only webhook commits and docs-only pushes skip CI because they cannot change executable code;
this avoids consuming runners for every WhatsApp interaction. Operational workflows execute only
from the default branch and rely on the already-required CI check instead of reinstalling test-only
dependencies and rerunning the full suite during image, page and delivery work.

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
