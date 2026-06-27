# DynamoDB Data Model

This document defines the single-table design that backs every read and write path in the
platform. The goal is to satisfy all access patterns with a single physical table plus a
small number of GSIs, while keeping per-partition write throughput bounded and avoiding
hot-partition scans.

---

## 1. Design Principles

1. **One table, many entity types.** Different entity types coexist in the same table,
   discriminated by the sort key prefix. This eliminates costly joins and lets us load
   an entire user profile in a single `Query`.
2. **Owner-scoped access.** Every record (with the exception of platform-wide look-up
   tables) carries an `OWNER` segment in its partition key so that per-tenant queries
   never scan another tenant's data.
3. **Write idempotency.** Sort keys include a deterministic version (`v#<timestamp>` or
   `v#<etag>`) so re-processing an S3 event produces an overwrite of the same logical
   record rather than a duplicate.
4. **Time-bucketed GSIs.** Where we need "latest N items" queries, we bucket records by
   time period (`YYYY-MM`) inside the GSI partition key to bound the partition size.

---

## 2. Base Table Schema

Table name: **`serverless_data_platform_core`**

| Attribute | Type | Description |
|-----------|------|-------------|
| `PK` | S | Partition key |
| `SK` | S | Sort key |
| `GSI1PK` | S | GSI 1 partition key (optional, sparse) |
| `GSI1SK` | S | GSI 1 sort key (optional) |
| `GSI2PK` | S | GSI 2 partition key (optional, sparse) |
| `GSI2SK` | S | GSI 2 sort key (optional) |
| `GSI3PK` | S | GSI 3 partition key (optional, sparse) |
| `GSI3SK` | S | GSI 3 sort key (optional) |
| `ETag` | S | S3 object ETag (for idempotency on ingest) |
| `CreatedAt` | S | ISO-8601 UTC timestamp |
| `UpdatedAt` | S | ISO-8601 UTC timestamp |
| `Type` | S | Entity discriminator (`USER`, `ITEM`, `REPORT`, `UPLOAD`, `AUDIT`) |
| `...` | — | Entity-specific attributes (see below) |

**Billing mode:** on-demand (`PAY_PER_REQUEST`). We opt for on-demand because ingest is
bursty (Monday morning batches) and we want zero capacity-planning overhead. For sustained
high-write workloads (≥ 1k WCU), provision with auto-scaling.

---

## 3. Entity Catalogue

The platform tracks five entity types. Each one has a canonical PK/SK shape.

### 3.1 USER

A platform user (typically one per Cognito user). Stores profile metadata and the
Monday.com person ID for cross-system linking.

| Field | Value |
|-------|-------|
| `PK` | `USER#<cognito_sub>` |
| `SK` | `PROFILE` |
| `Type` | `USER` |
| `Email` | string |
| `DisplayName` | string |
| `MondayPersonId` | string \| null |
| `Role` | string (`admin`, `operator`, `viewer`) |

### 3.2 ITEM

A normalized record derived from an uploaded report. Items are the primary read
target of the API and the BI dashboard.

| Field | Value |
|-------|-------|
| `PK` | `USER#<owner_sub>` |
| `SK` | `ITEM#<item_id>#v#<created_at_unix>` |
| `GSI1PK` | `ITEM#<status>` (for status filtering) |
| `GSI1SK` | `<created_at_unix>` |
| `GSI2PK` | `ITEM#<report_id>` (for join-back to source) |
| `GSI2SK` | `<item_id>` |
| `Type` | `ITEM` |
| `ItemId` | UUID v4 |
| `OwnerSub` | Cognito sub |
| `Status` | `pending` \| `active` \| `archived` |
| `Title` | string |
| `Body` | map (parsed fields) |
| `SourceUploadId` | string |
| `ReportId` | string |

> **Why two SK formats?** The base table is partitioned by owner (so an owner's items
> are co-located for efficient listing). The GSI1 lets the platform filter items by
> status across all owners (admin view). GSI2 joins items back to the source REPORT.

### 3.3 REPORT

A raw or summarized ingestion manifest. One REPORT is created per upload event and
points at one or more derived ITEMs.

