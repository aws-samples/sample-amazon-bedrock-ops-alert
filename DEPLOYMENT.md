# Amazon Bedrock Ops Alert: Registry-Driven Deployment Guide

This guide explains how to deploy Amazon Bedrock Ops Alert from a DynamoDB registry. It covers the row format, prerequisites, packaging, deployment, testing, and cleanup.

Rather than assembling a 30-parameter CloudFormation command for each model, you define every deployment as a row in a DynamoDB table and run a local script that applies it. Each row represents one monitoring stack, so you add a model by adding a row and remove one by changing a single column.

For the Terraform equivalent, see [TF-DEPLOYMENT.md](TF-DEPLOYMENT.md). It deploys the same workload from the same registry table and produces identical resources.

## Choose your deployment mode

The registry supports two modes. Both use the same row format, the same validation, and the same status model; they differ only in where they run and how they deploy.

| Mode | Use it when | Deploys with | Guide |
|------|-------------|--------------|-------|
| Single-account | You deploy into the account you are signed in to | One CloudFormation stack per row (`registry.py`) | [Method 1](#method-1-single-account-deployment) |
| Organization | You deploy into many member accounts of an AWS Organization from the management account | One service-managed StackSet per customer+model, one stack instance per row (`registry_stackset.py`) | [Method 2](#method-2-organization-deployment-service-managed-stacksets) |

Read [How it works](#how-it-works) and the [Registry reference](#registry-reference) first, then follow the method that fits.

## How it works

The script runs on your machine under your own credentials. CloudFormation acts with your permissions, exactly as it does when you run `aws cloudformation create-stack` yourself, so the registry introduces no standing IAM role, no Lambda, and no privileged identity of its own. Each run reads the rows, validates them, applies the required change, and writes the outcome back to the row.

Single-account mode deploys a stack into the account you are signed in to:

```
   your terminal  (workload account)
        │  your credentials
        ├──────────────► DynamoDB registry      reads rows, writes status
        └──────────────► CloudFormation ───────► one workload stack per row
```

Organization mode runs from the management account and deploys into member accounts through service-managed StackSets:

```
   your terminal  (management account)
        │  your credentials
        ├──────────────► DynamoDB registry            reads rows, writes status
        └──────────────► CloudFormation StackSets ──► one stack instance per row,
                                                        in the target member account
```

Both modes offer the same commands. Use `registry.py` for single-account and `registry_stackset.py` for organization.

| Command | Effect |
|---------|--------|
| `plan` | Reads every row, validates each one, and reports what would happen. Makes no change in AWS. |
| `apply` | Runs the same preview, asks you to confirm, then creates or deletes per the row `Status`. |
| `apply --force` | Applies without the preview and the confirmation prompt, for unattended runs. |

In single-account mode, for each valid create the driver also writes the equivalent `aws cloudformation create-stack` command, with your real values, to `code/registry/output/<date>/<stack_name>.txt`. This is a record of what was deployed. These files are gitignored and are only for your reference. Organization mode does not write these records.

## Registry reference

Each row is one deployment. Two attributes form the key, a few columns are written by the script, one column is optional input, and the rest map to the workload template parameters.

**Leaving a column blank omits the corresponding CloudFormation parameter, so the template default applies.** The defaults for the following columns are validated for production and recommended for most deployments: SupportCaseLookbackDays, TokensPerMinuteIncreasePercent, RequestsPerMinuteIncreasePercent, ErrorThreshold, CriticalAlarmEvaluationPeriods, RequestsPerMinuteThresholdPercent, TokensPerMinuteThresholdPercent, LatencyThresholdMs, WarningAlarmEvaluationPeriods, LatencyAlarmPeriod, LatencyAlarmEvaluationPeriods, AnomalyDetectionPeriod, AnomalyEvaluationPeriods, AnomalySensitivity, AlarmEvaluationPeriod, and ThresholdUpdateScheduleIntervalDays. Adjust them only if your workload needs it.

The **Modify** column tells you when each value needs your attention:

| Modify | Meaning |
|--------|---------|
| Required | Must be set. The registry rejects a blank value |
| Per-model(Required) / Per-model(Optional) | Review this every time you monitor a different model. Required rejects a blank value; Optional falls back to the default |
| Optional | Leave blank to accept the validated default. Tune only if your workload needs it |
| Org only | Used by the organization method only. Leave absent for single-account |
| Derived | Parsed from `DeploymentTarget`. Not a column, never entered |
| Automatic | Written by the script. Do not set manually |

**Registry Key and Deployment State**

These columns identify the deployment and report its outcome. They are the ones you read when checking on a row.

| Column | Modify | Description | Default |
|--------|--------|-------------|---------|
| AccountId | Required | Partition key, a 12-digit AWS account ID. Single-account: the account you are signed in to; the driver reads only rows matching it. Organization: the target member account the stack instance is deployed to |  |
| DeploymentTarget | Required | Sort key, formatted `<Region>#<CustomerName>#<BedrockModelName>`, for example `us-east-1#AcmeCorp#G-Opus-4-6`. CustomerName (1-10 chars) and BedrockModelName (1-15 chars) are short, meaningful labels you choose for resource naming, not the full model ID; both allow letters, numbers, and hyphens. The script parses all three and passes them to CloudFormation |  |
| OrganizationalUnitId | Org only | The OU id (`ou-....`) or root id (`r-....`) that contains the target account. The StackSet deploys to exactly that account within the OU. Leave absent for single-account |  |
| Status | Required | Empty or REQUESTED to deploy, PENDING_DELETE to tear down. See [Deployment status reference](#deployment-status-reference) |  |
| StatusReason | Automatic | Outcome of the last operation. On failure, carries the root cause read from CloudFormation |  |

**General Configuration**

| Column | Modify | Description | Default |
|--------|--------|-------------|---------|
| CustomerName | Derived | Derived from `DeploymentTarget`, not a column. Your company or team identifier for resource naming (max 10 characters). Used in alarm names, Parameter Store paths, and Lambda function names |  |
| StakeholderEmailList | Required | Comma-separated list of stakeholder emails for alerts, that is, the people notified when alarms trigger (your SRE team) |  |
| NotificationPreference | Optional | Email notification filter: all, critical, or warning. Updatable via Parameter Store post-deployment | all |
| Tags | Optional | Tags applied to the deployed stack, formatted `Key=Value,Key=Value`. Only the tags in this column are applied; nothing is added automatically. The sample row includes a `Solution` tag you can keep or remove. Up to 50 tags. Keys and values allow letters, numbers, spaces and `_ . : / = + - @`; the `aws:` prefix is reserved |  |

**Lambda Deployment Configuration**

| Column | Modify | Description | Default |
|--------|--------|-------------|---------|
| LambdaS3Bucket | Required | S3 bucket holding the Lambda deployment packages and the workload template. Must be in the same Region as the deployment. For the organization method this is the management-account artifact bucket for that Region |  |

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

**Registry-Managed Columns**

These columns are written by the script. Do not set them manually.

| Column | Modify | Description |
|--------|--------|-------------|
| StackId | Automatic | Single-account: the stack ARN. Organization: the StackSet name. Written after CloudFormation accepts the request, so an interrupted run still identifies what was created |
| CreatedAt, UpdatedAt | Automatic | CreatedAt is set on the first state change and never changed after; UpdatedAt is refreshed on every state change |

### Deployment status reference

| Status | Meaning | Set by |
|--------|---------|--------|
| *(empty)* or REQUESTED | Deploy this row | You |
| PENDING_DELETE | Tear down this deployment | You |
| TEST | Ignored permanently. Used by the sample row | You |
| DEPLOYED | Deployed successfully | Script |
| FAILED | Deployment failed. See StatusReason | Script |
| VALIDATION_FAILED | Rejected before anything was created. See StatusReason | Script |
| DELETED | Removed, and (single-account) secrets force-deleted | Script |
| DELETE_FAILED | Teardown failed. See StatusReason | Script |

`DEPLOYED`, `FAILED`, `VALIDATION_FAILED`, `DELETED`, and `DELETE_FAILED` are terminal. Clear `Status` to act on the row again.

## Prerequisites

Before deploying the solution, confirm you have the following.

**AWS account requirements:**

- Active Amazon Bedrock usage with established quotas for your target model
- AWS Business or Enterprise Support plan (required for automated support case creation through the Support API)
- IAM permissions to create AWS CloudFormation stacks, Amazon DynamoDB tables, and the associated resources
- Service Quotas service-linked role (required for the deterministic quota increase path). If your account has never requested a quota increase, create it with: `aws iam create-service-linked-role --aws-service-name servicequotas.amazonaws.com`. Without this role, the solution falls back to creating support cases directly — functionality is not impacted.

**Required information:**

- S3 bucket for Lambda deployment packages (per Region for the organization method)
- Stakeholder email addresses for alert notifications
- Service Quota codes for your target Bedrock model (RPM and TPM). Find codes at the [Service Quotas console](https://console.aws.amazon.com/servicequotas/home/services/bedrock/quotas)

**Tools:**

- AWS CLI configured with appropriate credentials
- Zip utility for packaging Lambda functions
- Python 3.9 or later with the AWS SDK for Python (boto3)

The organization method has one additional prerequisite (AWS Organizations trusted access), described in [Method 2](#method-2-organization-deployment-service-managed-stacksets).

## Common setup

Complete these steps once, for either mode.

### Clone the repository and create a virtual environment

```bash
git clone https://github.com/aws-samples/sample-amazon-bedrock-ops-alert.git
cd sample-amazon-bedrock-ops-alert

python3 -m venv .venv
.venv/bin/pip install boto3
```

This guide invokes the script as `.venv/bin/python code/registry/registry.py`, which works whether or not the file's execute bit is set.

### Package the Lambda functions and layer

The solution uses Lambda deployment packages for all functions. The Lambda layer holds shared quota calculation utilities used by multiple functions, which removes code duplication and keeps behavior consistent.

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

The registry validates that the four zips and the template exist in the target bucket before every deployment, and rejects the row if any is missing.

## Method 1: Single-account deployment

> Before you start, complete [Common setup](#common-setup) and review the [Registry reference](#registry-reference) and [Prerequisites](#prerequisites). This method deploys into the account you are signed in to.

### Step 1: Create the S3 bucket and upload the artifacts

Set your bucket name, create the bucket, and enable versioning to protect the packages from accidental overwrites.

```bash
BUCKET_NAME=YOUR-BUCKET-NAME
aws s3 mb s3://$BUCKET_NAME --region us-east-1
aws s3api put-bucket-versioning --bucket $BUCKET_NAME \
  --versioning-configuration Status=Enabled
```

Upload the four Lambda packages and the workload template. The registry builds the template URL from each row's `LambdaS3Bucket`, so the template must sit alongside the packages.

```bash
aws s3 cp code/lambda/notification_processor.zip s3://$BUCKET_NAME/
aws s3 cp code/lambda/quota_calculator.zip s3://$BUCKET_NAME/
aws s3 cp code/lambda/alarm_updater.zip s3://$BUCKET_NAME/
aws s3 cp code/quota_utils_layer.zip s3://$BUCKET_NAME/
aws s3 cp code/bedrock-quota-alarm.yml s3://$BUCKET_NAME/
```

**Note:** AWS Lambda requires the deployment-package bucket to be in the same Region as the function. If you deploy monitoring to more than one Region, create a bucket per Region and set each row's `LambdaS3Bucket` accordingly.

### Step 2: Create the registry table

Deploy the registry table once per account. The template defines a single DynamoDB table and no IAM resources, so `--capabilities` is not required, and it is small enough to pass inline with `--template-body`.

```bash
aws cloudformation create-stack \
  --stack-name bedrock-ops-alert-registry-table \
  --template-body file://code/registry/registry_table.yml

aws cloudformation wait stack-create-complete \
  --stack-name bedrock-ops-alert-registry-table
```

To use a table name other than the default `bedrock-ops-alert-registry`, add `--parameters ParameterKey=RegistryTableName,ParameterValue=YOUR-TABLE-NAME` and pass the same name to every script command with `--table`.

### Step 3: Add a deployment row

`code/registry/sample_row.json` is a complete reference row with `Status` set to `TEST`, which the registry always ignores, so it is safe to keep as a template to copy. Seed it, then copy and edit the copy. See the [Registry reference](#registry-reference) for every column.

```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

sed -e "s/REPLACE_ME_ACCOUNT_ID/$ACCOUNT_ID/" \
    -e "s/REPLACE_ME_BUCKET_NAME/$BUCKET_NAME/" \
    code/registry/sample_row.json > /tmp/seed_row.json

aws dynamodb put-item --cli-input-json file:///tmp/seed_row.json
```

In the [DynamoDB console](https://console.aws.amazon.com/dynamodbv2/home#tables), open the `bedrock-ops-alert-registry` table, choose **Explore table items**, select the sample row, choose **Actions**, **Duplicate item**, then set at least:

- `DeploymentTarget`: `<Region>#<CustomerName>#<BedrockModelName>`, for example `us-east-1#AcmeCorp#G-Opus-4-6`
- `StakeholderEmailList`: your stakeholder emails, comma-separated
- `Status`: an empty string to request deployment

If you monitor a model other than the example (Claude Opus 4.6), also update `BedrockModelId`, `TokensPerMinuteQuotaCode`, `RequestsPerMinuteQuotaCode`, `InputModalities`, and `LatencyThresholdMs`. Quota codes are model-specific; copying them unchanged points the alarms at Claude Opus 4.6's quotas.

For example, `us-east-1#AcmeCorp#G-Opus-4-6` produces the stack `bedrock-ops-alert-acmecorp-g-opus-4-6`, alarm names `AcmeCorp-Bedrock-*-G-Opus-4-6`, and Parameter Store paths `/AcmeCorp/bedrock/quota-monitoring/G-Opus-4-6/`.

### Step 4: Preview with plan

`plan` validates every row for your account and reports what `apply` would do. It changes nothing in AWS, and exits 1 if any row is invalid, so you can use it as a gate.

```bash
.venv/bin/python code/registry/registry.py plan
```

```
Registry : bedrock-ops-alert-registry
Account  : 111122223333
Identity : arn:aws:sts::111122223333:assumed-role/Admin/you
Command  : plan

  CREATE    us-east-1#AcmeCorp#G-Opus-4-6
            stack:      bedrock-ops-alert-acmecorp-g-opus-4-6
            template:   https://YOUR-BUCKET-NAME.s3.us-east-1.amazonaws.com/bedrock-quota-alarm.yml
            parameters: 30 supplied, 0 defaulted
            model:      confirmed (SYSTEM_DEFINED -> anthropic.claude-opus-4-6-v1)
            tags:       Solution=Amazon-Bedrock-Ops-Alert
            record:     code/registry/output/2026-01-01/bedrock-ops-alert-acmecorp-g-opus-4-6.txt
  SKIP      us-east-1#EXAMPLE#G-Opus-4-6
            sample row (Status=TEST)

Plan: 1 actionable, 0 invalid, 1 skipped.
```

Before creating any stack, `plan` confirms that the mandatory values are present, the five artifacts exist in the bucket, and the model is reachable in the target Region.

### Step 5: Deploy with apply

`apply` shows the plan again, then asks you to confirm before making any change. Press Enter (or `y`) to proceed, or `n` to abort. Add `--force` to skip the preview and prompt for unattended runs.

```bash
.venv/bin/python code/registry/registry.py apply
```

Deployment typically takes 3–5 minutes per row.

```
Apply these changes? [Y/n] y

  CREATE: us-east-1#AcmeCorp#G-Opus-4-6
      record: code/registry/output/2026-01-01/bedrock-ops-alert-acmecorp-g-opus-4-6.txt
      created arn:aws:cloudformation:us-east-1:111122223333:stack/bedrock-ops-alert-acmecorp-g-opus-4-6/...
      waiting for CREATE_COMPLETE (typically 3-5 min)...
      DEPLOYED

Apply: 1 succeeded, 0 failed, 1 skipped.
```

If a deployment fails, the row records the root cause in `StatusReason` and the status becomes `FAILED`. Fix the row, clear `Status`, and run `apply` again. The script deletes a rolled-back stack automatically before retrying, because CloudFormation cannot update a stack left in `ROLLBACK_COMPLETE`. If a run is interrupted after the stack was created but before the row was updated, run `apply` again; it finds the stack already `CREATE_COMPLETE` and adopts it, instead of creating a duplicate.

Continue with [Verify and test](#verify-and-test).

## Method 2: Organization deployment (service-managed StackSets)

> Before you start, complete [Common setup](#common-setup) and review the [Registry reference](#registry-reference) and [Prerequisites](#prerequisites). This method runs from the AWS Organizations **management account** and deploys into member accounts. Service-managed StackSets do not deploy to the management account itself; monitor that account with [Method 1](#method-1-single-account-deployment).

### Additional prerequisites

- AWS Organizations with **all features** enabled, and **trusted access** enabled for CloudFormation StackSets. With these on, StackSets creates its own IAM roles in the management and member accounts, so there is no per-account role to set up.
- The workload template must contain no `Transform` or macros. The shipped `code/bedrock-quota-alarm.yml` does not.

### Step 1: Create per-Region artifact buckets shared to the organization

Lambda requires its deployment-package bucket to be in the **same Region** as the function, but [that bucket can be in a different account](https://docs.aws.amazon.com/securityhub/1.0/APIReference/API_AwsLambdaFunctionCode.html). So you do not put a bucket in every account. Instead, create one artifact bucket **per Region** in the management account, and grant read access to the whole organization. Each member account's Lambda then reads the code cross-account.

Run this once per Region you deploy into:

```bash
REGION=us-east-1
BUCKET=bedrock-ops-alert-artifacts-<mgmt-account-id>-$REGION
ORG_ID=$(aws organizations describe-organization --query 'Organization.Id' --output text)

# Create the bucket (us-east-1 needs no LocationConstraint)
aws s3api create-bucket --bucket $BUCKET --region $REGION \
  $( [ "$REGION" = us-east-1 ] || echo --create-bucket-configuration LocationConstraint=$REGION )

# Block public access
aws s3api put-public-access-block --bucket $BUCKET \
  --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

# Allow only accounts in your organization to read objects
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

# Upload the four zips and the template (built in Common setup)
aws s3 cp code/lambda/notification_processor.zip s3://$BUCKET/
aws s3 cp code/lambda/quota_calculator.zip s3://$BUCKET/
aws s3 cp code/lambda/alarm_updater.zip s3://$BUCKET/
aws s3 cp code/quota_utils_layer.zip s3://$BUCKET/
aws s3 cp code/bedrock-quota-alarm.yml s3://$BUCKET/
```

**Note:** Amazon S3 encrypts objects with SSE-S3 (AES256) by default, which needs no extra setup. Do not switch these buckets to SSE-KMS, or cross-account reads would also need KMS key-policy grants. Each row's `LambdaS3Bucket` is the management-account bucket for that row's Region.

### Step 2: Create the registry table in the management account

Deploy the same registry table template, in the management account. The schema is identical to the single-account table. Create it once: one table holds the rows for every target Region and account, so unlike the per-Region artifact bucket, the table is not repeated per Region.

```bash
aws cloudformation create-stack \
  --stack-name bedrock-ops-alert-registry-table \
  --template-body file://code/registry/registry_table.yml

aws cloudformation wait stack-create-complete \
  --stack-name bedrock-ops-alert-registry-table
```

### Step 3: Add a deployment row

`code/registry/sample_org_row.json` is the organization reference row, with `Status` set to `TEST`. It is the single-account sample plus the `OrganizationalUnitId` column and a target `AccountId`. Seed it, then copy and edit the copy. See the [Registry reference](#registry-reference) for every column.

Set `TARGET_ACCOUNT_ID` to the target member account and `OU_ID` to the OU that contains it, then seed the row:

```bash
TARGET_ACCOUNT_ID=444455556666
OU_ID=ou-ab12-cd34ef56

sed -e "s/REPLACE_ME_TARGET_ACCOUNT_ID/$TARGET_ACCOUNT_ID/" \
    -e "s/REPLACE_ME_OU_ID/$OU_ID/" \
    -e "s/REPLACE_ME_BUCKET_NAME/$BUCKET/" \
    code/registry/sample_org_row.json > /tmp/seed_org_row.json

aws dynamodb put-item --cli-input-json file:///tmp/seed_org_row.json
```

Then copy the row and edit the copy for a real deployment. Compared with the single-account row, two columns are specific to this method:

- `AccountId`: the **target member account**, not your management account.
- `OrganizationalUnitId`: the OU that contains that account, for example `ou-ab12-cd34ef56` (or a root id `r-ab12`). Find it in the [AWS Organizations console](https://console.aws.amazon.com/organizations/v2/home/accounts): select the account and read the parent OU's id (or run `aws organizations list-parents --child-id <AccountId>`).

Set `LambdaS3Bucket` to the management-account artifact bucket for the row's Region, `Status` to an empty string, and the model columns as in Method 1.

### Step 4: Preview and apply

The commands mirror Method 1, using the organization driver:

```bash
.venv/bin/python code/registry/registry_stackset.py plan
.venv/bin/python code/registry/registry_stackset.py apply
.venv/bin/python code/registry/registry_stackset.py apply --force
```

`plan` scans every row, validates it (required values, artifacts present in the Regional bucket, and the OU id and account id shapes), and groups the rows by stack set. Rows that share a CustomerName and BedrockModelName form one stack set, and each row is one stack instance in it; those rows must set the same columns and the same Tags, or `plan` reports the group as invalid. `apply` previews, asks for confirmation, then creates each stack set once and adds one stack instance per row, passing that row's values as per-instance parameter overrides.

```
Registry : bedrock-ops-alert-registry (organization / StackSets)
Account  : 111122223333 (management account)
Identity : arn:aws:sts::111122223333:assumed-role/Admin/you
Command  : apply

  CREATE    stack set: bedrock-ops-alert-acmecorp-g-opus-4-6
            tags:      Solution=Amazon-Bedrock-Ops-Alert
            instances (2):
              + 444455556666 / us-east-1 (OU ou-ab12-cd34ef56)
                  template:   https://bedrock-ops-alert-artifacts-111122223333-us-east-1.s3.us-east-1.amazonaws.com/bedrock-quota-alarm.yml
                  parameters: 30 supplied, 0 defaulted
              + 555566667777 / us-west-2 (OU ou-ab12-cd34ef56)
                  template:   https://bedrock-ops-alert-artifacts-111122223333-us-west-2.s3.us-west-2.amazonaws.com/bedrock-quota-alarm.yml
                  parameters: 30 supplied, 0 defaulted

Apply these changes? [Y/n] y

  CREATE: us-east-1#AcmeCorp#G-Opus-4-6
      created stack set bedrock-ops-alert-acmecorp-g-opus-4-6
      adding stack instance in 444455556666/us-east-1 (typically 3-5 min)...
      DEPLOYED

  CREATE: us-west-2#AcmeCorp#G-Opus-4-6
      adding stack instance in 555566667777/us-west-2 (typically 3-5 min)...
      DEPLOYED

Apply: 2 succeeded, 0 failed, 0 skipped.
```

Two behavior differences from Method 1:

- No pre-flight model-availability check. The management account cannot probe Bedrock in the target account, so a wrong `BedrockModelId` surfaces when the stack instance fails, not during `plan`.
- No local deploy-record file is written.

Verify in the target account, as described in [Verify and test](#verify-and-test).

## Verify and test

After a successful deployment, review the stack outputs (in the target account for the organization method):

```bash
aws cloudformation describe-stacks \
  --stack-name bedrock-ops-alert-acmecorp-g-opus-4-6 \
  --query 'Stacks[0].Outputs'
```

Confirm the alarms were created. Replace `AcmeCorp` and `G-Opus-4-6` with your own values:

```bash
aws cloudwatch describe-alarms --alarm-name-prefix "AcmeCorp-Bedrock" \
  --query '{MetricAlarms:length(MetricAlarms),CompositeAlarms:length(CompositeAlarms)}'
```

The result shows 10 metric alarms and 1 composite alarm.

**Confirm email subscriptions.** After deployment, stakeholders receive SNS confirmation emails. Each recipient must choose the confirmation link to receive alerts. A `DEPLOYED` status means the stack was created, not that alerts are flowing; SNS email subscriptions cannot be confirmed programmatically.

**Test alarm notifications.** Trigger a test alarm and confirm the notification arrives:

```bash
aws cloudwatch set-alarm-state \
  --alarm-name AcmeCorp-Bedrock-HighInvocationRate-Warning-G-Opus-4-6 \
  --state-value ALARM \
  --state-reason "Testing notification workflow"
```

## Clean up

When you no longer need the solution, remove the deployments and then the shared resources.

1. Tear down the deployments. Set `Status` to `PENDING_DELETE` on every row you want removed, then run `apply` with the driver for your mode. This removes each workload stack, including all Lambda functions, CloudWatch alarms, SNS topics, Secrets Manager secrets, Parameter Store parameters, IAM roles, and EventBridge rules.

   Single-account: the script also force-deletes the two Secrets Manager secrets after the stack is removed, so the secret names are immediately reusable.

   ```bash
   .venv/bin/python code/registry/registry.py apply
   ```

   Organization: set `Status` to `PENDING_DELETE` only on the rows you want removed. The driver deletes just those rows' stack instances and removes the stack set once its last instance is gone, so tearing down one row never affects the others that share the set. The two secrets live in the target account, which the management account cannot force-delete across the account boundary, so they keep the standard recovery window (at least seven days). To reuse the same account, Region, and model within that window, delete the two secrets from inside the target account first.

   ```bash
   .venv/bin/python code/registry/registry_stackset.py apply
   ```

2. Delete the registry table stack:

```bash
aws cloudformation delete-stack --stack-name bedrock-ops-alert-registry-table
```

3. Delete the artifact bucket(s). Single-account has one; the organization method has one per Region:

```bash
aws s3 rm s3://$BUCKET_NAME --recursive
aws s3 rb s3://$BUCKET_NAME
```

4. Delete the Parameter Store threshold parameters not managed by CloudFormation. Replace `AcmeCorp` and `G-Opus-4-6` with your `DeploymentTarget` values (run in the target account for the organization method):

```bash
aws ssm delete-parameters --names \
  "/AcmeCorp/bedrock/quota-monitoring/G-Opus-4-6/thresholds/rpm-threshold-calculated" \
  "/AcmeCorp/bedrock/quota-monitoring/G-Opus-4-6/thresholds/tpm-threshold-calculated" \
  "/AcmeCorp/bedrock/quota-monitoring/G-Opus-4-6/thresholds/last-updated" \
  "/AcmeCorp/bedrock/quota-monitoring/G-Opus-4-6/resolved-model-id-display"
```

5. (Optional) Delete the CloudWatch log groups:

```bash
aws logs delete-log-group --log-group-name /aws/lambda/AcmeCorp-Bedrock-Notification-Processor-G-Opus-4-6
aws logs delete-log-group --log-group-name /aws/lambda/AcmeCorp-Alarm-Updater-G-Opus-4-6
aws logs delete-log-group --log-group-name /aws/lambda/AcmeCorp-Quota-Calculator-G-Opus-4-6
aws logs delete-log-group --log-group-name /aws/lambda/AcmeCorp-Email-Subscription-Manager-G-Opus-4-6
```

**Note:** Deleting the registry table removes the deployment records, not the deployments. Any workload stack still running is unaffected. To resume management, recreate the table, re-add the rows, and run `apply`; it adopts any stack that already exists and marks the row `DEPLOYED`.
