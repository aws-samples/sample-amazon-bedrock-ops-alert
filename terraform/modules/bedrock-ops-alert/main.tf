# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# =============================================================================
# Amazon Bedrock Ops Alert - Main Terraform Configuration
# 1:1 translation from bedrock-quota-alarm.yml CloudFormation template
# Zero features killed. Zero logic changes. Same AWS resources.
# =============================================================================

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  region     = data.aws_region.current.region
  account_id = data.aws_caller_identity.current.account_id
}

# =============================================================================
# KMS Key — encrypts SNS topics and Secrets Manager secrets
# =============================================================================

resource "aws_kms_key" "bedrock_ops_alert" {
  description             = "KMS key for Bedrock Ops Alert — encrypts SNS topics and Secrets Manager secrets"
  enable_key_rotation     = true
  deletion_window_in_days = 30

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "Enable IAM User Permissions for Key Management"
        Effect = "Allow"
        Principal = {
          AWS = "arn:aws:iam::${local.account_id}:root"
        }
        Action   = ["kms:*"]
        Resource = "*"
      },
      {
        Sid    = "Allow CloudWatch Alarms to Encrypt"
        Effect = "Allow"
        Principal = {
          Service = "cloudwatch.amazonaws.com"
        }
        Action   = ["kms:Decrypt", "kms:GenerateDataKey*"]
        Resource = "*"
      },
      {
        Sid    = "Allow SNS Service to Use Key"
        Effect = "Allow"
        Principal = {
          Service = "sns.amazonaws.com"
        }
        Action   = ["kms:Decrypt", "kms:GenerateDataKey*"]
        Resource = "*"
      }
    ]
  })
}

resource "aws_kms_alias" "bedrock_ops_alert" {
  name          = "alias/${var.customer_name}-bedrock-ops-alert-${var.bedrock_model_name}"
  target_key_id = aws_kms_key.bedrock_ops_alert.key_id
}

# =============================================================================
# Secrets Manager — sensitive data encrypted with shared KMS key
# =============================================================================

resource "aws_secretsmanager_secret" "customer_name" {
  name                    = "${var.customer_name}/bedrock/quota-monitoring/${var.bedrock_model_name}/customer-name"
  kms_key_id              = aws_kms_key.bedrock_ops_alert.arn
  description             = "Customer name for Bedrock quota monitoring"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "customer_name" {
  secret_id     = aws_secretsmanager_secret.customer_name.id
  secret_string = var.customer_name
}

resource "aws_secretsmanager_secret" "stakeholder_emails" {
  name                    = "${var.customer_name}/bedrock/quota-monitoring/${var.bedrock_model_name}/stakeholder-emails"
  kms_key_id              = aws_kms_key.bedrock_ops_alert.arn
  description             = "Stakeholder emails for quota monitoring alerts"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "stakeholder_emails" {
  secret_id     = aws_secretsmanager_secret.stakeholder_emails.id
  secret_string = join(",", var.stakeholder_email_list)
}

# =============================================================================
# SSM Parameters — configuration store (updatable post-deployment)
# =============================================================================

resource "aws_ssm_parameter" "notification_preference" {
  name        = "/${var.customer_name}/bedrock/quota-monitoring/${var.bedrock_model_name}/notification-preference"
  type        = "String"
  value       = var.notification_preference
  description = "Email notification preference: all (Critical and Warning), critical (Critical Only), warning (Warning Only)"
}

resource "aws_ssm_parameter" "inference_profile_type" {
  name        = "/${var.customer_name}/bedrock/quota-monitoring/${var.bedrock_model_name}/inference-profile-type"
  type        = "String"
  value       = var.inference_profile_type
  description = "Inference profile type: System-Defined or Application"
}

resource "aws_ssm_parameter" "enable_automated_support_case" {
  name        = "/${var.customer_name}/bedrock/quota-monitoring/${var.bedrock_model_name}/enable-automated-support-case"
  type        = "String"
  value       = var.enable_automated_support_case
  description = "Enable automated support case creation for quota increases"
}

resource "aws_ssm_parameter" "use_case_description" {
  name        = "/${var.customer_name}/bedrock/quota-monitoring/${var.bedrock_model_name}/use-case-description"
  type        = "String"
  value       = var.use_case_description
  description = "Use case description for quota increase justification"
}

resource "aws_ssm_parameter" "tokens_per_minute_increase_percent" {
  name        = "/${var.customer_name}/bedrock/quota-monitoring/${var.bedrock_model_name}/tokens-per-minute-increase-percent"
  type        = "String"
  value       = tostring(var.tokens_per_minute_increase_percent)
  description = "Percentage increase to request for Tokens Per Minute quota"
}

resource "aws_ssm_parameter" "requests_per_minute_increase_percent" {
  name        = "/${var.customer_name}/bedrock/quota-monitoring/${var.bedrock_model_name}/requests-per-minute-increase-percent"
  type        = "String"
  value       = tostring(var.requests_per_minute_increase_percent)
  description = "Percentage increase to request for Requests Per Minute quota"
}

# =============================================================================
# SNS Topics — Raw (Lambda only) + Formatted (Email subscribers)
# =============================================================================

resource "aws_sns_topic" "raw_alarm" {
  name              = "${var.customer_name}-Bedrock-Alarms-Raw-${var.bedrock_model_name}"
  kms_master_key_id = aws_kms_key.bedrock_ops_alert.key_id
}

resource "aws_sns_topic" "formatted_notification" {
  name              = "${var.customer_name}-Bedrock-Alarms-Formatted-${var.bedrock_model_name}"
  kms_master_key_id = aws_kms_key.bedrock_ops_alert.key_id
}

resource "aws_sns_topic_policy" "raw_alarm" {
  arn = aws_sns_topic.raw_alarm.arn

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowCloudWatchAlarmsToPublish"
        Effect = "Allow"
        Principal = {
          Service = "cloudwatch.amazonaws.com"
        }
        Action   = ["SNS:Publish"]
        Resource = aws_sns_topic.raw_alarm.arn
        Condition = {
          ArnLike = {
            "aws:SourceArn" = "arn:aws:cloudwatch:${local.region}:${local.account_id}:alarm:${var.customer_name}-Bedrock-*"
          }
        }
      }
    ]
  })
}
