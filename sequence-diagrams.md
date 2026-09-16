# Daily Darshan — Sequence Diagrams

## Webhook snapshot and bounded reply processing

```mermaid
sequenceDiagram
    participant User
    participant Web as Webhook
    participant Git as Git main
    participant Meta
    User->>Web: Inbound message
    Web->>Git: Read branch head and immutable snapshot
    Note over Web,Git: Reuse only a successfully loaded baseline and unchanged blobs
    Web->>Web: Apply state and reserve up to five replies for this customer
    Web->>Git: Commit state and reservations
    Git-->>Web: Commit accepted
    Web->>Meta: Send reserved replies
    Meta-->>Web: Acceptance or definitive failure or ambiguous result
    Web->>Git: Persist results
    Note over Web,Git: Ambiguous sends remain blocked for reconciliation
```

The retry endpoint uses the same bounded preparation across customers. A status-only
callback does not drain unrelated replies. Commit failures before sending abort the
send; failures after sending require reconciliation rather than blind retries.

These diagrams show **when** each operation runs and **at what stage it writes to
local disk vs. the shared GitHub repo (`main` branch)**.

Three kinds of flows:

- **Image entry point** — Daily Image currently uses manual/external dispatch. Collection
  queues admin review; the committed decision starts regeneration, deployment and delivery.
- **Manual Actions** — image, Pages deployment, page regeneration and delivery can be run from
  the Actions tab. Direct delivery does not rebuild or redeploy pages.
- **Event-driven (untimed)** — the webhook on Render, triggered by WhatsApp/Meta. Writes via
  the **Git Data API** (`GitHubApiRepository` through `RepoSync`): snapshot read before handling,
  atomic publication before HTTP acknowledgement.

| Operation | Trigger | Time (UTC / IST) | Writes to repo? |
|-----------|---------|------------------|-----------------|
| Prune `logs.csv` + `sentlog.csv` | image and delivery workflows | At workflow execution | Only when rows outside the inclusive 30-day window exist |
| Fetch/store image candidates | `image.yml` manual/external dispatch | On demand | Yes — candidates and pending admin review batch |
| Expire subscriptions + prune inactive pages | end of `image.yml`; delivery safety repeat | Before publication | Yes when state/pages change |
| Deploy `docs/` | successful approved page regeneration, or manual | Event-driven | No repo write; gate requires approved bytes and rendered stamp |
| Send renewal reminders | successful Pages deployment, or manual delivery | After publication | Yes — renewals, shared sentlog and logs |
| Deliver today's page link | same delivery run | After renewal/gap | Yes — sentlog and logs |
| GET `/webhook` (verify) | Meta handshake | any (setup) | No — read-only |
| POST `/webhook` (inbound) | user message | any | Yes — `RepoSync` pull then push |
| opt-in / name / subscribe / plan | inside POST | any | Yes (via the POST push) |
| User makes payment (UTR) | inside POST | any | Yes (via the POST push) |
| Admin verification | WhatsApp ADMIN or CLI | any | Atomic webhook commit, or CLI with `--commit` |
| renewal reminder opt-out (STOP) | inside POST | any | Yes (via the POST push) |

Webhook persistence has no clock-based quiet window. Fixed windows cannot cover delayed or
manual Actions runs, and deferred writes on ephemeral disk can be lost. Repository conflicts
fail closed and request retry.

---

## Write classification (read this first)

Every write in this system is one of two kinds, and the **push timing** differs by machine.

**Where the write lands**

| Symbol | Meaning |
|--------|---------|
| 📝 **LOCAL** | Write to the machine's local disk only. Not yet in the repo. On Render this disk is **ephemeral** (lost on restart) until pushed. |
| ✅ **REMOTE** | The change is now in the GitHub repo (`main`). This is what other machines see. |
| ⬇️ **REPO READ** | Pull from the repo into local disk. |

**When the local write reaches the remote (push timing)**

| Machine | Mechanism | Push timing |
|---------|-----------|-------------|
| **Scheduler** (GitHub Actions) | signed git CLI `commit` + `push` | After each command, plus a per-subscriber reservation **before** each send and its outcome **after**. |
| **Admin** (`admin.py`) | git CLI `commit` + `push` | **Only if `--commit` is passed**, at the end of the command. Otherwise the write stays 📝 LOCAL and must be pushed **manually**. |
| **Webhook** (Render) | Git Data API via `RepoSync` | Atomic CSV publication **before HTTP 200**; concurrent branch advances reject stale snapshots and return 503. |

