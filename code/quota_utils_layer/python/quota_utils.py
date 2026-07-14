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


def discover_sibling_profiles(profile_id):
    """
    Discover all application inference profiles sharing the same underlying model.
    Shared utility used by AlarmUpdater and NotificationProcessor.
    
    Flow:
      1. GetInferenceProfile(profile_id) → this profile's full models[] array
      2. ListInferenceProfiles(typeEquals=APPLICATION) → all app profiles
      3. Filter: profiles whose FULL sorted models[] array matches → sibling IDs
    
    Matching uses the FULL sorted models[] array (not models[0] alone) so that
    profiles drawing from different quota pools are never grouped. Example collision
    avoided: an on-demand single-region profile (models=[us-east-1]) and a us
    cross-region profile (models=[us-east-1, us-east-2, us-west-2]) share models[0]
    but are DIFFERENT quotas — they must NOT be treated as siblings. This mirrors the
    matching logic in resolve_application_profile() for consistency.
    
    Args:
        profile_id: Short profile ID (e.g., 'e5t98lwp1dsr')
    
    Returns:
        List of short profile IDs (including the input profile_id itself).
        Capped at 10 (CloudWatch metric math limit).
        Returns empty list on failure.
    """
    try:
        bedrock = boto3.client('bedrock')
        
        # Step 1: Get this profile's full models array (the quota-pool fingerprint)
        this_profile = bedrock.get_inference_profile(inferenceProfileIdentifier=profile_id)
        underlying_model_arns = sorted([m['modelArn'] for m in this_profile.get('models', [])])
        
        # Step 2-3: List all APPLICATION profiles and filter by FULL models[] array match
        sibling_ids = []
        paginator = bedrock.get_paginator('list_inference_profiles')
        for page in paginator.paginate(typeEquals='APPLICATION'):
            for profile in page.get('inferenceProfileSummaries', []):
                if profile.get('status') != 'ACTIVE' or not profile.get('models'):
                    continue
                profile_model_arns = sorted([m['modelArn'] for m in profile['models']])
                if profile_model_arns == underlying_model_arns:
                    sibling_ids.append(profile['inferenceProfileId'])
        
        # Cap at 10 (CloudWatch put_metric_alarm MetricStat limit)
        if len(sibling_ids) > 10:
            print(f'WARNING: Found {len(sibling_ids)} sibling profiles, capping at 10 (CloudWatch limit)')
            sibling_ids = sibling_ids[:10]
        
        print(f'Discovered {len(sibling_ids)} sibling profiles for {profile_id}: {sibling_ids}')
        return sibling_ids
    except Exception as e:
        print(f'Failed to discover sibling profiles for {profile_id}: {str(e)}')
        return []


