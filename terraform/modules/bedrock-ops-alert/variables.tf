# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# =============================================================================
# Amazon Bedrock Ops Alert - Terraform Variables
# 1:1 translation from CloudFormation Parameters (32 parameters)
# =============================================================================

# --- General Configuration ---

variable "customer_name" {
  type        = string
  description = "Your company or team identifier for resource naming (max 10 characters). Used in alarm names, Parameter Store paths, and Lambda function names."

  validation {
    condition     = length(var.customer_name) >= 1 && length(var.customer_name) <= 10 && can(regex("^[a-zA-Z0-9-]+$", var.customer_name))
    error_message = "CustomerName must be 1-10 characters, alphanumeric and hyphens only."
  }
}

variable "stakeholder_email_list" {
  type        = list(string)
  description = "List of stakeholder emails for alerts (TAMs, Solutions Architects, customer contacts)"
}

variable "notification_preference" {
  type        = string
  default     = "all"
  description = "Email notification preference: all (Critical and Warning), critical (Critical Only), warning (Warning Only). Stored in Parameter Store, updatable post-deployment"

  validation {
    condition     = contains(["all", "critical", "warning"], var.notification_preference)
    error_message = "NotificationPreference must be one of: all, critical, warning."
  }
}

# --- Lambda Deployment Configuration ---

variable "lambda_s3_bucket" {
  type        = string
  description = "S3 bucket name containing the Lambda deployment package (notification_processor.zip)"

  validation {
    condition     = length(var.lambda_s3_bucket) >= 3 && length(var.lambda_s3_bucket) <= 63
    error_message = "LambdaS3Bucket must be 3-63 characters."
  }
}


# --- Model Specification ---

variable "bedrock_model_name" {
  type        = string
  default     = "G-Opus-4-6"
  description = "Short model name for resource naming (max 15 characters). Alphanumeric characters and hyphens only."

  validation {
    condition     = length(var.bedrock_model_name) >= 1 && length(var.bedrock_model_name) <= 15 && can(regex("^[a-zA-Z0-9-]+$", var.bedrock_model_name))
    error_message = "BedrockModelName must be 1-15 characters, alphanumeric and hyphens only."
  }
}

variable "inference_profile_type" {
  type        = string
  default     = "System-Defined"
  description = "Inference profile type. System-Defined = direct model ID or AWS-managed cross-region profile. Application = customer-created inference profile for cost tracking."

  validation {
    condition     = contains(["System-Defined", "Application"], var.inference_profile_type)
    error_message = "inference_profile_type must be 'System-Defined' or 'Application'."
  }
}

variable "bedrock_model_id" {
  type        = string
  default     = "global.anthropic.claude-opus-4-6-v1"
  description = "Bedrock model/inference profile identifier. For System-Defined: model ID. For Application: short profile ID."
}

variable "geo_data_residency_requirement" {
  type        = string
  default     = "No"
  description = "Geographic data residency requirement (Yes/No/NA). If Yes, Global Cross Region Inference cannot be considered."

  validation {
    condition     = contains(["Yes", "No", "NA"], var.geo_data_residency_requirement)
    error_message = "GeoDataResidencyRequirement must be one of: Yes, No, NA."
  }
}

variable "input_modalities" {
  type        = string
  default     = "TEXT and IMAGE"
  description = "Input modalities used by the model. Included in automated support cases for quota increase context."

  validation {
    condition     = contains(["TEXT and IMAGE", "TEXT Only", "IMAGE Only"], var.input_modalities)
    error_message = "InputModalities must be one of: TEXT and IMAGE, TEXT Only, IMAGE Only."
  }
}

# --- Model Quota Configuration ---

variable "requests_per_minute_quota_code" {
  type        = string
  default     = "L-3DD46812"
  description = "Set to NA if the model has no RPM quota; otherwise provide the RPM quota code, required when RequestsPerMinuteIncreasePercent > 0"
}

variable "requests_per_minute_increase_percent" {
  type        = number
  default     = 25
  description = "RPM quota increase % (0, 25, 50, 75, 100). Set 0 to skip or when RequestsPerMinuteQuotaCode is NA. Updatable via Parameter Store"

  validation {
    condition     = contains([0, 25, 50, 75, 100], var.requests_per_minute_increase_percent)
    error_message = "RequestsPerMinuteIncreasePercent must be one of: 0, 25, 50, 75, 100."
  }
}

variable "tokens_per_minute_quota_code" {
  type        = string
  default     = "L-3DCCFAA4"
  description = "TPM quota code. Required if TokensPerMinuteIncreasePercent > 0"
}

variable "tokens_per_minute_increase_percent" {
  type        = number
  default     = 25
  description = "TPM quota increase % (0, 25, 50, 75, 100). Set 0 to skip. Updatable via Parameter Store"

  validation {
    condition     = contains([0, 25, 50, 75, 100], var.tokens_per_minute_increase_percent)
    error_message = "TokensPerMinuteIncreasePercent must be one of: 0, 25, 50, 75, 100."
  }
}

# --- Model Usage Budget (Threshold) ---

variable "requests_per_minute_threshold_percent" {
  type        = number
  default     = 80
  description = "RPM threshold as percentage of quota (e.g., 80 for 80% of quota limit). Ignored when RequestsPerMinuteQuotaCode is NA"

  validation {
    condition     = var.requests_per_minute_threshold_percent >= 1 && var.requests_per_minute_threshold_percent <= 100
    error_message = "RequestsPerMinuteThresholdPercent must be between 1 and 100."
  }
}

