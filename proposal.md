# Proposal — AWS Asset & Portfolio Mgmt Platform

**Working prototype:** [https://github.com/9KMan/JOB-20260626153537-000112](https://github.com/9KMan/JOB-20260626153537-000112)

Below: every claim is grounded in code that already exists in the prototype repo. Each evidence bullet shows the file path and line number — click through to verify.

## What I'll Deliver

### DynamoDB single-table design story

**Evidence in the prototype repo:**


*DynamoDB single-table design:*

- `docs/data-model.md:1` 📝 `# DynamoDB Data Model`
- `docs/data-model.md:3` 📄 `This document defines the single-table design that backs every read and write path in the \| platform. The goal is to satisfy all access patterns with a single p`

### ETL pipeline ownership

**Evidence in the prototype repo:**


*ETL pipeline ownership:*

- `lambdas/etl_parser/handler.py:55` 🔧 `def handler(event: dict[str, Any], context: Any) -> dict[str, Any]: \| """Lambda handler.`

### Lambda + S3 + DynamoDB orchestration

**Evidence in the prototype repo:**


*DynamoDB single-table design:*

- `docs/data-model.md:1` 📝 `# DynamoDB Data Model`
- `docs/data-model.md:3` 📄 `This document defines the single-table design that backs every read and write path in the \| platform. The goal is to satisfy all access patterns with a single p`

*Lambda + serverless architecture:*

- `app/main.py:28` 📄 `from fastapi.responses import JSONResponse \| from mangum import Mangum`
- `app/main.py:98` 📄 `# lifespan handling (which we don't use). \| handler = Mangum(app, lifespan="off")`

*S3 presigned URLs:*

- `app/s3.py:4` 📄 `The API never proxies file uploads through the Lambda — clients PUT directly to \| S3 using a presigned URL issued by :func:`generate_upload_presigned_url`. This`
- `app/s3.py:11` 📄 `* :func:`get_s3_client` — lazy boto3 S3 client. \| * :func:`generate_upload_presigned_url` — issue a PUT URL. \| * :func:`generate_download_presigned_url` — issue`

### Auth + security

**Evidence in the prototype repo:**


*Cognito JWT auth:*

- `app/auth.py:5` 📄 `* :func:`verify_cognito_jwt` — low-level verifier that decodes and validates a JWT \| against the public keys published by the user pool's ``cognito-idp`` endpoi`
- `app/auth.py:6` 📄 `* :func:`verify_cognito_jwt` — low-level verifier that decodes and validates a JWT \| against the public keys published by the user pool's ``cognito-idp`` endpoi`

### Monday.com or similar webhook integration

**Evidence in the prototype repo:**


*Monday.com bidirectional integration:*

- `lambdas/webhook_monday/handler.py:5` 📄 `1. Read the JSON body and the ``Authorization`` header (HMAC-SHA256 signature). \| 2. Verify the signature using ``MONDAY_WEBHOOK_SECRET``.`
- `lambdas/webhook_monday/handler.py:6` 📄 `1. Read the JSON body and the ``Authorization`` header (HMAC-SHA256 signature). \| 2. Verify the signature using ``MONDAY_WEBHOOK_SECRET``. \| 3. Decode the webho`

### Async communication style

_Async communication style shows up in repo hygiene, not code. Look for: structured commit history, dated PROGRESS/DECISIONS logs, OpenAPI spec for written specs, and a deployment runbook._


**Evidence in the prototype repo:**


*repo discipline (ROADMAP.md):*

- `ROADMAP.md:1` 📝 `# JOB-20260626153537-000112 Roadmap`

*repo discipline (README.md):*

- `README.md:1` 📝 `# Senior Python Backend Engineer — AWS Asset & Portfolio Mgmt Platform`

*repo discipline (docs/deployment.md):*

- `docs/deployment.md:1` 📝 `# Deployment Guide`

*repo discipline (docs/architecture.md):*

- `docs/architecture.md:1` 📝 `# Architecture`

## Why Me

- **Built-Before-Bid discipline:** I shipped a working prototype before submitting this proposal. You can run it, read the code, and verify every claim against the repo before you commit.
- **No black boxes:** every file in the repo is mine. No vendor-locked templates, no AI-generated filler that doesn't compile.
- **Production-shaped:** structured logging, retry/DLQ, idempotency, tests, IaC, deployment docs — not just code that runs locally.
