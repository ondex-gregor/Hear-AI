"""Testes para paster.py."""

from __future__ import annotations

import os
import pytest

from hear_ai.paster import PasteResult, Paster, SystemClipboard, send_ctrl_v


class FakeClipboard:
    def __init__(self) -> None:
        self._content: str | None = None
        self._get_side_effect = None
        self._set_side_effect = None

    def get(self) -> str | None:
        if self._get_side_effect:
            return self._get_side_effect()
        return self._content

    def set(self, text: str) -> None:
        if self._set_side_effect:
            self._set_side_effect(text)
        else:
            self._content = text


class FakeSendPaste:
    """Fake que registra eventos na ordem: set, send, sleep, restore."""

    def __init__(self, clipboard: FakeClipboard) -> None:
        self.clipboard = clipboard
        self.events: list[str] = []
        self.sleep_called_with: float | None = None
        self.fail_on_send = False

    def __call__(self) -> None:
        if self.fail_on_send:
            raise RuntimeError("send_paste falhou")
        # Registra o conteúdo do clipboard NO MOMENTO da colagem
        content = self.clipboard.get()
        self.events.append(f"send:{content}")

    def record_set(self, text: str) -> None:
        self.events.append(f"set:{text}")

    def record_sleep(self, delay: float) -> None:
        self.events.append(f"sleep:{delay}")


def test_paste_ordem_eventos_set_send_sleep_restore():
    """Verifica a ordem exata: set(novo) → send → sleep(delay) → set(anterior)."""
    clipboard = FakeClipboard()
    clipboard.set("conteúdo anterior")
    send_paste = FakeSendPaste(clipboard)

    def sleep_fake(delay: float) -> None:
        send_paste.record_sleep(delay)

    # Wrap clipboard.set para registrar
    original_set = clipboard.set
    def set_wrap(text: str) -> None:
        send_paste.record_set(text)
        original_set(text)
    clipboard.set = set_wrap  # type: ignore

    # delay_s=0.15 (base_s), chars_per_s=2500, max_s=3.0
    # texto "novo texto" = 10 chars -> delay = 0.15 + 10/2500 = 0.154s
    paster = Paster(clipboard, send_paste, delay_s=0.15, chars_per_s=2500, max_s=3.0, sleep=sleep_fake)
    result = paster.paste("novo texto")

    assert result == PasteResult(pasted=True, restored=True, reason="ok")
    assert send_paste.events == [
        "set:novo texto",
        "send:novo texto",
        "sleep:0.154",
        "set:conteúdo anterior",
    ]


def test_paste_restore_delay_proporcional_ao_tamanho():
    """Delay de restauração aumenta com o tamanho do texto."""
    clipboard = FakeClipboard()
    clipboard.set("conteúdo anterior")
    send_paste = FakeSendPaste(clipboard)

    def sleep_fake(delay: float) -> None:
        send_paste.record_sleep(delay)

    original_set = clipboard.set
    def set_wrap(text: str) -> None:
        send_paste.record_set(text)
        original_set(text)
    clipboard.set = set_wrap  # type: ignore

    # 5000 chars: delay = 0.25 + 5000/2500 = 2.25s (clamp max=3.0)
    paster = Paster(clipboard, send_paste, delay_s=0.25, chars_per_s=2500, max_s=3.0, sleep=sleep_fake)
    result = paster.paste("x" * 5000)

    assert result == PasteResult(pasted=True, restored=True, reason="ok")
    assert send_paste.events[2] == "sleep:2.25"


def test_paste_restore_delay_max_limit():
    """Delay de restauração não excede max_s."""
    clipboard = FakeClipboard()
    clipboard.set("conteúdo anterior")
    send_paste = FakeSendPaste(clipboard)

    def sleep_fake(delay: float) -> None:
        send_paste.record_sleep(delay)

    original_set = clipboard.set
    def set_wrap(text: str) -> None:
        send_paste.record_set(text)
        original_set(text)
    clipboard.set = set_wrap  # type: ignore

    # 10000 chars: delay = 0.25 + 10000/2500 = 4.25s -> clamp to 3.0
    paster = Paster(clipboard, send_paste, delay_s=0.25, chars_per_s=2500, max_s=3.0, sleep=sleep_fake)
    result = paster.paste("x" * 10000)

    assert result == PasteResult(pasted=True, restored=True, reason="ok")
    assert send_paste.events[2] == "sleep:3.0"


