import boto3

client = boto3.client("bedrock-agentcore-control")


def handler(event, context):
    request_type = event["RequestType"]
    gateway_id = event["ResourceProperties"]["GatewayId"]
    physical_id = f"gateway-audience-{gateway_id}"

    if request_type == "Delete":
        return {"PhysicalResourceId": physical_id}

    current = client.get_gateway(gatewayIdentifier=gateway_id)

    authorizer_config = current["authorizerConfiguration"]
    authorizer_config["customJWTAuthorizer"]["allowedAudience"] = [gateway_id]

    update_kwargs = {
        "gatewayIdentifier": gateway_id,
        "name": current["name"],
        "roleArn": current["roleArn"],
        "protocolType": current["protocolType"],
        "authorizerType": current["authorizerType"],
        "authorizerConfiguration": authorizer_config,
    }

    for optional in ("description", "exceptionLevel", "protocolConfiguration", "kmsKeyArn"):
        if current.get(optional) is not None:
            update_kwargs[optional] = current[optional]

    client.update_gateway(**update_kwargs)

    return {
        "PhysicalResourceId": physical_id,
        "Data": {"GatewayId": gateway_id},
    }