Push timings are per-command/per-contact for the scheduler, optional for admin, and
before acknowledgement for the webhook.

---

## 1. Image collection → admin selection → publication → delivery

```mermaid
sequenceDiagram
    participant Runner as GitHub Actions
    participant Repo as Git main
    participant Admin as Admin WhatsApp
    participant Web as Render webhook
    participant Pages as GitHub Pages
    participant User as Subscriber
    Runner->>Repo: Store candidate images and pending review batch
    Runner-->>Admin: Ops template invites reply ADMIN
    Note over Runner,User: No page regeneration or delivery while approval is missing
    Admin->>Web: ADMIN then Select daily image
    Web-->>Admin: Source list
    Admin->>Web: Select source
    Web-->>Admin: Image preview and Approve image button
    opt View another candidate
        Admin->>Web: Other sources then another source
        Web-->>Admin: New preview and new confirmation
    end
    Admin->>Web: Approve image
    Web->>Repo: Atomically commit approved source and publication request
    Repo-->>Runner: Publication request push starts page regeneration
    Runner->>Repo: Read fresh main and verify approved bytes
    Runner->>Repo: Commit canonical image, subscriber pages and approval stamp
    Runner->>Pages: Deploy matching artifact
    Pages-->>Runner: Deployment succeeded
    Runner->>Pages: Verify live approval stamp and subscriber page
    Runner->>Repo: Reserve daily contact slot
    Runner-->>User: Welcome or renewal or delivery
    Runner->>Repo: Persist accepted or failed or ambiguous result
```

A stale preview, changed image, previous-day selection or unauthorized sender cannot approve.
Pending runs release their runners. Page publication rebuilds from fresh main on bounded Git
conflicts. A repeat image run preserves an already-approved selection. Each send reservation
is committed before contacting Meta; ambiguous outcomes remain blocked for reconciliation.

---

## 2. GET /webhook — verification handshake (no writes)

```mermaid
sequenceDiagram
    autonumber
    participant Meta
    participant Web as Webhook (main.py)
    Meta->>Web: GET /webhook?hub.mode=subscribe&hub.verify_token=...&hub.challenge=...
    alt token matches WEBHOOK_VERIFY_TOKEN
        Web-->>Meta: 200 + hub.challenge (plain text)
    else mismatch
        Web-->>Meta: 403 forbidden
    end
    Note over Web: Read-only. No local or repo writes.
```

---

## 3. POST /webhook — inbound message (subscribe → plan → name → opt-in → pay)

This is the main event-driven flow. `RepoSync.pull()` runs at the start and
`RepoSync.push()` publishes an atomic CSV transaction before HTTP acknowledgement.

```mermaid
sequenceDiagram
    autonumber
    participant User as User (WhatsApp)
    participant Meta
    participant Web as Webhook (main.py)
    participant Svc as Services (subscriber/payment)
    participant Local as Local CSVs (Render disk)
    participant Repo as GitHub repo (main)

    User->>Meta: taps/sends message
    Meta->>Web: POST /webhook (signed)
    Web->>Web: verify HMAC signature
    Note over Web,Repo: First short lock protects the read and intent commit
    Web->>Repo: RepoSync.pull() — fetch latest CSVs  ⬇️ REPO READ
    Repo-->>Local: overwrite local subscribers/payments/processed/logs

    Web->>Web: dedupe on message id (processed.csv)

    alt CTA_SUBSCRIBE
        Web->>User: send plan list (PLAN_*)
    else PLAN_[plan] selected
        Web->>Svc: upsert_pending(mobile, plan, name)
        Svc->>Local: write subscribers.csv (PENDING)  📝 LOCAL
        alt name missing
            Web->>User: "What name should we greet you by?"
            User->>Meta: types name
            Meta->>Web: POST /webhook (name)
            Web->>Svc: set_name(mobile, name)
            Svc->>Local: write subscribers.csv  📝 LOCAL
        end
        Web->>User: consent disclosure + "I agree" button
    else CTA_OPTIN_AGREE
        Web->>Svc: grant_opt_in(mobile, "whatsapp_cta")
        Svc->>Local: write subscribers.csv (opt_in=true, ts)  📝 LOCAL
        Web->>Svc: create_payment(mobile, plan) + UPI intent
        Svc->>Local: write payments.csv (PENDING)  📝 LOCAL
        Web->>User: UPI intent + reference id, "reply with 12-digit UTR"
    end

    Note over Web,Repo: Replies above are queued locally during handling
    Web->>Repo: Commit state and PENDING reply reservations
    Note over Web,Meta: Release lock before provider network call
    Web->>Meta: Send detached durable reservations
    Meta-->>Web: accepted, failed or ambiguous
    Note over Web,Repo: Reacquire short lock and refresh main
    Web->>Repo: Merge matching outcome fields and commit
    alt durable commits and replies succeeded
        Web-->>Meta: 200 accepted
    else persistence or reply failure
        Web-->>Meta: 503 retry
    end
```

