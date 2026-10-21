"""The bucket's lifecycle rules, as code (13 HLD bucket rules, 12 HLD deployment), applied to the S3 test bucket."""

from bucket_rules import RULES, apply_rules
from conftest import s3


def test_trx08_shr01_uploads_clips_and_superseded_shares_expire_and_running_it_again_changes_nothing(
    bucket: str,
) -> None:
    client = s3()
    apply_rules(client, bucket, RULES)
    apply_rules(client, bucket, RULES)
    stored = client.get_bucket_lifecycle_configuration(Bucket=bucket)["Rules"]
    assert [rule["ID"] for rule in stored] == ["abort-incomplete-mpu", "expire-clips", "expire-superseded"]
    assert (stored[0]["Filter"], stored[0]["AbortIncompleteMultipartUpload"]) == (
        {"Prefix": ""},
        {"DaysAfterInitiation": 2},
    )
    assert (stored[1]["Filter"]["Prefix"], stored[1]["Expiration"]["Days"]) == ("clips/", 1)
    assert (stored[2]["Filter"], stored[2]["Expiration"]["Days"]) == (
        {"Tag": {"Key": "superseded", "Value": "true"}},
        1,
    )
