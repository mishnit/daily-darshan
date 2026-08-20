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

`GITHUB_REPO` is auto-provided in Actions via `${{ github.repository }}`, and the built-in
`GITHUB_TOKEN` covers the commit/push — you do **not** add those manually. The image job
needs no secrets.

> **Webhook host secrets are separate.** The serverless webhook (`main.py`) needs its own
> environment variables set on its host (Render/Fly), not as GitHub Actions secrets:
> `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WEBHOOK_VERIFY_TOKEN`,
> `WHATSAPP_APP_SECRET` (Meta app secret — verifies the `X-Hub-Signature-256` on inbound
> webhooks; if unset, signature checks are skipped, so always set it in production), and
> **`GITHUB_TOKEN` + `GITHUB_REPO` (required for durability)** — with
> `persistence.mode=github_api`, the webhook uses these to pull/push its CSV writes to the
> shared repo. **Without them the webhook writes local-only and those writes are lost on the
> ephemeral host and never reach the scheduler/admin.** See the
> [README Secrets table](./README.md#secrets) and the durability note in the
> [README Deployment section](./README.md#deployment) for details.

### D. Confirm the workflows are registered

Once pushed, `.github/workflows/image.yml` and `.github/workflows/delivery.yml` appear
under the **Actions** tab automatically. They run on schedule:

| Workflow | Cron (UTC) | Local time | Action |
|----------|-----------|------------|--------|
| **Daily Image** (`image.yml`) | `30 2 * * *` | 08:00 IST | Fetch → validate → store `images/YYYY-MM-DD.jpg` → commit |
| **Daily Delivery** (`delivery.yml`) | `0 3 * * *` | 08:30 IST | Expire lapsed subscriptions (`ACTIVE`→`EXPIRED`), renewal reminders, then deliver today's image → update CSVs → commit |

### E. Test without waiting for the cron (manual run)

Both workflows support `workflow_dispatch`:

1. **Actions** tab → pick **Daily Image** (or **Daily Delivery**) → **Run workflow** →
   select `main` → **Run workflow**.
2. Watch the run: it checks out the repo, installs deps, runs `pytest`, executes the job,
   and commits results back to the repository.

> **Notes on scheduled runs:** GitHub disables scheduled workflows in a repo with **no
> activity for 60 days**, and cron start times can be delayed under load. For a personal
> MVP this is usually acceptable.

---

## Part 1b — Utility-Template Delivery Mode (optional, cost optimization)

By default `config.json` ships with `delivery.mode = "utility_template"`. Instead of sending
the darshan image inline (billed as **Marketing**, ~₹0.88/msg), this mode sends an approved
**utility template** whose `{{2}}` links to a per-subscriber **GitHub Pages** page that renders
today's image + delivery status. Utility is ~7× cheaper (~₹0.125/msg) — **if** Meta classifies
the template as Utility.

### One-time setup

1. **Enable GitHub Pages** — repo → **Settings → Pages** → *Deploy from a branch* → branch
   `main`, folder **`/docs`**. Pages are written to `docs/<subscription_id>/index.html`.
   > Pages is public. Pages carry no PII (no mobile number) and use an unguessable
   > `subscription_id` in the path, plus `noindex`. Confirm you're comfortable with per-subscriber
   > status pages being publicly reachable by URL.

   **When pages are generated (so a new user's URL is never a 404):**
   - The daily **image job** (`scheduler.py image`) regenerates *all* subscriber pages on
     every run — even when today's image already exists — so anyone who signed up since the
     last run gets a page.
   - **Activation** (`admin.py verify --activate`) renders that one subscriber's page
     immediately, so a mid-day signup has a working URL without waiting for the next image job.
   - GitHub Pages publishes a commit in ~1 minute, so a page is reachable shortly after the
     commit that creates it (not instantaneously).

2. **Set the config URLs** in `config.json` → `delivery`:
   - `page_base_url` — the base users are sent to, e.g. `https://<user>.github.io/daily-darshan/docs`
     (or a custom branded domain). The per-subscriber URL is `page_base_url/<subscription_id>`.
   - `image_public_base` — public base for images, e.g. `https://<user>.github.io/daily-darshan`.
   - `template_name` / `template_lang` — your approved template.

3. **Submit and get the template approved** in WhatsApp Manager (see caveat below). Suggested body:
   > "Hello {{1}}, your Daily Darshan Delivery status has been updated. Please log into your dashboard to view your profile and delivery status {{2}}

4. **Backfill subscription ids** for any existing subscribers (new signups get one automatically):
   ```bash
   python -m migrations.backfill_subscription_ids --commit
   ```

### ⚠️ Utility-approval caveat

Meta assigns the template category from **content and intent**, and **continuously
re-evaluates** it. A daily template can be **reclassified to Marketing** (₹0.88) if it looks
like recurring content delivery rather than a genuine account/status update — the transport
(link vs image vs PDF) does **not** change the category. Treat the ₹0.125 utility rate as
**best-case, not guaranteed**:

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
DD2608190001,919999999999,monthly,49,PENDING,123456789012,2026-08-19T14:05:00,
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
   DD2608190001,919999999999,monthly,49,SUCCESS,123456789012,2026-08-19T14:05:00,2026-08-19T14:40:00
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
     subscriber's GitHub Pages page immediately** (so their utility-template URL works right
     away in `utility_template` mode), and commits the changed CSVs + `docs/` page. Omit
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
     `end_date = start_date + plan days` (monthly = 30, quarterly = 90, yearly = 365).
     Commit as above.

Once the payment is `SUCCESS` **and** the subscriber is `ACTIVE` / opted-in / unexpired,
the next delivery run picks them up automatically. The `date + mobile` idempotency key in
`sentlog.csv` prevents duplicate sends.

### Concurrency caution

Do **not** hand-edit CSVs while a scheduler job might be committing:

- Always `git pull --rebase` **before** editing, and push promptly after.
- The scheduler retries once on push conflict via `pull --rebase` and never force-pushes,
  but an in-progress manual edit can still collide.
- The webhook also commits `csv/payments.csv`, `csv/subscribers.csv`, and
  `csv/processed.csv` (a message-dedup log) as users subscribe — so pull before editing to
  pick up any rows it added.
- Git history is the audit trail — every verification/activation is a traceable commit.
