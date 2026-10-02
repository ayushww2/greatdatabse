import os


class MemoryStore:
    def __init__(self):
        self.objects = {}

    def put(self, key, fileobj, content_type):
        data = fileobj.read()
        self.objects[key] = {"body": data, "content_type": content_type}
        return len(data)

    def put_bytes(self, key, data, content_type):
        self.objects[key] = {"body": data, "content_type": content_type}
        return len(data)

    def get_bytes(self, key):
        item = self.objects.get(key)
        return item["body"] if item else None

    def get(self, key, range_header=None):
        item = self.objects[key]
        body = item["body"]
        if range_header and range_header.startswith("bytes="):
            start_text, end_text = range_header.split("=", 1)[1].split("-", 1)
            start = int(start_text) if start_text else 0
            end = int(end_text) if end_text else len(body) - 1
            chunk = body[start : end + 1]
            return chunk, 206, {
                "Content-Range": f"bytes {start}-{end}/{len(body)}",
                "Content-Length": str(len(chunk)),
            }
        return body, 200, {"Content-Length": str(len(body))}

    def delete(self, key):
        self.objects.pop(key, None)


class R2Store:
    def __init__(self, client, bucket):
        self.client = client
        self.bucket = bucket

    def put(self, key, fileobj, content_type):
        self.client.upload_fileobj(
            fileobj,
            self.bucket,
            key,
            ExtraArgs={"ContentType": content_type or "application/octet-stream"},
        )
        head = self.client.head_object(Bucket=self.bucket, Key=key)
        return int(head["ContentLength"])

    def put_bytes(self, key, data, content_type):
        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentType=content_type or "application/octet-stream",
        )
        return len(data)

    def get_bytes(self, key):
        from botocore.exceptions import ClientError

        try:
            obj = self.client.get_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if exc.response["Error"]["Code"] in ("NoSuchKey", "404"):
                return None
            raise
        return obj["Body"].read()

    def get(self, key, range_header=None):
        kwargs = {"Bucket": self.bucket, "Key": key}
        if range_header:
            kwargs["Range"] = range_header
        obj = self.client.get_object(**kwargs)
        headers = {}
        if obj.get("ContentRange"):
            headers["Content-Range"] = obj["ContentRange"]
        if obj.get("ContentLength") is not None:
            headers["Content-Length"] = str(obj["ContentLength"])
        status = 206 if range_header else 200
        return obj["Body"], status, headers

    def delete(self, key):
        self.client.delete_object(Bucket=self.bucket, Key=key)


def r2_client():
    import boto3
    from botocore.client import Config

    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        config=Config(signature_version="s3v4"),
        region_name="auto",
    )
