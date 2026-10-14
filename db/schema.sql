-- PostgreSQL reference schema for auditability and evidence provenance.
-- The MVP code does not require a database yet; this is the target persistence model.

CREATE TABLE IF NOT EXISTS analysis_requests (
    request_id TEXT PRIMARY KEY,
    as_of TIMESTAMPTZ NOT NULL,
    asset_type TEXT NOT NULL,
    ticker TEXT NOT NULL,
    exchange TEXT,
    currency TEXT,
    user_question TEXT NOT NULL,
    investment_horizon TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sources (
    source_id TEXT PRIMARY KEY,
    source_type TEXT NOT NULL,
    publisher TEXT,
    source_url TEXT,
    observed_at TIMESTAMPTZ,
    content_hash TEXT,
    quality_score NUMERIC(5,4) CHECK (quality_score BETWEEN 0 AND 1),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS agent_runs (
    run_id UUID PRIMARY KEY,
    request_id TEXT NOT NULL REFERENCES analysis_requests(request_id) ON DELETE CASCADE,
    agent_id TEXT NOT NULL,
    prompt_version TEXT,
    model_name TEXT,
    stage TEXT NOT NULL,
    status TEXT NOT NULL,
    score NUMERIC(6,5) CHECK (score BETWEEN -1 AND 1),
    confidence NUMERIC(5,4) CHECK (confidence BETWEEN 0 AND 1),
    evidence_quality NUMERIC(5,4) CHECK (evidence_quality BETWEEN 0 AND 1),
    input_hash TEXT,
    latency_ms INTEGER CHECK (latency_ms >= 0),
    input_tokens INTEGER CHECK (input_tokens >= 0),
    output_tokens INTEGER CHECK (output_tokens >= 0),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    raw_output JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_agent_runs_request ON agent_runs(request_id);
CREATE INDEX IF NOT EXISTS idx_agent_runs_agent ON agent_runs(agent_id, completed_at DESC);

CREATE TABLE IF NOT EXISTS claims (
    claim_id UUID PRIMARY KEY,
    request_id TEXT NOT NULL REFERENCES analysis_requests(request_id) ON DELETE CASCADE,
    agent_run_id UUID REFERENCES agent_runs(run_id) ON DELETE SET NULL,
    claim_text TEXT NOT NULL,
    claim_kind TEXT NOT NULL CHECK (claim_kind IN ('fact', 'analysis', 'risk', 'invalidator')),
    impact NUMERIC(6,5) CHECK (impact BETWEEN -1 AND 1),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS claim_evidence (
    claim_id UUID NOT NULL REFERENCES claims(claim_id) ON DELETE CASCADE,
    source_id TEXT NOT NULL REFERENCES sources(source_id) ON DELETE RESTRICT,
    support_strength NUMERIC(5,4) CHECK (support_strength BETWEEN 0 AND 1),
    PRIMARY KEY (claim_id, source_id)
);

CREATE TABLE IF NOT EXISTS reports (
    request_id TEXT PRIMARY KEY REFERENCES analysis_requests(request_id) ON DELETE CASCADE,
    stance TEXT NOT NULL,
    score NUMERIC(6,5) NOT NULL CHECK (score BETWEEN -1 AND 1),
    confidence NUMERIC(5,4) NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    evidence_quality NUMERIC(5,4) NOT NULL CHECK (evidence_quality BETWEEN 0 AND 1),
    report_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
