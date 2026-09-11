# VIP Seva — Distribution, Pricing & Retention Strategy

This document reflects the current WhatsApp-first journey, utility-template delivery and
plan catalog in `config.json`. Treat prices and Meta messaging charges as test inputs: measure
them in production and review them whenever Meta changes template classification or rates.

## Current offer

| Customer-facing plan | Price | Access | Intended role |
|---|---:|---:|---|
| Starter | ₹9 | 3 days | Low-friction first purchase |
| Weekly | ₹69 | 30 days | Entry recurring offer |
| Monthly | ₹199 | 90 days | Mid-term retention offer |
| Yearly | ₹699 | 365 days | Best-value premium commitment |

The labels `weekly` and `monthly` currently describe neither seven nor thirty days. Because
the webhook displays those keys directly, this can reduce trust at the plan-selection CTA.
Prefer customer-facing names such as **30 Days** and **90 Days**, or rename the configuration
keys in a coordinated code/config migration. Until then, support and marketing copy must show
both price and exact day count.

## Positioning

Lead with the outcome: **personalized daily temple darshan on WhatsApp, with one familiar
link every day**. Trust is part of the product, so always identify the source temple, explain
that payment activation is manually verified, and never imply an official temple affiliation
unless one exists.

Primary audiences:

- Families building a shared morning devotional habit.
- Devotees following particular temples or weekday deities.
- Adult children gifting a simple devotional service to parents.
- Existing short-plan users ready for a longer, better-value commitment.

## Acquisition journey

Use inbound-first acquisition through temple/community QR codes, `wa.me` links, referrals,
organic devotional posts and measured Click-to-WhatsApp campaigns. The ideal first session is:

1. Customer sends “Radhe Radhe”.
2. Welcome menu offers Subscribe, Renew and Stop messages.
3. Subscribe opens the complete plan list with price and exact duration.
4. The service captures a greeting name when needed.
5. Customer explicitly accepts WhatsApp delivery consent.
6. The service creates a UPI instruction and reference number.
7. Customer pays and replies with the 12-digit UTR.
8. Admin verifies payment, activates or renews, and publishes the personalized page.
9. Daily utility-template delivery links to that page.

Do not advertise “instant activation” while UTR verification remains manual. State a realistic
verification service level and send a clear confirmation after approval.

## Conversion strategy

- Use Starter at ₹9 as the acquisition offer; do not add a second free trial unless abuse and
  conversion are measured.
- Mark Yearly ₹699 as **Best Value** and show its effective cost as approximately ₹58/month.
- Position the 90-day ₹199 plan as **Most Popular** to bridge the commitment gap.
- Always display duration alongside the plan label.
- Let returning customers renew their existing plan without recapturing their name.
- Keep amount, UPI link, reference and “reply with 12-digit UTR” in one instruction.
- Track time from UTR receipt to admin activation; long verification delays reduce trust.

## Premium retention

Annual retention should come from service value, not repeated discounting:

- Reliable delivery and a working personalized page every day.
- Visible source-temple attribution.
- Festival-category fallback when regular Vrindavan darshan is unavailable.
- Renewal reminders three, two and one day before expiry.
- Renewal extending from the current expiry date so paid days are never lost.
- Easy STOP/UNSUBSCRIBE/CANCEL handling and easy resubscription.
- Optional loyalty renewal pricing only after measuring annual renewal behavior.

## Referral and gifting

Make acquisition content easy to share without exposing a subscriber’s name, status or
unguessable subscription URL. A separate public acquisition link is safer than forwarding a
personalized page. Test:

- “Gift 30 days of darshan” for parents or relatives.
- Temple-specific QR codes for attribution.
- Referral codes stored separately from subscription identifiers.
- Festival campaigns linked to the relevant weekday/source content.

## Metrics

Review the funnel weekly:

| Stage | Primary metric |
|---|---|
| Reach | QR/wa.me/advert click → inbound “Radhe Radhe” |
| Intent | Welcome → Subscribe or Renew CTA rate |
| Choice | Plan-list view → plan selection |
| Consent | Plan selection → opt-in acceptance |
| Payment | UPI instruction → valid UTR submission |
| Operations | Median UTR-to-activation time |
| Delivery | Sent, failed and duplicate-prevention rate |
| Retention | 30-/90-day continuation, annual renewal and opt-out rate |
| Economics | Revenue, Meta cost, support cost, CAC and contribution by plan |

Do not infer retention from messages sent. Join `payments.csv`, `subscribers.csv`,
`renewals.csv` and `sentlog.csv`; remember that operational `logs.csv` and `sentlog.csv` keep
only the latest 30 calendar days.

## Testing plan

Change one major variable at a time and keep each test long enough to observe payment and
delivery behavior:

1. Test the current catalog against clearer duration-based labels without changing prices.
2. Test the ₹9 Starter CTA placement.
3. Test “Most Popular” on the 90-day plan and “Best Value” on Yearly.
4. Test the UTR-verification service-level message.
5. Review annual conversion and renewal before introducing a loyalty discount.

Every experiment must preserve explicit consent, transparent duration, easy opt-out and the
same delivery quality for all paying customers.
