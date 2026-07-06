DEFAULT_LANG = 'en-US'

LANG_CONFIG = {
    'en-US': {
        'voice': 'thalia',
        'language_name': 'English',
        'greeting': 'Hello, this is Smart Machines support. We are calling about your machine $.Attributes.machine_id. How can we help you today?'
    },
    'es-ES': {
        'voice': 'nestor',
        'language_name': 'Spanish',
        'greeting': 'Hola, le llama el soporte de Smart Machines. Le contactamos en relación a su máquina $.Attributes.machine_id. ¿En qué podemos ayudarle?'
    },
    'ja-JP': {
        'voice': 'izanami',
        'language_name': 'Japanese',
        'greeting': 'こんにちは、スマートマシンサポートです。お客様の機械 $.Attributes.machine_id についてご連絡いたしました。本日はどのようなご用件でしょうか。'
    },
}

RESPONSE_HEADERS = {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': '*',
    'Access-Control-Allow-Headers': '*',
    'Access-Control-Allow-Credentials': True
}