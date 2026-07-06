import boto3
import os
import json

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(os.environ["TABLE_NAME"])


def handler(event, context):
    try:
        body = json.loads(event.get("body", "{}")) if isinstance(event.get("body"), str) else event

        customer_address = body["customer_address"]
        accepted = body.get("accepted", False)
        windows = body.get("availability_windows")

        key = {"CustomerAddress": customer_address}

        if not accepted:
            table.update_item(
                Key=key,
                UpdateExpression="SET #decision = :decision",
                ExpressionAttributeNames={"#decision": "technician_visit_decision"},
                ExpressionAttributeValues={":decision": "REJECTED"},
            )

            return {
                "statusCode": 200,
                "body": json.dumps({"status": "REJECTED"}),
            }

        if not isinstance(windows, list) or len(windows) == 0:
            raise ValueError("availability_windows is required and must be a non-empty list when accepted is true")

        for i, w in enumerate(windows):
            if not isinstance(w, dict) or "start" not in w or "end" not in w:
                raise ValueError(f"availability_windows[{i}] must have 'start' and 'end' properties")

        table.update_item(
            Key=key,
            UpdateExpression="SET #windows = :windows, #decision = :decision",
            ExpressionAttributeNames={
                "#windows": "technician_visit_availability_windows",
                "#decision": "technician_visit_decision",
            },
            ExpressionAttributeValues={
                ":windows": sorted(windows, key=lambda x: x["start"]),
                ":decision": "CONFIRMED",
            },
        )

        return {
            "statusCode": 200,
            "body": json.dumps({"status": "CONFIRMED"}),
        }
    except Exception as e:
        return {
            "statusCode": 200,
            "body": json.dumps({"status": "SCHEDULE_ERROR", "reason": str(e)}),
        }
