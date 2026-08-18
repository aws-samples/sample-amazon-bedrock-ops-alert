#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Amazon Bedrock Ops Alert - registry driver (CloudFormation StackSets, organization method).

Reads deployment rows from a DynamoDB registry in the AWS Organizations management account and
applies them as service-managed CloudFormation StackSets. Rows that share a CustomerName and
BedrockModelName map to ONE stack set; each row becomes one stack instance (one account and
Region) in that set, with its own values supplied as per-instance ParameterOverrides. A new
stack set is created only when the customer or model differs. Engine-agnostic logic (parsing,
validation, parameter building, status writes) is shared from registry_core.py.

    python registry_stackset.py plan          # show what would happen, change nothing
    python registry_stackset.py apply         # preview, confirm, then create / delete per row
    python registry_stackset.py apply --force  # apply without the preview and prompt

Row key (registry table in the management account):
    AccountId        (HASH)   the TARGET account the stack instance is deployed to
    DeploymentTarget (RANGE)  <Region>#<CustomerName>#<BedrockModelName>

Extra column required for this method:
    OrganizationalUnitId       the OU that contains the target account (ou-... or a root r-...)

One stack set, many instances:
    Rows in one stack set must set the same columns (the same parameter keys) and the same Tags.
    Only the VALUES may differ per row, which is how each instance gets its own emails, bucket,
    and tuning. Two rows that set different columns, or different Tags, are reported before
    apply. Setting a row's Status to PENDING_DELETE removes only that row's stack instance; the
    stack set itself is removed once its last instance is gone.

