import base64
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlsplit

from cachelib import SimpleCache

from manytask import main


def test_gitlab_authorization_requests_read_user_with_pkce(monkeypatch):
    monkeypatch.setenv("FLASK_SECRET_KEY", "test-session-key")
    monkeypatch.setenv("STORAGE", "gsheets")
    monkeypatch.setenv("GDOC_SPREADSHEET_ID", "test-spreadsheet")
    monkeypatch.setenv("GDOC_ACCOUNT_CREDENTIALS_BASE64", base64.b64encode(b"{}").decode())
    monkeypatch.setattr(main.logging.config, "dictConfig", lambda _: None)
    monkeypatch.setattr(main, "FileSystemCache", lambda *args, **kwargs: SimpleCache())
    monkeypatch.setattr(main.glab, "GitLabApi", MagicMock())
    monkeypatch.setattr(main.gdoc, "GoogleDocApi", MagicMock())
    monkeypatch.setattr(main.solutions, "SolutionsApi", MagicMock())
    monkeypatch.setattr(
        main.local_config,
        "TestConfig",
        lambda: main.local_config.DebugLocalConfig(gitlab_url="https://gitlab.com", gitlab_client_id="test-client"),
    )
    app = main.create_app(test=True)
    with app.test_request_context():
        authorization = app.oauth.gitlab.create_authorization_url(
            "https://course.example.test/login_finish", code_verifier="test-verifier-" * 4
        )
    url = urlsplit(authorization["url"])
    query = parse_qs(url.query)
    assert url.netloc == "gitlab.com"
    assert query["scope"] == ["read_user"]
    assert query["redirect_uri"] == ["https://course.example.test/login_finish"]
    assert query["response_type"] == ["code"]
    assert query["code_challenge_method"] == ["S256"]
