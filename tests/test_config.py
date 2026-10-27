import json

from pokescan.config import ObsSettings, obs_settings


def write_config(tmp_path, **config):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    return path


def test_reads_port_and_password_from_obs_websocket_config(tmp_path):
    path = write_config(tmp_path, server_port=4466, server_password="hunter2", auth_required=True)
    settings = obs_settings(env={}, config_paths=[tmp_path / "missing.json", path])
    assert settings == ObsSettings("ws://127.0.0.1:4466", "hunter2", str(path))


def test_ignores_stored_password_when_auth_is_off(tmp_path):
    path = write_config(tmp_path, server_port=4455, server_password="stale", auth_required=False)
    assert obs_settings(env={}, config_paths=[path]).password == ""


def test_environment_overrides_config_file(tmp_path):
    path = write_config(tmp_path, server_port=4466, server_password="from-file")
    env = {"POKESCAN_OBS_URL": "ws://studio-pc:4455", "POKESCAN_OBS_PASSWORD": "from-env"}
    settings = obs_settings(env=env, config_paths=[path])
    assert (settings.url, settings.password, settings.source) == ("ws://studio-pc:4455", "from-env", "environment")


def test_password_alone_from_environment_keeps_port_from_file(tmp_path):
    path = write_config(tmp_path, server_port=4466, server_password="from-file")
    settings = obs_settings(env={"POKESCAN_OBS_PASSWORD": "from-env"}, config_paths=[path])
    assert (settings.url, settings.password) == ("ws://127.0.0.1:4466", "from-env")


def test_falls_back_to_obs_defaults(tmp_path):
    settings = obs_settings(env={}, config_paths=[tmp_path / "missing.json"])
    assert (settings.url, settings.password) == ("ws://127.0.0.1:4455", "")


def test_repr_hides_password():
    assert "hunter2" not in repr(ObsSettings("ws://127.0.0.1:4455", "hunter2", "test"))
