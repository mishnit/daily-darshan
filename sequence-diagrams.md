# Daily Darshan webhook sequence diagrams

These diagrams describe production with `WEBHOOK_BEST_EFFORT_QUEUE=true`. They distinguish HTTP
acknowledgement, business processing, WhatsApp transport, Git persistence, and delivery receipts.
Dates are evaluated in IST unless stated otherwise.

## Shared ingress, processing, reply, and persistence

A `200 queued` response means only that the bounded in-memory queue accepted the event. It does not
mean the transition ran, Meta accepted a reply, the customer received it, or Git was updated.

```mermaid
sequenceDiagram
    participant M as Meta
    participant W as Webhook
    participant Q as Ingress queue
    participant A as State writer
    participant C as CSV state
    participant S as Sender pool
    participant G as GitHub main
    M->>W: Signed webhook event
    W->>W: Verify signature and parse JSON
    alt Signature is invalid
        W-->>M: HTTP 403
    else Event is accepted by queue
        W->>Q: Enqueue event
        W-->>M: HTTP 200 queued
    else Event cannot be queued
        W-->>M: HTTP 200 dropped
        Note over W,M: Best effort mode prevents provider retry
    end
    Q->>A: Drain an ordered batch
    A->>C: Dedupe message and update conversation
    A->>C: Apply transition and reserve reply
    opt Event requires immediate durability
        A->>G: Merge state and push immediately
        Note over A,G: Retry a branch advance up to three times
    end
    A->>S: Submit immutable reply
    S->>M: Send WhatsApp response
    M-->>S: Return transport result
    S-->>A: Queue reply outcome
    A->>C: Merge outcome and delivery receipt
    loop Periodic snapshot
        A->>G: Merge state and push every 15 minutes
    end
```

| Layer | Intermediate states |
|---|---|
| Ingress | accepted in queue, or dropped but acknowledged |
| Message | unseen → recorded in `processed.csv` → duplicate ignored |
| Conversation | version incremented → handler fields updated |
| Reply | reserved → prepared → `SENT`, `FAILED`, `UNKNOWN`, or `CANCELLED` |
| Receipt | sent → delivered → read with monotonic reconciliation |
| Ordinary persistence | memory dirty → periodic semantic merge → Git commit |
| Critical persistence | transition → immediate semantic merge and Git push → reply send |

The actor alone mutates state. Sender threads only perform network transport and return immutable
outcomes. Ordinary replies have no automatic retry in best-effort mode.

### Git persistence classification

| Inbound case | Lane | Git behavior |
|---|---|---|
| Any `ADMIN` text | Critical | Refresh state and push immediately, including an unauthorized denial reply |
| Any `ADM_*` admin button, including list, preview, approve, and reject | Critical | Merge and push immediately, including an unauthorized denial reply |
| `UTR_CONFIRM_*` or `UTR_EDIT_*` | Critical | Merge and push the confirmed or cleared draft immediately |
| `CTA_PAYMENT_REVIEW` for a rejected payment | Critical | Push `PAYMENT_REVIEW_REQUESTED` immediately before acknowledgement |
| Greeting, menu, status, plan selection, name, opt-in, payment creation, or UTR draft | Ordinary | Keep in memory and include in the normally 15-minute snapshot |
| STOP, resume consent, referral capture, provider status callback, and reply outcome | Ordinary | Keep in memory and include in the normally 15-minute snapshot |

An immediate critical snapshot serializes all currently dirty repositories, so earlier ordinary
changes already in memory may be included in that same commit. Remote payment state is coalesced at
most once per configured refresh interval for ordinary batches, but is refreshed for every critical
command. This payment refresh is distinct from the normal 15-minute full Git export.

## A. New user says Radhe Radhe and submits or revises a UTR

