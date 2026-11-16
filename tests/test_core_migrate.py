"""bin/migrate_config.py: the user config follows the code. Old keys are dropped, supplied values added, the result
is valid for the settings models, and no value is ever printed."""

import subprocess
import sys
import tomllib
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "bin" / "migrate_config.py"


def migrate(tmp_path, source: str, values: str = ""):
    (tmp_path / "old.toml").write_text(source)
    if values:
        (tmp_path / "values.toml").write_text(values)
    run = subprocess.run([sys.executable, str(SCRIPT), str(tmp_path / "old.toml"), str(tmp_path / "values.toml"), str(tmp_path / "new.toml")],
                         capture_output=True, text=True, check=False)
    new = tomllib.loads((tmp_path / "new.toml").read_text()) if (tmp_path / "new.toml").exists() else None
    return run, new


def test_old_keys_are_dropped_values_added_and_nothing_private_is_printed(tmp_path):
    run, new = migrate(tmp_path, '[project]\nname = "p"\nplugin = "layer"\n\n[layer]\nfrozen = true\nlong_gone = "x"\n',
                       '[layer]\nwindow_open = false\nnot_a_setting = "SECRET-VALUE"\n')
    assert run.returncode == 0, run.stderr
    assert new == {"project": {"name": "p", "plugin": "layer"}, "layer": {"frozen": True, "window_open": False}}
    assert "dropped: layer.long_gone" in run.stdout and "added: layer.window_open" in run.stdout and "ignored values: layer.not_a_setting" in run.stdout
    assert "SECRET-VALUE" not in run.stdout + run.stderr


def test_an_invalid_result_exits_1_and_writes_nothing(tmp_path):
    run, new = migrate(tmp_path, '[project]\nname = "p"\nplugin = "layer"\n\n[layer]\nfrozen = "perhaps"\n')
    assert run.returncode == 1 and new is None and "not valid" in run.stderr
    run, new = migrate(tmp_path, '[project]\nname = "p"\nplugin = "not-installed"\n')
    assert run.returncode == 1 and "not installed" in run.stderr


def test_dictionary_settings_remain_valid_toml(tmp_path):
    run, new = migrate(tmp_path, '[project]\nname = "p"\nplugin = "heinzel"\n\n[heinzel]\n'
                                 'brief_clones = {core = "heinzel", "pet🐾" = "steuermaschine"}\n')
    assert run.returncode == 0, run.stderr
    assert new["heinzel"]["brief_clones"] == {"core": "heinzel", "pet🐾": "steuermaschine"}


def test_the_user_config_can_live_beside_the_release(tmp_path, monkeypatch):
    from steuermaschine import config

    monkeypatch.setenv("STEUERMASCHINE_USER_CONFIG", str(tmp_path / "release" / "user-config.toml"))
    assert config.user_config() == tmp_path / "release" / "user-config.toml"


def test_a_path_value_that_names_an_old_state_path_moves_to_its_new_name(tmp_path):
    """The live config names the queue's file and the train's directory at their old names; the release config names
    the new ones, so it agrees with `sm self migrate-state`. Only key names are printed."""
    import os

    home = Path(os.environ["HOME"])
    state = Path(os.environ["STEUERMASCHINE_HOME"]) / "p"
    run, new = migrate(tmp_path, '[project]\nname = "p"\nplugin = "heinzel"\n\n[heinzel]\nqueue_state_file = "~/.cache/hz-queue.json"\n'
                                 'release_train_state_dir = "~/.cache/hz-train/sub"\nqueue_records_dir = "~/Code/records"\n')
    assert run.returncode == 0, run.stderr
    assert new["heinzel"]["queue_state_file"] == str(state / "queue.json") and new["heinzel"]["release_train_state_dir"] == str(state / "train" / "sub")
    assert new["heinzel"]["queue_records_dir"] == "~/Code/records"
    assert "moved to the new state path: heinzel.queue_state_file, heinzel.release_train_state_dir" in run.stdout
    assert str(home) not in run.stdout
