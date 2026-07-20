from routes.ws_configs import parse_ws_message


def test_parse_ws_message_accepts_select_and_delete():
    assert parse_ws_message("select:7") == ("select", 7)
    assert parse_ws_message("delete:12") == ("delete", 12)


def test_parse_ws_message_rejects_malformed_frames():
    """Regression: malformed frames used to raise ValueError and kill the
    websocket connection; unknown actions were silently misparsed."""
    for frame in ("select:", "select:abc", "select", "update:3", "", ":3", "select:3:4"):
        assert parse_ws_message(frame) is None