Failed conversational replies restore pre-message state. STOP and confirmed UTR are retained with a
durable acknowledgement retry record instead. Callbacks are stored even before their send row
exists; delivered/read evidence wins over delayed failures. Uncertain sends hold the daily slot.

### 3a. Navigation, backtracking and stale CTA cases

The conversation is intentionally recoverable. `BACK`, `GO BACK`, `PREVIOUS`, `MENU`,
`Radhe Radhe`, `RENEW` and `SUBSCRIBE` are navigation commands at every step. They never become
the subscriber's name, create a payment, change consent, or consume a daily delivery slot.

```mermaid
sequenceDiagram
    autonumber
    participant User as User (WhatsApp)
    participant Web as Webhook
    participant State as Subscriber/payment state

    User->>Web: sends MENU / Radhe Radhe
    Web->>State: clear awaiting-name flag only
    Web-->>User: Status-aware menu with subscription and payment actions
    Note over Web,User: New users View plans and expired users View renewal plans
    Note over Web,User: Beyond three days active users Upgrade to larger plans only
    Note over Web,User: At zero to three days remaining Renew offers the same or larger plans
    Note over Web,User: Largest plan has no upgrade but can renew within the window
    Note over Web,User: Subscription status includes the current plan type
    Note over Web,User: Active opted-out users Resume messages without payment

    User->>Web: taps CTA_SUBSCRIBE
    Web-->>User: plan list (PLAN_<plan>)
    User->>Web: taps PLAN_<plan>
    Web->>State: save checkout plan, preserve any paid entitlement until approval
    alt name missing
        Web-->>User: ask for greeting name
        User->>Web: sends BACK
        Web->>State: clear awaiting-name flag, retain no payment
        Web-->>User: main menu
    else name available
        Web-->>User: consent disclosure (I agree / No thanks)
    end

    User->>Web: taps an old/unknown PLAN or CTA id
    Web-->>User: current main menu or plan list
    Note over Web,State: stale IDs are ignored, they cannot apply a new paid entitlement

    User->>Web: taps CTA_OPTIN_AGREE twice
    Web->>State: first tap grants consent and creates one current PENDING payment
    Web-->>User: UPI instruction + reference
    Web-->>User: duplicate message ID is ignored, fresh tap reuses current payment
```

### 3b. Payment and UTR recovery cases

