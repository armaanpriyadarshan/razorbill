import pytest

from razorbill import config


def test_defaults():
    c = config.Config()
    assert c.transcribe_model == "gpt-4o-transcribe-diarize"
    assert c.echo_cancel is True
    assert "razorbill" in c.all_ignores()


def test_load_rejects_unknown_keys(tmp_path, monkeypatch):
    p = tmp_path / "config.toml"
    p.write_text('api_key = "x"\nnot_a_real_option = 1\n')
    monkeypatch.setenv("RAZORBILL_CONFIG", str(p))
    with pytest.raises(SystemExit):
        config.load()


def test_save_api_key_creates_file(tmp_path, monkeypatch):
    p = tmp_path / "config.toml"
    monkeypatch.setenv("RAZORBILL_CONFIG", str(p))
    config.save_api_key("sk-test-123")
    assert 'api_key = "sk-test-123"' in p.read_text()
    assert (p.stat().st_mode & 0o777) == 0o600
    monkeypatch.setenv("OPENAI_API_KEY", "")
    assert config.load().api_key == "sk-test-123"


def test_save_api_key_replaces_existing(tmp_path, monkeypatch):
    p = tmp_path / "config.toml"
    p.write_text('# mine\napi_key = "old"\nnotes_model = "gpt-5"\n')
    monkeypatch.setenv("RAZORBILL_CONFIG", str(p))
    config.save_api_key("new-key")
    text = p.read_text()
    assert 'api_key = "new-key"' in text
    assert '"old"' not in text
    assert 'notes_model = "gpt-5"' in text
