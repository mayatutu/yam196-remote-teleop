"""The test adapter must own exactly one UART and mirror both stop states."""
import sys
from pathlib import Path
from types import SimpleNamespace as NS, ModuleType

import pytest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
from mirror_mate_test import SDK, mirror_source


@pytest.mark.parametrize('input_error', [False, True, None])
def test_mirror_opens_reads_and_closes_one_uart_and_sends_matching_blocks(monkeypatch, input_error):
    monkeypatch.syspath_prepend(str(SDK))
    module = ModuleType('mirror_test_adapter')
    module.__file__ = str(SDK / 'remote_dual_client.py')
    monkeypatch.setitem(sys.modules, module.__name__, module)
    namespace = module.__dict__
    exec(compile(mirror_source((SDK / 'remote_dual_client.py').read_text(encoding='utf-8')), '<test>', 'exec'), namespace)
    counts = {'open': 0, 'read': 0, 'close': 0}
    class Reader:
        def __init__(self, *args): counts['open'] += 1
        def close(self): counts['close'] += 1
    def read_once(reader, state, lock, stop):
        counts['read'] += 1
        if input_error is None:
            return  # No first valid sample yet: both outputs must remain stopped.
        state.targets = namespace['PiperTargets']((10, 20, 30, 40, 50, 60), 0)
        state.input_error = input_error
        state.read_count = 1
    class Worker:
        def __init__(self, *, target, args, **kwargs): self.target, self.args = target, args
        def start(self): self.target(*self.args)
        def join(self, **kwargs): pass
    packets = []
    class Sock:
        def setblocking(self, *args): pass
        def close(self): pass
    namespace['parse_args'] = lambda: NS(rate=60, status_rate=1, all_stop_retries=1,
        all_stop_interval_ms=20, server_ip='127.0.0.1', server_port=5011,
        left_pipermate='COM_TEST', right_pipermate='MIRROR_TEST', gripper=False,
        manual_session_id='', heartbeat_path='unused')
    namespace['PiperMateReader'] = Reader
    namespace['read_loop'] = read_once
    monkeypatch.setattr(namespace['threading'], 'Thread', Worker)
    monkeypatch.setattr(namespace['socket'], 'socket', lambda *a: Sock())
    def send(sock, addr, left, right, all_stop=False):
        packets.append((left, right))
        raise KeyboardInterrupt
    namespace['send_command'] = send
    namespace['send_all_stop'] = lambda *a: None
    if input_error is None:
        def pending_sleep(_seconds):
            raise KeyboardInterrupt
        monkeypatch.setattr(namespace['time'], 'sleep', pending_sleep)
    namespace['main']()
    assert counts == {'open': 1, 'read': 1, 'close': 1}
    if input_error is None:
        assert packets == []  # Must not lock the real no-auto-resume server with an initial stop.
        return
    assert len(packets) == 1
    left, right = packets[0]
    assert left.joint_targets == right.joint_targets
    assert left.stop is right.stop is (input_error is not False)
    assert left.seq == right.seq


def test_adapter_refuses_unmatched_sdk():
    with pytest.raises(RuntimeError, match='refusing'):
        mirror_source('unknown SDK revision')