def get_combined_usage_metrics(sibling_ids, lookback_days=14):
    """
    Retrieve COMBINED usage metrics across multiple inference profiles using
    CloudWatch get_metric_data with metric math expressions.
    
    Uses m1+m2+... expression to sum per-minute values across profiles at each
    timestamp, then computes peaks/steadies from the combined time series.
    
    Cannot reuse get_usage_metrics() per-profile because:
    - Individual profile peaks may occur at different minutes
    - Summing individual peaks ≠ combined per-minute peak
    - avg_input_tokens_per_request needs total tokens / total invocations
    
    Args:
        sibling_ids: List of short profile IDs sharing same underlying model
        lookback_days: Number of days to look back (default 14)
    
    Returns:
        Same dict shape as get_usage_metrics():
        {
            'lookback_days': 14,
            'steady_rpm': ...,
            'steady_tpm': ...,
            'peak_rpm': ...,
            'peak_tpm': ...,
            'avg_input_tokens_per_request': ...,
            'avg_output_tokens_per_request': ...
        }
        Returns None if metrics retrieval fails.
    """
    from datetime import timedelta
    
    if not sibling_ids:
        return None
    
    try:
        cloudwatch = boto3.client('cloudwatch')
        end_time = datetime.now(timezone.utc)
        start_time = end_time - timedelta(days=lookback_days)
        
        def _build_metric_queries(metric_name, prefix):
            """Build MetricDataQuery list for one metric across all siblings."""
            queries = []
            ids = []
            for i, pid in enumerate(sibling_ids):
                mid = f'{prefix}{i + 1}'
                ids.append(mid)
                queries.append({
                    'Id': mid,
                    'MetricStat': {
                        'Metric': {
                            'Namespace': 'AWS/Bedrock',
                            'MetricName': metric_name,
                            'Dimensions': [{'Name': 'ModelId', 'Value': pid}]
                        },
                        'Period': 60,
                        'Stat': 'Sum'
                    },
                    'ReturnData': False
                })
            # Sum expression
            expr_id = f'{prefix}_total'
            queries.append({
                'Id': expr_id,
                'Expression': '+'.join(ids),
                'Label': f'Combined{metric_name}',
                'ReturnData': True
            })
            return queries, expr_id
        
        # Build queries for all needed metrics
        inv_queries, inv_id = _build_metric_queries('Invocations', 'inv')
        input_queries, input_id = _build_metric_queries('InputTokenCount', 'inp')
        output_queries, output_id = _build_metric_queries('OutputTokenCount', 'out')
        
        all_queries = inv_queries + input_queries + output_queries
        
        # get_metric_data supports up to 500 queries — well within limits for ≤10 profiles × 3 metrics
        response = cloudwatch.get_metric_data(
            MetricDataQueries=all_queries,
            StartTime=start_time,
            EndTime=end_time
        )
        
        # Extract combined time series values
        results = {r['Id']: r['Values'] for r in response.get('MetricDataResults', [])}
        
        inv_values = results.get(inv_id, [0])
        input_values = results.get(input_id, [0])
        output_values = results.get(output_id, [0])
        
        if not inv_values:
            inv_values = [0]
        if not input_values:
            input_values = [0]
        if not output_values:
            output_values = [0]
        
        # Compute peaks and steadies from combined time series
        peak_rpm = max(inv_values)
        peak_input_tpm = max(input_values)
        peak_output_tpm = max(output_values)
        
        steady_rpm = sum(inv_values) / len(inv_values)
        steady_input_tpm = sum(input_values) / len(input_values)
        steady_output_tpm = sum(output_values) / len(output_values)
        
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
        print(f'Failed to get combined usage metrics: {str(e)}')
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


# --- Quota Code Validation ---
# Validates that user-provided quota codes match the BedrockModelId before deployment proceeds.

def _determine_expected_mode(model_id):
    """
    Determine expected inference mode from BedrockModelId prefix.
    
    Logic:
        - 'global.' prefix → global cross-region
        - 2-3 letter alphabetic prefix (us., eu., ap., me., af., sa., ca., etc.) → geographic cross-region
        - Anything else (provider name like anthropic., meta., amazon.) → on-demand
    
    Future-proof: no hardcoded geographic prefix list. Uses length heuristic:
    geographic codes are 2-3 chars, provider names are 4+ chars.
    
    Returns:
        'global' | 'cross-region' | 'on-demand'
    """
    if model_id.startswith('global.'):
        return 'global'
    first_segment = model_id.split('.')[0]
    if len(first_segment) <= 3 and first_segment.isalpha():
        return 'cross-region'
    return 'on-demand'


def _infer_mode_from_quota_name(quota_name):
    """
    Determine inference mode from the QuotaName string returned by Service Quotas.
    
    Patterns (confirmed from live API):
        - "Global cross-region model inference ... for {model}" → global
        - "Cross-region model inference ... for {model}" → cross-region (geographic)
        - "On-demand model inference ... for {model}" → on-demand
        - "Bedrock Mantle ... for {model}" → mantle (not supported by this solution)
    """
    lower = quota_name.lower()
    if 'bedrock-mantle' in lower:
        return 'mantle'
    elif lower.startswith('global cross-region') or lower.startswith('global '):
        return 'global'
    elif 'cross-region' in lower:
        return 'cross-region'
    elif 'on-demand' in lower:
        return 'on-demand'
    else:
        return 'unknown'


