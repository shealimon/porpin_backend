# When Supabase rows are written (billing / jobs / referrals)

Word-credit consumption order (unchanged): **`free_credits` → `referral_bonus_words` → subscription pool → PAYG** — see `app/services/word_credits.py` (`compute_word_charge` / `apply_word_charge`).

---

## `public.jobs` (model: `DocumentJob`)

| When | What |
|------|------|
| User uploads a file (async RQ path) | **Insert** new row: `app/services/async_job.py` → `create_and_enqueue_job_from_bytes` → `DocumentJob(...)` with `status` `pending` or `awaiting_payment` if `deferred_payment`. |
| Job runs / finishes | **Update** `status`, `tokens_used`, `cost_inr`, `output_file_path`, `completed_at`, etc. from workers: `app/workers/rq_tasks.py` (e.g. `process_document_job`, `finalize_document_translation_job`). |
| Pay-after-upload flow | If milestone/legacy path persists to DB, `app/api/routes/legacy_compat.py` may insert/update rows to mirror in-memory state. |
| Admin / recovery | Re-queue paths may touch `translation_attempt`, `error_message`, etc. |

**Note:** The product “translation job” for Supabase Auth users is **`public.jobs`**, not `translation_jobs` (see below).

---

## `public.translation_jobs` (model: `TranslationJob`)

| When | What |
|------|------|
| Historically: **API-key (`public.users`)** flow | `TranslationJob` rows reference `users.id`, not `profiles.id`. |
| Current main app (Supabase login) | **Does not** use this table for the primary upload → queue path; that uses **`public.jobs`**. |

If this table is empty in your project, that is expected when you only use Auth + `DocumentJob` pipeline.

---

## `public.transactions` (model: `PaymentTransaction`, `__tablename__ = "transactions"`)

| When | What |
|------|------|
| PAYG per-job payment verified (Razorpay) | **Insert** in `app/services/profile_inr_credit.py` → `credit_inr_from_razorpay` — `provider="razorpay_wallet"`, `kind="payg_translation"`, `external_id=payment_id`, optional `razorpay_order_id`, `job_id`. |
| Subscription webhook de-dupe | **Insert** in `app/services/razorpay_webhook.py` → `_mark_webhook_processed` — `amount_inr=0`, `provider=WEBHOOK_TX_PROVIDER`, `status="applied"` to avoid re-processing (dedup key in `external_id`). |

`payment_capture.apply_razorpay_captured_order` does **not** insert `PaymentTransaction` itself; it calls `credit_inr_from_razorpay`, which does.

---

## `public.credit_transactions` (model: `CreditTransaction`)

All inserts go through `app/services/referrals.py` → `insert_credit_transaction_if_absent` (idempotent by `idempotency_key`).

| `type` (examples) | When |
|-------------------|------|
| `referral_signup_bonus` | New referee used a code: `app/services/referrals.py` → `claim_referral` (bonus to referee on signup). |
| `referral_reward_verify` | Referrer step 1: `app/services/referral_lifecycle.py` → `try_referrer_verify_reward` / `_apply_referrer_word_credit`. |
| `referral_reward_payment` | Referrer step 2 (referee’s first pay): `try_referrer_payment_reward`. |

`referral_attribution_id` is set when the credit is tied to a specific `referral_attributions` row.

---

## `public.referral_attributions` (model: `ReferralAttribution`)

| When | What |
|------|------|
| User claims invite code at signup / sync | **Insert** in `app/services/referrals.py` → `claim_referral` — `referee_user_id`, `referrer_user_id`, `status="pending"`, `claim_ip`, `device_hash`. |
| Referee email verified (Supabase) | **Update** `status` → `verified`: `app/services/referral_lifecycle.py` → `advance_referee_referral_to_verified` / `try_referrer_verify_reward`. |
| Payout / completion | **Update** `words_credited_to_referrer`, `status` → `completed` where applicable: `try_referrer_verify_reward`, `try_referrer_payment_reward`. |

---

## Related tables (brief)

- **`public.usage`:** Row added when a job’s word/PAYG usage is recorded — `app/services/word_credits.py` → `add_usage_row` (after translation settle).
- **`public.profiles`:** Updated on sync (`syncBackendProfile` / `applySyncProfileResponse`), subscription activate, `apply_word_charge`, referral credits, etc.

---

## Code entry points (quick map)

| Concern | Primary modules |
|--------|-----------------|
| Job row create | `app/services/async_job.py` |
| Job row update (worker) | `app/workers/rq_tasks.py` |
| PAYG payment → INR + `transactions` row | `app/services/profile_inr_credit.py` |
| PAYG order notes → activate job | `app/services/payment_capture.py` |
| Referral row + credit ledger | `app/services/referrals.py`, `app/services/referral_lifecycle.py` |
| Word buckets | `app/services/word_credits.py` |
