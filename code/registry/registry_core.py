# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Amazon Bedrock Ops Alert - registry core.

Engine-agnostic building blocks shared by every registry driver: the parameter contract, row
parsing and validation, parameter building, pre-flight checks, and status writes. Nothing here
calls CloudFormation, so a driver for another infrastructure-as-code tool can reuse all of it and
add only its own create, delete, and status logic.
"""

import re
from datetime import datetime, timezone

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

# ---------------------------------------------------------------------------
# Contract with code/bedrock-quota-alarm.yml
# ---------------------------------------------------------------------------

# The 30 workload template parameters, in template order. tests/test_registry.py parses the
# template and asserts this list matches exactly, so adding a template parameter without
# updating this list fails the suite.
PARAM_KEYS = [
    'CustomerName',
    'StakeholderEmailList',
    'NotificationPreference',
    'BedrockModelName',
    'InferenceProfileType',
    'BedrockModelId',
    'GeoDataResidencyRequirement',
    'InputModalities',
    'LambdaS3Bucket',
    'EnableAutomatedSupportCase',
    'SupportCaseLookbackDays',
    'UseCaseDescription',
    'TokensPerMinuteIncreasePercent',
    'TokensPerMinuteQuotaCode',
    'RequestsPerMinuteIncreasePercent',
    'RequestsPerMinuteQuotaCode',
    'ErrorThreshold',
    'CriticalAlarmEvaluationPeriods',
    'RequestsPerMinuteThresholdPercent',
    'TokensPerMinuteThresholdPercent',
    'LatencyThresholdMs',
    'WarningAlarmEvaluationPeriods',
    'LatencyAlarmPeriod',
    'LatencyAlarmEvaluationPeriods',
    'AnomalyDetectionPeriod',
    'AnomalyEvaluationPeriods',
    'AnomalySensitivity',
    'AlarmEvaluationPeriod',
    'EnableAutomatedThresholdUpdate',
    'ThresholdUpdateScheduleIntervalDays',
]

# Derived from DeploymentTarget rather than stored as columns.
DERIVED_PARAM_KEYS = ['CustomerName', 'BedrockModelName']

# Values the registry requires before deploying. CustomerName, StakeholderEmailList and
# LambdaS3Bucket have no template default. The remaining four have defaults that describe the
# example model, so a blank value would deploy against the wrong model, quota, or profile type;
# requiring them makes each row state its own values. For a model with no RPM quota, set
# RequestsPerMinuteQuotaCode to "NA".
REQUIRED_PARAM_KEYS = [
    'CustomerName',
    'StakeholderEmailList',
    'LambdaS3Bucket',
    'BedrockModelId',
    'InferenceProfileType',
    'TokensPerMinuteQuotaCode',
    'RequestsPerMinuteQuotaCode',
    'UseCaseDescription',
]

# Columns that are not workload parameters: the row key, the deployment state the registry
# writes, and the optional customer-supplied Tags input.
CONTROL_COLUMNS = [
    'AccountId',
    'DeploymentTarget',
    'Status',
    'StatusReason',
    'StackId',
    'CreatedAt',
    'UpdatedAt',
    'Tags',
]

# Every column a row may legitimately carry: the workload parameter columns (the derived keys are
# parsed from DeploymentTarget, so they are not columns), the control columns, and the
# organization method's OrganizationalUnitId input.
KNOWN_COLUMNS = (
    (set(PARAM_KEYS) - set(DERIVED_PARAM_KEYS)) | set(CONTROL_COLUMNS) | {'OrganizationalUnitId'}
)

# Lambda packages the workload stack pulls from LambdaS3Bucket at deploy time.
WORKLOAD_ARTIFACT_KEYS = [
    'notification_processor.zip',
    'quota_calculator.zip',
    'alarm_updater.zip',
    'quota_utils_layer.zip',
]

# The workload template name is fixed here rather than read from a row, so every deployment
# uses this template.
WORKLOAD_TEMPLATE_KEY = 'bedrock-quota-alarm.yml'

DEFAULT_TABLE_NAME = 'bedrock-ops-alert-registry'
STACK_NAME_PREFIX = 'bedrock-ops-alert'
TARGET_SEPARATOR = '#'

# Mirrors the workload template's own AllowedPattern / MaxLength, so a malformed target fails
# here instead of producing a broken stack name.
CUSTOMER_NAME_PATTERN = re.compile(r'^[a-zA-Z0-9-]{1,10}$')
MODEL_NAME_PATTERN = re.compile(r'^[a-zA-Z0-9-]{1,15}$')
REGION_PATTERN = re.compile(r'^[a-z]{2}(-[a-z]+)+-\d$')

STATUS_TEST = 'TEST'
STATUS_REQUESTED = 'REQUESTED'
STATUS_DEPLOYED = 'DEPLOYED'
STATUS_FAILED = 'FAILED'
STATUS_VALIDATION_FAILED = 'VALIDATION_FAILED'
STATUS_PENDING_DELETE = 'PENDING_DELETE'
STATUS_DELETED = 'DELETED'
STATUS_DELETE_FAILED = 'DELETE_FAILED'

OP_CREATE = 'CREATE'
OP_DELETE = 'DELETE'
OP_SKIP = 'SKIP'

# Stacks are tagged only from the row's Tags column. Nothing is injected automatically; the
# sample row ships with a Solution tag the customer can keep or remove. The aws: prefix is
# reserved by AWS. Max 50 tags per resource.
MAX_TAGS = 50

# AWS tag keys and values allow letters, numbers, spaces and _ . : / = + - @ only - not '#' or
# ',', so neither the DeploymentTarget separator nor the Tags separator can appear in a value.
# https://docs.aws.amazon.com/tag-editor/latest/userguide/best-practices-and-strats.html
TAG_KEY_PATTERN = re.compile(r'^[a-zA-Z0-9 _.:/=+\-@]{1,128}$')
TAG_VALUE_PATTERN = re.compile(r'^[a-zA-Z0-9 _.:/=+\-@]{0,256}$')


class ValidationFailure(Exception):
    """Pre-flight rejection. No stack is created, so there is nothing to clean up."""


# ---------------------------------------------------------------------------
# Keys and derived values
# ---------------------------------------------------------------------------

def is_blank(value):
    """Missing, empty and whitespace-only all mean 'customer left this blank'."""
    return value is None or str(value).strip() == ''


def utc_now_iso():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def build_deployment_target(region, customer_name, model_name):
    return TARGET_SEPARATOR.join([region, customer_name, model_name])


def parse_deployment_target(target):
    """
    Split <Region>#<CustomerName>#<BedrockModelName> and validate each part against the
    workload template's own constraints. Raises ValidationFailure with an actionable message.
    """
    parts = str(target).strip().split(TARGET_SEPARATOR)
    if len(parts) != 3 or any(not p.strip() for p in parts):
        raise ValidationFailure(
            f'DeploymentTarget "{target}" must be '
            f'<Region>{TARGET_SEPARATOR}<CustomerName>{TARGET_SEPARATOR}<BedrockModelName>, '
            f'e.g. us-east-1{TARGET_SEPARATOR}AcmeCorp{TARGET_SEPARATOR}G-Opus-4-6'
        )

    region, customer_name, model_name = (p.strip() for p in parts)

    if not REGION_PATTERN.match(region):
        raise ValidationFailure(f'"{region}" is not a valid Region code')
    if not CUSTOMER_NAME_PATTERN.match(customer_name):
        raise ValidationFailure(
            f'CustomerName "{customer_name}" must be 1-10 characters, alphanumeric and hyphens'
        )
    if not MODEL_NAME_PATTERN.match(model_name):
        raise ValidationFailure(
            f'BedrockModelName "{model_name}" must be 1-15 characters, alphanumeric and hyphens'
        )
    return region, customer_name, model_name


def build_stack_name(customer_name, model_name):
    """
    Stack names allow [a-zA-Z][-a-zA-Z0-9]* only. CustomerName and BedrockModelName are already
    validated to ^[a-zA-Z0-9-]+$, so no sanitising is needed.
    """
    return f'{STACK_NAME_PREFIX}-{customer_name}-{model_name}'.lower()


def secret_names(customer_name, model_name):
    base = f'{customer_name}/bedrock/quota-monitoring/{model_name}'
    return [f'{base}/customer-name', f'{base}/stakeholder-emails']


def row_key(row):
    return {'AccountId': row['AccountId'], 'DeploymentTarget': row['DeploymentTarget']}


def build_template_url(bucket, region):
    """
    S3 URL for the workload template, shared by the CloudFormation drivers.

    TemplateURL (1 MB limit) rather than TemplateBody (51,200 bytes), because the workload
    template exceeds the inline limit. Region-specific endpoint, not the legacy global form.
    """
    return f'https://{bucket}.s3.{region}.amazonaws.com/{WORKLOAD_TEMPLATE_KEY}'


# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------

def build_parameters(row, customer_name, model_name):
    """
    Build the ordered list of workload parameters as {'ParameterKey', 'ParameterValue'} pairs.

    Blank means OMIT THE KEY, never ParameterValue=''. CloudFormation applies a template default
    only when the parameter is absent; an empty string is a real value and fails MinLength /
    AllowedValues validation.

    CustomerName and BedrockModelName come from the parsed DeploymentTarget, so they cannot
    disagree with the key.
    """
    derived = {'CustomerName': customer_name, 'BedrockModelName': model_name}
    parameters = []
    for key in PARAM_KEYS:
        value = derived.get(key, row.get(key))
        if not is_blank(value):
            parameters.append({'ParameterKey': key, 'ParameterValue': str(value).strip()})
    return parameters


def find_missing_mandatory(row, customer_name=None):
    """Required values that are absent or blank. CustomerName is derived, so pass it in."""
    supplied = dict(row)
    if customer_name:
        supplied['CustomerName'] = customer_name
    return [k for k in REQUIRED_PARAM_KEYS if is_blank(supplied.get(k))]


def find_unknown_columns(row):
    """
    Row columns the registry does not recognize, sorted.

    A mistyped column (for example AnomalyaEvaluationPeriods) is read by nobody, so the parameter
    it was meant to set silently falls back to its default. Reporting it turns that silent default
    into an actionable error.
    """
    return sorted(set(row) - KNOWN_COLUMNS)


def parse_tags(row):
    """
    Parse the optional Tags column ('Key=Value,Key=Value') into an ordered dict. Nothing is
    added automatically - a blank column yields no tags. Raises ValidationFailure on a
    malformed, reserved, or over-limit tag.

    ',' separates pairs and '=' separates key from value; neither is a legal tag character, so
    a value can never contain either. split('=', 1) keeps any '=' the value might carry.
    """
    tags = {}
    raw = row.get('Tags')
    if not is_blank(raw):
        for pair in str(raw).split(','):
            pair = pair.strip()
            if not pair:
                continue
            if '=' not in pair:
                raise ValidationFailure(f'Tag "{pair}" must be written Key=Value')
            key, value = (part.strip() for part in pair.split('=', 1))
            if key.lower().startswith('aws:'):
                raise ValidationFailure(f'Tag key "{key}" uses the reserved aws: prefix')
            if not TAG_KEY_PATTERN.match(key):
                raise ValidationFailure(
                    f'Tag key "{key}" must be 1-128 characters from a-z A-Z 0-9 space _.:/=+-@'
                )
            if not TAG_VALUE_PATTERN.match(value):
                raise ValidationFailure(
                    f'Tag value for "{key}" must be 0-256 characters from a-z A-Z 0-9 space _.:/=+-@'
                )
            tags[key] = value
    if len(tags) > MAX_TAGS:
        raise ValidationFailure(f'{len(tags)} tags exceeds the AWS limit of {MAX_TAGS}')
    return tags


def to_cfn_tags(tags):
    """Convert the parsed {key: value} tag map into the [{'Key','Value'}] list AWS expects."""
    return [{'Key': key, 'Value': value} for key, value in tags.items()]


# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------

def check_artifacts_exist(s3_client, bucket, keys=None):
    """
    HeadObject the objects the workload needs in the bucket. Defaults to the 4 Lambda zips plus
    the CloudFormation template (what the CloudFormation drivers need); the Terraform driver
    passes only the zips, since it does not use the template. A missing artifact otherwise fails
    mid-deploy.

    Also confirms the bucket is in the deployment Region (the s3_client's Region). The bucket
    must be co-located: Lambda loads its code only from a same-Region bucket, and the
    CloudFormation template URL must use that Region's endpoint. A cross-Region bucket would
    otherwise pass here and then fail mid-deploy. The Region is read from the HeadBucket response
    header, so this needs no extra call or permission.
    """
    if keys is None:
        keys = WORKLOAD_ARTIFACT_KEYS + [WORKLOAD_TEMPLATE_KEY]
    try:
        head = s3_client.head_bucket(Bucket=bucket)
    except ClientError as e:
        code = e.response['Error']['Code']
        if code in ('404', 'NoSuchBucket'):
            raise ValidationFailure(f'LambdaS3Bucket "{bucket}" does not exist')
        if code in ('403', 'AccessDenied'):
            raise ValidationFailure(f'Access denied to bucket "{bucket}"')
        raise

    bucket_region = head['ResponseMetadata']['HTTPHeaders'].get('x-amz-bucket-region')
    expected_region = s3_client.meta.region_name
    if bucket_region and expected_region and bucket_region != expected_region:
        raise ValidationFailure(
            f'LambdaS3Bucket "{bucket}" is in {bucket_region}, but the deployment Region is '
            f'{expected_region}. Artifacts must be in a bucket in the deployment Region '
            f'(Lambda loads its code from a same-Region bucket). Create a bucket in '
            f'{expected_region} and upload the artifacts there.'
        )

    missing = []
    for key in keys:
        try:
            s3_client.head_object(Bucket=bucket, Key=key)
        except ClientError as e:
            code = e.response['Error']['Code']
            if code in ('404', 'NoSuchKey', 'NotFound'):
                missing.append(key)
            elif code in ('403', 'AccessDenied'):
                raise ValidationFailure(f'Access denied reading s3://{bucket}/{key}')
            elif code == 'NoSuchBucket':
                raise ValidationFailure(f'LambdaS3Bucket "{bucket}" does not exist')
            else:
                raise
    if missing:
        raise ValidationFailure(
            f'Missing in s3://{bucket}/: {", ".join(missing)}. Package and upload first.'
        )


# Row InferenceProfileType maps 1:1 to the API's inference-profile "type" field.
PROFILE_TYPE_TO_API = {'Application': 'APPLICATION', 'System-Defined': 'SYSTEM_DEFINED'}
# Reverse map, so status messages use the row's own vocabulary (System-Defined / Application),
# the same values the workload template accepts, rather than the API's SYSTEM_DEFINED spelling.
API_TO_PROFILE_TYPE = {api: row for row, api in PROFILE_TYPE_TO_API.items()}


def probe_model_availability(session, region, model_id, profile_type):
    """
    Confirm the model is reachable in the target Region and matches the declared
    InferenceProfileType. If it is absent the QuotaCalculator custom resource fails and the
    whole stack rolls back, which is slow and leaves cleanup behind.

    Returns a short status built from the resolved type, shown in the row's own vocabulary
    (System-Defined / Application). Raises ValidationFailure when the model is absent or its type
    disagrees with the row. Only a permission gap reports unconfirmed, so a missing IAM action
    cannot block an otherwise valid deployment.
    """
    bedrock = session.client('bedrock', region_name=region)
    skip = ('AccessDeniedException', 'UnrecognizedClientException')
    expected = PROFILE_TYPE_TO_API.get(profile_type, 'SYSTEM_DEFINED')

    try:
        profile = bedrock.get_inference_profile(inferenceProfileIdentifier=model_id)
        actual = profile.get('type')
        if actual != expected:
            raise ValidationFailure(
                f'BedrockModelId "{model_id}" is an inference profile of type '
                f'{API_TO_PROFILE_TYPE.get(actual, actual)}, but the row declares '
                f'InferenceProfileType={profile_type}. Correct one to match the other.'
            )
        models = profile.get('models') or []
        routed = ' -> ' + models[0]['modelArn'].split('/')[-1] if models else ''
        return f'confirmed ({API_TO_PROFILE_TYPE.get(actual, actual)}{routed})'
    except ClientError as e:
        profile_error = e.response['Error']['Code']

    if profile_error in skip:
        return f'unconfirmed ({profile_error})'

    # Application profiles exist only as inference profiles - no foundation-model fallback.
    if profile_type == 'Application':
        raise ValidationFailure(
            f'Application inference profile "{model_id}" not found in {region} ({profile_error})'
        )

    # A System-Defined id may instead be a bare foundation model id (no Region prefix). The row
    # declared System-Defined, so report that; the absent " -> " routing marks it as a
    # foundation model rather than a system-defined inference profile.
    try:
        bedrock.get_foundation_model(modelIdentifier=model_id)
        return 'confirmed (System-Defined)'
    except ClientError as e:
        model_error = e.response['Error']['Code']

    if model_error in skip:
        return f'unconfirmed ({model_error})'
    if model_error in ('ResourceNotFoundException', 'ValidationException'):
        raise ValidationFailure(
            f'BedrockModelId "{model_id}" is not available in {region} '
            f'(profiles: {profile_error}, models: {model_error})'
        )
    return f'unconfirmed ({profile_error} / {model_error})'


# ---------------------------------------------------------------------------
# Row state
# ---------------------------------------------------------------------------

def classify(row):
    """Decide what a row needs, from its Status alone."""
    status = str(row.get('Status', '')).strip().upper()
    if status == STATUS_TEST:
        return OP_SKIP, 'sample row (Status=TEST)'
    if status in ('', STATUS_REQUESTED):
        return OP_CREATE, 'new deployment'
    if status == STATUS_PENDING_DELETE:
        return OP_DELETE, 'teardown requested'
    return OP_SKIP, f'terminal ({status}); clear Status to act again'


def query_account_rows(table, account_id):
    """All rows for this account. Paginated Query, not a Scan - AccountId is the partition key."""
    rows = []
    kwargs = {'KeyConditionExpression': Key('AccountId').eq(account_id)}
    while True:
        response = table.query(**kwargs)
        rows.extend(response['Items'])
        if 'LastEvaluatedKey' not in response:
            return rows
        kwargs['ExclusiveStartKey'] = response['LastEvaluatedKey']


def scan_all_rows(table):
    """
    Every row in the table, across all accounts. Paginated Scan, used by the organization driver
    where the registry lives in the management account and each row targets a different account.
    """
    rows = []
    kwargs = {}
    while True:
        response = table.scan(**kwargs)
        rows.extend(response['Items'])
        if 'LastEvaluatedKey' not in response:
            return rows
        kwargs['ExclusiveStartKey'] = response['LastEvaluatedKey']


def write_status(table, row, status, reason='', stack_id=None):
    """
    Write the row's Status and reason, refresh UpdatedAt, and set CreatedAt on the first write.

    CreatedAt is the immutable creation time. The seed row carries it as an empty string, so
    DynamoDB's if_not_exists would never fill it; it is therefore set whenever the row has no
    real value yet, and left unchanged afterwards.
    """
    parts = ['#st = :status', '#sr = :reason', 'UpdatedAt = :now']
    values = {':status': status, ':reason': str(reason)[:1000], ':now': utc_now_iso()}
    if is_blank(row.get('CreatedAt')):
        parts.append('CreatedAt = :now')
    if stack_id:
        parts.append('StackId = :sid')
        values[':sid'] = stack_id

    table.update_item(
        Key=row_key(row),
        UpdateExpression='SET ' + ', '.join(parts),
        ExpressionAttributeNames={'#st': 'Status', '#sr': 'StatusReason'},
        ExpressionAttributeValues=values,
    )


# ---------------------------------------------------------------------------
# Operator prompt
# ---------------------------------------------------------------------------

def confirm(prompt):
    """Return True if the operator confirms. Empty answer means yes; non-interactive means no."""
    try:
        return input(prompt).strip().lower() in ('', 'y', 'yes')
    except EOFError:
        return False
