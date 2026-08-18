# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
    external = {
      source  = "hashicorp/external"
      version = ">= 2.3"
    }
  }
}

provider "aws" {
  region = var.aws_region

  # Organization deployment: assume a role in the target account. Terraform has no StackSets, so
  # the recommended multi-account pattern is one provider per account via assume_role, run once
  # per account. Left empty for single-account, where local credentials are used directly.
  dynamic "assume_role" {
    for_each = var.target_account_id == "" ? [] : [1]
    content {
      role_arn = "arn:aws:iam::${var.target_account_id}:role/${var.assume_role_name}"
    }
  }

  default_tags {
    tags = {
      Solution = "Amazon-Bedrock-Ops-Alert"
    }
  }
}
