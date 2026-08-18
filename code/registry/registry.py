#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Amazon Bedrock Ops Alert - registry driver (CloudFormation).

Reads deployment rows from DynamoDB and applies them with CloudFormation. Runs locally under your
AWS credentials, so CloudFormation acts with your permissions, the same as the manual deployment
in DEPLOYMENT.md. The registry table is the only resource it adds.

Engine-agnostic logic (parsing, validation, parameter building, status writes) lives in
registry_core.py; this file adds only the CloudFormation create, delete, and status handling.

    python registry.py plan     # show what would happen, change nothing
    python registry.py apply    # create / delete per row Status

Table key:
    AccountId        (HASH)   111122223333
    DeploymentTarget (RANGE)  <Region>#<CustomerName>#<BedrockModelName>

Region, CustomerName and BedrockModelName are parsed from DeploymentTarget and passed to
CloudFormation, so they are never typed twice.

Row Status drives the operation:
    (blank) / REQUESTED   create the workload stack
    PENDING_DELETE        delete it, then force-delete its secrets
    TEST                  ignored, always (the sample row)
    DEPLOYED / FAILED / VALIDATION_FAILED / DELETED / DELETE_FAILED
                          terminal; clear Status to act again
"""

import argparse
import sys
from datetime import date
from pathlib import Path

import boto3
from botocore.exceptions import ClientError, WaiterError

from registry_core import (
    # constants
    PARAM_KEYS,
    DERIVED_PARAM_KEYS,
    REQUIRED_PARAM_KEYS,
    CONTROL_COLUMNS,
    WORKLOAD_ARTIFACT_KEYS,
    WORKLOAD_TEMPLATE_KEY,
    DEFAULT_TABLE_NAME,
    STACK_NAME_PREFIX,
    TARGET_SEPARATOR,
    CUSTOMER_NAME_PATTERN,
    MODEL_NAME_PATTERN,
    REGION_PATTERN,
    STATUS_TEST,
    STATUS_REQUESTED,
    STATUS_DEPLOYED,
    STATUS_FAILED,
    STATUS_VALIDATION_FAILED,
    STATUS_PENDING_DELETE,
    STATUS_DELETED,
    STATUS_DELETE_FAILED,
    OP_CREATE,
    OP_DELETE,
    OP_SKIP,
    TAG_KEY_PATTERN,
    TAG_VALUE_PATTERN,
    ValidationFailure,
    # shared functions
    is_blank,
    utc_now_iso,
    build_deployment_target,
    parse_deployment_target,
    build_stack_name,
    secret_names,
    row_key,
    build_parameters,
    find_missing_mandatory,
    find_unknown_columns,
    parse_tags,
    check_artifacts_exist,
    probe_model_availability,
    classify,
    query_account_rows,
    write_status,
    build_template_url,
    to_cfn_tags,
    confirm,
)

# ---------------------------------------------------------------------------
# CloudFormation-specific values
# ---------------------------------------------------------------------------

# Deploy records are written next to this script, under registry/output/<date>/, and gitignored.
OUTPUT_DIR = Path(__file__).resolve().parent / 'output'


# The only parameters that can hold a space or a list comma. StakeholderEmailList is a
# CommaDelimitedList; InputModalities and UseCaseDescription are free-text strings.
SINGLE_QUOTED_PARAMS = ('StakeholderEmailList', 'InputModalities', 'UseCaseDescription')


def _cli_param(key, value):
    """
    Format one ``ParameterKey=..,ParameterValue=..`` token for the create-stack command.

    StakeholderEmailList, InputModalities and UseCaseDescription can carry list commas or spaces,
    so the whole token is single-quoted and any comma is backslash-escaped for the CLI shorthand
    parser. Every other value is a constrained token, so only its value is double-quoted.
    """
    if key in SINGLE_QUOTED_PARAMS:
        return "'ParameterKey=" + key + ',ParameterValue=' + value.replace(',', '\\,') + "'"
    return 'ParameterKey=' + key + ',ParameterValue="' + value + '"'


def _cli_tag(key, value):
    """Format one ``Key=..,Value=..`` tag token, single-quoted when the value has a space."""
    pair = 'Key=' + key + ',Value=' + value
    return "'" + pair + "'" if ' ' in value else pair


def build_cli_command(stack_name, resolved):
    """
    Return the ``aws cloudformation create-stack`` command that matches this deployment.

    The command uses the real resolved values the driver sends to CloudFormation, so the
    operator gets a readable, runnable record of exactly what was deployed.
    """
    lines = [
        'aws cloudformation create-stack',
        '  --stack-name ' + stack_name,
        '  --template-url "' + resolved['TemplateURL'] + '"',
        '  --parameters',
    ]
    lines += ['    ' + _cli_param(p['ParameterKey'], p['ParameterValue'])
              for p in resolved['Parameters']]
    lines.append('  --capabilities CAPABILITY_NAMED_IAM')
    if resolved['Tags']:
        tags = ' '.join(_cli_tag(k, v) for k, v in resolved['Tags'].items())
        lines.append('  --tags ' + tags)
    return ' \\\n'.join(lines) + '\n'


def write_deploy_record(stack_name, resolved):
    """
    Write the create-stack command to ``output/<date>/<stack_name>.txt`` and return the path.

    The record is a convenience for the operator, so a write failure must never stop the run;
    an OSError is reported and skipped. These files are gitignored.
    """
    try:
        folder = OUTPUT_DIR / date.today().isoformat()
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (stack_name + '.txt')
        path.write_text(build_cli_command(stack_name, resolved))
        return path
    except OSError as e:
        print('      could not write deploy record: ' + str(e))
        return None


def resolve(session, row):
    """Parse the key, run every pre-flight check, return the CreateStack arguments."""
    region, customer_name, model_name = parse_deployment_target(row['DeploymentTarget'])

    unknown = find_unknown_columns(row)
    if unknown:
        raise ValidationFailure(f'Unknown column(s): {", ".join(unknown)}. Check for a typo.')

    missing = find_missing_mandatory(row, customer_name)
    if missing:
        raise ValidationFailure(f'Required and must be set: {", ".join(missing)}')

    tags = parse_tags(row)

    bucket = str(row['LambdaS3Bucket']).strip()
    check_artifacts_exist(session.client('s3', region_name=region), bucket)

    profile_type = (
        str(row['InferenceProfileType']).strip()
        if not is_blank(row.get('InferenceProfileType')) else 'System-Defined'
    )
    probe = probe_model_availability(
        session, region, str(row['BedrockModelId']).strip(), profile_type
    )

    parameters = build_parameters(row, customer_name, model_name)
    supplied = {p['ParameterKey'] for p in parameters}

    return {
        'Region': region,
        'CustomerName': customer_name,
        'BedrockModelName': model_name,
        'StackName': build_stack_name(customer_name, model_name),
        'TemplateURL': build_template_url(bucket, region),
        'Parameters': parameters,
        'Tags': tags,
        'DefaultedKeys': [k for k in PARAM_KEYS if k not in supplied],
        'ModelProbe': probe,
    }


# ---------------------------------------------------------------------------
# CloudFormation
# ---------------------------------------------------------------------------

def stack_status(cfn, stack_name):
    """Current status and id, or (None, None) if the stack does not exist."""
    try:
        stack = cfn.describe_stacks(StackName=stack_name)['Stacks'][0]
        return stack['StackStatus'], stack['StackId']
    except ClientError as e:
        if 'does not exist' in e.response['Error']['Message']:
            return None, None
        raise


def failure_reason(cfn, stack_id):
    """
    Root cause from stack events. DescribeStacks reports only the symptom ("The following
    resource(s) failed to create"), so walk the events for the first real resource failure.
    """
    noise = (
        'Resource creation cancelled',
        'Resource update cancelled',
        'The following resource(s) failed to',
    )
    try:
        causes = []
        for page in cfn.get_paginator('describe_stack_events').paginate(StackName=stack_id):
            for event in page['StackEvents']:
                if event.get('ResourceStatus') not in ('CREATE_FAILED', 'DELETE_FAILED'):
                    continue
                reason = event.get('ResourceStatusReason', '')
                if reason and not reason.startswith(noise):
                    causes.append(f'{event["LogicalResourceId"]}: {reason}')
        if causes:
            return causes[-1][:1000]  # events are newest-first, so the oldest is the root cause
    except ClientError as e:
        return f'Could not read stack events: {e}'
    return 'Stack operation failed; see stack events'


def force_delete_secrets(session, region, customer_name, model_name):
    """
    Stack deletion places both secrets in a recovery window of at least 7 days, during which the
    secret name cannot be reused. Force-deleting them lets the same DeploymentTarget be
    redeployed immediately.
    """
    client = session.client('secretsmanager', region_name=region)
    deleted = 0
    for name in secret_names(customer_name, model_name):
        try:
            client.delete_secret(SecretId=name, ForceDeleteWithoutRecovery=True)
            deleted += 1
        except ClientError as e:
            if e.response['Error']['Code'] != 'ResourceNotFoundException':
                raise
    return deleted


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------

def do_create(session, table, row, resolved):
    cfn = session.client('cloudformation', region_name=resolved['Region'])
    stack_name = resolved['StackName']

    existing, existing_id = stack_status(cfn, stack_name)
    if existing == 'CREATE_COMPLETE':
        # A previous run created the stack, then stopped before recording the result. Adopt it.
        write_status(table, row, STATUS_DEPLOYED, 'Stack already CREATE_COMPLETE', existing_id)
        print('      already CREATE_COMPLETE -> DEPLOYED')
        return True
    if existing == 'ROLLBACK_COMPLETE':
        # A failed create cannot be updated; it must be deleted before retrying.
        print('      stack is ROLLBACK_COMPLETE, deleting before retry')
        cfn.delete_stack(StackName=stack_name)
        cfn.get_waiter('stack_delete_complete').wait(StackName=stack_name)
    elif existing:
        write_status(table, row, STATUS_FAILED,
                     f'Stack "{stack_name}" already exists ({existing}). Delete it first.')
        print(f'      FAILED: stack already exists ({existing})')
        return False
    
    stack_id = cfn.create_stack(
        StackName=stack_name,
        TemplateURL=resolved['TemplateURL'],
        Parameters=resolved['Parameters'],
        Capabilities=['CAPABILITY_NAMED_IAM'],
        OnFailure='ROLLBACK',
        Tags=to_cfn_tags(resolved['Tags']),
    )['StackId']

    print(f'      created {stack_id}')
    print('      waiting for CREATE_COMPLETE (typically 3-5 min)...')

    try:
        cfn.get_waiter('stack_create_complete').wait(StackName=stack_id)
    except WaiterError:
        reason = failure_reason(cfn, stack_id)
        write_status(table, row, STATUS_FAILED, reason, stack_id)
        print(f'      FAILED: {reason}')
        return False

    write_status(table, row, STATUS_DEPLOYED, 'Stack created successfully', stack_id)
    print('      DEPLOYED')
    return True


def do_delete(session, table, row):
    region, customer_name, model_name = parse_deployment_target(row['DeploymentTarget'])
    cfn = session.client('cloudformation', region_name=region)
    stack_name = build_stack_name(customer_name, model_name)

    existing, stack_id = stack_status(cfn, stack_name)
    if existing:
        print(f'      deleting {stack_name}...')
        cfn.delete_stack(StackName=stack_name)
        try:
            cfn.get_waiter('stack_delete_complete').wait(StackName=stack_name)
        except WaiterError:
            reason = failure_reason(cfn, stack_id or stack_name)
            write_status(table, row, STATUS_DELETE_FAILED, reason)
            print(f'      DELETE_FAILED: {reason}')
            return False
    else:
        print(f'      stack {stack_name} not found, cleaning up the row')

    deleted = force_delete_secrets(session, region, customer_name, model_name)
    if deleted:
        print(f'      force-deleted {deleted} secret(s) so the name is reusable')

    write_status(table, row, STATUS_DELETED, 'Stack deleted; secrets force-deleted')
    print('      DELETED')
    return True


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_plan(session, table, rows, write_records=True):
    """
    Preview each row, and for valid creates optionally write the deploy record file.

    write_records is False when this runs only as the apply preview, so the record is written
    once, by cmd_apply, at the point the create actually runs.
    """
    actionable = invalid = 0

    for row in rows:
        operation, why = classify(row)
        target = row['DeploymentTarget']

        if operation == OP_SKIP:
            print(f'  SKIP      {target}\n            {why}')
            continue
        if operation == OP_DELETE:
            try:
                _, customer_name, model_name = parse_deployment_target(target)
            except ValidationFailure as vf:
                print(f'  INVALID   {target}\n            {vf}')
                invalid += 1
                continue
            print(f'  DELETE    {target}')
            print(f'            stack:   {build_stack_name(customer_name, model_name)}')
            print('            then force-deletes 2 secrets so the name is reusable')
            actionable += 1
            continue

        try:
            resolved = resolve(session, row)
        except ValidationFailure as vf:
            print(f'  INVALID   {target}\n            {vf}')
            invalid += 1
            continue

        defaulted = resolved['DefaultedKeys']
        print(f'  CREATE    {target}')
        print(f'            stack:      {resolved["StackName"]}')
        print(f'            template:   {resolved["TemplateURL"]}')
        print(f'            parameters: {len(resolved["Parameters"])} supplied, '
              f'{len(defaulted)} defaulted')
        if defaulted:
            print(f'            defaulted:  {", ".join(defaulted)}')
        print(f'            model:      {resolved["ModelProbe"]}')
        tag_display = ', '.join(f'{k}={v}' for k, v in resolved['Tags'].items()) or '(none)'
        print(f'            tags:       {tag_display}')
        if write_records:
            record = write_deploy_record(resolved['StackName'], resolved)
            if record:
                print(f'            record:     {record}')
        actionable += 1

    print(f'\nPlan: {actionable} actionable, {invalid} invalid, '
          f'{len(rows) - actionable - invalid} skipped.')
    if invalid:
        print('Fix the INVALID rows before apply; they would be rejected.')
    return actionable, invalid


def cmd_apply(session, table, rows):
    succeeded = failed = skipped = 0

    for row in rows:
        operation, _ = classify(row)
        target = row['DeploymentTarget']

        if operation == OP_SKIP:
            skipped += 1
            continue

        print(f'\n  {operation}: {target}')

        # One row must never abort the run. An unexpected error is recorded on that row and the
        # sweep continues with the next one.
        try:
            if operation == OP_DELETE:
                ok = do_delete(session, table, row)
            else:
                # Validate first, so a rejected row keeps its original Status.
                try:
                    resolved = resolve(session, row)
                except ValidationFailure as vf:
                    print(f'      VALIDATION_FAILED: {vf}')
                    write_status(table, row, STATUS_VALIDATION_FAILED, str(vf))
                    failed += 1
                    continue
                record = write_deploy_record(resolved['StackName'], resolved)
                if record:
                    print(f'      record: {record}')
                ok = do_create(session, table, row, resolved)
        except KeyboardInterrupt:
            print('\n      interrupted. Re-run apply to continue.')
            raise
        except Exception as e:
            reason = f'{type(e).__name__}: {e}'
            print(f'      FAILED: {reason}')
            terminal = STATUS_DELETE_FAILED if operation == OP_DELETE else STATUS_FAILED
            try:
                write_status(table, row, terminal, reason)
            except ClientError as write_error:
                print(f'      could not record the failure on the row: {write_error}')
            failed += 1
            continue

        succeeded += 1 if ok else 0
        failed += 0 if ok else 1

    print(f'\nApply: {succeeded} succeeded, {failed} failed, {skipped} skipped.')
    return 1 if failed else 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Apply Bedrock Ops Alert deployments from a DynamoDB registry.')
    parser.add_argument('command', choices=['plan', 'apply'])
    parser.add_argument('--table', default=DEFAULT_TABLE_NAME,
                        help=f'registry table name (default: {DEFAULT_TABLE_NAME})')
    parser.add_argument('--region', help='Region of the registry table (default: AWS config)')
    parser.add_argument('--profile', help='AWS profile to use')
    parser.add_argument('--force', action='store_true',
                        help='apply without the plan preview and confirmation prompt')
    args = parser.parse_args(argv)

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    identity = session.client('sts').get_caller_identity()

    print(f'Registry : {args.table}')
    print(f'Account  : {identity["Account"]}')
    print(f'Identity : {identity["Arn"]}')
    print(f'Command  : {args.command}\n')

    # Query this account's partition only. Rows for other accounts are invisible, which matches
    # the fact that the script can only deploy into the account it is authenticated to.
    table = session.resource('dynamodb').Table(args.table)
    rows = query_account_rows(table, identity['Account'])
    if not rows:
        print(f'No rows for account {identity["Account"]}. Seed the sample row:')
        print(f'  aws dynamodb put-item --table-name {args.table} '
              f'--cli-input-json file://code/registry/sample_row.json')
        return 0

    rows.sort(key=lambda r: r['DeploymentTarget'])

    if args.command == 'plan':
        _, invalid = cmd_plan(session, table, rows)
        return 1 if invalid else 0

    # apply: preview then confirm, unless --force skips straight to applying.
    if not args.force:
        # Preview only; cmd_apply writes the record when the create actually runs.
        actionable, invalid = cmd_plan(session, table, rows, write_records=False)
        if actionable == 0:
            print('\nNothing to apply.')
            return 1 if invalid else 0
        if not confirm('\nApply these changes? [Y/n] '):
            print('Aborted. No changes made.')
            return 0
    return cmd_apply(session, table, rows)


if __name__ == '__main__':
    sys.exit(main())
