-- food_db schema — real application data
-- Runs from docker-entrypoint-initdb.d AFTER 00-init-multi-db.sh has created
-- the databases. The initdb scripts connect to the default database, so switch
-- into food_db explicitly first.
\connect food_db

-- ---------------------------------------------------------------------------
-- menu_catalog: one row per recipe, built offline by the Airflow pipeline.
-- The Model Service only READS from this table (including cluster_id) — it
-- never writes cluster assignments. K-Means runs in Airflow, not per request.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS menu_catalog (
    menu_id                    TEXT PRIMARY KEY,            -- Spoonacular recipe id, as string (API contract: menu_id is string)
    source                     TEXT NOT NULL DEFAULT 'spoonacular',
    name                       TEXT NOT NULL,
    servings                   NUMERIC CHECK (servings IS NULL OR servings > 0),

    -- How this row's nutrition was obtained (CLAUDE.md Phase 2b — mandatory,
    -- never NULL). 'spoonacular_computed' = the primary path (source API's own
    -- computed nutrition). 'usda_estimated' = TheMealDB recipe whose nutrition
    -- was estimated by parsing measures -> grams -> USDA FDC lookup.
    nutrition_source           TEXT NOT NULL DEFAULT 'spoonacular_computed'
        CHECK (nutrition_source IN ('spoonacular_computed', 'usda_estimated')),

    -- Computed per-serving nutrition. For 'spoonacular_computed' these come
    -- from Spoonacular; for 'usda_estimated' they are summed from matched
    -- ingredients (per 100 g) and divided by servings. Physical-plausibility
    -- checks also run in the Airflow validate step; these are a last-line
    -- guard at the storage layer.
    calories                   NUMERIC NOT NULL CHECK (calories >= 0),
    protein_g                  NUMERIC NOT NULL CHECK (protein_g >= 0),
    carbs_g                    NUMERIC NOT NULL CHECK (carbs_g   >= 0),
    fat_g                      NUMERIC NOT NULL CHECK (fat_g     >= 0),

    -- Ingredient names as a JSON array of strings — input to the rule-based
    -- safety filter (stage 1). Not used by clustering.
    ingredients                JSONB NOT NULL DEFAULT '[]'::jsonb,

    -- Diet/allergen tags supplied by the source API. ALLOWED for the safety
    -- filter only. BANNED as a clustering feature or training label.
    diet_tags                  JSONB NOT NULL DEFAULT '[]'::jsonb,

    -- Layer B feature columns (computed in compute_nutrition_ratios).
    -- Formulas: food-rec-domain skill section 5.
    pct_calories_from_protein  NUMERIC,
    pct_calories_from_carbs    NUMERIC,
    pct_calories_from_fat      NUMERIC,
    calories_per_serving       NUMERIC,

    -- Layer B output. NULL until a K-Means run assigns them.
    cluster_id                 INTEGER,
    model_version              TEXT,
    -- true when the K-Means model was trained on a catalog of 150-499 rows
    -- (Phase 3 gate): usable but not yet stable. Downstream (Phase 5) may
    -- surface this. false once trained on >= 500 rows.
    model_provisional          BOOLEAN NOT NULL DEFAULT false,

    raw_payload                JSONB,                       -- original API response for traceability
    created_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                 TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_menu_catalog_cluster_id ON menu_catalog (cluster_id);
CREATE INDEX IF NOT EXISTS idx_menu_catalog_model_version ON menu_catalog (model_version);
CREATE INDEX IF NOT EXISTS idx_menu_catalog_nutrition_source ON menu_catalog (nutrition_source);

-- ---------------------------------------------------------------------------
-- prediction_log: one row per /recommend call, for system-quality monitoring.
--
-- PRIVACY: this table stores age / weight / goal and the other profile inputs
-- WITH NO IDENTIFIER TIED TO A REAL PERSON (no name, email, account id, IP).
-- It exists only to monitor the quality of the system over time — it is NOT a
-- long-term user profile store. Do not add a user identifier column without
-- revisiting the consent/privacy scope (see CLAUDE.md "When unsure — ask").
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS prediction_log (
    id                     BIGSERIAL PRIMARY KEY,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- request profile (anonymous)
    request_age            INTEGER,
    request_sex            TEXT,
    request_weight_kg      NUMERIC,
    request_height_cm      NUMERIC,
    request_activity_level TEXT,
    request_goal           TEXT,
    request_allergies      JSONB NOT NULL DEFAULT '[]'::jsonb,
    request_diet_type      TEXT,

    -- Layer A output
    target_calories        NUMERIC,
    target_protein_g       NUMERIC,
    target_carbs_g         NUMERIC,
    target_fat_g           NUMERIC,

    -- response summary
    recommended_menu_ids   JSONB NOT NULL DEFAULT '[]'::jsonb,
    excluded_count         INTEGER NOT NULL,               -- safety-filter transparency signal; always present
    model_version          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_prediction_log_created_at ON prediction_log (created_at);

-- ---------------------------------------------------------------------------
-- extraction_quota: persisted Spoonacular usage counter, one row per day.
-- The extractor reads today's row on startup instead of resetting an
-- in-memory counter, so a DAG/worker restart does not blow the daily budget.
-- Usage is tracked in POINTS (not request count) — see docs/spoonacular-quota.md.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS extraction_quota (
    quota_date     DATE PRIMARY KEY DEFAULT CURRENT_DATE,
    points_used    NUMERIC NOT NULL DEFAULT 0 CHECK (points_used >= 0),
    request_count  INTEGER NOT NULL DEFAULT 0 CHECK (request_count >= 0),
    last_call_at   TIMESTAMPTZ,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
