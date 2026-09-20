# Deployment & Admin Guide

## Consistency limits and follow-up work

Render is configured for best-effort asynchronous webhook processing. Valid payloads
are acknowledged after a nonblocking enqueue, and a full queue is acknowledged and
dropped. One actor processes batches; sender threads use immutable reply snapshots.
Local CSV/state locking is disabled and Git export is attempted every 15 minutes.

This configuration accepts data loss during restart, spin-down, deployment, overflow
or failed export. Keep exactly one Uvicorn process and one Render instance: the queue
and actor are process-local. `/internal/retry-replies` is disabled in this mode.

Remaining infrastructure decisions are not implemented by these code fixes:

- Move customer CSV state out of any public repository. Commit signing does not
  encrypt it; deleting a current file does not erase history. Plan a private-state
  migration, access review and coordinated history/link cleanup separately.
- Git availability still gates writes. True high availability and lower write latency
  require an authoritative transactional store with a durable inbox/outbox; CSV can
  remain an export, but that changes the current Git-main-authoritative contract.
- Coordinate all writers before scaling instances. Do not add overlapping retry
  workers or automatically convert ambiguous attempts to FAILED.
- Confirm external scheduling and workflow permissions in the deployed environment;
  local tests do not validate Render, Meta, runner capacity or production credentials.

Step-by-step instructions to deploy Daily Darshan on **GitHub** (source control +
persistence + Actions scheduler), and the operational runbook for **admin payment
verification** in CSV.