Run it from the Organizations management account, in the Region where the registry table lives:
that Region administers the stack sets, while each instance still deploys to the Region named in
its row. Service-managed StackSets create their own IAM roles, so there is no per-account role to
set up. CloudFormation does not deploy to the management account itself (use the single-account
driver there), and on teardown the target account's two Secrets Manager secrets keep their
recovery window, because the management account cannot force-delete them across the account
boundary.
"""

import argparse
import re
import sys
import time

import boto3
from botocore.exceptions import ClientError

from registry_core import (
    PARAM_KEYS,
    DEFAULT_TABLE_NAME,
    STATUS_DEPLOYED,
    STATUS_FAILED,
    STATUS_VALIDATION_FAILED,
    STATUS_DELETED,
    STATUS_DELETE_FAILED,
    OP_CREATE,
    OP_DELETE,
    OP_SKIP,
    ValidationFailure,
    parse_deployment_target,
    build_stack_name,
    build_parameters,
    find_missing_mandatory,
    find_unknown_columns,
    parse_tags,
    check_artifacts_exist,
    classify,
    scan_all_rows,
    write_status,
    confirm,
    build_template_url,
    to_cfn_tags,
)

# ---------------------------------------------------------------------------
# StackSet configuration
# ---------------------------------------------------------------------------

# Extra row column: the OU that contains the target account.
ORG_UNIT_COLUMN = 'OrganizationalUnitId'
ORG_UNIT_PATTERN = re.compile(r'^(ou-[0-9a-z]+-[0-9a-z]+|r-[0-9a-z]+)$')
ACCOUNT_PATTERN = re.compile(r'^[0-9]{12}$')

# Deploy to exactly the listed account within the given OU (the intersection of both).
ACCOUNT_FILTER_TYPE = 'INTERSECTION'
# Service-managed StackSets create their own roles; no per-account setup is needed.
PERMISSION_MODEL = 'SERVICE_MANAGED'
# Instances are created explicitly per row, so do not auto-deploy to new accounts in the OU.
# RetainStacksOnAccountRemoval may be set only when Enabled is True, so it is omitted here.
AUTO_DEPLOYMENT = {'Enabled': False}
# Run from the management account. Use DELEGATED_ADMIN only from a registered delegated admin.
CALL_AS = 'SELF'
# StackSet operations have no botocore waiter, so poll at this interval.
POLL_SECONDS = 15


def row_key(row):
    """The (AccountId, DeploymentTarget) pair that uniquely identifies a row in this run."""
    return (str(row['AccountId']).strip(), str(row['DeploymentTarget']).strip())


# ---------------------------------------------------------------------------
# Row parsing and resolution
# ---------------------------------------------------------------------------

def parse_org_unit(row):
    """The OU id that contains the target account, validated to the ou-.../r-... shape."""
    org_unit = str(row.get(ORG_UNIT_COLUMN, '')).strip()
    if not ORG_UNIT_PATTERN.match(org_unit):
        raise ValidationFailure(
            f'{ORG_UNIT_COLUMN} "{org_unit}" must be an OU id (ou-....) or a root id (r-....)'
        )
    return org_unit


def resolve_stackset(session, row):
    """
    Parse one row, run every pre-flight check, and return its create arguments.

    The StackSet name is derived from CustomerName and BedrockModelName only, so rows that share
    those two group into one stack set and this row is one instance in it. Parameters double as
    the stack-set defaults (used when this row first creates the set) and as this instance's
    ParameterOverrides.
    """
    region, customer_name, model_name = parse_deployment_target(row['DeploymentTarget'])
    account = str(row['AccountId']).strip()
    if not ACCOUNT_PATTERN.match(account):
        raise ValidationFailure(f'AccountId "{account}" must be a 12-digit account id')
    org_unit = parse_org_unit(row)

    unknown = find_unknown_columns(row)
    if unknown:
        raise ValidationFailure(f'Unknown column(s): {", ".join(unknown)}. Check for a typo.')

    missing = find_missing_mandatory(row, customer_name)
    if missing:
        raise ValidationFailure(f'Required and must be set: {", ".join(missing)}')

    tags = parse_tags(row)

    bucket = str(row['LambdaS3Bucket']).strip()
    check_artifacts_exist(session.client('s3', region_name=region), bucket)

    parameters = build_parameters(row, customer_name, model_name)
    supplied = {p['ParameterKey'] for p in parameters}

    return {
        'Target': str(row['DeploymentTarget']).strip(),
        'Region': region,
        'Account': account,
        'OrganizationalUnitId': org_unit,
        'CustomerName': customer_name,
        'BedrockModelName': model_name,
        'StackSetName': build_stack_name(customer_name, model_name),
        'TemplateURL': build_template_url(bucket, region),
        'Parameters': parameters,
        'ParamKeys': supplied,
        'Tags': tags,
        'DefaultedKeys': [k for k in PARAM_KEYS if k not in supplied],
    }


def find_group_conflicts(resolved_rows):
    """
    Rows that share a stack set must set the same columns and the same Tags, because a stack set
    has one template and one tag set; only parameter values can vary per instance. Return
    {stackset_name: reason} for each group whose rows disagree, so those rows are reported before
    apply rather than failing partway through it.
    """
    groups = {}
    for resolved in resolved_rows:
        groups.setdefault(resolved['StackSetName'], []).append(resolved)

    conflicts = {}
    for name, members in groups.items():
        if len(members) < 2:
            continue
        key_sets = {frozenset(m['ParamKeys']) for m in members}
        tag_sets = {tuple(sorted(m['Tags'].items())) for m in members}
        if len(key_sets) > 1:
            conflicts[name] = (
                'rows in this stack set set different columns; rows that share a CustomerName '
                'and BedrockModelName must set the same columns (the values may differ)'
            )
        elif len(tag_sets) > 1:
            conflicts[name] = (
                'rows in this stack set have different Tags; tags apply to the whole stack set, '
                'so they must be identical across the group'
            )
    return conflicts


# ---------------------------------------------------------------------------
# StackSet operations
#
# A stack set is a Regional administrative resource: it is created in, and addressed from, one
# Region, while its instances deploy to the Regions named in create_stack_instances. Every call
# below therefore uses one admin-Region client (cfn), never the target Region.
# ---------------------------------------------------------------------------

def get_stack_instance(cfn, name, account, region):
    """The stack instance for this account/Region, or None if the StackSet or instance is absent."""
    try:
        return cfn.describe_stack_instance(
            StackSetName=name,
            StackInstanceAccount=account,
            StackInstanceRegion=region,
            CallAs=CALL_AS,
        )['StackInstance']
    except ClientError as e:
        if e.response['Error']['Code'] in (
            'StackInstanceNotFoundException', 'StackSetNotFoundException'
        ):
            return None
        raise


def ensure_stack_set(cfn, name, resolved):
    """
    Create the service-managed stack set if it is absent. If it already exists, confirm this
    row's columns match the set's, so a per-instance override cannot silently inherit another
    row's value, and AWS cannot reject the instance for an unknown parameter.
    """
    try:
        existing = cfn.describe_stack_set(StackSetName=name, CallAs=CALL_AS)['StackSet']
    except ClientError as e:
        if e.response['Error']['Code'] != 'StackSetNotFoundException':
            raise
        cfn.create_stack_set(
            StackSetName=name,
            Description=(
                'Amazon Bedrock Ops Alert monitoring for '
                + resolved['CustomerName'] + ' / ' + resolved['BedrockModelName']
            ),
            TemplateURL=resolved['TemplateURL'],
            Parameters=resolved['Parameters'],
            Capabilities=['CAPABILITY_NAMED_IAM'],
            Tags=to_cfn_tags(resolved['Tags']),
            PermissionModel=PERMISSION_MODEL,
            AutoDeployment=AUTO_DEPLOYMENT,
            CallAs=CALL_AS,
        )
        print('      created stack set ' + name)
        return

    existing_keys = {p['ParameterKey'] for p in existing.get('Parameters', [])}
    if resolved['ParamKeys'] != existing_keys:
        detail = []
        extra = resolved['ParamKeys'] - existing_keys
        missing = existing_keys - resolved['ParamKeys']
        if extra:
            detail.append('adds ' + ', '.join(sorted(extra)))
        if missing:
            detail.append('is missing ' + ', '.join(sorted(missing)))
        raise ValidationFailure(
            'Row columns do not match the existing stack set ' + name + ' (' + '; '.join(detail)
            + '). Rows in one stack set must set the same columns.'
        )


def operation_failure_reason(cfn, name, operation_id):
    """The first failed instance's reason from the operation results, for the row's StatusReason."""
    try:
        summaries = cfn.list_stack_set_operation_results(
            StackSetName=name, OperationId=operation_id, CallAs=CALL_AS,
        )['Summaries']
    except ClientError:
        return ''
    for item in summaries:
        if item.get('Status') in ('FAILED', 'CANCELLED'):
            reason = item.get('StatusReason', '')
            if reason:
                return (item.get('Account', '') + ': ' + reason)[:1000]
    return ''


def poll_operation(cfn, name, operation_id):
    """
    Wait for a StackSet operation to finish and return (ok, reason). StackSet operations have no
    botocore waiter, so poll describe_stack_set_operation until it leaves the running states.
    """
    while True:
        status = cfn.describe_stack_set_operation(
            StackSetName=name, OperationId=operation_id, CallAs=CALL_AS,
        )['StackSetOperation']['Status']
        if status == 'SUCCEEDED':
            return True, ''
        if status in ('FAILED', 'STOPPED'):
            return False, operation_failure_reason(cfn, name, operation_id) or f'operation {status}'
        time.sleep(POLL_SECONDS)


def stack_set_instance_count(cfn, name):
    """How many stack instances the set still has, or 0 if the set itself is already gone."""
    try:
        total = 0
        paginator = cfn.get_paginator('list_stack_instances')
        for page in paginator.paginate(StackSetName=name, CallAs=CALL_AS):
            total += len(page['Summaries'])
        return total
    except ClientError as e:
        if e.response['Error']['Code'] == 'StackSetNotFoundException':
            return 0
        raise


def do_create_stackset(cfn, table, row, resolved):
    """Ensure the stack set, then add this row's stack instance and record the result."""
    name = resolved['StackSetName']
    account, region = resolved['Account'], resolved['Region']

    instance = get_stack_instance(cfn, name, account, region)
    if instance:
        status = instance.get('Status')
        if status == 'CURRENT':
            # A previous run created the instance, then stopped before recording the result.
            write_status(table, row, STATUS_DEPLOYED, 'Stack instance already CURRENT', name)
            print('      already CURRENT -> DEPLOYED')
            return True
        detail = instance.get('StatusReason') or status
        write_status(table, row, STATUS_FAILED,
                     'Stack instance is ' + str(status) + ': ' + str(detail), name)
        print('      FAILED: stack instance is ' + str(status))
        return False

    try:
        ensure_stack_set(cfn, name, resolved)
    except ValidationFailure as vf:
        write_status(table, row, STATUS_VALIDATION_FAILED, str(vf))
        print('      VALIDATION_FAILED: ' + str(vf))
        return False

    operation_id = cfn.create_stack_instances(
        StackSetName=name,
        DeploymentTargets={
            'OrganizationalUnitIds': [resolved['OrganizationalUnitId']],
            'Accounts': [account],
            'AccountFilterType': ACCOUNT_FILTER_TYPE,
        },
        Regions=[region],
        ParameterOverrides=resolved['Parameters'],
        CallAs=CALL_AS,
    )['OperationId']

    print('      adding stack instance in ' + account + '/' + region + ' (typically 3-5 min)...')
    ok, reason = poll_operation(cfn, name, operation_id)
    if not ok:
        write_status(table, row, STATUS_FAILED, reason, name)
        print('      FAILED: ' + reason)
        return False

    # A SUCCEEDED operation can still create nothing: with INTERSECTION, an account that is not
    # in the OU yields an empty target set. Confirm the instance exists before reporting success,
    # so a well-formed but wrong OU or account cannot produce a false DEPLOYED.
    if get_stack_instance(cfn, name, account, region) is None:
        reason = ('No stack instance was created. Confirm account ' + account
                  + ' is a member of ' + resolved['OrganizationalUnitId'] + '.')
        write_status(table, row, STATUS_FAILED, reason, name)
        print('      FAILED: ' + reason)
        return False

    write_status(table, row, STATUS_DEPLOYED, 'Stack instance created successfully', name)
    print('      DEPLOYED')
    return True


