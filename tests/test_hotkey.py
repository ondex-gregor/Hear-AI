import logging
import queue
import threading
from types import SimpleNamespace

import pytest

from hear_ai.hotkey import (
    DEFAULT_HOTKEY,
    ERROR_HOTKEY_ALREADY_REGISTERED,
    KEY_VK,
    MOD_ALT,
    MOD_CONTROL,
    MOD_NOREPEAT,
    MOD_SHIFT,
    HotkeyBinder,
    Win32HotkeyApi,
    parse_hotkey,
    record_hotkey,
    validate_hotkey,
    wait_keys_released,
)

# T16: o atalho deixou de usar o hook da lib `keyboard` e passou a usar RegisterHotKey.
# Os testes antigos que mandavam eventos de scan code para `_on_event` foram trocados por
# testes equivalentes com uma API Win32 falsa (WM_HOTKEY + GetAsyncKeyState simulados):
# hold/toggle, repetição, soltar qualquer tecla, atalho inválido, uninstall, set_mode,
# wait_released. Nenhum teste injeta teclas.

VK_CTRL, VK_ALT, VK_SHIFT, VK_LWIN, VK_SPACE = 0x11, 0x12, 0x10, 0x5B, 0x20


class FakeApi:
    """RegisterHotKey/UnregisterHotKey/GetMessageW/GetAsyncKeyState simulados."""

    def __init__(self, register_ok=True, err=ERROR_HOTKEY_ALREADY_REGISTERED):
        self.register_ok = register_ok
        self.err = err
        self.msgs: queue.Queue = queue.Queue()
        self.down: set[int] = set()
        self.registered: dict[int, tuple[int, int]] = {}
        self.register_calls = []
        self.unregistered = []
        self.threads = []

    def thread_id(self):
        self.threads.append(threading.get_ident())
        return 4242

    def ensure_queue(self):
        pass

    def register(self, hk_id, mods, vk):
        self.register_calls.append((hk_id, mods, vk))
        if not self.register_ok:
            return False, self.err
        self.registered[hk_id] = (mods, vk)
        return True, 0

    def unregister(self, hk_id):
        self.unregistered.append(hk_id)
        self.registered.pop(hk_id, None)

    def get_message(self):
        return self.msgs.get()

    def post_quit(self, tid):
        assert tid == 4242
        self.msgs.put(None)

    def is_down(self, vk):
        return vk in self.down

    # simulação de "o Windows mandou WM_HOTKEY"
    def hotkey(self, hk_id=HotkeyBinder.HOTKEY_ID):
        self.msgs.put((Win32HotkeyApi.WM_HOTKEY, hk_id))

    def other_message(self):
        self.msgs.put((0x0400, 0))


class FakeKB:
    """Só para os testes de bandeja abaixo (o binder aceita `kb` por compatibilidade)."""

    def is_pressed(self, name):
        return False


class FakeController:
    def __init__(self):
        self.calls = []
        self.cond = threading.Condition()

    def _add(self, name):
        with self.cond:
            self.calls.append(name)
            self.cond.notify_all()

    def begin(self):
        self._add("begin")

    def end(self):
        self._add("end")

    def toggle(self):
        self._add("toggle")

    def wait_for(self, n, timeout=3.0):
        with self.cond:
            return self.cond.wait_for(lambda: len(self.calls) >= n, timeout)


def fast_sleep(_s):
    threading.Event().wait(0.002)


def make(mode="hold", hotkey="ctrl+alt+space", api=None, **kw):
    api = api or FakeApi()
    ctl = FakeController()
    b = HotkeyBinder(hotkey, mode, ctl, api=api, sleep=fast_sleep, **kw)
    return api, ctl, b


# --- parse / validação -------------------------------------------------------------

def test_parse_e_forma_canonica():
    assert parse_hotkey("Ctrl+Alt+Space") == (("ctrl", "alt"), "space")
    assert parse_hotkey("alt + control + H") == (("ctrl", "alt"), "h")
    assert validate_hotkey("shift+CTRL+f9") == (True, "ctrl+shift+f9")
    assert validate_hotkey("left windows+ctrl+k") == (True, "ctrl+win+k")


@pytest.mark.parametrize("hk", ["", "ctrl+alt", "ctrl+a+b", "ctrl+tecla_inexistente", "ctrl++a", "h"])
def test_combinacoes_mal_formadas(hk):
    ok, motivo = validate_hotkey(hk)
    assert not ok and motivo