def test_paste_send_failed_deixa_texto_no_clipboard():
    """Se send_paste falhar, texto fica no clipboard e não restaura."""
    clipboard = FakeClipboard()
    clipboard.set("conteúdo anterior")
    send_paste = FakeSendPaste(clipboard)
    send_paste.fail_on_send = True

    def sleep_fake(delay: float) -> None:
        send_paste.record_sleep(delay)

    original_set = clipboard.set
    def set_wrap(text: str) -> None:
        send_paste.record_set(text)
        original_set(text)
    clipboard.set = set_wrap  # type: ignore

    paster = Paster(clipboard, send_paste, delay_s=0.15, chars_per_s=2500, max_s=3.0, sleep=sleep_fake)
    result = paster.paste("novo texto")

    assert result == PasteResult(pasted=False, restored=False, reason="send_failed", in_clipboard=True)
    # Texto ditado permanece no clipboard
    assert clipboard.get() == "novo texto"
    # Não tentou restaurar
    assert "set:conteúdo anterior" not in send_paste.events


def test_paste_clipboard_mudou_nao_restaura():
    """Se clipboard mudou durante a espera, não restaura (não apaga cópia nova do usuário)."""
    clipboard = FakeClipboard()
    clipboard.set("conteúdo anterior")
    send_paste = FakeSendPaste(clipboard)

    def sleep_fake(delay: float) -> None:
        send_paste.record_sleep(delay)
        # Simula que outro programa copiou algo durante o delay
        clipboard._content = "copia do usuario"

    original_set = clipboard.set
    def set_wrap(text: str) -> None:
        send_paste.record_set(text)
        original_set(text)
    clipboard.set = set_wrap  # type: ignore

    paster = Paster(clipboard, send_paste, delay_s=0.15, chars_per_s=2500, max_s=3.0, sleep=sleep_fake)
    result = paster.paste("novo texto")

    assert result == PasteResult(pasted=True, restored=False, reason="clipboard_changed")
    # Cópia do usuário permanece
    assert clipboard.get() == "copia do usuario"
    # Não restaurou
    assert "set:conteúdo anterior" not in send_paste.events


def test_paste_clipboard_mudou_com_crlf_normaliza_e_restaura():
    """Se clipboard mudou mas só por \r\n vs \n, normaliza e restaura normalmente."""
    clipboard = FakeClipboard()
    clipboard.set("conteúdo anterior")
    send_paste = FakeSendPaste(clipboard)

    def sleep_fake(delay: float) -> None:
        send_paste.record_sleep(delay)
        # Simula que o clipboard devolveu com \r\n (Windows) mas o texto original era \n
        clipboard._content = "novo texto\r\n"

    original_set = clipboard.set
    def set_wrap(text: str) -> None:
        send_paste.record_set(text)
        original_set(text)
    clipboard.set = set_wrap  # type: ignore

    paster = Paster(clipboard, send_paste, delay_s=0.15, chars_per_s=2500, max_s=3.0, sleep=sleep_fake)
    result = paster.paste("novo texto\n")

    # Normalização: \r\n -> \n, strip() -> igual
    assert result == PasteResult(pasted=True, restored=True, reason="ok")
    assert "set:conteúdo anterior" in send_paste.events[-1]


def test_paste_texto_vazio_nao_faz_nada():
    clipboard = FakeClipboard()
    clipboard.set("conteúdo anterior")
    send_paste = FakeSendPaste(clipboard)
    paster = Paster(clipboard, send_paste, delay_s=0.0, sleep=lambda _: None)

    result = paster.paste("")
    assert result == PasteResult(pasted=False, restored=False, reason="empty")
    assert clipboard.get() == "conteúdo anterior"

    result = paster.paste("   ")
    assert result == PasteResult(pasted=False, restored=False, reason="empty")
    assert clipboard.get() == "conteúdo anterior"

    result = paster.paste("\t\n")
    assert result == PasteResult(pasted=False, restored=False, reason="empty")
    assert clipboard.get() == "conteúdo anterior"


