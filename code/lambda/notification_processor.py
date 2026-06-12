# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Amazon Bedrock Ops Alert — Notification Processor Lambda

Processes composite alarm events from CloudWatch via SNS. Orchestrates two workflows:
1. Support case creation (quota increase or service investigation)
2. Email notification to stakeholders.

Flow: SNS → handler → handle_alarm_and_case:
  ├─ Compute scenario + case_type_suffix (always available for email)
  ├─ create_support_case (may return new case, existing case, or None)
  └─ send_email_notification (always runs in finally block)

Smart Quota Guard: Compares 14-day peak usage against calculated thresholds to
route support case content to the appropriate scenario (high_usage, low_usage,
new_model, non_quota). Non-quota alarms (ServerErrors, HighLatency, LatencyAnomaly)
get investigation-focused content instead of quota increase requests.
"""
import boto3
import logging
import json
import os
import re
from datetime import datetime, timedelta, timezone
from botocore.waiter import WaiterModel, create_waiter_with_client
from botocore.exceptions import WaiterError
from botocore.config import Config as BotoConfig
from quota_utils import get_usage_metrics, get_stored_thresholds, determine_support_case_scenario, determine_case_type_suffix, CASE_TYPE_QUOTA_REQUEST, CASE_TYPE_INVESTIGATION_REQUEST, RPM_DISABLED_SENTINEL

# Product name used in support case subject, email subject, body header, and duplicate detection
PRODUCT_NAME = 'Bedrock Ops Alert'

logger = logging.getLogger()
logger.setLevel(logging.INFO)
secrets_client = boto3.client('secretsmanager')
ssm = boto3.client('ssm')
sns_client = boto3.client('sns')
cloudwatch_client = boto3.client('cloudwatch')
support_client = boto3.client('support')
service_quotas_client = boto3.client('service-quotas')
# Retry-configured client for Service Quotas write operations (prone to throttling)
# Matches pattern from quota_utils.py validate_quota_codes()
service_quotas_retry_client = boto3.client(
    'service-quotas',
    config=BotoConfig(retries={'mode': 'adaptive', 'max_attempts': 10})
)

def get_secret(secret_name):
    """Retrieve secret value from Secrets Manager. Returns 'Not available' on failure."""
    try:
        return secrets_client.get_secret_value(SecretId=secret_name)['SecretString']
    except Exception as e:
        logger.error(f"Failed to get secret: {str(e)}")
        return 'Not available'

def _resolve_case_details(case_id):
    """
    Resolve displayId, subject, and timeCreated from a Support case ID.
    Uses describe_cases API to get the human-readable case identifiers.
    
    Returns:
        dict with 'display_id', 'subject', 'time_created'. Falls back to case_id on failure.
    """
    result = {'display_id': case_id, 'subject': '', 'time_created': ''}
    try:
        case_details = support_client.describe_cases(caseIdList=[case_id], includeResolvedCases=False)
        cases = case_details.get('cases', [])
        if cases:
            result['display_id'] = cases[0].get('displayId', case_id)
            result['subject'] = cases[0].get('subject', '')
            result['time_created'] = cases[0].get('timeCreated', '')
    except Exception as e:
        logger.warning(f"Failed to resolve case details for {case_id}: {str(e)}")
    return result


def _resolve_display_id_to_case_id(display_id):
    """
    Convert a Service Quotas display ID (e.g., '178093546000760') to the internal
    AWS Support caseId format (e.g., 'case-578707974125-muen-2026-XXXXX').
    Service Quotas returns display IDs in its CaseId field; AWS Support API requires internal format.
    
    Returns:
        Internal caseId string if found, None otherwise.
    """
    try:
        case_details = support_client.describe_cases(displayId=display_id, includeResolvedCases=False)
        cases = case_details.get('cases', [])
        if cases:
            internal_id = cases[0].get('caseId')
            logger.info(f"Resolved display ID {display_id} to internal caseId: {internal_id}")
            return internal_id
    except Exception as e:
        logger.warning(f"Failed to resolve display ID {display_id} to internal caseId: {str(e)}")
    return None


def _check_alarm_dedup(case_id, triggered_eligible):
    """
    Check if this alarm type should be suppressed (already communicated on this case).
    Only anomaly and high-latency alarms are dedup-checked (frequent re-triggers).
    Non-anomaly alarms (ServerErrors, Throttles, etc.) always proceed.
    
    Returns:
        True if alarm should be suppressed, False if should proceed with append.
    """
    alarm_to_check = triggered_eligible[0] if triggered_eligible else ''
    if not alarm_to_check:
        return False
    is_dedup_alarm = 'Anomaly' in alarm_to_check or 'HighLatency' in alarm_to_check
    if is_dedup_alarm and has_alarm_already_appended(case_id, alarm_to_check):
        logger.info(f"Alarm {alarm_to_check} already communicated on case {case_id} — suppressing")
        return True
    return False


def _append_to_case(case_id, body):
    """
    Append communication body to an existing support case.
    Logs success or failure. Returns True on success, False on failure.
    """
    try:
        support_client.add_communication_to_case(
            caseId=case_id,
            communicationBody=body
        )
        logger.info(f"Appended communication to case: {case_id}")
        return True
    except Exception as e:
        logger.error(f"Failed to append to case {case_id}: {str(e)}")
        return False


def has_alarm_already_appended(case_id, alarm_pattern):
    """
    Check if a specific alarm type has already been communicated on this case.
    Scans case correspondence (excluding AWS Support engineer replies) for the
    alarm pattern in the CHILD ALARM TRIGGERED field.
    
    Uses early-exit: returns True as soon as the pattern is found on any page.
    Fails open (returns False) on API errors — better to append a duplicate
    than silently suppress a legitimate first-time signal.
    
    Args:
        case_id: AWS Support case ID
        alarm_pattern: Alarm name pattern to search for (e.g., 'Acme-Bedrock-InvocationAnomaly-Warning-G-Opus-4-6')
    
    Returns:
        True if alarm pattern found (suppress append), False otherwise (proceed with append)
    """
    try:
        paginator = support_client.get_paginator('describe_communications')
        for page in paginator.paginate(caseId=case_id):
            for comm in page.get('communications', []):
                # Skip AWS Support engineer replies to avoid false matches from quoted content
                if comm.get('submittedBy') == 'Amazon Web Services':
                    continue
                # Check if this alarm type was already communicated
                if alarm_pattern in comm.get('body', ''):
                    logger.info(f"Alarm pattern '{alarm_pattern}' already found in case {case_id} — suppressing append")
                    return True
        return False
    except Exception as e:
        logger.error(f"Failed to check case communications for dedup (failing open): {str(e)}")
        return False

def poll_until_composite_ok(cloudwatch, composite_alarm_name, child_alarm_names, eligible_patterns):
    """
    Poll until composite alarm goes OK, checking for eligible child alarms.
    Uses boto3 waiter for delay (no time.sleep).
    
    Returns:
        List of eligible alarm names in ALARM state, or empty list
    """
    
    # Waiter for 10-second delay between checks
    waiter_config = {
        "version": 2,
        "waiters": {
            "Delay": {
                "operation": "DescribeAlarms",
                "delay": 10,
                "maxAttempts": 30,  # 30 * 10s = 300s (Lambda timeout)
                "acceptors": [{"matcher": "status", "expected": 200, "state": "success"}]
            }
        }
    }
    
    delay_waiter = create_waiter_with_client('Delay', WaiterModel(waiter_config), cloudwatch)
    
    logger.info(f"Polling until composite alarm {composite_alarm_name} goes OK...")
    
    logger.info(f"Eligible Alarm: {eligible_patterns}")
    # Loop until composite goes OK (Lambda timeout is natural limit)
    while True:
        # Check composite alarm state
        composite_response = cloudwatch.describe_alarms(
            AlarmNames=[composite_alarm_name],
            AlarmTypes=['CompositeAlarm']
        )
        
        if not composite_response.get('CompositeAlarms'):
            logger.error("Composite alarm not found")
            return []
        
        composite_state = composite_response['CompositeAlarms'][0]['StateValue']
        
        # Exit condition: Composite is OK
        if composite_state == 'OK':
            logger.info("Composite alarm is OK - no support case needed")
            return []
        
        # Check for eligible child alarms
        child_response = cloudwatch.describe_alarms(
            AlarmNames=child_alarm_names,
            AlarmTypes=['MetricAlarm']
        )
        
        triggered = [a['AlarmName'] for a in child_response['MetricAlarms'] if a['StateValue'] == 'ALARM']
        triggered_eligible = [a for a in triggered if any(pattern in a for pattern in eligible_patterns)]
        
        # Found eligible alarms - create support case
        if triggered_eligible:
            logger.info(f"Found eligible alarms: {triggered_eligible}")
            return triggered_eligible
        
        logger.info(f"Composite=ALARM, Eligible=0, waiting 10s...")
        
        # Wait 10 seconds before next check (no time.sleep!)
        try:
            delay_waiter.wait(AlarmNames=[composite_alarm_name])
        except WaiterError as e:
            logger.info(f"Waiter completed: {e}")
            return []

def handler(event, context):
    """Lambda entry point. Routes SNS alarm events to the alarm-and-case workflow."""
    try:
        logger.info(f"Event: {event}")
        if 'Records' in event:
            return handle_alarm_and_case(event, context)
        else:
            return {'statusCode': 400, 'body': 'Unknown event type'}
    except Exception as e:
        logger.error(f"Error: {str(e)}")
        return {'statusCode': 500, 'body': str(e)}

def request_quota_increase_via_service_quotas(alarm_ctx, quota_requests, usage_metrics, scenario, thresholds, case_type_suffix, triggered_eligible):
    """
    Deterministic quota path: Use Service Quotas API to request quota increase.
    AWS auto-creates a support case (title: "Quota Increase: Bedrock"), then we
    append our context body to that case.
    
    Called from create_support_case() Step 7.5 when deterministic quota alarms fire.
    Falls back to existing create_case() path on any failure (returns None).
    
    Flow:
      For each quota in quota_requests:
        1. request_service_quota_increase(SupportCaseAllowed=True)
        2. Extract CaseId from response
        3. _append_to_case with full case_body (all quota details)
      
      On ResourceAlreadyExistsException (pending request exists):
        1. list_requested_service_quota_change_history_by_quota → get CaseId
        2. _check_alarm_dedup (suppress if already communicated)
        3. _append_to_case if not duplicate
    
    Returns:
        case_result dict on success, None on failure (triggers fallback to existing path)
    """
    if not quota_requests:
        return None
    
    # Case subject for the return dict (auto-created case title is "Quota Increase: Bedrock")
    case_subject = "Quota Increase: Bedrock"
    
    # Process first quota via Service Quotas API — remaining quotas are included
    # in the case_body context (build_msg_body receives full quota_requests list).
    # Early return on first success/failure is intentional.
    for quota in quota_requests:
        try:
            logger.info(f"Requesting quota increase via Service Quotas API: {quota['code']} -> {quota['new']}")
            response = service_quotas_retry_client.request_service_quota_increase(
                ServiceCode='bedrock',
                QuotaCode=quota['code'],
                DesiredValue=float(quota['new']),
                SupportCaseAllowed=True
            )
            
            requested_quota = response.get('RequestedQuota', {})
            request_id = requested_quota.get('Id')
            case_id = requested_quota.get('CaseId')
            status = requested_quota.get('Status', '')
            logger.info(f"Quota increase request submitted: status={status}, request_id={request_id}, case_id={case_id}")
            
            if not request_id:
                logger.warning(f"No RequestId returned for quota {quota['code']} — falling back")
                return None
            
            # CaseId may not be in the immediate response (async lag ~1-2s).
            # Use custom waiter on GetRequestedServiceQuotaChange to poll until CaseId appears.
            if not case_id:
                logger.info(f"CaseId not in immediate response — waiting for async case creation")
                case_id = _wait_for_quota_case_id(request_id)
                logger.info(f"Post waiter found service quota case id: {case_id}")
            
            if not case_id:
                # Quota is successfully queued but no case available yet.
                # Do NOT fall back to create_case (would create duplicate).
                logger.info(f"Quota increase submitted (request_id={request_id}) but no CaseId available — returning without append")
                return {
                    'case_id': None, 'display_id': request_id,
                    'subject': "Quota Increase: Bedrock",
                    'case_type_suffix': case_type_suffix, 'scenario': scenario,
                    'is_append': False, 'quotas': quota_requests,
                    'quota_request_id': request_id
                }
            
            # CaseId found — Service Quotas returns displayId format, resolve to internal caseId
            case_id = _resolve_display_id_to_case_id(case_id)
            if not case_id:
                logger.warning("Could not resolve display ID to internal caseId — returning without append")
                return {
                    'case_id': None, 'display_id': request_id,
                    'subject': "Quota Increase: Bedrock",
                    'case_type_suffix': case_type_suffix, 'scenario': scenario,
                    'is_append': False, 'quotas': quota_requests,
                    'quota_request_id': request_id
                }
            
            details = _resolve_case_details(case_id)
            display_id = details['display_id']
            case_subject = details['subject'] if details['subject'] else "Quota Increase: Bedrock"
            
            # Wait for AWS Support automation reply before appending our context.
            # Service Quotas cases get an auto-reply (~60s) asking for info. If we append
            # before that, our data doesn't register as a "reply" and case stays pending.
            # Wait up to 2 min, then append anyway if automation doesn't fire.
            _wait_for_support_automation_reply(case_id)
            
            case_body = build_msg_body(alarm_ctx, quota_requests, usage_metrics, scenario, thresholds)
            _append_to_case(case_id, case_body)
            
            return {
                'case_id': case_id, 'display_id': display_id, 'subject': case_subject,
                'case_type_suffix': case_type_suffix, 'scenario': scenario,
                'is_append': False, 'quotas': quota_requests
            }
        
        except service_quotas_retry_client.exceptions.ResourceAlreadyExistsException:
            logger.info("At service_quotas_retry_client.exceptions.ResourceAlreadyExistsException")
            # Pending request already exists for this quota — find the case and append
            logger.info(f"Pending quota request already exists for {quota['code']} — looking up case")
            try:
                case_id = _get_pending_quota_request_case_id(quota['code'])
                logger.info(f"Post polling found service quota case id: {case_id}")
                if not case_id:
                    logger.warning(f"Could not find CaseId for pending request {quota['code']} — falling back")
                    return None
                
                # Service Quotas returns displayId format — resolve to internal caseId
                case_id = _resolve_display_id_to_case_id(case_id)
                if not case_id:
                    logger.warning("Could not resolve display ID to internal caseId — falling back")
                    return None
                
                # Resolve case details
                details = _resolve_case_details(case_id)
                display_id = details['display_id']
                time_created = details['time_created']
                case_subject = details['subject'] if details['subject'] else "Quota Increase: Bedrock"
                
                # Dedup: check if this alarm type was already appended
                if _check_alarm_dedup(case_id, triggered_eligible):
                    return {
                        'case_id': case_id, 'display_id': display_id,
                        'subject': case_subject, 'case_type_suffix': case_type_suffix,
                        'scenario': scenario, 'is_suppressed': True,
                        'time_created': time_created, 'quotas': quota_requests
                    }
                
                # Append context to existing case
                case_body = build_msg_body(alarm_ctx, quota_requests, usage_metrics, scenario, thresholds, is_append=True)
                _append_to_case(case_id, case_body)
                
                return {
                    'case_id': case_id, 'display_id': display_id,
                    'subject': case_subject, 'case_type_suffix': case_type_suffix,
                    'scenario': scenario, 'is_append': True,
                    'time_created': time_created, 'quotas': quota_requests
                }
            except Exception as e:
                logger.error(f"Failed to handle existing quota request for {quota['code']}: {str(e)}")
                return None
        
        except Exception as e:
            logger.error(f"Service Quotas API failed for {quota['code']}: {str(e)}")
            return None
    
    return None


def _wait_for_support_automation_reply(case_id):
    """
    Wait for AWS Support automation reply on a newly created Service Quotas case.
    
    Service Quotas cases receive an automated reply (~60s after creation) from
    "Amazon Web Services" asking for additional info. Appending our context AFTER
    this reply ensures it registers as a customer response, moving the case from
    'pending-customer-action' to 'pending-amazon-action'.
    
    Only called for first-time service quota case creation (not appends to existing cases).
    
    Polls describe_communications every 5s, max 2 minutes.
    If automation doesn't fire within 2 min, returns anyway (append proceeds regardless).
    Throttle errors are handled defensively — polling continues on any exception.
    """
    import time
    max_attempts = 150     # 150 × 2s = 300s (5 minutes)
    delay_seconds = 2
    
    logger.info(f"Waiting for AWS Support automation reply on case {case_id}...")
    
    for attempt in range(max_attempts):
        time.sleep(delay_seconds)
        
        try:
            paginator = support_client.get_paginator('describe_communications')
            for page in paginator.paginate(caseId=case_id):
                for comm in page.get('communications', []):
                    if comm.get('submittedBy') == 'Amazon Web Services':
                        logger.info(f"AWS Support automation replied on case {case_id} (attempt {attempt + 1})")
                        return
        except Exception as e:
            # Throttle or transient error — continue polling, don't abort.
            # 5s interval is generous; transient failures recover on next attempt.
            logger.warning(f"Failed to check communications on case {case_id} (attempt {attempt + 1}): {str(e)}")
    
    # Timeout — automation didn't fire within 2 min. Append anyway.
    logger.info(f"AWS Support automation did not reply within 2 min on case {case_id} — proceeding with append")


def _get_pending_quota_request_case_id(quota_code):
    """
    Retrieve CaseId from a pending/open quota increase request for a specific quota code.
    Uses botocore waiter pattern (no time.sleep) for delay between attempts.
    CaseId is populated asynchronously (~1-2s after request_service_quota_increase).
    
    Returns:
        CaseId string if found, None otherwise.
    """
    # Waiter for 2-second delay between checks (same pattern as poll_until_composite_ok)
    waiter_config = {
        "version": 2,
        "waiters": {
            "CaseIdDelay": {
                "operation": "DescribeAlarms",
                "delay": 2,
                "maxAttempts": 3,
                "acceptors": [{"matcher": "status", "expected": 200, "state": "success"}]
            }
        }
    }
    delay_waiter = create_waiter_with_client('CaseIdDelay', WaiterModel(waiter_config), cloudwatch_client)
    
    max_attempts = 3
    attempt = 0
    
    while attempt < max_attempts:
        for status in ['PENDING', 'CASE_OPENED']:
            try:
                response = service_quotas_retry_client.list_requested_service_quota_change_history_by_quota(
                    ServiceCode='bedrock',
                    QuotaCode=quota_code,
                    Status=status
                )
                requests = response.get('RequestedQuotas', [])
                if requests:
                    case_id = requests[0].get('CaseId')
                    if case_id:
                        logger.info(f"Found {status} quota request for {quota_code}: CaseId={case_id} (attempt {attempt + 1})")
                        return case_id
            except Exception as e:
                logger.warning(f"Failed to list {status} quota requests for {quota_code}: {str(e)}")
        
        attempt += 1
        if attempt < max_attempts:
            logger.info(f"CaseId not available yet (attempt {attempt}/{max_attempts}) — waiting 2s")
            try:
                delay_waiter.wait(AlarmNames=['_delay_only'])
            except WaiterError:
                pass  # Waiter timeout is expected — we just want the delay
    
    return None


def _wait_for_quota_case_id(request_id):
    """
    Wait for CaseId to become available on a quota increase request.
    CaseId is populated asynchronously (~1-2s after request_service_quota_increase).
    Uses time.sleep for delay between polls.
    
    Args:
        request_id: The quota increase request ID from request_service_quota_increase response.
    
    Returns:
        CaseId string if found, None if max attempts exceeded.
    """
    import time
    max_attempts = 10
    delay_seconds = 2
    
    logger.info(f"Waiting for CaseId on quota request {request_id}...")
    
    while max_attempts > 0:
        time.sleep(delay_seconds)
        
        try:
            response = service_quotas_retry_client.get_requested_service_quota_change(RequestId=request_id)
            case_id = response.get('RequestedQuota', {}).get('CaseId')
            if case_id:
                logger.info(f"CaseId found: {case_id}")
                return case_id
        except Exception as e:
            logger.warning(f"Failed to get quota request status: {str(e)}")
        
        max_attempts -= 1
        if max_attempts > 0:
            logger.info(f"CaseId not available yet ({max_attempts} attempts remaining)")
    
    return None


def create_support_case(alarm_ctx, scenario, case_type_suffix, usage_metrics, thresholds):
    """
    Create an AWS Support case for quota increase or service investigation.
    
    Flow: validate config → verify support plan → poll for eligible child alarms →
    determine quota type (RPM/TPM/both) → dedup check → create or append case.
    
    Scenario, case_type_suffix, usage_metrics, and thresholds are computed by the
    caller (handle_alarm_and_case) so they're always available for the email.
    
    Returns:
        New case: {case_id, display_id, subject, case_type_suffix, scenario, is_append: False}
        Appended case: {case_id, display_id, subject, ..., is_append: True, time_created}
        None: case creation disabled, failed, or no eligible alarms
    """
    try:
        alarm_name = alarm_ctx['alarm_name']
        model_id = alarm_ctx['model_id']
        model_name = alarm_ctx['model_name']
        customer_name = alarm_ctx['customer_name']
        
        # --- Step 1: Validate configuration from Parameter Store ---
        enable_support_case = ssm.get_parameter(Name=os.environ['ENABLE_AUTOMATED_SUPPORT_CASE_PARAM'])['Parameter']['Value']
        if enable_support_case.lower() != 'yes':
            logger.info("Automated support case disabled")
            return None
        
        tokens_increase_percent = int(ssm.get_parameter(Name=os.environ['TOKENS_PER_MINUTE_INCREASE_PERCENT_PARAM'])['Parameter']['Value'])
        requests_increase_percent = int(ssm.get_parameter(Name=os.environ['REQUESTS_PER_MINUTE_INCREASE_PERCENT_PARAM'])['Parameter']['Value'])
        
        if tokens_increase_percent == 0 and requests_increase_percent == 0:
            logger.info("Both quota increase percentages set to 0, skipping")
            return None
        
        logger.info(f"Model: {model_name}, Tokens: {tokens_increase_percent}%, Requests: {requests_increase_percent}%")
        
        # --- Step 2: Verify AWS Support plan availability ---
        try:
            if not support_client.describe_severity_levels().get('severityLevels'):
                logger.warning("No support plan available")
                return None
            logger.info("Support plan verified")
        except Exception as e:
            logger.error(f"Support plan check failed: {str(e)}")
            return None
        
        # --- Step 3: Validate quota codes ---
        rpm_quota_code = os.environ.get('REQUESTS_PER_MINUTE_QUOTA_CODE', '').strip()
        tpm_quota_code = os.environ.get('TOKENS_PER_MINUTE_QUOTA_CODE', '').strip()
        
        if requests_increase_percent > 0 and not rpm_quota_code:
            logger.error(f"RequestsPerMinuteQuotaCode required when RequestsPerMinuteIncreasePercent={requests_increase_percent}%")
            return None
        
        if tokens_increase_percent > 0 and not tpm_quota_code:
            logger.error(f"TokensPerMinuteQuotaCode required when TokensPerMinuteIncreasePercent={tokens_increase_percent}%")
            return None
        
        # --- Step 4: Extract child alarms from composite alarm rule ---
        try:
            logger.info(f"Describe Alarm : {alarm_name}")
            alarm_response = cloudwatch_client.describe_alarms(AlarmNames=[alarm_name], AlarmTypes=['CompositeAlarm'])
            composite_alarms = alarm_response.get('CompositeAlarms', [])
            
            if not composite_alarms:
                logger.error(f"Composite alarm {alarm_name} not found")
                return None
            
            alarm_rule = composite_alarms[0].get('AlarmRule', '')
            child_alarm_names = re.findall(r'ALARM\(([^)]+)\)', alarm_rule)
            logger.info(f"Found {len(child_alarm_names)} child alarms in composite")
        except Exception as e:
            logger.error(f"Failed to get composite alarm details: {str(e)}")
            return None
        
        # Get eligible alarm patterns from environment variable
        eligible_patterns = os.environ.get(
            'ELIGIBLE_ALARM_PATTERNS',
            'ServerErrors-Critical,Throttles-Critical,ClientErrors-Critical,HighLatency-Warning,LatencyAnomaly-Warning,HighInvocationRate-Warning,HighTPMQuotaUsage-Warning,InvocationAnomaly-Warning,InputTokenAnomaly-Warning,OutputTokenAnomaly-Warning'
        ).split(',')
        
        # --- Step 5: Poll until composite goes OK or eligible child alarms found ---
        triggered_eligible = poll_until_composite_ok(cloudwatch_client, alarm_name, child_alarm_names, eligible_patterns)
        
        if not triggered_eligible:
            logger.info("No eligible alarms found for support case creation")
            return None
        
        # --- Step 6: Fetch current quota values from Service Quotas API ---
        reason_text = ', '.join(triggered_eligible)
        model_quota_map = {}
        
        if rpm_quota_code:
            try:
                rpm_q = service_quotas_client.get_service_quota(ServiceCode='bedrock', QuotaCode=rpm_quota_code)['Quota']
                model_quota_map['rpm'] = {'code': rpm_quota_code, 'name': rpm_q['QuotaName'], 'value': rpm_q['Value']}
                logger.info(f"RPM: {rpm_quota_code}={rpm_q['Value']}")
            except Exception as e:
                logger.warning(f"RPM quota lookup skipped ({rpm_quota_code}): {str(e)}")
        if tpm_quota_code:
            try:
                tpm_q = service_quotas_client.get_service_quota(ServiceCode='bedrock', QuotaCode=tpm_quota_code)['Quota']
                model_quota_map['tpm'] = {'code': tpm_quota_code, 'name': tpm_q['QuotaName'], 'value': tpm_q['Value']}
                logger.info(f"TPM: {tpm_quota_code}={tpm_q['Value']}")
            except Exception as e:
                logger.warning(f"TPM quota lookup skipped ({tpm_quota_code}): {str(e)}")
        
        if not model_quota_map:
            logger.error("No valid quota codes available — cannot determine quota requests")
            return None
        
        # --- Step 7: Determine which quotas to request (RPM, TPM, or both) ---
        quota_requests = []
        
        rpm_alarms = os.environ.get('RPM_ALARM_PATTERNS', 'HighInvocationRate,InvocationAnomaly').split(',')
        tpm_alarms = os.environ.get('TPM_ALARM_PATTERNS', 'HighTPMQuotaUsage,InputTokenAnomaly,OutputTokenAnomaly').split(',')
        
        # Deterministic quota alarms: alarms that definitively indicate quota consumption.
        # RPM alarms (request rate), TPM alarms (token rate), and Throttles (HTTP 429 = quota exceeded).
        # Reference: https://docs.aws.amazon.com/bedrock/latest/userguide/scaling-throughput-best-practices.html
        deterministic_quota_alarms = rpm_alarms + tpm_alarms
        
        # Smart Quota Guard: Throttles and ClientErrors trigger both RPM+TPM increases
        # because the specific quota breached cannot be determined from the metric alone.
        # ServerErrors, HighLatency, LatencyAnomaly are NOT quota-related.
        unknown_quota_alarms = ['Throttles-Critical', 'ClientErrors-Critical']
        has_unknown_quota_issue = any(alarm in reason_text for alarm in unknown_quota_alarms)
        
        if (any(rpm_alarm in reason_text for rpm_alarm in rpm_alarms) or has_unknown_quota_issue) and requests_increase_percent > 0 and 'rpm' in model_quota_map:
            q = model_quota_map['rpm']
            new_val = int(q['value'] * (1 + requests_increase_percent / 100))
            quota_requests.append({'code': q['code'], 'name': f"Model Inference requests per minute for {model_name} ({model_id})", 'percent': requests_increase_percent, 'current': q['value'], 'new': new_val})
            logger.info(f"RPM: {q['value']} -> {new_val}")
        
        if (any(tpm_alarm in reason_text for tpm_alarm in tpm_alarms) or has_unknown_quota_issue) and tokens_increase_percent > 0 and 'tpm' in model_quota_map:
            q = model_quota_map['tpm']
            new_val = int(q['value'] * (1 + tokens_increase_percent / 100))
            quota_requests.append({'code': q['code'], 'name': f"Model Inference tokens per minute for {model_name} ({model_id})", 'percent': tokens_increase_percent, 'current': q['value'], 'new': new_val})
            logger.info(f"TPM: {q['value']} -> {new_val}")
        
        # Non-quota scenarios: create case for service-side investigation (no quota details)
        # Quota-related scenarios: require at least one quota request
        if not quota_requests and scenario != 'non_quota':
            logger.info("No quota increase needed and not a non-quota alarm scenario")
            return None
        
        case_subject = f"{customer_name} - {PRODUCT_NAME} - {model_name} - {case_type_suffix}"
        
        # --- Step 7.5: Deterministic quota path via Service Quotas API ---
        # For alarms that definitively indicate quota consumption, use request_service_quota_increase
        # which auto-creates a properly categorized support case. Falls back to Step 8+9 on failure.
        has_deterministic_quota_alarm = any(
            alarm in reason_text for alarm in deterministic_quota_alarms
        )
        
        if has_deterministic_quota_alarm:
            result = request_quota_increase_via_service_quotas(
                alarm_ctx, quota_requests, usage_metrics, scenario,
                thresholds, case_type_suffix, triggered_eligible
            )
            if result:
                return result
            logger.info("Service Quotas API path failed — falling back to create_case()")
        
        # --- Step 8: Duplicate detection — append to existing case if found ---
        try:
            lookback_days = int(os.environ.get('SUPPORT_CASE_LOOKBACK_DAYS', '60'))
            after_time = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime('%Y-%m-%dT%H:%M:%S')
            cases_response = support_client.describe_cases(includeResolvedCases=False, afterTime=after_time)
            
            for case in cases_response.get('cases', []):
                subj = case.get('subject', '').lower()
                if case_subject.lower() in subj:
                    existing_case_id = case.get('caseId')
                    existing_display_id = case.get('displayId', existing_case_id)
                    existing_time = case.get('timeCreated', '')
                    logger.info(f"Existing unresolved {case_type_suffix} case found: {existing_display_id}, raised {existing_time}")
                    
                    # Dedup: Check if this specific alarm type was already appended to this case
                    if _check_alarm_dedup(existing_case_id, triggered_eligible):
                        return {
                            'case_id': existing_case_id, 'display_id': existing_display_id,
                            'subject': case.get('subject', case_subject), 'case_type_suffix': case_type_suffix,
                            'scenario': scenario, 'is_suppressed': True,
                            'time_created': existing_time, 'quotas': quota_requests
                        }
                    
                    # Not previously appended — proceed with append
                    logger.info(f"Appending update to case {existing_display_id}")
                    append_body = build_msg_body(alarm_ctx, quota_requests, usage_metrics, scenario, thresholds, is_append=True)
                    _append_to_case(existing_case_id, append_body)
                    
                    return {
                        'case_id': existing_case_id, 'display_id': existing_display_id,
                        'subject': case.get('subject', case_subject), 'case_type_suffix': case_type_suffix,
                        'scenario': scenario, 'is_append': True,
                        'time_created': existing_time, 'quotas': quota_requests
                    }
            logger.info(f"No unresolved {case_type_suffix} cases found for {model_name}, proceeding")
        except Exception as e:
            logger.error(f"Duplicate case check failed: {str(e)}")

        # --- Step 9: Create support case and retrieve displayId ---
        case_body = build_msg_body(alarm_ctx, quota_requests, usage_metrics, scenario, thresholds)
        
        try:
            case_id = support_client.create_case(
                subject=case_subject,
                serviceCode='service-bedrock',
                severityCode='normal',
                categoryCode='general-guidance',
                communicationBody=case_body,
                language='en'
            ).get('caseId')
            logger.info(f"Created case: {case_id}")
            
            # Retrieve displayId — caseId and displayId are different per AWS docs
            details = _resolve_case_details(case_id)
            display_id = details['display_id']
            
            return {
                'case_id': case_id, 'display_id': display_id, 'subject': case_subject,
                'case_type_suffix': case_type_suffix, 'scenario': scenario,
                'is_append': False, 'quotas': quota_requests
            }
        except Exception as e:
            logger.error(f"Case creation failed: {str(e)}")
            return None
        
    except Exception as e:
        logger.error(f"Error in create_quota_support_case: {str(e)}")
        return None

def build_alarm_context(message):
    """
    Build shared context dict from composite alarm SNS message.
    Determines severity (CRITICAL/WARNING), generates granular exec_summary
    per triggered child alarm, and fetches use case description from Parameter Store.
    Used by both email notification and support case workflows.
    """
    alarm_name = message.get('AlarmName', '')
    reason = message.get('NewStateReason', '')
    model_id = os.environ.get('BEDROCK_MODEL_ID', 'Not specified')
    model_name = os.environ.get('BEDROCK_MODEL_NAME', 'Not specified')
    input_modalities = os.environ.get('INPUT_MODALITIES', 'Not specified')
    geo_data_residency = os.environ.get('GEO_DATA_RESIDENCY_REQUIREMENT', 'NA')
    customer_name = get_secret(os.environ.get('CUSTOMER_NAME_SECRET', ''))
    
    # Determine severity based on triggering child alarms
    # Get triggering children from composite alarm message
    triggering_children = message.get('TriggeringChildren', [])
    
    # Check if any Layer 1 critical alarms are triggered
    is_critical = False
    triggered_critical = []
    triggered_warning = []
    for child in triggering_children:
        child_arn = child.get('Arn', '')
        if 'ClientErrors-Critical' in child_arn:
            is_critical = True
            triggered_critical.append('ClientErrors')
        elif 'ServerErrors-Critical' in child_arn:
            is_critical = True
            triggered_critical.append('ServerErrors')
        elif 'Throttles-Critical' in child_arn:
            is_critical = True
            triggered_critical.append('Throttles')
        elif 'HighInvocationRate-Warning' in child_arn:
            triggered_warning.append('HighInvocationRate')
        elif 'HighTPMQuotaUsage-Warning' in child_arn:
            triggered_warning.append('HighTPMQuotaUsage')
        elif 'HighLatency-Warning' in child_arn:
            triggered_warning.append('HighLatency')
        elif 'InvocationAnomaly-Warning' in child_arn:
            triggered_warning.append('InvocationAnomaly')
        elif 'InputTokenAnomaly-Warning' in child_arn:
            triggered_warning.append('InputTokenAnomaly')
        elif 'OutputTokenAnomaly-Warning' in child_arn:
            triggered_warning.append('OutputTokenAnomaly')
        elif 'LatencyAnomaly-Warning' in child_arn:
            triggered_warning.append('LatencyAnomaly')
    
    # Set severity and impact based on triggered alarms
    if is_critical:
        severity = "CRITICAL"
        impact_level = "HIGH"
        # Build granular executive summary based on which critical alarm(s) fired
        critical_descriptions = {
            'ClientErrors': 'client errors (HTTP 4xx), indicating requests are being rejected — likely due to invalid request parameters or quota limits being exceeded or other unknown client side error',
            'ServerErrors': 'server errors (HTTP 5xx), indicating an server error that may or may not be quota-related',
            'Throttles': 'throttling, indicating RPM/TPM quota limits have been reached'
        }
        issues = [critical_descriptions[a] for a in triggered_critical]
        exec_summary = f"Our Bedrock AI model ({model_name}) is experiencing {'; and '.join(issues)}. Immediate intervention is required to restore normal operations and ensure service continuity. Please investigate the triggering alarm ({', '.join(triggered_critical)}) to determine whether this is a quota-related issue (RPM/TPM limits) or another underlying issue. If it is quota-related issue, increase the limit(s) as requested below."
    else:
        severity = "WARNING"
        impact_level = "MEDIUM"
        # Build granular executive summary based on which warning/anomaly alarm(s) fired
        warning_descriptions = {
            'HighInvocationRate': 'request rate (RPM) is approaching quota limits',
            'HighTPMQuotaUsage': 'estimated TPM quota consumption is approaching limits',
            'HighLatency': 'latency is elevated, which may indicate throttling or quota limit',
            'InvocationAnomaly': 'unusual request pattern detected by anomaly detection',
            'InputTokenAnomaly': 'unusual input token usage pattern detected by anomaly detection',
            'OutputTokenAnomaly': 'unusual output token usage pattern detected by anomaly detection',
            'LatencyAnomaly': 'unusual latency pattern detected by anomaly detection'
        }
        if triggered_warning:
            issues = [warning_descriptions[a] for a in triggered_warning]
            exec_summary = f"Our Bedrock AI model ({model_name}) — {'; and '.join(issues)}. Please investigate the triggering alarm ({', '.join(triggered_warning)}) to determine root cause. If this is quota-related, increase the quota limit as requested below."
        else:
            exec_summary = f"Our Bedrock AI model ({model_name}) is approaching quota limits or showing unusual usage patterns. Please investigate the triggering alarm ({alarm_name}) to determine root cause. If this is quota-related, increase the quota limit as requested below."
            logger.info(f"********** GENERIC Executive Summary Fetched at function: build_alarm_context")

    impact_assessment = f"Severity Level: {severity}\nService Impact: {impact_level} - AI features may be degraded or unavailable\nAffected Services: Application functionality dependent on {model_name}"
    
    # Fetch use case description from Parameter Store
    use_case_description = ''
    try:
        use_case_param = os.environ.get('USE_CASE_DESCRIPTION_PARAM', '')
        if use_case_param:
            use_case_description = ssm.get_parameter(Name=use_case_param)['Parameter']['Value']
    except Exception as e:
        logger.error(f"Failed to get use case description: {str(e)}")

    return {
        'alarm_name': alarm_name,
        'model_id': model_id,
        'model_name': model_name,
        'customer_name': customer_name,
        'input_modalities': input_modalities,
        'geo_data_residency': geo_data_residency,
        'severity': severity,
        'impact_level': impact_level,
        'exec_summary': exec_summary,
        'impact_assessment': impact_assessment,
        'use_case_description': use_case_description,
        # Store triggered alarm names for scenario-aware exec_summary
        'triggered_alarms_str': ', '.join(triggered_critical) if triggered_critical else ', '.join(triggered_warning) if triggered_warning else alarm_name,
        'new_state': message.get('NewStateValue', ''),
        'reason': reason,
        'timestamp': message.get('StateChangeTime', 'Unknown')
    }


def build_msg_body(alarm_ctx, quota_requests=None, usage_metrics=None, scenario=None, thresholds=None, case_result=None, audience='support_engineer', is_append=False, is_suppressed=False):
    """
    Build the core message body used by both support case and email notification.
    
    Smart Quota Guard scenario → content mapping:
      high_usage:  assertive quota increase request, "Do not wait"
      low_usage:   investigate-first, quota details as reference
      new_model:   quota increase with limited-history note
      non_quota:   service-side investigation, no quota sections
      None:        default exec_summary (fallback)
    
    audience: 'support_engineer' (support case) or 'ops_team' (email).
    Controls ACTION REQUESTED and EXECUTIVE SUMMARY tone.
    
    is_append: True when building body for appending to an existing case.
    Prefixes EXECUTIVE SUMMARY and ACTION REQUESTED with urgency context.
    
    is_suppressed: True when append was suppressed (same alarm already communicated).
    Used for ops_team email to reflect that the case was not updated.
    """
    is_ops = audience == 'ops_team'
    
    # Header differs for appended communications vs new cases
    if is_append:
        body = f"Amazon {PRODUCT_NAME} - Automated Update\n\n"
    else:
        body = f"Amazon {PRODUCT_NAME} - Automated Request\n\n"
    body += f"CUSTOMER: {alarm_ctx['customer_name']}\n"
    body += f"MODEL: {alarm_ctx['model_name']}\n"
    body += f"MODEL ID: {alarm_ctx['model_id']}\n"
    body += f"COMPOSITE ALARM: {alarm_ctx['alarm_name']}\n"
    body += f"CHILD ALARM TRIGGERED: {alarm_ctx['reason']}\n"
    body += f"TIMESTAMP: {alarm_ctx['timestamp']}\n"
    body += f"REGION: {os.environ.get('AWS_REGION', 'us-east-1')}\n\n"
    
    # Prefix for appended communications — signals escalation urgency
    append_prefix = "An additional issue has been detected while the previously reported alarm in this case remains unresolved. Multiple alarms indicate our system is actively experiencing issues with business impact — please expedite resolution without waiting for confirmation. " if is_append and not is_ops else ""
    
    # Scenario-aware EXECUTIVE SUMMARY
    if scenario == 'non_quota':
        base_issue = alarm_ctx['exec_summary'].split('. Please investigate')[0] if '. Please investigate' in alarm_ctx['exec_summary'] else alarm_ctx['exec_summary'].split('. Immediate')[0]
        if is_ops:
            if is_append:
                body += f"EXECUTIVE SUMMARY:\nAn additional issue has been detected while the previously reported alarm in this case remains unresolved. {base_issue}. The existing support case has been updated instructing the support engineer to investigate this new alarm together with the previously reported alarm, expedite permanent fix at service level and provide alternate solution until permanent fix. Please monitor the support case and engage with the support engineer for remediation.\n\n"
            else:
                body += f"EXECUTIVE SUMMARY:\n{base_issue}. A support case has been raised instructing the support engineer to investigate root cause, expedite permanent fix at service level and provide alternate solution until permanent fix. Please monitor the support case and engage with the support engineer for remediation.\n\n"
        else:
            body += f"EXECUTIVE SUMMARY:\n{append_prefix}{base_issue}. Please investigate the triggering alarm to determine root cause, expedite permanent fix at service level and provide alternate solution until permanent fix.\n\n"
    elif scenario == 'low_usage':
        # Low usage: investigate-first tone, quota details as reference
        base_issue = alarm_ctx['exec_summary'].split('. Please investigate')[0] if '. Please investigate' in alarm_ctx['exec_summary'] else alarm_ctx['exec_summary'].split('. Immediate')[0]
        triggered_alarms = alarm_ctx.get('triggered_alarms_str', '')
        if is_ops:
            if is_append:
                body += f"EXECUTIVE SUMMARY:\nAn additional issue has been detected while the previously reported alarm in this case remains unresolved. {base_issue} but usage metrics do not indicate sustained quota consumption. The existing support case has been updated instructing the support engineer to investigate the triggering alarm ({triggered_alarms}) together with the previously reported alarm and determine root cause first. Please monitor the support case and engage with the support engineer for remediation.\n\n"
            else:
                body += f"EXECUTIVE SUMMARY:\n{base_issue} but usage metrics do not indicate sustained quota consumption. A support case has been raised instructing the support engineer to investigate the triggering alarm ({triggered_alarms}) and determine root cause first. Please monitor the support case and engage with the support engineer for remediation.\n\n"
        else:
            body += f"EXECUTIVE SUMMARY:\n{append_prefix}{base_issue} but usage metrics do not indicate sustained quota consumption. Please investigate the triggering alarm ({triggered_alarms}) to determine root cause first. If investigation confirms this is quota-related, quota increase details are provided below for reference which you can use to increase quota without waiting for additional confirmation.\n\n"
    elif scenario == 'new_model':
        triggered_alarms = alarm_ctx.get('triggered_alarms_str', '')
        if is_ops:
            if is_append:
                body += f"EXECUTIVE SUMMARY:\nAn additional issue has been detected while the previously reported alarm in this case remains unresolved. Our Bedrock AI model ({alarm_ctx['model_name']}) is newly deployed with limited usage history and is experiencing issues. The existing support case has been updated instructing the support engineer to investigate and proceed with quota increase if needed. Please monitor the support case and engage with the support engineer for remediation.\n\n"
            else:
                body += f"EXECUTIVE SUMMARY:\nOur Bedrock AI model ({alarm_ctx['model_name']}) is newly deployed with limited usage history and is experiencing issues. A support case has been raised instructing the support engineer to investigate and proceed with quota increase if needed. Please monitor the support case and engage with the support engineer for remediation.\n\n"
        else:
            body += f"EXECUTIVE SUMMARY:\n{append_prefix}Our Bedrock AI model ({alarm_ctx['model_name']}) is newly deployed with limited usage history and is experiencing issues. Please investigate the triggering alarm ({triggered_alarms}) to determine root cause. If it is quota-related, increase the limit(s) as requested below.\n\n"
    elif scenario == 'high_usage':
        base_summary = alarm_ctx['exec_summary']
        if is_ops:
            # Ops team: inform about what the support engineer was instructed
            base_issue = base_summary.split('. Please investigate')[0] if '. Please investigate' in base_summary else base_summary.split('. Immediate')[0]
            if is_append:
                body += f"EXECUTIVE SUMMARY:\nAn additional issue has been detected while the previously reported alarm in this case remains unresolved. {base_issue}. The existing support case has been updated instructing the support engineer to proceed with quota increase without waiting for additional confirmation. Please monitor the support case and engage with the support engineer for remediation.\n\n"
            else:
                body += f"EXECUTIVE SUMMARY:\n{base_issue}. A support case has been raised instructing the support engineer to proceed with quota increase without waiting for additional confirmation. Please monitor the support case and engage with the support engineer for remediation.\n\n"
        else:
            # Support engineer: direct instruction with assertive tone
            if 'increase the limit(s) as requested below.' in base_summary:
                base_summary = base_summary.replace('increase the limit(s) as requested below.', 'increase the limit(s) as requested below. Do not wait for our confirmation. Immediate action is requested.')
            elif 'increase the quota limit as requested below.' in base_summary:
                base_summary = base_summary.replace('increase the quota limit as requested below.', 'increase the limit(s) as requested below. Do not wait for our confirmation. Immediate action is requested.')
            body += f"EXECUTIVE SUMMARY:\n{append_prefix}{base_summary}\n\n"
    else:
        # Fallback: scenario not determined (should not happen after refactor)
        logger.warning("build_msg_body called without scenario — using default exec_summary")
        body += f"EXECUTIVE SUMMARY:\n{append_prefix}{alarm_ctx['exec_summary']}\n\n"
        logger.info(f"********** GENERIC Executive Summary Fetched at function: build_msg_body")
    
    if alarm_ctx.get('use_case_description'):
        body += f"USE CASE:\n{alarm_ctx['use_case_description']}\n\n"
    body += f"IMPACT ASSESSMENT:\n{alarm_ctx['impact_assessment']}\n\n"
    if usage_metrics:
        body += f"USAGE METRICS (Last {usage_metrics['lookback_days']} days):\n"
        body += f"  Steady State TPM: {usage_metrics['steady_tpm']}\n"
        body += f"  Peak TPM: {usage_metrics['peak_tpm']}\n"
        body += f"  Steady State RPM: {usage_metrics['steady_rpm']}\n"
        body += f"  Peak RPM: {usage_metrics['peak_rpm']}\n"
        body += f"  Avg Input Tokens/Request: {usage_metrics['avg_input_tokens_per_request']}\n"
        body += f"  Avg Output Tokens/Request: {usage_metrics['avg_output_tokens_per_request']}\n"
        body += f"  Input Modalities: {alarm_ctx.get('input_modalities', 'Not specified')}\n"
        if not alarm_ctx.get('model_id', '').startswith('global.') and alarm_ctx.get('geo_data_residency', '').lower() == 'yes':
            body += f"  Cross Region Inference: Due to Geographic data residency requirement, Global Cross Region Inference can't be considered.\n"
        body += "\n"
    
    # Scenario-aware QUOTA INCREASE REQUESTS, JUSTIFICATION, and ACTION REQUESTED
    # Append prefix for ACTION REQUESTED — directs engineer to process together with prior alarms
    if is_append and not is_ops:
        if scenario == 'non_quota':
            action_prefix = "Investigate the previously reported alarm in this case together with this new alarm. "
        else:
            action_prefix = "Process the previously reported quota increase request in this case together with this new quota increase request. "
    else:
        action_prefix = ""
    
    # Suppressed append: ops team gets notification that case was not updated
    if is_suppressed and is_ops:
        body += "ACTION REQUESTED:\nThis alarm type has already been communicated to the support engineer on this case. No additional update was sent. Please monitor the support case and engage with the support engineer if escalation is needed.\n\n"
    elif scenario == 'non_quota':
        if is_ops:
            if is_append:
                body += "ACTION REQUESTED:\nThe existing support case has been updated instructing the support engineer to investigate this new alarm together with the previously reported alarm, expedite permanent fix and provide alternate solution until permanent fix. Please monitor the support case and engage with the support engineer for remediation.\n\n"
            else:
                body += "ACTION REQUESTED:\nAn automated support case has been raised instructing the support engineer to investigate the issue from the service side, expedite permanent fix and provide alternate solution until permanent fix. Please monitor the support case and engage with the support engineer for remediation.\n\n"
        else:
            body += f"ACTION REQUESTED:\n{action_prefix}This is an automated alert generated by our monitoring system. Please investigate the issue from the service side, expedite permanent fix at service level and provide alternate solution until permanent fix.\n\n"
    elif scenario == 'low_usage' and quota_requests:
        # Low usage: include quota details as reference with investigate-first framing
        body += "QUOTA INCREASE REQUESTS (for reference — pending support engineer investigation):\n"
        for q in quota_requests:
            body += f"\n{q['name']}:\n  Current: {q['current']}\n  Requested: {q['new']}\n  Increase: {q['percent']}%\n  Code: {q['code']}\n"
        rpm_thresh = thresholds.get('rpm_threshold', 'N/A') if thresholds else 'N/A'
        tpm_thresh = thresholds.get('tpm_threshold', 'N/A') if thresholds else 'N/A'
        peak_rpm = usage_metrics.get('peak_rpm', 'N/A') if usage_metrics else 'N/A'
        peak_tpm = usage_metrics.get('peak_tpm', 'N/A') if usage_metrics else 'N/A'
        thresh_str = f"RPM threshold: {rpm_thresh}, TPM threshold: {tpm_thresh}" if not (isinstance(rpm_thresh, (int, float)) and rpm_thresh >= RPM_DISABLED_SENTINEL) else f"TPM threshold: {tpm_thresh}"
        body += f"\nJUSTIFICATION:\nAutomated request triggered by CloudWatch alarms indicating quota limits about to be exceeded or already exceeded. Quota increase needed to maintain service availability and prevent application disruptions. However, usage metrics over the past 14 days (Peak RPM: {peak_rpm}, Peak TPM: {peak_tpm}) are below the alarm thresholds ({thresh_str}), suggesting this may be a transient event rather than sustained quota consumption.\n\n"
        if is_ops:
            if is_append:
                body += "ACTION REQUESTED:\nThe existing support case has been updated instructing the support engineer to investigate the alarm root cause first. If confirmed quota-related, the support engineer has been provided quota increase details for reference. Please monitor the support case and engage with the support engineer for remediation.\n\n"
            else:
                body += "ACTION REQUESTED:\nAn automated support case has been raised instructing the support engineer to investigate the alarm root cause first. If confirmed quota-related, the support engineer has been provided quota increase details for reference. Please monitor the support case and engage with the support engineer for remediation.\n\n"
        else:
            body += f"ACTION REQUESTED:\n{action_prefix}This is an automated alert generated by our monitoring system. Usage metrics do not indicate sustained quota consumption at this time. Please investigate the alarm root cause first. If your investigation confirms this is a quota-related issue, the quota increase details above are provided for your reference. All required details — current usage, quota values, and requested increases — included above are accurate and verified. Please proceed with the quota increase as specified without additional confirmation.\n\n"
    elif scenario == 'new_model' and quota_requests:
        # New model: assertive tone, note limited history
        body += "QUOTA INCREASE REQUESTS:\n"
        for q in quota_requests:
            body += f"\n{q['name']}:\n  Current: {q['current']}\n  Requested: {q['new']}\n  Increase: {q['percent']}%\n  Code: {q['code']}\n"
        thresh_str = f"RPM threshold: {thresholds.get('rpm_threshold')}, TPM threshold: {thresholds.get('tpm_threshold')}" if not (isinstance(thresholds.get('rpm_threshold'), (int, float)) and thresholds.get('rpm_threshold') >= RPM_DISABLED_SENTINEL) else f"TPM threshold: {thresholds.get('tpm_threshold')}"
        body += f"\nJUSTIFICATION:\nAutomated request triggered by CloudWatch alarms on a newly deployed model with limited usage history. Quota increase requested to support initial scaling. Alarm thresholds ({thresh_str}).\n\n"
        if is_ops:
            if is_append:
                body += "ACTION REQUESTED:\nThe existing support case has been updated instructing the support engineer to proceed with the quota increase for this newly deployed model. Please monitor the support case and engage with the support engineer for remediation.\n\n"
            else:
                body += "ACTION REQUESTED:\nAn automated support case has been raised instructing the support engineer to proceed with the quota increase for this newly deployed model. Please monitor the support case and engage with the support engineer for remediation.\n\n"
        else:
            body += f"ACTION REQUESTED:\n{action_prefix}This is an automated quota increase request generated by our monitoring system. This model is newly deployed with limited usage history, so usage-based validation could not be performed. We have intentionally configured and enabled this automation to streamline quota management. Please proceed with the quota increase as specified without additional confirmation.\n\n"
    elif quota_requests:
        # High usage (or default with quota_requests): assertive tone
        body += "QUOTA INCREASE REQUESTS:\n"
        for q in quota_requests:
            body += f"\n{q['name']}:\n  Current: {q['current']}\n  Requested: {q['new']}\n  Increase: {q['percent']}%\n  Code: {q['code']}\n"
        thresh_str = f"RPM threshold: {thresholds.get('rpm_threshold')}, TPM threshold: {thresholds.get('tpm_threshold')}" if not (isinstance(thresholds.get('rpm_threshold'), (int, float)) and thresholds.get('rpm_threshold') >= RPM_DISABLED_SENTINEL) else f"TPM threshold: {thresholds.get('tpm_threshold')}"
        body += f"\nJUSTIFICATION:\nAutomated request triggered by CloudWatch alarms indicating quota limits about to be exceeded or already exceeded. Quota increase needed to maintain service availability and prevent application disruptions. Usage metrics over the past 14 days (Peak RPM: {usage_metrics.get('peak_rpm', 'N/A') if usage_metrics else 'N/A'}, Peak TPM: {usage_metrics.get('peak_tpm', 'N/A') if usage_metrics else 'N/A'}) meet or exceed alarm thresholds ({thresh_str}).\n\n"
        if is_ops:
            if is_append:
                body += "ACTION REQUESTED:\nThe existing support case has been updated instructing the support engineer to proceed with the quota increase without waiting for additional confirmation."
            else:
                body += "ACTION REQUESTED:\nAn automated support case has been raised instructing the support engineer to proceed with the quota increase without waiting for additional confirmation."
            if alarm_ctx.get('severity') == 'CRITICAL':
                body += " Service is actively impacted — expedited processing has been requested."
            body += " Please monitor the support case and engage with the support engineer for remediation.\n\n"
        else:
            body += f"ACTION REQUESTED:\n{action_prefix}This is an automated quota increase request generated by our monitoring system. We have intentionally configured and enabled this automation to streamline quota management and reduce resolution time by eliminating manual back-and-forth. All required details — current usage, quota values, and requested increases — included above are accurate and verified. Please proceed with the quota increase as specified without additional confirmation."
            if alarm_ctx.get('severity') == 'CRITICAL':
                body += " Service is actively impacted — expedited processing is appreciated."
            body += "\n\n"
    
    # AUTOMATED SUPPORT CASE section: renders for new, appended, and suppressed cases.
    # Support case body is built BEFORE case creation (case_result=None) — section skipped.
    # Email body is built AFTER (case_result provided) — section included.
    if case_result and case_result.get('case_id'):
        display_id = case_result.get('display_id', case_result['case_id'])
        case_subject = case_result.get('subject', '')
        is_appended = case_result.get('is_append', False)
        case_suppressed = case_result.get('is_suppressed', False)
        time_created = case_result.get('time_created', '')
        
        if case_suppressed:
            body += "EXISTING SUPPORT CASE (not updated — same alarm type already communicated):\n"
            body += f"  Case ID: {display_id}\n"
            body += f"  Subject: {case_subject}\n"
            if time_created:
                body += f"  Originally Raised: {time_created}\n"
            body += f"  Console: https://console.aws.amazon.com/support/home#/case/?displayId={display_id}\n\n"
        elif is_appended:
            body += "EXISTING SUPPORT CASE (updated with new alarm details):\n"
            body += f"  Case ID: {display_id}\n"
            body += f"  Subject: {case_subject}\n"
            if time_created:
                body += f"  Originally Raised: {time_created}\n"
            body += f"  Console: https://console.aws.amazon.com/support/home#/case/?displayId={display_id}\n\n"
        else:
            body += "AUTOMATED SUPPORT CASE:\n"
            body += f"  Case ID: {display_id}\n"
            body += f"  Subject: {case_subject}\n"
            body += f"  Console: https://console.aws.amazon.com/support/home#/case/?displayId={display_id}\n\n"
    
    # Attribution note for support case only (not email) — enables internal adoption tracking
    if not is_ops:
        body += "---\nNote: This case was created by automated solution: https://github.com/aws-samples/sample-amazon-bedrock-ops-alert\n"
    
    return body

def handle_alarm_and_case(event, context):
    """
    Orchestrator — 3-phase flow:
    
    Phase 1: Analysis (always runs)
      build_alarm_context → get_usage_metrics → get_stored_thresholds →
      determine_support_case_scenario → determine_case_type_suffix
      Result: scenario + case_type_suffix always available for email
    
    Phase 2: Support case (try block, may fail/skip)
      create_support_case → returns new case, existing case, or None
    
    Phase 3: Email notification (finally block, always runs)
      send_email_notification → uses scenario + case_result for consistent content
    """
    # Build alarm context once for the matching composite alarm
    alarm_ctx = None
    for record in event['Records']:
        msg = json.loads(record['Sns']['Message'])
        if 'QuotaHealth-Composite' in msg.get('AlarmName', ''):
            alarm_ctx = build_alarm_context(msg)
            break

    # --- Compute scenario and case_type_suffix BEFORE support case workflow ---
    # These are always available for the email regardless of case outcome.
    triggered = alarm_ctx.get('triggered_alarms_str', '') if alarm_ctx else ''
    model_id = alarm_ctx.get('model_id', '') if alarm_ctx else os.environ.get('BEDROCK_MODEL_ID', '')
    customer_name = alarm_ctx.get('customer_name', '') if alarm_ctx else get_secret(os.environ.get('CUSTOMER_NAME_SECRET', ''))
    model_name = os.environ.get('BEDROCK_MODEL_NAME', '')
    
    usage_metrics = get_usage_metrics(model_id) if model_id else None
    thresholds = get_stored_thresholds(customer_name, model_name) if customer_name and model_name else None
    scenario = determine_support_case_scenario(usage_metrics, thresholds, triggered) if triggered else None
    case_type_suffix = determine_case_type_suffix(triggered)
    
    logger.info(f"Smart Quota Guard scenario: {scenario}, case_type: {case_type_suffix} (peak_rpm={usage_metrics.get('peak_rpm') if usage_metrics else 'N/A'}, peak_tpm={usage_metrics.get('peak_tpm') if usage_metrics else 'N/A'}, rpm_threshold={thresholds.get('rpm_threshold') if thresholds else 'N/A'}, tpm_threshold={thresholds.get('tpm_threshold') if thresholds else 'N/A'})")

    # --- Run support case workflow, then email in finally ---
    case_result = None
    try:
        expected_composite_alarm = f"{customer_name}-Bedrock-QuotaHealth-Composite-{model_name}"
        
        for record in event['Records']:
            msg = json.loads(record['Sns']['Message'])
            alarm_name = msg.get('AlarmName', '')
            
            if alarm_name == expected_composite_alarm:
                logger.info(f"Processing composite alarm: {alarm_name}")
                case_result = create_support_case(alarm_ctx, scenario, case_type_suffix, usage_metrics, thresholds)
            elif 'QuotaHealth-Composite' in alarm_name:
                logger.info(f"Ignoring composite alarm from different stack: {alarm_name}")
    except Exception as e:
        logger.error(f"Support case workflow error: {str(e)}")
    finally:
        # Email always sends — scenario is always available from above
        result = send_email_notification(event, context, alarm_ctx, case_result, case_type_suffix, scenario)
    return result

def send_email_notification(event, context, alarm_ctx=None, case_result=None, case_type_suffix=CASE_TYPE_QUOTA_REQUEST, scenario=None):
    """
    Send formatted email notification to stakeholders via SNS.
    Applies notification preference filter (all/critical/warning),
    severity icon, perspective swap (Our→Your), and includes support case details if available.
    """
    for record in event['Records']:
        message = json.loads(record['Sns']['Message'])
        if 'QuotaHealth-Composite' in message.get('AlarmName', ''):
            ctx = alarm_ctx if alarm_ctx else build_alarm_context(message)

            notification_preference = ssm.get_parameter(Name=os.environ['NOTIFICATION_PREFERENCE_PARAM'])['Parameter']['Value']
            # Skip the email notification if based on notification preference user selected
            if notification_preference != 'all':
                if ctx['severity'].lower() != notification_preference:
                    logger.info(f"Skipping notification: severity={ctx['severity']}, preference={notification_preference}")
                    continue  # ✅ Skip this record, continue to next one
            # Alert icon based on severity
            if ctx['severity'].lower()=='critical':
                alert_icon = '🔴'
            else:
                alert_icon = '🟡'

            subject = f"{alert_icon} [{ctx['severity']}] {ctx['customer_name']} - {PRODUCT_NAME} - {ctx['model_name']} - {case_type_suffix}"

            # Reuse build_msg_body for email — same shared content as support case
            # Adjust perspective: support case uses "Our" (1st person), email uses "Your" (2nd person for ops team)
            email_ctx = dict(ctx)
            email_ctx['exec_summary'] = ctx['exec_summary'].replace('Our Bedrock AI model', 'Your Bedrock AI model')
            is_appended = case_result.get('is_append', False) if case_result else False
            is_suppressed = case_result.get('is_suppressed', False) if case_result else False
            core_body = build_msg_body(email_ctx, case_result=case_result, scenario=scenario, audience='ops_team', is_append=is_appended, is_suppressed=is_suppressed)

            enhanced_message = f"SEVERITY: {ctx['severity']} - IMMEDIATE ACTION REQUIRED\n\n"
            enhanced_message += core_body + "\n"
            
            # Scenario-aware recommended actions
            if scenario == 'non_quota':
                enhanced_message += "RECOMMENDED ACTIONS:\n"
                enhanced_message += "1. Monitor the support case and engage with the support engineer\n"
                enhanced_message += "2. Review CloudWatch Alarms and metrics for error patterns\n"
                enhanced_message += "3. Document application impacts for the support case\n\n"
            else:
                enhanced_message += "RECOMMENDED ACTIONS:\n"
                enhanced_message += "1. Monitor the support case and engage with the support engineer\n"
                enhanced_message += "2. Contact AWS TAM/SA for quota increase escalation if needed\n"
                enhanced_message += "3. Review CloudWatch Alarms and metrics for usage patterns\n"
                enhanced_message += "4. Document application impacts\n\n"
            enhanced_message += "RESOURCES:\n"
            enhanced_message += "🔗 AWS Support: https://console.aws.amazon.com/support/home\n"
            enhanced_message += "📊 CloudWatch Alarms: https://console.aws.amazon.com/cloudwatch/home#alarmsV2:\n\n"
            enhanced_message += f"Original Message:\n{json.dumps(message, indent=2)}\n"
            
            # Send formatted notification to email subscribers
            formatted_topic_arn = os.environ.get('FORMATTED_TOPIC_ARN')
            
            try:
                sns_client.publish(
                    TopicArn=formatted_topic_arn,
                    Subject=subject,
                    Message=enhanced_message
                )
                logger.info(f"Formatted notification sent to email subscribers")
            except Exception as e:
                logger.error(f"Failed to send formatted notification: {str(e)}")
    
    return {'statusCode': 200}