@pytest.mark.parametrize("hk", ["win+l", "ctrl+alt+del", "ctrl+alt+delete", "alt+tab", "alt+f4",
                                "ctrl+shift+esc", "ctrl+esc", "alt+space", "alt+shift+tab"])
def test_combinacoes_reservadas_do_windows_sao_rejeitadas(hk):
    ok, motivo = validate_hotkey(hk)
    assert not ok and "reservad" in motivo


def test_so_shift_ou_so_win_nao_bastam():
    assert validate_hotkey("shift+a")[0] is False
    assert validate_hotkey("f9")[0] is False
    assert validate_hotkey("win+h")[0] is False  # ditado do Windows / reservados
    assert validate_hotkey("ctrl+win+h")[0] is True


def test_altgr_do_abnt2_e_rejeitado():
    ok, motivo = validate_hotkey("ctrl+alt+q")
    assert not ok and "AltGr" in motivo
    assert validate_hotkey("ctrl+alt+h")[0] is True


def test_atalho_invalido_cai_no_padrao_com_aviso(caplog):
    with caplog.at_level(logging.WARNING, logger="hear_ai.hotkey"):
        _, _, b = make(hotkey="ctrl+tecla_inexistente")
    assert b.hotkey == DEFAULT_HOTKEY
    assert "inválido" in caplog.text


def test_atalho_reservado_no_config_cai_no_padrao():
    _, _, b = make(hotkey="alt+tab")
    assert b.hotkey == DEFAULT_HOTKEY


# --- registro e conflito -------------------------------------------------------------

def test_install_registra_com_noreplay_na_thread_do_laco():
    api, ctl, b = make(hotkey="ctrl+alt+space")
    assert b.install() is True
    assert b.installed and not b.conflict and b.conflict_reason is None
    (hk_id, mods, vk), = api.register_calls
    assert mods == MOD_CONTROL | MOD_ALT | MOD_NOREPEAT and vk == VK_SPACE
    assert api.threads and api.threads[0] != threading.get_ident()  # thread própria
    assert b.install() is True and len(api.register_calls) == 1  # idempotente
    b.uninstall()
    assert api.unregistered == [hk_id] and not b.installed


def test_conflito_detectado_avisa_e_nao_instala_outra_coisa(caplog):
    avisos = []
    api, ctl, b = make(api=FakeApi(register_ok=False), on_conflict=avisos.append)
    with caplog.at_level(logging.WARNING, logger="hear_ai.hotkey"):
        assert b.install() is False
    assert b.conflict is True
    assert "já está em uso" in b.conflict_reason and "ctrl+alt+space" in b.conflict_reason
    assert avisos == [b.conflict_reason]
    assert "já está em uso" in caplog.text
    assert not b.installed and api.registered == {}
    api.hotkey()  # sem laço de mensagens: nada acontece
    assert ctl.calls == []
    b.uninstall()  # não quebra


def test_outro_erro_do_windows_tambem_e_conflito_com_codigo():
    avisos = []
    _, _, b = make(api=FakeApi(register_ok=False, err=5), on_conflict=avisos.append)
    assert b.install() is False
    assert b.conflict and "erro 5" in b.conflict_reason and avisos


def test_on_conflict_com_erro_nao_quebra():
    def ruim(_r):
        raise RuntimeError("ui")

    _, _, b = make(api=FakeApi(register_ok=False), on_conflict=ruim)
    assert b.install() is False and b.conflict


def test_api_indisponivel_nao_e_conflito():
    class Quebrada(FakeApi):
        def ensure_queue(self):
            raise OSError("sem user32")

    avisos = []
    _, _, b = make(api=Quebrada(), on_conflict=avisos.append)
    assert b.install() is False
    assert b.conflict is False and "OSError" in b.conflict_reason and avisos == []


def test_reinstalar_depois_de_uninstall():
    api, _, b = make()
    b.install()
    b.uninstall()
    assert b.install() is True
    assert len(api.register_calls) == 2
    b.uninstall()


# --- modo segurar / alternar -----------------------------------------------------------