def test_paste_restore_false_nao_restaura():
    clipboard = FakeClipboard()
    clipboard.set("conteúdo anterior")
    send_paste = FakeSendPaste(clipboard)

    def sleep_fake(delay: float) -> None:
        send_paste.record_sleep(delay)

    original_set = clipboard.set
    def set_wrap(text: str) -> None:
        send_paste.record_set(text)
        original_set(text)
    clipboard.set = set_wrap  # type: ignore

    paster = Paster(clipboard, send_paste, delay_s=0.0, restore=False, sleep=sleep_fake)
    result = paster.paste("novo texto")

    assert result == PasteResult(pasted=True, restored=False, reason="ok", in_clipboard=True)
    # base 0: vale só a parte proporcional (10 caracteres / 2500 = 0,004 s) — revisão 014 item 1
    assert send_paste.events == ["set:novo texto", "send:novo texto", "sleep:0.004"]
    assert clipboard.get() == "novo texto"


def test_paste_sem_conteudo_anterior_nao_falha():
    """Clipboard vazio (None): cola sem restaurar."""
    clipboard = FakeClipboard()  # None por padrão
    send_paste = FakeSendPaste(clipboard)

    def sleep_fake(delay: float) -> None:
        send_paste.record_sleep(delay)

    original_set = clipboard.set
    def set_wrap(text: str) -> None:
        send_paste.record_set(text)
        original_set(text)
    clipboard.set = set_wrap  # type: ignore

    paster = Paster(clipboard, send_paste, delay_s=0.0, sleep=sleep_fake)
    result = paster.paste("novo texto")

    assert result == PasteResult(pasted=True, restored=False, reason="ok", in_clipboard=True)
    assert send_paste.events[:2] == ["set:novo texto", "send:novo texto"]
    assert send_paste.events[2].startswith("sleep:0.")
    assert clipboard.get() == "novo texto"


def test_paste_clipboard_vazio_string_restaura_string_vazia():
    """pyperclip devolve '' para conteúdo não-texto; deve restaurar ''."""
    clipboard = FakeClipboard()
    clipboard.set("")  # string vazia, não None
    send_paste = FakeSendPaste(clipboard)

    def sleep_fake(delay: float) -> None:
        send_paste.record_sleep(delay)

    original_set = clipboard.set
    def set_wrap(text: str) -> None:
        send_paste.record_set(text)
        original_set(text)
    clipboard.set = set_wrap  # type: ignore

    paster = Paster(clipboard, send_paste, delay_s=0.0, sleep=sleep_fake)
    result = paster.paste("novo texto")

    assert result == PasteResult(pasted=True, restored=True, reason="ok")
    assert send_paste.events[-1] == "set:"
    assert clipboard.get() == ""


def test_paste_get_falha_cola_sem_restaurar(caplog):
    """Se get() falhar, cola sem restaurar e loga warning."""
    clipboard = FakeClipboard()
    clipboard.set("conteúdo anterior")

    call_count = 0
    def get_falha() -> str | None:
        nonlocal call_count
        call_count += 1
        if call_count <= 3:
            raise RuntimeError("clipboard travado")
        return clipboard._content

    clipboard._get_side_effect = get_falha

    send_paste = FakeSendPaste(clipboard)

    def sleep_fake(delay: float) -> None:
        send_paste.record_sleep(delay)

    original_set = clipboard.set
    def set_wrap(text: str) -> None:
        send_paste.record_set(text)
        original_set(text)
    clipboard.set = set_wrap  # type: ignore

    paster = Paster(clipboard, send_paste, delay_s=0.0, sleep=sleep_fake)

    with caplog.at_level("WARNING"):
        result = paster.paste("novo texto")

    assert result == PasteResult(pasted=True, restored=False, reason="clipboard_busy", in_clipboard=True)
    assert "falha ao restaurar" not in caplog.text
    assert "colando sem restaurar" in caplog.text
    assert clipboard.get() == "novo texto"