| Field | Value |
|-------|-------|
| `PK` | `USER#<owner_sub>` |
| `SK` | `REPORT#<report_id>` |
| `GSI1PK` | `REPORT#<yyyy-mm>` (for monthly rollups) |
| `GSI1SK` | `<created_at_unix>` |
| `Type` | `REPORT` |
| `ReportId` | UUID v4 |
| `OwnerSub` | Cognito sub |
| `SourceBucket` | string |
| `SourceKey` | string |
| `SourceETag` | string (drives idempotency) |
| `Format` | `csv` \| `xlsx` \| `pdf` |
| `ItemCount` | number |
| `Status` | `received` \| `parsed` \| `failed` |

### 3.4 UPLOAD

Tracks presigned URL grants so the API can correlate a client request with the
resulting S3 object. Sparse — only written when an upload is initiated through
the presign endpoint.

| Field | Value |
|-------|-------|
| `PK` | `USER#<owner_sub>` |
| `SK` | `UPLOAD#<upload_id>` |
| `Type` | `UPLOAD` |
| `UploadId` | UUID v4 |
| `ObjectKey` | string |
| `PresignedUrl` | string (returned once, never re-emitted) |
| `ExpiresAt` | ISO-8601 |
| `ETag` | string \| null (filled by ETL) |

### 3.5 AUDIT

Append-only event log used for compliance and incident review.

| Field | Value |
|-------|-------|
| `PK` | `USER#<owner_sub>` |
| `SK` | `AUDIT#<yyyy-mm-dd>#<event_id>` |
| `Type` | `AUDIT` |
| `EventId` | UUID v4 |
| `Action` | string (`item.created`, `item.updated`, …) |
| `ActorSub` | string |
| `Metadata` | map |

The sort-key prefix includes the date so audit writes within a day are co-located
(cheap `Query` for "all events on day X") and a partition never grows unbounded.

---

## 4. Global Secondary Indexes

| GSI | PK | SK | Projection | Purpose |
|-----|----|----|-----------|---------|
| **GSI1 — Status/Timeline** | `GSI1PK` | `GSI1SK` | ALL | Filter records by status across owners and time. Drives admin dashboards and "recent items" feeds. |
| **GSI2 — Report Lookup** | `GSI2PK` | `GSI2SK` | ALL | Resolve the items derived from a specific upload. Used by the ETL success handler and by report-detail UIs. |
| **GSI3 — Lookup by ETag** | `GSI3PK` (`ETAG#<hex>`) | `GSI3SK` (`<pk>#<sk>`) | KEYS_ONLY | Idempotency check: before writing a parsed item, the ETL Lambda `Query`s GSI3 to see if this ETag was already processed. |

**Why three and not one?** Each GSI carries a distinct access shape:

- GSI1 answers "what's happening right now" (status + time).
- GSI2 answers "what came from where" (report → items join).
- GSI3 answers "have we seen this exact file before" (idempotency without scanning the base table).

A single GSI could not satisfy all three without either scanning or carrying redundant
attributes, which is more expensive than the marginal cost of three sparse indexes.

---

## 5. Access Pattern → Key Derivation

Every read and write in the system maps to a `GetItem`, `Query`, `PutItem`, or
`UpdateItem` with explicit key construction. The table below is the **single source of
truth** that the implementation in `app/db.py` must follow.