def test_hold_begin_no_wm_hotkey_e_end_quando_uma_tecla_solta():
    api, ctl, b = make("hold")
    b.install()
    api.down = {VK_CTRL, VK_ALT, VK_SPACE}
    api.hotkey()
    assert ctl.wait_for(1) and ctl.calls == ["begin"]
    threading.Event().wait(0.05)
    assert ctl.calls == ["begin"]  # ainda segurando
    api.down.discard(VK_ALT)  # soltou só o Alt
    assert ctl.wait_for(2) and ctl.calls == ["begin", "end"]
    b.uninstall()
    assert ctl.calls == ["begin", "end"]


def test_hold_debounce_exige_duas_leituras_soltas():
    """Uma leitura solta isolada (ruído) não encerra; duas seguidas encerram."""
    todas = {VK_CTRL, VK_ALT, VK_SPACE}
    sem_ctrl = {VK_ALT, VK_SPACE}
    # estado visto em cada rodada de polling (uma rodada por sleep)
    rodadas = [todas, sem_ctrl, todas, todas, sem_ctrl, sem_ctrl, todas, todas]
    seq = {"n": 0, "fim_em": None}

    class Roteiro(FakeApi):
        def is_down(self, vk):
            return vk in rodadas[min(seq["n"], len(rodadas)) - 1]

    def sleep(_s):
        seq["n"] += 1
        threading.Event().wait(0.001)

    api = Roteiro()
    ctl = FakeController()
    original_end = ctl.end

    def end():
        seq["fim_em"] = seq["n"]
        original_end()

    ctl.end = end
    b = HotkeyBinder("ctrl+alt+space", "hold", ctl, api=api, sleep=sleep)
    b.install()
    api.hotkey()
    assert ctl.wait_for(2)
    assert seq["fim_em"] == 6  # a rodada 2 (solta isolada) foi ignorada; 5 e 6 encerraram
    b.uninstall()


def test_hold_tap_rapido_tambem_termina():
    api, ctl, b = make("hold")
    b.install()
    api.down = set()  # já soltou quando o WM_HOTKEY chegou
    api.hotkey()
    assert ctl.wait_for(2) and ctl.calls == ["begin", "end"]
    b.uninstall()


def test_hold_ignora_wm_hotkey_repetido_enquanto_segura():
    api, ctl, b = make("hold")
    b.install()
    api.down = {VK_CTRL, VK_ALT, VK_SPACE}
    for _ in range(5):
        api.hotkey()
    assert ctl.wait_for(1)
    threading.Event().wait(0.05)
    assert ctl.calls == ["begin"]
    api.down.clear()
    assert ctl.wait_for(2) and ctl.calls == ["begin", "end"]
    b.uninstall()


def test_hold_segunda_vez_funciona():
    api, ctl, b = make("hold")
    b.install()
    for i in range(2):
        api.down = {VK_CTRL, VK_ALT, VK_SPACE}
        api.hotkey()
        assert ctl.wait_for(2 * i + 1)
        api.down.clear()
        assert ctl.wait_for(2 * i + 2)
    assert ctl.calls == ["begin", "end", "begin", "end"]
    b.uninstall()


def test_uninstall_durante_hold_encerra_a_gravacao():
    api, ctl, b = make("hold")
    b.install()
    api.down = {VK_CTRL, VK_ALT, VK_SPACE}
    api.hotkey()
    assert ctl.wait_for(1)
    b.uninstall()
    assert ctl.calls == ["begin", "end"]


def test_toggle_cada_wm_hotkey_alterna_sem_polling():
    api, ctl, b = make("toggle")
    b.install()
    api.hotkey()
    assert ctl.wait_for(1)
    api.hotkey()
    assert ctl.wait_for(2) and ctl.calls == ["toggle", "toggle"]
    b.uninstall()


def test_mensagens_alheias_e_outro_id_sao_ignorados():
    api, ctl, b = make("toggle")
    b.install()
    api.other_message()
    api.hotkey(hk_id=999)
    api.hotkey()
    assert ctl.wait_for(1)
    threading.Event().wait(0.05)
    assert ctl.calls == ["toggle"]
    b.uninstall()


def test_set_mode_vale_no_proximo_atalho():
    api, ctl, b = make("hold")
    b.install()
    b.set_mode("toggle")
    api.hotkey()
    assert ctl.wait_for(1) and ctl.calls == ["toggle"]
    b.set_mode("invalido")
    assert b.mode == "toggle"
    b.uninstall()


