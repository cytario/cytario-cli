"""Tests for the `cytario image describe` flow."""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from typer.testing import CliRunner

from cytario_cli import cli, imagedescribe, oidc
from cytario_cli.awsconfig import Connection
from cytario_cli.config import CliState
from cytario_cli.imagedescribe import match_connection, parse_s3_uri, probe_describe_route
from cytario_cli.oidc import LoopbackReceiver

runner = CliRunner()

HOST = "https://app.example.com"


def connection(name: str, bucket: str, prefix: str, conn_id: str | None = None) -> Connection:
    return Connection(
        name=name,
        id=conn_id or f"conn-{name.lower()}",
        bucket_name=bucket,
        prefix=prefix,
        region="eu-central-1",
        s3_endpoint="https://s3.eu-central-1.amazonaws.com",
        sts_endpoint="https://sts.eu-central-1.amazonaws.com",
        role_arn=None,
        access_level="read-only",
    )


CONNECTIONS = [
    connection("Root", "shared-bucket", ""),
    connection("Slides", "shared-bucket", "slides/"),
    connection("Other", "other-bucket", "data/"),
]


class TestParseS3Uri:
    def test_splits_bucket_and_key(self):
        assert parse_s3_uri("s3://bucket/prefix/slide.ome.tif") == ("bucket", "prefix/slide.ome.tif")

    def test_rejects_non_s3_scheme(self):
        assert parse_s3_uri("https://bucket/slide.ome.tif") is None
        assert parse_s3_uri("slide.ome.tif") is None

    def test_rejects_missing_key_or_bucket(self):
        assert parse_s3_uri("s3://bucket") is None
        assert parse_s3_uri("s3:///key.tif") is None


class TestMatchConnection:
    def test_root_prefix_matches_together_with_prefixed(self):
        result = match_connection(CONNECTIONS, "shared-bucket", "slides/a.tif")
        assert [c.name for c in result.matches] == ["Root", "Slides"]
        assert result.path == "a.tif"

    def test_prefix_stripped_from_key(self):
        result = match_connection([CONNECTIONS[1]], "shared-bucket", "slides/a.tif")
        assert [c.name for c in result.matches] == ["Slides"]
        assert result.path == "a.tif"

    def test_other_bucket_single_match(self):
        result = match_connection(CONNECTIONS, "other-bucket", "data/x.tif")
        assert [c.name for c in result.matches] == ["Other"]
        assert result.path == "x.tif"

    def test_no_match(self):
        result = match_connection(CONNECTIONS, "unknown-bucket", "x.tif")
        assert result.matches == []

    def test_key_outside_prefix_matches_only_root(self):
        result = match_connection(CONNECTIONS, "shared-bucket", "other/x.tif")
        assert [c.name for c in result.matches] == ["Root"]


class TestProbeDescribeRoute:
    @respx.mock
    def test_redirect_means_route_present(self):
        respx.get(f"{HOST}/agent/describe").respond(status_code=302, headers={"Location": f"{HOST}/login"})
        assert probe_describe_route(HOST) is True

    @respx.mock
    def test_json_response_means_route_present(self):
        respx.get(f"{HOST}/agent/describe").respond(json={"ok": True})
        assert probe_describe_route(HOST) is True

    @respx.mock
    def test_html_response_means_old_deployment(self):
        respx.get(f"{HOST}/agent/describe").respond(status_code=200, html="<html><body>app</body></html>")
        assert probe_describe_route(HOST) is False

    @respx.mock
    def test_network_error_means_absent(self):
        respx.get(f"{HOST}/agent/describe").mock(side_effect=httpx.ConnectError("no"))
        assert probe_describe_route(HOST) is False