| # | Access pattern | API surface | Keys / index used | Notes |
|---|----------------|-------------|-------------------|-------|
| AP-01 | Fetch user profile | `GET /me` | `GetItem(PK=USER#<sub>, SK=PROFILE)` | Single-item read. |
| AP-02 | Create item | `POST /items` | `PutItem(PK=USER#<sub>, SK=ITEM#<id>#v#<ts>)` with `ConditionExpression: attribute_not_exists(SK)` | Fails with 409 on collision. |
| AP-03 | Get item | `GET /items/{id}` | First `Query(PK=USER#<sub>, SK begins_with ITEM#<id>#)`, then select latest version | Uses `ScanIndexForward=False, Limit=1` after filtering. |
| AP-04 | List items for owner | `GET /items` | `Query(PK=USER#<sub>, SK begins_with ITEM#)` paginated | No GSI needed. |
| AP-05 | List items by status (admin) | `GET /admin/items?status=…` | `Query(GSI1, GSI1PK=ITEM#<status>)` | Status partition can grow; mitigate with a time-window `KeyConditionExpression` on `GSI1SK`. |
| AP-06 | Update item | `PUT /items/{id}` | `PutItem` with `ConditionExpression` on existing version | Optimistic concurrency. |
| AP-07 | Soft delete | `DELETE /items/{id}` | `UpdateItem` setting `Status='archived'` | No hard delete; audit trail preserved. |
| AP-08 | Get items for a report | `GET /reports/{id}/items` | `Query(GSI2, GSI2PK=ITEM#<report_id>)` | |
| AP-09 | Get report | `GET /reports/{id}` | `GetItem(PK=USER#<sub>, SK=REPORT#<id>)` | |
| AP-10 | Create report | ETL Lambda | `PutItem` with `ConditionExpression: attribute_not_exists(SK)` | Idempotent on `ReportId`. |
| AP-11 | Idempotency check | ETL Lambda | `Query(GSI3, GSI3PK=ETAG#<hex>, Limit=1)` | Returns early if record exists. |
| AP-12 | Append audit event | middleware | `PutItem(PK=USER#<sub>, SK=AUDIT#<date>#<event_id>)` | Date buckets prevent unbounded partitions. |
| AP-13 | List uploads | `GET /uploads` | `Query(PK=USER#<sub>, SK begins_with UPLOAD#)` | |
| AP-14 | Presign upload | `POST /uploads/presign` | `PutItem(PK=USER#<sub>, SK=UPLOAD#<id>)` then S3 `generate_presigned_url` | UPLOAD record is the audit trail. |

---

## 6. Idempotency Strategy

The ETL pipeline is naturally replay-prone: S3 can deliver `ObjectCreated` more than
once for the same object (Lambda retries, EventBridge retries, manual re-trigger).
We layer two complementary safeguards:

1. **ETag-based dedup via GSI3.** Before parsing, the ETL Lambda `Query`s
   `GSI3PK = ETAG#<source_etag>`. If any record is returned, the upload was already
   processed and we short-circuit with a 200 to S3.
2. **Conditional writes.** The final `PutItem` for the `REPORT` carries
   `ConditionExpression: attribute_not_exists(SK)`. A `ConditionalCheckFailedException`
   means another worker won the race; we treat it as success and exit.

This combination handles both slow retries (we see the ETag) and concurrent retries
(we see the conditional check fail).

---

## 7. Hot-Partition Mitigation

The platform's dominant write pattern is `ITEM#<uuid>#v#<ts>` writes spread across
many owner partitions. On the read side, GSI1 (`ITEM#<status>`) could concentrate on
the `active` status. Mitigations:

- **GSI1 status partition is time-windowed** by including `<yyyy-mm>` in the GSI sort
  key and adding a `KeyConditionExpression` for the trailing 30 days. This keeps
  each GSI1 partition under ~5k items.
- **Write sharding** is not currently needed; if item writes to a single owner exceed
  1k/s, we introduce a `<shard>` segment in the SK.

---

## 8. Capacity & Cost Notes

- On-demand mode is appropriate up to roughly 30k writes/min. Above that, switch the
  base table to provisioned with auto-scaling (`min=5, max=400, target=70 %`).
- GSI partitions cost the same as base-table partitions but share the table-level
  throughput; sparse GSIs (KEYS_ONLY) reduce per-item storage cost.
- `audit` records can be tiered to S3 via DynamoDB Streams + a daily export job; the
  table only retains the last 90 days.

---

## 9. Schema Evolution

Schema changes follow these rules:

1. **New optional attribute** — no migration. Add to Pydantic model with `Optional`.
2. **New required attribute** — two-phase deploy: (a) add as optional, (b) backfill,
   (c) flip to required.
3. **Attribute rename** — never rename in place. Write to new name + dual-read in the
   API for one release, then remove the old.
4. **GSI change** — GSIs are managed by CDK; adding a new GSI does not require code
   changes (it appears as an empty index until populated).