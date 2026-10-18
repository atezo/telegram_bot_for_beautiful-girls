import pytest
from sqlalchemy import select
from app.bootstrap import configure_master
from app.config import Config
from app.db import Database, Master


def test_file_and_env_override_without_cloud_fields(tmp_path, monkeypatch):
    config_file = tmp_path / 'master.toml'
    config_file.write_text('master_id = 100\nmaster_name = "Анна"\nbreak_minutes = 20\n')
    monkeypatch.setenv('CONFIG_FILE', str(config_file))
    monkeypatch.setenv('BREAK_MINUTES', '35')
    monkeypatch.setenv('OVERTIME', 'false')
    config = Config.load()
    assert (config.master_id, config.master_name, config.break_minutes, config.overtime) == (100, 'Анна', 35, False)
    assert not hasattr(config, 'google_client_id')


def test_master_created_from_config_and_keeps_bot_settings_on_restart(tmp_path):
    db = Database('sqlite:///' + str(tmp_path / 'booking.db'))
    db.initialize()
    config = Config(master_id=100, master_name='Анна', break_minutes=30)
    configure_master(db, config)
    config.break_minutes = 40
    configure_master(db, config)
    with db.transaction() as s:
        masters = list(s.scalars(select(Master)))
        assert len(masters) == 1
        assert masters[0].break_minutes == 30
    config.master_id = 200
    with pytest.raises(ValueError):
        configure_master(db, config)


def test_separate_databases_allow_independent_master_settings(tmp_path):
    for uid, pause in [(100, 20), (200, 35)]:
        db = Database('sqlite:///' + str(tmp_path / f'{uid}.db'))
        db.initialize()
        configure_master(db, Config(master_id=uid, break_minutes=pause))
        with db.transaction() as s:
            master = s.scalar(select(Master))
            assert (master.telegram_id, master.break_minutes) == (uid, pause)
