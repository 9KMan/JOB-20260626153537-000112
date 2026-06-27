"""AWS CDK application — provisions the entire data-platform stack.

Resources created (see ``docs/deployment.md`` for the full table):

* S3 buckets (uploads + artifacts) with versioning, encryption, lifecycle.
* DynamoDB single-table + 3 GSIs.
* Cognito user pool + app client + API authorizer.
* API Gateway (REST) with Cognito auth on every non-health route.
* Lambdas: ``api``, ``etl_parser``, ``webhook_monday`` (Lambda layer for deps).
* EventBridge rule for S3 → ETL Lambda.
* SQS DLQs for both async Lambdas.
* IAM roles (least-privilege) per Lambda.
* CloudWatch log groups + alarms.
* Secrets Manager secrets for Monday.com credentials.

Run ``cdk synth`` / ``cdk deploy`` from the directory containing this file.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import aws_cdk as cdk
from aws_cdk import (
    App,
    Duration,
    RemovalPolicy,
    Stack,
    StackProps,
)
from aws_cdk import aws_apigateway as apigw
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_lambda_event_sources as event_sources
from aws_cdk import aws_logs as logs
from aws_cdk import aws_secretsmanager as secrets
from aws_cdk import aws_sqs as sqs

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROJECT_NAME = "serverless-data-platform"
DEFAULT_REGION = "us-east-1"

LAMBDA_RUNTIME = lambda_.Runtime.PYTHON_3_11
LAMBDA_MEMORY_MB = 512
LAMBDA_TIMEOUT_SECONDS = 60
ETL_LAMBDA_MEMORY_MB = 1024
ETL_LAMBDA_TIMEOUT_SECONDS = 300


class DataPlatformStack(Stack):
    """The single CDK stack that provisions the entire platform."""

    def __init__(
        self,
        scope: App,
        construct_id: str,
        *,
        env: Optional[cdk.Environment] = None,
        **kwargs: StackProps,
    ) -> None:
        super().__init__(scope, construct_id, env=env, **kwargs)

        # ------------------------------------------------------------------
        # S3 buckets
        # ------------------------------------------------------------------

        self.uploads_bucket = self._create_bucket(
            bucket_id="UploadsBucket",
            purpose="raw uploads",
        )
        self.artifacts_bucket = self._create_bucket(
            bucket_id="ArtifactsBucket",
            purpose="processed artifacts",
        )

        # ------------------------------------------------------------------
        # DynamoDB
        # ------------------------------------------------------------------

        self.table = dynamodb.Table(
            self,
            "CoreTable",
            table_name=f"{PROJECT_NAME}-core",
            partition_key=dynamodb.Attribute(
                name="PK", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="SK", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
            point_in_time_recovery=True,
            removal_policy=RemovalPolicy.RETAIN,
        )
        # GSI1 — status/timeline
        self.table.add_global_secondary_index(
            index_name="GSI1",
            partition_key=dynamodb.Attribute(name="GSI1PK", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="GSI1SK", type=dynamodb.AttributeType.STRING),
            projection_type=dynamodb.ProjectionType.ALL,
        )
        # GSI2 — report lookup
        self.table.add_global_secondary_index(
            index_name="GSI2",
            partition_key=dynamodb.Attribute(name="GSI2PK", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="GSI2SK", type=dynamodb.AttributeType.STRING),
            projection_type=dynamodb.ProjectionType.ALL,
        )
        # GSI3 — idempotency (keys-only to keep storage costs minimal)
        self.table.add_global_secondary_index(
            index_name="GSI3",
            partition_key=dynamodb.Attribute(name="GSI3PK", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="GSI3SK", type=dynamodb.AttributeType.STRING),
            projection_type=dynamodb.ProjectionType.KEYS_ONLY,
        )

        # ------------------------------------------------------------------
        # Cognito
        # ------------------------------------------------------------------

        self.user_pool = cognito.UserPool(
            self,
            "UserPool",
            user_pool_name=f"{PROJECT_NAME}-users",
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(email=True, username=False),
            password_policy=cognito.PasswordPolicy(
                min_length=12,
                require_lowercase=True,
                require_uppercase=True,
                require_digits=True,
                require_symbols=True,
                temp_password_validity=Duration.days(3),
            ),
            mfa=cognito.Mfa.OPTIONAL,
            mfa_second_factor=cognito.MfaSecondFactor(sms=True, otp=True),
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            removal_policy=RemovalPolicy.RETAIN,
        )

        self.user_pool_client = self.user_pool.add_client(
            "AppClient",
            generate_secret=False,
            auth_flows=cognito.AuthFlow(user_srp=True, user_password=True),
            prevent_user_existence_errors=True,
        )

        # ------------------------------------------------------------------
        # Lambda layer for shared dependencies
        # ------------------------------------------------------------------

        layer_code_path = (
            Path(__file__).resolve().parent.parent / "lambdas" / "layer_requirements.txt"
        )
        self.dependencies_layer = lambda_.LayerVersion(
            self,
            "DependenciesLayer",
            code=lambda_.Code.from_asset(
                str(Path(__file__).resolve().parent.parent / "lambdas"),
                bundling=cdk.BundlingOptions(
                    image=lambda_.Runtime.PYTHON_3_11.bundling_image,
                    command=[
                        "bash",
                        "-c",
                        "pip install -r layer_requirements.txt -t /asset-output/python",
                    ],
                ),
            ),
            compatible_runtimes=[LAMBDA_RUNTIME],
            description="Shared Python dependencies for the data platform Lambdas",
        )

        # ------------------------------------------------------------------
        # Common Lambda environment
        # ------------------------------------------------------------------

        common_env = {
            "DYNAMODB_TABLE_NAME": self.table.table_name,
            "S3_UPLOADS_BUCKET": self.uploads_bucket.bucket_name,
            "S3_ARTIFACTS_BUCKET": self.artifacts_bucket.bucket_name,
            "COGNITO_USER_POOL_ID": self.user_pool.user_pool_id,
            "COGNITO_APP_CLIENT_ID": self.user_pool_client.user_pool_client_id,
            "COGNITO_REGION": self.region,
            "AWS_REGION": self.region,
            "LOG_LEVEL": "INFO",
        }

        # ------------------------------------------------------------------
        # DLQs
        # ------------------------------------------------------------------

        self.etl_dlq = sqs.Queue(
            self,
            "EtlDLQ",
            queue_name=f"{PROJECT_NAME}-etl-dlq",
            retention_period=Duration.days(14),
            encryption=sqs.QueueEncryption.SQS_MANAGED,
        )
        self.webhook_dlq = sqs.Queue(
            self,
            "WebhookDLQ",
            queue_name=f"{PROJECT_NAME}-webhook-dlq",
            retention_period=Duration.days(14),
            encryption=sqs.QueueEncryption.SQS_MANAGED,
        )

        # ------------------------------------------------------------------
        # Lambdas
        # ------------------------------------------------------------------

        log_group_retention = logs.RetentionDays.ONE_MONTH

        # --- ETL parser ---
        self.etl_lambda = lambda_.Function(
            self,
            "EtlParserFn",
            function_name=f"{PROJECT_NAME}-etl-parser",
            runtime=LAMBDA_RUNTIME,
            handler="lambdas.etl_parser.handler.handler",
            code=lambda_.Code.from_asset(
                str(Path(__file__).resolve().parent.parent)
            ),
            memory_size=ETL_LAMBDA_MEMORY_MB,
            timeout=Duration.seconds(ETL_LAMBDA_TIMEOUT_SECONDS),
            environment={
                **common_env,
                "ALLOWED_UPLOAD_BUCKETS": self.uploads_bucket.bucket_name,
                "ETL_MAX_RETRIES": "3",
                "ETL_BACKOFF_BASE_SECONDS": "0.5",
            },
            layers=[self.dependencies_layer],
            log_retention=log_group_retention,
            dead_letter_queue=self.etl_dlq,
            reserved_concurrent_executions=10,
        )
        self.uploads_bucket.grant_read(self.etl_lambda)
        self.table.grant_read_write_data(self.etl_lambda)

        # --- API Lambda ---
        self.api_lambda = lambda_.Function(
            self,
            "ApiFn",
            function_name=f"{PROJECT_NAME}-api",
            runtime=LAMBDA_RUNTIME,
            handler="app.main.handler",
            code=lambda_.Code.from_asset(
                str(Path(__file__).resolve().parent.parent)
            ),
            memory_size=LAMBDA_MEMORY_MB,
            timeout=Duration.seconds(LAMBDA_TIMEOUT_SECONDS),
            environment=common_env,
            layers=[self.dependencies_layer],
            log_retention=log_group_retention,
        )
        self.table.grant_read_write_data(self.api_lambda)
        self.uploads_bucket.grant_read_write(self.api_lambda)
        self.artifacts_bucket.grant_read_write(self.api_lambda)

        # --- Monday webhook Lambda ---
        monday_token_secret = secrets.Secret(
            self,
            "MondayApiToken",
            secret_name=f"{PROJECT_NAME}/monday/api-token",
            description="Monday.com API token (value set out-of-band).",
            generate_secret_string=secrets.SecretStringGenerator(
                exclude_punctuation=True,
                password_length=40,
            ),
        )
        monday_webhook_secret = secrets.Secret(
            self,
            "MondayWebhookSecret",
            secret_name=f"{PROJECT_NAME}/monday/webhook-secret",
            description="HMAC secret used to verify Monday webhook signatures.",
            generate_secret_string=secrets.SecretStringGenerator(
                password_length=64,
            ),
        )
        self.webhook_lambda = lambda_.Function(
            self,
            "MondayWebhookFn",
            function_name=f"{PROJECT_NAME}-webhook-monday",
            runtime=LAMBDA_RUNTIME,
            handler="lambdas.webhook_monday.handler.handler",
            code=lambda_.Code.from_asset(
                str(Path(__file__).resolve().parent.parent)
            ),
            memory_size=LAMBDA_MEMORY_MB,
            timeout=Duration.seconds(LAMBDA_TIMEOUT_SECONDS),
            environment={
                **common_env,
                "MONDAY_WEBHOOK_SECRET": monday_webhook_secret.secret_value.unsafe_unwrap(),
            },
            layers=[self.dependencies_layer],
            log_retention=log_group_retention,
            dead_letter_queue=self.webhook_dlq,
        )
        monday_token_secret.grant_read(self.webhook_lambda)
        self.table.grant_read_write_data(self.webhook_lambda)

        # ------------------------------------------------------------------
        # EventBridge: S3 → ETL
        # ------------------------------------------------------------------

        self.event_bus = events.EventBus(
            self,
            "PlatformBus",
            event_bus_name=f"{PROJECT_NAME}-bus",
        )
        events.Rule(
            self,
            "S3ObjectCreatedRule",
            event_pattern=events.EventPattern(
                source=["aws.s3"],
                detail_type=["Object Created"],
                detail={
                    "bucket": {"name": [self.uploads_bucket.bucket_name]},
                },
            ),
            targets=[
                targets.LambdaFunction(
                    self.etl_lambda,
                    dead_letter_queue=self.etl_dlq,
                    retry_attempts=2,
                )
            ],
        )

        # ------------------------------------------------------------------
        # API Gateway
        # ------------------------------------------------------------------

        self.api = apigw.RestApi(
            self,
            "PlatformApi",
            rest_api_name=f"{PROJECT_NAME}-api",
            description="HTTPS front door for the serverless data platform.",
            deploy_options=apigw.StageOptions(
                stage_name="prod",
                logging_level=apigw.MethodLoggingLevel.INFO,
                tracing_enabled=True,
                metrics_enabled=True,
            ),
            default_cors_preflight_options=apigw.CorsOptions(
                allow_origins=apigw.Cors.ALL_ORIGINS,
                allow_methods=apigw.Cors.ALL_METHODS,
            ),
        )

        cognito_authorizer = apigw.CognitoUserPoolsAuthorizer(
            self,
            "CognitoAuthorizer",
            cognito_user_pools=[self.user_pool],
            identity_source="method.request.header.Authorization",
        )

        # /health → unauthenticated
        health_resource = self.api.root.add_resource("health")
        health_resource.add_method(
            "GET",
            apigw.LambdaIntegration(self.api_lambda, proxy=True),
            authorization_type=apigw.AuthorizationType.NONE,
        )

        # /items, /uploads → authenticated
        items_resource = self.api.root.add_resource("items")
        items_resource.add_method(
            "ANY",
            apigw.LambdaIntegration(self.api_lambda, proxy=True),
            authorizer=cognito_authorizer,
            authorization_type=apigw.AuthorizationType.COGNITO,
        )
        items_resource.add_proxy(
            any_method=True,
            default_integration=apigw.LambdaIntegration(self.api_lambda, proxy=True),
            default_method_options=apigw.MethodOptions(
                authorizer=cognito_authorizer,
                authorization_type=apigw.AuthorizationType.COGNITO,
            ),
        )

        uploads_resource = self.api.root.add_resource("uploads")
        uploads_resource.add_proxy(
            any_method=True,
            default_integration=apigw.LambdaIntegration(self.api_lambda, proxy=True),
            default_method_options=apigw.MethodOptions(
                authorizer=cognito_authorizer,
                authorization_type=apigw.AuthorizationType.COGNITO,
            ),
        )

        # /webhooks/monday → authenticated by HMAC, not Cognito
        webhook_resource = self.api.root.add_resource("webhooks").add_resource("monday")
        webhook_resource.add_method(
            "POST",
            apigw.LambdaIntegration(self.webhook_lambda, proxy=True),
            authorization_type=apigw.AuthorizationType.NONE,
        )

        # ------------------------------------------------------------------
        # Alarms
        # ------------------------------------------------------------------

        self._add_dlq_alarm("EtlDLQAlarm", self.etl_dlq)
        self._add_dlq_alarm("WebhookDLQAlarm", self.webhook_dlq)

        cw.Alarm(
            self,
            "Api5xxAlarm",
            metric=self.api.metric_server_error(statistic="Sum", period=Duration.minutes(5)),
            threshold=5,
            evaluation_periods=2,
            datapoints_to_alarm=2,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            alarm_description="API Gateway 5xx rate exceeded threshold.",
        )

        # ------------------------------------------------------------------
        # Outputs
        # ------------------------------------------------------------------

        cdk.CfnOutput(self, "ApiUrl", value=self.api.url)
        cdk.CfnOutput(self, "UploadsBucketName", value=self.uploads_bucket.bucket_name)
        cdk.CfnOutput(self, "ArtifactsBucketName", value=self.artifacts_bucket.bucket_name)
        cdk.CfnOutput(self, "DataTableName", value=self.table.table_name)
        cdk.CfnOutput(self, "UserPoolId", value=self.user_pool.user_pool_id)
        cdk.CfnOutput(self, "UserPoolClientId", value=self.user_pool_client.user_pool_client_id)

    # ----------------------------------------------------------------------
    # Helpers
    # ----------------------------------------------------------------------

    def _create_bucket(self, *, bucket_id: str, purpose: str):
        bucket = cdk.aws_s3.Bucket(
            self,
            bucket_id,
            bucket_name=f"{PROJECT_NAME}-{bucket_id.lower()}",
            versioned=True,
            encryption=cdk.aws_s3.BucketEncryption.S3_MANAGED,
            block_public_access=cdk.aws_s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.RETAIN,
            lifecycle_rules=[
                cdk.aws_s3.LifecycleRule(
                    id=f"{bucket_id}-archive",
                    transitions=[
                        cdk.aws_s3.Transition(
                            storage_class=cdk.aws_s3.StorageClass.GLACIER,
                            transition_after=Duration.days(90),
                        ),
                    ],
                    expiration=Duration.days(2555),  # 7 years
                ),
            ],
        )
        cdk.Tags.of(bucket).add("Purpose", purpose)
        return bucket

    def _add_dlq_alarm(self, alarm_id: str, queue: sqs.Queue) -> None:
        cw.Alarm(
            self,
            alarm_id,
            metric=queue.metric_approximate_number_of_messages_visible(
                period=Duration.minutes(5),
                statistic="Maximum",
            ),
            threshold=1,
            evaluation_periods=1,
            datapoints_to_alarm=1,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            alarm_description=f"DLQ depth > 0 for {queue.queue_name}",
        )


# ---------------------------------------------------------------------------
# App entry point
# ---------------------------------------------------------------------------


def main() -> None:
    app = App()
    account = os.environ.get("AWS_ACCOUNT_ID") or os.environ.get("CDK_DEFAULT_ACCOUNT")
    region = os.environ.get("AWS_REGION") or os.environ.get("CDK_DEFAULT_REGION") or DEFAULT_REGION
    DataPlatformStack(
        app,
        f"{PROJECT_NAME}-stack",
        env=cdk.Environment(account=account, region=region),
        description="Serverless Data Platform reference stack.",
    )
    app.synth()


if __name__ == "__main__":
    main()