"""Exercise the actual HTTP transport against a local quota-response server."""

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import requests
from google.auth.credentials import AnonymousCredentials
from gspread.exceptions import APIError
from gspread.http_client import HTTPClient

from manytask import gdoc as candidate


class FakeAssertionSession(requests.Session):
    JWT_BEARER_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:jwt-bearer"

    def __init__(self, **kwargs):
        super().__init__()


def session():
    credentials = dict(
        token_uri="https://oauth2.googleapis.com/token", client_email="fake@example.invalid", private_key="fake"
    )
    with (
        patch.object(candidate, "AssertionSession", FakeAssertionSession),
        patch.object(candidate.GoogleDocApi, "_get_sheet", return_value=SimpleNamespace(id=1)),
    ):
        api = candidate.GoogleDocApi("https://docs.google.com", credentials, "fake", 0, cache=None)
    return api._assertion_session


@contextmanager
def endpoint(statuses, retry_after=False):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def handle_request(self):
            calls.append(self.command)
            status = statuses[min(len(calls) - 1, len(statuses) - 1)]
            body = json.dumps({"error": {"code": status, "message": "test response"}}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            if retry_after:
                self.send_header("Retry-After", "1")
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = do_PUT = handle_request

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def client_for(url):
    s = session()
    s.mount(url, s.get_adapter("https://sheets.googleapis.com/"))
    return HTTPClient(AnonymousCredentials(), session=s)


def test_sheets_get_recovers_after_quota_window():
    with (
        endpoint([429] * 6 + [200]) as (url, calls),
        patch("urllib3.util.retry.time.sleep") as sleep,
        patch("urllib3.util.retry.random.random", return_value=0.5),
    ):
        response = client_for(url).request("GET", url)
    assert response.status_code == 200
    assert calls == ["GET"] * 7
    pauses = [call.args[0] for call in sleep.call_args_list]
    assert 62 <= sum(pauses) <= 67
    assert max(pauses) <= 32


def test_exhausted_quota_remains_an_error():
    with endpoint([429]) as (url, calls), patch("urllib3.util.retry.time.sleep"):
        with pytest.raises(APIError) as error:
            client_for(url).request("GET", url)
    assert error.value.response.status_code == 429
    assert calls == ["GET"] * 7


@pytest.mark.parametrize(
    "method,status,retry_after",
    [
        ("POST", 429, False),
        ("PUT", 429, False),
        ("GET", 403, False),
        ("GET", 503, True),
    ],
)
def test_writes_and_unrelated_errors_are_not_retried(method, status, retry_after):
    with endpoint([status], retry_after) as (url, calls), patch("urllib3.util.retry.time.sleep") as sleep:
        with pytest.raises(APIError) as error:
            client_for(url).request(method, url)
    assert error.value.response.status_code == status
    assert calls == [method]
    sleep.assert_not_called()


def test_retry_is_scoped_to_sheets_endpoint():
    s = session()
    assert s.get_adapter("https://oauth2.googleapis.com/token").max_retries.total == 0
    assert s.get_adapter("https://gitlab.com/api/v4/user").max_retries.total == 0