def _extract_model_name_from_quota(quota_name):
    """
    Extract model display name from QuotaName (text after " for ").
    
    Example:
        "Cross-region model inference tokens per minute for Anthropic Claude Opus 4.6 V1"
        → "Anthropic Claude Opus 4.6 V1"
    """
    if ' for ' in quota_name:
        return quota_name.split(' for ', 1)[1].strip()
    return ''


def _normalize_model_id_to_tokens(model_id):
    """
    Extract meaningful tokens from BedrockModelId for matching against QuotaName.
    Uses regex for clean prefix/suffix stripping.
    
    Steps:
        1. Strip geographic/global prefix (global., us., eu., or any 2-3 char geo code)
        2. Strip provider prefix (first segment after geo)
        3. Remove trailing version patterns (-v1, :0, -v1:0)
        4. Remove date patterns (YYYYMMDD)
        5. Split on hyphens, merge adjacent digits into version numbers
    
    Example:
        "global.anthropic.claude-opus-4-6-v1" → ['claude', 'opus', '4.6']
        "us.anthropic.claude-sonnet-4-5-20250929-v1:0" → ['claude', 'sonnet', '4.5']
        "us.anthropic.claude-sonnet-5" → ['claude', 'sonnet', '5']
        "anthropic.claude-opus-4-6-v1" → ['claude', 'opus', '4.6']
        "meta.llama3-1-70b-instruct-v1:0" → ['llama', '3.1', '70b', 'instruct']
        "meta.llama4-scout-17b-instruct-v1:0" → ['llama', '4', 'scout', '17b', 'instruct']
    """
    import re
    
    # Strip geographic/global prefix: 'global.' or any 2-3 letter geo code followed by dot
    # Examples: 'global.anthropic.claude-opus-4-6-v1' → 'anthropic.claude-opus-4-6-v1'
    #           'us.anthropic.claude-sonnet-4-5-v1:0' → 'anthropic.claude-sonnet-4-5-v1:0'
    #           'me.anthropic.claude-opus-4-6-v1'     → 'anthropic.claude-opus-4-6-v1'
    stripped = re.sub(r'^(global\.|[a-z]{2,3}\.)', '', model_id)
    
    # Strip provider prefix (first segment before dot)
    if '.' in stripped:
        stripped = stripped.split('.', 1)[1]
    
    # Remove colon-based version suffixes: ':0', ':1.0'
    # Examples: 'claude-sonnet-4-5-v1:0' → 'claude-sonnet-4-5-v1'
    #           'claude-opus-4-6-v1'     → unchanged (no colon)
    stripped = re.sub(r':[\d.]+$', '', stripped)
    # Remove -v suffixed versions: '-v1', '-v1:0' (but NOT bare '-7' which is part of model name)
    # Examples: 'claude-opus-4-6-v1' → 'claude-opus-4-6'
    #           'claude-opus-4-7'    → unchanged (no -v prefix before digit)
    stripped = re.sub(r'-v\d+([:.]\d+)*$', '', stripped)
    
    # Remove date patterns (-YYYYMMDD embedded in model IDs)
    # Examples: 'claude-sonnet-4-5-20250929' → 'claude-sonnet-4-5'
    #           'claude-opus-4-6'            → unchanged (no 8-digit date)
    stripped = re.sub(r'-\d{8}', '', stripped)
    
    # Split on hyphens
    tokens = stripped.split('-')
    
    # Split word+digit tokens: 'llama3' → ['llama', '3'], 'qwen3' → ['qwen', '3']
    # Handles providers like Meta (llama3, llama4), Qwen (qwen3), etc. where model
    # ID merges name+version but QuotaName separates them ("Llama 3", "Qwen3").
    # Only splits when alphabetic part is 2+ chars. Short tokens like 'r1' stay intact.
    # Does NOT split digit+letter patterns (e.g., '70b' stays intact).
    expanded = []
    for token in tokens:
        match = re.match(r'^([a-zA-Z]{2,})(\d+)$', token)
        if match:
            expanded.extend([match.group(1), match.group(2)])
        else:
            expanded.append(token)
    tokens = expanded
    
    # Merge adjacent single-digit tokens into version numbers: ['4', '6'] → ['4.6']
    # Examples: ['claude', 'opus', '4', '6'] → ['claude', 'opus', '4.6']
    #           ['claude', 'opus', '4', '7'] → ['claude', 'opus', '4.7']
    #           ['llama3', '1', '70b']       → ['llama3', '1.70b'] — won't merge (70b is not purely digits)
    merged = []
    i = 0
    while i < len(tokens):
        if (i + 1 < len(tokens)
                and re.match(r'^\d{1,2}$', tokens[i])
                and re.match(r'^\d{1,2}$', tokens[i + 1])):
            merged.append(tokens[i] + '.' + tokens[i + 1])
            i += 2
        else:
            merged.append(tokens[i])
            i += 1
    
    # Filter out empty strings and single-char non-digit tokens (preserve version digits like '5')
    filtered = [t for t in merged if (len(t) > 1 or '.' in t or t.isdigit()) and t]
    
    return filtered


