# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# QuotaCalculator Lambda — validates quota codes and calculates alarm thresholds.
# Shared source: deployed via S3 (CloudFormation) and archive_file (Terraform).

import json
import urllib3
from quota_utils import validate_quota_codes, calculate_thresholds, store_in_parameter_store, resolve_application_profile

http = urllib3.PoolManager()

def send_response(event, context, status, reason=None, physical_id=None, data=None):
    response_body = {
        'Status': status,
        'Reason': reason or 'See CloudWatch Log Stream: ' + context.log_stream_name,
        'PhysicalResourceId': physical_id or context.log_stream_name,
        'StackId': event['StackId'],
        'RequestId': event['RequestId'],
        'LogicalResourceId': event['LogicalResourceId'],
        'Data': data or {}
    }
    json_response = json.dumps(response_body).encode('utf-8')
    try:
        http.request('PUT', event['ResponseURL'], body=json_response, headers={'Content-Type': ''})
        print('Response sent successfully: ' + status)
    except Exception as e:
        print('Failed to send response: ' + str(e))
    return response_body

def handler(event, context):
    print('Event: ' + json.dumps(event))
    try:
        request_type = event['RequestType']
        if request_type in ['Create', 'Update']:
            props = event['ResourceProperties']
            rpm_quota_code = props['RequestsPerMinuteQuotaCode']
            tpm_quota_code = props['TokensPerMinuteQuotaCode']
            rpm_percent = float(props['RequestsPerMinuteThresholdPercent'])
            tpm_percent = float(props['TokensPerMinuteThresholdPercent'])
            customer_name = props['CustomerName']
            model_name = props['BedrockModelName']
            model_id = props['BedrockModelId']
            inference_profile_type = props.get('InferenceProfileType', 'System-Defined')
            print('InferenceProfileType: ' + inference_profile_type + ', model_id: ' + model_id)
            
            # Resolve and validate based on inference profile type
            if inference_profile_type == 'Application':
                # NEW path: resolve profile → get underlying model → validate against that
                profile_info = resolve_application_profile(model_id)
                resolved_model_id = profile_info['resolved_model_id']
                metric_dimension_value = profile_info['metric_dimension_value']
                resolved_model_id_display = resolved_model_id
                print('Resolved application profile to model: ' + resolved_model_id)
                validate_quota_codes(rpm_quota_code, tpm_quota_code, resolved_model_id)
            else:
                # EXISTING path: validate directly against model_id (unchanged)
                metric_dimension_value = model_id
                resolved_model_id_display = model_id
                validate_quota_codes(rpm_quota_code, tpm_quota_code, model_id)
            
            print('Quota code validation passed')
            print('Calculating thresholds: RPM=' + str(rpm_percent) + '%, TPM=' + str(tpm_percent) + '%')
            thresholds = calculate_thresholds(rpm_quota_code, tpm_quota_code, rpm_percent, tpm_percent)
            print('Calculated thresholds: ' + json.dumps(thresholds))
            # Convert keys to match CloudFormation output format
            cf_thresholds = {
                'RpmThreshold': thresholds['rpm_threshold'],
                'TpmThreshold': thresholds['tpm_threshold'],
                'MetricDimensionValue': metric_dimension_value,
                'ResolvedModelIdDisplay': resolved_model_id_display
            }
            store_in_parameter_store(customer_name, model_name, thresholds)
            # Store resolved display ID in SSM (used by Terraform to read via data source)
            import boto3 as _boto3
            _ssm = _boto3.client('ssm')
            _ssm.put_parameter(
                Name='/' + customer_name + '/bedrock/quota-monitoring/' + model_name + '/resolved-model-id-display',
                Value=resolved_model_id_display,
                Type='String',
                Overwrite=True,
                Description='Resolved model ID for display (human-readable)'
            )
            return send_response(event, context, 'SUCCESS', reason='Thresholds calculated successfully', physical_id=customer_name + '-' + model_name + '-thresholds', data=cf_thresholds)
        elif request_type == 'Delete':
            print('Delete request - no action needed')
            return send_response(event, context, 'SUCCESS', reason='Delete completed', physical_id=event.get('PhysicalResourceId', context.log_stream_name))
    except ValueError as ve:
        print('Validation Error: ' + str(ve))
        return send_response(event, context, 'FAILED', reason='Quota code validation failed: ' + str(ve), physical_id=context.log_stream_name)
    except Exception as e:
        print('Error: ' + str(e))
        import traceback
        traceback.print_exc()
        return send_response(event, context, 'FAILED', reason=str(e), physical_id=context.log_stream_name)
