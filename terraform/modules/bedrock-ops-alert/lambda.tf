# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# =============================================================================
# Lambda Layer — shared quota calculation utilities
# =============================================================================

resource "aws_lambda_layer_version" "quota_utils" {
  layer_name               = "${var.customer_name}-Quota-Utils-Layer-${var.bedrock_model_name}"
  description              = "Shared utilities for quota calculation and threshold management"
  s3_bucket                = var.lambda_s3_bucket
  s3_key                   = "quota_utils_layer.zip"
  compatible_runtimes      = ["python3.14"]
  compatible_architectures = ["arm64"]
}

# =============================================================================
# Lambda: QuotaCalculator — calculates alarm thresholds from Service Quotas
# Source: code/lambda/quota_calculator.py (shared with CloudFormation deployment)
# =============================================================================

resource "aws_lambda_function" "quota_calculator" {
  function_name = "${var.customer_name}-Quota-Calculator-${var.bedrock_model_name}"
  role          = aws_iam_role.quota_calculator.arn
  handler       = "quota_calculator.handler"
  runtime       = "python3.14"
  timeout       = 300
  architectures = ["arm64"]
  s3_bucket     = var.lambda_s3_bucket
  s3_key        = "quota_calculator.zip"
  layers        = [aws_lambda_layer_version.quota_utils.arn]
}

# =============================================================================
# Lambda: AlarmUpdater — updates alarms when quotas change (EventBridge trigger)
# =============================================================================

resource "aws_lambda_function" "alarm_updater" {
  function_name = "${var.customer_name}-Alarm-Updater-${var.bedrock_model_name}"
  role          = aws_iam_role.alarm_updater.arn
  handler       = "alarm_updater.handler"
  runtime       = "python3.14"
  timeout       = 300
  architectures = ["arm64"]
  s3_bucket     = var.lambda_s3_bucket
  s3_key        = "alarm_updater.zip"
  layers        = [aws_lambda_layer_version.quota_utils.arn]

  environment {
    variables = {
      CUSTOMER_NAME          = var.customer_name
      BEDROCK_MODEL_NAME     = var.bedrock_model_name
      RPM_QUOTA_CODE         = var.requests_per_minute_quota_code
      TPM_QUOTA_CODE         = var.tokens_per_minute_quota_code
      RPM_THRESHOLD_PERCENT  = tostring(var.requests_per_minute_threshold_percent)
      TPM_THRESHOLD_PERCENT  = tostring(var.tokens_per_minute_threshold_percent)
      INFERENCE_PROFILE_TYPE = var.inference_profile_type
      BEDROCK_MODEL_ID       = var.bedrock_model_id
    }
  }
}

# =============================================================================
# Lambda: NotificationProcessor — processes raw alarms, creates support cases, sends emails
# =============================================================================

resource "aws_lambda_function" "notification_processor" {
  function_name = "${var.customer_name}-Bedrock-Notification-Processor-${var.bedrock_model_name}"
  role          = aws_iam_role.notification_processor.arn
  handler       = "notification_processor.handler"
  runtime       = "python3.14"
  timeout       = 600
  architectures = ["arm64"]
  s3_bucket     = var.lambda_s3_bucket
  s3_key        = "notification_processor.zip"
  layers        = [aws_lambda_layer_version.quota_utils.arn]

  environment {
    variables = {
      CUSTOMER_NAME_SECRET                       = aws_secretsmanager_secret.customer_name.name
      BEDROCK_MODEL_ID                           = var.bedrock_model_id
      BEDROCK_MODEL_ID_DISPLAY                   = data.aws_ssm_parameter.resolved_model_id_display.value
      INFERENCE_PROFILE_TYPE                     = var.inference_profile_type
      BEDROCK_MODEL_NAME                         = var.bedrock_model_name
      GEO_DATA_RESIDENCY_REQUIREMENT             = var.geo_data_residency_requirement
      INPUT_MODALITIES                           = var.input_modalities
      STAKEHOLDER_EMAILS_SECRET                  = aws_secretsmanager_secret.stakeholder_emails.name
      NOTIFICATION_PREFERENCE_PARAM              = aws_ssm_parameter.notification_preference.name
      FORMATTED_TOPIC_ARN                        = aws_sns_topic.formatted_notification.arn
      ENABLE_AUTOMATED_SUPPORT_CASE_PARAM        = aws_ssm_parameter.enable_automated_support_case.name
      USE_CASE_DESCRIPTION_PARAM                 = aws_ssm_parameter.use_case_description.name
      TOKENS_PER_MINUTE_INCREASE_PERCENT_PARAM   = aws_ssm_parameter.tokens_per_minute_increase_percent.name
      REQUESTS_PER_MINUTE_INCREASE_PERCENT_PARAM = aws_ssm_parameter.requests_per_minute_increase_percent.name
      SUPPORT_CASE_LOOKBACK_DAYS                 = tostring(var.support_case_lookback_days)
      AUTOMATION_REPLY_MAX_ATTEMPTS              = "120"
      REQUESTS_PER_MINUTE_QUOTA_CODE             = var.requests_per_minute_quota_code
      TOKENS_PER_MINUTE_QUOTA_CODE               = var.tokens_per_minute_quota_code
      ELIGIBLE_ALARM_PATTERNS                    = "ServerErrors-Critical,Throttles-Critical,ClientErrors-Critical,HighLatency-Warning,LatencyAnomaly-Warning,HighInvocationRate-Warning,HighTPMQuotaUsage-Warning,InvocationAnomaly-Warning,InputTokenAnomaly-Warning,OutputTokenAnomaly-Warning"
      RPM_ALARM_PATTERNS                         = "HighInvocationRate,InvocationAnomaly"
      TPM_ALARM_PATTERNS                         = "HighTPMQuotaUsage,InputTokenAnomaly,OutputTokenAnomaly"
    }
  }
}

