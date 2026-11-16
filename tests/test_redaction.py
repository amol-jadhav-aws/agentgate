from agentgate.common.redaction import redact


def test_redacts_nested():
    out, counts = redact(
        {"a": ["AKIAABCDEFGHIJKLMNOP", {"b": "mail me at x@y.io", "c": 3}], "token": "Bearer abc.def"}
    )
    assert out["a"][0] == "[REDACTED:aws_access_key]"
    assert "[REDACTED:email]" in out["a"][1]["b"] and out["a"][1]["c"] == 3
    assert out["token"] == "[REDACTED:bearer]"
    assert counts == {"aws_access_key": 1, "email": 1, "bearer": 1}