```mermaid
sequenceDiagram
    autonumber
    participant User
    participant Webhook
    participant State as Subscriber and payment state
    participant WA
    participant Git
    User->>Webhook: Radhe Radhe
    Webhook->>State: No subscriber and no checkout
    Webhook->>WA: Menu with View plans
    User->>Webhook: CTA_SUBSCRIBE
    Webhook->>WA: Eligible plans
    User->>Webhook: PLAN selected
    Webhook->>State: Pending subscriber without entitlement
    alt name missing
        Webhook->>State: awaiting_name true
        Webhook->>WA: Ask greeting name
        User->>Webhook: Name text
        Webhook->>State: Save name and awaiting_name false
    end
    Webhook->>WA: Opt-in disclosure
    User->>Webhook: CTA_OPTIN_AGREE
    Webhook->>State: opt_in true with source and timestamp
    Webhook->>State: Create PENDING payment and DD reference
    Webhook->>WA: UPI instruction reference and UTR format
    User->>Webhook: UTR reference plus 12 digits
    Webhook->>State: Save draft reference and one-time token
    Webhook->>WA: Confirm UTR or Change UTR
    alt Change UTR
        User->>Webhook: UTR_EDIT token
        Webhook->>State: Clear draft reference and token
        Webhook->>Git: Immediate critical push
        Webhook->>WA: Ask for corrected UTR
        User->>Webhook: Corrected reference and UTR
        Webhook->>State: Save new draft and token
        Webhook->>WA: Confirm corrected UTR
    end
    User->>Webhook: UTR_CONFIRM current token
    Webhook->>State: Revalidate sender reference and competing review
    Webhook->>State: Copy draft to payment and set confirmation time
    Webhook->>State: Supersede other PENDING checkouts
    Webhook->>State: Clear draft and token
    Webhook->>Git: Immediate critical push
    Webhook->>WA: Awaiting admin verification
```

The subscriber remains non-entitled until admin approval. Typed UTR data is only a conversation
draft. The current sender-bound confirmation token moves it into `payments.csv`; stale tokens cannot.

## B. Existing user checks subscription and plans

```mermaid
sequenceDiagram
    autonumber
    participant User
    participant Webhook
    participant State
    participant WA
    User->>Webhook: Radhe Radhe MENU or STATUS
    Webhook->>State: Read entitlement opt-in and checkout
    alt STATUS or CTA_STATUS
        Webhook->>WA: Status plan expiry consent and payment status
    else active with more than three days remaining
        Webhook->>WA: Status and Upgrade when larger plan exists
        User->>Webhook: Upgrade
        Webhook->>WA: Strictly larger plans only
    else active with zero to three days remaining
        Webhook->>WA: Status and Renew
        User->>Webhook: Renew
        Webhook->>WA: Current plan plus larger plans
    else largest plan outside renewal window
        Webhook->>WA: Status only
    else expired subscriber
        Webhook->>WA: Expired status and Renew
        User->>Webhook: Renew
        Webhook->>WA: All configured plans
    else active but opted out
        Webhook->>WA: Resume messages without payment
    end
```

Plan rank is duration then amount. Checkout never changes paid entitlement before approval.
Same-plan renewal opens only in the last three calendar days, including expiry day.

## C. Existing user requests payment instructions and revises a UTR

The normal menu body remains generic in every payment state. Payment references, UTRs, amounts,
and administrator decisions are returned only after the customer selects Payment instructions or
Payment status. Review past UTR identifies only the relevant reference in its list row.

```mermaid
sequenceDiagram
    autonumber
    participant User
    participant Webhook
    participant State
    participant WA
    User->>Webhook: MENU
    Webhook->>WA: Generic menu body and payment list option
    User->>Webhook: PAYMENT option
    Webhook->>State: Resolve checkout
    alt PENDING without UTR
        Webhook->>WA: Existing payment instructions
    else conversation draft exists
        Webhook->>WA: Resume Confirm or Change UTR
    else confirmed UTR exists
        Webhook->>WA: Awaiting review and do not pay again
    else FAILED
        Webhook->>WA: Rejected, payment and UTR changes blocked until release date
    else unresolved SUPERSEDED with UTR and no prior admin decision
        Webhook->>WA: Offer Review past UTR alongside eligible checkout actions
    else SUPERSEDED after rejection cleanup
        Webhook->>WA: Prior decision is final and offer only eligible new checkout actions
    else SUCCESS not applied
        Webhook->>WA: Approved and activation in progress
    else SUCCESS and applied
        Webhook->>WA: Approved and publication pending
    end
    User->>Webhook: New UTR text
    alt checkout history unambiguous
        Webhook->>State: Draft against current payment
    else plan changed and older reference may be paid
        Webhook->>WA: Require reference-qualified UTR
        User->>Webhook: UTR DD reference plus 12 digits
        Webhook->>State: Validate ownership and draft correction
    end
    Webhook->>WA: Confirm or change
    User->>Webhook: Confirm current token
    Webhook->>State: Persist latest UTR and confirmation time
```

Confirmed payment evidence survives navigation and plan changes. A second payment confirmation is
blocked while another PENDING payment for that mobile is already under review.

## D. Active user upgrades and confirms payment

