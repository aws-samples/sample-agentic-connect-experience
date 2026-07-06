import boto3
import os

SECRET_ARN = os.environ["SECRET_ARN"]

connect = boto3.client("connect")
secretsmanager = boto3.client("secretsmanager")


def get_password():
    response = secretsmanager.get_secret_value(SecretId=SECRET_ARN)
    return response["SecretString"]


def on_create(event):
    props = event["ResourceProperties"]
    instance_id = props["InstanceId"]

    routing_profiles = connect.list_routing_profiles(InstanceId=instance_id)
    routing_profile_arn = next(
        rp["Arn"] for rp in routing_profiles["RoutingProfileSummaryList"]
        if rp["Name"] == "Basic Routing Profile"
    )

    security_profiles = connect.list_security_profiles(InstanceId=instance_id)
    security_profile_arn = next(
        sp["Arn"] for sp in security_profiles["SecurityProfileSummaryList"]
        if sp["Name"] == "Admin"
    )

    response = connect.create_user(
        InstanceId=instance_id,
        Username=props["Username"],
        Password=get_password(),
        IdentityInfo={"FirstName": "Admin", "LastName": "User"},
        PhoneConfig={"PhoneType": "SOFT_PHONE", "AutoAccept": True},
        RoutingProfileId=routing_profile_arn,
        SecurityProfileIds=[security_profile_arn],
    )

    return {
        "PhysicalResourceId": response["UserId"],
        "Data": {
            "UserId": response["UserId"],
            "RoutingProfileArn": routing_profile_arn,
            "SecurityProfileArn": security_profile_arn,
        },
    }


def on_delete(event):
    instance_id = event["ResourceProperties"]["InstanceId"]
    user_id = event["PhysicalResourceId"]

    if user_id:
        connect.delete_user(InstanceId=instance_id, UserId=user_id)


def handler(event, context):
    request_type = event["RequestType"]

    if request_type == "Create":
        return on_create(event)
    elif request_type == "Update":
        return on_create(event)
    elif request_type == "Delete":
        on_delete(event)