```mermaid
sequenceDiagram
    autonumber
    participant User as User (WhatsApp)
    participant Web as Webhook
    participant State as Payment state

    User->>Web: sends Hi or MENU while awaiting UTR
    Web->>State: read current checkout without resetting it
    Web-->>User: Payment instructions or Payment status
    User->>Web: sends a 12-digit UTR
    alt No superseded checkout exists
        Web->>State: save conversation draft for current payment
    else Checkout was changed
        Web-->>User: request original reference and 12-digit UTR
        User->>Web: UTR DD2609130001 123456789012
        Web->>State: validate ownership and save conversation draft
    end
    Web-->>User: Confirm UTR or Change UTR
    opt Customer changes the number
        User->>Web: Change UTR then corrected reference and UTR
        Web->>State: replace draft and invalidate old confirmation
        Web-->>User: Confirm corrected UTR or Change UTR
    end
    User->>Web: Confirm UTR
    Web->>State: recheck sender and payment then record confirmed UTR
    Web-->>User: latest UTR and reference recorded, review within 24 hours, do not pay again
    alt acknowledgement fails
        Web->>State: retain UTR + store acknowledgement in reply_retries.csv
        Web-->>User: HTTP 503, Meta may redeliver
        Web->>State: retry acknowledgement only, do not record UTR twice
    else no PENDING payment
        Web-->>User: main menu, no payment is changed
    end

    User->>Web: sends BACK / RENEW / SUBSCRIBE after UTR
    Web-->>User: navigation menu
    Note over State: existing UTR remains attached until admin accepts or rejects it
    User->>Web: taps stale plan or sends a different UTR during review
    Web-->>User: a reference-qualified correction needs confirmation
    Note over Web,State: existing confirmed UTR remains until correction is confirmed
    alt Admin rejects payment
        State-->>Web: FAILED payment
        Web-->>User: Rejection notice, Payment status and Request review
        User->>Web: Request review
        Web->>State: log PAYMENT_REVIEW_REQUESTED without approving payment
    else Approved but activation incomplete
        State-->>Web: SUCCESS with activation pending
        Web-->>User: Approved, activation being completed
    else Activated but publication unconfirmed
        State-->>Web: APPLIED payment, publication not verified
        Web-->>User: Approved, page being prepared
    end
```

Navigation during name/consent shows the missing prompt without saving the greeting as a
name. Active opted-out users retain Resume messages even during payment review; its consent
action leaves checkout unchanged. Admin `reopen-payment --no-payment-confirmed` resolves
rejection only when no payment occurred. A real payment must be verified against its original
reference instead. Daily publication checks additionally inspect the displayed image date;
welcome/renewal publication checks remain independent. Obsolete activation welcomes are cancelled.

### 3c. Menu options by entitlement and expiry

This diagram uses short participant names and one action per line so GitHub Mermaid renders it
consistently. The menu is derived from persisted state each time; unexpected text or stale buttons
return the user to a safe step without changing payment or entitlement.

```mermaid
sequenceDiagram
    autonumber
    participant User
    participant Webhook
    participant State

    User->>Webhook: sends Hi or MENU
    Webhook->>State: read subscriber and checkout
    alt new user
        Webhook-->>User: View plans
        User->>Webhook: selects View plans
        Webhook-->>User: Starter Weekly Monthly Yearly
        User->>Webhook: sends unexpected question
        Webhook-->>User: menu again no payment created
    else active starter or weekly beyond three days
        Webhook-->>User: Subscription status and Upgrade
        User->>Webhook: selects Upgrade
        Webhook-->>User: only larger plans
        User->>Webhook: sends RENEW or PAYMENT
        Webhook-->>User: same current menu or checkout status
    else active monthly within three days
        Webhook-->>User: Subscription status and Renew
        User->>Webhook: selects Renew
        Webhook-->>User: current Monthly and larger plans
        User->>Webhook: sends invalid UTR
        Webhook-->>User: request UTR reference and twelve digits
        User->>Webhook: retries valid UTR
        Webhook->>State: save conversation draft only
        Webhook-->>User: Confirm UTR or Change UTR
        User->>Webhook: Confirm UTR
        Webhook->>State: attach UTR to pending payment
    else active yearly within three days
        Webhook-->>User: Subscription status and Renew
        User->>Webhook: selects Renew
        Webhook-->>User: Yearly only renew your current plan
        User->>Webhook: taps old plan navigation CTA
        Webhook-->>User: eligible Yearly renewal only, no entitlement change
    else active yearly beyond three days
        Webhook-->>User: Subscription status only
        User->>Webhook: sends RENEW
        Webhook-->>User: subscription status only
    else expired subscriber
        Webhook-->>User: Subscription status and Renew
        User->>Webhook: selects Renew
        Webhook-->>User: all configured plans
        User->>Webhook: sends PAYMENT
        Webhook-->>User: payment instructions or payment status
    end
```

---

## 4. User makes payment (submits UTR)