```mermaid
sequenceDiagram
    autonumber
    participant User
    participant Webhook
    participant Payments
    participant Subscriber
    participant Admin
    User->>Webhook: Upgrade
    Webhook->>Subscriber: Read effective active plan
    Webhook-->>User: Strictly larger plans
    User->>Webhook: Select larger PLAN
    Webhook->>Payments: Supersede old PENDING and create new PENDING
    Note over Subscriber: Current entitlement stays unchanged
    Webhook-->>User: UPI instructions
    User->>Webhook: Submit then confirm UTR
    Webhook->>Payments: Store confirmed UTR
    Admin->>Payments: Verify bank evidence and approve
    Payments->>Payments: SUCCESS and activation APPLIED
    Payments->>Subscriber: Add purchased days and applied reference
    Payments->>Subscriber: Keep larger approved plan label
```

Approval extends an existing entitlement and prevents double credit with `applied_payment_refs` and
the payment `activation_state`.

## E. Expiring user renews and confirms payment

```mermaid
sequenceDiagram
    autonumber
    participant User
    participant Webhook
    participant Payments
    participant Subscriber
    participant Admin
    User->>Webhook: Renew in last three days
    Webhook->>Subscriber: Confirm zero through three days remaining
    Webhook-->>User: Same and larger plans
    User->>Webhook: Select PLAN
    Webhook->>Payments: Create PENDING renewal
    Note over Subscriber: Existing dates stay unchanged before approval
    Webhook-->>User: UPI instructions
    User->>Webhook: Submit and confirm UTR
    Webhook->>Payments: Store confirmed UTR
    Admin->>Payments: Approve after bank check
    Payments->>Subscriber: Extend from current expiry or effective date
    Payments->>Subscriber: Preserve largest plan and add applied reference
```

Outside the window, the same plan is ineligible but larger upgrades remain available. Expired users
may choose any configured plan.

## F. Customer requests another review after rejection

```mermaid
sequenceDiagram
    autonumber
    participant User
    participant Webhook
    participant Logs
    participant Payments
    participant AdminWA as WhatsApp admin queue
    participant AdminCLI as Admin CLI
    participant Cleanup as Delivery cleanup job
    Payments-->>Webhook: FAILED payment
    Webhook-->>User: Proactive rejection notice with blocked period and release date
    Note over User,Payments: FAILED blocks new checkout and UTR revision for three full days
    Cleanup->>Payments: FAILED to SUPERSEDED after three full calendar days
    Note over User,Payments: Rejection timestamp remains, this reference cannot be reviewed or revised again
    Payments-->>User: New eligible checkout is unblocked
    AdminWA->>Payments: List only PENDING rows with UTR
    Note over AdminWA,Payments: FAILED and SUPERSEDED rows are absent
    alt bank evidence shows payment
        AdminCLI->>Payments: Verify and activate original reference
    else administrator confirms no payment
        AdminCLI->>Payments: Reopen with no-payment-confirmed
        Payments->>Payments: FAILED to SUPERSEDED
    end
```

An unresolved `SUPERSEDED` UTR that has never been accepted or rejected follows a separate recovery
edge: customer Review past UTR changes it to `PENDING`, records `PAYMENT_REVIEW_REQUESTED`, and makes
it visible in the admin queue. After an admin decision, that reference can never use this edge again.

### Payment CSV state transitions and customer permissions

The diagram uses only values that can appear in `payments.csv.status`. UTR presence,
`rejected_at`, and `activation_state` are conditions on a row, not additional payment states.

```mermaid
stateDiagram-v2
    [*] --> PENDING: Checkout created
    PENDING --> PENDING: Confirm or revise UTR before admin decision
    PENDING --> SUPERSEDED: Checkout replaced before admin decision
    SUPERSEDED --> PENDING: Review or revise under three days when UTR exists and rejected_at is empty
    PENDING --> SUCCESS: Admin approves
    PENDING --> FAILED: Admin rejects
    PENDING --> FAILED: Another same plan UTR is approved
    FAILED --> SUPERSEDED: Cleanup after three calendar days retains rejected_at

    note right of PENDING
        View payment information
        Revise UTR before admin decision
        UTR present means admin review is already pending
        New payment is blocked while a UTR is under review
    end note

    note right of SUCCESS
        View approval while activation or publication is pending
        UTR revision is not allowed
        Review request is not allowed
        New payment follows upgrade and renewal eligibility
    end note

    note right of FAILED
        View rejection and release date
        UTR revision is not allowed
        Review request is not allowed
        New payment is blocked for three calendar days
    end note

    note right of SUPERSEDED
        If rejected_at is empty the unresolved UTR may be revised or reviewed for three days
        If rejected_at exists the old reference is final and hidden from review
        A new payment is allowed subject to plan eligibility
    end note
```

