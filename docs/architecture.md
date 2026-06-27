# Architecture

## High-Level Diagram

```
                            ┌────────────────────────────────────────────┐
                            │                  CLIENTS                    │
                            │  (Browser, CLI, BI dashboards, ETL jobs)   │
                            └─────────┬──────────────────────┬────────────┘
                                      │                      │
                              HTTPS + Cognito JWT           │ presigned PUT
                                      │                      ▼
                                      ▼               ┌─────────────────┐
                              ┌──────────────┐       │   S3 (uploads)  │
                              │ API Gateway  │       └────────┬────────┘
                              │  (REST)      │                │
                              └──────┬───────┘                │ ObjectCreated
                                     │                        ▼
                                     │               ┌──────────────────────┐
                                     │               │   EventBridge bus    │
                                     │               └────────┬─────────────┘
                                     │                        │
                          ┌──────────▼────────┐               │
                          │   Lambda:  api    │               ▼
                          │  (FastAPI+Mangum) │       ┌─────────────────┐
                          └──┬───────────────┘       │ Lambda:         │
                             │                       │  etl_parser     │
                             │ IAM                    └────┬────────────┘
                             ▼                            │ Parse (csv/
                       ┌────────────┐                      │  xlsx/pdf)
                       │  DynamoDB  │ ◄────────────────────┘ writes (idem)
                       │ single-    │            ┌─────────────┐
                       │ table +    │ ◄──────────│   DLQ       │
                       │ 3 GSIs     │            │ (per Lambda)│
                       └────▲───────┘            └─────────────┘
                            │
                            │
              ┌─────────────┴──────────────┐
              │                            │
       ┌──────────────┐              ┌──────────────┐
       │ Cognito User │              │ Monday.com   │
       │    Pool      │              │   webhook    │
       │  (JWT iss.)  │              │  (HMAC)      │
       └──────────────┘              └──────┬───────┘
                                            │
                                            ▼
                                  ┌──────────────────┐
                                  │ Lambda:          │
                                  │  webhook_monday  │ ───► DynamoDB
                                  └──────────────────┘
```

---

## Component Responsibilities

### 1. Clients

Internal applications — the BI dashboard, the finance reconciliation job, and the
operations console. All authenticate with a Cognito JWT issued by the user pool.

### 2. Amazon S3 — uploads bucket

Holds raw uploaded files. S3 ObjectCreated events drive the ingestion pipeline.

**Key properties:**

- Versioning enabled (rollback is non-negotiable for an ingestion pipeline).
- Server-side encryption with SSE-KMS.
- Lifecycle rule: transition to S3 Glacier after 90 days; expire after 7 years.
- Public access blocked at the account level.

### 3. EventBridge bus

Receives S3 events and routes them to the ETL Lambda. Future fan-out targets
(notifications, thumbnails) can subscribe without modifying the producer.

### 4. Lambda — `etl_parser`

Triggered by `ObjectCreated`. Responsibilities:

1. Verify the event source (S3 bucket/ARN).
2. `GetObject` from S3.
3. Dispatch to the correct parser based on file extension.
4. Run idempotency check via GSI3 (`ETAG#<hex>`).
5. `PutItem` for the `REPORT` (conditional on absence).
6. `BatchWriteItem` for derived `ITEM`s (with retries on unprocessed items).
7. Emit a CloudWatch metric for parsed rows and a structured log line.

**Failure modes:**

- Transient AWS failures → automatic retry (Lambda default).
- Parse failures after retries → message lands in the per-Lambda DLQ (SQS).
- DLQ messages are alerted on via a CloudWatch alarm (in CDK).

### 5. API Gateway + Lambda — `api`

REST API. Routes are declared in `app/main.py` and translated by Mangum into
Lambda invocations. The same code runs locally under `uvicorn` for dev.

**Auth:** API Gateway has a Cognito user pool authorizer attached. As a defence-in-depth
measure, the FastAPI dependency `current_user` re-verifies the JWT signature and
expiry on every request — authorizer caching at the gateway level is bypassed for
sensitive routes.

### 6. DynamoDB — `serverless_data_platform_core`

The single source of truth. See `docs/data-model.md` for the full schema. Briefly:

- One base table with PK/SK = owner/record-shape.
- Three GSIs: status/timeline, report lookup, ETag idempotency.
- On-demand billing; provisioned auto-scaling for sustained traffic.

### 7. Cognito User Pool

Issues JWTs (RS256, 1-hour access tokens). The user pool client is wired to the API
Gateway authorizer. Password policy, MFA, and account-recovery settings are
configured in `infra/cdk_app.py`.

### 8. Lambda — `webhook_monday`

Receives HTTPS POSTs from Monday.com when a task changes status.

- HMAC-SHA256 signature verification using `MONDAY_WEBHOOK_SECRET`.
- Maps Monday `pulse_id` to our internal `ItemId` (looked up via GSI2 or stored mapping).
- Writes an updated `ITEM` record and an `AUDIT` entry.

A separate outbound client (`client.py`) issues GraphQL v2 queries to read/write
boards on demand.

### 9. Dead-Letter Queues (SQS)

One DLQ per async Lambda. Failed messages remain for 14 days. A CloudWatch alarm
fires when `ApproximateNumberOfMessagesVisible > 0`.

### 10. Observability

- **CloudWatch Logs**: every Lambda logs to its own log group with structured JSON.
- **CloudWatch Metrics**: emitted via `boto3`'s embedded metric format (EMF).
- **X-Ray**: enabled on all Lambdas and API Gateway stages.
- **Alarms**: DLQ depth, API 5xx rate, Lambda duration > p99 SLO.

---

## Data Flow Walkthroughs

### Flow A — Upload & Read

1. Client calls `POST /uploads/presign` with file metadata → API returns `{uploadId, url}`.
2. Client `PUT`s the file directly to S3 (no API traffic).
3. S3 emits `ObjectCreated` → EventBridge → ETL Lambda.
4. ETL Lambda downloads, parses, writes `REPORT` + `ITEM`s.
5. Client polls `GET /items/{id}` (or subscribes via webhook) and receives the parsed record.

### Flow B — Monday.com Sync

1. User changes a Monday task status.
2. Monday POSTs to the API Gateway endpoint configured for `webhook_monday`.
3. Lambda verifies the HMAC signature; rejects with 401 on mismatch.
4. Lambda maps `pulse_id` → `ItemId` and updates DynamoDB.
5. (Optional) Lambda calls the outbound GraphQL client to comment on the Monday task
   with the sync timestamp.

---

## Security Posture

- All public endpoints sit behind Cognito-issued JWTs.
- S3 buckets are private; the only way to write to them is via the API-issued
  presigned URLs (which themselves require a valid JWT).
- IAM roles follow least-privilege: each Lambda has a role granting only the actions
  it performs.
- Secrets (Monday API token, webhook secret) live in AWS Secrets Manager and are
  injected as environment variables at Lambda init.
- VPC: not required at this scale. If PII processing is added, the ETL Lambda joins
  a private subnet with a VPC endpoint to DynamoDB and S3.

---

## Cost Snapshot

| Component | Driver | Monthly est. (500 docs/day) |
|-----------|--------|------------------------------|
| Lambda invocations | duration × invocations | < $5 |
| DynamoDB | on-demand reads/writes | $20–40 |
| S3 | storage + requests | $5–10 |
| API Gateway | request count | $5 |
| Cognito | MAU | $0 (free under 50k MAU) |
| CloudWatch | logs + metrics | $5 |
| **Total** | | **~$40–65** |

Well under the $250/month ceiling specified in `SPEC.md`.