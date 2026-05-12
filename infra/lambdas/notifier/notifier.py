"""Lambda notifier: Batch state-change -> SNS."""

import os
import boto3

sns = boto3.client("sns")
TOPIC_ARN = os.environ["TOPIC_ARN"]


def lambda_handler(event, context):
    detail = event["detail"]
    status = detail["status"]
    if status not in ("SUCCEEDED", "FAILED"):
        return {"skipped": status}
    msg = (
        f"[ml-training] {status}\n"
        f"jobName={detail.get('jobName','?')}\n"
        f"jobId={detail.get('jobId','?')}\n"
        f"command={' '.join(detail.get('container',{}).get('command',[]))}\n"
    )
    if status == "FAILED":
        msg += f"reason={detail.get('statusReason','(sin razon)')}\n"
    sns.publish(TopicArn=TOPIC_ARN, Subject=f"ml-training {status}", Message=msg)
    return {"published": True}
