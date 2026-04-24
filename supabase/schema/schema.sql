-- =============================================================================
-- Translator — Supabase schema (SQL Editor)
-- =============================================================================
-- WARNING: Drops public app tables and auth sync triggers, then recreates from
-- scratch. auth.users is NOT dropped. All app data in these tables is lost.
-- Order: extensions → drops → legacy tables → profiles → auth triggers → jobs → usage → transactions
-- =============================================================================

-- --- Extensions -------------------------------------------------------------

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- --- Tear down (public app tables + profile sync on auth.users) ------------

DROP TRIGGER IF EXISTS on_auth_user_metadata_updated ON auth.users;
DROP TRIGGER IF EXISTS on_auth_user_email_updated ON auth.users;
DROP TRIGGER IF EXISTS on_auth_user_created ON auth.users;
DROP FUNCTION IF EXISTS public.sync_profile_from_auth_user() CASCADE;

-- Order: children before parents (FKs).
DROP TABLE IF EXISTS public.credit_transactions CASCADE;
DROP TABLE IF EXISTS public.transactions CASCADE;
DROP TABLE IF EXISTS public.usage CASCADE;
DROP TABLE IF EXISTS public.jobs CASCADE;
DROP TABLE IF EXISTS public.referral_attributions CASCADE;
DROP TABLE IF EXISTS public.translation_jobs CASCADE;
DROP TABLE IF EXISTS public.profiles CASCADE;
DROP TABLE IF EXISTS public.users CASCADE;

-- --- Legacy: API-key users (SEED_API_KEY / X-API-Key). Not Supabase Auth. ---

CREATE TABLE public.users (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  api_key_hash VARCHAR(128) NOT NULL UNIQUE,
  tier VARCHAR(16) NOT NULL DEFAULT 'free',
  email VARCHAR(255),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX ix_users_api_key_hash ON public.users (api_key_hash);

-- --- Legacy: old TranslationJob model (distinct from public.jobs) -------------

CREATE TABLE public.translation_jobs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES public.users (id) ON DELETE CASCADE,
  status VARCHAR(32) NOT NULL DEFAULT 'pending',
  input_filename VARCHAR(512) NOT NULL,
  input_relpath VARCHAR(1024) NOT NULL,
  export_format VARCHAR(16) NOT NULL DEFAULT 'docx',
  output_relpath VARCHAR(1024),
  error_message TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX ix_translation_jobs_user_id ON public.translation_jobs (user_id);

-- --- Profiles (1:1 with auth.users) -----------------------------------------

CREATE TABLE public.profiles (
  id UUID PRIMARY KEY REFERENCES auth.users (id) ON DELETE CASCADE,
  email VARCHAR(255),
  first_name VARCHAR(100),
  last_name VARCHAR(100),
  mobile VARCHAR(32),
  city VARCHAR(120),
  country VARCHAR(120),
  plan VARCHAR(32) NOT NULL DEFAULT 'free',
  credits_inr_balance NUMERIC(12, 2) NOT NULL DEFAULT 0,
  free_credits INTEGER NOT NULL DEFAULT 10000,
  subscription_active BOOLEAN NOT NULL DEFAULT FALSE,
  subscription_credits INTEGER NOT NULL DEFAULT 0,
  subscription_started_at TIMESTAMPTZ,
  subscription_period_start TIMESTAMPTZ,
  subscription_contract_end TIMESTAMPTZ,
  pending_subscription_kind VARCHAR(16),
  subscription_expiry TIMESTAMPTZ,
  razorpay_subscription_id VARCHAR(255),
  stripe_customer_id VARCHAR(255),
  referral_code VARCHAR(32) UNIQUE,
  referred_by_user_id UUID REFERENCES public.profiles (id) ON DELETE SET NULL,
  referral_bonus_words INTEGER NOT NULL DEFAULT 0,
  referral_words_earned_total INTEGER NOT NULL DEFAULT 0,
  preview_quota_utc_date DATE,
  preview_quota_count INTEGER NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT chk_profiles_referral_not_self
    CHECK (referred_by_user_id IS NULL OR referred_by_user_id <> id)
);

CREATE INDEX ix_profiles_email ON public.profiles (email);

-- --- Trigger: keep profiles in sync when Auth users are created/updated ------

CREATE OR REPLACE FUNCTION public.sync_profile_from_auth_user()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
  INSERT INTO public.profiles (id, email, first_name, last_name)
  VALUES (
    NEW.id,
    NEW.email,
    NULLIF(btrim(COALESCE(NEW.raw_user_meta_data->>'first_name', '')), ''),
    NULLIF(btrim(COALESCE(NEW.raw_user_meta_data->>'last_name', '')), '')
  )
  ON CONFLICT (id) DO UPDATE
    SET
      email = COALESCE(EXCLUDED.email, public.profiles.email),
      first_name = COALESCE(EXCLUDED.first_name, public.profiles.first_name),
      last_name = COALESCE(EXCLUDED.last_name, public.profiles.last_name),
      updated_at = NOW();
  RETURN NEW;
END;
$$;

CREATE TRIGGER on_auth_user_created
  AFTER INSERT ON auth.users
  FOR EACH ROW
  EXECUTE FUNCTION public.sync_profile_from_auth_user();

CREATE TRIGGER on_auth_user_email_updated
  AFTER UPDATE OF email ON auth.users
  FOR EACH ROW
  WHEN (OLD.email IS DISTINCT FROM NEW.email)
  EXECUTE FUNCTION public.sync_profile_from_auth_user();

CREATE TRIGGER on_auth_user_metadata_updated
  AFTER UPDATE OF raw_user_meta_data ON auth.users
  FOR EACH ROW
  WHEN (OLD.raw_user_meta_data IS DISTINCT FROM NEW.raw_user_meta_data)
  EXECUTE FUNCTION public.sync_profile_from_auth_user();

-- --- Referrals (one row per referee; caps referrer rewards server-side) --------

CREATE TABLE public.referral_attributions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  referee_user_id UUID NOT NULL UNIQUE REFERENCES public.profiles (id) ON DELETE CASCADE,
  referrer_user_id UUID NOT NULL REFERENCES public.profiles (id) ON DELETE CASCADE,
  status VARCHAR(16) NOT NULL DEFAULT 'pending',
  claim_ip VARCHAR(45),
  device_hash VARCHAR(64),
  words_credited_to_referrer INTEGER NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT chk_referral_attributions_distinct_users
    CHECK (referrer_user_id <> referee_user_id)
);

