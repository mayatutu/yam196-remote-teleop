from pathlib import Path
import sys
from types import SimpleNamespace as NS
import pytest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
from open_operator_mirror_test import bind_test, load_credential
import open_operator_mirror_test as launcher


def test_only_dedicated_window_uses_one_reader_and_abort():
    events = []
    dual = NS(python='python', server_ip='127.0.0.1', server_port=5011, rate=60)
    monitor = NS(set_ports=lambda *p: events.append(p))
    abort = lambda *a: events.append(('abort', a))
    window = NS(_teleop=NS(dual=dual, mate_monitor=monitor),
                _state_adapter=NS(teleop_abort=abort, teleop_stop=None),
                mate_panel=NS(hide=lambda: None, setEnabled=lambda v: None), _on_mate_applied_changed=lambda v: None)
    bind_test(window, 'COM18')
    cmd = dual.command_for('COM18', 'COM18')
    assert cmd[2:6] == ['--mirror-test', '--mate', 'COM18', '--server-ip']
    assert '--left-pipermate' not in cmd and '--right-pipermate' not in cmd
    assert window._state_adapter.teleop_stop is abort
    assert window._teleop.mate_com_applied
    assert events == [('COM18', 'COM18')]


@pytest.mark.parametrize('configured_value', ['local-fixture-only', 'dev-state-token'])
def test_reopen_without_shell_environment_or_history_loads_existing_station_credential(tmp_path, monkeypatch, configured_value):
    monkeypatch.delenv('OPERATOR_STATE_TOKEN', raising=False)
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    calls = []
    def run(cmd, **kwargs):
        calls.append(cmd)
        assert kwargs['capture_output'] and kwargs['timeout'] == 15
        return NS(returncode=0, stdout=configured_value + '\n')
    monkeypatch.setattr(launcher.subprocess, 'run', run)
    load_credential()
    assert launcher.os.environ['OPERATOR_STATE_TOKEN'] == configured_value
    assert calls[0][-1] == 'cat /home/ai/yam-teleop/config/state.token'
    assert configured_value not in ' '.join(calls[0])
    monkeypatch.delenv('OPERATOR_STATE_TOKEN')


def test_missing_sources_never_fall_back_to_default(tmp_path, monkeypatch):
    monkeypatch.setenv('OPERATOR_STATE_TOKEN', 'dev-state-token')
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(launcher.subprocess, 'run', lambda *a, **kw: NS(returncode=1, stdout=''))
    with pytest.raises(RuntimeError, match='unavailable'):
        load_credential()
