"""Seção "Modo de operação" das Configurações (T47b P4).

Modo A (fiel, padrão) ou Modo B (gerador de prompts). No Modo B: provedor (Ollama local ou nuvem), o ESTADO HONESTO do
provedor ("encontrado, modelos: …" ou "indisponível: gerador local não encontrado" + o que fazer), contexto do
aplicativo, preferências permanentes, atalho do modo e tempo máximo. Nuvem: URL, modelo e o NOME da variável de
ambiente da chave (a chave nunca é digitada aqui) e o CONSENTIMENTO, pedido DENTRO da janela (nunca MessageBox).

A consulta ao Ollama (127.0.0.1) roda numa thread (`status_fn`, injetável nos testes): nunca trava a interface.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSpinBox,
                               QVBoxLayout, QWidget)

from hear_ai import modos
from hear_ai.hotkey import validate_hotkey

MODE_ITEMS = [(modos.MODE_FIEL, "Transcrição fiel (Modo A, padrão)"), (modos.MODE_PROMPT, "Gerador de prompts (Modo B)")]
PROVIDER_ITEMS = [("ollama", "Ollama (local, nada sai do PC)"),
                  ("openai_compat", "Nuvem (compatível com OpenAI)")]
MODE_HINTS = {
    modos.MODE_FIEL: "Transcreve localmente e cola a sua fala como foi dita. Nada sai do computador.",
    modos.MODE_PROMPT: ("Transcreve fielmente (localmente) e um modelo reescreve a fala como um prompt bem "
                        "estruturado. Se o gerador falhar, a transcrição fiel é colada."),
}


class _Signals(QObject):
    status = Signal(int, object)  # (token, ProviderStatus)


class ModoPanel(QWidget):
    def __init__(self, parent=None, status_fn=None, run_async: bool = True):
        super().__init__(parent)
        self._status_fn = status_fn or modos.provider_status
        self._run_async = run_async
        self._sig = _Signals()
        self._sig.status.connect(self._on_status)
        self._token = 0
        self._ollama_url = "http://127.0.0.1:11434"
        self._consent = False
        self._consent_host = ""
        self.status: modos.ProviderStatus | None = None
        self.errors: dict[str, QLabel] = {}
        self._build()

    # --- montagem ----------------------------------------------------------------------------------------
    @staticmethod
    def _shrinkable(combo: QComboBox) -> QComboBox:
        """Sem isto o QComboBox exige como largura mínima o texto do item mais longo e empurra o cartão para além
        da janela (o QScrollArea não rola na horizontal): o texto longo passa a ser cortado com "…"."""
        combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        combo.setMinimumContentsLength(14)
        return combo

    @classmethod
    def _combo(cls, items) -> QComboBox:
        c = cls._shrinkable(QComboBox())
        for value, text in items:
            c.addItem(text, value)
        return c

    def _label(self, text="", name=None, wrap=True) -> QLabel:
        lab = QLabel(text)
        if name:
            lab.setObjectName(name)
        lab.setWordWrap(wrap)
        return lab

    def _row(self, lay, text, widget, err_field=None) -> None:
        row = QHBoxLayout()
        row.setSpacing(12)
        lab = QLabel(text)
        lab.setMinimumWidth(130)
        row.addWidget(lab)
        row.addWidget(widget, 1)
        lay.addLayout(row)
        if err_field:
            self._error(lay, err_field)

    def _error(self, lay, field: str) -> None:
        err = self._label(name="error")
        err.hide()
        lay.addWidget(err)
        self.errors[field] = err

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)

        self.mode_combo = self._combo(MODE_ITEMS)
        self._row(root, "Operação", self.mode_combo, "operation_mode")
        self.mode_hint = self._label(name="muted")
        root.addWidget(self.mode_hint)

        self.prompt_box = QWidget()  # tudo do Modo B
        pb = QVBoxLayout(self.prompt_box)
        pb.setContentsMargins(0, 0, 0, 0)
        pb.setSpacing(8)
        self.provider_combo = self._combo(PROVIDER_ITEMS)
        self._row(pb, "Gerador", self.provider_combo, "generator_provider")

        # Ollama (local)
        self.ollama_box = QWidget()
        ob = QVBoxLayout(self.ollama_box)
        ob.setContentsMargins(0, 0, 0, 0)
        ob.setSpacing(8)
        self.ollama_model = self._shrinkable(QComboBox())
        self._row(ob, "Modelo do Ollama", self.ollama_model, "ollama_model")
        pb.addWidget(self.ollama_box)

        # nuvem (opcional)
        self.cloud_box = QWidget()
        cb = QVBoxLayout(self.cloud_box)
        cb.setContentsMargins(0, 0, 0, 0)
        cb.setSpacing(8)
        self.cloud_url = QLineEdit()
        self.cloud_url.setPlaceholderText("https://api.exemplo.com/v1")
        self._row(cb, "Endereço (URL)", self.cloud_url, "cloud_base_url")
        self.cloud_model = QLineEdit()
        self._row(cb, "Modelo", self.cloud_model, "cloud_model")
        self.cloud_env = QLineEdit()
        self.cloud_env.setPlaceholderText("NOME_DA_VARIAVEL")
        self._row(cb, "Variável da chave", self.cloud_env, "cloud_key_env")
        cb.addWidget(self._label("Digite só o NOME da variável de ambiente que guarda a chave (a chave nunca é "
                                 "guardada pelo Hear AI).", "muted"))
        self.consent_label = self._label()
        cb.addWidget(self.consent_label)
        self.consent_btn = QPushButton("Ler e aceitar…")
        self.consent_btn.setCursor(Qt.PointingHandCursor)
        self.consent_btn.clicked.connect(self._ask_consent)
        cb.addWidget(self.consent_btn, 0, Qt.AlignLeft)
        self.consent_box = QFrame()  # confirmação DENTRO da janela
        self.consent_box.setObjectName("model_box")
        self.consent_box.setAttribute(Qt.WA_StyledBackground, True)
        cv = QVBoxLayout(self.consent_box)
        cv.setContentsMargins(12, 10, 12, 10)
        self.consent_text = self._label()
        cv.addWidget(self.consent_text)
        crow = QHBoxLayout()
        self.consent_yes = QPushButton("Aceito")
        self.consent_yes.setObjectName("primary")
        self.consent_yes.clicked.connect(self._accept_consent)
        self.consent_no = QPushButton("Não aceito")
        self.consent_no.setObjectName("ghost")
        self.consent_no.clicked.connect(self._refuse_consent)
        crow.addWidget(self.consent_yes)
        crow.addWidget(self.consent_no)
        crow.addStretch(1)
        cv.addLayout(crow)
        self.consent_box.hide()
        cb.addWidget(self.consent_box)
        self._error(cb, "cloud_consent")
        pb.addWidget(self.cloud_box)

        # estado honesto do provedor
        self.status_box = QFrame()
        self.status_box.setObjectName("model_box")
        self.status_box.setAttribute(Qt.WA_StyledBackground, True)
        sv = QVBoxLayout(self.status_box)
        sv.setContentsMargins(12, 10, 12, 10)
        sv.setSpacing(6)
        self.status_label = self._label()
        sv.addWidget(self.status_label)
        self.instructions = self._label(name="muted")
        sv.addWidget(self.instructions)
        self.check_btn = QPushButton("Verificar de novo")
        self.check_btn.setCursor(Qt.PointingHandCursor)
        self.check_btn.clicked.connect(self.refresh_status)
        sv.addWidget(self.check_btn, 0, Qt.AlignLeft)
        pb.addWidget(self.status_box)
        self.warn_label = self._label(modos.NO_PROVIDER_WARNING, "warn")
        pb.addWidget(self.warn_label)

        self.app_ctx = QCheckBox("Usar o nome do aplicativo ativo como contexto")
        pb.addWidget(self.app_ctx)
        pb.addWidget(self._label("Só o nome do programa (ex.: Code), nunca o título da janela.", "muted"))
        self.prefs = QCheckBox("Usar minhas preferências permanentes")
        pb.addWidget(self.prefs)
        pb.addWidget(self._label("Lê as linhas “- …” de preferencias-permanentes.md (na pasta do config.json).",
                                 "muted"))
        self.timeout = QSpinBox()
        self.timeout.setRange(5, 300)
        self.timeout.setSuffix(" s")
        self.timeout.lineEdit().setTextMargins(9, 0, 0, 0)
        self._row(pb, "Tempo máximo", self.timeout, "generator_timeout_s")
        root.addWidget(self.prompt_box)

        self.hotkey = QLineEdit()
        self.hotkey.setPlaceholderText("ctrl+alt+m")
        self._row(root, "Atalho para alternar", self.hotkey, "mode_hotkey")
        root.addWidget(self._label("Aperte este atalho em qualquer programa para alternar entre os modos; a bolha "
                                   "mostra o modo novo por um instante.", "muted"))

        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        self.provider_combo.currentIndexChanged.connect(self._on_provider_changed)
        self.cloud_url.textChanged.connect(lambda _t: self._update_consent())
        # o estado da nuvem vem só do que está preenchido: reverifica (sem rede) pouco depois de digitar
        self._recheck = QTimer(self)
        self._recheck.setSingleShot(True)
        self._recheck.setInterval(400)
        self._recheck.timeout.connect(self.refresh_status)
        for edit in (self.cloud_url, self.cloud_model, self.cloud_env):
            edit.textChanged.connect(lambda _t: self._schedule_recheck())

    # --- carregar / coletar ------------------------------------------------------------------------------
    def load(self, cfg) -> None:
        for c in (self.mode_combo, self.provider_combo):
            c.blockSignals(True)
        self._select(self.mode_combo, cfg.operation_mode)
        self._select(self.provider_combo, cfg.generator_provider)
        for c in (self.mode_combo, self.provider_combo):
            c.blockSignals(False)
        self._ollama_url = cfg.ollama_url
        self._set_models((), cfg.ollama_model)
        self.cloud_url.setText(cfg.cloud_base_url)
        self.cloud_model.setText(cfg.cloud_model)
        self.cloud_env.setText(cfg.cloud_key_env)
        # config antigo (aceite sem host) ou aceite de outro host = PENDENTE: a janela pede o aceite de novo
        ok = modos.consent_ok(cfg)
        self._consent, self._consent_host = ok, (modos.host_of(cfg.cloud_base_url) if ok else "")
        self.consent_box.hide()
        self.app_ctx.setChecked(bool(cfg.generator_app_context))
        self.prefs.setChecked(bool(cfg.generator_permanent_prefs))
        self.hotkey.setText(cfg.mode_hotkey)
        self.timeout.setValue(int(cfg.generator_timeout_s))
        for lab in self.errors.values():
            lab.clear()
            lab.hide()
        self.status = None
        self._refresh_visibility()
        if cfg.operation_mode == modos.MODE_PROMPT:
            self.refresh_status()

    def set_mode(self, mode: str) -> None:
        """O modo foi alterado por fora (bandeja/atalho): a janela aberta acompanha."""
        self.mode_combo.blockSignals(True)
        self._select(self.mode_combo, mode)
        self.mode_combo.blockSignals(False)
        self._refresh_visibility()
        if mode == modos.MODE_PROMPT and self.status is None:
            self.refresh_status()

    @staticmethod
    def _select(combo: QComboBox, value) -> None:
        i = combo.findData(value)
        if i >= 0:
            combo.setCurrentIndex(i)

    def _host_now(self) -> str:
        return modos.host_of(self.cloud_url.text().strip())

    def consent_effective(self) -> bool:
        """O aceite vale só para o provedor (host) para o qual foi dado (e nunca sem host)."""
        host = self._host_now()
        return self._consent and bool(host) and host == self._consent_host

    def values(self) -> dict:
        ok, canon = validate_hotkey(self.hotkey.text().strip())
        return {
            "operation_mode": self.mode_combo.currentData(),
            "generator_provider": self.provider_combo.currentData(),
            "ollama_url": self._ollama_url,
            "ollama_model": self.ollama_model.currentData() or "",
            "cloud_base_url": self.cloud_url.text().strip(),
            "cloud_model": self.cloud_model.text().strip(),
            "cloud_key_env": self.cloud_env.text().strip(),
            "cloud_consent": self.consent_effective(),
            "cloud_consent_host": self._host_now() if self.consent_effective() else "",
            "generator_app_context": self.app_ctx.isChecked(),
            "generator_permanent_prefs": self.prefs.isChecked(),
            "mode_hotkey": canon if ok else self.hotkey.text().strip(),
            "generator_timeout_s": self.timeout.value(),
        }

    def needs_consent(self) -> bool:
        v = self.values()
        return (v["operation_mode"] == modos.MODE_PROMPT and v["generator_provider"] == "openai_compat"
                and not v["cloud_consent"])

    # --- visibilidade ------------------------------------------------------------------------------------
    def _refresh_visibility(self) -> None:
        mode = self.mode_combo.currentData()
        self.mode_hint.setText(MODE_HINTS.get(mode, ""))
        prompt = mode == modos.MODE_PROMPT
        self.prompt_box.setVisible(prompt)
        cloud = self.provider_combo.currentData() == "openai_compat"
        self.ollama_box.setVisible(not cloud)
        self.cloud_box.setVisible(cloud)
        self._update_consent()
        self._render_status()

    def _schedule_recheck(self) -> None:
        if (self.mode_combo.currentData() == modos.MODE_PROMPT
                and self.provider_combo.currentData() == "openai_compat"):
            self._recheck.start()

    def _on_mode_changed(self, _i) -> None:
        self._refresh_visibility()
        if self.mode_combo.currentData() == modos.MODE_PROMPT:
            self.refresh_status()

    def _on_provider_changed(self, _i) -> None:
        self.status = None
        self._refresh_visibility()
        self.refresh_status()

    # --- consentimento da nuvem (dentro da janela) -----------------------------------------------------------
    def _update_consent(self) -> None:
        if not hasattr(self, "consent_label"):
            return
        host = self._host_now() or "o provedor"
        if self.consent_effective():
            self.consent_label.setText(f"Consentimento: dado para {host}.")
            self.consent_label.setObjectName("ok")
            self.consent_btn.setText("Rever o aviso…")
        else:
            self.consent_label.setText("Consentimento: ainda não dado. Sem ele, o Modo B cola a transcrição.")
            self.consent_label.setObjectName("warn")
            self.consent_btn.setText("Ler e aceitar…")
        self.consent_label.style().unpolish(self.consent_label)
        self.consent_label.style().polish(self.consent_label)

    def _ask_consent(self) -> None:
        if not self._host_now():  # sem endereço não há para quem aceitar
            err = self.errors["cloud_base_url"]
            err.setText("Preencha o endereço (URL) antes de aceitar.")
            err.show()
            return
        self.consent_text.setText(modos.consent_text(SimpleNamespace(cloud_base_url=self.cloud_url.text().strip())))
        self.consent_box.show()

    def _accept_consent(self) -> None:
        self._consent, self._consent_host = True, self._host_now()
        self.consent_box.hide()
        self.errors["cloud_consent"].hide()
        self._update_consent()
        self.refresh_status()

    def _refuse_consent(self) -> None:
        self._consent = False
        self.consent_box.hide()
        self._update_consent()
        self.refresh_status()

    # --- estado do provedor --------------------------------------------------------------------------------
    def _cfg_like(self):
        v = self.values()
        return SimpleNamespace(**v)

    def refresh_status(self) -> None:
        """Consulta o provedor numa thread (o Ollama é a 127.0.0.1) e mostra o resultado quando chegar."""
        self._token += 1
        token, cfg = self._token, self._cfg_like()
        self.status_label.setText("verificando o gerador…")
        self.status_label.setObjectName("muted")
        self.status_box.show()

        def work() -> None:
            try:
                status = self._status_fn(cfg)
            except Exception:  # noqa: BLE001 - a janela nunca quebra por causa da checagem
                status = modos.ProviderStatus(cfg.generator_provider, False, "não foi possível verificar o gerador")
            self._sig.status.emit(token, status)

        if self._run_async:
            threading.Thread(target=work, name="hear-ai-gerador-status", daemon=True).start()
        else:
            work()

    def _on_status(self, token: int, status) -> None:
        if token != self._token:  # uma consulta mais nova já foi pedida
            return
        self.status = status
        if status.kind == "ollama":
            current = self.ollama_model.currentData() or ""
            self._set_models(status.models, current)
        self._render_status()

    def _set_models(self, models, current: str) -> None:
        self.ollama_model.clear()
        if models:
            for m in models:
                self.ollama_model.addItem(m, m)
            self._select(self.ollama_model, current if current in models else models[0])
        else:  # sem lista (Ollama fora do ar): mantém o modelo configurado, sem apagá-lo
            self.ollama_model.addItem(current or "(nenhum modelo encontrado)", current)

    def _render_status(self) -> None:
        prompt = self.mode_combo.currentData() == modos.MODE_PROMPT
        st = self.status
        if not prompt or st is None:
            self.warn_label.hide()
            if st is None and prompt:
                self.status_box.hide()
            return
        self.status_box.show()
        self.status_label.setText(("✔ " if st.ok else "✖ ") + st.text)
        self.status_label.setObjectName("ok" if st.ok else "warn")
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)
        self.instructions.setText("\n".join(st.instructions))
        self.instructions.setVisible(bool(st.instructions))
        self.warn_label.setVisible(not st.ok)