| `payments.csv.status` and row condition | View transaction/payment information | Revise UTR | Request admin review | Create another payment |
|---|---|---|---|---|
| `PENDING`, UTR empty | Yes, payment instructions | Yes | No, nothing submitted yet | Yes; replacing it makes the old row `SUPERSEDED` |
| `PENDING`, UTR present | Yes, verification pending | Yes, until an admin decision | Already in review automatically | No |
| `SUCCESS` | Yes while activation/publication is pending; afterward subscription status is shown | No | No | Only when upgrade or renewal rules permit |
| `FAILED` | Yes, including rejection and release date | No | No | No for three calendar days |
| `SUPERSEDED`, UTR present, `rejected_at` empty, under three days old | Reference is exposed through Review past UTR, not the normal payment-status card | Yes, using the reference | Yes; changes status to `PENDING` | Yes, subject to plan eligibility |
| `SUPERSEDED`, UTR present, `rejected_at` empty, at least three days old | No ordinary transaction action; retained for audit | No | No | Yes, subject to plan eligibility |
| `SUPERSEDED`, `rejected_at` present | No ordinary transaction action; retained for audit | No | No | Yes, subject to plan eligibility |
| `SUPERSEDED`, UTR empty | No ordinary transaction action | No | No | Yes, subject to plan eligibility |

The WhatsApp admin Review payments option contains only `PENDING` rows with a non-empty UTR.
`FAILED`, `SUPERSEDED`, and `SUCCESS` rows never appear in that queue.

## G. Admin selects the canonical image

```mermaid
sequenceDiagram
    autonumber
    participant ImageJob as Daily Image
    participant Reviews as image_reviews.csv
    participant Admin
    participant Webhook
    participant Requests as pipeline_requests.csv
    participant Pages
    participant Deploy
    participant Delivery
    ImageJob->>Reviews: Store candidates as PENDING with hash
    Admin->>Webhook: ADMIN then Select daily image
    Webhook-->>Admin: Today's PENDING sources
    Admin->>Webhook: Select source
    Webhook->>Reviews: Store fingerprint candidate and token
    Webhook-->>Admin: Preview and Approve image
    Admin->>Webhook: Approve current token
    Webhook->>Reviews: Revalidate date status and fingerprint
    Webhook->>Reviews: Supersede all candidates for date
    Webhook->>Reviews: Mark chosen candidate APPROVED
    Webhook->>Requests: Add idempotent image-generation request
    Webhook->>Requests: Immediate critical Git push
    Requests->>Pages: Push starts Regenerate Daily Pages
    Pages->>Pages: Materialize approved bytes as canonical image
    Pages->>Pages: Render pages and approval stamp
    Pages->>Requests: Consume request and commit output
    Pages->>Deploy: Start Pages deployment
    Deploy->>Delivery: Successful default-branch deploy starts delivery
    Delivery->>Delivery: Verify live stamp and pages
    Delivery->>Delivery: Welcome then renewal then daily delivery
```

One `APPROVED` image is allowed per date, and its hash must match canonical bytes. Customer sends
wait for a live approval stamp. Welcome, renewal, and delivery share one date-plus-mobile slot.

## H. Admin approves payment and triggers welcome

```mermaid
sequenceDiagram
    autonumber
    participant Customer
    participant Admin
    participant Webhook
    participant Payments
    participant Subscribers
    participant Welcomes
    participant Requests as pipeline_requests.csv
    participant Pages
    participant Delivery
    participant WA
    Customer->>Webhook: Confirm UTR
    Webhook->>Payments: Confirmed PENDING or SUPERSEDED
    Admin->>Webhook: ADMIN then Review payments
    Webhook-->>Admin: Confirmed UTR queue
    Admin->>Webhook: Select reference
    Webhook->>Webhook: Store fingerprint and admin token
    Webhook-->>Admin: Customer plan amount UTR and actions
    Admin->>Admin: Match bank UTR and amount
    alt approve unchanged snapshot
        Admin->>Webhook: Approve
        Webhook->>Payments: SUCCESS
        Webhook->>Subscribers: Activate renew or upgrade once
        Webhook->>Payments: activation_state APPLIED
        Webhook->>Payments: Fail other submitted UTRs for the same customer and plan
        Note over Payments: Same-plan FAILED rows block changes for three full days, then cleanup supersedes them
        Webhook->>Payments: Supersede unpaid or different-plan competing checkouts
        Webhook-->>Customer: Immediate approval notice with plan and updated expiry
        Webhook->>Welcomes: Reference-keyed QUEUED welcome
        Webhook->>Requests: Payment publication request
        Webhook->>Requests: Immediate critical Git push
        Requests->>Pages: Regenerate after image approval
        Pages->>Delivery: Deployment success starts delivery
        Delivery->>Delivery: Verify live page and daily slot
        Delivery->>WA: Subscription status welcome template
        Delivery->>Welcomes: Record outcome and timestamp
    else reject unchanged snapshot
        Admin->>Webhook: Reject
        Webhook->>Payments: FAILED without entitlement
        Webhook->>Payments: Immediate critical Git push
    else row changed after preview
        Webhook-->>Admin: Reject stale decision and request fresh review
    end
```