def _model_matches_quota(model_id, quota_name):
    """
    Strict matching: verify that model tokens from model_id appear in QuotaName.
    
    Extracts the model display name from QuotaName (after "for "), strips the
    trailing quota version suffix (e.g., "V1"), and checks that all meaningful
    tokens from model_id are present. Numeric tokens use boundary-aware matching
    to prevent '5' from matching inside '3.5' or '4.5'.
    
    Returns True if all tokens match, False otherwise.
    """
    import re
    
    model_display_name = _extract_model_name_from_quota(quota_name).lower()
    if not model_display_name:
        return False
    
    # Strip trailing quota version suffix (V1, V2, etc.) to avoid collision
    # with model version digits
    model_display_name = re.sub(r'\s+v\d+$', '', model_display_name)
    
    tokens = _normalize_model_id_to_tokens(model_id)
    if not tokens:
        return False
    
    for token in tokens:
        t = token.lower()
        if re.match(r'^[\d.]+$', t):
            # Numeric token: use boundary-aware match to prevent '5' matching inside '3.5'
            if not re.search(r'(?<![.\d])' + re.escape(t) + r'(?![.\d])', model_display_name):
                return False
        else:
            if t not in model_display_name:
                return False
    
    return True


def _is_rpm_quota(quota_name):
    """Check if a QuotaName is an RPM (requests per minute) quota."""
    return 'requests per minute' in quota_name.lower()


def _is_tpm_quota(quota_name):
    """Check if a QuotaName is a TPM (tokens per minute) quota."""
    return 'tokens per minute' in quota_name.lower()


# --- Application Inference Profile Resolution ---

