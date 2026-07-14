# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# AlarmUpdater Lambda — recalculates thresholds and updates CloudWatch alarms.
# Shared source: deployed via S3 (CloudFormation) and archive_file (Terraform).

import boto3
import json
import os
from quota_utils import calculate_thresholds, store_in_parameter_store, discover_sibling_profiles


def update_alarms_sys(customer_name, model_name, thresholds):
    """Update L2 alarms with new thresholds (System-Defined path). Preserves existing Metrics array."""
    cloudwatch = boto3.client('cloudwatch')
    alarm_updates = [
        {'name': customer_name + '-Bedrock-HighInvocationRate-Warning-' + model_name, 'threshold': thresholds['rpm_threshold']},
        {'name': customer_name + '-Bedrock-HighTPMQuotaUsage-Warning-' + model_name, 'threshold': thresholds['tpm_threshold']}
    ]
    for alarm in alarm_updates:
        try:
            response = cloudwatch.describe_alarms(AlarmNames=[alarm['name']])
            if not response['MetricAlarms']:
                print('Alarm not found: ' + alarm['name'])
                continue
            existing_alarm = response['MetricAlarms'][0]
            
            # Alarms always use Metrics format (metric math with Expression: "m1")
            cloudwatch.put_metric_alarm(
                AlarmName=alarm['name'],
                AlarmDescription=existing_alarm.get('AlarmDescription', ''),
                EvaluationPeriods=existing_alarm['EvaluationPeriods'],
                Threshold=alarm['threshold'],
                ComparisonOperator=existing_alarm['ComparisonOperator'],
                TreatMissingData=existing_alarm.get('TreatMissingData', 'notBreaching'),
                Metrics=existing_alarm['Metrics']
            )
            print('Updated alarm: ' + alarm['name'] + ' with threshold: ' + str(alarm['threshold']))
        except Exception as e:
            print('Failed to update alarm ' + alarm['name'] + ': ' + str(e))


def update_alarms_app(customer_name, model_name, thresholds, profile_id):
    """
    Update L2 alarms with metric math aggregation (Application path).
    Discovers all sibling profiles and builds m1+m2+... expression.
    Alarm state is preserved on update (confirmed via AWS docs).
    """
    cloudwatch = boto3.client('cloudwatch')
    
    # Discover all sibling profiles sharing the same underlying model
    sibling_ids = discover_sibling_profiles(profile_id)
    
    if not sibling_ids:
        print('No sibling profiles found — skipping metric math update')
        return
    
    alarm_configs = [
        {'name': customer_name + '-Bedrock-HighInvocationRate-Warning-' + model_name,
         'metric': 'Invocations', 'threshold': thresholds['rpm_threshold']},
        {'name': customer_name + '-Bedrock-HighTPMQuotaUsage-Warning-' + model_name,
         'metric': 'EstimatedTPMQuotaUsage', 'threshold': thresholds['tpm_threshold']}
    ]
    
    for alarm_cfg in alarm_configs:
        try:
            # Read existing alarm to preserve EvaluationPeriods and Period
            existing = cloudwatch.describe_alarms(AlarmNames=[alarm_cfg['name']])
            eval_periods = 5  # default
            period = 60  # default
            if existing.get('MetricAlarms'):
                ea = existing['MetricAlarms'][0]
                eval_periods = ea.get('EvaluationPeriods', 5)
                # For metric math alarms, Period is in the Metrics array
                if ea.get('Metrics'):
                    for m in ea['Metrics']:
                        if m.get('MetricStat', {}).get('Period'):
                            period = m['MetricStat']['Period']
                            break
                elif ea.get('Period'):
                    period = ea['Period']
            
            # Build Metrics array with all sibling profiles
            metrics = []
            ids = []
            for i, pid in enumerate(sibling_ids):
                mid = 'm' + str(i + 1)
                ids.append(mid)
                metrics.append({
                    'Id': mid,
                    'MetricStat': {
                        'Metric': {
                            'Namespace': 'AWS/Bedrock',
                            'MetricName': alarm_cfg['metric'],
                            'Dimensions': [{'Name': 'ModelId', 'Value': pid}]
                        },
                        'Period': period,
                        'Stat': 'Sum'
                    },
                    'ReturnData': False
                })
            
            # Expression: m1+m2+m3+...
            expression = '+'.join(ids)
            metrics.append({
                'Id': 'total',
                'Expression': expression,
                'Label': 'CombinedUsage',
                'ReturnData': True
            })
            
            cloudwatch.put_metric_alarm(
                AlarmName=alarm_cfg['name'],
                AlarmDescription='Bedrock model ' + alarm_cfg['metric'] + ' approaching limits (aggregated across ' + str(len(sibling_ids)) + ' application inference profiles)',
                EvaluationPeriods=eval_periods,
                ComparisonOperator='GreaterThanThreshold',
                Threshold=alarm_cfg['threshold'],
                TreatMissingData='notBreaching',
                Metrics=metrics
            )
            print('Updated alarm ' + alarm_cfg['name'] + ' with ' + str(len(sibling_ids)) + ' profiles: ' + json.dumps(sibling_ids) + ', threshold: ' + str(alarm_cfg['threshold']))
        except Exception as e:
            print('Failed to update alarm ' + alarm_cfg['name'] + ': ' + str(e))


def handler(event, context):
    print('Event: ' + json.dumps(event))
    try:
        customer_name = os.environ['CUSTOMER_NAME']
        model_name = os.environ['BEDROCK_MODEL_NAME']
        rpm_quota_code = os.environ['RPM_QUOTA_CODE']
        tpm_quota_code = os.environ['TPM_QUOTA_CODE']
        rpm_percent = float(os.environ['RPM_THRESHOLD_PERCENT'])
        tpm_percent = float(os.environ['TPM_THRESHOLD_PERCENT'])
        inference_profile_type = os.environ.get('INFERENCE_PROFILE_TYPE', 'System-Defined')
        profile_id = os.environ.get('BEDROCK_MODEL_ID', '')
        
        print('Recalculating thresholds for ' + customer_name + '/' + model_name + ' (type: ' + inference_profile_type + ')')
        thresholds = calculate_thresholds(rpm_quota_code, tpm_quota_code, rpm_percent, tpm_percent)
        print('New thresholds: ' + json.dumps(thresholds))
        
        if inference_profile_type == 'Application':
            update_alarms_app(customer_name, model_name, thresholds, profile_id)
        else:
            update_alarms_sys(customer_name, model_name, thresholds)
        
        store_in_parameter_store(customer_name, model_name, thresholds)
        return {'statusCode': 200, 'body': json.dumps({'message': 'Alarms updated successfully', 'thresholds': thresholds})}
    except Exception as e:
        print('Error: ' + str(e))
        import traceback
        traceback.print_exc()
        return {'statusCode': 500, 'body': json.dumps({'error': str(e)})}
