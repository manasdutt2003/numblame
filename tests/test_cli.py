import pytest

from numblame.cli import build_parser, main

ORDERS = "order_id,amount\nO1,10\nO2,20\n"
CONFIG = '[sources.orders]\npath = "data/orders.csv"\nkey = ["order_id"]\n'
STEP = "SELECT sum(amount) AS total FROM orders"


@pytest.fixture
def project(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "steps").mkdir()
    (tmp_path / "data" / "orders.csv").write_text(ORDERS)
    (tmp_path / "numblame.toml").write_text(CONFIG)
    (tmp_path / "steps" / "01_total.sql").write_text(STEP)
    return tmp_path


def test_project_defaults_to_environment_variable(monkeypatch, project):
    monkeypatch.setenv("NUMBLAME_PROJECT", str(project))
    assert build_parser().parse_args(["runs"]).project == str(project)
    assert main(["run"]) == 0
    assert list((project / ".numblame" / "runs").glob("*.duckdb"))


def test_dash_c_overrides_the_environment_variable(monkeypatch, tmp_path):
    monkeypatch.setenv("NUMBLAME_PROJECT", str(tmp_path / "elsewhere"))
    assert build_parser().parse_args(["-C", "here", "runs"]).project == "here"


def test_project_defaults_to_current_folder(monkeypatch):
    monkeypatch.delenv("NUMBLAME_PROJECT", raising=False)
    assert build_parser().parse_args(["runs"]).project == "."


@pytest.mark.parametrize("flag", ["--limit", "--matches"])
@pytest.mark.parametrize("value", ["0", "-1", "abc"])
def test_non_positive_counts_are_rejected(flag, value, capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["why", "x", flag, value])
    assert exit_info.value.code == 2
    assert "invalid" in capsys.readouterr().err


def test_runs_lists_stored_runs(project, capsys):
    assert main(["-C", str(project), "runs"]) == 0
    assert "No runs yet" in capsys.readouterr().out
    assert main(["-C", str(project), "run"]) == 0
    capsys.readouterr()
    assert main(["-C", str(project), "runs"]) == 0
    assert "no git commit" in capsys.readouterr().out or "  " in capsys.readouterr().out


def test_a_bad_where_clause_exits_with_an_error_not_a_traceback(project, capsys):
    assert main(["-C", str(project), "run"]) == 0
    capsys.readouterr()
    assert main(["-C", str(project), "why", "orders", "--where", "nope ==="]) == 2
    assert capsys.readouterr().err.startswith("numblame:")


def test_missing_project_is_reported(tmp_path, capsys):
    assert main(["-C", str(tmp_path), "run"]) == 2
    assert "No numblame.toml" in capsys.readouterr().err