For the serverless webhook host (Render / Fly / Docker) see the
[Deployment section of the README](./README.md#deployment). This document focuses on the
GitHub side and admin operations.

---

## Table of Contents

1. [Part 1 — Deploy to GitHub](#part-1--deploy-to-github)
   - [A. Push the project to GitHub](#a-push-the-project-to-github)
   - [B. Give Actions permission to commit back](#b-give-actions-permission-to-commit-back)
   - [C. Add the secrets the workflows use](#c-add-the-secrets-the-workflows-use)
   - [D. Confirm the workflows are registered](#d-confirm-the-workflows-are-registered)
   - [E. Test without waiting for the cron](#e-test-without-waiting-for-the-cron-manual-run)
2. [Part 1b — Utility-Template Delivery Mode](#part-1b--utility-template-delivery-mode-optional-cost-optimization)
3. [Part 2 — Admin Payment Verification in CSV](#part-2--admin-payment-verification-in-csv)
   - [The payments.csv row](#the-paymentscsv-row)
   - [Step-by-step approval](#step-by-step-approval)
   - [Concurrency caution](#concurrency-caution)

---

## Part 1 — Deploy to GitHub

### A. Push the project to GitHub

```bash
cd daily-darshan

# 1. Initialize git (skip if already a repo)
git init -b main

# 2. Confirm secrets/artifacts are ignored (already handled by .gitignore)
cat .gitignore   # should list .env, .venv, __pycache__, .DS_Store, etc.

# 3. Stage and commit
git add .
git status       # sanity-check: no .env, no .venv, no secrets staged
git commit -m "Initial commit: Daily Darshan platform v2.0"
```

Create the remote repo and push. Using the GitHub CLI:

```bash
gh repo create daily-darshan --private --source=. --remote=origin --push
```

Or manually (create an empty repo in the GitHub UI first, then):

```bash
git remote add origin https://github.com/<your-user>/daily-darshan.git
git push -u origin main
```

### B. Give Actions permission to commit back

The scheduler jobs commit CSV/image changes, so Actions must be able to write:

1. GitHub repo → **Settings → Actions → General**.
2. Under **Workflow permissions**, select **Read and write permissions** → **Save**.

> The workflow YAMLs already declare `permissions: contents: write`, but this repo-level
> toggle must also allow it.

### C. Add the secrets the workflows use

Repo → **Settings → Secrets and variables → Actions → New repository secret**. Add:

| Secret | Needed for |
|--------|-----------|
| `WHATSAPP_ACCESS_TOKEN` | delivery + renewal jobs |
| `WHATSAPP_PHONE_NUMBER_ID` | delivery + renewal jobs |
| `GPG_PRIVATE_KEY` | signed scheduler commits (ASCII-armored private key) |
| `GPG_PASSPHRASE` | non-interactive unlock and signing check for that private key |

`GITHUB_REPO` is auto-provided in Actions via `${{ github.repository }}`, and the built-in
`GITHUB_TOKEN` covers the commit/push — you do **not** add those manually. Add the matching
GPG public key to the GitHub account; a successful scheduler commit should display
**Verified**. The image job needs the two GPG secrets but no WhatsApp secrets.

> **Webhook host secrets are separate.** The serverless webhook (`main.py`) needs its own
> environment variables set on its host (Render/Fly), not as GitHub Actions secrets:
> `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WEBHOOK_VERIFY_TOKEN`,
> `WHATSAPP_APP_SECRET` (Meta app secret — verifies the `X-Hub-Signature-256` on inbound
> webhooks; production fails closed when it is unset), and
> **`GITHUB_TOKEN` + `GITHUB_REPO` (required for durability)** — with
> `persistence.mode=github_api`, the webhook uses these to pull/push its CSV writes to the
> shared repo. **Without them the webhook writes local-only and those writes are lost on the
> ephemeral host and never reach the scheduler/admin.** See the
> [README Secrets table](./README.md#secrets) and the durability note in the
> [README Deployment section](./README.md#deployment) for details.

### D. Confirm the workflows are registered

Before deploying this release, set `WHATSAPP_ADMIN_NUMBERS=919535507255` in Render's environment
and in GitHub Actions repository **Variables**. Keep `WHATSAPP_PHONE_NUMBER_ID` mapped to the
business sender 916361699109 in both environments. No customer number is authorized by default.
Image and payment alerts reuse the existing WhatsApp secrets and `daily_darshan_ops_alert` with
language `en`; its URL button base is `https://github.com/mishnit/daily-darshan/actions/runs/`
and the dynamic suffix is the run ID. The body tells the admin to reply ADMIN. Ordinary
interactive review messages follow that inbound message; no new approved template is needed.

Keep Render's GitHub PAT able to write Contents on main. It commits `csv/pipeline_requests.csv`,
whose push starts page regeneration. Confirm the repository allows Actions from that PAT.
The image preview base in `config.json` uses publicly accessible raw repository images; a private
repository requires a separate HTTPS preview host accessible to Meta before enabling this flow.

Schema changes are backward-compatible: `payments.csv` adds `utr_confirmed_at`; conversation
rows add draft/admin decision fields. New `image_reviews.csv` and `pipeline_requests.csv` are
created automatically and included in atomic webhook persistence. Do not manually reset their
rows to force retries. Reopen ADMIN to review a fresh snapshot after corrections.

Payment dates use the customer-confirmation timestamp in IST. Example: confirmed Sep 16,
expiry Sep 20, purchased 30 days, approved Sep 18 → start Sep 16, expiry Oct 20. With no remaining
days, expiry is Oct 16. Approval delay consumes calendar days under this requested policy.
Legacy payments without a timestamp use approval day. Correcting and confirming a UTR sets
the timestamp for the newly confirmed value. Reapproving a payment never adds its days twice.

Once pushed, the image, Pages-deployment and delivery workflows appear under **Actions**.
The current image and payment-alert workflows are manual entry points. External scheduling
can dispatch Daily Image. Admin approval advances the gated publication chain:

| Workflow | Cron (UTC) | Local time | Action |
|----------|-----------|------------|--------|
| **Daily Image** (`image.yml`) | Manual / external dispatch | On demand | Store source candidates, alert admin and wait for visual source approval without holding a runner |
| **Pending Payment UTR Alert** (`payment-utr-alert.yml`) | Manual only | On demand | Count confirmed UTRs awaiting review and today's missing UTRs; invite admin to reply ADMIN |
| **Regenerate Daily Pages** (`pages.yml`) | Publication-request push / manual | After approval | Validate chosen bytes, render pages and approval stamp |
| **Deploy Daily Darshan Pages** (`deploy-pages.yml`) | Event-driven | After approved rendering | Publish only an artifact matching today's approved image |
| **Daily Delivery** (`delivery.yml`) | After successful Pages deployment, every 30 minutes, or manual | After publication | Renewal reminder or today's published page link, at most one successful contact per subscriber/date. The cadence recovers confirmed failures; it never blindly resends ambiguous Meta outcomes. |

### E. Test without waiting for the cron (manual run)

The image, Pages-deployment and delivery workflows support `workflow_dispatch`:

1. **Actions** tab → pick **Daily Image** (or a recovery workflow) → **Run workflow** →
   select `main` → **Run workflow**.
2. Watch the run: it checks out the repo, installs dependencies, runs `pytest`, verifies the
   signing key/passphrase, executes the job, and commits results back to the repository.

Choose the recovery entry point deliberately:

- **Daily Image** on `main` stores candidates and asks the admin to preview/approve a source.
  Approval triggers regeneration, deployment and delivery. A single source still requires approval.
- **Deploy Daily Darshan Pages** publishes the current `main` `docs/` tree once and then starts
  Daily Delivery. Use this after a mid-day activation or manual page regeneration.
- **Daily Delivery** sends against the already-published site. It does not build or deploy Pages.
  After publication, a recovery run is also scheduled at minute 17 and 47 of each hour.
  It is serialized with all other repository writers. It retries only rows Meta has
  definitively rejected; `PENDING` and `UNKNOWN` rows require a delivery callback or
  provider-evidence reconciliation so a subscriber never receives an accidental duplicate.
- **Regenerate Daily Pages** on `main` updates and commits page files, then automatically
  triggers Pages deployment on success. Successful deployment starts Daily Delivery with
  the existing daily-send safeguards. Failed or non-default-branch regeneration is not published.
- A failed/cancelled image run, a non-default-branch image run, or a failed Pages deployment stops
  the automatic chain before WhatsApp delivery.

For an end-to-end signing test, run **Daily Image** manually and verify both that the run
succeeds and that its generated `Daily darshan image + pages ...` commit is marked
**Verified**. Import success alone does not prove that the passphrase can sign.

> **Notes on scheduled runs:** GitHub disables scheduled workflows in a repo with **no
> activity for 60 days**, and cron start times can be delayed under load. For a personal
> MVP this is usually acceptable.

### F. Verify the complete production journey

Use this sequence when validating a release end to end:

1. Send **Radhe Radhe** to the WhatsApp number and complete every CTA, name, consent, payment and
   UTR step. Confirm Render returns 2xx responses, deduplicates the inbound message ID and commits
   the updated subscriber/payment/processed CSVs to `main` in one Git Data API commit before 200.
   Inject a reply/persistence failure and expect 503 rather than a false acknowledgement.
2. From admin 919535507255, send ADMIN to business sender 916361699109. Review the confirmed UTR
   against bank records and approve. Confirm payment, subscription, welcome and pipeline request
   persist atomically; no customer message is sent before publication.
3. Run **Daily Image** on `main`, then use ADMIN → Select daily image → preview → Approve image.
   Confirm no pages/deployment/delivery occurs before approval. Check regeneration uses the selected
   source even when another candidate has a higher resolution.
4. Confirm exactly one **Deploy Daily Darshan Pages** run follows and completes before delivery.
5. Confirm exactly one automatic **Daily Delivery** run follows publication. A successful renewal
   reminder or delivery first commits a `date + mobile` PENDING reservation in `sentlog.csv`,
   then commits SENT (API accepted), FAILED (rejected) or UNKNOWN (ambiguous).
6. Rerun delivery manually on the same date and confirm that subscriber is skipped. If a renewal
   send was definitively rejected, confirm delivery remains eligible. PENDING/UNKNOWN entries
   remain blocked for operator reconciliation; never clear them just to make a rerun send.

### Safety changes: release and recovery checklist

- Regenerate subscriber pages and deploy them before running delivery after this release.
  Both message types now require public page metadata matching subscription ID, date and expiry.
  Legacy pages, 404s, redirects and unreachable pages fail closed without sending.
- Keep Render on one instance/shared filesystem. Webhook handling uses a local transaction lock,
  immutable GitHub reads and atomic multi-file publication. No new secret is required; the PAT
  still needs repository Contents read/write. Branch rules may reject API-generated commits;
  test persistence before enabling live traffic and do not weaken signing/protection rules.
- The new `csv/message_statuses.csv` and `csv/reply_outbox.csv` are initialized automatically
  and included in webhook persistence. Keep them private with the other operational CSVs.
- Simulate a failed STOP/UTR acknowledgement: the instruction remains saved, HTTP is 503, and
  the independent worker retries only a still-relevant stored acknowledgement. No background task is relied on after 200.
- Repeat the same `admin.py verify ... --activate`: dates must not extend twice. New activations
  store `applied_payment_refs` with subscriber dates and `activation_state` on the payment.
  A legacy SUCCESS payment without markers fails closed: reconcile whether it was applied before
  retrying. If already applied, add its reference to the subscriber marker and mark it APPLIED;
  only mark activation_state PENDING after proving it has never granted an entitlement.
- Welcome, renewal and delivery use the approved `dailydarshan_subscription_status` template.
  Activation queues one task per applied payment in `csv/welcomes.csv`, committed with activation
  state. After publication, delivery runs `scheduler.py welcome`. The worker also idempotently
  creates a missing welcome row for every ACTIVE subscriber `applied_payment_refs` value. This
  supports manual CSV activation provided the reference is genuine and the existing
  `subscription_id` is preserved. The worker checks consent and public page metadata before sending.
  Welcome outcome remains in `welcomes.csv`, but it reserves the shared date+mobile `sentlog.csv`
  slot before contacting Meta, so welcome, renewal and delivery cannot all send on the same day.
  QUEUED/FAILED tasks can retry; PENDING/UNKNOWN tasks require evidence-based reconciliation.
  Never clear an uncertain reservation merely because it is old. Welcome errors are surfaced
  after the remaining delivery steps, so other eligible subscribers can still be processed.
  For a manual retry after publication, run `python scheduler.py welcome` from an up-to-date
  main checkout with the normal WhatsApp credentials and signing setup.
  When activating through `subscribers.csv`, set `status=ACTIVE`, retain the mobile's existing
  `subscription_id`, and append the genuine reference to semicolon-separated
  `applied_payment_refs`. Do not create another subscriber row or page ID for the same mobile.
- Production webhook replies are persisted in `csv/reply_outbox.csv` with conversation state
  before contacting Meta. Customer requests send only their own new replies; uncertain
  attempts remain blocked. The Retry WhatsApp Replies workflow has been removed.
  Recovery requires an explicit signed call to POST /internal/retry-replies with a JSON
  timestamp and X-Hub-Signature-256 HMAC using Render's WHATSAPP_APP_SECRET.
  The endpoint rejects unsigned and stale requests; there is no automatic retry schedule.
  Calls use Render's existing state lock and GitHub persistence. Keep one Render instance
  and one Uvicorn worker; this file-lock architecture is not a distributed lock.
  No additional Meta template is needed for these in-session replies.
- Replies retry at most five attempts, with exponential backoff from 60 seconds capped at
  one hour. Replies older than 23 hours from the triggering inbound message, legacy queued
  rows without freshness metadata, and obsolete conversation versions are cancelled.
  Changes to subscriber/payment data also invalidate old instructions. The customer can
  send MENU and choose the relevant action for a fresh response. PENDING/UNKNOWN attempts remain blocked and
  produce a failing retry job for operator investigation; they are never blindly resent.
- Payment instructions/Payment status reconstructs the current checkout from saved data.
  Existing payment references are reused and confirmed UTRs show verification pending.
  Subscription status shows entitlement without applying another activation. Continue,
  Resend and Back are hidden; legacy inputs remain accepted for older messages.
  A user-requested payment instruction is a fresh reply, separate from the daily send ledger. If an
  earlier uncertain reply was actually accepted, both copies can still arrive.
- Customers can send `MENU`, `Radhe Radhe`, `RENEW` or `SUBSCRIBE` at any
  conversational step. These commands return to navigation and never save themselves as a name,
  alter consent, or replace a paid payment. Old CTA taps are validated against the current plan
  and state; a missing/expired CTA shows the menu.

Menu verification: new users receive View plans only; incomplete signup returns to name or
consent. Expired users receive Renew with all configured plans. Active users outside the
three-day renewal window receive Upgrade with only plans strictly larger than their current
plan; the largest active plan has no upgrade action. Active users inside the three-day window
receive Renew with their current plan plus larger plans, including Yearly renewal for a Yearly
subscriber. When no larger plan exists, the CTA description is “Renew your current plan”;
otherwise it is “Renew or choose a larger plan.”
Eligibility uses the Asia/Kolkata date: days remaining 3, 2, 1 and 0 permit same-plan
renewal; day 4 does not. Changing `renewal.reminder_days` changes reminder scheduling only.
The page renewal CTA also opens at three days. Upgrades remain available within the Renew list.
Unpaid lower-plan and early same-plan checkouts are superseded on return. Submitted UTRs
remain reviewable, including a reference-qualified UTR for an older superseded checkout.
Active opted-out users additionally receive Resume messages. Only existing dated subscriptions have a
Subscription status menu option. Help, Stop messages, Continue, Resend and Back are hidden.
Subscription status includes the current plan type. Typed STOP and consent No thanks remain
supported. Unpaid checkout shows payment instructions plus Change plan for new users, Renew for
expired users, and Upgrade/Renew for active users according to the renewal window. UTR review retains the applicable plan
action while stale plan taps are revalidated against the current entitlement. Verify active renewal
leaves the current paid plan/dates unchanged until admin approval, and extends from expiry.
Resume messages requires consent but no payment. STOP changes consent, not paid dates.
Run `pytest tests/test_plan_eligibility.py -q` to verify all four plan combinations at
expiry offsets -1, 0, 1, 2, 3, 4 and 30, reminder-setting independence, old payment buttons,
consent recovery, repeated selection, and UTR recovery after entitlement changes.
No new secrets or Meta templates are required. `welcomes.csv` gains the optional trailing
column `publication_verified`; existing CSV headers are upgraded automatically on repository
rewrite. Blank means publication has not been confirmed by this worker. The worker persists
`true` after checking the published page, independently of notification outcome or opt-out.
Old PENDING/UNKNOWN/SENT/DELIVERED/READ welcome rows imply the existing publication gate passed.
The webhook never performs a network publication check while replying.
Rejected payments remain blocked for user checkout until administrator resolution. Verify
the original payment using `admin.py verify <reference> --activate --commit` only after
validating payment proof. If no payment occurred, an administrator must explicitly reconcile
  the rejected record before opening another checkout; rejection alone is not permission to pay again.
Use `python admin.py list-rejected` and inspect `PAYMENT_REVIEW_REQUESTED` events in logs daily.
The customer Request review action records a request, not an automatic admin notification.
If proof validates a payment, verify its original reference. Only after confirming no payment
occurred, run `python admin.py reopen-payment <reference> --no-payment-confirmed --commit`.
This preserves the old row as SUPERSEDED and permits a fresh checkout; it does not erase UTRs.

Acceptance checks for edge cases:

- Send Hi, Hello, Radhe Radhe and MENU while awaiting name, consent, UTR and admin review.
  Confirm the relevant options reappear without changing plan/reference/UTR/consent.
- Change plans, then submit a bare UTR: it must request the original reference, not attach
  to the newest plan. `UTR <reference> <12 digits>` restores the paid-against checkout for review.
- STOP during renewal review, then Resume messages and I agree: only consent changes.
- Reject numeric/payment-looking names. Missing name/consent takes priority over unpaid checkout.
- Verify a new payment for CANCELLED status twice: exactly one fresh term, no consent grant.
- Delay welcome until after expiry/cancellation: no outdated activation welcome is sent.
- Regenerate with an older image then create today's canonical image without rerendering:
  daily delivery must still fail the published page's actual-image-date check.
- Confirm a queued 'preparing page' reply is cancelled when publication state changes.

No new secrets or Meta templates are required for these changes. Review requests need regular
administrator attention; no automatic payment approval or reconciliation is introduced.
Activation/renewal approval continues to queue a welcome-status record after publication using
`dailydarshan_subscription_status`. Welcome, renewal and delivery retain separate audit CSVs but
share the maximum-one-per-day date+mobile reservation in `sentlog.csv`.
Run `pytest -q` including `tests/test_product_journey.py`; live acceptance must additionally
exercise each menu using the configured production sender and verify Meta callbacks.
After a valid 12-digit UTR, verify the bot displays `Confirm UTR` and `Change UTR`.
The draft is persisted in `conversations.csv` only; `payments.csv.utr` and subscription
state must remain unchanged. Existing conversation CSV headers are expanded on write.
Test a corrected draft, an old confirmation button, a different sender, and a restart
before confirmation. Opening payment details should resume the confirmation.
Only after tapping `Confirm UTR`, verify the reply names the latest UTR and payment reference, says it
replaced the previous UTR (if any), requests admin verification within 24 hours, and says not to
pay again. Production saves that acknowledgement in the outbox
before sending; retrying the same inbound message must not duplicate it. If the live user
still receives nothing, inspect reply_outbox status/error and the configured sender ID.
- Reconcile PENDING/UNKNOWN sends against provider evidence before any manual change. A failed
  outcome push may leave the reservation ID without a provider ID; do not assume no send occurred.
  This is duplicate prevention under uncertainty, not guaranteed exactly-once delivery.
- Webhook persistence has no clock-based quiet window. It remains available during scheduled
  and manual workflows; repository conflicts fail closed with 503 so Meta can retry safely.
  CSV snapshot files are fetched concurrently and unchanged snapshots are reused. Inbound state
  and the outbound PENDING reservation are committed atomically before Meta is contacted; provider
  outcomes are persisted in one follow-up commit. Meta transport runs outside the state lock;
  outcome persistence refreshes main and updates only the matching reservation fields, preserving
  concurrent status callbacks and customer changes. A bounded wait still returns 503 if one of
  the short Git transactions overlaps; Render logs this expected backpressure as one warning line.
- Callback records can precede send records and are reconciled on later webhook/delivery runs.
  A delivered/read callback must not be reversed by a delayed failed callback.

Pull requests and code/configuration pushes to `main` trigger the **Tests** CI workflow. CSV-only
Render persistence commits and docs-only pushes skip CI, preventing webhook traffic from flooding
the runner queue. They intentionally do not trigger Pages CD, because the repository uses the custom
Actions publisher rather than the legacy branch publisher. A previously published subscriber page
stays reachable until a later successful deployment replaces or removes it. To publish an urgent
mid-day activation, manually run **Deploy Daily Darshan Pages**; it will start delivery only after
publication succeeds.

---

## Part 1b — Utility-Template Delivery Mode (optional, cost optimization)

By default `config.json` ships with `delivery.mode = "utility_template"`. Instead of sending
the darshan image inline, this mode sends an approved template whose dynamic URL button links to a
per-subscriber **GitHub Pages** page containing today's image and delivery status. Billing
depends on Meta's assigned category and current country rate; verify both in WhatsApp Manager.

### One-time setup

1. **Enable GitHub Pages** — repo → **Settings → Pages** → *Build and deployment* → source
   **GitHub Actions**. Pages are written to `docs/<subscription_id>/index.html` and published
   by `Deploy Daily Darshan Pages` before WhatsApp delivery begins.
   > Pages is public. Pages show a customer name and subscription expiry (not a mobile number) and use an unguessable
   > `subscription_id` in the path, plus `noindex`. Confirm you're comfortable with per-subscriber
   > status pages being publicly reachable by URL.

   **When pages are generated (so a new user's URL is never a 404):**
   - The daily **image job** (`scheduler.py image`) regenerates *all* subscriber pages on
     every run — even when today's image already exists — so anyone who signed up since the
     last run gets a page.
   - **Activation** (`admin.py verify --activate`) renders that one subscriber's page locally
     and `--commit` pushes it. A commit is not a deployment: for a mid-day publication, manually
     run **Deploy Daily Darshan Pages** after reviewing the page. A successful manual deployment
     then starts Daily Delivery.
   - A page becomes reachable only after the Pages deployment succeeds.

2. **Set the config URLs** in `config.json` → `delivery`:
   - `page_base_url` — the base users are sent to, e.g. `https://<user>.github.io/daily-darshan/docs`
     (or a custom branded domain). The per-subscriber URL is `page_base_url/<subscription_id>`.
   - `image_public_base` — public base for images, e.g. `https://<user>.github.io/daily-darshan`.
   - `template_name` / `template_lang` — your approved template.

3. **Verify the approved template** in WhatsApp Manager (see caveat below). Exact body:

   > Radhe Radhe {{1}} Ji,
   >
   > Your Daily Darshan delivery status has been updated as {{2}}.
   > Please check subscription status in personalised link below on VIPSeva.com.

   Use template name `dailydarshan_subscription_status`, language `en`, body variable
   `{{1}}` for the customer name, `{{2}}` for subscription status, an **Image** header,
   and one dynamic **Visit website** button labelled
   **Daily Darshan Subscription** with URL `https://vipseva.com/{{1}}`. The button's `{{1}}` receives
   only the subscriber's unguessable subscription ID; Meta appends it to the URL prefix.

   Welcome, renewal and delivery all use this template and `en`. Welcome status is `Activated`;
   other sends use `Active`, `Expiring in 3 days`, `Expiring in 2 days`, `Expiring in 1 day`,
   or `Expiring today` based on the run date. Expired subscribers are not automatically messaged.
   The sender supports the `Expired` status for future explicit use but does not start a new flow.
   No extra body variables, referral link, or button are sent.

   All three production header settings below are `image`; the new approved template always
   requires an image. Legacy templates without media headers use `template_header: "none"`.
   To switch an
   approved template to a dynamic Meta **Image** header without another code change, update its
   template name and set the matching configuration field to `"image"`:

   - `delivery.template_header` for daily delivery
   - `delivery.welcome_template_header` for activation welcomes
   - `renewal.template_header` for renewal reminders

   The header receives today's publicly deployed canonical image URL. The body and URL-button
   parameters do not change. Do not set `image` until the corresponding Meta template is approved
   with an Image header; a header mismatch is rejected by Meta. A missing/invalid daily image
   fails the configured media-header phase before a WhatsApp send is attempted.

4. **Backfill subscription ids** for any existing subscribers (new signups get one automatically):
   ```bash
   python -m migrations.backfill_subscription_ids --commit
   ```

### ⚠️ Utility-approval caveat

Meta assigns the template category from **content and intent**, and can continuously
re-evaluate it. A daily template can be **reclassified to Marketing** if it looks like
recurring content delivery rather than a genuine account/status update; transport type
(link, image or PDF) does not determine category. Never hard-code a cost assumption:

1. Submit the template as Utility and confirm the **assigned category** in WhatsApp Manager.
2. Send daily for a week and verify it **stays** Utility.
3. If it flips to Marketing, either accept the cost, or switch `delivery.mode` back to `image`
   (better engagement) and rely on the free 24-hour session window for cost.

To revert to inline images at any time: set `delivery.mode = "image"` in `config.json`.

---

## Part 2 — Admin Payment Verification in CSV

**Key rule (Tech Doc §6):** a user-submitted UTR is **only a signal that the user claims
they paid** — it is **not** proof of payment. The **`reference_id`** is the value the admin
verifies against; the UTR is stored as supporting evidence only. Nothing activates
automatically. An admin must:

1. Confirm the real UPI transaction,
2. Mark the payment `SUCCESS` in `payments.csv`, and
3. Ensure the subscriber is **activated** (a separate step — see below).

> **Verification and activation are decoupled.** Setting a payment to `SUCCESS` records
> that money was received; it does **not** by itself flip the subscriber to `ACTIVE` with
> start/end dates. Both must be done for the subscriber to receive deliveries.

### The `payments.csv` row

Columns: `reference_id,mobile,plan,amount,status,utr,created_at,verified_at`

```
reference_id,mobile,plan,amount,status,utr,created_at,verified_at
DD2608190001,919999999999,monthly,199,PENDING,123456789012,2026-08-19T14:05:00,
```

- `status`: `PENDING` → `SUCCESS` (or `FAILED` if fraudulent/unmatched).
- `verified_at`: set to the verification timestamp when marking `SUCCESS`.

### Which payment to verify, and the role of the UTR vs. the reference id

**Which rows need action:** the verification queue is every row in `payments.csv` with
`status = PENDING` **and** a non-empty `utr`. A `PENDING` row with an empty `utr` means the
user selected a plan but hasn't paid/submitted a UTR yet — **not** actionable. List the queue
with `python admin.py list-pending`.

**The `reference_id` is the operational key — the UTR is only a claim.**

- The **`reference_id`** (`DD` + `YYMMDD` + daily sequence, e.g. `DD2608190001`) is
  **system-generated and unique**. It is how you *select which order* to verify, and it is
  embedded in the UPI intent as the `tn` (transaction note), so it should also appear in the
  UPI transaction description. **This is the value the admin verifies against.**
- The **`utr`** is a 12-digit number **typed by the user** claiming they paid. Treat it as a
  *signal that the user says a payment was made* — **not proof**. A UTR can be mistyped, made
  up, reused, or belong to an unrelated transaction. The system stores it as evidence but does
  **not** treat a UTR as validation on its own.

**Therefore:** the admin uses the **`reference_id` to identify the order**, then manually
confirms that a **real UPI credit of the matching `amount`** actually landed (matching the
`utr` and/or the `tn=reference_id` note against the bank/UPI statement). Only after that
human money-check does the admin mark the reference id `SUCCESS`. No automated UTR matching
happens — approval is a deliberate human trust gate (Tech Doc §6).

### Step-by-step approval

1. **Find the pending payment.** Run `python admin.py list-pending` (or open
   `csv/payments.csv` on the `main` branch). Identify the row by its **`reference_id`** — this
   is the key you will verify. The `utr` shown is the user's *claim*, used only as a matching
   hint in the next step.

2. **Verify the real transaction.** In your actual UPI/bank statement, confirm a credit
   exists matching the `amount` and the `utr` / `tn=reference_id` note. This is the human
   check the system deliberately cannot do for you — the UTR alone is not proof.

3. **Edit the row** — set `status` to `SUCCESS` and fill `verified_at`:
   ```
   DD2608190001,919999999999,monthly,199,SUCCESS,123456789012,2026-08-19T14:05:00,2026-08-19T14:40:00
   ```
   If it does not match, set `status` to `FAILED` and leave `verified_at` blank.

4. **Commit the change.**
   - GitHub web UI: **Edit (pencil) → Commit changes** directly to `main`
     (message e.g. `Verify payment DD2608190001`).
   - Or locally:
     ```bash
     git pull --rebase
     # edit csv/payments.csv
     git add csv/payments.csv
     git commit -m "Verify payment DD2608190001"
     git push
     ```

5. **Activate the subscriber** — separate step. Eligibility requires the subscriber to be
   `ACTIVE` with start/end dates, in addition to the `SUCCESS` payment.

   - **Recommended — one-step admin CLI (verify + activate + commit):** instead of steps
     3–5 you can do everything in a single command:
     ```bash
     python admin.py verify DD2608190001 --activate --commit
     ```
     This marks the payment `SUCCESS` (sets `verified_at`), transitions the subscriber
     `PENDING -> ACTIVE` with `start_date`/`end_date` computed from the plan, **renders that
     subscriber's GitHub Pages page**, and commits the changed CSVs + `docs/` page. It is not
     public until the next Pages deployment; run **Deploy Daily Darshan Pages** manually when
     immediate publication/delivery is required. Omit
     `--commit` to review before committing yourself; omit `--activate` to only verify the
     payment.

     > **Renewals are auto-detected.** If the subscriber is already `ACTIVE`/`PAUSED`/`EXPIRED`,
     > `--activate` **renews** instead — extending `end_date` from the current expiry (not from
     > today), per Tech Doc §29 — and prints `Renewed …`. Pass `--renew` to force renewal
     > semantics explicitly.

     Related commands:
     ```bash
     python admin.py list-pending          # show payments awaiting verification
     python admin.py verify DD2608190001    # verify only (status -> SUCCESS)
     python admin.py reject DD2608190001    # mark a non-matching payment FAILED
     ```

   - **Manual activation via the use case** (if you already edited `payments.csv` by hand):
     ```bash
     python -c "from config import Container; Container().subscriber_service.activate('919999999999')"
     git add csv/subscribers.csv csv/logs.csv
     git commit -m "Activate 919999999999"
     git push
     ```
     This transitions `PENDING -> ACTIVE` and sets `start_date` / `end_date` from the plan
     length.

   - **Manual CSV edit** (only if you compute dates yourself). In `csv/subscribers.csv`
     (`mobile,plan,start_date,end_date,status,opt_in`):
     ```
     mobile,plan,start_date,end_date,status,opt_in
     919999999999,monthly,2026-08-19,2026-09-18,ACTIVE,true
     ```
     `end_date = start_date + plan days`. Current catalog: starter = 3 days,
     weekly = 30 days, monthly = 90 days, yearly = 365 days. These are configuration keys
     shown to users, so rename them in `config.json` if the labels should describe cadence.
     Commit as above.

Once the payment is `SUCCESS` **and** the subscriber is `ACTIVE` / opted-in / unexpired,
the next delivery run picks them up automatically. The `date + mobile` idempotency key in
`sentlog.csv` prevents duplicate sends.

### Concurrency caution

Do **not** hand-edit CSVs while a scheduler job might be committing:

- Always `git pull --rebase` **before** editing, and push promptly after.
- The scheduler makes at most five push attempts after branch-advance rejections,
  fetching and rebasing between attempts; it never force-pushes. Only concurrent
  append-only changes to `csv/logs.csv` are combined automatically. Business CSV
  conflicts and log edits/deletions abort the rebase and fail for reconciliation.
  Git commands time out after 60 seconds and failures include sanitized stderr.
  Daily Image instead fetches `main` and builds in a disposable checkout on every
  retry. It reuses downloaded candidates but reloads CSV/config data, regenerates
  pages and applies expiry before one atomic push. Five attempts with randomized
  delays bound contention. Audit logs are uploaded separately as 30-day workflow
  artifacts even on failure; image publication no longer modifies `logs.csv`.
  A final failure triggers the existing ops-alert workflow. Other concurrent
  writers can still cause a bounded failure, but cannot cause a stale page rebase.
- The webhook commits `csv/payments.csv`, `csv/subscribers.csv`, `csv/processed.csv` and logs as
  users subscribe. Meta failure-status callbacks also reconcile `csv/sentlog.csv` and
  `csv/renewals.csv`, so pull before editing to pick up any rows it changed.
- Git history is the audit trail — every verification/activation is a traceable commit.
# Webhook reply priority and recovery

The legacy reply_retries.csv is no longer initialized, read, or synchronized.
Existing copies are retained as historical data, not automatically replayed.
All new acknowledgement recovery uses reply_outbox.csv. MAX_RETRIES is three:
one initial send plus three retries. Confirmed failures on the fourth attempt
become CANCELLED immediately and are excluded from automatic retries. Ambiguous
PENDING/UNKNOWN outcomes remain held for reconciliation, never blindly resent.

Consistency review: inbound changes and reply reservations are committed to one
GitHub snapshot before sending. Non-force branch updates reject conflicting
writers; persistence failures remain retryable HTTP 503 responses. Meta transport
runs outside the local state lock. After transport, the outcome is merged into a
fresh snapshot and any already-recorded delivery receipt is reconciled immediately.
Thread-lock and process-lock waits share one timeout budget rather than restarting
the budget between locks.

This is not an exactly-once distributed transaction. A crash after Meta acceptance
but before storing the message ID can leave an ambiguous reservation. Do not reset
it blindly: use provider evidence to reconcile it. A fresh user command can still
proceed. GitHub outages prevent synchronous durable processing, and retry scheduling
does not guarantee eventual delivery after exhaustion or expiry. Cancelled replies
need a fresh user request; a missing receipt cannot be treated as delivery proof.

UTF-8 CSV writes are embedded in the Git tree request, followed by one commit
and one non-force branch update. This removes one network request per changed
CSV while retaining atomic persistence. Binary writes retain explicit blob uploads.
Performance regression tests cover signed Hi, MENU, STATUS and Radhe Radhe
requests with simulated 20 ms storage operations and a pending reply backlog.
Their one-second local ceiling is a regression budget, not a production SLA;
Render/GitHub/Meta latency still requires live measurement.

Changed immutable CSV blobs are fetched with at most four concurrent reads.
All reads must succeed before the refreshed snapshot is installed. Retry workers
yield immediately when the state lock is occupied and reserve only one reply per
invocation, reducing interference with current customer requests. This is bounded
background work, not a strict priority queue across Render processes.

Customer requests send only replies created by that request. Conversation state
and outbound reservations are committed before transport; provider outcomes are
committed afterward. Old pending replies do not cause a new request to return
503. Persistence and inbound processing failures still return 503 for recovery.

There is no scheduled reply retry workflow. Explicit recovery calls retry confirmed
failed replies receive at most three retries after the initial send, then become
CANCELLED. Superseded or expired replies are cancelled. PENDING/UNKNOWN sends have
an ambiguous provider outcome and are never blindly resent: reconcile them using
provider evidence. A fresh MENU request can proceed while these records remain.
