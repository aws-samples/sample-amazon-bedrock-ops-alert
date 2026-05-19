# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Shared utilities for quota calculation and threshold management.
This module is used by both QuotaCalculator and AlarmUpdater Lambda functions.
"""
import boto3
from datetime import datetime, timezone

# Sentinel value used when RPM quota code is invalid/NA — effectively disables the RPM alarm
RPM_DISABLED_SENTINEL = 999999999


def calculate_thresholds(rpm_quota_code, tpm_quota_code, rpm_percent, tpm_percent):
    """
    Calculate alarm thresholds based on Service Quotas.
    
    Args:
        rpm_quota_code: Service Quotas code for requests per minute
        tpm_quota_code: Service Quotas code for tokens per minute
        rpm_percent: Percentage of RPM quota to use as threshold
        tpm_percent: Percentage of TPM quota to use as threshold
    
    Returns:
        Dictionary with rpm_threshold, tpm_threshold
    """
    quotas = boto3.client('service-quotas')
    
    # Get RPM quota — if code is invalid/NA, disable RPM alarm with unreachable threshold
    try:
        rpm_quota_response = quotas.get_service_quota(
            ServiceCode='bedrock',
            QuotaCode=rpm_quota_code
        )
        rpm_quota_value = rpm_quota_response['Quota']['Value']
        rpm_threshold = int(rpm_quota_value * (rpm_percent / 100))
    except Exception as e:
        print(f'RPM quota code {rpm_quota_code} not found — RPM alarm effectively disabled: {str(e)}')
        rpm_threshold = RPM_DISABLED_SENTINEL
    
    # Get TPM quota
    tpm_quota_response = quotas.get_service_quota(
        ServiceCode='bedrock',
        QuotaCode=tpm_quota_code
    )
    tpm_quota_value = tpm_quota_response['Quota']['Value']
    
    # Apply TPM threshold percentage
    tpm_threshold = int(tpm_quota_value * (tpm_percent / 100))
    
    return {
        'rpm_threshold': rpm_threshold,
        'tpm_threshold': tpm_threshold
    }


def store_in_parameter_store(customer_name, model_name, thresholds):
    """
    Store calculated thresholds in Parameter Store.
    
    Args:
        customer_name: Customer identifier
        model_name: Bedrock model name
        thresholds: Dictionary with threshold values
    """
    ssm = boto3.client('ssm')
    base_path = f'/{customer_name}/bedrock/quota-monitoring/{model_name}/thresholds'
    timestamp = datetime.now(timezone.utc).strftime('%d-%b-%Y %H:%M:%S UTC')
    
    parameters = {
        f'{base_path}/rpm-threshold-calculated': str(thresholds['rpm_threshold']),
        f'{base_path}/tpm-threshold-calculated': str(thresholds['tpm_threshold']),
        f'{base_path}/last-updated': timestamp
    }
    
    for name, value in parameters.items():
        try:
            ssm.put_parameter(
                Name=name,
                Value=value,
                Type='String',
                Overwrite=True,
                Description='Auto-calculated alarm threshold'
            )
            print(f'Stored parameter: {name} = {value}')
        except Exception as e:
            print(f'Failed to store parameter {name}: {str(e)}')


def get_usage_metrics(model_id, lookback_days=14):
    """
    Retrieve usage metrics from CloudWatch for a Bedrock model.
    Uses 1-minute granularity with daily API calls to get accurate per-minute
    peak and steady-state values. CloudWatch caps at 1,440 data points per call,
    so each day (1,440 minutes) requires a separate API call.
    
    Returns:
        Dictionary with steady/peak RPM, TPM, and avg tokens per request.
        Returns None if metrics retrieval fails.
    """
    from datetime import timedelta
    cloudwatch = boto3.client('cloudwatch')
    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(days=lookback_days)

    dimensions = [{'Name': 'ModelId', 'Value': model_id}]
    namespace = 'AWS/Bedrock'

    def _get_daily_sums(metric_name, day_start, day_end):
        """Get per-minute Sum data points for a single day."""
        try:
            resp = cloudwatch.get_metric_statistics(
                Namespace=namespace,
                MetricName=metric_name,
                Dimensions=dimensions,
                StartTime=day_start,
                EndTime=day_end,
                Period=60,
                Statistics=['Sum']
            )
            return [dp['Sum'] for dp in resp.get('Datapoints', [])]
        except Exception as e:
            print(f'Failed to get {metric_name} for {day_start}: {str(e)}')
            return []

    def _get_all_minute_sums(metric_name):
        """Collect per-minute Sum values across all days in the lookback window."""
        all_values = []
        for day_offset in range(lookback_days):
            day_start = start_time + timedelta(days=day_offset)
            day_end = day_start + timedelta(days=1)
            if day_end > end_time:
                day_end = end_time
            all_values.extend(_get_daily_sums(metric_name, day_start, day_end))
        return all_values

    try:
        inv_values = _get_all_minute_sums('Invocations') or [0]
        input_values = _get_all_minute_sums('InputTokenCount') or [0]
        output_values = _get_all_minute_sums('OutputTokenCount') or [0]

        # Peak = max of per-minute sums (actual peak RPM/TPM)
        peak_rpm = max(inv_values)
        peak_input_tpm = max(input_values)
        peak_output_tpm = max(output_values)

        # Steady state = average of per-minute sums (actual avg RPM/TPM)
        steady_rpm = sum(inv_values) / len(inv_values)
        steady_input_tpm = sum(input_values) / len(input_values)
        steady_output_tpm = sum(output_values) / len(output_values)

        # Totals for per-request averages
        total_invocations = sum(inv_values)
        total_input_tokens = sum(input_values)
        total_output_tokens = sum(output_values)

        avg_input_per_req = round(total_input_tokens / total_invocations, 2) if total_invocations else 0
        avg_output_per_req = round(total_output_tokens / total_invocations, 2) if total_invocations else 0

        return {
            'lookback_days': lookback_days,
            'steady_rpm': round(steady_rpm, 2),
            'steady_tpm': round(steady_input_tpm + steady_output_tpm, 2),
            'peak_rpm': round(peak_rpm, 2),
            'peak_tpm': round(peak_input_tpm + peak_output_tpm, 2),
            'avg_input_tokens_per_request': avg_input_per_req,
            'avg_output_tokens_per_request': avg_output_per_req
        }
    except Exception as e:
        print(f'Failed to get usage metrics: {str(e)}')
        return None


def get_stored_thresholds(customer_name, model_name):
    """
    Smart Quota Guard: Read already-calculated thresholds from Parameter Store.
    These thresholds are computed by QuotaCalculator during deployment and updated by
    AlarmUpdater on schedule. Reusing them avoids new CFN parameters.
    
    Returns:
        Dictionary with rpm_threshold, tpm_threshold.
        Returns None if retrieval fails.
    """
    ssm = boto3.client('ssm')
    base_path = f'/{customer_name}/bedrock/quota-monitoring/{model_name}/thresholds'
    
    try:
        rpm_thresh = float(ssm.get_parameter(Name=f'{base_path}/rpm-threshold-calculated')['Parameter']['Value'])
        tpm_thresh = float(ssm.get_parameter(Name=f'{base_path}/tpm-threshold-calculated')['Parameter']['Value'])
        
        return {
            'rpm_threshold': rpm_thresh,
            'tpm_threshold': tpm_thresh
        }
    except Exception as e:
        print(f'Failed to get stored thresholds: {str(e)}')
        return None


# Smart Quota Guard: Single source of truth for non-quota alarm patterns.
# These alarms should never trigger quota increase requests.
# ServerErrors (5xx) = AWS infrastructure, HighLatency/LatencyAnomaly = not quota metrics.
NON_QUOTA_ALARM_PATTERNS = ['ServerErrors-Critical', 'HighLatency-Warning', 'LatencyAnomaly-Warning']

# Case type suffixes — used in support case subject, email subject, and duplicate detection
CASE_TYPE_QUOTA_REQUEST = 'Quota Request'
CASE_TYPE_INVESTIGATION_REQUEST = 'Investigation Request'


def is_non_quota_alarm(triggered_alarms_str):
    """
    Check if ALL triggered alarms are non-quota (ServerErrors, HighLatency, LatencyAnomaly).
    Returns True only if every triggered alarm matches a non-quota pattern.
    
    Args:
        triggered_alarms_str: Comma-separated string of triggered alarm names
    
    Returns:
        Boolean
    """
    if not triggered_alarms_str:
        return False
    alarms = [a.strip() for a in triggered_alarms_str.split(',')]
    return all(any(alarm in p for p in NON_QUOTA_ALARM_PATTERNS) for alarm in alarms)


def determine_case_type_suffix(triggered_alarms_str):
    """
    Determine support case category suffix based on triggered alarms.
    Single source of truth — used by both support case subject and email subject.
    
    Returns:
        'Investigation Request' for non-quota alarms, 'Quota Request' otherwise.
    """
    return CASE_TYPE_INVESTIGATION_REQUEST if is_non_quota_alarm(triggered_alarms_str) else CASE_TYPE_QUOTA_REQUEST


def determine_support_case_scenario(usage_metrics, thresholds, triggered_alarms):
    """
    Smart Quota Guard: Determine support case scenario.
    Compares 14-day peak usage against already-calculated thresholds to route to the correct
    content strategy. Scenarios:
        - 'non_quota': Non-quota alarms (ServerErrors, HighLatency, LatencyAnomaly) — no quota increase
        - 'new_model': peak_rpm=0 AND peak_tpm=0 — newly deployed model, bypass usage guard
        - 'high_usage': peak >= threshold — sustained quota consumption, assertive tone
        - 'low_usage': peak < threshold — transient event, investigate-first tone
    
    Args:
        usage_metrics: Dictionary from get_usage_metrics() with peak_rpm, peak_tpm
        thresholds: Dictionary from get_stored_thresholds() with rpm_threshold, tpm_threshold
        triggered_alarms: Comma-separated string of triggered alarm names
    
    Returns:
        String: 'high_usage', 'low_usage', 'new_model', or 'non_quota'
    """
    # Non-quota alarms always route to non_quota scenario regardless of usage
    if is_non_quota_alarm(triggered_alarms):
        return 'non_quota'
    
    # If no usage metrics available, treat as new model (safe default)
    if not usage_metrics or not thresholds:
        return 'new_model'
    
    peak_rpm = usage_metrics.get('peak_rpm', 0)
    peak_tpm = usage_metrics.get('peak_tpm', 0)
    
    # New model bypass: no usage history at all
    if peak_rpm == 0 and peak_tpm == 0:
        return 'new_model'
    
    rpm_threshold = thresholds.get('rpm_threshold', 0)
    tpm_threshold = thresholds.get('tpm_threshold', 0)
    
    # High usage: peak meets or exceeds either threshold
    if peak_rpm >= rpm_threshold or peak_tpm >= tpm_threshold:
        return 'high_usage'
    
    # Low usage: peak below both thresholds — likely transient event
    return 'low_usage'
