# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# =============================================================================
# Outputs — 1:1 from CloudFormation Outputs (22 outputs)
# =============================================================================

# --- Monitored Model ---
output "monitored_model_id" {
  description = "Bedrock model being monitored"
  value       = var.bedrock_model_id
}

# --- Encryption ---
output "bedrock_ops_alert_encryption_key_id" {
  description = "KMS key ID for Bedrock Ops Alert encryption"
  value       = aws_kms_key.bedrock_ops_alert.key_id
}

output "bedrock_ops_alert_encryption_key_arn" {
  description = "KMS key ARN for Bedrock Ops Alert encryption"
  value       = aws_kms_key.bedrock_ops_alert.arn
}

output "bedrock_ops_alert_encryption_key_alias" {
  description = "KMS key alias for Bedrock Ops Alert encryption"
  value       = aws_kms_alias.bedrock_ops_alert.name
}

# --- SNS Topics ---
output "raw_alarm_topic_arn" {
  description = "Raw alarm topic ARN (Lambda only)"
  value       = aws_sns_topic.raw_alarm.arn
}

output "formatted_notification_topic_arn" {
  description = "Formatted notification topic ARN (Email subscribers)"
  value       = aws_sns_topic.formatted_notification.arn
}

# --- Composite Alarm ---
output "composite_alarm_name" {
  description = "Composite alarm for overall quota health"
  value       = aws_cloudwatch_composite_alarm.quota_health.alarm_name
}

# --- Calculated Thresholds ---
output "calculated_rpm_threshold" {
  description = "Calculated RPM threshold based on quota and percentage"
  value       = local.rpm_threshold
  sensitive   = true
}

output "calculated_tpm_threshold" {
  description = "Calculated combined TPM threshold (used by HighTPMQuotaUsage alarm and usage validation)"
  value       = local.tpm_threshold
  sensitive   = true
}

# --- Layer 1: Critical Error Detection Configuration ---
output "error_threshold" {
  description = "Configured error count threshold per evaluation period"
  value       = var.error_threshold
}

output "critical_alarm_evaluation_periods" {
  description = "Configured consecutive periods before critical alarm triggers"
  value       = var.critical_alarm_evaluation_periods
}

# --- Layer 2: Usage Rate Monitoring Configuration ---
output "warning_alarm_evaluation_periods" {
  description = "Configured consecutive periods before warning alarm triggers"
  value       = var.warning_alarm_evaluation_periods
}

output "alarm_evaluation_period" {
  description = "Configured alarm evaluation period in seconds"
  value       = var.alarm_evaluation_period
}

output "latency_threshold_ms" {
  description = "Configured latency threshold in milliseconds"
  value       = var.latency_threshold_ms
}

# --- Layer 3: Anomaly Detection Configuration ---
output "anomaly_sensitivity" {
  description = "Configured anomaly detection sensitivity (1=most sensitive, 10=least)"
  value       = var.anomaly_sensitivity
}

output "anomaly_evaluation_periods" {
  description = "Configured consecutive anomaly periods before alarm triggers"
  value       = var.anomaly_evaluation_periods
}

# --- Automated Threshold Updates ---
output "alarm_updater_function_name" {
  description = "Lambda function name for manually updating alarms after quota changes"
  value       = aws_lambda_function.alarm_updater.function_name
}

output "threshold_update_schedule_enabled" {
  description = "Whether automated threshold updates are enabled"
  value       = var.enable_automated_threshold_update
}

output "threshold_update_interval" {
  description = "Automated threshold update interval in days"
  value       = var.threshold_update_schedule_interval_days
}

# --- Parameter Store Paths (updatable without stack updates) ---
output "notification_preference_parameter" {
  description = "Parameter Store path for notification preference"
  value       = aws_ssm_parameter.notification_preference.name
}

output "enable_automated_support_case_parameter" {
  description = "Parameter Store path for automated support case enablement"
  value       = aws_ssm_parameter.enable_automated_support_case.name
}

output "tokens_per_minute_increase_percent_parameter" {
  description = "Parameter Store path for tokens per minute increase percentage"
  value       = aws_ssm_parameter.tokens_per_minute_increase_percent.name
}

output "requests_per_minute_increase_percent_parameter" {
  description = "Parameter Store path for requests per minute increase percentage"
  value       = aws_ssm_parameter.requests_per_minute_increase_percent.name
}
