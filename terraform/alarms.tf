# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# =============================================================================
# CloudWatch Alarms — 3-Layer Monitoring
# =============================================================================

# Parse thresholds from Parameter Store (written by QuotaCalculator Lambda during invocation)
data "aws_ssm_parameter" "rpm_threshold" {
  name = "/${var.customer_name}/bedrock/quota-monitoring/${var.bedrock_model_name}/thresholds/rpm-threshold-calculated"

  depends_on = [aws_lambda_invocation.quota_calculator]
}

data "aws_ssm_parameter" "tpm_threshold" {
  name = "/${var.customer_name}/bedrock/quota-monitoring/${var.bedrock_model_name}/thresholds/tpm-threshold-calculated"

  depends_on = [aws_lambda_invocation.quota_calculator]
}

data "aws_ssm_parameter" "resolved_model_id_display" {
  name = "/${var.customer_name}/bedrock/quota-monitoring/${var.bedrock_model_name}/resolved-model-id-display"

  depends_on = [aws_lambda_invocation.quota_calculator]
}

locals {
  rpm_threshold = tonumber(data.aws_ssm_parameter.rpm_threshold.value)
  tpm_threshold = tonumber(data.aws_ssm_parameter.tpm_threshold.value)
}

# =============================================================================
# Layer 1: Critical Quota Breach Detection
# =============================================================================

resource "aws_cloudwatch_metric_alarm" "client_errors" {
  alarm_name          = "${var.customer_name}-Bedrock-ClientErrors-Critical-${var.bedrock_model_name}"
  alarm_description   = "Bedrock model client errors detected - investigation required"
  metric_name         = "InvocationClientErrors"
  namespace           = "AWS/Bedrock"
  statistic           = "Sum"
  period              = var.alarm_evaluation_period
  evaluation_periods  = var.critical_alarm_evaluation_periods
  threshold           = var.error_threshold
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    ModelId = var.bedrock_model_id
  }
}

resource "aws_cloudwatch_metric_alarm" "server_errors" {
  alarm_name          = "${var.customer_name}-Bedrock-ServerErrors-Critical-${var.bedrock_model_name}"
  alarm_description   = "Bedrock model service errors - AWS-side issue detected"
  metric_name         = "InvocationServerErrors"
  namespace           = "AWS/Bedrock"
  statistic           = "Sum"
  period              = var.alarm_evaluation_period
  evaluation_periods  = var.critical_alarm_evaluation_periods
  threshold           = var.error_threshold
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    ModelId = var.bedrock_model_id
  }
}

resource "aws_cloudwatch_metric_alarm" "throttles" {
  alarm_name          = "${var.customer_name}-Bedrock-Throttles-Critical-${var.bedrock_model_name}"
  alarm_description   = "Bedrock model requests throttled - rate limit detected"
  metric_name         = "InvocationThrottles"
  namespace           = "AWS/Bedrock"
  statistic           = "Sum"
  period              = var.alarm_evaluation_period
  evaluation_periods  = var.critical_alarm_evaluation_periods
  threshold           = var.error_threshold
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    ModelId = var.bedrock_model_id
  }
}

# =============================================================================
# Layer 2: Usage Rate Monitoring
# =============================================================================

resource "aws_cloudwatch_metric_alarm" "high_invocation_rate" {
  alarm_name          = "${var.customer_name}-Bedrock-HighInvocationRate-Warning-${var.bedrock_model_name}"
  alarm_description   = "Bedrock model request rate approaching limits"
  evaluation_periods  = var.warning_alarm_evaluation_periods
  comparison_operator = "GreaterThanThreshold"
  threshold           = local.rpm_threshold
  treat_missing_data  = "notBreaching"

  metric_query {
    id          = "m1"
    return_data = false
    metric {
      metric_name = "Invocations"
      namespace   = "AWS/Bedrock"
      period      = var.alarm_evaluation_period
      stat        = "Sum"
      dimensions  = { ModelId = var.bedrock_model_id }
    }
  }

  metric_query {
    id          = "total"
    expression  = "m1"
    label       = "CombinedRPM"
    return_data = true
  }

  depends_on = [aws_lambda_invocation.quota_calculator]
}

