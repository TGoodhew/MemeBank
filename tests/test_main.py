"""The heartbeat line the "gone quiet" alert looks for, and the log lines the errors alert must not."""
import logging
import re
from types import SimpleNamespace

from slack_sdk.socket_mode.builtin.connection import Connection

from memebank.__main__ import ConnectionDropsAreWarnings, heartbeat_line, socket_mode_logger
from memebank.store import LocalStore, Meme


def handler(connected: bool):
    return SimpleNamespace(client=SimpleNamespace(is_connected=lambda: connected))


def test_heartbeat_line_says_whether_slack_is_connected(tmp_path):
    store = LocalStore(str(tmp_path))
    store.put(Meme(1, "slack:F1", "A", "d", "", blob_name="000001.png"), b"x")
    # deploy.ps1's alert query matches this exact text
    assert heartbeat_line(handler(True), store) == "Heartbeat: connected=True, 1 memes"
    assert "connected=True" not in heartbeat_line(handler(False), store)


# deploy.ps1's errors alert, applied to a line in the log format __main__ sets up
ERROR_LINE = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d+ (ERROR|CRITICAL) ")
LOG_FORMAT = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")


def record(level: int, message: str) -> logging.LogRecord:
    rec = logging.LogRecord("slack_bolt.SocketMode", level, __file__, 1, message, None, None)
    ConnectionDropsAreWarnings().filter(rec)
    return rec


def test_connection_drops_are_logged_as_warnings():
    rec = record(logging.ERROR, "Failed to check the state of sock (session id: abc, error: BrokenPipeError, "
                                "message: [Errno 32] Broken pipe)")
    assert (rec.levelno, rec.levelname) == (logging.WARNING, "WARNING")
    assert not ERROR_LINE.match(LOG_FORMAT.format(rec))


def test_other_errors_still_set_off_the_alert():
    # a reconnect that keeps failing, or a failed listener, still logs as ERROR
    for message in ["Failed to send a request to Slack API server: <urlopen error [Errno -3] "
                    "Temporary failure in name resolution>", "Failed to run a message listener: boom"]:
        rec = record(logging.ERROR, message)
        assert rec.levelname == "ERROR"
        assert ERROR_LINE.match(LOG_FORMAT.format(rec))
    assert record(logging.CRITICAL, "(session id: abc) gone").levelname == "CRITICAL"
    assert record(logging.INFO, "A new session has been established (session id: abc)").levelname == "INFO"


class BrokenSocket:
    def send(self, data):
        raise BrokenPipeError(32, "Broken pipe")


def test_slack_sdk_connection_drop_is_a_warning(caplog):
    # the real failed-ping path in slack_sdk, so a library upgrade that rewords it fails here
    conn = Connection(url="wss://example.invalid/", logger=socket_mode_logger())
    conn.sock = BrokenSocket()
    with caplog.at_level(logging.INFO, logger="slack_bolt.SocketMode"):
        conn.check_state()
    drops = [r for r in caplog.records if "Failed to check the state of sock" in r.getMessage()]
    assert drops and all(r.levelname == "WARNING" for r in drops)


def test_socket_mode_logger_adds_its_filter_once():
    assert socket_mode_logger() is socket_mode_logger()
    assert sum(isinstance(f, ConnectionDropsAreWarnings) for f in socket_mode_logger().filters) == 1
