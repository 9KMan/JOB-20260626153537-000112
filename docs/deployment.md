# Deployment Guide

This guide walks through provisioning the platform on AWS using the CDK application in
`infra/cdk_app.py`.

---

## Prerequisites

| Tool | Version | Notes |
|------|---------|-------|
| AWS CLI | ≥ 2.15 | configured with `aws configure` |
| Node.js | ≥ 18 | required by CDK CLI |
| AWS CDK | ≥ 2.140 | `npm install -g aws-cdk` |
| Python | 3.11 / 3.12 | match Lambda runtime |
| Poetry | ≥ 1.7 | or use plain `pip` against `lambdas/layer_requirements.txt` |

---

## One-Time Setup

```bash
# 1. Clone / copy this template into a project directory.
cp -r aws-serverless-data-platform/ ~/projects/my-data-platform
cd ~/projects/my-data-platform

# 2. Create a virtualenv and install dependencies.
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r lambdas/layer_requirements.txt
pip install aws-cdk-lib

# 3. Configure environment for CDK synthesis.
export AWS_REGION=us-east-1
export AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

# 4. Bootstrap CDK in the target account/region (idempotent).
cd infra
cdk bootstrap aws://$AWS_ACCOUNT_ID/$AWS_REGION
```

---

## Synthesize

```bash
cdk synth
```

This produces a CloudFormation template in `cdk.out/`. If synthesis fails, the most
common causes are:

- Missing IAM permissions for `cdk deploy` (CDK needs to create roles, buckets, etc.).
- Region mismatch between CLI config and `AWS_REGION`.
- A stale `cdk.context.json` after upgrading CDK (delete it and re-synth).

---

## Deploy

```bash
cdk deploy
```

CDK prompts for approval before applying the changeset. In CI:

```bash
cdk deploy --require-approval never --outputs-file cdk-outputs.json
```

Deployment typically takes 3–6 minutes. On completion, `cdk-outputs.json` contains:

| Output key | Meaning |
|------------|---------|
| `ApiUrl` | HTTPS endpoint of the API Gateway stage |
| `UploadsBucketName` | S3 bucket for raw uploads |
| `ArtifactsBucketName` | S3 bucket for processed artifacts |
| `DataTableName` | DynamoDB table name |
| `UserPoolId` | Cognito user pool ID |
| `UserPoolClientId` | App client ID for JWT acquisition |

---

## AWS Resource Inventory

The CDK application provisions (or references) the following resources:

| Resource | CDK construct | Purpose |
|----------|---------------|---------|
| VPC (optional) | `aws_ec2.Vpc` | not created by default; opt-in for PII workloads |
| S3 bucket — uploads | `aws_s3.Bucket` | raw uploaded files |
| S3 bucket — artifacts | `aws_s3.Bucket` | derived artifacts |
| DynamoDB table | `aws_dynamodb.Table` | single-table core |
| DynamoDB GSIs | same table | GSI1, GSI2, GSI3 |
| Lambda — `api` | `aws_lambda.Function` | FastAPI/Mangum handler |
| Lambda — `etl_parser` | `aws_lambda.Function` | S3-triggered ETL |
| Lambda — `webhook_monday` | `aws_lambda.Function` | Monday.com inbound |
| Lambda layer — `python_deps` | `aws_lambda.LayerVersion` | third-party packages |
| API Gateway (REST) | `aws_apigateway.RestApi` | HTTPS front door |
| Cognito user pool | `aws_cognito.UserPool` | JWT issuer |
| Cognito user pool client | `aws_cognito.UserPoolClient` | app client |
| Cognito authorizer | `aws_apigateway.CognitoUserPoolsAuthorizer` | API auth |
| SQS — `etl_dlq` | `aws_sqs.Queue` | ETL failure capture |
| SQS — `webhook_dlq` | `aws_sqs.Queue` | webhook failure capture |
| EventBridge bus | `aws_events.EventBus` | internal events |
| EventBridge rule — S3→ETL | `aws_events.Rule` | triggers ETL Lambda |
| IAM roles (per Lambda) | `aws_iam.Role` | least-privilege |
| CloudWatch log groups | `aws_logs.LogGroup` | per Lambda |
| CloudWatch alarms | `aws_cloudwatch.Alarm` | DLQ depth, API 5xx |
| Secrets Manager — `monday/token` | `aws_secretsmanager.Secret` | Monday API token |
| Secrets Manager — `monday/webhook_secret` | `aws_secretsmanager.Secret` | HMAC secret |

---

## Stack Outputs & Local Wiring

After `cdk deploy`, populate your local `.env` with the values from
`cdk-outputs.json`:

```bash
export API_BASE_URL=$(jq -r '.ApiUrl' cdk-outputs.json)
export DYNAMODB_TABLE_NAME=$(jq -r '.DataTableName' cdk-outputs.json)
export S3_UPLOADS_BUCKET=$(jq -r '.UploadsBucketName' cdk-outputs.json)
export COGNITO_USER_POOL_ID=$(jq -r '.UserPoolId' cdk-outputs.json)
export COGNITO_APP_CLIENT_ID=$(jq -r '.UserPoolClientId' cdk-outputs.json)
```

---

## Running the API Locally

```bash
uvicorn app.main:app --reload --port 8000
```

Local requests still hit real AWS unless `DYNAMODB_ENDPOINT_URL` is set to a
LocalStack / DynamoDB Local endpoint. Tests use `moto` so they need no AWS access.

---

## CI/CD Recommendations

A typical pipeline (GitHub Actions example) looks like:

```yaml
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.12' }
      - run: pip install -r lambdas/layer_requirements.txt
      - run: pip install pytest moto[s3,dynamodb,sqs] aws-cdk-lib
      - run: pytest -v

  deploy:
    needs: test
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ secrets.AWS_DEPLOY_ROLE }}
          aws-region: us-east-1
      - run: npm install -g aws-cdk
      - run: |
          cd infra
          cdk synth
          cdk deploy --require-approval never --outputs-file cdk-outputs.json
      - uses: actions/upload-artifact@v4
        with: { name: cdk-outputs, path: infra/cdk-outputs.json }
```

---

## Teardown

To remove all resources (irreversible):

```bash
cd infra
cdk destroy
```

Empty S3 buckets and disable termination protection before running this if you've
enabled those safeguards.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `cdk synth` fails on `aws_lambda` | CDK version mismatch | upgrade to ≥ 2.140 |
| API returns `401` with a valid JWT | Authorizer cache stale | wait 5 min or restart stage |
| ETL Lambda times out on large PDFs | Lambda memory too low | bump to 1024 MB in CDK |
| DynamoDB throttling on burst | On-demand quota | request quota increase |
| `moto` tests fail on AWS-shaped fixture | boto3 version drift | pin boto3 in layer requirements |