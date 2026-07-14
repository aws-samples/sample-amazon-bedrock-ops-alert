# Amazon Bedrock Ops Alert: Terraform Deployment Guide

This guide walks you through deploying Amazon Bedrock Ops Alert using Terraform, including prerequisites, packaging, deployment, testing, and cleanup.

For AWS CloudFormation deployment instructions, see [DEPLOYMENT.md](DEPLOYMENT.md).

## Prerequisites

Before deploying the solution, confirm you have the following:

**AWS account requirements:**

- Active Amazon Bedrock usage with established quotas for your target model
- AWS Business or Enterprise Support plan (required for automated support case creation through the Support API)
- AWS Identity and Access Management (IAM) permissions to create the resources defined in this configuration
- Service Quotas service-linked role (required for the deterministic quota increase path). If your account has never requested a quota increase, create it with: `aws iam create-service-linked-role --aws-service-name servicequotas.amazonaws.com`. Without this role, the solution falls back to creating support cases directly — functionality is not impacted.

**Required information:**

- S3 bucket for Lambda deployment packages
- Stakeholder email addresses for alert notifications
- Service Quota codes for your target Bedrock model (RPM and TPM). Find codes at the [Service Quotas console](https://console.aws.amazon.com/servicequotas/home/services/bedrock/quotas)

**Tools:**

- AWS CLI configured with appropriate credentials
- Zip utility for packaging Lambda functions
- Terraform >= 1.5.0 (`brew install hashicorp/tap/terraform` on macOS)

## File Structure

```
terraform/
├── providers.tf                      # Terraform + AWS provider config
├── variables.tf                      # 32 input variables (1:1 from CFN Parameters)
├── main.tf                           # KMS, Secrets Manager, SSM, SNS
├── lambda.tf                         # Lambda functions, layer, invocations, EventBridge
├── iam.tf                            # IAM roles and policies (3 roles)
├── alarms.tf                         # CloudWatch alarms (Layer 1, 2, 3 + Composite)
├── outputs.tf                        # 22 outputs (1:1 from CFN Outputs)
├── terraform.tfvars.example          # Example variable values
└── .gitignore                        # Ignores state files, .terraform/, .build/
```

## Step 1: Clone the GitHub repository

Clone the solution repository to your local machine:

```bash
git clone https://github.com/aws-samples/sample-amazon-bedrock-ops-alert.git
cd sample-amazon-bedrock-ops-alert
```

## Step 2: Create an S3 bucket for Lambda deployment

Set your bucket name as an environment variable and create the S3 bucket:

```bash
BUCKET_NAME=YOUR-BUCKET-NAME
aws s3 mb s3://$BUCKET_NAME --region us-east-1
```

Enable versioning on the bucket to protect Lambda deployment packages from accidental overwrites.

```bash
aws s3api put-bucket-versioning --bucket $BUCKET_NAME --versioning-configuration Status=Enabled
```

## Step 3: Package the Lambda function and layer

The solution uses Lambda deployment packages for all functions, uploaded to S3. The Lambda layer contains shared quota calculation utilities used by multiple functions, eliminating code duplication and maintaining consistent behavior. The source files in `code/lambda/` are shared with the CloudFormation deployment.

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

## Step 4: Upload packages to S3

```bash
aws s3 cp code/lambda/notification_processor.zip s3://$BUCKET_NAME/
aws s3 cp code/lambda/quota_calculator.zip s3://$BUCKET_NAME/
aws s3 cp code/lambda/alarm_updater.zip s3://$BUCKET_NAME/
aws s3 cp code/quota_utils_layer.zip s3://$BUCKET_NAME/
```

## Step 5: Configure Terraform variables and deploy

Navigate to the Terraform directory and create your variable file:

```bash
cd terraform
cp terraform.tfvars.example g-opus-4-6.tfvars
```

Edit `g-opus-4-6.tfvars` with your values. Before deploying, replace the following placeholder values:

- `lambda_s3_bucket`: Your S3 bucket name from Step 2
- `customer_name`: Your customer identifier (max 10 characters). This value appears in resource names, alarm prefixes, Parameter Store paths, and Lambda function names.
- `stakeholder_email_list`: Your stakeholder email addresses

If you are monitoring a model other than the example (Claude Opus 4.6), also update the following variables:

- `bedrock_model_name`: Short model name for resource naming (max 15 characters)
- `inference_profile_type`: Set to "Application" if using a customer-created inference profile
- `bedrock_model_id`: For System-Defined: model ID. For Application: short profile ID
- `geo_data_residency_requirement`: Set to "Yes" if your workload has geographic data residency requirements that prevent using Global Cross Region Inference
- `input_modalities`: Input modalities used by the model
- `requests_per_minute_quota_code`: Your model-specific RPM quota code from the [Service Quotas console](https://console.aws.amazon.com/servicequotas/home/services/bedrock/quotas)
- `tokens_per_minute_quota_code`: Your model-specific TPM quota code from the [Service Quotas console](https://console.aws.amazon.com/servicequotas/home/services/bedrock/quotas)

For example, if you set `customer_name` to `Acme` and `bedrock_model_name` to `G-Opus-4-6`, your alarm names follow the pattern `Acme-Bedrock-*-G-Opus-4-6`, your Parameter Store paths follow `/Acme/bedrock/quota-monitoring/G-Opus-4-6/`, and your Lambda function names follow `Acme-Bedrock-Notification-Processor-G-Opus-4-6`.

**Note:** The default values for the following variables have been validated in production environments and are recommended for most deployments: support_case_lookback_days, tokens_per_minute_increase_percent, requests_per_minute_increase_percent, error_threshold, critical_alarm_evaluation_periods, requests_per_minute_threshold_percent, tokens_per_minute_threshold_percent, latency_threshold_ms, warning_alarm_evaluation_periods, latency_alarm_period, latency_alarm_evaluation_periods, anomaly_detection_period, anomaly_evaluation_periods, anomaly_sensitivity, alarm_evaluation_period, and threshold_update_schedule_interval_days. You can adjust these values to match your specific workload characteristics, but the defaults provide a balanced configuration that minimizes false positives while detecting genuine operational issues.

Deploy the infrastructure:

```bash
terraform init
terraform workspace new g-opus-4-6
terraform plan -var-file="g-opus-4-6.tfvars" -out=tfplan
terraform apply tfplan
```

The deployment typically takes 1–2 minutes.

### Deploying multiple models

Each Bedrock model requires its own deployment. Terraform workspaces isolate each model's state, allowing you to manage multiple deployments from the same directory.

For each additional model, create a separate `.tfvars` file and workspace:

```bash
# Create a tfvars file for the new model (e.g., deepseek.tfvars)
cp g-opus-4-6.tfvars deepseek.tfvars
# Edit deepseek.tfvars with the model-specific values (bedrock_model_name, bedrock_model_id, quota codes, etc.)

# Create a workspace and deploy
terraform workspace new deepseek
terraform apply -var-file="deepseek.tfvars"
```

To list all deployed models:

```bash
terraform workspace list
```

To switch between models for updates:

```bash
terraform workspace select g-opus-4-6
terraform plan
```


The following tables describe all Terraform variables organized by configuration group.

**General Configuration**

| Variable | Default | Description |
|----------|---------|-------------|
| customer_name | | Your company or team identifier for resource naming (max 10 characters). Used in alarm names, Parameter Store paths, and Lambda function names. |
| stakeholder_email_list | | List of stakeholder emails for alerts |
| notification_preference | all | Email notification filter: all, critical, or warning. Updatable via Parameter Store post-deployment |

**Lambda Deployment Configuration**

| Variable | Default | Description |
|----------|---------|-------------|
| lambda_s3_bucket | | S3 bucket name containing the Lambda deployment packages |

**Model Specification**

| Variable | Default | Description |
|----------|---------|-------------|
| bedrock_model_name | G-Opus-4-6 | Short model name for resource naming (max 15 characters) |
| inference_profile_type | System-Defined | Inference profile type: System-Defined or Application |
| bedrock_model_id | global.anthropic.claude-opus-4-6-v1 | For System-Defined: model ID. For Application: short profile ID |
| geo_data_residency_requirement | No | Geographic data residency requirement (Yes/No/NA). If Yes, Global Cross Region Inference cannot be considered |
| input_modalities | TEXT and IMAGE | Input modalities used by the model (TEXT and IMAGE, TEXT Only, IMAGE Only) |

**Model Quota Configuration**

| Variable | Default | Description |
|----------|---------|-------------|
| requests_per_minute_quota_code | L-3DD46812 | Set to NA if the model has no RPM quota; otherwise provide the RPM quota code, required when requests_per_minute_increase_percent > 0 |
| requests_per_minute_increase_percent | 25 | RPM quota increase % (0, 25, 50, 75, 100). Set 0 to skip or when requests_per_minute_quota_code is NA. Updatable via Parameter Store |
| tokens_per_minute_quota_code | L-3DCCFAA4 | TPM quota code. Required if tokens_per_minute_increase_percent > 0 |
| tokens_per_minute_increase_percent | 25 | TPM quota increase % (0, 25, 50, 75, 100). Set 0 to skip. Updatable via Parameter Store |

**Model Usage Budget (Threshold)**

| Variable | Default | Description |
|----------|---------|-------------|
| requests_per_minute_threshold_percent | 80 | RPM threshold as percentage of quota (e.g., 80 for 80% of quota limit). Breaching this budget triggers an automated RPM quota increase support case, if enable_automated_support_case is set to Yes. Ignored when requests_per_minute_quota_code is NA |
| tokens_per_minute_threshold_percent | 80 | TPM threshold as percentage of quota (e.g., 80 for 80% of quota limit). Breaching this budget triggers an automated TPM quota increase support case, if enable_automated_support_case is set to Yes |
| latency_threshold_ms | 240000 | Latency threshold in milliseconds (Min: 15000) |

**Automated Support Case Configuration**

| Variable | Default | Description |
|----------|---------|-------------|
| enable_automated_support_case | Yes | Enable automated support case creation (Yes/No). Updatable via Parameter Store post-deployment |
| support_case_lookback_days | 60 | Number of days to look back when checking for existing unresolved support cases (duplicate detection). Allowed values: 45, 60, 90 |
| use_case_description | Enterprise production workload serving real-time AI-powered features to end users. | Brief description of the use case for quota increase justification. Included in automated support cases. Updatable via Parameter Store |

**Automated Threshold Update Configuration**

| Variable | Default | Description |
|----------|---------|-------------|
| enable_automated_threshold_update | Yes | Enable automated threshold updates based on quota changes (Yes/No). |
| threshold_update_schedule_interval_days | 1 | How often to check and update alarm thresholds in days (1-30). Recommended: 1 for daily checks and update. |

**Layer 1: Critical Error Detection Alarm Configuration**

| Variable | Default | Description |
|----------|---------|-------------|
| error_threshold | 5 | Error count threshold per evaluation period. 0 = immediate alert on any error. Default: 5. Example: >5 errors/min (say 6) × 5 evaluation periods = 30+ errors in 5 minutes will trigger the critical alarm |
| critical_alarm_evaluation_periods | 5 | Consecutive periods before critical alarm. 5 with 60s period = sustained errors for 5 minutes |

**Layer 2: Usage Rate Monitoring Alarm Configuration**

| Variable | Default | Description |
|----------|---------|-------------|
| warning_alarm_evaluation_periods | 5 | Consecutive periods for warning alarm (Min: 1). Default: 5. Example: usage exceeding threshold for 5 consecutive minutes will trigger the warning alarm |
| latency_alarm_period | 300 | Latency check period in seconds (300 = 5 min) |
| latency_alarm_evaluation_periods | 2 | Consecutive latency checks before alert (Min: 1). Default: 2. Example: sustained high latency for 10 minutes (2 × 300s) will trigger the alarm |

**Layer 3: Anomaly Detection Alarm Configuration**

| Variable | Default | Description |
|----------|---------|-------------|
| anomaly_detection_period | 300 | Anomaly check period in seconds (300 = 5 min) |
| anomaly_evaluation_periods | 12 | Consecutive anomaly periods before alert (Min: 1). Default: 12. Example: 60 minutes of sustained anomaly (12 × 300s) will trigger the alarm |
| anomaly_sensitivity | 9 | Anomaly sensitivity. 1 = most sensitive, 10 = least sensitive |

**Common Alarm Configuration**

| Variable | Default | Description |
|----------|---------|-------------|
| alarm_evaluation_period | 60 | Alarm check period in seconds (60 = 1 min) |

## Step 6: Verify deployment outputs

After successful deployment, review the outputs:

```bash
terraform output
```

Review the key outputs, including the composite alarm name, notification topic ARN, and calculated thresholds.

## Step 7: Confirm email subscriptions

After deployment, stakeholders receive SNS confirmation emails. Each recipient must choose the confirmation link to receive alerts.

## Step 8: Test alarm notifications

To validate the notification workflow, trigger a test alarm. Replace `AcmeCorp` and `G-Opus-4-6` with the `customer_name` and `bedrock_model_name` values used during deployment:

```bash
aws cloudwatch set-alarm-state \
  --alarm-name AcmeCorp-Bedrock-HighInvocationRate-Warning-G-Opus-4-6 \
  --state-value ALARM \
  --state-reason "Testing notification workflow"
```

Verify that email notifications are received and Lambda functions run successfully.

## Clean up

When you no longer need this solution, complete the following steps to delete the AWS resources and avoid ongoing charges to your account:

1. Destroy all Terraform-managed resources. For each workspace (model), select it and destroy:

```bash
# List all deployed models
terraform workspace list

# Destroy each model (use the same tfvars used during deployment)
terraform workspace select g-opus-4-6
terraform destroy -var-file="g-opus-4-6.tfvars"

terraform workspace select deepseek
terraform destroy -var-file="deepseek.tfvars"
```

After destroying all models, clean up workspaces:

```bash
terraform workspace select default
terraform workspace delete g-opus-4-6
terraform workspace delete deepseek
```

The destroy removes all Lambda functions, CloudWatch alarms, SNS topics, Secrets Manager secrets, Parameter Store parameters, IAM roles, and EventBridge rules.

Complete the following manual cleanup steps for resources not managed by Terraform:

Note: These actions are irreversible. The S3 bucket contents and Lambda CloudWatch log groups will be permanently deleted.

2. Delete the S3 bucket contents:

```bash
# Delete S3 bucket contents
aws s3 rm s3://$BUCKET_NAME --recursive
```

3. Delete the S3 bucket:

```bash
# Delete S3 bucket
aws s3 rb s3://$BUCKET_NAME
```

4. Delete the Parameter Store threshold parameters. In the following cleanup commands, replace `AcmeCorp` and `G-Opus-4-6` with the `customer_name` and `bedrock_model_name` values used during deployment:

```bash
# Delete threshold parameters (created by Lambda at runtime, not managed by Terraform)
aws ssm delete-parameters --names \
  "/AcmeCorp/bedrock/quota-monitoring/G-Opus-4-6/thresholds/rpm-threshold-calculated" \
  "/AcmeCorp/bedrock/quota-monitoring/G-Opus-4-6/thresholds/tpm-threshold-calculated" \
  "/AcmeCorp/bedrock/quota-monitoring/G-Opus-4-6/thresholds/last-updated" \
  "/AcmeCorp/bedrock/quota-monitoring/G-Opus-4-6/resolved-model-id-display"
```

5. (Optional) Delete the CloudWatch log groups:

```bash
# Delete CloudWatch log groups (optional)
aws logs delete-log-group --log-group-name /aws/lambda/AcmeCorp-Quota-Calculator-G-Opus-4-6
aws logs delete-log-group --log-group-name /aws/lambda/AcmeCorp-Alarm-Updater-G-Opus-4-6
aws logs delete-log-group --log-group-name /aws/lambda/AcmeCorp-Bedrock-Notification-Processor-G-Opus-4-6
```