def test_atalho_com_shift_observa_o_shift():
    api, ctl, b = make("hold", hotkey="ctrl+shift+f9")
    assert b.install()
    assert api.register_calls[0][1] == MOD_CONTROL | MOD_SHIFT | MOD_NOREPEAT
    api.down = {VK_CTRL, VK_SHIFT, KEY_VK["f9"]}
    api.hotkey()
    assert ctl.wait_for(1)
    api.down.discard(VK_SHIFT)
    assert ctl.wait_for(2)
    b.uninstall()


def test_win_aceita_qualquer_lado():
    api, ctl, b = make("hold", hotkey="ctrl+win+k")
    b.install()
    api.down = {VK_CTRL, 0x5C, KEY_VK["k"]}  # Win direito
    api.hotkey()
    assert ctl.wait_for(1)
    threading.Event().wait(0.05)
    assert ctl.calls == ["begin"]
    api.down.discard(0x5C)
    assert ctl.wait_for(2)
    b.uninstall()


# --- esperar soltar as modificadoras antes de colar -----------------------

def test_wait_released_ja_solto_retorna_na_hora():
    sleeps = []
    assert wait_keys_released(["ctrl", "alt"], lambda k: False, sleep=sleeps.append) is True
    assert sleeps == []


def test_wait_released_espera_soltar():
    estado = {"n": 0}

    def is_pressed(k):
        estado["n"] += 1
        return estado["n"] < 6

    t = {"now": 0.0}
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        t["now"] += s

    assert wait_keys_released(["ctrl"], is_pressed, timeout=2.0, sleep=sleep, clock=lambda: t["now"]) is True
    assert 0 < len(sleeps) < 10


def test_wait_released_estoura_o_limite():
    t = {"now": 0.0}

    def sleep(s):
        t["now"] += s

    ok = wait_keys_released(["ctrl"], lambda k: True, timeout=0.5, sleep=sleep, clock=lambda: t["now"])
    assert ok is False
    assert 0.5 <= t["now"] < 0.6


def test_binder_wait_released_usa_as_teclas_do_atalho():
    api = FakeApi()
    api.down = {VK_ALT}
    t = {"now": 0.0}

    def sleep(s):
        t["now"] += s
        if t["now"] > 0.1:
            api.down.clear()

    b = HotkeyBinder("ctrl+alt+space", "hold", FakeController(), api=api, sleep=sleep, clock=lambda: t["now"])
    assert b.key_names == ["ctrl", "alt", "space"]
    assert b.wait_released(timeout=2.0) is True


def test_binder_wait_released_estoura_se_tecla_do_atalho_continua_pressionada():
    api = FakeApi()
    api.down = {VK_SPACE}
    t = {"now": 0.0}

    def sleep(s):
        t["now"] += s

    b = HotkeyBinder("ctrl+alt+space", "hold", FakeController(), api=api, sleep=sleep, clock=lambda: t["now"])
    assert b.wait_released(timeout=0.2) is False
    api.down = {KEY_VK["v"]}  # tecla fora do atalho não conta
    assert b.wait_released(timeout=0.2) is True


# --- gravar novo atalho (record_hotkey) -----------------------------------------------

class ScriptedKeys:
    """Estado das teclas que muda a cada leitura completa (uma 'rodada' por sleep)."""

    def __init__(self, frames):
        self.frames = list(frames)
        self.i = 0

    def is_down(self, vk):
        return vk in self.frames[min(self.i, len(self.frames) - 1)]

    def tick(self, _s):
        self.i += 1


def run_record(frames, **kw):
    keys = ScriptedKeys(frames)
    t = {"now": 0.0}

    def sleep(s):
        keys.tick(s)
        t["now"] += s

    return record_hotkey(kb=keys, sleep=sleep, clock=lambda: t["now"], **kw)


def test_record_captura_combinacao():
    frames = [set(), {VK_CTRL}, {VK_CTRL, VK_ALT}, {VK_CTRL, VK_ALT, KEY_VK["h"]}, set()]
    assert run_record(frames) == "ctrl+alt+h"


def test_record_ignora_tecla_ja_apertada_no_inicio():
    enter = KEY_VK["enter"]
    frames = [{enter}, {enter, VK_CTRL}, {VK_CTRL}, {VK_CTRL, KEY_VK["f9"]}]
    assert run_record(frames) == "ctrl+f9"


