from brainstem.sse_consumer import parse_sse_block


def test_parse_basic_event():
    block = 'event:token\ndata:{"command_id":"abc","token":"hi"}'
    result = parse_sse_block(block)
    assert result is not None
    assert result["event_type"] == "token"
    assert result["data"]["command_id"] == "abc"
    assert result["data"]["token"] == "hi"
    assert result["event_id"] is None


def test_parse_event_with_id():
    block = 'id:evt-123\nevent:status\ndata:{"phase":"routing"}'
    result = parse_sse_block(block)
    assert result is not None
    assert result["event_id"] == "evt-123"
    assert result["event_type"] == "status"


def test_parse_heartbeat():
    block = "event:heartbeat\ndata:{}"
    result = parse_sse_block(block)
    assert result is not None
    assert result["event_type"] == "heartbeat"
    assert result["data"] == {}


def test_parse_multi_line_data():
    block = 'event:token\ndata:{"token":\ndata:"hello"}'
    result = parse_sse_block(block)
    assert result is not None


def test_parse_empty_block():
    result = parse_sse_block("")
    assert result is None


def test_parse_no_event_type():
    block = 'data:{"x":1}'
    result = parse_sse_block(block)
    assert result is None
