from aws_lambda_powertools import Logger
import boto3
from botocore.config import Config
import json
from langchain.llms.bedrock import LLMInputOutputAdapter
import logging
import os
import time
import traceback

logger = logging.getLogger(__name__)
if len(logging.getLogger().handlers) > 0:
    logging.getLogger().setLevel(logging.INFO)
else:
    logging.basicConfig(level=logging.INFO)

cloudwatch_logger = Logger()

dynamodb = boto3.resource('dynamodb')

bedrock_region = os.environ.get("BEDROCK_REGION", "us-east-1")
bedrock_role = os.environ.get("BEDROCK_ROLE", None)
bedrock_url = os.environ.get("BEDROCK_URL", None)
table_name = os.environ.get("TABLE_NAME", None)

def _get_bedrock_client():
    try:
        logger.info(f"Create new client\n  Using region: {bedrock_region}")
        session_kwargs = {"region_name": bedrock_region}
        client_kwargs = {**session_kwargs}

        retry_config = Config(
            region_name=bedrock_region,
            retries={
                "max_attempts": 10,
                "mode": "standard",
            },
        )
        session = boto3.Session(**session_kwargs)

        if bedrock_role is not None:
            logger.info(f"Using role: {bedrock_role}")
            sts = session.client("sts")

            response = sts.assume_role(
                RoleArn=str(bedrock_role),  #
                RoleSessionName="amazon-bedrock-assume-role"
            )

            client_kwargs = dict(
                aws_access_key_id=response['Credentials']['AccessKeyId'],
                aws_secret_access_key=response['Credentials']['SecretAccessKey'],
                aws_session_token=response['Credentials']['SessionToken']
            )

        if bedrock_url:
            client_kwargs["endpoint_url"] = bedrock_url

        bedrock_client = session.client(
            service_name="bedrock",
            config=retry_config,
            **client_kwargs
        )

        logger.info("boto3 Bedrock client successfully created!")
        logger.info(bedrock_client._endpoint)
        return bedrock_client

    except Exception as e:
        stacktrace = traceback.format_exc()
        logger.error(stacktrace)

        raise e

def _invoke_text_streaming(bedrock_client, model_id, body, model_kwargs):
    try:
        provider = model_id.split(".")[0]

        request_body = LLMInputOutputAdapter.prepare_input(provider, body["inputs"], model_kwargs)

        request_body = json.dumps(request_body)

        response = bedrock_client.invoke_model_with_response_stream(
            body=request_body,
            modelId=model_id,
            accept="application/json",
            contentType="application/json"
        )

        response = LLMInputOutputAdapter.prepare_output_stream(provider, response)

        return "".join([chunk.text for chunk in response])

    except Exception as e:
        stacktrace = traceback.format_exc()

        logger.error(stacktrace)

        raise e

def _stream(bedrock_client, model_id, body):
    try:
        provider = model_id.split(".")[0]

        response = bedrock_client.invoke_model_with_response_stream(
            body=body,
            modelId=model_id,
            accept="application/json",
            contentType="application/json",
        )
    except Exception as e:
        stacktrace = traceback.format_exc()

        logger.error(stacktrace)

        raise e

    for chunk in LLMInputOutputAdapter.prepare_output_stream(
            provider, response
    ):
        yield chunk

def lambda_handler(event, context):
    try:
        bedrock_client = _get_bedrock_client()

        logger.info(event)
        model_id = event["queryStringParameters"]['model_id']
        request_id = event['queryStringParameters']['request_id']

        logger.info(f"Model ID: {model_id}")
        logger.info(f"Request ID: {request_id}")

        body = json.loads(event["body"])

        logger.info(f"Input body: {body}")

        model_kwargs = body["parameters"] if "parameters" in body else {}

        response = _invoke_text_streaming(bedrock_client, model_id, body, model_kwargs)

        item = {
            "request_id": request_id,
            "generated_text": response,
            "inputs": body["inputs"],
            "model_id": model_id,
            "ttl": int(time.time()) + 2 * 60
        }

        connections = dynamodb.Table(table_name)

        response = connections.put_item(Item=item)

        logger.info(f"Put item: {response}")

        results = {"statusCode": 200, "body": response}

        return results
    except Exception as e:
        stacktrace = traceback.format_exc()

        logger.error(stacktrace)
        return {"statusCode": 500, "body": json.dumps([{"generated_text": stacktrace}])}