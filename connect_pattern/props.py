from aws_cdk import (
    aws_connect as connect,
    aws_lambda as _lambda,
    aws_bedrockagentcore as agentcore,
    SecretValue,
    aws_customerprofiles as profiles,
    aws_s3 as s3,
)

from dataclasses import dataclass, field
from typing import List, Optional, Literal, Dict

SUPPORTED_VOICE_PROVIDERS = Literal["amazon", "deepgram", "elevenlabs"]

DEFAULT_LANG_DEFINITION = {
    "+1": {
        "language_code": "en-US",
        "language_name": "English",
        "deepgram_voice": "thalia",
        "neural_voice": "Mathew",
        "welcome_prompt": "Welcome, how can we help you?",
        "error_message": "We're currently experiencing a technical issue. We'll call you back shortly — apologies for the inconvenience."
      }
}


@dataclass
class ConnectPatternProps:
    @dataclass
    class Prompt:
        text: str
        name: str
        model_id: str = 'us.amazon.nova-pro-v1:0'
        template_type: str = 'TEXT'
        api_format: str = 'MESSAGES'
        type: str = 'ORCHESTRATION'

    @dataclass
    class PhoneNumber:
        country_code: str
        type: str = 'TOLL_FREE'

    @dataclass
    class Instance:
        alias: str
        arn: Optional[str] = None
        default_domain_expiration_days: int = 365
        domain_rule_based_matching: profiles.CfnDomain.RuleBasedMatchingProperty = field(
            default_factory=lambda: profiles.CfnDomain.RuleBasedMatchingProperty(
                enabled=True,
                matching_rules=[
                    profiles.CfnDomain.MatchingRuleProperty(
                        rule=["PhoneNumber"],
                    ),
                ],
                max_allowed_rule_level_for_matching=1,
                max_allowed_rule_level_for_merging=1,
                attribute_types_selector=profiles.CfnDomain.AttributeTypesSelectorProperty(
                    attribute_matching_model="ONE_TO_ONE",
                    phone_number=["PhoneNumber"],
                    email_address=["EmailAddress"],
                ),
                conflict_resolution=profiles.CfnDomain.ConflictResolutionProperty(
                    conflict_resolving_model="RECENCY",
                ),
            ),
        )
        attributes: connect.CfnInstance.AttributesProperty = field(
            default_factory=lambda: connect.CfnInstance.AttributesProperty(
                inbound_calls=True,
                outbound_calls=True,
                contactflow_logs=True,
                contact_lens=True,
                auto_resolve_best_voices=True,
                high_volume_out_bound=True
            )
        )

    @dataclass
    class EscalationQueue:
        time_zone: str
        name: str = 'Escalation queue'
        config: List[connect.CfnHoursOfOperation.HoursOfOperationConfigProperty] = field(
            default_factory=lambda: [
                connect.CfnHoursOfOperation.HoursOfOperationConfigProperty(
                    day=day,
                    start_time=connect.CfnHoursOfOperation.HoursOfOperationTimeSliceProperty(
                        hours=8, minutes=0
                    ),
                    end_time=connect.CfnHoursOfOperation.HoursOfOperationTimeSliceProperty(
                        hours=20, minutes=0
                    ),
                )
                for day in ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY"]
            ]
        )

    @dataclass
    class Agent:
        name: str
        prompt: ConnectPatternProps.Prompt
        mcp_tools: List[ConnectPatternProps.McpTool] = field(default_factory=lambda: [])
        type: str = 'ORCHESTRATION'
        retrieve_tool_target: Optional[s3.Bucket] = None

    @dataclass
    class LexBot:
        name: str
        locales: Optional[List[str]] = field(default_factory=lambda: [])
        create_service_linked_role: Optional[bool] = False

    @dataclass
    class McpTool:
        function: _lambda.Function
        schema: agentcore.ToolSchema
        name: str

    @dataclass
    class AdminUser:
        password: SecretValue
        username: str = 'admin'

    @dataclass
    class DataTable:
        name: str
        table_definition: Dict[str, Dict[str, str]]
        time_zone: str
        description: str = '.'

    instance: Instance
    lex_bot: LexBot
    country_code_detection_fallback: str

    phone_number: Optional[PhoneNumber] = None
    escalation_queue: Optional[EscalationQueue] = None
    agents: Optional[List[Agent]] = field(default_factory=lambda: [])
    admin_user: Optional[AdminUser] = None
    voice_provider: SUPPORTED_VOICE_PROVIDERS = 'amazon'
    data_tables: List[DataTable] = field(default_factory=lambda: [
        ConnectPatternProps.DataTable(
            name='Default',
            time_zone='Europe/Madrid',
            table_definition = field(default_factory=lambda: DEFAULT_LANG_DEFINITION)
        )
    ])
