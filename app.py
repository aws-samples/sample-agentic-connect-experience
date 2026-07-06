import os
import aws_cdk as cdk

from stacks import *
from aws_cdk import Tags


app = cdk.App()

ConnectStack(
    app, "AgenticConnectStack",
    env=cdk.Environment(
        account=app.node.try_get_context("account") or os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region='us-east-1',
    )
)

Tags.of(app).add(key='auto-delete', value='false')

app.synth()