# =============================================================================
# Custom Resource Replacement: QuotaCalculator invocation at deploy time
# =============================================================================

resource "aws_lambda_invocation" "quota_calculator" {
  function_name = aws_lambda_function.quota_calculator.function_name

  input = jsonencode({
    RequestType       = "Create"
    StackId           = "arn:aws:cloudformation:${local.region}:${local.account_id}:stack/terraform-managed/00000000"
    RequestId         = "terraform-quota-calculator"
    LogicalResourceId = "QuotaCalculatorCustomResource"
    ResourceProperties = {
      RequestsPerMinuteQuotaCode        = var.requests_per_minute_quota_code
      TokensPerMinuteQuotaCode          = var.tokens_per_minute_quota_code
      RequestsPerMinuteThresholdPercent = tostring(var.requests_per_minute_threshold_percent)
      TokensPerMinuteThresholdPercent   = tostring(var.tokens_per_minute_threshold_percent)
      CustomerName                      = var.customer_name
      BedrockModelName                  = var.bedrock_model_name
      BedrockModelId                    = var.bedrock_model_id
      InferenceProfileType              = var.inference_profile_type
    }
  })

  depends_on = [aws_lambda_function.quota_calculator]

  lifecycle {
    postcondition {
      condition     = try(jsondecode(self.result)["Status"] == "SUCCESS", true)
      error_message = "QuotaCalculator Lambda failed: ${try(jsondecode(self.result)["Reason"], "Check Lambda logs for details")}"
    }
  }
}

# =============================================================================
# Email Subscriptions — native Terraform resource (replaces EmailSubscriptionManager Lambda)
# =============================================================================

resource "aws_sns_topic_subscription" "email" {
  for_each  = toset(var.stakeholder_email_list)
  topic_arn = aws_sns_topic.formatted_notification.arn
  protocol  = "email"
  endpoint  = each.value
}

# =============================================================================
# SNS Subscription: Raw alarm topic → NotificationProcessor Lambda
# =============================================================================

resource "aws_sns_topic_subscription" "raw_alarm_lambda" {
  topic_arn = aws_sns_topic.raw_alarm.arn
  protocol  = "lambda"
  endpoint  = aws_lambda_function.notification_processor.arn
}

resource "aws_lambda_permission" "raw_alarm_invoke" {
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.notification_processor.function_name
  principal     = "sns.amazonaws.com"
  source_arn    = aws_sns_topic.raw_alarm.arn
}

# =============================================================================
# EventBridge: Automated threshold updates (conditional)
# =============================================================================

resource "aws_cloudwatch_event_rule" "threshold_update_schedule" {
  count = var.enable_automated_threshold_update == "Yes" ? 1 : 0

  name                = "${var.customer_name}-Threshold-Update-Schedule-${var.bedrock_model_name}"
  description         = "Automated schedule to update alarm thresholds based on current quotas"
  schedule_expression = var.threshold_update_schedule_interval_days == 1 ? "rate(1 day)" : "rate(${var.threshold_update_schedule_interval_days} days)"
  state               = "ENABLED"
}

resource "aws_cloudwatch_event_target" "alarm_updater" {
  count = var.enable_automated_threshold_update == "Yes" ? 1 : 0

  rule      = aws_cloudwatch_event_rule.threshold_update_schedule[0].name
  target_id = "AlarmUpdaterTarget"
  arn       = aws_lambda_function.alarm_updater.arn
}

resource "aws_lambda_permission" "alarm_updater_event" {
  count = var.enable_automated_threshold_update == "Yes" ? 1 : 0

  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.alarm_updater.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.threshold_update_schedule[0].arn
}
