# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# =============================================================================
# Registry-driven Terraform deployment.
#
# registry_rows.py reads the DynamoDB registry (reusing code/registry/registry_core.py),
# selects the deployable rows for this run's account and Region, validates them, and returns
# them as JSON. Terraform then instantiates the workload module once per row. The registry is
# the desired state: add a row (or clear its Status) to deploy it, set PENDING_DELETE or remove
# the row to have Terraform destroy it.
# =============================================================================

data "aws_caller_identity" "current" {}

data "external" "registry" {
  program = ["python3", "${path.module}/registry_rows.py"]

  query = {
    table        = var.registry_table_name
    table_region = var.registry_table_region == "" ? var.aws_region : var.registry_table_region
    region       = var.aws_region
    account_id   = var.target_account_id == "" ? data.aws_caller_identity.current.account_id : var.target_account_id
  }
}

locals {
  # registry_rows.py returns {"rows": "<json array string>"}; decode to a list of row objects.
  rows = jsondecode(data.external.registry.result.rows)
}

module "deployment" {
  source   = "./modules/bedrock-ops-alert"
  for_each = { for row in local.rows : row.deployment_target => row }

  # A null value makes Terraform fall back to the module variable's default, which mirrors the
  # registry's "blank column means use the template default" behavior.
  customer_name          = each.value.customer_name
  bedrock_model_name     = each.value.bedrock_model_name
  stakeholder_email_list = each.value.stakeholder_email_list
  lambda_s3_bucket       = each.value.lambda_s3_bucket

  notification_preference        = each.value.notification_preference
  inference_profile_type         = each.value.inference_profile_type
  bedrock_model_id               = each.value.bedrock_model_id
  geo_data_residency_requirement = each.value.geo_data_residency_requirement
  input_modalities               = each.value.input_modalities

  requests_per_minute_quota_code       = each.value.requests_per_minute_quota_code
  requests_per_minute_increase_percent = each.value.requests_per_minute_increase_percent
  tokens_per_minute_quota_code         = each.value.tokens_per_minute_quota_code
  tokens_per_minute_increase_percent   = each.value.tokens_per_minute_increase_percent

  requests_per_minute_threshold_percent = each.value.requests_per_minute_threshold_percent
  tokens_per_minute_threshold_percent   = each.value.tokens_per_minute_threshold_percent
  latency_threshold_ms                  = each.value.latency_threshold_ms

  enable_automated_support_case = each.value.enable_automated_support_case
  support_case_lookback_days    = each.value.support_case_lookback_days
  use_case_description          = each.value.use_case_description

  enable_automated_threshold_update       = each.value.enable_automated_threshold_update
  threshold_update_schedule_interval_days = each.value.threshold_update_schedule_interval_days

  error_threshold                   = each.value.error_threshold
  critical_alarm_evaluation_periods = each.value.critical_alarm_evaluation_periods

  warning_alarm_evaluation_periods = each.value.warning_alarm_evaluation_periods
  latency_alarm_period             = each.value.latency_alarm_period
  latency_alarm_evaluation_periods = each.value.latency_alarm_evaluation_periods

  anomaly_detection_period   = each.value.anomaly_detection_period
  anomaly_evaluation_periods = each.value.anomaly_evaluation_periods
  anomaly_sensitivity        = each.value.anomaly_sensitivity

  alarm_evaluation_period = each.value.alarm_evaluation_period
}
