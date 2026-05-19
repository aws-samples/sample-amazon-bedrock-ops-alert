# Amazon Bedrock Ops Alert: Deployment Guide

This guide walks you through deploying Amazon Bedrock Ops Alert in your own environment, including prerequisites, packaging, stack deployment, testing, and cleanup.

## Prerequisites

Before deploying the solution, confirm you have the following:

**AWS account requirements:**

- Active Amazon Bedrock usage with established quotas for your target model
- AWS Business or Enterprise Support plan (required for automated support case creation through the Support API)
- AWS Identity and Access Management (IAM) permissions to create AWS CloudFormation stacks and associated resources

**Required information:**

- S3 bucket for Lambda deployment packages
- Stakeholder email addresses for alert notifications
- Service Quota codes for your target Bedrock model (RPM and TPM). Find codes at the [Service Quotas console](https://console.aws.amazon.com/servicequotas/home/services/bedrock/quotas)

**Tools:**

- AWS CLI configured with appropriate credentials
- Zip utility for packaging Lambda functions

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

The solution uses two Lambda deployment packages. The Lambda layer contains shared quota calculation utilities used by multiple functions, eliminating code duplication and maintaining consistent behavior.

```bash
# Package the notification processor Lambda
cd code/lambda
zip notification_processor.zip notification_processor.py

# Package the quota utils Lambda layer
cd ../quota_utils_layer
zip -r ../quota_utils_layer.zip python/

# Return to project root
cd ../..
```

## Step 4: Upload packages to S3

```bash
aws s3 cp code/lambda/notification_processor.zip s3://$BUCKET_NAME/
aws s3 cp code/quota_utils_layer.zip s3://$BUCKET_NAME/
```

## Step 5: Upload the template and deploy the CloudFormation stack

Upload the template to S3 and deploy using a presigned URL.

Before running the deploy command, replace the following placeholder values with your own:

- `YourCompany`: Your customer identifier (max 10 characters). This value appears in resource names, alarm prefixes, Parameter Store paths, and Lambda function names.
- `email1@example.com`, `email2@example.com` : Your stakeholder email addresses

If you are monitoring a model other than the example (Claude Opus 4.6), also update the following parameters in the deploy command:

- `--stack-name`(CLI argument): Stack name reflecting your model (for example, bedrock-ops-alert-sonnet-4-5)
- `BedrockModelName`: Short model name for resource naming (max 15 characters)
- `BedrockModelId`: Bedrock model identifier
- `GeoDataResidencyRequirement` : Set to Yes if your workload has geographic data residency requirements that prevent using Global Cross Region Inference
- `InputModalities`: Input modalities used by the model
- `RequestsPerMinuteQuotaCode` : Your model-specific RPM quota code from the [Service Quotas console](https://console.aws.amazon.com/servicequotas/home/services/bedrock/quotas)
- `TokensPerMinuteQuotaCode`: Your model-specific TPM quota code from the [Service Quotas console](https://console.aws.amazon.com/servicequotas/home/services/bedrock/quotas)

For example, if you set `CustomerName` to `Acme` and `BedrockModelName` to `G-Opus-4-6`, your alarm names follow the pattern `Acme-Bedrock-*-G-Opus-4-6`, your Parameter Store paths follow `/Acme/bedrock/quota-monitoring/G-Opus-4-6/`, and your Lambda function names follow `Acme-Bedrock-Notification-Processor-G-Opus-4-6`.

**Note:** The default values for the following parameters have been validated in production environments and are recommended for most deployments: SupportCaseLookbackDays, TokensPerMinuteIncreasePercent, RequestsPerMinuteIncreasePercent, ErrorThreshold, CriticalAlarmEvaluationPeriods, RequestsPerMinuteThresholdPercent, TokensPerMinuteThresholdPercent, LatencyThresholdMs, WarningAlarmEvaluationPeriods, LatencyAlarmPeriod, LatencyAlarmEvaluationPeriods, AnomalyDetectionPeriod, AnomalyEvaluationPeriods, AnomalySensitivity, AlarmEvaluationPeriod, and ThresholdUpdateScheduleIntervalDays. You can adjust these values to match your specific workload characteristics, but the defaults provide a balanced configuration that minimizes false positives while detecting genuine operational issues.

```bash
# Upload template to S3
aws s3 cp code/bedrock-quota-alarm.yml s3://$BUCKET_NAME/

# Generate presigned URL for template
TEMPLATE_URL=$(aws s3 presign s3://$BUCKET_NAME/bedrock-quota-alarm.yml --expires-in 3600)

# Deploy the stack
aws cloudformation create-stack \
  --stack-name bedrock-ops-alert-g-opus-4-6 \
  --template-url "$TEMPLATE_URL" \
  --parameters \
 ParameterKey=LambdaS3Bucket,ParameterValue=$BUCKET_NAME \
 ParameterKey=LambdaS3Key,ParameterValue=notification_processor.zip \
 ParameterKey=CustomerName,ParameterValue=YourCompany \
 'ParameterKey=StakeholderEmailList,ParameterValue=email1@example.com\,email2@example.com' \
 ParameterKey=NotificationPreference,ParameterValue=all \
 ParameterKey=BedrockModelName,ParameterValue=G-Opus-4-6 \
 ParameterKey=BedrockModelId,ParameterValue=global.anthropic.claude-opus-4-6-v1 \
 ParameterKey=GeoDataResidencyRequirement,ParameterValue=No \
 'ParameterKey=InputModalities,ParameterValue=TEXT and IMAGE' \
 ParameterKey=EnableAutomatedSupportCase,ParameterValue=Yes \
 ParameterKey=SupportCaseLookbackDays,ParameterValue=60 \
 'ParameterKey=UseCaseDescription,ParameterValue=Enterprise production workload serving real-time AI-powered features to end users.' \
 ParameterKey=TokensPerMinuteIncreasePercent,ParameterValue=25 \
 ParameterKey=TokensPerMinuteQuotaCode,ParameterValue=L-3DCCFAA4 \
 ParameterKey=RequestsPerMinuteIncreasePercent,ParameterValue=25 \
 ParameterKey=RequestsPerMinuteQuotaCode,ParameterValue=L-3DD46812 \
 ParameterKey=ErrorThreshold,ParameterValue=5 \
 ParameterKey=CriticalAlarmEvaluationPeriods,ParameterValue=5 \
 ParameterKey=RequestsPerMinuteThresholdPercent,ParameterValue=80 \
 ParameterKey=TokensPerMinuteThresholdPercent,ParameterValue=80 \
 ParameterKey=LatencyThresholdMs,ParameterValue=240000 \
 ParameterKey=WarningAlarmEvaluationPeriods,ParameterValue=5 \
 ParameterKey=LatencyAlarmPeriod,ParameterValue=300 \
 ParameterKey=LatencyAlarmEvaluationPeriods,ParameterValue=2 \
 ParameterKey=AnomalyDetectionPeriod,ParameterValue=300 \
 ParameterKey=AnomalyEvaluationPeriods,ParameterValue=12 \
 ParameterKey=AnomalySensitivity,ParameterValue=9 \
 ParameterKey=AlarmEvaluationPeriod,ParameterValue=60 \
 ParameterKey=EnableAutomatedThresholdUpdate,ParameterValue=Yes \
 ParameterKey=ThresholdUpdateScheduleIntervalDays,ParameterValue=1 \
  --capabilities CAPABILITY_NAMED_IAM \
  --tags Key=Solution,Value=Amazon-Bedrock-Ops-Alert
```

The following tables describe all CloudFormation parameters organized by configuration group.

**General Configuration**

| Parameter | Default | Description |
|-----------|---------|-------------|
| CustomerName | | Your company or team identifier for resource naming (max 10 characters). Used in alarm names, Parameter Store paths, and Lambda function names. |
| StakeholderEmailList | | Comma-separated list of stakeholder emails for alerts |
| NotificationPreference | all | Email notification filter: all, critical, or warning. Updatable via Parameter Store post-deployment |

**Lambda Deployment Configuration**

| Parameter | Default | Description |
|-----------|---------|-------------|
| LambdaS3Bucket | | S3 bucket name containing the Lambda deployment package |
| LambdaS3Key | notification_processor.zip | S3 key (path) to the Lambda deployment package zip file |

**Model Specification**

| Parameter | Default | Description |
|-----------|---------|-------------|
| BedrockModelName | G-Opus-4-6 | Short model name for resource naming (max 15 characters) |
| BedrockModelId | global.anthropic.claude-opus-4-6-v1 | Bedrock model identifier being monitored |
| GeoDataResidencyRequirement | No | Geographic data residency requirement (Yes/No/NA). If Yes, Global Cross Region Inference cannot be considered |
| InputModalities | TEXT and IMAGE | Input modalities used by the model (TEXT and IMAGE, TEXT Only, IMAGE Only) |

**Model Quota Configuration**

| Parameter | Default | Description |
|-----------|---------|-------------|
| RequestsPerMinuteQuotaCode | L-3DD46812 | Set to NA if the model has no RPM quota; otherwise provide the RPM quota code, required when RequestsPerMinuteIncreasePercent > 0 |
| RequestsPerMinuteIncreasePercent | 25 | RPM quota increase % (0, 25, 50, 75, 100). Set 0 to skip or when Requests Per Minute Quota Code is NA. Updatable via Parameter Store |
| TokensPerMinuteQuotaCode | L-3DCCFAA4 | TPM quota code. Required if TokensPerMinuteIncreasePercent > 0 |
| TokensPerMinuteIncreasePercent | 25 | TPM quota increase % (0, 25, 50, 75, 100). Set 0 to skip. Updatable via Parameter Store |

**Model Usage Budget (Threshold)**

| Parameter | Default | Description |
|-----------|---------|-------------|
| RequestsPerMinuteThresholdPercent | 80 | RPM threshold as percentage of quota (e.g., 80 for 80% of quota limit). Breaching this budget triggers an automated RPM quota increase support case, if Enable Automated Support Case is set to Yes. Ignored when Requests Per Minute Quota Code is NA |
| TokensPerMinuteThresholdPercent | 80 | TPM threshold as percentage of quota (e.g., 80 for 80% of quota limit). Breaching this budget triggers an automated TPM quota increase support case, if Enable Automated Support Case is set to Yes |
| LatencyThresholdMs | 240000 | Latency threshold in milliseconds (Min: 15000) |

**Automated Support Case Configuration**

| Parameter | Default | Description |
|-----------|---------|-------------|
| EnableAutomatedSupportCase | Yes | Enable automated support case creation (Yes/No). Updatable via Parameter Store post-deployment |
| SupportCaseLookbackDays | 60 | Number of days to look back when checking for existing unresolved support cases (duplicate detection). Allowed values: 45, 60, 90 |
| UseCaseDescription | Enterprise production workload serving real-time AI-powered features to end users. | Brief description of the use case for quota increase justification. Included in automated support cases. Updatable via Parameter Store |

**Automated Threshold Update Configuration**

| Parameter | Default | Description |
|-----------|---------|-------------|
| EnableAutomatedThresholdUpdate | Yes | Enable automated threshold updates based on quota changes (Yes/No). |
| ThresholdUpdateScheduleIntervalDays | 1 | How often to check and update alarm thresholds in days (1-30). Recommended: 1 for daily checks and update. |

**Layer 1: Critical Error Detection Alarm Configuration**

| Parameter | Default | Description |
|-----------|---------|-------------|
| ErrorThreshold | 5 | Error count threshold per evaluation period. 0 = immediate alert on any error. Default: 5. Example: >5 errors/min (say 6) × 5 evaluation periods = 30+ errors in 5 minutes will trigger the critical alarm |
| CriticalAlarmEvaluationPeriods | 5 | Consecutive periods before critical alarm. 5 with 60s period = sustained errors for 5 minutes |

**Layer 2: Usage Rate Monitoring Alarm Configuration**

| Parameter | Default | Description |
|-----------|---------|-------------|
| WarningAlarmEvaluationPeriods | 5 | Consecutive periods for warning alarm (Min: 1). Default: 5. Example: usage exceeding threshold for 5 consecutive minutes will trigger the warning alarm |
| LatencyAlarmPeriod | 300 | Latency check period in seconds (300 = 5 min) |
| LatencyAlarmEvaluationPeriods | 2 | Consecutive latency checks before alert (Min: 1). Default: 2. Example: sustained high latency for 10 minutes (2 × 300s) will trigger the alarm |

**Layer 3: Anomaly Detection Alarm Configuration**

| Parameter | Default | Description |
|-----------|---------|-------------|
| AnomalyDetectionPeriod | 300 | Anomaly check period in seconds (300 = 5 min) |
| AnomalyEvaluationPeriods | 12 | Consecutive anomaly periods before alert (Min: 1). Default: 12. Example: 60 minutes of sustained anomaly (12 × 300s) will trigger the alarm |
| AnomalySensitivity | 9 | Anomaly sensitivity. 1 = most sensitive, 10 = least sensitive |

**Common Alarm Configuration**

| Parameter | Default | Description |
|-----------|---------|-------------|
| AlarmEvaluationPeriod | 60 | Alarm check period in seconds (60 = 1 min) |

## Step 6: Monitor stack deployment

Monitor the CloudFormation stack creation progress:

1. Open the [AWS CloudFormation console](https://console.aws.amazon.com/cloudformation/).
2. Select your stack (`bedrock-ops-alert-g-opus-4-6`).
3. Choose the **Events** tab. The Events tab displays the deployment progress.
4. Wait for the stack status to change to `CREATE_COMPLETE`.

The deployment typically takes 3–5 minutes. If the stack fails, review the Events tab for error details.

Alternatively, monitor deployment progress using the AWS CLI:

```bash
aws cloudformation describe-stacks \
  --stack-name bedrock-ops-alert-g-opus-4-6 \
  --query 'Stacks[0].StackStatus' \
  --output text
```

## Step 7: Verify stack outputs

After successful deployment, review the stack outputs:

1. In the CloudFormation console, select your stack.
2. Choose the **Outputs** tab.
3. Review the key outputs, including the composite alarm name, notification topic ARN, and calculated thresholds.

Alternatively, retrieve outputs using the AWS CLI:

```bash
aws cloudformation describe-stacks \
  --stack-name bedrock-ops-alert-g-opus-4-6 \
  --query 'Stacks[0].Outputs'
```

## Step 8: Confirm email subscriptions

After stack deployment, stakeholders receive SNS confirmation emails. Each recipient must choose the confirmation link to receive alerts.

## Step 9: Test alarm notifications

To validate the notification workflow, trigger a test alarm:

```bash
aws cloudwatch set-alarm-state \
  --alarm-name YourCompany-Bedrock-HighInvocationRate-Warning-G-Opus-4-6 \
  --state-value ALARM \
  --state-reason "Testing notification workflow"
```

Verify that email notifications are received and Lambda functions run successfully.

## Clean up

When you no longer need this solution, complete the following steps to delete the AWS resources and avoid ongoing charges to your account:

1. Delete the CloudFormation stack:

```bash
aws cloudformation delete-stack --stack-name bedrock-ops-alert-g-opus-4-6
```

The stack deletion removes all Lambda functions, CloudWatch alarms, SNS topics, Secrets Manager secrets, Parameter Store parameters, IAM roles, and EventBridge rules.

Complete the following manual cleanup steps for resources not managed by CloudFormation:

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

4. Delete the Parameter Store threshold parameters. In the following cleanup commands, replace `YourCompany` and `G-Opus-4-6` with the `CustomerName` and `BedrockModelName` values used during deployment:

```bash
# Delete threshold parameters (not managed by CloudFormation)
aws ssm delete-parameters --names \
  "/YourCompany/bedrock/quota-monitoring/G-Opus-4-6/thresholds/rpm-threshold-calculated" \
  "/YourCompany/bedrock/quota-monitoring/G-Opus-4-6/thresholds/tpm-threshold-calculated" \
  "/YourCompany/bedrock/quota-monitoring/G-Opus-4-6/thresholds/last-updated"
```

5. (Optional) Delete the CloudWatch log groups:

```bash
# Delete CloudWatch log groups (optional)
aws logs delete-log-group --log-group-name /aws/lambda/YourCompany-Bedrock-Notification-Processor-G-Opus-4-6
```
