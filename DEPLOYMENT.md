# Deployment & Admin Guide

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

Once pushed, the image, Pages-deployment and delivery workflows appear under **Actions**.
Only Daily Image has a cron; successful completion advances through the gated chain:

| Workflow | Cron (UTC) | Local time | Action |
|----------|-----------|------------|--------|
| **Daily Image** (`image.yml`) | `1 3 * * *` | 08:31 IST target | Prune logs, store UUID-prefixed candidates/canonical image, regenerate pages, expire subscribers and prune inactive pages/old images → signed commits |
| **Deploy Daily Darshan Pages** (`deploy-pages.yml`) | Event-driven | After successful image | Publish `docs/` once through GitHub Actions |
| **Daily Delivery** (`delivery.yml`) | Event-driven | After successful Pages deployment | Renewal reminder or today's published page link, at most one successful contact per subscriber/date |

### E. Test without waiting for the cron (manual run)

The image, Pages-deployment and delivery workflows support `workflow_dispatch`:

1. **Actions** tab → pick **Daily Image** (or a recovery workflow) → **Run workflow** →
   select `main` → **Run workflow**.
2. Watch the run: it checks out the repo, installs dependencies, runs `pytest`, verifies the
   signing key/passphrase, executes the job, and commits results back to the repository.

Choose the recovery entry point deliberately:

- **Daily Image** on `main` performs image preparation and automatically continues through one
  Pages deployment and Daily Delivery. Historical backfill misses warn; today's image must exist.
- **Deploy Daily Darshan Pages** publishes the current `main` `docs/` tree once and then starts
  Daily Delivery. Use this after a mid-day activation or manual page regeneration.
- **Daily Delivery** sends against the already-published site. It does not build or deploy Pages.
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
2. Verify the payment and activate or renew the subscriber. Confirm the signed commit includes the
   CSV state and subscriber page. This commit runs **Tests**, but does not itself publish Pages.
3. For the normal daily path, wait for or manually run **Daily Image** on `main`. Confirm today's
   UUID-prefixed canonical image and pages are committed and the run succeeds.
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
- The new `csv/message_statuses.csv` and `csv/reply_retries.csv` are initialized automatically
  and included in webhook persistence. Keep them private with the other operational CSVs.
- Simulate a failed STOP/UTR acknowledgement: the instruction remains saved, HTTP is 503, and
  redelivery retries only the stored acknowledgement. No background task is relied on after 200.
- Repeat the same `admin.py verify ... --activate`: dates must not extend twice. New activations
  store `applied_payment_refs` with subscriber dates and `activation_state` on the payment.
  A legacy SUCCESS payment without markers fails closed: reconcile whether it was applied before
  retrying. If already applied, add its reference to the subscriber marker and mark it APPLIED;
  only mark activation_state PENDING after proving it has never granted an entitlement.
- Configure and approve `daily_darshan_welcome` separately from
  `daily_darshan_delivery_update`. Activation queues one task per payment in `csv/welcomes.csv`,
  committed with activation/page state. After publication, delivery runs `scheduler.py welcome`.
  The worker checks consent and public page metadata before sending, using a separate ledger.
  QUEUED/FAILED tasks can retry; PENDING/UNKNOWN tasks require evidence-based reconciliation.
  Never clear an uncertain reservation merely because it is old. Welcome errors are surfaced
  after the remaining delivery steps, so other eligible subscribers can still be processed.
  For a manual retry after publication, run `python scheduler.py welcome` from an up-to-date
  main checkout with the normal WhatsApp credentials and signing setup.
- Production webhook replies are persisted in `csv/reply_outbox.csv` with conversation state
  before contacting Meta. Subsequent webhook processing drains queued/failed replies; uncertain
  attempts remain blocked. There is no independent timer for reply retries; monitor this ledger.
- Customers can send `BACK`, `GO BACK`, `MENU`, `Radhe Radhe`, `RENEW` or `SUBSCRIBE` at any
  conversational step. These commands return to navigation and never save themselves as a name,
  alter consent, or replace a paid payment. Old CTA taps are validated against the current plan
  and state; a missing/expired CTA shows the menu.
- Reconcile PENDING/UNKNOWN sends against provider evidence before any manual change. A failed
  outcome push may leave the reservation ID without a provider ID; do not assume no send occurred.
  This is duplicate prevention under uncertainty, not guaranteed exactly-once delivery.
- Callback records can precede send records and are reconciled on later webhook/delivery runs.
  A delivered/read callback must not be reversed by a delayed failed callback.

Pull-request merges, Render persistence commits and other pushes to `main` trigger the **Tests** CI
workflow. They intentionally do not trigger Pages CD, because the repository uses the custom
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

3. **Submit and get the template approved** in WhatsApp Manager (see caveat below). Suggested body:
   > "Radhe Radhe {{1}} Ji, Your Daily Darshan delivery status has been updated. It is your personalised link. Do not share this link with others."

   Use template name `daily_darshan_delivery_update`, language `en`, body variable
   `{{1}}` for the customer name, and a dynamic **Visit website** button labelled
   **Daily Darshan** with URL `https://vipseva.com/{{1}}`. The button's `{{1}}` receives
   only the subscriber's unguessable subscription ID; Meta appends it to the URL prefix.

   Renewal uses this same template and language with the same customer-name and URL-button
   parameters. It intentionally does not submit the expiry date as another body variable.

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
- The scheduler retries once on push conflict via `pull --rebase` and never force-pushes,
  but an in-progress manual edit can still collide.
- The webhook commits `csv/payments.csv`, `csv/subscribers.csv`, `csv/processed.csv` and logs as
  users subscribe. Meta failure-status callbacks also reconcile `csv/sentlog.csv` and
  `csv/renewals.csv`, so pull before editing to pick up any rows it changed.
- Git history is the audit trail — every verification/activation is a traceable commit.
