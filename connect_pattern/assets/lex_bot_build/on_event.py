import boto3

from botocore.exceptions import ClientError

lex = boto3.client("lexv2-models")

# States from which we can (or should) submit a fresh build.
BUILDABLE_STATES = {"NotBuilt", "ReadyExpressTesting", "Built", "Failed"}


def handler(event, context):
    request_type = event["RequestType"]
    props = event["ResourceProperties"]
    physical_id = f"{props['BotId']}-{props['ConfigHash']}"

    if request_type == "Delete":
        return {"PhysicalResourceId": event.get("PhysicalResourceId", "noop")}

    bot_id = props["BotId"]

    for locale_id in props["Locales"]:
        _submit_build(bot_id, locale_id)

    return {"PhysicalResourceId": physical_id}


def _submit_build(bot_id: str, locale_id: str) -> None:
    """Submit a build for the locale, tolerating in-flight builds."""

    try:
        current = lex.describe_bot_locale(
            botId=bot_id, botVersion="DRAFT", localeId=locale_id
        )["botLocaleStatus"]
    except ClientError as e:
        # Locale might not exist yet if the CfnBot resource is still settling;
        # let the polling loop handle it.
        if e.response["Error"]["Code"] == "ResourceNotFoundException":
            return

        raise

    if current in {"Creating", "Building", "Processing", "Importing"}:
        # A build is already in flight (either auto-build or a previous submit).
        # Do not submit again; is_complete will wait for it to finish.
        return

    if current in BUILDABLE_STATES:
        try:
            lex.build_bot_locale(
                botId=bot_id, botVersion="DRAFT", localeId=locale_id
            )
        except ClientError as e:
            # Race: something else started building between describe and build.
            if e.response["Error"]["Code"] == "ConflictException":
                return

            raise
