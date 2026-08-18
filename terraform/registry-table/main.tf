# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# Bootstrap: the DynamoDB registry table, created once before any deployment.
#
# This is a separate Terraform configuration and state from the parent directory on purpose. The
# deployment config (../) reads this table through a data source while planning, so the table must
# already exist. Apply this once; then run the deployment. It mirrors code/registry/registry_table.yml
# so a Terraform user never needs CloudFormation.

terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

variable "aws_region" {
  type        = string
  description = "Region to create the registry table in. Single-account: your account. Organization: the hub account that holds the registry."
}

variable "registry_table_name" {
  type        = string
  default     = "bedrock-ops-alert-registry"
  description = "DynamoDB registry table name. Pass the same value to the deployment if you change it."
}

# AccountId (HASH) + DeploymentTarget (RANGE); Region leads the sort key so a Region prefix
# matches all its rows. No secondary index: rows are always read by account.
resource "aws_dynamodb_table" "registry" {
  name         = var.registry_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "AccountId"
  range_key    = "DeploymentTarget"

  attribute {
    name = "AccountId"
    type = "S"
  }

  attribute {
    name = "DeploymentTarget"
    type = "S"
  }

  server_side_encryption {
    enabled = true
  }

  tags = {
    Solution = "Amazon-Bedrock-Ops-Alert"
  }
}

output "registry_table_name" {
  description = "Registry table name. Pass to the deployment with -var=\"registry_table_name=...\" if not the default."
  value       = aws_dynamodb_table.registry.name
}

output "registry_table_arn" {
  description = "Registry table ARN."
  value       = aws_dynamodb_table.registry.arn
}
