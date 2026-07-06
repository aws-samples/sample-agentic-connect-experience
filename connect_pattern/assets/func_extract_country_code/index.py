import phonenumbers
import os
import json

FALLBACK_COUNTRY_CODE = os.environ.get('FALLBACK_COUNTRY_CODE', '+1')
SUPPORTED_COUNTRY_CODES = json.loads(os.environ.get('SUPPORTED_COUNTRY_CODES', "[]"))


def handler(event, context):
    """
    Extract the E.164 country code prefix from the customer's phone number
    using the `phonenumbers` library.

    Wire this Lambda into a contact flow via an "Invoke AWS Lambda function"
    block. Return values are exposed to the flow as $.External.<key>.

    Output:
      {
        "country_code": "+34"
      }
    """
    address = (
        event.get("Details", {})
        .get("ContactData", {})
        .get("CustomerEndpoint", {})
        .get("Address", "")
        .strip()
    )

    try:
        parsed = phonenumbers.parse(address, None)
    except phonenumbers.NumberParseException:
        return _fallback()

    country_code = f"+{parsed.country_code}"

    if not phonenumbers.is_valid_number(parsed) or country_code not in SUPPORTED_COUNTRY_CODES:
        return _fallback()

    return {
        "country_code": country_code,
    }


def _fallback():
    return {
        "country_code": FALLBACK_COUNTRY_CODE
    }
