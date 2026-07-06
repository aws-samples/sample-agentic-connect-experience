import json
import hashlib

from aws_cdk import (
    Stack,
    aws_connect as connect,
    aws_lambda as _lambda, Duration,
    aws_iam as iam,
    aws_s3 as s3,
    aws_s3_deployment as s3_deployment,
    Fn, RemovalPolicy,
    CfnParameter,
    SecretValue,
    aws_dynamodb as dynamodb,
    aws_bedrockagentcore as agentcore,
)
from constructs import Construct
from pathlib import Path
from connect_pattern import *


class ConnectStack(Stack):
    VOICE_PARAMETERS = {
        'deepgram': {
            "TextToSpeechVoice": "$.DataTables.LangConfig.deepgram_voice",
            "TextToSpeechEngine": "deepgram:aura-2",
            "ExternalCredentialSecretARN": ""
        },
        'amazon': {
            "TextToSpeechVoice": "$.DataTables.LangConfig.amazon_voice",
            "TextToSpeechEngine": "$.DataTables.LangConfig.amazon_speaking_engine",
            "TextToSpeechStyle": "$.DataTables.LangConfig.amazon_speaking_style"
        }
    }

    TTS_PARAMETERS = {
        'deepgram': {
            "TextToSpeechEngine": {
                "voiceProvider": "deepgram"
            },
            "TextToSpeechVoice": {
                "useDynamic": True
            }
        },
        'amazon': {
            "TextToSpeechVoice": {
                "useDynamic": True,
                "languageCode": "$.DataTables.LangConfig.language_code"
            },
        }
    }

    Q_IN_CONNECT_SESSION_DATA = {
        "language_name": "$.DataTables.LangConfig.language_name",
        "customer_address": "$.CustomerEndpoint.Address"
    }

    _LAMBDA_RUNTIME = _lambda.Runtime.PYTHON_3_14
    _LAMBDA_ARCH = _lambda.Architecture.ARM_64

    def __init__(self, scope: Construct, construct_id: str, **kwargs):
        super().__init__(scope, construct_id, **kwargs)

        connect_pattern = ConnectPattern(
            self, 'ConnectPattern',
            ConnectPatternProps(
                voice_provider='deepgram',
                country_code_detection_fallback='+1',
                data_tables=[
                    ConnectPatternProps.DataTable(
                        name='CustomerSupportDataTable',
                        time_zone='Europe/Madrid',
                        table_definition=json.loads(
                            Path('assets/connect/customer_support_data_table_definition.json').read_text()
                        ),
                    ),
                    ConnectPatternProps.DataTable(
                        name='TechnicianVisitDataTable',
                        time_zone='Europe/Madrid',
                        table_definition=json.loads(
                            Path('assets/connect/technician_visit_data_table_definition.json').read_text()
                        ),
                    )
                ],
                instance=ConnectPatternProps.Instance(
                    alias='agentic-connect-experience',
                ),
                phone_number=ConnectPatternProps.PhoneNumber(
                    country_code='ES'
                ),
                escalation_queue=ConnectPatternProps.EscalationQueue(
                    time_zone='Europe/Madrid'
                ),
                admin_user=ConnectPatternProps.AdminUser(
                    username='admin',
                    password=SecretValue.unsafe_plain_text(
                        CfnParameter(
                            self, "ConnectAdminPassword",
                            type="String",
                            no_echo=True,
                            min_length=6,
                        ).value_as_string
                    ),
                ),
                lex_bot=ConnectPatternProps.LexBot(
                    name='AgenticConnectBot',
                    locales=['en_US', 'es_ES'],
                ),
                agents=[
                    ConnectPatternProps.Agent(
                        name='CustomerSupportAgent',
                        retrieve_tool_target=self._initialise_customer_support_corpus(),
                        prompt=ConnectPatternProps.Prompt(
                            name='CustomerSupportPrompt',
                            model_id='us.anthropic.claude-haiku-4-5-20251001-v1:0',
                            text=Path('assets/connect/customer_support_prompt.yaml').read_text(),
                        )
                    ),
                    ConnectPatternProps.Agent(
                        name='TechnicianVisitAgent',
                        prompt=ConnectPatternProps.Prompt(
                            name='TechnicianVisitPrompt',
                            model_id='us.anthropic.claude-haiku-4-5-20251001-v1:0',
                            text=Path('assets/connect/technician_visit_prompt.yaml').read_text(),
                        ),
                        mcp_tools=[
                            ConnectPatternProps.McpTool(
                                name='UPDATE_TECHNICIAN_VISIT',
                                schema=agentcore.ToolSchema.from_local_asset(
                                    'assets/connect/technician_visit_tool_schema.json'),
                                function=self._create_technician_visits_table_and_func()
                            )
                        ]
                    )
                ]
            )
        )

        customer_support_contact_flow = self._create_customer_support_contact_flow(connect_pattern)

        connect_pattern.associate_contact_flow_with_phone_number(
            customer_support_contact_flow,
            connect_pattern.phone_number
        )

        technician_visit_contact_flow = self._create_technician_visit_contact_flow(connect_pattern)
        self._create_func_start_outbound_call(connect_pattern, technician_visit_contact_flow)

    def _create_technician_visits_table_and_func(self):
        table = dynamodb.Table(
            self, 'TechnicianVisitsTable',
            partition_key=dynamodb.Attribute(name='CustomerAddress', type=dynamodb.AttributeType.STRING),
            removal_policy=RemovalPolicy.DESTROY,
        )

        func = _lambda.Function(
            self, 'FuncUpdateTechnicianVisit',
            runtime=self._LAMBDA_RUNTIME,
            architecture=self._LAMBDA_ARCH,
            timeout=Duration.seconds(30),
            code=_lambda.Code.from_asset('assets/_lambda/func_update_technician_visit'),
            handler='index.handler',
            environment={
                'TABLE_NAME': table.table_name,
            }
        )

        table.grant_write_data(func)

        return func

    def _initialise_customer_support_corpus(self):
        customer_support_docs_bucket = s3.Bucket(
            self, 'CustomerSupportDocsBucket',
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            enforce_ssl=True,
        )

        s3_deployment.BucketDeployment(
            self, "DeployKbCorpus",
            sources=[s3_deployment.Source.asset('assets/customer_support_docs')],
            destination_bucket=customer_support_docs_bucket,
            prune=True,
            retain_on_delete=False,
        )

        return customer_support_docs_bucket

    def _create_func_start_outbound_call(self, connect_pattern: ConnectPattern, contact_flow):
        contact_flow_id = Fn.select(3, Fn.split("/", contact_flow.attr_contact_flow_arn))

        func = _lambda.Function(
            self,'FuncStartOutboundCall',
            code=_lambda.Code.from_asset(f'assets/_lambda/func_start_outbound_call'),
            handler='index.handler',
            runtime=self._LAMBDA_RUNTIME,
            architecture=self._LAMBDA_ARCH,
            timeout=Duration.seconds(30),
            environment={
                'CONTACT_FLOW_ID': contact_flow_id,
                'INSTANCE_ID': connect_pattern.instance.attr_id,
                'SOURCE_PHONE': connect_pattern.phone_number.attr_address,
            }
        )

        func.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=['connect:StartOutboundVoiceContact'],
                resources=[contact_flow.attr_contact_flow_arn],
            )
        )

        return func

    def _create_customer_support_contact_flow(self, connect_pattern: ConnectPattern):
        flow_name = 'CustomerSupport'
        agent_arn = connect_pattern.agents['CustomerSupportAgent'].attr_ai_agent_arn
        data_table = connect_pattern.data_tables['CustomerSupportDataTable']
        flow_content = self._substitute_template_parameters(connect_pattern, agent_arn, data_table, flow_name)

        return connect.CfnContactFlow(
            self, "CustomerSupportContactFlow",
            description=hashlib.md5(flow_content.encode()).hexdigest()[:8],
            instance_arn=connect_pattern.instance.attr_arn,
            name=flow_name,
            content=flow_content,
            type='CONTACT_FLOW',
            state='ACTIVE',
        )

    def _create_technician_visit_contact_flow(self, connect_pattern: ConnectPattern):
        flow_name = 'TechnicianVisit'
        agent_arn = connect_pattern.agents['TechnicianVisitAgent'].attr_ai_agent_arn
        data_table = connect_pattern.data_tables['TechnicianVisitDataTable']
        flow_content = self._substitute_template_parameters(connect_pattern, agent_arn, data_table, flow_name)

        return connect.CfnContactFlow(
            self, "TechnicianVisitContactFlow",
            description=hashlib.md5(flow_content.encode()).hexdigest()[:8],
            instance_arn=connect_pattern.instance.attr_arn,
            name=flow_name,
            content=flow_content,
            type='CONTACT_FLOW',
            state='ACTIVE',
        )

    def _substitute_template_parameters(self, connect_pattern: ConnectPattern, agent_arn, data_table, contact_flow_name):
        voice_parameters = self.VOICE_PARAMETERS[connect_pattern.voice_provider]
        data_table_id = Fn.select(3, Fn.split("/", data_table.attr_arn))

        if connect_pattern.voice_provider == 'deepgram':
            voice_parameters['ExternalCredentialSecretARN'] = connect_pattern.deepgram_secret.secret_arn

        replacements = {
            '${WISDOM_ASSISTANT_ARN}': connect_pattern.assistant.attr_assistant_arn,
            '${LEX_BOT_ALIAS_ARN}': connect_pattern.bot_alias.attr_arn,
            '${ESCALATION_QUEUE_ARN}': connect_pattern.escalation_queue.attr_queue_arn,
            '${AI_AGENT_VERSION_ARN}': agent_arn,
            '${SESSION_DATA_UPDATER_FUNC_ARN}': connect_pattern.session_data_updater_func.function_arn,
            '${COUNTRY_CODE_EXTRACTOR_FUNC_ARN}': connect_pattern.country_code_extractor_func.function_arn,
            '"${SESSION_DATA_ATTRS}"': json.dumps(self.Q_IN_CONNECT_SESSION_DATA),
            '"${VOICE_PARAMETERS}"': json.dumps(voice_parameters),
            '"${TTS_PARAMETERS}"': json.dumps(self.TTS_PARAMETERS[connect_pattern.voice_provider]),
            '${CONTACT_FLOW_NAME}': contact_flow_name,
            '${DATA_TABLE_NAME}': data_table.name,
            '${DATA_TABLE_ID}': data_table_id,
        }

        flow_template = Path('assets/connect/contact_flow.json').read_text()

        for key, replacement in replacements.items():
            flow_template = flow_template.replace(key, replacement)

        return flow_template
