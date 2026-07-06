import json
import boto3
import os

from config import *

CONTACT_FLOW_ID = os.environ.get("CONTACT_FLOW_ID")
INSTANCE_ID = os.environ.get("INSTANCE_ID")
SOURCE_PHONE = os.environ.get("SOURCE_PHONE")

connect = boto3.client("connect", region_name='us-east-1')


def _response(status_code, body):
    return {
        'statusCode': status_code,
        'body': json.dumps(body),
        'headers': RESPONSE_HEADERS
    }


def handler(event, context):
    customer_phone = event.get("customer_phone", '')

    try:
        connect.start_outbound_voice_contact(
            DestinationPhoneNumber=customer_phone,
            ContactFlowId=CONTACT_FLOW_ID,
            InstanceId=INSTANCE_ID,
            SourcePhoneNumber=SOURCE_PHONE,
            TrafficType='CAMPAIGN',
        )

        return _response(200, 'Flow started successfully')
    except Exception as e:
        return _response(500, str(e))
