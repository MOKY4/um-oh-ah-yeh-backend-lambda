import json
import logging
import os
import boto3
import openai
from botocore.exceptions import ClientError
from boto3.dynamodb.conditions import Key

#로깅 레벨 설정
logger = logging.getLogger()
logger.setLevel(logging.INFO)
# API KEY 세팅
openai.api_key = os.getenv("OPENAI_API_KEY")
#기본 프롬프트 세팅
DEFAULT_PROMPT = os.getenv("DEFAULT_PROMPT")

def lambda_handler(event, context):
    # DynamoDB 테이블 정보 가져옴
    table_name = os.getenv("DYNAMODB_TABLE_NAME")
    # API Gateway에서 요청하는 경로 매핑
    route_key = event.get("requestContext", {}).get("routeKey")
    # 커넥션 ID 값 가져옴
    connection_id = event.get('requestContext', {}).get('connectionId')
    if table_name is None or route_key is None or connection_id is None:
        return {'statusCode': 400}

    table = boto3.resource("dynamodb").Table(table_name)
    logger.info("Request: %s, use table %s.", route_key, table.name)

    response = {"statusCode": 200}
    # 연결 요청이라면
    if route_key == "$connect":
        response["statusCode"] = handle_connect(table, connection_id)
    # 연결 해제 요청이라면
    elif route_key == "$disconnect":
        response["statusCode"] = handle_disconnect(table, connection_id)
    # 메시지 전송 요청이라면
    elif route_key == "sendmessage":
        body = event.get('body')
        body = json.loads(body if body is not None else '{"message": ""}')
        domain = event.get('requestContext', {}).get('domainName')
        stage = event.get('requestContext', {}).get('stage')
        if domain is None or stage is None:
            logger.warning(
                "Couldn't send message. Bad endpoint in request: domain '%s', "
                "stage '%s'", domain, stage)
            response['statusCode'] = 400
        else:
            # 웹소켓 통신을 위한 APIGatewayManagement 클라이언트 생성
            apig_management_client = boto3.client(
                'apigatewaymanagementapi', endpoint_url=f'https://{domain}/{stage}')
            response["statusCode"] = handle_message(
                table, connection_id, body, apig_management_client)
    # 이외의 요청
    else:
        response["statusCode"] = 404
    return response

def handle_message(table, connection_id, event_body, apig_management_client):
    status_code = 200
    try:
        # 저장되어있는 프롬프트 메시지들을 가져옴
        messages = get_messages(table, connection_id)
        logger.info("Got prompt messages %s.", messages)
        # 메시지 입력받아서 chatgpt에게 요청
        user_message = event_body["message"]
        messages.append({"role": "user", "content": user_message})
        completion = chat_completion(messages)
        # chatgpt 응답
        response = completion.choices[0].message.content
        # 클라이언트에게 응답 전송
        logger.info("response=%s, connectionId=%s", response, connection_id)
        send_response = apig_management_client.post_to_connection(
            Data=response, ConnectionId=connection_id
        )
        logger.info(
            "Posted message to connection %s, got response %s.",
            connection_id, send_response)
        # 응답값으로 메시지 업데이트
        messages.append({"role": "assistant", "content": response})
        add_messages(table, connection_id, messages)

        # 성공적으로 클라이언트에게 응답이 가지 않았다면 재전송
        while send_response['ResponseMetadata']['HTTPStatusCode'] == 200 and 'retry-after' in \
                send_response['ResponseMetadata']['HTTPHeaders']:
            time.sleep(int(send_response['ResponseMetadata']['HTTPHeaders']['retry-after']))
            send_response = apig_management_client.post_to_connection(
                Data=response, ConnectionId=connection_id
            )

    except ClientError:
        logger.exception("Couldn't find prompt messages.")
        status_code = 500
    return status_code

def get_messages(table, connection_id):
    response = table.query(
        KeyConditionExpression=Key('connection_id').eq(connection_id)
    )
    return response['Items'][0]['messages']

def add_messages(table, connection_id, messages):
    table.put_item(Item={'connection_id': connection_id, 'messages': messages})

def chat_completion(messages):
    return openai.ChatCompletion.create(
        model="gpt-3.5-turbo", messages=messages)

def handle_connect(table, connection_id):
    status_code = 200
    try:
        messages = [{'role': "system", 'content': DEFAULT_PROMPT}]
        add_messages(table, connection_id, messages)
        logger.info(
            "Added connection %s", connection_id)
    except ClientError:
        logger.exception(
            "Couldn't add connection %s", connection_id)
        status_code = 503
    return status_code

def handle_disconnect(table, connection_id):
    status_code = 200
    try:
        table.delete_item(Key={'connection_id': connection_id})
        logger.info("Disconnected connection %s.", connection_id)
    except ClientError:
        logger.exception("Couldn't disconnect connection %s.", connection_id)
        status_code = 503
    return status_code