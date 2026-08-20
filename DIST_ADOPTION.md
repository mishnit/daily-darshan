# Daily Darshan — Distribution & Adoption Strategy

A plan to maximize **reach** (top of funnel) and **conversion** (paid subscription), tuned
to the product's realities: WhatsApp-native delivery, UPI-intent payments with manual
verification, ~46% contribution margin on the ₹49 monthly plan, and the "marketing message
= ₹0.88" cost structure that makes *paid* WhatsApp outreach expensive.

> Cost/margin figures reference the unit-economics analysis: variable cost ≈ **₹26.65 per
> paying user/month** (daily image billed as a Marketing template), contribution margin
> ≈ **₹22.35/user/month (~46%)** on the monthly plan. Rates are India-specific (₹88≈$1)
> and Meta revises them periodically — re-validate before launch.

---

## Table of Contents

1. [Core Strategic Constraint](#1-core-strategic-constraint-read-this-first)
2. [Target Audience & Positioning](#2-target-audience--positioning)
3. [Reach — Acquisition Channels](#3-reach--acquisition-channels-ranked-by-cost-efficiency)
4. [Conversion — Reach → Paying Subscribers](#4-conversion--turning-reach-into-paying-subscribers)
5. [Growth Flywheel — Referral & Sharing](#5-growth-flywheel--referral--sharing)
6. [Retention — Protects LTV](#6-retention--protects-ltv)
7. [Phased Rollout](#7-phased-rollout)
8. [Metrics](#8-metrics-to-run-the-machine)
9. [Highest-Leverage Moves](#9-three-highest-leverage-moves)
10. [Product Changes Implied](#10-product-changes-this-strategy-implies)

---

## 1. Core Strategic Constraint (read this first)

The unit economics have one dominant fact: **outbound WhatsApp marketing costs ₹0.88 per
message, but inbound (user-initiated) conversations are free for 24 hours.** This flips the
usual acquisition playbook:

- **Do NOT** cold-blast WhatsApp marketing templates to acquire users — it burns margin
  before anyone pays.
- **DO** make users **message you first** (Click-to-WhatsApp, QR codes, `wa.me` links).
  That opens a free 24-hour window in which the entire subscribe → UPI → UTR flow costs ₹0.
- **Reach should be driven by free/organic channels + Click-to-WhatsApp ads**, not by paid
  WhatsApp templates.

Every recommendation below is built around this principle.

---

## 2. Target Audience & Positioning

**Primary audience:** devotees of specific temples/deities — high-intent, emotionally
engaged, share within tight networks (family, community groups), and skew toward daily
ritual. Ideal for **word-of-mouth virality**.

**Positioning:** *"Start your day with darshan — delivered to your WhatsApp every morning."*
Frame it as a **daily spiritual habit**, not an app. The ₹49/month price is an
impulse / devotional-donation-sized decision.

**Segment by deity/temple from day one** — a Tirupati devotee and a Vaishno Devi devotee
want different images. This makes messaging razor-relevant and enables temple-level
partnerships.

---

## 3. Reach — Acquisition Channels (ranked by cost-efficiency)

### Tier 1 — Free / organic (lead with these)
- **WhatsApp community virality (the flywheel).** Devotees already sit in family and temple
  WhatsApp/Telegram groups. Give every subscriber a one-tap **"Share darshan"** forward
  (image + a `wa.me` link back to your number). Each morning's image becomes an organic ad.
  Cheapest, highest-trust channel — engineer for it deliberately (see §5).
- **Temple & community partnerships.** Co-brand with temples/trusts. They announce to their
  devotee lists, put **QR codes at the physical temple** ("Scan for daily darshan on
  WhatsApp"), and mention it in newsletters. High trust, zero CAC.
- **Organic social + short video.** Instagram Reels / YouTube Shorts of the daily darshan
  with a link-in-bio to the WhatsApp number. Devotional content has strong organic reach.
- **Influencers / priests / regional micro-creators.** Loyal, high-intent audiences; gift
  free subscriptions in exchange for a mention.

### Tier 2 — Paid, but margin-safe
- **Click-to-WhatsApp (CTWA) ads** on Meta (FB/Instagram). Critical: these are
  **user-initiated**, so the tap opens a free 72-hour messaging window and the subscribe
  flow is free. The **only** paid channel that doesn't fight the unit economics. Track CAC
  and keep **LTV:CAC ≥ 3:1** (LTV ≈ ₹134 on 6-month monthly retention → target CAC < ~₹45).
- **Regional-language search/social** targeting deity/temple keywords and festivals.

### Tier 3 — Avoid
- **Outbound WhatsApp marketing templates for cold acquisition** — ₹0.88 each, before any
  revenue. Reserve templates for *retention* of existing users (renewal reminders, already
  utility-priced at ₹0.125).

---

## 4. Conversion — Turning Reach into Paying Subscribers

The funnel: **message us → choose plan → UPI intent → pay → submit UTR → admin verify →
active.** Each step leaks. Fixes:

- **Free trial (3–7 days) before asking to pay.** A devotional habit forms fast; let users
  feel the morning ritual first. Trial→paid on a formed habit beats cold paywalling.
  (Requires a `TRIAL` subscriber state that delivers without a `SUCCESS` payment for N days.)
- **Reduce payment friction — the UTR step is the biggest leak.** Paying via UPI, then
  manually typing a 12-digit UTR, then waiting for manual admin verification is
  high-friction with a drop-off-inducing delay. Mitigations:
  - One-tap UPI intent (deep link) pre-filled with amount + reference.
  - Set expectations: "You'll be activated within X hours after we confirm," and send a
    confirmation the moment admin verifies (the `PAYMENT_VERIFIED` event already exists).
  - **Medium-term:** move to a payment gateway with auto-reconciliation (Razorpay / UPI
    AutoPay) to eliminate the manual UTR + verify step. Highest-leverage conversion fix;
    adds ~2% transaction fee.
- **Price anchoring & annual nudge.** Present yearly (₹449) beside monthly (₹49) as
  "₹37/month, save 24%." Annual improves cash flow and retention (monthly has higher
  margin %, annual has higher LTV).
- **Festival-timed pushes.** Diwali, Navratri, Janmashtami, deity-specific days = peak
  devotional intent. Run trial offers and "gift a subscription" campaigns around them.
- **Social proof in the welcome flow:** "Join 10,000+ devotees starting their day with
  darshan."

---

## 5. Growth Flywheel — Referral & Sharing

Because the audience is communal and sharing is free, **referral should be the primary
growth engine**, not paid ads.

- **"Gift darshan"** — let a subscriber gift a month to family (culturally resonant). The
  sender pays, or a free gift-trial is granted → viral acquisition.
- **Referral reward:** "Refer 3 devotees who subscribe → 1 month free." Cheap for you
  (~₹26.65 marginal cost of a free month) vs. a paid CAC.
- **Shareable daily artifact:** every morning's image is inherently forwardable — add a
  subtle watermark + `wa.me` link so every forward is a growth loop.

**Model it:** if each subscriber brings **0.3 new paying subscribers** (k-factor 0.3),
blended CAC drops ~30% and growth compounds organically.

---

## 6. Retention — Protects LTV

Retention is not separate from adoption; **higher retention raises LTV, which lets you
spend more to acquire.**

- The **renewal reminder** system is built (3-day + 1-day, utility-priced ₹0.125). Ensure
  renewal is one-tap and extends from current expiry (already implemented).
- **Win-back** lapsed users with a single utility reminder + a festival re-subscribe offer.
- **Engagement variety:** occasional special darshan (festival specials, aarti timings)
  keeps the daily message valuable and reduces opt-out.

---

## 7. Phased Rollout

| Phase | Focus | Channels | Goal |
|---|---|---|---|
| **0–3 mo (Seed)** | 1–2 temples, one deity segment | Temple QR + partnership, WhatsApp groups, organic social | Prove trial→paid conversion & retention; nail the payment flow |
| **3–9 mo (Grow)** | Add deities/temples; turn on referral | CTWA ads (measured CAC), influencers, referral/gifting | Scale paying base at LTV:CAC ≥ 3:1 |
| **9+ mo (Scale)** | Multi-deity, festivals, gifting at scale | All channels + seasonal festival pushes | Compounding organic + paid; shift plan mix toward annual |

---

## 8. Metrics to Run the Machine

- **Reach:** inbound conversations started (free-window opens), CTWA click→chat rate, QR
  scans, shares/forwards per user (**k-factor**).
- **Conversion:** message→plan-select, plan-select→paid, and
  **UTR-submit→verified→active** (watch the manual-verify drop-off and latency).
- **Economics:** CAC by channel, contribution margin/user (~₹22.35 monthly plan), LTV,
  **LTV:CAC**, payback period.
- **Retention:** monthly churn, renewal-reminder→renew rate, opt-out rate.

---

## 9. Three Highest-Leverage Moves

1. **Make acquisition inbound-first** (CTWA + QR + shareable images) so the subscribe flow
   stays in the free WhatsApp window — protects the ₹0.88 marketing margin.
2. **Kill the UTR / manual-verification friction** with a free trial now and a payment
   gateway later — this is where paying conversion leaks most.
3. **Engineer referral/gifting** into the daily image — the communal, high-trust audience
   makes word-of-mouth the cheapest, most scalable channel.

---

## 10. Product Changes This Strategy Implies

Both are small and testable, and fit the existing architecture:

- **`TRIAL` subscriber state** — delivers the daily image for N days without a `SUCCESS`
  payment, then converts or expires. Fits cleanly into the existing `SubscriberStatus`
  state machine and eligibility rules.
- **Share/referral attribution** — a field to record who referred a new subscriber, to
  measure k-factor and reward referrers.

Downstream, the biggest conversion win is replacing the **manual UTR + admin-verify** step
with an **auto-reconciling payment gateway** (Razorpay / UPI AutoPay), which removes the
largest friction point in the funnel at the cost of a ~2% transaction fee.