def test_record_rejeita_reservada_e_continua():
    motivos = []
    frames = [set(), {VK_ALT}, {VK_ALT, KEY_VK["f4"]}, set(), {VK_CTRL}, {VK_CTRL, VK_ALT, KEY_VK["j"]}]
    assert run_record(frames, on_rejected=motivos.append) == "ctrl+alt+j"
    assert len(motivos) == 1 and "reservad" in motivos[0]


def test_record_rejeita_sem_modificador():
    motivos = []
    frames = [set(), {KEY_VK["a"]}, set()]
    assert run_record(frames, timeout_s=0.2, on_rejected=motivos.append) is None
    assert motivos and "Ctrl" in motivos[0]


def test_record_esc_sozinho_cancela():
    assert run_record([set(), {KEY_VK["esc"]}]) is None


def test_record_tempo_esgotado():
    assert run_record([set()], timeout_s=0.5) is None


def test_record_win_direito():
    frames = [set(), {VK_CTRL, 0x5C}, {VK_CTRL, 0x5C, KEY_VK["k"]}]
    assert run_record(frames) == "ctrl+win+k"


# --- bandeja: ícone e menu (sem abrir pystray) -----------------------------

def test_icone_visivel_e_muda_por_estado():
    from hear_ai.tray import make_icon

    base = make_icon("idle")
    assert base.size == (64, 64) and base.mode == "RGBA"
    assert base.getpixel((4, 32))[3] == 255  # fundo branco opaco (o PNG original é preto/transparente)
    assert make_icon("recording").getpixel((55, 55))[:3] == (220, 38, 38)
    assert make_icon("transcribing").getpixel((55, 55))[:3] == (234, 179, 8)
    assert make_icon("loading").getpixel((55, 55))[:3] == (156, 163, 175)
    assert base.tobytes() != make_icon("recording").tobytes()


def test_tray_alternar_modo_salva_config_e_atualiza_binder(tmp_path):
    from hear_ai.config import Config, load_config
    from hear_ai.tray import TrayApp

    ctl = SimpleNamespace(state="idle")
    cfg = Config()
    b = HotkeyBinder(cfg.hotkey, cfg.mode, FakeController(), kb=FakeKB())
    app = TrayApp(ctl, cfg, on_quit=lambda: None, config_path=tmp_path / "config.json", binder=b, log_dir=tmp_path)
    app.toggle_mode()
    assert cfg.mode == "toggle" and b.mode == "toggle"
    assert load_config(tmp_path / "config.json").mode == "toggle"
    app.toggle_mode()
    assert cfg.mode == "hold"
    assert "carregando" in app.title()
    app.set_loading(False)
    assert "pronto" in app.title()


# --- rodada 1: erro de modelo visível, abrir arquivos protegido ---------------

def test_icone_de_erro_e_tooltip(tmp_path):
    from hear_ai.config import Config
    from hear_ai.tray import make_icon, TrayApp

    assert make_icon("error").getpixel((55, 55))[:3] == (17, 17, 17)
    app = TrayApp(SimpleNamespace(state="idle"), Config(), on_quit=lambda: None, config_path=tmp_path / "c.json", log_dir=tmp_path)
    assert "carregando" in app.title()
    app.set_error()
    assert app.loading is False
    assert "erro" in app.title() and "log" in app.title()
    assert app._visual_state() == "error"
    app.set_loading(False)
    assert app._visual_state() == "error"  # erro não some sozinho


def test_mensagem_de_erro_do_modelo(tmp_path):
    from hear_ai.__main__ import describe_load_error
    from hear_ai.transcriber import ModelNotFoundError, Transcriber

    with __import__("pytest").raises(ModelNotFoundError) as exc:
        Transcriber("small", tmp_path).load()
    msg = describe_load_error(exc.value)
    assert "baixar_modelo.py" in msg
    outra = describe_load_error(RuntimeError("detalhe interno"))
    assert "RuntimeError" in outra and "logs" in outra and "detalhe interno" not in outra


def test_abrir_arquivos_protegido_contra_oserror(tmp_path):
    from hear_ai.config import Config
    from hear_ai.tray import TrayApp

    beeps, avisos = [], []

    class Log:
        def warning(self, fmt, *a):
            avisos.append(fmt % a)

    class Fb:
        def error(self):
            beeps.append(1)

    def opener_ruim(path):
        raise OSError("sem programa associado")

    app = TrayApp(SimpleNamespace(state="idle"), Config(), on_quit=lambda: None,
                  config_path=tmp_path / "c.json", log_dir=tmp_path / "logs",
                  feedback=Fb(), logger=Log(), opener=opener_ruim)
    app.open_settings()
    app.open_logs()
    assert len(beeps) == 2 and len(avisos) == 2
    assert all("OSError" in a for a in avisos)
    assert not any("sem programa" in a for a in avisos)