def do_delete_stackset(cfn, table, row):
    """
    Delete only this row's stack instance. The stack set is removed only once its last instance
    is gone, so tearing down one row never affects the others that share the set.
    """
    region, customer_name, model_name = parse_deployment_target(row['DeploymentTarget'])
    account = str(row['AccountId']).strip()
    name = build_stack_name(customer_name, model_name)

    try:
        org_unit = parse_org_unit(row)
    except ValidationFailure as vf:
        write_status(table, row, STATUS_DELETE_FAILED, str(vf))
        print('      DELETE_FAILED: ' + str(vf))
        return False

    instance = get_stack_instance(cfn, name, account, region)
    if instance:
        print('      deleting stack instance ' + account + '/' + region + '...')
        operation_id = cfn.delete_stack_instances(
            StackSetName=name,
            DeploymentTargets={
                'OrganizationalUnitIds': [org_unit],
                'Accounts': [account],
                'AccountFilterType': ACCOUNT_FILTER_TYPE,
            },
            Regions=[region],
            RetainStacks=False,
            CallAs=CALL_AS,
        )['OperationId']
        ok, reason = poll_operation(cfn, name, operation_id)
        if not ok:
            write_status(table, row, STATUS_DELETE_FAILED, reason)
            print('      DELETE_FAILED: ' + reason)
            return False
    else:
        print('      no stack instance found for ' + account + '/' + region)

    secrets_note = ''
    if instance:
        secrets_note = (' The target account keeps its two Secrets Manager secrets for the 7-day '
                        'recovery window.')

    remaining = stack_set_instance_count(cfn, name)
    if remaining == 0:
        try:
            cfn.delete_stack_set(StackSetName=name, CallAs=CALL_AS)
        except ClientError as e:
            if e.response['Error']['Code'] != 'StackSetNotFoundException':
                raise
        write_status(table, row, STATUS_DELETED,
                     'Stack instance deleted and the stack set removed (last instance).'
                     + secrets_note)
        print('      DELETED (stack set removed)')
    else:
        write_status(table, row, STATUS_DELETED,
                     'Stack instance deleted; the stack set is kept because ' + str(remaining)
                     + ' instance(s) remain.' + secrets_note)
        print('      DELETED (stack set kept; ' + str(remaining) + ' instance(s) remain)')
    return True


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def print_create_groups(creates):
    """Print CREATE rows grouped by their stack set, one block per set, one line per instance."""
    groups = {}
    for resolved in creates:
        groups.setdefault(resolved['StackSetName'], []).append(resolved)

    for name in sorted(groups):
        members = sorted(groups[name], key=lambda r: (r['Account'], r['Region']))
        tag_display = ', '.join(f'{k}={v}' for k, v in members[0]['Tags'].items()) or '(none)'
        print(f'  CREATE    stack set: {name}')
        print(f'            tags:      {tag_display}')
        print(f'            instances ({len(members)}):')
        for resolved in members:
            print(f'              + {resolved["Account"]} / {resolved["Region"]} '
                  f'(OU {resolved["OrganizationalUnitId"]})')
            print(f'                  template:   {resolved["TemplateURL"]}')
            print(f'                  parameters: {len(resolved["Parameters"])} supplied, '
                  f'{len(resolved["DefaultedKeys"])} defaulted')


