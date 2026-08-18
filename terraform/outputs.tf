# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

output "deployed_targets" {
  description = "The DeploymentTarget of each row deployed in this run."
  value       = keys(module.deployment)
}

output "composite_alarm_names" {
  description = "Composite alarm name per deployed target."
  value       = { for target, m in module.deployment : target => m.composite_alarm_name }
}

output "formatted_notification_topic_arns" {
  description = "Formatted notification (email) topic ARN per deployed target."
  value       = { for target, m in module.deployment : target => m.formatted_notification_topic_arn }
}