def test_paste_restaurar_falha_devolve_restore_failed(caplog):
    """Colou, mas pôr de volta o conteúdo anterior falhou: restore_failed, texto ditado continua lá."""
    clipboard = FakeClipboard()
    clipboard.set("conteúdo anterior")

    # send_paste succeeds, but restore fails
    def send_paste_ok():
        pass

    def set_falha(text: str) -> None:
        if text == "conteúdo anterior":
            raise RuntimeError("erro no restore")
        clipboard._content = text

    clipboard._set_side_effect = set_falha

    send_paste = FakeSendPaste(clipboard)

    def sleep_fake(delay: float) -> None:
        send_paste.record_sleep(delay)

    paster = Paster(clipboard, send_paste_ok, delay_s=0.0, sleep=sleep_fake)

    with caplog.at_level("WARNING"):
        result = paster.paste("novo texto")

    assert result == PasteResult(pasted=True, restored=False, reason="restore_failed", in_clipboard=True)
    assert "falha ao restaurar" in caplog.text


def test_paste_compatibilidade_delay_s_alias_base_s():
    """delay_s continua funcionando como alias de base_s."""
    clipboard = FakeClipboard()
    clipboard.set("conteúdo anterior")
    send_paste = FakeSendPaste(clipboard)

    def sleep_fake(delay: float) -> None:
        send_paste.record_sleep(delay)

    original_set = clipboard.set
    def set_wrap(text: str) -> None:
        send_paste.record_set(text)
        original_set(text)
    clipboard.set = set_wrap  # type: ignore

    # Usando delay_s antigo (sem base_s explícito)
    # "novo texto" = 10 chars -> delay = 0.2 + 10/2500 = 0.204
    paster = Paster(clipboard, send_paste, delay_s=0.2, chars_per_s=2500, max_s=3.0, sleep=sleep_fake)
    result = paster.paste("novo texto")

    assert result == PasteResult(pasted=True, restored=True, reason="ok")
    assert send_paste.events[2] == "sleep:0.204"


def test_paste_base_s_explicito_sobrepoe_delay_s():
    """base_s explícito tem prioridade sobre delay_s."""
    clipboard = FakeClipboard()
    clipboard.set("conteúdo anterior")
    send_paste = FakeSendPaste(clipboard)

    def sleep_fake(delay: float) -> None:
        send_paste.record_sleep(delay)

    original_set = clipboard.set
    def set_wrap(text: str) -> None:
        send_paste.record_set(text)
        original_set(text)
    clipboard.set = set_wrap  # type: ignore

    # delay_s=0.5 mas base_s=0.1 -> usa base_s=0.1
    # "novo texto" = 10 chars -> delay = 0.1 + 10/2500 = 0.104
    paster = Paster(clipboard, send_paste, delay_s=0.5, base_s=0.1, chars_per_s=2500, max_s=3.0, sleep=sleep_fake)
    result = paster.paste("novo texto")

    assert result == PasteResult(pasted=True, restored=True, reason="ok")
    assert send_paste.events[2] == "sleep:0.104"


# Testes dos backends reais (exigem pyperclip/keyboard instalados)
# Rodam APENAS com variável de ambiente HEAR_AI_REAL_IO=1
# para evitar acesso real à área de transferência/teclado durante pytest normal.

def _skip_unless_real_io() -> None:
    if os.environ.get("HEAR_AI_REAL_IO") != "1":
        pytest.skip("HEAR_AI_REAL_IO=1 não definido; pulando teste real")


@pytest.mark.slow
def test_system_clipboard_real():
    _skip_unless_real_io()
    try:
        cb = SystemClipboard()
        # Salva e restaura conteúdo anterior para não poluir clipboard do usuário
        old = cb.get()
        try:
            cb.set("teste real")
            assert cb.get() == "teste real"
        finally:
            if old is not None:
                cb.set(old)
    except Exception as e:
        pytest.skip(f"pyperclip não funcional: {e}")


