# Amazon Bedrock Ops Alert: Terraform Registry-Driven Deployment Guide

This guide explains how to deploy Amazon Bedrock Ops Alert from a DynamoDB registry with Terraform. It covers the row format, prerequisites, packaging, deployment, testing, and cleanup.

Rather than keeping a `.tfvars` file and a workspace per model, you define each deployment as a row in a DynamoDB table. Terraform reads the rows and deploys one workload per row. Each row represents one monitored model, so you add a model by adding a row.

This is the Terraform equivalent of the CloudFormation registry in [DEPLOYMENT.md](DEPLOYMENT.md). Both read the same registry table and row format, and produce identical resources.

## Choose your deployment mode

The registry supports two modes. Both use the same row format, the same validation, and the same status model; they differ only in where they run and how Terraform reaches the target account.

| Mode | Use it when | How Terraform reaches the account | Guide |
|------|-------------|-----------------------------------|-------|
| Single-account | You deploy into the account you are signed in to | Local credentials | [Method 1](#method-1-single-account-deployment) |
| Organization | You deploy into member accounts of an AWS Organization | The provider assumes a role in the target account (Terraform has no StackSets) | [Method 2](#method-2-organization-deployment) |

Read [How it works](#how-it-works) and the [Registry reference](#registry-reference) first, then follow the method that fits.

## How it works

Terraform reads the registry through an [external data source](https://registry.terraform.io/providers/hashicorp/external/latest/docs/data-sources/external) that runs `terraform/registry_rows.py`. That script reuses `code/registry/registry_core.py` to scan the table, select the deployable rows for this run's account and Region, validate them, and return them as JSON. Terraform then instantiates the workload module (`terraform/modules/bedrock-ops-alert`) once per row with `for_each`.

```
   terraform apply
        │
        ├─ data.external.registry ─► registry_rows.py ─► DynamoDB registry (reuses registry_core)
        │                                                 returns the deployable rows as JSON
        └─ for_each row ─► module.deployment[row] ─► KMS, Secrets, SNS, Lambda, alarms, ...
```

The registry is the desired state, and Terraform state is the record of what exists:

- A row with an empty (or `REQUESTED`) `Status` is deployed.
- A row set to `PENDING_DELETE`, or removed from the table, is destroyed on the next `apply`.
- `terraform plan` shows the difference; `terraform output` lists what is deployed. The driver does not write status back to DynamoDB.

Two properties follow from Terraform's model:

- Each `terraform apply` deploys to one account and one Region. A single run still covers every model for that account and Region, because `for_each` loops over all the matching rows. You run `apply` again only to reach a different Region or a different account (Method 2). For example: 3 models in account A / us-east-1 is one run; adding us-west-2 makes two runs; adding account B makes three. On each run, `registry_rows.py` returns only the rows for that run's account and Region.
- Use a Terraform workspace per account and Region combination to keep the state files separate.

## Registry reference

Each row is one deployment. Two attributes form the key, `Status` drives the action, and the rest map to the workload module's input variables. The row format is identical to the CloudFormation registry, so the same table serves both.

**Leaving a column blank omits the corresponding module input, so the module variable's default applies.** The defaults for the following columns are validated for production and recommended for most deployments: SupportCaseLookbackDays, TokensPerMinuteIncreasePercent, RequestsPerMinuteIncreasePercent, ErrorThreshold, CriticalAlarmEvaluationPeriods, RequestsPerMinuteThresholdPercent, TokensPerMinuteThresholdPercent, LatencyThresholdMs, WarningAlarmEvaluationPeriods, LatencyAlarmPeriod, LatencyAlarmEvaluationPeriods, AnomalyDetectionPeriod, AnomalyEvaluationPeriods, AnomalySensitivity, AlarmEvaluationPeriod, and ThresholdUpdateScheduleIntervalDays. Adjust them only if your workload needs it.

Terraform treats a few columns differently from the CloudFormation registry:

| Column | Terraform behavior |
|--------|--------------------|
| `Tags` | Ignored. The provider applies `Solution=Amazon-Bedrock-Ops-Alert` through `default_tags` |
| `OrganizationalUnitId` | Ignored. That is a CloudFormation StackSets concept; Terraform targets an account with `assume_role` |
| `StatusReason`, `StackId`, `CreatedAt`, `UpdatedAt` | Not written. Terraform state is the record |

The **Modify** column tells you when each value needs your attention:

| Modify | Meaning |
|--------|---------|
| Required | Must be set. `terraform plan` fails on a blank value |
| Per-model(Required) / Per-model(Optional) | Review this every time you monitor a different model. Required rejects a blank value; Optional falls back to the default |
| Optional | Leave blank to accept the validated default. Tune only if your workload needs it |
| Derived | Parsed from `DeploymentTarget`. Not a column, never entered |
| Ignored | Read by the CloudFormation registry, not by Terraform |

**Registry Key and Deployment State**

| Column | Modify | Description | Default |
|--------|--------|-------------|---------|
| AccountId | Required | Partition key, a 12-digit AWS account ID. Single-account: the account you are signed in to. Organization: the target member account the workload is deployed into. `registry_rows.py` returns only rows matching this account |  |
| DeploymentTarget | Required | Sort key, formatted `<Region>#<CustomerName>#<BedrockModelName>`, for example `us-east-1#AcmeCorp#G-Opus-4-6`. CustomerName (1-10 chars) and BedrockModelName (1-15 chars) are short, meaningful labels you choose for resource naming, not the full model ID; both allow letters, numbers, and hyphens. The script parses all three and passes them to the module; only rows whose Region matches this run's `aws_region` are deployed |  |
| OrganizationalUnitId | Ignored | A CloudFormation StackSets concept. Terraform targets an account with `assume_role`, so this column is not used |  |
| Status | Required | Empty or REQUESTED to deploy, PENDING_DELETE (or removing the row) to destroy. See [Deployment status reference](#deployment-status-reference) |  |

**General Configuration**

| Column | Modify | Description | Default |
|--------|--------|-------------|---------|
| CustomerName | Derived | Derived from `DeploymentTarget`, not a column. Your company or team identifier for resource naming (max 10 characters). Used in alarm names, Parameter Store paths, and Lambda function names |  |
| StakeholderEmailList | Required | Comma-separated list of stakeholder emails for alerts, that is, the people notified when alarms trigger (your SRE team) |  |
| NotificationPreference | Optional | Email notification filter: all, critical, or warning. Updatable via Parameter Store post-deployment | all |
| Tags | Ignored | Terraform does not apply this column; the provider applies `Solution=Amazon-Bedrock-Ops-Alert` through `default_tags` |  |

**Lambda Deployment Configuration**

| Column | Modify | Description | Default |
|--------|--------|-------------|---------|
| LambdaS3Bucket | Required | S3 bucket holding the four Lambda deployment packages. Must be in the same Region as the deployment. For the organization method this is the hub artifact bucket for that Region, readable by the target account |  |

**Model Specification**

| Column | Modify | Description | Default |
|--------|--------|-------------|---------|
| BedrockModelName | Derived | Derived from `DeploymentTarget`, not a column. Short model name for resource naming (max 15 characters) |  |
| InferenceProfileType | Per-model(Required) | Inference profile type: System-Defined (direct model or cross-Region profile) or Application (customer-created profile for cost tracking) | System-Defined |
| BedrockModelId | Per-model(Required) | For System-Defined: the model ID. For Application: the short profile ID (for example, e5t98lwp1dsr). Find profiles in the [Amazon Bedrock console](https://console.aws.amazon.com/bedrock/home#/inference-profiles) | global.anthropic.claude-opus-4-6-v1 |
| GeoDataResidencyRequirement | Optional | Geographic data residency requirement (Yes/No/NA). If Yes, Global Cross Region Inference cannot be considered | No |
| InputModalities | Per-model(Optional) | Input modalities used by the model (TEXT and IMAGE, TEXT Only, IMAGE Only) | TEXT and IMAGE |

**Model Quota Configuration**

| Column | Modify | Description | Default |
|--------|--------|-------------|---------|
| RequestsPerMinuteQuotaCode | Per-model(Required) | Set to NA if the model has no RPM quota; otherwise the RPM quota code, required when RequestsPerMinuteIncreasePercent > 0 | L-3DD46812 |
| RequestsPerMinuteIncreasePercent | Optional | RPM quota increase % (0, 25, 50, 75, 100). Set 0 to skip or when RequestsPerMinuteQuotaCode is NA. Updatable via Parameter Store | 25 |
| TokensPerMinuteQuotaCode | Per-model(Required) | TPM quota code. Required if TokensPerMinuteIncreasePercent > 0 | L-3DCCFAA4 |
| TokensPerMinuteIncreasePercent | Optional | TPM quota increase % (0, 25, 50, 75, 100). Set 0 to skip. Updatable via Parameter Store | 25 |

**Model Usage Budget (Threshold)**

| Column | Modify | Description | Default |
|--------|--------|-------------|---------|
| RequestsPerMinuteThresholdPercent | Optional | RPM threshold as percentage of quota (for example, 80). Breaching it triggers an automated RPM quota increase support case, if EnableAutomatedSupportCase is Yes. Ignored when RequestsPerMinuteQuotaCode is NA | 80 |
| TokensPerMinuteThresholdPercent | Optional | TPM threshold as percentage of quota (for example, 80). Breaching it triggers an automated TPM quota increase support case, if EnableAutomatedSupportCase is Yes | 80 |
| LatencyThresholdMs | Per-model(Optional) | Latency threshold in milliseconds (Min: 15000) | 240000 |

**Automated Support Case Configuration**

| Column | Modify | Description | Default |
|--------|--------|-------------|---------|
| EnableAutomatedSupportCase | Optional | Enable automated support case creation (Yes/No). Updatable via Parameter Store post-deployment | Yes |
| SupportCaseLookbackDays | Optional | Days to look back when checking for existing unresolved support cases (duplicate detection). Allowed: 45, 60, 90 | 60 |
| UseCaseDescription | Required | Brief description of the use case for quota increase justification. Included in automated support cases. Updatable via Parameter Store | Enterprise production workload serving real-time AI-powered features to end users. |

**Automated Threshold Update Configuration**

| Column | Modify | Description | Default |
|--------|--------|-------------|---------|
| EnableAutomatedThresholdUpdate | Optional | Enable automated threshold updates based on quota changes (Yes/No) | Yes |
| ThresholdUpdateScheduleIntervalDays | Optional | How often to check and update alarm thresholds, in days (1-30). Recommended: 1 for daily | 1 |

**Layer 1: Critical Error Detection Alarm Configuration**

| Column | Modify | Description | Default |
|--------|--------|-------------|---------|
| ErrorThreshold | Optional | Error count threshold per evaluation period. 0 = immediate alert on any error. Example: >5 errors/min (say 6) × 5 evaluation periods = 30+ errors in 5 minutes triggers the critical alarm | 5 |
| CriticalAlarmEvaluationPeriods | Optional | Consecutive periods before critical alarm. 5 with 60s period = sustained errors for 5 minutes | 5 |

**Layer 2: Usage Rate Monitoring Alarm Configuration**

| Column | Modify | Description | Default |
|--------|--------|-------------|---------|
| WarningAlarmEvaluationPeriods | Optional | Consecutive periods for warning alarm (Min: 1). Example: usage exceeding threshold for 5 consecutive minutes triggers the warning alarm | 5 |
| LatencyAlarmPeriod | Optional | Latency check period in seconds (300 = 5 min) | 300 |
| LatencyAlarmEvaluationPeriods | Optional | Consecutive latency checks before alert (Min: 1). Example: sustained high latency for 10 minutes (2 × 300s) triggers the alarm | 2 |

**Layer 3: Anomaly Detection Alarm Configuration**

| Column | Modify | Description | Default |
|--------|--------|-------------|---------|
| AnomalyDetectionPeriod | Optional | Anomaly check period in seconds (900 = 15 min) | 900 |
| AnomalyEvaluationPeriods | Optional | Consecutive anomaly periods before alert (Min: 1). Example: 3 hours of sustained anomaly (12 × 900s) triggers the alarm | 12 |
| AnomalySensitivity | Optional | Anomaly sensitivity. 1 = most sensitive, 10 = least sensitive | 9 |

**Common Alarm Configuration**

| Column | Modify | Description | Default |
|--------|--------|-------------|---------|
| AlarmEvaluationPeriod | Optional | Alarm check period in seconds (60 = 1 min) | 60 |

**CloudFormation-only columns**

`StatusReason`, `StackId`, `CreatedAt`, and `UpdatedAt` are written by the CloudFormation registry. Terraform does not write them; `terraform plan` and state are the record. They are harmless if present in a shared row.

Validation is preserved: the workload module keeps all 30 variable validations (ranges, allowed values), and `registry_rows.py` adds the target, required-field, and artifact checks. For a single-account run, it also confirms the model is reachable and its actual inference-profile type matches the row, the same check the CloudFormation single-account driver runs; an organization run skips this, because the hub credentials cannot see the target account's Bedrock. If a deployable row fails any of these checks, `terraform plan` stops with a clear error, so an invalid row is caught before any resource is created.

### Deployment status reference

Terraform reads `Status` to decide what to deploy; it does not write status back.

| Status | Meaning |
|--------|---------|
| *(empty)* or REQUESTED | Deploy this row |
| PENDING_DELETE | Destroy this deployment on the next `apply` |
| TEST | Ignored permanently. Used by the sample row |
| DEPLOYED, FAILED, VALIDATION_FAILED, DELETED, DELETE_FAILED | Terminal states the CloudFormation registry writes. Terraform never sets them, and skips any row that carries one, so a row shared between both registries is not redeployed |

## Prerequisites

Before deploying the solution, confirm you have the following.

**AWS account requirements:**

- Active Amazon Bedrock usage with established quotas for your target model
- AWS Business or Enterprise Support plan (required for automated support case creation through the Support API)
- IAM permissions to create the resources in the workload module, plus read access to the registry table
- Service Quotas service-linked role (required for the deterministic quota increase path). If your account has never requested a quota increase, create it with: `aws iam create-service-linked-role --aws-service-name servicequotas.amazonaws.com`. Without this role, the solution falls back to creating support cases directly — functionality is not impacted.

**Required information:**

- S3 bucket for Lambda deployment packages (per Region for the organization method)
- Stakeholder email addresses for alert notifications
- Service Quota codes for your target Bedrock model (RPM and TPM). Find codes at the [Service Quotas console](https://console.aws.amazon.com/servicequotas/home/services/bedrock/quotas)

**Tools:**

- AWS CLI configured with appropriate credentials
- Zip utility for packaging Lambda functions
- Terraform >= 1.5.0 (`brew install hashicorp/tap/terraform` on macOS)
- Python 3.9 or later with the AWS SDK for Python (boto3). Terraform runs `registry_rows.py` with `python3` from your `PATH`, so this must resolve to a Python that has boto3 installed (activating the virtual environment from Common setup does this).

The organization method has two more prerequisites (a role to assume in each target account, and per-Region artifact buckets shared to the organization), described in [Method 2](#method-2-organization-deployment).

## Common setup

Complete these steps once, for either mode. Run every command block in this guide from the repository root (`sample-amazon-bedrock-ops-alert/`); each block that enters a subdirectory returns to the root at the end, so the next block starts from the right place.

### Clone the repository and create a virtual environment

```bash
git clone https://github.com/aws-samples/sample-amazon-bedrock-ops-alert.git
cd sample-amazon-bedrock-ops-alert

python3 -m venv .venv
source .venv/bin/activate
pip install boto3
```

Keep the virtual environment active while you run Terraform, so the external data source's `python3` has boto3.

### Package the Lambda functions and layer

```bash
# Package the notification processor Lambda
cd code/lambda
zip notification_processor.zip notification_processor.py

# Package the quota calculator Lambda
zip quota_calculator.zip quota_calculator.py

# Package the alarm updater Lambda
zip alarm_updater.zip alarm_updater.py

# Package the quota utils Lambda layer
cd ../quota_utils_layer
zip -r ../quota_utils_layer.zip python/

# Return to project root
cd ../..
```

**Note:** Terraform reads only the four Lambda zips from the bucket. Unlike the CloudFormation registry, it does not need the `bedrock-quota-alarm.yml` template uploaded.

## Method 1: Single-account deployment

> Before you start, complete [Common setup](#common-setup) and review the [Registry reference](#registry-reference) and [Prerequisites](#prerequisites). This method deploys into the account you are signed in to.

### Step 1: Create the S3 bucket and upload the Lambda packages

```bash
BUCKET_NAME=YOUR-BUCKET-NAME
aws s3 mb s3://$BUCKET_NAME --region us-east-1
aws s3api put-bucket-versioning --bucket $BUCKET_NAME --versioning-configuration Status=Enabled

aws s3 cp code/lambda/notification_processor.zip s3://$BUCKET_NAME/
aws s3 cp code/lambda/quota_calculator.zip s3://$BUCKET_NAME/
aws s3 cp code/lambda/alarm_updater.zip s3://$BUCKET_NAME/
aws s3 cp code/quota_utils_layer.zip s3://$BUCKET_NAME/
```

The bucket must be in the same Region you deploy into.

### Step 2: Create the registry table and add rows

If you have not created the registry table yet, apply the bootstrap configuration once. It is a separate Terraform state from the deployment, because the deployment reads this table while planning, so the table must exist first.

```bash
cd terraform/registry-table && terraform init && terraform apply -var="aws_region=us-east-1" && cd ../..
```

`code/registry/sample_row.json` is a complete reference row with `Status` set to `TEST`, which the registry always ignores. Seed it, then copy and edit the copy. Replace `REPLACE_ME_ACCOUNT_ID` with your account and `REPLACE_ME_BUCKET_NAME` with your bucket:

```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

sed -e "s/REPLACE_ME_ACCOUNT_ID/$ACCOUNT_ID/" \
    -e "s/REPLACE_ME_BUCKET_NAME/$BUCKET_NAME/" \
    code/registry/sample_row.json > /tmp/seed_row.json

aws dynamodb put-item --cli-input-json file:///tmp/seed_row.json
```

Then duplicate that row in the [DynamoDB console](https://console.aws.amazon.com/dynamodbv2/home#tables) and edit the copy: set `DeploymentTarget` (`<Region>#<CustomerName>#<BedrockModelName>`), `StakeholderEmailList`, `Status` to an empty string, and the model columns for your model. See the [Registry reference](#registry-reference) for every column. The `Tags` column, if present, is ignored by Terraform.

### Step 3: Deploy with Terraform

Create a workspace for the Region, then apply. Two variables matter: `aws_region` is where the workload deploys, and `registry_table_region` tells Terraform where the registry table lives (from Step 2). Pass both on every run — only `aws_region` changes between Regions; the table's Region stays the same. The workspace keeps this Region's state separate (see the caution below).

```bash
cd terraform
terraform init
terraform workspace new us-east-1        # first time; later runs: terraform workspace select us-east-1
terraform apply -var="aws_region=us-east-1" -var="registry_table_region=us-east-1"
cd ..
```

To avoid retyping `registry_table_region`, put it in a `terraform.tfvars` file, which Terraform loads automatically; keep that file out of version control.

`terraform apply` runs the plan first and asks for confirmation. It deploys every deployable row whose Region matches `aws_region`. Each row becomes one `module.deployment["<Region>#<Customer>#<Model>"]` instance. Deployment typically takes 1–2 minutes per model.

To deploy a second Region, use its own workspace. This is required, not optional: each `apply` filters the registry to one Region, so applying a different Region in the same workspace would destroy the first Region's deployment and replace it. Change only `aws_region`; keep `registry_table_region` pointed at the table's Region.

```bash
cd terraform
terraform workspace new eu-west-1
terraform apply -var="aws_region=eu-west-1" -var="registry_table_region=us-east-1"
cd ..
```

Continue with [Verify and test](#verify-and-test).

## Method 2: Organization deployment

> Before you start, complete [Common setup](#common-setup) and review the [Registry reference](#registry-reference) and [Prerequisites](#prerequisites). This method runs from a hub account (where the registry table lives) and deploys into member accounts, once per account.

Terraform has no StackSets. The recommended multi-account pattern is the provider assuming a role in the target account, run once per account. When you set `target_account_id`, the provider assumes `arn:aws:iam::<target_account_id>:role/<assume_role_name>`.

### Additional prerequisites

- A role your hub credentials can assume in each target account, with permission to create the workload resources. AWS Organizations provisions `OrganizationAccountAccessRole` by default; override with `-var="assume_role_name=..."` if you use a different role.
- Per-Region artifact buckets that the target accounts can read (Lambda reads its package from a bucket in the same Region, which [may be in another account](https://docs.aws.amazon.com/securityhub/1.0/APIReference/API_AwsLambdaFunctionCode.html)).

### Step 1: Create per-Region artifact buckets shared to the organization

Create one bucket per Region in the hub account, and grant read access to the organization. This is the same pattern as the CloudFormation StackSets method. Run once per Region:

```bash
REGION=us-east-1
BUCKET=bedrock-ops-alert-artifacts-<hub-account-id>-$REGION
ORG_ID=$(aws organizations describe-organization --query 'Organization.Id' --output text)

aws s3api create-bucket --bucket $BUCKET --region $REGION \
  $( [ "$REGION" = us-east-1 ] || echo --create-bucket-configuration LocationConstraint=$REGION )
aws s3api put-public-access-block --bucket $BUCKET \
  --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
aws s3api put-bucket-policy --bucket $BUCKET --policy "{
  \"Version\": \"2012-10-17\",
  \"Statement\": [{
    \"Sid\": \"OrgReadArtifacts\",
    \"Effect\": \"Allow\",
    \"Principal\": \"*\",
    \"Action\": \"s3:GetObject\",
    \"Resource\": \"arn:aws:s3:::$BUCKET/*\",
    \"Condition\": {\"StringEquals\": {\"aws:PrincipalOrgID\": \"$ORG_ID\"}}
  }]
}"

aws s3 cp code/lambda/notification_processor.zip s3://$BUCKET/
aws s3 cp code/lambda/quota_calculator.zip s3://$BUCKET/
aws s3 cp code/lambda/alarm_updater.zip s3://$BUCKET/
aws s3 cp code/quota_utils_layer.zip s3://$BUCKET/
```

Set each row's `LambdaS3Bucket` to the hub bucket for that row's Region. Keep SSE-S3 (the default); SSE-KMS would also need cross-account KMS grants.

### Step 2: Create the registry table and add rows in the hub account

Create the registry table once in the hub account, in your hub Region (where the table will live). It is a separate Terraform state from the deployment, and the deployment reads it while planning, so it must exist first. One table holds the rows for every target Region and account, so unlike the per-Region artifact bucket, it is not repeated per Region.

```bash
cd terraform/registry-table && terraform init && terraform apply -var="aws_region=us-east-1" && cd ../..
```

Add one row per target. Seed the sample row (its `Status` is `TEST`, which the registry ignores), then duplicate and edit the copy. Set `AccountId` to the **target member account**, not your hub account; Terraform ignores `OrganizationalUnitId`, so the single-account sample works as-is. Run this with your hub credentials, since the table lives in the hub account:

```bash
TARGET_ACCOUNT_ID=444455556666        # the member account you deploy into
sed -e "s/REPLACE_ME_ACCOUNT_ID/$TARGET_ACCOUNT_ID/" \
    -e "s/REPLACE_ME_BUCKET_NAME/$BUCKET/" \
    code/registry/sample_row.json > /tmp/seed_row.json
aws dynamodb put-item --cli-input-json file:///tmp/seed_row.json
```

Then duplicate that row in the [DynamoDB console](https://console.aws.amazon.com/dynamodbv2/home#tables) and edit the copy: set `DeploymentTarget` (`<Region>#<CustomerName>#<BedrockModelName>`), `StakeholderEmailList`, `Status` to an empty string, and the model columns. `OrganizationalUnitId` is not needed for Terraform. See the [Registry reference](#registry-reference) for every column.

### Step 3: Deploy per account

Run once per target account, using a separate workspace to isolate state. This is required: each `apply` filters the registry to one account and Region, so reusing a workspace for a different target would destroy the previous target's deployment and replace it. `registry_table_region` is the hub Region where the table lives; `aws_region` is the deployment Region.

```bash
cd terraform
terraform init
# first run creates the workspace; on later runs use: terraform workspace select 444455556666-us-east-1
terraform workspace new 444455556666-us-east-1

terraform apply \
  -var="aws_region=us-east-1" \
  -var="registry_table_region=us-east-1" \
  -var="target_account_id=444455556666" \
  -var="assume_role_name=OrganizationAccountAccessRole"

cd ..
```

`registry_rows.py` runs with your hub credentials, scans the hub registry table, and returns the rows for that account and Region. Terraform deploys them into the target account through the assumed role. Repeat for each account and Region, one workspace each.

Continue with [Verify and test](#verify-and-test).

## Verify and test

Review the outputs (run in the workspace you deployed):

```bash
terraform output
```

`deployed_targets` lists what was deployed, and `composite_alarm_names` maps each target to its composite alarm.

Confirm the alarms were created. Replace `AcmeCorp` and `G-Opus-4-6` with your own values (for the organization method, run this in the target account):

```bash
aws cloudwatch describe-alarms --alarm-name-prefix "AcmeCorp-Bedrock" \
  --query '{MetricAlarms:length(MetricAlarms),CompositeAlarms:length(CompositeAlarms)}'
```

The result shows 10 metric alarms and 1 composite alarm.

**Confirm email subscriptions.** Stakeholders receive SNS confirmation emails after deployment. Each recipient must choose the confirmation link to receive alerts. A successful `apply` means the resources were created, not that alerts are flowing; SNS email subscriptions cannot be confirmed programmatically.

**Test alarm notifications.** Trigger a test alarm in the deployed account. Replace `AcmeCorp` and `G-Opus-4-6` with your values:

```bash
aws cloudwatch set-alarm-state \
  --alarm-name AcmeCorp-Bedrock-HighInvocationRate-Warning-G-Opus-4-6 \
  --state-value ALARM \
  --state-reason "Testing notification workflow"
```

## Clean up

Run each block below from the repository root; the `cd` lines assume that.

1. Tear down the deployments, in each workspace, before deleting anything else. To remove one model, set its row `Status` to `PENDING_DELETE` (or delete the row) and run `terraform apply` again in the matching workspace; Terraform destroys what is no longer desired. To remove everything in a workspace, run `terraform destroy` with the same variables you deployed with. Both paths read the registry table, so pass `registry_table_region` exactly as you do when deploying.

   ```bash
   cd terraform
   terraform workspace select us-east-1        # select the workspace you are tearing down

   # single-account
   terraform destroy -var="aws_region=us-east-1" -var="registry_table_region=us-east-1"

   # organization (per account workspace)
   terraform destroy -var="aws_region=us-east-1" -var="registry_table_region=us-east-1" \
     -var="target_account_id=444455556666"

   cd ..
   ```

   The destroy removes all Lambda functions, CloudWatch alarms, SNS topics, Secrets Manager secrets, Parameter Store parameters, IAM roles, and EventBridge rules. Secrets use a zero-day recovery window, so their names are immediately reusable.

2. Delete the registry table, only after every deployment has been torn down in Step 1. Each `plan` and `apply` reads the table, so removing it earlier would leave any remaining deployment unmanageable. Destroy the bootstrap configuration with the same Region you created it with:

   ```bash
   cd terraform/registry-table && terraform destroy -var="aws_region=us-east-1" && cd ../..
   ```

3. Delete the artifact bucket(s). Single-account has one; the organization method has one per Region:

```bash
aws s3 rm s3://$BUCKET_NAME --recursive
aws s3 rb s3://$BUCKET_NAME
```

4. Delete the Parameter Store threshold parameters, which the Lambda creates at runtime and Terraform does not manage. Replace `AcmeCorp` and `G-Opus-4-6` with your values (run in the target account for the organization method):

```bash
aws ssm delete-parameters --names \
  "/AcmeCorp/bedrock/quota-monitoring/G-Opus-4-6/thresholds/rpm-threshold-calculated" \
  "/AcmeCorp/bedrock/quota-monitoring/G-Opus-4-6/thresholds/tpm-threshold-calculated" \
  "/AcmeCorp/bedrock/quota-monitoring/G-Opus-4-6/thresholds/last-updated" \
  "/AcmeCorp/bedrock/quota-monitoring/G-Opus-4-6/resolved-model-id-display"
```

5. (Optional) Delete the CloudWatch log groups:

```bash
aws logs delete-log-group --log-group-name /aws/lambda/AcmeCorp-Quota-Calculator-G-Opus-4-6
aws logs delete-log-group --log-group-name /aws/lambda/AcmeCorp-Alarm-Updater-G-Opus-4-6
aws logs delete-log-group --log-group-name /aws/lambda/AcmeCorp-Bedrock-Notification-Processor-G-Opus-4-6
```

**Note:** The registry table is the desired-state input for `plan` and `apply`, so keep it while any workspace still holds deployed rows. If the table is missing, `registry_rows.py` fails and Terraform stops rather than destroying anything. Deleting the table does not remove running deployments — Terraform state still tracks them — but you cannot manage them until the table and their rows are restored.