resource "aws_cloudwatch_metric_alarm" "high_tpm_quota_usage" {
  alarm_name          = "${var.customer_name}-Bedrock-HighTPMQuotaUsage-Warning-${var.bedrock_model_name}"
  alarm_description   = "Bedrock model estimated TPM quota consumption approaching limits"
  evaluation_periods  = var.warning_alarm_evaluation_periods
  comparison_operator = "GreaterThanThreshold"
  threshold           = local.tpm_threshold
  treat_missing_data  = "notBreaching"

  metric_query {
    id          = "m1"
    return_data = false
    metric {
      metric_name = "EstimatedTPMQuotaUsage"
      namespace   = "AWS/Bedrock"
      period      = var.alarm_evaluation_period
      stat        = "Sum"
      dimensions  = { ModelId = var.bedrock_model_id }
    }
  }

  metric_query {
    id          = "total"
    expression  = "m1"
    label       = "CombinedTPM"
    return_data = true
  }

  depends_on = [aws_lambda_invocation.quota_calculator]
}

resource "aws_cloudwatch_metric_alarm" "high_latency" {
  alarm_name          = "${var.customer_name}-Bedrock-HighLatency-Warning-${var.bedrock_model_name}"
  alarm_description   = "Bedrock model high latency detected - investigation required"
  metric_name         = "InvocationLatency"
  namespace           = "AWS/Bedrock"
  statistic           = "Average"
  period              = var.latency_alarm_period
  evaluation_periods  = var.latency_alarm_evaluation_periods
  threshold           = var.latency_threshold_ms
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    ModelId = var.bedrock_model_id
  }
}

# =============================================================================
# Layer 3: Anomaly Detection
# =============================================================================

resource "aws_cloudwatch_metric_alarm" "invocation_anomaly" {
  alarm_name          = "${var.customer_name}-Bedrock-InvocationAnomaly-Warning-${var.bedrock_model_name}"
  alarm_description   = "Bedrock model unusual request pattern detected"
  comparison_operator = "GreaterThanUpperThreshold"
  evaluation_periods  = var.anomaly_evaluation_periods
  threshold_metric_id = "ad1"
  treat_missing_data  = "notBreaching"

  metric_query {
    id          = "m1"
    return_data = true
    metric {
      metric_name = "Invocations"
      namespace   = "AWS/Bedrock"
      period      = var.anomaly_detection_period
      stat        = "Sum"
      dimensions = {
        ModelId = var.bedrock_model_id
      }
    }
  }

  metric_query {
    id          = "ad1"
    expression  = "ANOMALY_DETECTION_BAND(m1, ${var.anomaly_sensitivity})"
    label       = "Invocations (Expected)"
    return_data = true
  }
}

resource "aws_cloudwatch_metric_alarm" "input_token_anomaly" {
  alarm_name          = "${var.customer_name}-Bedrock-InputTokenAnomaly-Warning-${var.bedrock_model_name}"
  alarm_description   = "Bedrock model unusual input token pattern detected"
  comparison_operator = "GreaterThanUpperThreshold"
  evaluation_periods  = var.anomaly_evaluation_periods
  threshold_metric_id = "ad2"
  treat_missing_data  = "notBreaching"

  metric_query {
    id          = "m2"
    return_data = true
    metric {
      metric_name = "InputTokenCount"
      namespace   = "AWS/Bedrock"
      period      = var.anomaly_detection_period
      stat        = "Sum"
      dimensions = {
        ModelId = var.bedrock_model_id
      }
    }
  }

  metric_query {
    id          = "ad2"
    expression  = "ANOMALY_DETECTION_BAND(m2, ${var.anomaly_sensitivity})"
    label       = "InputTokenCount (Expected)"
    return_data = true
  }
}