```mermaid
sequenceDiagram
    autonumber
    participant User as User (WhatsApp)
    participant Meta
    participant Web as Webhook (main.py)
    participant Pay as PaymentService
    participant Local as Local CSVs
    participant Repo as GitHub repo (main)

    User->>Meta: pays via UPI, replies with 12-digit UTR
    Meta->>Web: POST /webhook - text = UTR
    Web->>Repo: RepoSync.pull  ⬇️ REPO READ
    Web->>Local: save recoverable conversation draft only
    Web->>Repo: persist draft and confirmation reply
    Web-->>User: Confirm UTR or Change UTR
    User->>Meta: taps Confirm UTR for latest draft
    Meta->>Web: POST interactive confirmation
    Web->>Repo: refresh state and validate sender and payment
    Web->>Pay: record_utr(reference_id, utr)
    Pay->>Local: write payments.csv (UTR attached, still PENDING)  📝 LOCAL
    Web->>User: Received your UTR. Activates once an admin verifies.
    Web->>Repo: Persist UTR and any failed acknowledgement atomically
    Web-->>Meta: 200 if persisted and reply succeeded, otherwise 503
    Note over Web,Repo: Payment is NOT yet SUCCESS. A UTR is not proof of payment.
```

---

## 5. Admin payment verification on WhatsApp

```mermaid
sequenceDiagram
    participant Customer
    participant Admin as Admin 919535507255
    participant Web as Business 916361699109
    participant Git as Git main
    participant Jobs as Publication pipeline
    Customer->>Web: Confirm UTR
    Web->>Git: Save UTR and confirmation timestamp
    Note over Admin,Git: Payment alert invites ADMIN to review confirmed payments
    Admin->>Web: ADMIN then Review payments
    Web-->>Admin: Confirmed payment list
    Admin->>Web: Select reference
    Web-->>Admin: Customer, plan, amount, UTR and Approve or Reject
    Admin->>Admin: Match UTR and amount against bank records
    alt Approve current snapshot
        Admin->>Web: Approve payment
        Web->>Web: Recheck admin identity and unchanged payment snapshot
        Web->>Git: Atomically apply entitlement, welcome and publication request
        Note over Web,Git: Start is customer confirmation day in IST
        Note over Web,Git: End is max of old expiry and confirmation day plus purchased days
        Git-->>Jobs: Regenerate only after today's image approval
        Jobs->>Jobs: Deploy before welcome or daily delivery
    else Reject
        Admin->>Web: Reject payment
        Web->>Git: Record FAILED without adding entitlement
    end
```

CLI verification remains available. With image approval enabled, it queues publication rather
than rendering locally. Use `--commit` to publish the CSV transaction; if its Git credentials do
not trigger push workflows, manually run Regenerate Daily Pages. Records without a historical
confirmation timestamp use the approval date. Applied payment references prevent double credit.
Approval is a human check against bank evidence, not automated financial verification.

---

## 6. Renewal reminder + opt-out (STOP)

Renewal eligibility and the page CTA open three IST calendar days before expiry, including
expiry day; reminder scheduling does not change that window. Payment and old CTA recovery
recheck eligibility. Obsolete unpaid checkouts become SUPERSEDED, but recorded UTRs and
reference-qualified UTR submissions remain reviewable without requesting another payment.

```mermaid
sequenceDiagram
    autonumber
    participant Sched as scheduler.py (after successful Pages deployment)
    participant WA as WhatsApp (Meta)
    participant User
    participant Web as Webhook (main.py)
    participant Sub as SubscriberService
    participant Local as Local CSVs
    participant Repo as GitHub repo (main)

    Note over Sched,Repo: Renewal reminder (event-driven delivery step)
    Sched->>Sub: find active opted-in subs expiring in reminder_days [3,2,1]
    Sched->>Sub: skip when date+mobile is SENT, DELIVERED, PENDING or UNKNOWN
    Sched->>Repo: persist PENDING reservation before sending
    Sched->>WA: send renewal reminder
    WA->>User: Delivery-status template with personalized Daily Darshan link
    Sched->>Local: append renewals.csv + successful daily sentlog row  📝 LOCAL
    Sched->>Repo: git commit + push outcome per subscriber

    Note over User,Repo: Opt-out (event-driven, any time)
    User->>WA: replies STOP / UNSUBSCRIBE / CANCEL
    WA->>Web: POST /webhook
    Web->>Repo: RepoSync.pull  ⬇️ REPO READ
    Web->>Sub: revoke_opt_in(mobile) - opt_in=false, ts, source=opt_out
    Sub->>Local: write subscribers.csv  📝 LOCAL
    Web->>User: Opted out, paid dates unchanged. Active users Resume messages, expired users Renew.
    Web->>Repo: Persist opt-out and any failed acknowledgement atomically
    Web-->>WA: 200 if persisted and reply succeeded, otherwise 503
    Note over Sub: opt_in=false makes the subscriber non-deliverable immediately.
```