variable "tokens_per_minute_threshold_percent" {
  type        = number
  default     = 80
  description = "TPM threshold as percentage of quota (e.g., 80 for 80% of quota limit)"

  validation {
    condition     = var.tokens_per_minute_threshold_percent >= 1 && var.tokens_per_minute_threshold_percent <= 100
    error_message = "TokensPerMinuteThresholdPercent must be between 1 and 100."
  }
}

variable "latency_threshold_ms" {
  type        = number
  default     = 240000
  description = "Latency threshold in milliseconds (Min: 15000)"

  validation {
    condition     = var.latency_threshold_ms >= 15000
    error_message = "LatencyThresholdMs must be at least 15000."
  }
}

# --- Automated Support Case Configuration ---

variable "enable_automated_support_case" {
  type        = string
  default     = "Yes"
  description = "Enable automated support case creation (Yes/No). Stored in Parameter Store, updatable post-deployment"

  validation {
    condition     = contains(["Yes", "No"], var.enable_automated_support_case)
    error_message = "EnableAutomatedSupportCase must be Yes or No."
  }
}

variable "support_case_lookback_days" {
  type        = number
  default     = 60
  description = "Number of days to look back when checking for existing unresolved support cases (duplicate detection)"

  validation {
    condition     = contains([45, 60, 90], var.support_case_lookback_days)
    error_message = "SupportCaseLookbackDays must be one of: 45, 60, 90."
  }
}

variable "use_case_description" {
  type        = string
  default     = "Enterprise production workload serving real-time AI-powered features to end users."
  description = "Brief description of the use case for quota increase justification. Updatable via Parameter Store"

  validation {
    condition     = length(var.use_case_description) <= 1000
    error_message = "UseCaseDescription must be 1000 characters or fewer."
  }
}

# --- Automated Threshold Update Configuration ---

variable "enable_automated_threshold_update" {
  type        = string
  default     = "Yes"
  description = "Enable automated threshold updates based on quota changes (Yes/No)."

  validation {
    condition     = contains(["Yes", "No"], var.enable_automated_threshold_update)
    error_message = "EnableAutomatedThresholdUpdate must be Yes or No."
  }
}

variable "threshold_update_schedule_interval_days" {
  type        = number
  default     = 1
  description = "How often to check and update alarm thresholds in days (1-30). Recommended: 1 for daily checks and update."

  validation {
    condition     = var.threshold_update_schedule_interval_days >= 1 && var.threshold_update_schedule_interval_days <= 30
    error_message = "ThresholdUpdateScheduleIntervalDays must be between 1 and 30."
  }
}

# --- Layer 1: Critical Error Detection Alarm Configuration ---

variable "error_threshold" {
  type        = number
  default     = 5
  description = "Error count threshold per evaluation period. 0 = immediate alert on any error."

  validation {
    condition     = var.error_threshold >= 0
    error_message = "ErrorThreshold must be 0 or greater."
  }
}

variable "critical_alarm_evaluation_periods" {
  type        = number
  default     = 5
  description = "Consecutive periods before critical alarm. 5 with 60s period = sustained errors for 5 minutes"

  validation {
    condition     = var.critical_alarm_evaluation_periods >= 1
    error_message = "CriticalAlarmEvaluationPeriods must be at least 1."
  }
}

# --- Layer 2: Usage Rate Monitoring Alarm Configuration ---

variable "warning_alarm_evaluation_periods" {
  type        = number
  default     = 5
  description = "Consecutive periods for warning alarm (Min: 1)"

  validation {
    condition     = var.warning_alarm_evaluation_periods >= 1
    error_message = "WarningAlarmEvaluationPeriods must be at least 1."
  }
}

variable "latency_alarm_period" {
  type        = number
  default     = 300
  description = "Latency check period in seconds (300 = 5 min)"

  validation {
    condition     = contains([60, 300, 900, 3600], var.latency_alarm_period)
    error_message = "LatencyAlarmPeriod must be one of: 60, 300, 900, 3600."
  }
}

variable "latency_alarm_evaluation_periods" {
  type        = number
  default     = 2
  description = "Consecutive latency checks before alert (Min: 1)"

  validation {
    condition     = var.latency_alarm_evaluation_periods >= 1
    error_message = "LatencyAlarmEvaluationPeriods must be at least 1."
  }
}

# --- Layer 3: Anomaly Detection Alarm Configuration ---

variable "anomaly_detection_period" {
  type        = number
  default     = 900
  description = "Anomaly check period in seconds (900 = 15 min)"

  validation {
    condition     = contains([60, 300, 900, 3600], var.anomaly_detection_period)
    error_message = "AnomalyDetectionPeriod must be one of: 60, 300, 900, 3600."
  }
}

variable "anomaly_evaluation_periods" {
  type        = number
  default     = 12
  description = "Consecutive anomaly periods before alert (Min: 1)"

  validation {
    condition     = var.anomaly_evaluation_periods >= 1
    error_message = "AnomalyEvaluationPeriods must be at least 1."
  }
}

variable "anomaly_sensitivity" {
  type        = number
  default     = 9
  description = "Anomaly sensitivity. 1 = most sensitive, 10 = least sensitive"

  validation {
    condition     = var.anomaly_sensitivity >= 1
    error_message = "AnomalySensitivity must be at least 1."
  }
}

# --- Common Alarm Configuration ---

variable "alarm_evaluation_period" {
  type        = number
  default     = 60
  description = "Alarm check period in seconds (60 = 1 min)"

  validation {
    condition     = contains([60, 300, 900, 3600], var.alarm_evaluation_period)
    error_message = "AlarmEvaluationPeriod must be one of: 60, 300, 900, 3600."
  }
}