def test_abrir_arquivos_chama_o_opener(tmp_path):
    from hear_ai.config import Config
    from hear_ai.tray import TrayApp

    abertos = []
    app = TrayApp(SimpleNamespace(state="idle"), Config(), on_quit=lambda: None,
                  config_path=tmp_path / "c.json", log_dir=tmp_path / "logs", opener=abertos.append)
    app.open_settings()
    app.open_logs()
    assert abertos == [str(tmp_path / "c.json"), str(tmp_path / "logs")]
    assert (tmp_path / "c.json").exists() and (tmp_path / "logs").is_dir()


def test_sair_chama_on_quit_mesmo_com_erro_e_para_o_icone():
    from hear_ai.config import Config
    from hear_ai.tray import TrayApp

    ordem = []
    app = TrayApp(SimpleNamespace(state="idle"), Config(), on_quit=lambda: ordem.append("quit"))
    app._icon = SimpleNamespace(stop=lambda: ordem.append("stop"))
    app.quit()
    assert ordem == ["quit", "stop"]

    def quebra():
        raise RuntimeError("x")

    app2 = TrayApp(SimpleNamespace(state="idle"), Config(), on_quit=quebra)
    app2._icon = SimpleNamespace(stop=lambda: ordem.append("stop2"))
    try:
        app2.quit()
    except RuntimeError:
        pass
    assert ordem[-1] == "stop2"


# --- T17: avisos do ditado, conflito do atalho, recuperação -----------------------------

def _app(state="idle", **kw):
    from hear_ai.config import Config
    from hear_ai.tray import TrayApp

    ctl = SimpleNamespace(state=state)
    app = TrayApp(ctl, Config(), on_quit=lambda: None, **kw)
    app.set_loading(False)
    return app, ctl


def test_aviso_saved_aparece_e_some_no_proximo_ditado():
    from hear_ai.tray import make_icon

    app, ctl = _app()
    app.set_state("saved")
    assert "texto salvo — Ctrl+V" in app.title()
    assert app._visual_state() == "saved"
    assert make_icon("saved").getpixel((55, 55))[:3] == (37, 99, 235)
    ctl.state = "recording"
    app.set_state("recording")
    assert "texto salvo" not in app.title() and app._visual_state() == "recording"


def test_aviso_de_erro_e_de_limite():
    app, ctl = _app()
    app.set_state("error")
    assert "erro no último ditado" in app.title()
    ctl.state = "recording"
    app.set_state("recording")
    app.set_state("limit_warning")
    assert "1 min para o limite" in app.title()
    ctl.state = "transcribing"
    app.set_state("transcribing")
    assert "limite" not in app.title()


def test_atalho_em_conflito_fica_visivel():
    app, ctl = _app()
    app.set_conflict("o atalho ctrl+alt+h já está em uso por outro programa")
    assert app._visual_state() == "error"
    assert "Atalho em conflito" in app.title()


def test_recuperar_gravacoes_chama_callback_so_se_houver():
    chamadas = []
    app, ctl = _app(on_recover=lambda: chamadas.append(1))
    app.recover()
    assert chamadas == []
    app.set_orphans(2)
    assert "2 gravação(ões) para recuperar" in app.title()
    app.recover()
    assert chamadas == [1]
    app.set_orphans(0)
    assert "recuperar" not in app.title()


def test_tooltip_nunca_passa_do_limite_do_windows():
    from hear_ai.tray import TOOLTIP_MAX

    app, ctl = _app()
    app.set_orphans(3)
    app.set_state("saved")
    app.cfg.hotkey = "ctrl+alt+shift+win+page down"
    assert len(app.title()) <= TOOLTIP_MAX
    app.set_conflict("x" * 300)
    assert len(app.title()) <= TOOLTIP_MAX


def test_refresh_atualiza_icone_e_menu():
    app, ctl = _app()
    eventos = []
    app._icon = SimpleNamespace(icon=None, title="", update_menu=lambda: eventos.append("menu"))
    app.set_orphans(1)
    assert eventos == ["menu"] and "recuperar" in app._icon.title