@pytest.mark.slow
def test_send_ctrl_v_real():
    _skip_unless_real_io()
    try:
        send_ctrl_v()  # Apenas verifica se import funciona
    except Exception as e:
        pytest.skip(f"keyboard não funcional: {e}")

# --- rodada 1 (revisão 014) ----------------------------------------------------------------

import threading  # noqa: E402

SEGREDO = "senha secreta ditada 123"


def _paster(clipboard, send=None, sleeps=None, **kw):
    sleeps = sleeps if sleeps is not None else []
    send = send or (lambda: None)
    kw.setdefault("delay_s", 0.25)
    return Paster(clipboard, send, sleep=sleeps.append, **kw), sleeps


def test_base_zero_mantem_a_parte_proporcional():
    """Item 1: base 0 não pode zerar a espera (era o bug A1(b) de volta)."""
    clip = FakeClipboard()
    clip.set("anterior")
    paster, sleeps = _paster(clip, base_s=0.0, chars_per_s=2500, max_s=3.0)
    assert paster.paste("x" * 5000).restored
    assert sleeps == [2.0]


def test_espera_perto_da_base_e_max_menor_que_base():
    clip = FakeClipboard()
    clip.set("anterior")
    paster, sleeps = _paster(clip, base_s=0.25, chars_per_s=2500, max_s=3.0)
    paster.paste("x" * 1500)
    assert sleeps == [0.85]  # 0,25 + 1.500/2.500
    paster2, sleeps2 = _paster(clip, base_s=0.5, chars_per_s=2500, max_s=0.1)
    paster2.paste("x" * 5000)
    assert sleeps2 == [0.5]  # teto menor que a base: vale a base


def test_chars_per_s_invalido_falha_no_construtor():
    """Item 7: nada de ZeroDivisionError depois do Ctrl+V."""
    for ruim in (0, -10):
        with pytest.raises(ValueError):
            Paster(FakeClipboard(), lambda: None, chars_per_s=ruim)


def test_set_falhando_texto_nao_esta_na_area_de_transferencia(caplog):
    """Item 2: clipboard travado no set → clipboard_busy, pasted=False, in_clipboard=False."""
    clip = FakeClipboard()
    clip.set("anterior")
    enviados = []

    def set_falha(text):
        raise RuntimeError(f"travado com {text}")

    clip._set_side_effect = set_falha
    paster, sleeps = _paster(clip, send=lambda: enviados.append(1))
    with caplog.at_level("DEBUG", logger="hear_ai.paster"):
        r = paster.paste(SEGREDO)
    assert r == PasteResult(pasted=False, restored=False, reason="clipboard_busy", in_clipboard=False)
    assert enviados == []  # sem texto na área de transferência, nem tenta o Ctrl+V
    assert clip.get() == "anterior"
    assert SEGREDO not in caplog.text


def test_send_falhando_texto_fica_na_area_de_transferencia():
    clip = FakeClipboard()
    clip.set("anterior")

    def send():
        raise OSError("sem foco")

    paster, sleeps = _paster(clip, send=send)
    r = paster.paste(SEGREDO)
    assert r.reason == "send_failed" and r.in_clipboard is True and r.pasted is False
    assert clip.get() == SEGREDO and sleeps == []


def _troca_no_sleep(clip, valor):
    def sleep(_d):
        clip._content = valor
    return sleep


def test_crlf_no_meio_do_texto_restaura_nos_dois_sentidos():
    """Item 3: quebra de linha NO MEIO; o strip() sozinho não resolveria."""
    casos = [("linha 1\nlinha 2", "linha 1\r\nlinha 2"), ("linha 1\r\nlinha 2", "linha 1\nlinha 2"),
             ("a\rb", "a\nb")]
    for colado, devolvido in casos:
        clip = FakeClipboard()
        clip.set("anterior")
        paster = Paster(clip, lambda: None, delay_s=0.0, sleep=_troca_no_sleep(clip, devolvido))
        r = paster.paste(colado)
        assert r == PasteResult(pasted=True, restored=True, reason="ok", in_clipboard=False), (colado, devolvido)
        assert clip.get() == "anterior"


