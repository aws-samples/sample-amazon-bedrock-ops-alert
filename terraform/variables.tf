# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# =============================================================================
# Root variables for the registry-driven Terraform deployment.
# The per-model settings are NOT here; they come from the DynamoDB registry rows,
# read by registry_rows.py. These variables only locate the registry and select
# the account and Region this run deploys to.
# =============================================================================

variable "aws_region" {
  type        = string
  description = "Region to deploy into. Only registry rows whose DeploymentTarget Region matches are applied in this run."
}

variable "registry_table_name" {
  type        = string
  default     = "bedrock-ops-alert-registry"
  description = "DynamoDB registry table name."
}

variable "registry_table_region" {
  type        = string
  default     = ""
  description = "Region of the registry table. Defaults to aws_region when empty. For the organization method, set this to the management account Region where the table lives."
}

variable "target_account_id" {
  type        = string
  default     = ""
  description = "Organization method only: the member account to deploy into. When set, the AWS provider assumes a role in this account and only rows with this AccountId are applied. Leave empty for single-account deployment."

  validation {
    condition     = var.target_account_id == "" || can(regex("^[0-9]{12}$", var.target_account_id))
    error_message = "target_account_id must be empty or a 12-digit account id."
  }
}

variable "assume_role_name" {
  type        = string
  default     = "OrganizationAccountAccessRole"
  description = "Organization method only: the role name to assume in target_account_id. Ignored for single-account deployment."
}