def resolve_application_profile(profile_id):
    """
    Resolve an application inference profile to its underlying model.
    Called ONLY when InferenceProfileType == 'Application'.
    
    Uses bedrock:GetInferenceProfile API which accepts the short profile ID
    (e.g., 'e5t98lwp1dsr') directly — no full ARN needed.
    
    Args:
        profile_id: Short profile ID (e.g., 'e5t98lwp1dsr')
    
    Returns:
        dict: {
            'resolved_model_id': underlying model ID for quota validation,
            'metric_dimension_value': short profile ID for CloudWatch dimensions,
            'model_arn': full ARN of the underlying model/inference-profile
        }
    
    Raises:
        ValueError if profile not found, not ACTIVE, or has no models.
    """
    bedrock = boto3.client('bedrock')
    
    try:
        response = bedrock.get_inference_profile(inferenceProfileIdentifier=profile_id)
    except bedrock.exceptions.ResourceNotFoundException:
        raise ValueError(
            f"Inference profile not found: '{profile_id}'. "
            f"Verify the profile ID is correct and exists in this region. "
            f"Find profiles: https://console.aws.amazon.com/bedrock/home?/inference-profiles#/inference-profiles"
        )
    except Exception as e:
        raise ValueError(
            f"Failed to resolve inference profile '{profile_id}': {str(e)}"
        )
    
    if response.get('status') != 'ACTIVE':
        raise ValueError(
            f"Inference profile '{profile_id}' is not ACTIVE (status: {response.get('status')}). "
            f"Only ACTIVE profiles can be monitored."
        )
    
    profile_type = response.get('type', '')
    if profile_type != 'APPLICATION':
        raise ValueError(
            f"Inference profile '{profile_id}' is type '{profile_type}', not APPLICATION. "
            f"For System-Defined profiles, set InferenceProfileType to 'System-Defined' and use the profile ID directly as BedrockModelId."
        )
    
    models = response.get('models', [])
    if not models:
        raise ValueError(
            f"Inference profile '{profile_id}' has no models configured."
        )
    
    # Extract underlying model ID from the first model's ARN
    # Format: arn:aws:bedrock:{region}::foundation-model/{model-id}
    #     or: arn:aws:bedrock:{region}:{account}:inference-profile/{profile-id}
    model_arn = models[0]['modelArn']
    
    # metric_dimension_value = the short profile ID itself (verified empirically:
    # CloudWatch emits ModelId = short profile ID when invoked via application inference profile)
    metric_dimension_value = response.get('inferenceProfileId', profile_id)
    
    # Match app profile's models array against system-defined profiles to find the source.
    # This preserves the inference mode prefix (global., us., eu.) needed for quota validation.
    app_model_arns = sorted([m['modelArn'] for m in models])
    
    resolved_model_id = None
    resolved_arn = None
    
    paginator = bedrock.get_paginator('list_inference_profiles')
    for page in paginator.paginate(typeEquals='SYSTEM_DEFINED'):
        for sys_profile in page.get('inferenceProfileSummaries', []):
            sys_model_arns = sorted([m['modelArn'] for m in sys_profile.get('models', [])])
            if sys_model_arns == app_model_arns:
                resolved_model_id = sys_profile['inferenceProfileId']
                resolved_arn = sys_profile.get('inferenceProfileArn', '')
                break
        if resolved_model_id:
            break
    
    if not resolved_model_id:
        # Fallback: single-region app profile wrapping a foundation model directly (on-demand)
        if 'foundation-model/' in model_arn:
            resolved_model_id = model_arn.split('foundation-model/', 1)[1]
            resolved_arn = model_arn
        else:
            raise ValueError(
                f"Cannot determine underlying model for inference profile '{profile_id}'. "
                f"No matching system-defined profile found and cannot parse model ARN: '{model_arn}'"
            )
    
    print(f'Resolved application inference profile: profile_id={profile_id}, '
          f'underlying_model={resolved_model_id}, metric_dimension={metric_dimension_value}')
    
    return {
        'resolved_model_id': resolved_model_id,
        'metric_dimension_value': metric_dimension_value,
        'model_arn': resolved_arn
    }


