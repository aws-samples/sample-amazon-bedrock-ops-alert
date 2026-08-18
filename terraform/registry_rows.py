#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Terraform external data source for the registry-driven deployment.

Reads the DynamoDB registry, selects the deployable rows for one account and Region, validates
them, and returns them as JSON for Terraform to fan out with for_each. Engine-agnostic parsing
and validation are reused from code/registry/registry_core.py, so the row format and rules match
the CloudFormation registry drivers exactly.

Terraform's external data source protocol: a JSON object arrives on stdin, and a JSON object of
string values must be written to stdout. The deployable rows are returned as one JSON-encoded
string under the "rows" key, which the root module decodes with jsondecode().

    query in : {"table", "table_region", "region", "account_id"}
    stdout   : {"rows": "<json array of row objects>"}

Each returned object carries the workload module's input variables. A value is null when the
row leaves that column blank, so Terraform falls back to the module variable's default.
"""

import json
import re
import sys
from pathlib import Path

import boto3

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'code' / 'registry'))

from registry_core import (  # noqa: E402
    PARAM_KEYS,
    DERIVED_PARAM_KEYS,
    WORKLOAD_ARTIFACT_KEYS,
    OP_CREATE,
    ValidationFailure,
    is_blank,
    parse_deployment_target,
    find_missing_mandatory,
    find_unknown_columns,
    classify,
    query_account_rows,
    check_artifacts_exist,
    probe_model_availability,
)


def _snake(name):
    """CamelCase registry column to snake_case Terraform variable, e.g. LambdaS3Bucket -> lambda_s3_bucket."""
    return re.sub(r'(?<!^)(?=[A-Z])', '_', name).lower()


def row_to_tfvars(row):
    """
    Map one registry row to the workload module's input variables.

    CustomerName and BedrockModelName come from the parsed DeploymentTarget. StakeholderEmailList
    becomes a list. Every other column maps by snake_case; a blank column becomes null so the
    module default applies. Raises ValidationFailure if DeploymentTarget is malformed.

    Example: {"DeploymentTarget": "us-east-1#Acme#G-Opus-4-6", ...} ->
             {"region": "us-east-1", "customer_name": "Acme", "bedrock_model_name": "G-Opus-4-6", ...}
    """
    region, customer_name, model_name = parse_deployment_target(row['DeploymentTarget'])

    emails = [e.strip() for e in str(row.get('StakeholderEmailList', '')).split(',') if e.strip()]
    tfvars = {
        'deployment_target': str(row['DeploymentTarget']).strip(),
        'region': region,
        'customer_name': customer_name,
        'bedrock_model_name': model_name,
        'stakeholder_email_list': emails,
    }
    for key in PARAM_KEYS:
        if key in DERIVED_PARAM_KEYS or key == 'StakeholderEmailList':
            continue
        value = row.get(key)
        tfvars[_snake(key)] = None if is_blank(value) else str(value).strip()
    return tfvars


def collect_rows(table, region, account_id, probe_session=None):
    """
    Return (valid_rows, errors) for the deployable rows of this account in this Region.

    When probe_session is given (single-account runs, where the local credentials belong to the
    deploying account), each row is also checked against Bedrock: the model must be reachable in
    the Region and its actual inference-profile type must match the row's InferenceProfileType.
    This mirrors the CloudFormation single-account driver and turns a model or profile-type
    mismatch into a plan-time error instead of a Lambda failure during apply. Organization runs
    pass no session, because the hub credentials cannot see the target account's Bedrock.
    """
    valid, errors = [], []
    for row in query_account_rows(table, account_id):
        operation, _ = classify(row)
        if operation != OP_CREATE:
            continue
        target = str(row.get('DeploymentTarget', '')).strip()
        try:
            tfvars = row_to_tfvars(row)
        except ValidationFailure as vf:
            errors.append(f'{target}: {vf}')
            continue
        if tfvars['region'] != region:
            continue  # another Region; not part of this run
        unknown = find_unknown_columns(row)
        if unknown:
            errors.append(f'{target}: unknown column(s): {", ".join(unknown)}')
            continue
        missing = find_missing_mandatory(row, tfvars['customer_name'])
        if missing:
            errors.append(f'{target}: required and must be set: {", ".join(missing)}')
            continue
        try:
            check_artifacts_exist(
                boto3.client('s3', region_name=region),
                tfvars['lambda_s3_bucket'],
                keys=WORKLOAD_ARTIFACT_KEYS,
            )
        except ValidationFailure as vf:
            errors.append(f'{target}: {vf}')
            continue

        # Single-account runs only: confirm the model is reachable and its actual inference-profile
        # type matches the row. This is the same check the CloudFormation single-account driver
        # runs, so a mismatch (for example an Application profile id declared System-Defined) fails
        # at plan time here rather than in the QuotaCalculator Lambda during apply.
        if probe_session is not None:
            try:
                probe_model_availability(
                    probe_session, region,
                    tfvars['bedrock_model_id'],
                    tfvars['inference_profile_type'] or 'System-Defined',
                )
            except ValidationFailure as vf:
                errors.append(f'{target}: {vf}')
                continue

        valid.append(tfvars)
    return valid, errors


def main():
    query = json.load(sys.stdin)
    table_region = query['table_region']
    region = query['region']
    account_id = query['account_id']

    table = boto3.resource('dynamodb', region_name=table_region).Table(query['table'])

    # Probe Bedrock only when the local credentials belong to the deploying account, i.e. a
    # single-account run. In an organization run the caller is the hub account and cannot see the
    # target account's Bedrock, so a probe there would mislead; skip it, as the CloudFormation
    # organization driver also does. If the caller identity cannot be resolved, skip rather than
    # block the deployment.
    probe_session = None
    try:
        if boto3.client('sts').get_caller_identity()['Account'] == account_id:
            probe_session = boto3.Session()
    except Exception:
        probe_session = None

    valid, errors = collect_rows(table, region, account_id, probe_session=probe_session)

    if errors:
        sys.stderr.write('Invalid registry rows:\n  ' + '\n  '.join(errors) + '\n')
        sys.exit(1)

    json.dump({'rows': json.dumps(valid)}, sys.stdout)


if __name__ == '__main__':
    main()
