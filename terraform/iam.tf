# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# =============================================================================
# IAM Roles & Policies — 1:1 from CloudFormation
# =============================================================================

# --- QuotaCalculator Role ---

resource "aws_iam_role" "quota_calculator" {
  name = "${var.customer_name}-Quota-Calculator-Role-${var.bedrock_model_name}-${local.region}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = { Service = "lambda.amazonaws.com" }
        Action = "sts:AssumeRole"
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "quota_calculator_basic" {
  role       = aws_iam_role.quota_calculator.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "quota_calculator_service_quotas" {
  name = "ServiceQuotasPolicy"
  role = aws_iam_role.quota_calculator.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["servicequotas:GetServiceQuota", "servicequotas:ListServiceQuotas"]
        Resource = "*"
      }
    ]
  })
}

resource "aws_iam_role_policy" "quota_calculator_parameter_store" {
  name = "ParameterStorePolicy"
  role = aws_iam_role.quota_calculator.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["ssm:PutParameter"]
        Resource = [
          "arn:aws:ssm:${local.region}:${local.account_id}:parameter/${var.customer_name}/bedrock/quota-monitoring/${var.bedrock_model_name}/thresholds/*",
          "arn:aws:ssm:${local.region}:${local.account_id}:parameter/${var.customer_name}/bedrock/quota-monitoring/${var.bedrock_model_name}/resolved-model-id-display"
        ]
      }
    ]
  })
}

resource "aws_iam_role_policy" "quota_calculator_bedrock" {
  name = "BedrockInferenceProfilePolicy"
  role = aws_iam_role.quota_calculator.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["bedrock:GetInferenceProfile", "bedrock:ListInferenceProfiles"]
        Resource = "*"
      }
    ]
  })
}

# --- AlarmUpdater Role ---

resource "aws_iam_role" "alarm_updater" {
  name = "${var.customer_name}-Alarm-Updater-Role-${var.bedrock_model_name}-${local.region}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = { Service = "lambda.amazonaws.com" }
        Action = "sts:AssumeRole"
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "alarm_updater_basic" {
  role       = aws_iam_role.alarm_updater.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "alarm_updater_service_quotas" {
  name = "ServiceQuotasPolicy"
  role = aws_iam_role.alarm_updater.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["servicequotas:GetServiceQuota"]
        Resource = "*"
      }
    ]
  })
}

resource "aws_iam_role_policy" "alarm_updater_cloudwatch" {
  name = "CloudWatchAlarmsPolicy"
  role = aws_iam_role.alarm_updater.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = ["cloudwatch:DescribeAlarms", "cloudwatch:PutMetricAlarm"]
        Resource = [
          "arn:aws:cloudwatch:${local.region}:${local.account_id}:alarm:${var.customer_name}-Bedrock-HighInvocationRate-Warning-${var.bedrock_model_name}",
          "arn:aws:cloudwatch:${local.region}:${local.account_id}:alarm:${var.customer_name}-Bedrock-HighTPMQuotaUsage-Warning-${var.bedrock_model_name}"
        ]
      }
    ]
  })
}

resource "aws_iam_role_policy" "alarm_updater_parameter_store" {
  name = "ParameterStorePolicy"
  role = aws_iam_role.alarm_updater.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["ssm:PutParameter", "ssm:GetParameter"]
        Resource = ["arn:aws:ssm:${local.region}:${local.account_id}:parameter/${var.customer_name}/bedrock/quota-monitoring/${var.bedrock_model_name}/thresholds/*"]
      }
    ]
  })
}

resource "aws_iam_role_policy" "alarm_updater_bedrock" {
  name = "BedrockInferenceProfilePolicy"
  role = aws_iam_role.alarm_updater.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["bedrock:GetInferenceProfile", "bedrock:ListInferenceProfiles"]
        Resource = "*"
      }
    ]
  })
}

# --- NotificationProcessor Role ---

