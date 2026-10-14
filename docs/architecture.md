# Architecture decisions

## 1. Python owns orchestration

The LLM is not allowed to decide whether mandatory verification and risk-control stages run. The deterministic router can later be supplemented by an LLM relevance ranker, but the ranker may only add/reorder optional specialists.

## 2. Numeric scoring is deterministic

Specialists return `score`, `confidence`, `evidence_quality`, and `source_ids`. Python computes the aggregate using source-independence penalties. A100 CIO does not generate the final numeric score, confidence, source IDs, or execution metadata.

## 3. Security analysis and suitability are separated

A security can be attractive while being unsuitable for a given user. A62 Position Sizing is therefore blocked unless a risk profile is explicitly available. Production systems should isolate personalized portfolio context from generic security-analysis agents by default.

## 4. Data is injected, not improvised

The default provider is intentionally a passthrough. Production adapters should normalize source timestamps, reporting periods, currencies, units, and identifiers before any agent sees the data.

## 5. Evidence is a graph

The target PostgreSQL schema stores `claim -> source` edges. `source_id` deduplication in the MVP is a first approximation; production should cluster syndicated/reprinted content by underlying origin.

## 6. Failure degrades confidence

Agent failures become structured failed results instead of crashing an entire parallel stage. Verification quality caps final confidence.

## 7. 100 is a registry size, not a per-request fan-out

The 100 roles are available capabilities. The router normally activates a bounded subset plus mandatory verification/committee stages. This avoids unnecessary cost and correlated noise.