CREATE INDEX ix_referral_attributions_referrer ON public.referral_attributions (referrer_user_id);
CREATE INDEX ix_referral_attributions_device_hash ON public.referral_attributions (device_hash);

CREATE TABLE public.credit_transactions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES public.profiles (id) ON DELETE CASCADE,
  type VARCHAR(32) NOT NULL,
  credits INTEGER NOT NULL,
  referral_attribution_id UUID REFERENCES public.referral_attributions (id) ON DELETE SET NULL,
  idempotency_key VARCHAR(128) NOT NULL UNIQUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX ix_credit_transactions_user_id ON public.credit_transactions (user_id);

-- --- Async document jobs (DocumentJob / RQ) ----------------------------------

CREATE TABLE public.jobs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES public.profiles (id) ON DELETE CASCADE,
  status TEXT NOT NULL DEFAULT 'pending',
  input_filename TEXT NOT NULL DEFAULT '',
  input_file_path TEXT,
  export_format TEXT NOT NULL DEFAULT 'docx',
  output_file_path TEXT,
  error_message TEXT,
  content_hash VARCHAR(64),
  file_type TEXT,
  tokens_used BIGINT NOT NULL DEFAULT 0,
  cost_inr NUMERIC(10, 2) NOT NULL DEFAULT 0,
  processing_time_seconds DOUBLE PRECISION,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  completed_at TIMESTAMPTZ,
  quoted_payg_inr NUMERIC(10, 2) NOT NULL DEFAULT 0,
  translation_attempt INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX idx_jobs_user_id ON public.jobs (user_id);
CREATE INDEX idx_jobs_status ON public.jobs (status);
CREATE INDEX idx_jobs_content_hash ON public.jobs (content_hash);
CREATE INDEX idx_jobs_user_content_hash ON public.jobs (user_id, content_hash);

-- --- Usage metering -----------------------------------------------------------

CREATE TABLE public.usage (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES public.profiles (id) ON DELETE CASCADE,
  job_id UUID REFERENCES public.jobs (id) ON DELETE SET NULL,
  tokens_used INTEGER NOT NULL DEFAULT 0,
  cost_inr NUMERIC(12, 4) NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX ix_usage_user_id ON public.usage (user_id);
CREATE INDEX ix_usage_job_id ON public.usage (job_id);

-- --- Payment transactions (e.g. Stripe) -------------------------------------

CREATE TABLE public.transactions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES public.profiles (id) ON DELETE CASCADE,
  amount_inr NUMERIC(12, 4) NOT NULL,
  provider VARCHAR(32) NOT NULL DEFAULT 'stripe',
  external_id VARCHAR(255),
  status VARCHAR(32) NOT NULL DEFAULT 'completed',
  kind VARCHAR(32) NOT NULL DEFAULT 'wallet_topup',
  razorpay_order_id VARCHAR(255),
  job_id UUID REFERENCES public.jobs (id) ON DELETE SET NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX ix_transactions_user_id ON public.transactions (user_id);

-- -----------------------------------------------------------------------------
-- Troubleshooting: API returns empty rows but Table Editor shows data
-- -----------------------------------------------------------------------------
-- 1) Confirm backend SUPABASE_DATABASE_URL points at THIS project (pooler URI).
-- 2) jobs.user_id and profiles.id must match auth.users.id for the signed-in user.
-- 3) If you enabled RLS on public.profiles / public.jobs, the FastAPI DB user must
--    bypass RLS (default postgres pooler role) or you need policies; otherwise SELECT
--    returns zero rows to the API.