def cmd_plan(session, cfn, table, rows):
    creates, deletes, invalids, skips = [], [], [], []

    for row in rows:
        operation, why = classify(row)
        target = row['DeploymentTarget']

        if operation == OP_SKIP:
            skips.append((target, why))
            continue
        if operation == OP_DELETE:
            try:
                region, customer_name, model_name = parse_deployment_target(target)
            except ValidationFailure as vf:
                invalids.append((target, str(vf)))
                continue
            deletes.append((target, str(row['AccountId']).strip(), region,
                            build_stack_name(customer_name, model_name)))
            continue

        try:
            creates.append(resolve_stackset(session, row))
        except ValidationFailure as vf:
            invalids.append((target, str(vf)))

    # Rows that share a set but disagree on columns or tags are invalid, not actionable.
    conflicts = find_group_conflicts(creates)
    good_creates = []
    for resolved in creates:
        reason = conflicts.get(resolved['StackSetName'])
        if reason:
            invalids.append((resolved['Target'], reason))
        else:
            good_creates.append(resolved)

    print_create_groups(good_creates)
    for target, account, region, name in deletes:
        print(f'  DELETE    {target}')
        print(f'            stack set:  {name}')
        print(f'            instance:   {account} / {region}')
        print('            removes this instance; removes the stack set only if it is the last')
    for target, reason in invalids:
        print(f'  INVALID   {target}\n            {reason}')
    for target, why in skips:
        print(f'  SKIP      {target}\n            {why}')

    actionable = len(good_creates) + len(deletes)
    invalid = len(invalids)
    print(f'\nPlan: {actionable} actionable, {invalid} invalid, {len(skips)} skipped.')
    if invalid:
        print('Fix the INVALID rows before apply; they would be rejected.')
    return actionable, invalid