resource "aws_iam_role" "notification_processor" {
  name = "${var.customer_name}-Bedrock-Processor-Role-${var.bedrock_model_name}-${local.region}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = { Service = "lambda.amazonaws.com" }
        Action = "sts:AssumeRole"
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "notification_processor_basic" {
  role       = aws_iam_role.notification_processor.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "notification_processor_kms" {
  name = "KMSPolicy"
  role = aws_iam_role.notification_processor.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:DescribeKey", "kms:GenerateDataKey"]
        Resource = [aws_kms_key.bedrock_ops_alert.arn]
      }
    ]
  })
}

resource "aws_iam_role_policy" "notification_processor_sns" {
  name = "SNSPolicy"
  role = aws_iam_role.notification_processor.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["sns:Publish"]
        Resource = [aws_sns_topic.raw_alarm.arn, aws_sns_topic.formatted_notification.arn]
      }
    ]
  })
}

resource "aws_iam_role_policy" "notification_processor_secrets" {
  name = "SecretsManagerPolicy"
  role = aws_iam_role.notification_processor.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = [aws_secretsmanager_secret.customer_name.arn, aws_secretsmanager_secret.stakeholder_emails.arn]
      }
    ]
  })
}

resource "aws_iam_role_policy" "notification_processor_ssm" {
  name = "ParameterStorePolicy"
  role = aws_iam_role.notification_processor.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["ssm:GetParameter", "ssm:GetParameters"]
        Resource = ["arn:aws:ssm:${local.region}:${local.account_id}:parameter/${var.customer_name}/bedrock/quota-monitoring/${var.bedrock_model_name}/*"]
      }
    ]
  })
}

resource "aws_iam_role_policy" "notification_processor_service_quotas" {
  name = "ServiceQuotasPolicy"
  role = aws_iam_role.notification_processor.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["servicequotas:GetServiceQuota", "servicequotas:ListServiceQuotas", "servicequotas:GetRequestedServiceQuotaChange"]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = ["servicequotas:RequestServiceQuotaIncrease"]
        Resource = [
          "arn:aws:servicequotas:${local.region}:${local.account_id}:bedrock/${var.tokens_per_minute_quota_code}",
          "arn:aws:servicequotas:${local.region}:${local.account_id}:bedrock/${var.requests_per_minute_quota_code}"
        ]
        Condition = {
          StringEquals = {
            "servicequotas:service" = "bedrock"
          }
        }
      },
      {
        Effect    = "Allow"
        Action    = ["servicequotas:ListRequestedServiceQuotaChangeHistoryByQuota"]
        Resource  = "*"
        Condition = {
          StringEquals = {
            "servicequotas:service" = "bedrock"
          }
        }
      }
    ]
  })
}

resource "aws_iam_role_policy" "notification_processor_support" {
  name = "SupportPolicy"
  role = aws_iam_role.notification_processor.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["support:CreateCase", "support:AddCommunicationToCase", "support:DescribeCommunications", "support:DescribeCases", "support:DescribeSeverityLevels"]
        Resource = "*"
      }
    ]
  })
}

resource "aws_iam_role_policy" "notification_processor_cloudwatch" {
  name = "CloudWatchPolicy"
  role = aws_iam_role.notification_processor.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["cloudwatch:DescribeAlarms", "cloudwatch:GetMetricStatistics", "cloudwatch:GetMetricData"]
        # Note: DescribeAlarms requires Resource: "*" to retrieve composite alarm state
        # (poll_until_composite_ok). GetMetricStatistics/GetMetricData do not support resource-level permissions.
        # Reference: https://docs.aws.amazon.com/AmazonCloudWatch/latest/APIReference/API_DescribeAlarms.html
        Resource = "*"
      }
    ]
  })
}

resource "aws_iam_role_policy" "notification_processor_bedrock" {
  name = "BedrockInferenceProfilePolicy"
  role = aws_iam_role.notification_processor.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["bedrock:GetInferenceProfile", "bedrock:ListInferenceProfiles"]
        Resource = "*"
      }
    ]
  })
}