## 7. Welcome versus daily delivery

```mermaid
sequenceDiagram
    autonumber
    participant Admin
    participant WA as WhatsApp (Meta)
    participant User
    participant Sched as scheduler.py
    participant Repo as sentlog.csv

    Admin->>Repo: commit ACTIVE subscriber and applied payment reference
    Sched->>Repo: create missing payment-keyed welcomes.csv task if needed
    Note over Sched,Repo: Deploy page, then check publication and consent
    Sched->>Repo: persist publication_verified after published page check
    alt shared date and mobile slot is free
        Sched->>Repo: reserve welcome slot as PENDING
        Sched->>WA: daily_darshan_delivery_update(name, subscription_id)
        WA->>User: Subscription or delivery status update
        Sched->>Repo: update welcomes and sentlog outcome
        Note over User,Repo: SENT, PENDING or UNKNOWN blocks renewal and delivery today
    else slot was already used today
        Note over User,Repo: Keep welcome QUEUED for the next run or day
    end

    alt welcome failed definitively
        Sched->>Repo: mark welcome and shared slot FAILED
        Note over User,Repo: Renewal or delivery may use the released slot
    else welcome was accepted or uncertain
        Note over User,Repo: At most one welcome, renewal or delivery contact per day
    end
```

The welcome event proves activation; it is not evidence that a daily image was delivered.
The delivery event proves only that Meta accepted the daily template (`SENT`) unless a later
`delivered` or `read` status is received. A provider timeout is `UNKNOWN` and blocks automatic
reruns until reconciled, preventing duplicate customer messages.

---

## When does web-page rendering happen?

With production image approval enabled, Regenerate Daily Pages is the rendering entry point.
Admin image approval and payment activation create idempotent publication requests. If today's
image is not yet approved, the request's workflow skips and the eventual image approval starts a
fresh run covering all active subscribers. Manual page regeneration enforces the same gate.

The job checks the selected image hash, renders subscriber pages and an approval stamp, then
deploys. Customer message jobs verify that stamp is live and check individual subscriber pages.
CLI activation does not write pages in this mode. Existing public pages remain available while
approval is pending. The old automatic image/render path is used only if approval is disabled.

---

## Timing summary

```mermaid
sequenceDiagram
    participant User
    participant Web as Render
    participant Repo as Durable state and outbox
    participant Worker as Retry workflow
    participant Meta
    User->>Web: Select plan
    Web->>Repo: Commit payment reference and versioned reply
    Web->>Meta: Send reserved reply
    Meta-->>Web: Definitive failure
    Web->>Repo: Save FAILED and next attempt time
    Worker->>Web: Signed periodic retry request
    Web->>Repo: Load current state under lock
    alt Reply is current and retry is due
        Web->>Repo: Persist PENDING attempt
        Web->>Meta: Retry same instructions
    else State changed or reply window expired
        Web->>Repo: Cancel obsolete reply
    end
    User->>Web: MENU then Payment instructions or Payment status
    Web->>Repo: Read existing payment and advance conversation version
    Web->>Repo: Commit fresh reply using existing reference
    Web->>Meta: Send current instructions
```

- **Reply recovery** has a best-effort five-minute GitHub Actions schedule and manual dispatch.
  Unknown attempts require reconciliation; a user-requested resend may duplicate an earlier
  message already accepted by Meta, but never repeats the payment/activation mutation.

- **Image collection and payment alerts are manual/external entry points.** Image approval
  starts regeneration, then successful deployment triggers delivery.
- **All webhook operations are event-driven** (no fixed time): verification, subscribe, plan,
  name, opt-in, UTR, opt-out. They publish the state transaction before HTTP acknowledgement.
  Failures request retries with 503. Manual/delayed workflows use repository conflict detection;
  no fixed time window makes the webhook unavailable.
- **Admin verification is a human decision.** WhatsApp approval commits automatically through
  the webhook transaction; CLI approval still needs `--commit`.
