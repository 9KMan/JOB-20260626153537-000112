# SPEC: Senior Python Backend Engineer — AWS Asset & Portfolio Mgmt Platform

**Upwork Job:** `~022070493442751705197`
**Company:** (via Upwork — Worldwide)
**Job ID:** JOB-20260626153537-000112
**Engagement:** Ongoing, 3–6 months, >30 hrs/week (full-time)
**Rate:** $30–$50/hr (expert)
**Tier:** EXPERT
**Project Type:** Complex project

---

## Business Problem Solved

The client is building a **cloud-based asset and portfolio management platform** that connects three messy domains — financial reporting data, property management systems (Yardi, Excel-based reports), and workflow automation tools (Monday.com) — into a single source of truth their internal teams use every day. The architecture is already defined (AWS-native, DynamoDB single-table, S3 raw landing zone, Cognito/IAM auth, Monday integration layer, ETL pipeline tier). What they need now is a senior backend engineer who can **implement to the spec** — building services, ETL jobs, and integrations against an agreed-upon design without re-architecting from scratch.

Without this role, their internal portal sits half-built and the integrations between lender reports, Yardi exports, and Monday workflows stay manual — every property search becomes a multi-tool fire drill for the operations team.

---

## What You'll Build (Scope)

The system has three layers; the role owns the implementation of all of them.

### 1. **Backend API (AWS-native)**
- FastAPI / Flask REST endpoints behind AWS Cognito / IAM auth
- File upload/download to S3 with proper presigned-URL flow
- Read/write of core structured data in DynamoDB (single-table design)
- Task create/update calls into Monday.com
- Webhook receiver for Monday status sync

### 2. **ETL / Data Processing Tier (event-driven + batch)**
- Scheduled / triggered jobs (AWS Lambda / Batch) that:
  - Import reports from lenders/banks (PDF, CSV, XLSX)
  - Import reports from property manager systems (Yardi exports, Excel)
  - Parse financial data (totals, balances, line items, dates)
  - Land raw files in S3 with metadata
  - Write structured records into DynamoDB

### 3. **Integration Layer (Monday.com)**
- Outbound: push tasks and updates from the platform to Monday
- Inbound: receive webhook updates from Monday and synchronise task status back to the platform

### 4. **Cross-cutting Responsibilities**
- DynamoDB data model design (single-table, access patterns first)
- Secure authentication (Cognito user pools + IAM roles for service-to-service)
- Structured logging, CloudWatch metrics, error handling with retry/backoff
- Documentation (OpenAPI spec, runbooks, data dictionary)

---

## How You'll Work

- **Architecture is given, you implement.** Client provides the system design; your job is to ship working code against it.
- **Serverless-first.** Lambdas for compute, DynamoDB for state, S3 for files, Cognito for auth.
- **Production-grade from day one.** Type hints, tests, error handling, observability — not "we'll add that later".
- **Iterative delivery.** Small PRs, frequent reviews, working software each sprint.
- **Async-friendly.** Distributed team, written communication primary.

---

## Mandatory Tech Stack

| Category | Tech |
|---|---|
| **Languages** | Python (primary) |
| **Compute** | AWS Lambda, AWS Batch |
| **Storage** | DynamoDB (single-table), S3 |
| **Auth** | AWS Cognito, IAM |
| **API** | FastAPI / Flask, REST/JSON |
| **Data formats** | JSON, CSV, Excel (openpyxl / pandas), PDF parsing |
| **Patterns** | ETL pipelines, event-driven architecture, serverless |

### Strongly Preferred

- **Monday.com API** integration
- **Yardi / property management data** familiarity
- **Terraform / CDK** for infrastructure-as-code

### Nice to Have

- Financial-data domain experience (REIT, fund admin, accounting)
- BI/dashboard layer (QuickSight, Tableau)

---

## Proposed Technical Architecture

Based on the stated requirements, the implementation will look like:

```
┌───────────────────────────────────────────────────────────────┐
│  CLIENT LAYER — Web Application / Portal                      │
│  • KPI dashboards for internal teams                           │
│  • Authenticated via Cognito user pool JWTs                   │
└───────────────────────────────────────────────────────────────┘
                            │ HTTPS / REST + JSON
                            ▼
┌───────────────────────────────────────────────────────────────┐
│  API LAYER — AWS API Gateway + Lambda (FastAPI on Mangum)     │
│  • Auth: Cognito JWT verifier                                 │
│  • CRUD: DynamoDB single-table (PK/SK design)                 │
│  • Files: S3 presigned URLs (upload + download)               │
│  • Tasks: outbound to Monday.com GraphQL API                  │
│  • Webhooks: receive Monday updates, sync task status         │
└───────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌───────────────────────────────────────────────────────────────┐
│  ETL / DATA PROCESSING — EventBridge + Lambda / Batch         │
│  • Scheduled triggers (cron) for batch lender/Yardi imports   │
│  • S3 ObjectCreated events trigger parse-and-load Lambdas     │
│  • Parsers: PDF (pdfplumber), Excel (openpyxl), CSV (pandas)  │
│  • Raw landing in S3 with metadata, structured in DynamoDB    │
│  • Idempotent (S3 etag + DynamoDB conditional writes)         │
└───────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌───────────────────────────────────────────────────────────────┐
│  DATA LAYER                                                    │
│  • S3: raw uploads (versioned, lifecycle to Glacier)           │
│  • DynamoDB: single-table design (Entities + GSI patterns)    │
│  • KMS: envelope encryption on sensitive fields               │
└───────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌───────────────────────────────────────────────────────────────┐
│  EXTERNAL INTEGRATIONS                                         │
│  • Monday.com: GraphQL v2 (outbound + inbound webhook)        │
│  • Lender/bank portals: per-vendor connectors (HTTP/CSV)      │
│  • Yardi: scheduled export pull + parser                       │
└───────────────────────────────────────────────────────────────┘
```

---

## Acceptance Criteria

A working implementation must demonstrate:

1. **One end-to-end flow working** — at minimum: file upload → S3 → parse → DynamoDB → API read. Live, not mocked.
2. **Auth working** — Cognito user pool + JWT-protected API endpoint; IAM role for service-to-service
3. **DynamoDB single-table model** — designed for ≥3 access patterns, with GSIs where required
4. **ETL job** — one scheduled Lambda that ingests a real (or realistic) Yardi/Excel export into DynamoDB
5. **Monday.com integration** — outbound task creation from the API → Monday, with a sample status sync
6. **Observability** — CloudWatch structured logs, custom metric on at least one ETL job, alert on parse failure
7. **Tests** — pytest with ≥70% coverage on the API + parser layers
8. **IaC** — Terraform or CDK module that provisions the entire stack from scratch

---

## Out of Scope

- The web application / portal UI (client team owns)
- BI dashboards (downstream consumers)
- ML / analytics (not in current scope)
- Mobile apps

---

## Proposal Talking Points

1. **DynamoDB single-table design story** — describe a production system where you designed access patterns first and derived PK/SK from them, with at least one non-obvious GSI choice
2. **ETL pipeline ownership** — example of a serverless ETL job you owned end-to-end (ingest → parse → load → monitor), what broke, how you fixed it durably
3. **Lambda + S3 + DynamoDB orchestration** — show a real example with idempotency, retries, and DLQ patterns
4. **Auth + security** — Cognito JWT verifier + IAM policy you wrote, with the threat model in mind
5. **Monday.com or similar webhook integration** — concrete example of bidirectional sync you shipped
6. **Async communication style** — how you handle PR reviews, written status updates, and distributed team cadence