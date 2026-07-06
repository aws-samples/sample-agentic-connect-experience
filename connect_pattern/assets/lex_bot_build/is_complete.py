import boto3

lex = boto3.client('lexv2-models')


def handler(event, context):
    if event['RequestType'] == 'Delete':
        return {'IsComplete': True}

    bot_id = event['ResourceProperties']['BotId']
    locales = event['ResourceProperties']['Locales']

    for locale_id in locales:
        r = lex.describe_bot_locale(botId=bot_id, botVersion='DRAFT', localeId=locale_id)
        status = r['botLocaleStatus']

        if status == 'Failed':
            raise Exception(f'Build failed for {locale_id}: {r.get('failureReasons')}')

        if status != 'Built':
            return {'IsComplete': False}

    return {'IsComplete': True}