resource "aws_cloudwatch_metric_alarm" "output_token_anomaly" {
  alarm_name          = "${var.customer_name}-Bedrock-OutputTokenAnomaly-Warning-${var.bedrock_model_name}"
  alarm_description   = "Bedrock model unusual output token pattern detected"
  comparison_operator = "GreaterThanUpperThreshold"
  evaluation_periods  = var.anomaly_evaluation_periods
  threshold_metric_id = "ad3"
  treat_missing_data  = "notBreaching"

  metric_query {
    id          = "m3"
    return_data = true
    metric {
      metric_name = "OutputTokenCount"
      namespace   = "AWS/Bedrock"
      period      = var.anomaly_detection_period
      stat        = "Sum"
      dimensions = {
        ModelId = var.bedrock_model_id
      }
    }
  }

  metric_query {
    id          = "ad3"
    expression  = "ANOMALY_DETECTION_BAND(m3, ${var.anomaly_sensitivity})"
    label       = "OutputTokenCount (Expected)"
    return_data = true
  }
}

resource "aws_cloudwatch_metric_alarm" "latency_anomaly" {
  alarm_name          = "${var.customer_name}-Bedrock-LatencyAnomaly-Warning-${var.bedrock_model_name}"
  alarm_description   = "Bedrock model unusual latency pattern detected"
  comparison_operator = "GreaterThanUpperThreshold"
  evaluation_periods  = var.anomaly_evaluation_periods
  threshold_metric_id = "ad4"
  treat_missing_data  = "notBreaching"

  metric_query {
    id          = "m4"
    return_data = true
    metric {
      metric_name = "InvocationLatency"
      namespace   = "AWS/Bedrock"
      period      = var.anomaly_detection_period
      stat        = "Average"
      dimensions = {
        ModelId = var.bedrock_model_id
      }
    }
  }

  metric_query {
    id          = "ad4"
    expression  = "ANOMALY_DETECTION_BAND(m4, ${var.anomaly_sensitivity})"
    label       = "InvocationLatency (Expected)"
    return_data = true
  }
}

# =============================================================================
# Composite Alarm — Overall Health
# =============================================================================

resource "aws_cloudwatch_composite_alarm" "quota_health" {
  alarm_name        = "${var.customer_name}-Bedrock-QuotaHealth-Composite-${var.bedrock_model_name}"
  alarm_description = "Overall Bedrock model quota health status"
  actions_enabled   = true
  alarm_actions     = [aws_sns_topic.raw_alarm.arn]

  alarm_rule = join(" OR ", [
    "ALARM(${aws_cloudwatch_metric_alarm.client_errors.alarm_name})",
    "ALARM(${aws_cloudwatch_metric_alarm.server_errors.alarm_name})",
    "ALARM(${aws_cloudwatch_metric_alarm.throttles.alarm_name})",
    "ALARM(${aws_cloudwatch_metric_alarm.high_invocation_rate.alarm_name})",
    "ALARM(${aws_cloudwatch_metric_alarm.high_tpm_quota_usage.alarm_name})",
    "ALARM(${aws_cloudwatch_metric_alarm.high_latency.alarm_name})",
    "ALARM(${aws_cloudwatch_metric_alarm.invocation_anomaly.alarm_name})",
    "ALARM(${aws_cloudwatch_metric_alarm.input_token_anomaly.alarm_name})",
    "ALARM(${aws_cloudwatch_metric_alarm.output_token_anomaly.alarm_name})",
    "ALARM(${aws_cloudwatch_metric_alarm.latency_anomaly.alarm_name})",
  ])

  depends_on = [
    aws_cloudwatch_metric_alarm.client_errors,
    aws_cloudwatch_metric_alarm.server_errors,
    aws_cloudwatch_metric_alarm.throttles,
    aws_cloudwatch_metric_alarm.high_invocation_rate,
    aws_cloudwatch_metric_alarm.high_tpm_quota_usage,
    aws_cloudwatch_metric_alarm.high_latency,
    aws_cloudwatch_metric_alarm.invocation_anomaly,
    aws_cloudwatch_metric_alarm.input_token_anomaly,
    aws_cloudwatch_metric_alarm.output_token_anomaly,
    aws_cloudwatch_metric_alarm.latency_anomaly,
  ]
}