def test_quebra_de_linha_diferente_de_verdade_conta_como_mudanca():
    clip = FakeClipboard()
    clip.set("anterior")
    paster = Paster(clip, lambda: None, delay_s=0.0, sleep=_troca_no_sleep(clip, "linha 1 linha 2"))
    r = paster.paste("linha 1\nlinha 2")
    assert r.reason == "clipboard_changed" and not r.restored


def test_get_da_verificacao_falhando_nao_restaura_e_diz_busy():
    """Item 4: não dá para saber se o usuário copiou algo → não restaura, motivo clipboard_busy."""
    clip = FakeClipboard()
    clip.set("anterior")
    chamadas = {"n": 0}

    def get():
        chamadas["n"] += 1
        if chamadas["n"] >= 2:  # a 1ª leitura (conteúdo anterior) funciona; a da verificação, não
            raise RuntimeError("travado")
        return clip._content

    clip._get_side_effect = get
    paster, _ = _paster(clip)
    r = paster.paste("novo")
    assert r == PasteResult(pasted=True, restored=False, reason="clipboard_busy", in_clipboard=False)


def test_retry_do_get_usa_o_sleep_injetado():
    """Item 6: nada de time.sleep real; as pausas do retry passam pelo sleep injetado."""
    clip = FakeClipboard()
    clip.set("anterior")
    falhas = {"n": 0}

    def get():
        if falhas["n"] < 2:
            falhas["n"] += 1
            raise RuntimeError("travado")
        return clip._content

    clip._get_side_effect = get
    paster, sleeps = _paster(clip, base_s=0.25)
    paster.paste("novo")
    assert sleeps[:2] == [0.02, 0.02] and sleeps[2] == 0.252


def test_nenhum_texto_no_log_em_todos_os_ramos_de_falha(caplog):
    """Item 8: falha no get, no set, no Ctrl+V, na verificação e na restauração — sem o texto."""
    def cenario(get_falha=False, set_falha=False, send_falha=False, restore_falha=False):
        clip = FakeClipboard()
        clip.set("anterior")
        if get_falha:
            def get():
                raise RuntimeError(f"get {SEGREDO}")
            clip._get_side_effect = get
        if set_falha or restore_falha:
            def st(text):
                if set_falha or text == "anterior":
                    raise RuntimeError(f"set {SEGREDO}")
                clip._content = text
            clip._set_side_effect = st

        def send():
            if send_falha:
                raise RuntimeError(f"send {SEGREDO}")

        Paster(clip, send, delay_s=0.0, sleep=lambda _d: None).paste(SEGREDO)

    with caplog.at_level("DEBUG", logger="hear_ai.paster"):
        for kw in (dict(get_falha=True), dict(set_falha=True), dict(send_falha=True), dict(restore_falha=True)):
            cenario(**kw)
    assert caplog.text  # os ramos logaram algo
    assert SEGREDO not in caplog.text


def test_lock_duas_colagens_nao_se_misturam():
    """Item 8: a 2ª colagem não pode ler como 'anterior' o texto da 1ª."""
    clip = FakeClipboard()
    clip.set("original do usuario")
    dentro = threading.Event()
    soltar = threading.Event()

    def sleep(_d):
        if not dentro.is_set():
            dentro.set()
            soltar.wait(2)  # a 1ª colagem fica parada no meio da espera

    paster = Paster(clip, lambda: None, delay_s=0.1, sleep=sleep)
    r1 = {}
    t1 = threading.Thread(target=lambda: r1.setdefault("r", paster.paste("primeiro")))
    t1.start()
    assert dentro.wait(2)
    t2 = threading.Thread(target=lambda: r1.setdefault("r2", paster.paste("segundo")))
    t2.start()
    t2.join(0.2)
    assert t2.is_alive()  # esperando o lock
    soltar.set()
    t1.join(2)
    t2.join(2)
    assert r1["r"].restored and r1["r2"].restored
    assert clip.get() == "original do usuario"
