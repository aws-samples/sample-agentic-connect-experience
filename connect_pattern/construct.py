import os
import json
import hashlib

from aws_cdk import (
    aws_appintegrations as appintegrations,
    aws_connect as connect,
    aws_wisdom as wisdom,
    custom_resources as cr,
    aws_lex as lex,
    aws_secretsmanager as secretsmanager,
    aws_iam as iam,
    aws_kms as kms,
    aws_bedrockagentcore as agentcore,
    aws_lambda as _lambda,
    aws_lambda_python_alpha as _lambda_python,
    Duration,
    CustomResource,
    Stack,
    CfnTag,
    Fn,
    CfnResource, CfnOutput, RemovalPolicy,
    aws_customerprofiles as profiles,
)
from constructs import Construct
from typing import Dict
from .props import ConnectPatternProps


class ConnectPattern(Construct):
    class CustomCfnInstance:
        def __init__(self, attr_arn, attr_id, instance_alias):
            self.attr_arn = attr_arn
            self.attr_id = attr_id
            self.instance_alias = instance_alias

    _ASSETS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'assets')

    def _get_voice_provider_secret(self):
        return self._voice_provider_secrets.get(self.voice_provider)

    voice_provider_secret = property(_get_voice_provider_secret)

    def __init__(self, scope: Construct, construct_id: str, props: ConnectPatternProps):
        super().__init__(scope, construct_id)

        self.voice_provider = props.voice_provider
        self.instance = self._create_instance(props)
        self._create_customer_profiles_domain(props)
        self.data_tables = {props.name: self._create_data_table(props) for props in props.data_tables}
        self._gw_audience_updater = self._create_gateway_updater_function()
        self.session_data_updater_func = self._create_session_data_updater_function()
        self.country_code_extractor_func = self._create_country_code_extractor_function(props)
        self.assistant, assistant_association = self._create_assistant()
        self.bot_alias = self._create_lex_bot(props)
        self._voice_provider_secrets = {provider: self._create_secret(provider) for provider in ['deepgram', 'elevenlabs']}
        self.agents: Dict[str, wisdom.CfnAIAgent] = {}

        if props.phone_number:
            self.phone_number = self._claim_phone_number(props)
        else:
            self.phone_number = None

        if props.escalation_queue:
            self.escalation_queue = self._create_escalation_queue(props)
        else:
            self.escalation_queue = None

        if props.admin_user:
            self._create_admin_user(props)

        for agent_props in props.agents:
            tool_configs = [self._register_lambda_function_as_tool(tool) for tool in agent_props.mcp_tools]
            kb_association = None

            if agent_props.retrieve_tool_target:
                kb_association = self._add_s3_integration(agent_props.retrieve_tool_target)

            agent = self._create_agent(agent_props, tool_configs, assistant_association, kb_association)

            self._create_security_profile_for_agent(agent, tool_configs)
            self.agents[agent_props.name] = agent

        for provider, secret in self._voice_provider_secrets.items():
            CfnOutput(self, f'{provider.capitalize()}SecretArn', value=secret.secret_arn)

    def _create_instance(self, props: ConnectPatternProps):
        if props.instance.arn is not None:
            return self.CustomCfnInstance(
                attr_arn=props.instance.arn,
                attr_id=props.instance.arn.split('/')[-1],
                instance_alias=props.instance.alias,
            )

        return connect.CfnInstance(
            self, "ConnectInstance",
            identity_management_type="CONNECT_MANAGED",
            instance_alias=props.instance.alias,
            attributes=props.instance.attributes,
        )

    def _create_customer_profiles_domain(self, props: ConnectPatternProps):
        domain_name = f'amazon-connect-{self.instance.instance_alias}-domain'

        key = kms.Key(
            self, "ProfilesKey",
            enable_key_rotation=True,
            removal_policy=RemovalPolicy.DESTROY,
        )

        domain = profiles.CfnDomain(
            self, "CustomerProfilesDomain",
            domain_name=domain_name,
            default_expiration_days=props.instance.default_domain_expiration_days,
            default_encryption_key=key.key_arn,
            rule_based_matching=props.instance.domain_rule_based_matching
        )

        assoc = cr.AwsCustomResource(
            self, "LinkProfilesDomain",
            on_create=cr.AwsSdkCall(
                service="CustomerProfiles",
                action="putIntegration",
                parameters={
                    "DomainName": domain_name,
                    "Uri": self.instance.attr_arn,
                    "ObjectTypeName": "CTR",
                },
                physical_resource_id=cr.PhysicalResourceId.of("profiles-integration"),
            ),
            on_delete=cr.AwsSdkCall(
                service="CustomerProfiles",
                action="deleteIntegration",
                parameters={
                    "DomainName": domain_name,
                    "Uri": self.instance.attr_arn,
                },
                ignore_error_codes_matching="ResourceNotFoundException",
            ),
            policy=cr.AwsCustomResourcePolicy.from_statements([
                iam.PolicyStatement(
                    actions=[
                        "profile:PutIntegration",
                        "profile:DeleteIntegration",
                        "connect:DescribeInstance"
                    ],
                    resources=["*"],
                ),
                iam.PolicyStatement(
                    actions=[
                        "kms:CreateGrant",
                        "kms:DescribeKey",
                        "kms:RetireGrant",
                    ],
                    resources=[key.key_arn],
                ),
            ]),
        )

        assoc.node.add_dependency(domain)

        if isinstance(self.instance, CfnResource):
            domain.node.add_dependency(self.instance)

    def _create_data_table(self, props: ConnectPatternProps.DataTable):
        attr_values = list(props.table_definition.values())
        last_attr = None

        data_table = connect.CfnDataTable(
            self, f"DataTable{props.name}",
            instance_arn=self.instance.attr_arn,
            name=props.name,
            description=props.description,
            time_zone=props.time_zone,
            value_lock_level="NONE",
            status="PUBLISHED",
        )

        attributes = {
            key: connect.CfnDataTableAttribute(
                self, f"{props.name}{key}Attr",
                instance_arn=self.instance.attr_arn,
                data_table_arn=data_table.attr_arn,
                name=key,
                value_type="TEXT",
                primary=key == 'country_code'
            )

            for key in set(attr_values[0]) | {"country_code"}
        }

        for _, attr in attributes.items():
            if last_attr:
                attr.add_dependency(last_attr)

            last_attr = attr

        # Populate table
        for country_code, lang_config in props.table_definition.items():
            record = connect.CfnDataTableRecord(
                self, f"Record{props.name}{country_code}",
                instance_arn=self.instance.attr_arn,
                data_table_arn=data_table.attr_arn,
                data_table_record=connect.CfnDataTableRecord.DataTableRecordProperty(
                    primary_values=[
                        connect.CfnDataTableRecord.ValueProperty(
                            attribute_id=attributes['country_code'].attr_attribute_id,
                            attribute_value=country_code,
                        )
                    ],
                    values=[
                        connect.CfnDataTableRecord.ValueProperty(
                            attribute_id=attributes[k].attr_attribute_id,
                            attribute_value=v,
                        )
                        for k, v in lang_config.items()
                    ]
                )
            )

            record.add_dependency(last_attr)

        return data_table

    def _claim_phone_number(self, props: ConnectPatternProps):
        phone_number = connect.CfnPhoneNumber(
            self, "PhoneNumber",
            target_arn=self.instance.attr_arn,
            type=props.phone_number.type,
            country_code=props.phone_number.country_code,
        )

        if isinstance(self.instance, CfnResource):
            phone_number.add_dependency(self.instance)

        return phone_number

    def _create_escalation_queue(self, props: ConnectPatternProps):
        hours_of_operation = connect.CfnHoursOfOperation(
            self,"EscalationQueueHoursOfOperation",
            instance_arn=self.instance.attr_arn,
            name="Escalation queue hours of operation",
            time_zone=props.escalation_queue.time_zone,
            config=props.escalation_queue.config,
        )

        queue = connect.CfnQueue(
            self,"EscalationQueue",
            instance_arn=self.instance.attr_arn,
            name=props.escalation_queue.name,
            hours_of_operation_arn=hours_of_operation.attr_hours_of_operation_arn,
        )

        if isinstance(self.instance, CfnResource):
            queue.add_dependency(self.instance)

        return queue

    def _create_assistant(self):
        assistant = wisdom.CfnAssistant(
            self, "WisdomAssistant",
            name=f"WisdomAssistant-{Stack.of(self).stack_id}",
            type="AGENT",
        )

        wisdom_association = connect.CfnIntegrationAssociation(
            self,"WisdomAssociation",
            instance_id=self.instance.attr_arn,
            integration_arn=assistant.attr_assistant_arn,
            integration_type="WISDOM_ASSISTANT",
        )

        if isinstance(self.instance, CfnResource):
            wisdom_association.add_dependency(self.instance)

        return assistant, wisdom_association

    def _create_lex_bot(self, props: ConnectPatternProps):
        service_role_name = f"AWSServiceRoleForLexV2Bots_AmazonConnect_{Stack.of(self).account}"

        if props.lex_bot.create_service_linked_role:
            slr = iam.CfnServiceLinkedRole(
                self, "LexV2ConnectSlr",
                aws_service_name="lexv2.amazonaws.com",
                custom_suffix=f"AmazonConnect_{Stack.of(self).account}",
                description="Allows Lex V2 bots to be invoked by Amazon Connect.",
            )

        on_event_bot_build_func = _lambda.Function(
            self, 'FuncOnEventBotBuild',
            runtime=_lambda.Runtime.PYTHON_3_14,
            architecture=_lambda.Architecture.ARM_64,
            handler='on_event.handler',
            timeout=Duration.minutes(1),
            code=_lambda.Code.from_asset(f'{self._ASSETS_PATH}/lex_bot_build')
        )

        on_event_bot_build_func.add_to_role_policy(
            iam.PolicyStatement(
                actions=["lex:DescribeBotLocale", "lex:BuildBotLocale"],
                resources=["*"],
            )
        )

        is_complete_bot_build_func = _lambda.Function(
            self, 'FuncIsCompleteBotBuild',
            runtime=_lambda.Runtime.PYTHON_3_14,
            architecture=_lambda.Architecture.ARM_64,
            handler='is_complete.handler',
            timeout=Duration.minutes(15),
            code=_lambda.Code.from_asset(f'{self._ASSETS_PATH}/lex_bot_build')
        )

        is_complete_bot_build_func.add_to_role_policy(
            iam.PolicyStatement(
                actions=["lex:DescribeBotLocale"],
                resources=["*"],
            )
        )

        bot = lex.CfnBot(
            self,"LexBot",
            role_arn=f"arn:aws:iam::{Stack.of(self).account}:role/aws-service-role/lexv2.amazonaws.com/{service_role_name}",
            name=props.lex_bot.name,
            data_privacy={"ChildDirected": False},
            idle_session_ttl_in_seconds=300,
            auto_build_bot_locales=False,
            bot_locales=[
                lex.CfnBot.BotLocaleProperty(
                    locale_id=locale,
                    nlu_confidence_threshold=0.40,
                    intents=[
                        lex.CfnBot.IntentProperty(
                            name="FallbackIntent",
                            parent_intent_signature="AMAZON.FallbackIntent",
                        ),
                        lex.CfnBot.IntentProperty(
                            name="QInConnect",
                            parent_intent_signature="AMAZON.QInConnectIntent",
                            q_in_connect_intent_configuration=lex.CfnBot.QInConnectIntentConfigurationProperty(
                                q_in_connect_assistant_configuration=lex.CfnBot.QInConnectAssistantConfigurationProperty(
                                    assistant_arn=self.assistant.attr_assistant_arn
                                )
                            ),
                        ),
                    ],
                )

                for locale in props.lex_bot.locales
            ],
        )

        if props.lex_bot.create_service_linked_role:
            bot.node.add_dependency(slr)

        # --- Wait until every locale is fully built on DRAFT ---------------------

        waiter_provider = cr.Provider(
            self, "LexBuildWaiterProvider",
            on_event_handler=on_event_bot_build_func,
            is_complete_handler=is_complete_bot_build_func,
            query_interval=Duration.seconds(15),
            total_timeout=Duration.minutes(15),
        )

        config_hash = hashlib.sha256(
            json.dumps(
                {
                    "locales": props.lex_bot.locales,
                    "intents": ["FallbackIntent", "QInConnect"],
                    "assistant_arn": self.assistant.attr_assistant_arn,
                },
                sort_keys=True,
                default=str,
            ).encode()
        ).hexdigest()[:16]

        bot_built = CustomResource(
            self, "LexBuildWaiter",
            service_token=waiter_provider.service_token,
            properties={
                "BotId": bot.attr_id,
                "Locales": props.lex_bot.locales,
                "ConfigHash": config_hash,
            },
        )
        bot_built.node.add_dependency(bot)

        # --- Now it is safe to snapshot DRAFT into a version ---------------------

        bot_version = lex.CfnBotVersion(
            self,"LexBotVersion",
            bot_id=bot.attr_id,
            bot_version_locale_specification=[
                lex.CfnBotVersion.BotVersionLocaleSpecificationProperty(
                    locale_id=locale,
                    bot_version_locale_details=lex.CfnBotVersion.BotVersionLocaleDetailsProperty(
                        source_bot_version="DRAFT"
                    ),
                )

                for locale in props.lex_bot.locales
            ],
        )
        bot_version.node.add_dependency(bot_built)

        bot_alias = lex.CfnBotAlias(
            self,"LexBotAlias",
            bot_id=bot.attr_id,
            bot_alias_name="Prod",
            bot_version=bot_version.attr_bot_version,
            bot_alias_locale_settings=[
                lex.CfnBotAlias.BotAliasLocaleSettingsItemProperty(
                    locale_id=locale,
                    bot_alias_locale_setting=lex.CfnBotAlias.BotAliasLocaleSettingsProperty(
                        enabled=True,
                    ),
                )
                for locale in props.lex_bot.locales
            ],
        )

        bot_alias.node.add_dependency(bot_version)

        association = connect.CfnIntegrationAssociation(
            self,"LexBotAssociation",
            instance_id=self.instance.attr_arn,
            integration_arn=bot_alias.attr_arn,
            integration_type="LEX_BOT",
        )

        if isinstance(self.instance, CfnResource):
            association.node.add_dependency(self.instance)

        association.node.add_dependency(bot_alias)

        return bot_alias

    def _create_agent(self, props: ConnectPatternProps.Agent, tool_definitions, assistant_association, kb_association):
        tool_configurations = [
            wisdom.CfnAIAgent.ToolConfigurationProperty(
                tool_name='COMPLETE',
                tool_id='COMPLETE',
                tool_type='RETURN_TO_CONTROL',
                description="End the conversation when the customer's issue has been resolved.",
                instruction=wisdom.CfnAIAgent.ToolInstructionProperty(
                    instruction='Use this tool when the customer confirms their issue is resolved or has no further questions.'
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "summary": {
                            "type": "string",
                            "description": "Brief summary of how the issue was resolved"
                        }
                    }
                }
            ),
            wisdom.CfnAIAgent.ToolConfigurationProperty(
                tool_name='ESCALATE',
                tool_id='ESCALATE',
                tool_type='RETURN_TO_CONTROL',
                description='Transfer to a human agent when the issue cannot be resolved or the customer requests it.',
                instruction=wisdom.CfnAIAgent.ToolInstructionProperty(
                    instruction='Use this tool when the customer is frustrated, requests a human agent, or the issue cannot be resolved autonomously.'
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "reason": {
                            "type": "string",
                            "description": "The reason for escalation"
                        }
                    }
                }
            )
        ]

        for definition in tool_definitions:
            tool_configurations.append(
                self._mpc_tool_from_definition(definition)
            )

        if kb_association:
            tool_configurations.append(self._create_retrieve_tool(kb_association))

        prompt = wisdom.CfnAIPrompt(
            self,f"{props.prompt.name}Prompt",
            assistant_id=self.assistant.attr_assistant_id,
            name=props.prompt.name,
            api_format=props.prompt.api_format,
            model_id=props.prompt.model_id,
            template_type=props.prompt.template_type,
            type=props.prompt.type,
            template_configuration=wisdom.CfnAIPrompt.AIPromptTemplateConfigurationProperty(
                text_full_ai_prompt_edit_template_configuration=wisdom.CfnAIPrompt.TextFullAIPromptEditTemplateConfigurationProperty(
                    text=props.prompt.text,
                )
            )
        )
        prompt.add_dependency(assistant_association)

        prompt_version = wisdom.CfnAIPromptVersion(
            self,f"{props.prompt.name}Version",
            ai_prompt_id=prompt.attr_ai_prompt_id,
            assistant_id=self.assistant.attr_assistant_id
        )
        prompt_version.add_dependency(prompt)

        ai_agent = wisdom.CfnAIAgent(
            self,f"{props.name}Agent",
            assistant_id=self.assistant.attr_assistant_id,
            name=props.name,
            type=props.type,
            configuration=wisdom.CfnAIAgent.AIAgentConfigurationProperty(
                orchestration_ai_agent_configuration=wisdom.CfnAIAgent.OrchestrationAIAgentConfigurationProperty(
                    connect_instance_arn=self.instance.attr_arn,
                    orchestration_ai_prompt_id=f"{prompt.attr_ai_prompt_id}:$LATEST",
                    tool_configurations=tool_configurations
                )
            )
        )
        ai_agent.add_dependency(prompt_version)

        if kb_association:
            ai_agent.node.add_dependency(kb_association)

        # Wait for the Gateways to be created
        for definition in tool_definitions:
            ai_agent.node.add_dependency(definition['mcp_app_association'])

        agent_version = wisdom.CfnAIAgentVersion(
            self,f"{props.name}Version",
            ai_agent_id=ai_agent.attr_ai_agent_id,
            assistant_id=self.assistant.attr_assistant_id,
        )
        agent_version.add_dependency(ai_agent)

        return ai_agent

    def _create_security_profile_for_agent(self, agent: wisdom.CfnAIAgent, tool_configs):
        security_profile = connect.CfnSecurityProfile(
            self, f"{agent.name}SecurityProfile",
            instance_arn=self.instance.attr_arn,
            security_profile_name=f"{agent.name}SP",
            permissions=['Wisdom.View'],
            applications=[
                connect.CfnSecurityProfile.ApplicationProperty(
                    application_permissions=[tool_config['tool_name']],
                    namespace=tool_config['gateway'].gateway_id,
                    type='MCP'
                )

                for tool_config in tool_configs
            ],
        )

        # Wait for the Gateways to be created
        for tool_config in tool_configs:
            security_profile.node.add_dependency(tool_config['mcp_app_association'])

        security_profile_id = Fn.select(3, Fn.split("/", security_profile.attr_security_profile_arn))

        if isinstance(self.instance, CfnResource):
            security_profile.node.add_dependency(self.instance)

        agent.node.add_dependency(security_profile)

        associate_sp = cr.AwsCustomResource(
            self, f"{agent.name}SecurityProfileAssociation",
            install_latest_aws_sdk=True,
            on_create=cr.AwsSdkCall(
                service="Connect",
                action="associateSecurityProfiles",
                parameters={
                    "InstanceId": self.instance.attr_id,
                    "EntityType": "AI_AGENT",
                    "EntityArn": agent.attr_ai_agent_arn,
                    "SecurityProfiles": [
                        {"Id": security_profile_id},
                    ],
                },
                physical_resource_id=cr.PhysicalResourceId.of("agent-sp-association"),
            ),
            on_update=cr.AwsSdkCall(
                service="Connect",
                action="associateSecurityProfiles",
                parameters={
                    "InstanceId": self.instance.attr_id,
                    "EntityType": "AI_AGENT",
                    "EntityArn": agent.attr_ai_agent_arn,
                    "SecurityProfiles": [
                        {"Id": security_profile_id},
                    ],
                },
                physical_resource_id=cr.PhysicalResourceId.of("agent-sp-association"),
            ),
            on_delete=cr.AwsSdkCall(
                service="Connect",
                action="disassociateSecurityProfiles",
                parameters={
                    "InstanceId": self.instance.attr_id,
                    "EntityType": "AI_AGENT",
                    "EntityArn": agent.attr_ai_agent_arn,
                    "SecurityProfiles": [
                        {"Id": security_profile_id},
                    ],
                },
            ),
            policy=cr.AwsCustomResourcePolicy.from_statements([
                iam.PolicyStatement(
                    actions=[
                        "connect:AssociateSecurityProfiles",
                        "connect:DisassociateSecurityProfiles",
                        "wisdom:GetAIAgent",
                    ],
                    resources=["*"],
                )
            ])
        )

        associate_sp.node.add_dependency(agent)
        associate_sp.node.add_dependency(security_profile)

    def _create_secret(self, voice_provider):
        kms_key = kms.Key(
            self, f"{voice_provider}SecretKey",
            enable_key_rotation=True,
            removal_policy=RemovalPolicy.DESTROY,
        )

        kms_key.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AllowLexDecrypt",
                effect=iam.Effect.ALLOW,
                principals=[
                    iam.ServicePrincipal("lex.amazonaws.com"),
                    iam.ServicePrincipal("connect.amazonaws.com")
                ],
                actions=[
                    "kms:Decrypt",
                    "kms:DescribeKey"
                ],
                resources=["*"],
            )
        )

        secret = secretsmanager.Secret(
            self, f"{voice_provider}ApiKey",
            description=f"{voice_provider} API key for STT/TTS",
            encryption_key=kms_key,
            removal_policy=RemovalPolicy.DESTROY,
        )

        secret.add_to_resource_policy(
            iam.PolicyStatement(
                sid="LexTrust",
                effect=iam.Effect.ALLOW,
                principals=[
                    iam.ServicePrincipal("lex.amazonaws.com"),
                    iam.ServicePrincipal("connect.amazonaws.com")
                ],
                actions=[
                    "secretsmanager:GetSecretValue",
                ],
                resources=["*"]
            )
        )

        return secret

    def _register_lambda_function_as_tool(self, tool: ConnectPatternProps.McpTool):
        target_name = tool.function.function_name

        gateway = agentcore.Gateway(
            self, f"{tool.function.node.id}Gateway",
            gateway_name=tool.function.node.id,
            authorizer_configuration=agentcore.GatewayAuthorizer.using_custom_jwt(
                discovery_url=f"https://{self.instance.instance_alias}.my.connect.aws/.well-known/openid-configuration",
                allowed_audience=['placeholder']
            )
        )

        if isinstance(self.instance, CfnResource):
            gateway.node.add_dependency(self.instance)

        gateway.grant_invoke(iam.ServicePrincipal("connect.amazonaws.com"))

        agentcore.GatewayTarget.for_lambda(
            self, f"{tool.function.node.id}GatewayTarget",
            gateway_target_name=target_name,
            gateway=gateway,
            lambda_function=tool.function,
            tool_schema=tool.schema,
        )

        # Patch allowed_audience to equal the freshly-minted gateway_id
        audience_update = CustomResource(
            self, f"{tool.function.node.id}GatewayAudienceUpdate",
            service_token=self._gw_audience_updater.service_token,
            properties={"GatewayId": gateway.gateway_id},
            resource_type="Custom::GatewayAudienceUpdate",
        )
        audience_update.node.add_dependency(gateway)

        mcp_app = appintegrations.CfnApplication(
            self, f"{tool.function.node.id}McpApp",
            name=tool.function.node.id,
            namespace=gateway.gateway_id,
            application_source_config=appintegrations.CfnApplication.ApplicationSourceConfigProperty(
                external_url_config=appintegrations.CfnApplication.ExternalUrlConfigProperty(
                    access_url=gateway.gateway_url,
                    approved_origins=[],
                )
            ),
            is_service=False,
            application_type='MCP_SERVER',
            tags=[
                CfnTag(key='AmazonConnectEnabled', value='true'),
            ],
        )

        mcp_app.node.add_dependency(audience_update)

        association = connect.CfnIntegrationAssociation(
            self, f"{gateway.node.id}Association",
            instance_id=self.instance.attr_arn,
            integration_arn=mcp_app.attr_application_arn,
            integration_type="APPLICATION",
        )

        if isinstance(self.instance, CfnResource):
            association.node.add_dependency(self.instance)

        association.node.add_dependency(mcp_app)

        return {
            'tool_name': f"{target_name}___{tool.name}",
            'gateway': gateway,
            'mcp_app_association': association,
            'function': tool.function,
        }

    def _create_admin_user(self, props: ConnectPatternProps):
        secret = secretsmanager.Secret(
            self, "AdminPasswordSecret",
            secret_string_value=props.admin_user.password,
            removal_policy=RemovalPolicy.DESTROY
        )

        handler = _lambda.Function(
            self, 'AdminUserHandler',
            runtime=_lambda.Runtime.PYTHON_3_14,
            architecture=_lambda.Architecture.ARM_64,
            handler='index.handler',
            code=_lambda.Code.from_asset(f'{self._ASSETS_PATH}/func_create_admin'),
            timeout=Duration.seconds(30),
            environment={'SECRET_ARN': secret.secret_arn},
            retry_attempts=0
        )

        secret.grant_read(handler)

        handler.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "connect:CreateUser",
                    "connect:DeleteUser",
                    "connect:ListRoutingProfiles",
                    "connect:ListSecurityProfiles",
                ],
                resources=["*"],
            )
        )

        provider = cr.Provider(
            self, "AdminUserProvider",
            on_event_handler=handler,
        )

        admin_user = CustomResource(
            self, "AdminUserCr",
            service_token=provider.service_token,
            properties={
                "InstanceId": self.instance.attr_id,
                "Username": props.admin_user.username
            }
        )

        admin_user.node.add_dependency(secret)

        if isinstance(self.instance, CfnResource):
            admin_user.node.add_dependency(self.instance)

    def _create_gateway_updater_function(self):
        updater_fn = _lambda.Function(
            self, "GatewayAudienceUpdaterFn",
            runtime=_lambda.Runtime.PYTHON_3_14,
            handler="index.handler",
            code=_lambda.Code.from_asset(f"{self._ASSETS_PATH}/func_gateway_audience_updater"),
            timeout=Duration.minutes(2),
            memory_size=256,
        )

        updater_fn.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "bedrock-agentcore-control:GetGateway",
                "bedrock-agentcore-control:UpdateGateway",
                "bedrock-agentcore:GetGateway",
                "bedrock-agentcore:UpdateGateway",
            ],
            resources=["*"],
        ))

        updater_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["iam:PassRole"],
            resources=["*"],
            conditions={
                "StringEquals": {
                    "iam:PassedToService": "bedrock-agentcore.amazonaws.com"
                }
            },
        ))

        return cr.Provider(
            self, "GatewayAudienceUpdaterProvider",
            on_event_handler=updater_fn,
        )

    def _create_session_data_updater_function(self):
        updater_fn = _lambda.Function(
            self, "FuncUpdateSessionData",
            runtime=_lambda.Runtime.PYTHON_3_14,
            handler="index.handler",
            code=_lambda.Code.from_asset(f"{self._ASSETS_PATH}/func_update_session_data"),
            timeout=Duration.seconds(8),
            memory_size=256,
        )

        updater_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["connect:DescribeContact"],
                resources=[f"{self.instance.attr_arn}/contact/*"],
            )
        )

        updater_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["wisdom:UpdateSessionData"],
                resources=[
                    f"arn:aws:wisdom:{Stack.of(self).region}:{Stack.of(self).account}:assistant/*",
                    f"arn:aws:wisdom:{Stack.of(self).region}:{Stack.of(self).account}:session/*/*",
                ],
            )
        )

        # Let Connect invoke this Lambda from a contact flow.
        updater_fn.add_permission(
            "AllowConnectInvoke",
            principal=iam.ServicePrincipal("connect.amazonaws.com"),
            source_arn=self.instance.attr_arn,
            action="lambda:InvokeFunction",
        )

        association = connect.CfnIntegrationAssociation(
            self, "FuncUpdateSessionDataAssociation",
            instance_id=self.instance.attr_arn,
            integration_arn=updater_fn.function_arn,
            integration_type="LAMBDA_FUNCTION",
        )

        if isinstance(self.instance, CfnResource):
            association.node.add_dependency(self.instance)

        association.node.add_dependency(updater_fn)

        return updater_fn

    def _create_country_code_extractor_function(self, props: ConnectPatternProps):
        supported_country_codes = list({
            key
            for table in props.data_tables
            for key in table.table_definition
        })

        func = _lambda_python.PythonFunction(
            self, "FuncExtractCountryCode",
            runtime=_lambda.Runtime.PYTHON_3_14,
            entry=f"{self._ASSETS_PATH}/func_extract_country_code",
            timeout=Duration.seconds(8),
            memory_size=256,
            environment={
                'FALLBACK_COUNTRY_CODE': props.country_code_detection_fallback,
                'SUPPORTED_COUNTRY_CODES': json.dumps(supported_country_codes),
            }
        )

        # Let Connect invoke this Lambda from a contact flow.
        func.add_permission(
            "AllowConnectInvoke",
            principal=iam.ServicePrincipal("connect.amazonaws.com"),
            source_arn=self.instance.attr_arn,
            action="lambda:InvokeFunction",
        )

        association = connect.CfnIntegrationAssociation(
            self, "FuncExtractCountryCodeAssociation",
            instance_id=self.instance.attr_arn,
            integration_arn=func.function_arn,
            integration_type="LAMBDA_FUNCTION",
        )

        if isinstance(self.instance, CfnResource):
            association.node.add_dependency(self.instance)

        association.node.add_dependency(func)

        return func

    def _add_s3_integration(self, bucket):
        app_integrations = iam.ServicePrincipal("app-integrations.amazonaws.com")

        key = kms.Key(
            self, f"{bucket.node.id}Key",
            enable_key_rotation=True,
        )

        key.add_to_resource_policy(iam.PolicyStatement(
            principals=[app_integrations],
            actions=[
                "kms:Decrypt",
                "kms:GenerateDataKey*",
                "kms:CreateGrant",
                "kms:DescribeKey"
            ],
            resources=["*"],
        ))

        bucket.add_to_resource_policy(iam.PolicyStatement(
            sid="AllowAppIntegrationsRead",
            principals=[app_integrations],
            actions=["s3:*"],
            resources=[
                bucket.bucket_arn,
                bucket.arn_for_objects("*")
            ],
        ))

        data_integration = appintegrations.CfnDataIntegration(
            self, f"{bucket.node.id}S3DataIntegration",
            name=bucket.node.id,
            kms_key=key.key_arn,
            source_uri=f"s3://{bucket.bucket_name}",
        )

        if bucket.policy:
            data_integration.node.add_dependency(bucket.policy)

        kb = wisdom.CfnKnowledgeBase(
            self, f"{bucket.node.id}KnowledgeBase",
            name=bucket.node.id,
            knowledge_base_type="EXTERNAL",
            server_side_encryption_configuration=(
                wisdom.CfnKnowledgeBase.ServerSideEncryptionConfigurationProperty(
                    kms_key_id=key.key_arn,
                )
            ),
            source_configuration=wisdom.CfnKnowledgeBase.SourceConfigurationProperty(
                app_integrations=wisdom.CfnKnowledgeBase.AppIntegrationsConfigurationProperty(
                    app_integration_arn=data_integration.attr_data_integration_arn,
                ),
            ),
        )

        return wisdom.CfnAssistantAssociation(
            self, f"{bucket.node.id}Association",
            assistant_id=self.assistant.attr_assistant_id,
            association_type="KNOWLEDGE_BASE",
            association=wisdom.CfnAssistantAssociation.AssociationDataProperty(
                knowledge_base_id=kb.attr_knowledge_base_id,
            ),
        )

    @staticmethod
    def _create_retrieve_tool(kb_association):
        return wisdom.CfnAIAgent.ToolConfigurationProperty(
            tool_name='Retrieve',
            tool_type='MODEL_CONTEXT_PROTOCOL',
            tool_id='aws_service__qconnect_Retrieve',
            override_input_values=[
                wisdom.CfnAIAgent.ToolOverrideInputValueProperty(
                    json_path='$.retrievalConfiguration.knowledgeSource.assistantAssociationIds',
                    value=wisdom.CfnAIAgent.ToolOverrideInputValueConfigurationProperty(
                        constant=wisdom.CfnAIAgent.ToolOverrideConstantInputValueProperty(
                            type="JSON_STRING",
                            value=Fn.sub(
                                '["${AssocId}"]',
                                {"AssocId": kb_association.attr_assistant_association_id},
                            ),
                        )
                    )
                )
            ]
        )

    @staticmethod
    def _mpc_tool_from_definition(tool_definition):
        safe_name = Fn.join("_", Fn.split("-", tool_definition['function'].function_name))
        tool_id = f'gateway_{tool_definition["gateway"].gateway_id}__{tool_definition["tool_name"]}'

        return wisdom.CfnAIAgent.ToolConfigurationProperty(
            tool_name=f'{safe_name}_',
            tool_type='MODEL_CONTEXT_PROTOCOL',
            tool_id=tool_id
        )

    def associate_contact_flow_with_phone_number(self, contact_flow: connect.CfnContactFlow, phone_number: connect.CfnPhoneNumber):
        physical_id = f"{phone_number.node.id}-flow-association"

        associate_phone = cr.AwsCustomResource(
            self, f"{phone_number.node.id}FlowAssociation",
            on_create=cr.AwsSdkCall(
                service="Connect",
                action="associatePhoneNumberContactFlow",
                parameters={
                    "PhoneNumberId": phone_number.attr_phone_number_arn,
                    "InstanceId": self.instance.attr_arn,
                    "ContactFlowId": contact_flow.attr_contact_flow_arn,
                },
                physical_resource_id=cr.PhysicalResourceId.of(physical_id),
            ),
            on_update=cr.AwsSdkCall(
                service="Connect",
                action="associatePhoneNumberContactFlow",
                parameters={
                    "PhoneNumberId": phone_number.attr_phone_number_arn,
                    "InstanceId": self.instance.attr_arn,
                    "ContactFlowId": contact_flow.attr_contact_flow_arn,
                },
                physical_resource_id=cr.PhysicalResourceId.of(physical_id),
            ),
            on_delete=cr.AwsSdkCall(
                service="Connect",
                action="disassociatePhoneNumberContactFlow",
                parameters={
                    "PhoneNumberId": phone_number.attr_phone_number_arn,
                    "InstanceId": self.instance.attr_arn,
                },
                physical_resource_id=cr.PhysicalResourceId.of(physical_id),
            ),
            policy=cr.AwsCustomResourcePolicy.from_statements([
                iam.PolicyStatement(
                    actions=[
                        "connect:AssociatePhoneNumberContactFlow",
                        "connect:DisassociatePhoneNumberContactFlow",
                    ],
                    resources=["*"],
                )
            ]),
        )

        associate_phone.node.add_dependency(phone_number)
        associate_phone.node.add_dependency(contact_flow)