def cmd_apply(session, cfn, table, rows):
    succeeded = failed = skipped = 0

    # Resolve the CREATE rows first, so a group whose rows disagree is caught before any instance
    # is made. Each entry is a resolved dict, or ('INVALID', reason) for a row that failed here.
    resolved = {}
    for row in rows:
        if classify(row)[0] != OP_CREATE:
            continue
        try:
            resolved[row_key(row)] = resolve_stackset(session, row)
        except ValidationFailure as vf:
            resolved[row_key(row)] = ('INVALID', str(vf))

    good = [entry for entry in resolved.values() if not isinstance(entry, tuple)]
    conflicts = find_group_conflicts(good)

    for row in rows:
        operation, _ = classify(row)
        target = row['DeploymentTarget']

        if operation == OP_SKIP:
            skipped += 1
            continue

        print(f'\n  {operation}: {target}')

        # One row must never abort the run. An unexpected error is recorded on that row and the
        # run continues with the next row.
        try:
            if operation == OP_DELETE:
                ok = do_delete_stackset(cfn, table, row)
            else:
                entry = resolved[row_key(row)]
                reason = entry[1] if isinstance(entry, tuple) else conflicts.get(entry['StackSetName'])
                if reason:
                    print(f'      VALIDATION_FAILED: {reason}')
                    write_status(table, row, STATUS_VALIDATION_FAILED, reason)
                    failed += 1
                    continue
                ok = do_create_stackset(cfn, table, row, entry)
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
        description='Apply Bedrock Ops Alert deployments across an AWS Organization from a '
                    'DynamoDB registry, using service-managed CloudFormation StackSets.')
    parser.add_argument('command', choices=['plan', 'apply'])
    parser.add_argument('--table', default=DEFAULT_TABLE_NAME,
                        help=f'registry table name (default: {DEFAULT_TABLE_NAME})')
    parser.add_argument('--region', help='Region of the registry table and stack set admin '
                                         '(default: AWS config)')
    parser.add_argument('--profile', help='AWS profile to use')
    parser.add_argument('--force', action='store_true',
                        help='apply without the plan preview and confirmation prompt')
    args = parser.parse_args(argv)

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    identity = session.client('sts').get_caller_identity()

    print(f'Registry : {args.table} (organization / StackSets)')
    print(f'Account  : {identity["Account"]} (management account)')
    print(f'Identity : {identity["Arn"]}')
    print(f'Command  : {args.command}\n')

    # The registry lives in the management account and each row targets a different member
    # account, so read the whole table rather than one account's partition. The stack sets are
    # administered from the session Region (the same Region as the table).
    table = session.resource('dynamodb').Table(args.table)
    cfn = session.client('cloudformation')
    rows = scan_all_rows(table)
    if not rows:
        print(f'No rows in {args.table}.')
        return 0

    rows.sort(key=lambda r: (r['AccountId'], r['DeploymentTarget']))

    if args.command == 'plan':
        _, invalid = cmd_plan(session, cfn, table, rows)
        return 1 if invalid else 0

    # apply: preview then confirm, unless --force skips straight to applying.
    if not args.force:
        actionable, invalid = cmd_plan(session, cfn, table, rows)
        if actionable == 0:
            print('\nNothing to apply.')
            return 1 if invalid else 0
        if not confirm('\nApply these changes? [Y/n] '):
            print('Aborted. No changes made.')
            return 0
    return cmd_apply(session, cfn, table, rows)


if __name__ == '__main__':
    sys.exit(main())
