"""Resolve deployed resource names from the CloudFormation stack outputs (shared by the seed CLIs)."""
import boto3


def outputs(stack_name: str, region: str) -> dict:
    cf = boto3.client("cloudformation", region_name=region)
    stack = cf.describe_stacks(StackName=stack_name)["Stacks"][0]
    return {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}