class TestDescribeFlow:
    def test_pretty_prints_payload(self, monkeypatch, capsys):
        payload = {"connectionId": "Slides", "image": {"levelCount": 6}}
        monkeypatch.setattr(imagedescribe.webbrowser, "open", lambda url: True)
        monkeypatch.setattr(LoopbackReceiver, "start", lambda self: None)
        monkeypatch.setattr(LoopbackReceiver, "wait_ready", lambda self: 12345)
        monkeypatch.setattr(LoopbackReceiver, "wait_for_code", lambda self: {"payload": json.dumps(payload)})

        output = imagedescribe.describe_flow(HOST, "Slides", "a.tif", state=None)

        assert json.loads(output) == payload
        assert output.endswith("}")
        assert "levelCount" in output

    def test_error_param_raises(self, monkeypatch):
        monkeypatch.setattr(imagedescribe.webbrowser, "open", lambda url: True)
        monkeypatch.setattr(LoopbackReceiver, "start", lambda self: None)
        monkeypatch.setattr(LoopbackReceiver, "wait_ready", lambda self: 12345)
        monkeypatch.setattr(LoopbackReceiver, "wait_for_code", lambda self: {"error": "boom"})

        with pytest.raises(oidc.OidcError, match="boom"):
            imagedescribe.describe_flow(HOST, "Slides", "a.tif", state=None)

    def test_no_browser_fallback_prints_forwarding_hint(self, monkeypatch, capsys):
        monkeypatch.setattr(imagedescribe.webbrowser, "open", lambda url: False)
        monkeypatch.setattr(LoopbackReceiver, "start", lambda self: None)
        monkeypatch.setattr(LoopbackReceiver, "wait_ready", lambda self: 54321)
        monkeypatch.setattr(LoopbackReceiver, "wait_for_code", lambda self: {"payload": "{}"})

        imagedescribe.describe_flow(HOST, "Slides", "a.tif", state=None)

        output = capsys.readouterr().out
        assert "No browser available" in output
        assert "ssh -L 54321:127.0.0.1:54321" in output

    def test_url_carries_connection_path_and_port(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(imagedescribe.webbrowser, "open", lambda url: seen.update(url=url) or True)
        monkeypatch.setattr(LoopbackReceiver, "start", lambda self: None)
        monkeypatch.setattr(LoopbackReceiver, "wait_ready", lambda self: 12345)
        monkeypatch.setattr(LoopbackReceiver, "wait_for_code", lambda self: {"payload": "{}"})

        imagedescribe.describe_flow(HOST, "Slides", "a b.tif", state=None)

        assert seen["url"] == f"{HOST}/agent/describe?connectionId=Slides&path=a+b.tif&port=12345"


class TestReceiverDonePage:
    def test_custom_done_message(self):
        receiver = LoopbackReceiver(
            done_title="Describe complete",
            done_message="Describe complete — you can close this tab.",
        )
        assert b"Describe complete" in receiver._done_body
        assert b"you can close this tab" in receiver._done_body

    def test_default_done_message_unchanged(self):
        receiver = LoopbackReceiver()
        assert b"Sign-in complete. You can close this window." in receiver._done_body


STATE = {
    "host": HOST,
    "issuer": "https://auth.example.com/realms/cytario",
    "authorization_endpoint": "https://auth.example.com/auth",
    "token_endpoint": "https://auth.example.com/token",
    "refresh_token": "grant",
    "id_token": "header.e30.signature",
    "id_token_expires_at": 9_999_999_999.0,
    "access_token": "still-valid",
    "access_token_expires_at": 9_999_999_999.0,
}


@pytest.fixture(name="patched_state")
def _patched_state(tmp_path, monkeypatch):
    from cytario_cli import config

    monkeypatch.setattr(config, "STATE_FILE", tmp_path / "state.json")
    state = CliState(**STATE)
    state.save()
    return state


class TestImageDescribeCommand:
    def test_requires_sign_in(self, tmp_path, monkeypatch):
        from cytario_cli import config

        monkeypatch.setattr(config, "STATE_FILE", tmp_path / "state.json")
        result = runner.invoke(cli.app, ["image", "describe", "s3://bucket/key.tif", "--host", HOST])
        assert result.exit_code == 2
        assert "Not signed in" in result.output

    def test_rejects_non_s3_uri(self, patched_state):
        result = runner.invoke(cli.app, ["image", "describe", "https://x/y.tif", "--host", HOST])
        assert result.exit_code == 2
        assert "Not an s3 URI" in result.output

    @respx.mock
    def test_no_match_lists_all_connections(self, patched_state):
        api_route = respx.get(f"{HOST}/api/me/connections").respond(json={"connections": []})
        result = runner.invoke(cli.app, ["image", "describe", "s3://bucket/key.tif", "--host", HOST])
        assert result.exit_code == 1
        assert "No connection matches" in result.output
        assert api_route.called

    @respx.mock
    def test_multiple_matches_lists_candidates(self, patched_state):
        respx.get(f"{HOST}/api/me/connections").respond(
            json={
                "connections": [
                    {
                        "id": "conn-root",
                        "name": "Root",
                        "bucketName": "shared-bucket",
                        "prefix": "",
                        "region": "eu-central-1",
                        "s3Endpoint": "https://s3.example.com",
                        "stsEndpoint": "https://sts.example.com",
                        "roleArn": None,
                        "accessLevel": "read-only",
                    },
                    {
                        "id": "conn-slides",
                        "name": "Slides",
                        "bucketName": "shared-bucket",
                        "prefix": "slides/",
                        "region": "eu-central-1",
                        "s3Endpoint": "https://s3.example.com",
                        "stsEndpoint": "https://sts.example.com",
                        "roleArn": None,
                        "accessLevel": "read-write",
                    },
                ]
            }
        )
        result = runner.invoke(
            cli.app, ["image", "describe", "s3://shared-bucket/slides/a.tif", "--host", HOST]
        )
        assert result.exit_code == 1
        assert "matches more than one connection" in result.output
        assert "Root" in result.output
        assert "Slides" in result.output
        assert "read-write" in result.output

    @respx.mock
    def test_old_deployment_prints_version_error(self, patched_state):
        respx.get(f"{HOST}/api/me/connections").respond(
            json={
                "connections": [
                    {
                        "id": "conn-slides",
                        "name": "Slides",
                        "bucketName": "b",
                        "prefix": "p/",
                        "region": "eu-central-1",
                        "s3Endpoint": "https://s3.example.com",
                        "stsEndpoint": "https://sts.example.com",
                        "roleArn": None,
                        "accessLevel": "read-only",
                    }
                ]
            }
        )
        respx.get(f"{HOST}/agent/describe").respond(status_code=200, html="<html></html>")
        result = runner.invoke(cli.app, ["image", "describe", "s3://b/p/a.tif", "--host", HOST])
        assert result.exit_code == 1
        assert "predates the agent-describe route" in result.output
        assert imagedescribe.MIN_AGENT_DESCRIBE_WEB in result.output

    @respx.mock
    def test_happy_path_prints_pretty_json(self, patched_state, monkeypatch):
        payload = {"connectionId": "Slides", "path": "a.tif", "image": {"levelCount": 6}}
        respx.get(f"{HOST}/api/me/connections").respond(
            json={
                "connections": [
                    {
                        "id": "conn-slides",
                        "name": "Slides",
                        "bucketName": "b",
                        "prefix": "p/",
                        "region": "eu-central-1",
                        "s3Endpoint": "https://s3.example.com",
                        "stsEndpoint": "https://sts.example.com",
                        "roleArn": None,
                        "accessLevel": "read-only",
                    }
                ]
            }
        )
        respx.get(f"{HOST}/agent/describe").respond(status_code=302, headers={"Location": f"{HOST}/login"})
        monkeypatch.setattr(imagedescribe.webbrowser, "open", lambda url: True)
        monkeypatch.setattr(LoopbackReceiver, "start", lambda self: None)
        monkeypatch.setattr(LoopbackReceiver, "wait_ready", lambda self: 12345)
        monkeypatch.setattr(LoopbackReceiver, "wait_for_code", lambda self: {"payload": json.dumps(payload)})

        result = runner.invoke(cli.app, ["image", "describe", "s3://b/p/a.tif", "--host", HOST])

        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == payload
        assert "\n" in result.output.strip()

    @respx.mock
    def test_browser_url_carries_connection_id_not_name(self, patched_state, monkeypatch):
        respx.get(f"{HOST}/api/me/connections").respond(
            json={
                "connections": [
                    {
                        "id": "conn-slides",
                        "name": "Slides",
                        "bucketName": "b",
                        "prefix": "p/",
                        "region": "eu-central-1",
                        "s3Endpoint": "https://s3.example.com",
                        "stsEndpoint": "https://s3.example.com",
                        "roleArn": None,
                        "accessLevel": "read-only",
                    }
                ]
            }
        )
        respx.get(f"{HOST}/agent/describe").respond(status_code=302, headers={"Location": f"{HOST}/login"})
        opened: list[str] = []

        monkeypatch.setattr(imagedescribe.webbrowser, "open", lambda url: opened.append(url) or True)
        monkeypatch.setattr(LoopbackReceiver, "start", lambda self: None)
        monkeypatch.setattr(LoopbackReceiver, "wait_ready", lambda self: 12345)
        monkeypatch.setattr(LoopbackReceiver, "wait_for_code", lambda self: {"payload": "{}"})

        result = runner.invoke(cli.app, ["image", "describe", "s3://b/p/a.tif", "--host", HOST])

        assert result.exit_code == 0, result.output
        assert len(opened) == 1
        assert "connectionId=conn-slides" in opened[0]
        assert "Slides" not in opened[0]

    @respx.mock
    def test_error_param_exits_one(self, patched_state, monkeypatch):
        respx.get(f"{HOST}/api/me/connections").respond(
            json={
                "connections": [
                    {
                        "id": "conn-slides",
                        "name": "Slides",
                        "bucketName": "b",
                        "prefix": "p/",
                        "region": "eu-central-1",
                        "s3Endpoint": "https://s3.example.com",
                        "stsEndpoint": "https://sts.example.com",
                        "roleArn": None,
                        "accessLevel": "read-only",
                    }
                ]
            }
        )
        respx.get(f"{HOST}/agent/describe").respond(status_code=302, headers={"Location": f"{HOST}/login"})
        monkeypatch.setattr(imagedescribe.webbrowser, "open", lambda url: True)
        monkeypatch.setattr(LoopbackReceiver, "start", lambda self: None)
        monkeypatch.setattr(LoopbackReceiver, "wait_ready", lambda self: 12345)
        monkeypatch.setattr(LoopbackReceiver, "wait_for_code", lambda self: {"error": "no loader"})

        result = runner.invoke(cli.app, ["image", "describe", "s3://b/p/a.tif", "--host", HOST])

        assert result.exit_code == 1
        assert "no loader" in result.output

    @respx.mock
    def test_timeout_message(self, patched_state, monkeypatch):
        respx.get(f"{HOST}/api/me/connections").respond(
            json={
                "connections": [
                    {
                        "id": "conn-slides",
                        "name": "Slides",
                        "bucketName": "b",
                        "prefix": "p/",
                        "region": "eu-central-1",
                        "s3Endpoint": "https://s3.example.com",
                        "stsEndpoint": "https://sts.example.com",
                        "roleArn": None,
                        "accessLevel": "read-only",
                    }
                ]
            }
        )
        respx.get(f"{HOST}/agent/describe").respond(status_code=302, headers={"Location": f"{HOST}/login"})

        def timeout(_self):
            raise oidc.OidcError("timeout")

        monkeypatch.setattr(imagedescribe.webbrowser, "open", lambda url: True)
        monkeypatch.setattr(LoopbackReceiver, "start", lambda self: None)
        monkeypatch.setattr(LoopbackReceiver, "wait_ready", lambda self: 12345)
        monkeypatch.setattr(LoopbackReceiver, "wait_for_code", timeout)

        result = runner.invoke(cli.app, ["image", "describe", "s3://b/p/a.tif", "--host", HOST])

        assert result.exit_code == 1
        assert "The result redirect never arrived." in result.output
