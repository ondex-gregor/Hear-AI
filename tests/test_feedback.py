import time

from hear_ai.feedback import Feedback


def make(enabled=True):
    calls = []
    return Feedback(enabled, beep=lambda f, ms: calls.append((f, ms))), calls


def test_tons_diferentes_por_evento():
    seen = {}
    for name in ("start", "stop", "done", "error"):
        fb, calls = make()
        getattr(fb, name)()
        fb.join()
        assert calls, name
        seen[name] = tuple(calls)
    assert len(set(seen.values())) == 4


def test_desabilitado_nao_toca():
    fb, calls = make(enabled=False)
    fb.start()
    fb.stop()
    fb.done()
    fb.error()
    fb.join()
    assert calls == []


def test_nao_bloqueia():
    def lento(f, ms):
        time.sleep(0.5)

    fb = Feedback(True, beep=lento)
    t0 = time.perf_counter()
    fb.start()
    assert time.perf_counter() - t0 < 0.2
    fb.join()


def test_erro_no_beep_nao_propaga():
    def quebra(f, ms):
        raise RuntimeError("sem placa de som")

    fb = Feedback(True, beep=quebra)
    fb.error()
    fb.join()


def test_eventos_concorrentes_nao_perdem_threads():
    import threading

    gate = threading.Event()
    chamadas = []

    def beep(f, ms):
        gate.wait(2)
        chamadas.append(f)

    fb = Feedback(True, beep=beep)

    def disparar():
        for _ in range(20):
            fb.start()

    ts = [threading.Thread(target=disparar) for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(fb._threads) == 80  # nenhuma referência perdida (todas vivas, presas no gate)
    gate.set()
    fb.join()
    assert len(chamadas) == 80