Approval is idempotent. Welcome is queued with entitlement but sent only after publication. If the
image is not approved, publication waits until a later image approval starts regeneration.

## I. Other workflows and exceptional paths

### Opt out and resume

```mermaid
sequenceDiagram
    participant User
    participant Webhook
    participant Subscriber
    participant WA
    User->>Webhook: STOP UNSUBSCRIBE or CANCEL
    Webhook->>Subscriber: opt_in false with timestamp
    Note over Subscriber: Paid dates and payment review stay unchanged
    Webhook->>WA: Confirm messages stopped
    User->>Webhook: Radhe Radhe then Resume messages
    Webhook->>WA: Ask for fresh consent
    User->>Webhook: I agree
    Webhook->>Subscriber: opt_in true with timestamp
    Webhook->>WA: Confirm enabled without changing dates
```

### Receipts, duplicates, malformed, and unsupported events

```mermaid
sequenceDiagram
    participant Meta
    participant Webhook
    participant Dedupe as processed.csv
    participant Status as Message ledgers
    alt duplicate inbound id
        Meta->>Webhook: Redelivered message
        Webhook->>Dedupe: Already recorded
        Webhook-->>Meta: 200 without repeated transition
    else status callback
        Meta->>Webhook: sent delivered read or failed
        Webhook->>Status: Record by WhatsApp message id
        Webhook->>Status: Reconcile all message ledgers
        Note over Status: Monotonic state prevents late regression
    else malformed JSON after valid signature
        Meta->>Webhook: Invalid body
        Webhook-->>Meta: 200 ignored
    else unsupported message type
        Meta->>Webhook: Unsupported payload
        Webhook-->>Meta: 200 queued without business action
    else media without a UTR caption
        Meta->>Webhook: Payment screenshot
        Webhook-->>Meta: 200 queued
        Webhook->>Meta: Ask for reference and UTR as text
    end
```

`MENU`, `RADHE RADHE`, `PAYMENT`, `RENEW`, `SUBSCRIBE`, `BACK`, `CONTINUE`, and `RESEND`
navigate without approving payment. Unknown buttons return a safe current menu. Recovery is
rate-limited for 30 seconds. Admin and UTR actions validate tokens, ownership, status, and row
fingerprints.

## State ownership summary

| State | Primary key | Main transitions | Persistence timing |
|---|---|---|---|
| Subscriber | mobile | pending → active, active → expired, opt-in true or false | periodic or immediate admin approval |
| Payment | reference | PENDING without decision ↔ unresolved SUPERSEDED; PENDING review → SUCCESS or FAILED; FAILED → final SUPERSEDED after cleanup | UTR, re-review promotion and admin decisions immediate |
| UTR draft | conversation mobile | empty → drafted → edited or confirmed → empty | draft periodic, edit and confirm immediate |
| Welcome | payment reference | QUEUED → PENDING → SENT → DELIVERED or FAILED | delivery workflow commits |
| Renewal | mobile type expiry | eligible → PENDING → SENT → DELIVERED or FAILED | delivery workflow commits |
| Daily contact | date and mobile | free → PENDING → SENT → DELIVERED or FAILED | delivery workflow commits |
| Image review | candidate id | PENDING → APPROVED or SUPERSEDED | image approval immediate |
| Pipeline request | image generation or payment | absent → queued → consumed | approval then Pages workflow |
| Webhook reply | reply id | reserved → prepared → SENT or terminal outcome | periodic unless critical |

## Operational boundaries

- GET webhook verification is read-only and returns a challenge only for the configured token.
- HTTP success in best-effort mode is not a durability promise. Overflow, process restart before a
  snapshot, initialization failure, or critical Git failure can lose an acknowledged event.
- Ordinary replies are not automatically retried. `/internal/retry-replies` is disabled in this mode.
- Shared business CSVs use semantic row merges. The actor is the in-process writer and Git branch
  compare-and-swap protects cross-process publication.
- Provider `SENT` means Meta accepted the message. Only later `DELIVERED` or `READ` proves further
  progression.