def validate_quota_codes(rpm_quota_code, tpm_quota_code, model_id):
    """
    Validate that quota codes match the specified BedrockModelId.
    Called by QuotaCalculator before calculate_thresholds().
    
    Logic:
      Step 1 — For codes != "NA": calls get_service_quota(code) and verifies:
        a) Model match: QuotaName must contain the model from model_id
           e.g., L-4A6BFAB1 → "...for Anthropic Claude Sonnet 4.5 V1" ≠ model_id opus-4-6 → FAIL
        b) Mode match: QuotaName prefix must align with model_id prefix
           e.g., "Cross-region..." quota but model_id is global.xxx → FAIL
      
      Step 2 — For codes == "NA": lists all bedrock quotas and checks if a matching
        RPM/TPM quota actually exists for the model+mode. If it does → FAIL.
           e.g., RPM=NA but model global.anthropic.claude-opus-4-6-v1 has
                 "Global cross-region model inference requests per minute for Anthropic Claude Opus 4.6 V1"
                 → FAIL with message: "Provide the correct quota code: L-3DD46812"
    
    Raises ValueError with actionable message on validation failure.
    """
    from botocore.config import Config as BotoConfig
    quotas_client = boto3.client(
        'service-quotas',
        config=BotoConfig(
            retries={'mode': 'adaptive', 'max_attempts': 10}
        )
    )
    
    # Determine what type of quota to expect based on model_id prefix
    # global.anthropic.claude-opus-4-6-v1 → 'global'
    # us.anthropic.claude-sonnet-4-5-v1:0 → 'cross-region'
    # anthropic.claude-opus-4-6-v1        → 'on-demand'
    expected_mode = _determine_expected_mode(model_id)
    
    print(f'Validating quota codes: RPM={rpm_quota_code}, TPM={tpm_quota_code}, '
          f'model_id={model_id}, expected_mode={expected_mode}')
    
    # --- Step 1: Validate codes that are NOT "NA" ---
    # Calls get_service_quota() and checks the returned QuotaName for:
    #   a) Model name match — "...for Anthropic Claude Opus 4.6 V1" must match model_id tokens
    #   b) Inference mode match — "Global cross-region..." must match expected_mode from prefix
    for code, quota_type in [(rpm_quota_code, 'RPM'), (tpm_quota_code, 'TPM')]:
        if code.upper() == 'NA':
            continue
        
        try:
            response = quotas_client.get_service_quota(
                ServiceCode='bedrock',
                QuotaCode=code
            )
            quota_name = response['Quota']['QuotaName']
            print(f'{quota_type} code {code} resolved to: {quota_name}')
            
            # Check 1: Does the quota belong to the same model?
            # e.g., code for Sonnet 4.5 used with model_id for Opus 4.6 → FAIL
            if not _model_matches_quota(model_id, quota_name):
                model_in_quota = _extract_model_name_from_quota(quota_name)
                raise ValueError(
                    f"{quota_type} quota code {code} belongs to '{model_in_quota}' "
                    f"(QuotaName: '{quota_name}') but BedrockModelId is '{model_id}'. "
                    f"Provide the correct {quota_type} quota code for your model. "
                    f"Find it at: https://console.aws.amazon.com/servicequotas/home/services/bedrock/quotas"
                )
            
            # Check 2: Does the quota match the inference mode?
            # e.g., geographic code used with global. prefix model_id → FAIL
            actual_mode = _infer_mode_from_quota_name(quota_name)
            
            # Reject bedrock-mantle codes — this solution monitors bedrock-runtime only
            if actual_mode == 'mantle':
                raise ValueError(
                    f"{quota_type} quota code {code} belongs to the bedrock-mantle endpoint "
                    f"('{quota_name}'). This solution monitors the bedrock-runtime endpoint only. "
                    f"Provide the bedrock-runtime {quota_type} quota code for your model."
                )
            
            if actual_mode != expected_mode:
                mode_label = {'global': 'global cross-region', 'cross-region': 'geographic cross-region', 'on-demand': 'on-demand'}
                raise ValueError(
                    f"{quota_type} quota code {code} is for {mode_label.get(actual_mode, actual_mode)} "
                    f"inference ('{quota_name}') but BedrockModelId '{model_id}' uses "
                    f"{mode_label.get(expected_mode, expected_mode)} inference. "
                    f"Provide the {mode_label.get(expected_mode, expected_mode)} {quota_type} quota code for your model. "
                    f"Find it at: https://console.aws.amazon.com/servicequotas/home/services/bedrock/quotas"
                )
            
            # Check 3: Does the quota match the expected type (RPM or TPM)?
            # e.g., "tokens per day" code used as TPM (expects "tokens per minute") → FAIL
            if quota_type == 'RPM' and not _is_rpm_quota(quota_name):
                raise ValueError(
                    f"RequestsPerMinuteQuotaCode {code} is not a requests-per-minute quota "
                    f"(QuotaName: '{quota_name}'). Provide a quota code for 'requests per minute'. "
                    f"Find it at: https://console.aws.amazon.com/servicequotas/home/services/bedrock/quotas"
                )
            if quota_type == 'TPM' and not _is_tpm_quota(quota_name):
                raise ValueError(
                    f"TokensPerMinuteQuotaCode {code} is not a tokens-per-minute quota "
                    f"(QuotaName: '{quota_name}'). Provide a quota code for 'tokens per minute'. "
                    f"Find it at: https://console.aws.amazon.com/servicequotas/home/services/bedrock/quotas"
                )
            
            print(f'{quota_type} code {code} validated successfully')
            
        except quotas_client.exceptions.NoSuchResourceException:
            raise ValueError(
                f"{quota_type} quota code '{code}' not found in Service Quotas for the bedrock service. "
                f"Verify the code at: https://console.aws.amazon.com/servicequotas/home/services/bedrock/quotas"
            )
    
    # --- Step 2: Validate codes that ARE "NA" ---
    # Lists ALL bedrock quotas and checks if an RPM/TPM quota actually exists for this model+mode.
    # If it does exist → user must provide it, not NA.
    # If it doesn't exist (e.g., Opus 4.7 has no RPM) → NA is valid.
    na_checks = []
    if rpm_quota_code.upper() == 'NA':
        na_checks.append('RPM')
    if tpm_quota_code.upper() == 'NA':
        na_checks.append('TPM')
    
    if not na_checks:
        return  # All codes validated in Step 1
    
    print(f'Checking NA codes: {na_checks} — listing all bedrock quotas...')
    
    # Paginate once — reuse for both RPM and TPM checks
    # Uses adaptive retry mode (configured on client) to handle Service Quotas rate limits
    all_quotas = []
    paginator = quotas_client.get_paginator('list_service_quotas')
    for page in paginator.paginate(ServiceCode='bedrock'):
        all_quotas.extend(page.get('Quotas', []))
    
    print(f'Found {len(all_quotas)} total bedrock quotas')
    
    # Filter to quotas matching this model AND this inference mode
    matching_quotas = []
    for q in all_quotas:
        qname = q.get('QuotaName', '')
        if _model_matches_quota(model_id, qname) and _infer_mode_from_quota_name(qname) == expected_mode:
            matching_quotas.append(q)
    
    print(f'Found {len(matching_quotas)} quotas matching model+mode')
    
    # If user said NA for RPM but a matching RPM quota exists → FAIL with correct code
    if 'RPM' in na_checks:
        rpm_matches = [q for q in matching_quotas if _is_rpm_quota(q['QuotaName'])]
        if rpm_matches:
            found = rpm_matches[0]
            raise ValueError(
                f"RequestsPerMinuteQuotaCode is set to 'NA' but model '{model_id}' "
                f"has an RPM quota: '{found['QuotaName']}' (Code: {found['QuotaCode']}). "
                f"Provide the correct quota code: {found['QuotaCode']}"
            )
        else:
            print(f'Confirmed: No RPM quota exists for model {model_id} in {expected_mode} mode — NA is valid')
    
    # If user said NA for TPM but a matching TPM quota exists → FAIL with correct code
    if 'TPM' in na_checks:
        tpm_matches = [q for q in matching_quotas if _is_tpm_quota(q['QuotaName'])]
        if tpm_matches:
            found = tpm_matches[0]
            raise ValueError(
                f"TokensPerMinuteQuotaCode is set to 'NA' but model '{model_id}' "
                f"has a TPM quota: '{found['QuotaName']}' (Code: {found['QuotaCode']}). "
                f"Provide the correct quota code: {found['QuotaCode']}"
            )
        else:
            print(f'Confirmed: No TPM quota exists for model {model_id} in {expected_mode} mode — NA is valid')


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
