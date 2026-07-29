"""
Monitor de Chuva - Unidades Cocal  (uso pessoal)
================================================
Cruza TRES fontes de previsao e trata a chuva como FATO apenas quando pelo menos
2 fontes concordam no mesmo periodo do dia. Quando so 1 aponta, o e-mail menciona
como "possivel", mas nao considera fato.

Fontes:
  - Yr / MET Norway (api.met.no)      -> sem chave
  - Open-Meteo (open-meteo.com)       -> sem chave
  - OpenWeatherMap (openweathermap.org) -> precisa de chave gratuita (OWM_API_KEY)

Roda varias vezes ao dia (previsao e volatil). O e-mail so dispara quando:
  1. Segunda-feira de manha  -> RELATORIO da semana (o mais importante).
  2. Vespera de um dia com chuva prevista -> LEMBRETE (a previsao se mantem).
  3. Mudanca na previsao para amanha (passou a chover, ou nao chove mais)
     em relacao ao que ja havia sido avisado -> ALERTA DE MUDANCA.

O estado anterior fica salvo em estado.json (versionado no repositorio) para
permitir a deteccao de mudancas entre execucoes.
"""

import os
import sys
import json
import time
import smtplib
import requests
from datetime import datetime, timedelta, timezone, date
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formataddr

# ============================================================
# 1. CONFIGURACAO
# ============================================================
LOCAIS = {
    "Paraguacu Paulista (SP)": (-22.4131, -50.5761),
    "Narandiba (SP)":          (-22.4083, -51.5289),
    "Passa Tempo (VERIFICAR)": (-22.4083, -51.5289),   # troque pela coordenada real
    "Rio Brilhante (MS)":      (-21.8019, -54.5461),
}

DIAS_PREVISAO      = 7
LIMIAR_MM_PERIODO  = 0.2      # mm no periodo: abaixo disso a fonte nao "aponta chuva"

# Periodos do dia (nome, hora_inicio, hora_fim)
PERIODOS = [("Madrugada", 0, 6), ("Manha", 6, 12), ("Tarde", 12, 18), ("Noite", 18, 24)]


def _rotulo_periodo(nome):
    """Ex.: 'Tarde (12h-18h)'."""
    for n, ini, fim in PERIODOS:
        if n == nome:
            return f"{n} ({ini:02d}h-{fim:02d}h)"
    return nome

USER_AGENT  = "bomfimbernardo9@gmail.com"     # exigido pelo Yr
OWM_API_KEY = os.environ.get("20bdd314c43aaeaa46f1d066343e4c4f")                     # chave gratuita do OpenWeatherMap

SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT   = 587
EMAIL_REMETENTE   = "bomfimbernardo9@gmail.com"     # conta que ENVIA (com senha de app)
EMAIL_DESTINATARIO = "eduardo.moraes2040@gmail.com"               # seu e-mail PESSOAL (voce repassa manual)
EMAIL_SENHA = os.environ.get("EMAIL_SENHA")

ESTADO_PATH   = "estado.json"
FORCAR_RESUMO = os.environ.get("FORCAR_RESUMO") == "1"   # forca o relatorio semanal (teste)

FUSO_BR = timezone(timedelta(hours=-3))
DIAS_SEMANA = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sab", "Dom"]
NOMES_FONTES = ["Yr", "Open-Meteo", "OpenWeatherMap"]


# ============================================================
# 2. COLETA DAS TRES FONTES  (cada uma vira uma lista de amostras)
# amostra = (datetime_local, mm, prob_percent_ou_None)
# ============================================================
def coletar_yr(lat, lon):
    url = "https://api.met.no/weatherapi/locationforecast/2.0/complete"
    r = requests.get(url, params={"lat": round(lat, 4), "lon": round(lon, 4)},
                     headers={"User-Agent": USER_AGENT}, timeout=30)
    r.raise_for_status()
    amostras = []
    for item in r.json()["properties"]["timeseries"]:
        t_local = datetime.fromisoformat(item["time"].replace("Z", "+00:00")).astimezone(FUSO_BR)
        bloco = item["data"].get("next_1_hours") or item["data"].get("next_6_hours")
        if not bloco:
            continue
        det = bloco.get("details", {})
        amostras.append((t_local,
                         det.get("precipitation_amount", 0.0) or 0.0,
                         det.get("probability_of_precipitation")))
    return amostras


def coletar_openmeteo(lat, lon):
    url = "https://api.open-meteo.com/v1/forecast"
    r = requests.get(url, params={"latitude": round(lat, 4), "longitude": round(lon, 4),
                                  "hourly": "precipitation,precipitation_probability",
                                  "timezone": "America/Sao_Paulo",
                                  "forecast_days": DIAS_PREVISAO}, timeout=30)
    r.raise_for_status()
    h = r.json().get("hourly", {})
    tempos = h.get("time", []); precs = h.get("precipitation", [])
    probs = h.get("precipitation_probability", [None] * len(tempos))
    amostras = []
    for i, t in enumerate(tempos):
        t_local = datetime.fromisoformat(t).replace(tzinfo=FUSO_BR)
        mm = precs[i] if i < len(precs) and precs[i] is not None else 0.0
        prob = probs[i] if i < len(probs) else None
        amostras.append((t_local, mm, prob))
    return amostras


def coletar_owm(lat, lon):
    if not OWM_API_KEY:
        raise RuntimeError("OWM_API_KEY nao definida")
    url = "https://api.openweathermap.org/data/2.5/forecast"   # 5 dias / 3h
    r = requests.get(url, params={"lat": round(lat, 4), "lon": round(lon, 4),
                                  "appid": OWM_API_KEY, "units": "metric"}, timeout=30)
    r.raise_for_status()
    amostras = []
    for item in r.json().get("list", []):
        t_utc = datetime.fromtimestamp(item["dt"], tz=timezone.utc)
        t_local = t_utc.astimezone(FUSO_BR)
        mm = item.get("rain", {}).get("3h", 0.0) or 0.0
        prob = item.get("pop")
        prob = round(prob * 100) if prob is not None else None
        amostras.append((t_local, mm, prob))
    return amostras


# ============================================================
# 3. AGRUPAMENTO EM PERIODOS E CRUZAMENTO
# ============================================================
def _periodo_idx(hora):
    return min(hora // 6, 3)


def bucketizar(amostras, hoje):
    """amostras -> { date: { periodo_idx: {'mm':soma, 'prob':max} } }"""
    limite = hoje + timedelta(days=DIAS_PREVISAO)
    out = {}
    for t_local, mm, prob in amostras:
        d = t_local.date()
        if not (hoje <= d < limite):
            continue
        pidx = _periodo_idx(t_local.hour)
        cel = out.setdefault(d, {}).setdefault(pidx, {"mm": 0.0, "prob": None})
        cel["mm"] += mm
        if prob is not None:
            cel["prob"] = prob if cel["prob"] is None else max(cel["prob"], prob)
    return out


def cruzar(buckets_por_fonte, hoje):
    """buckets_por_fonte = {nome_fonte: bucket ou None}.
    Retorna: {'dias': {date: [periodos...]}, 'status_dia': {date: 'chuva'/'seco'}}"""
    fontes = {nome: b for nome, b in buckets_por_fonte.items() if b is not None}
    dias = {}
    status_dia = {}
    for n in range(DIAS_PREVISAO):
        d = hoje + timedelta(days=n)
        periodos_com_chuva = []
        dia_confirmado = False
        for pidx, (nome_p, _, _) in enumerate(PERIODOS):
            apontam, mm_por_fonte = [], {}
            for nome, b in fontes.items():
                cel = b.get(d, {}).get(pidx)
                mm = cel["mm"] if cel else 0.0
                mm_por_fonte[nome] = mm
                if mm >= LIMIAR_MM_PERIODO:
                    apontam.append(nome)
            if apontam:
                confirmado = len(apontam) >= 2
                dia_confirmado = dia_confirmado or confirmado
                periodos_com_chuva.append({
                    "periodo": nome_p,
                    "confirmado": confirmado,
                    "apontam": apontam,
                    "mm_por_fonte": mm_por_fonte,
                })
        if periodos_com_chuva:
            dias[d] = periodos_com_chuva
        status_dia[d] = "chuva" if dia_confirmado else "seco"
    return {"dias": dias, "status_dia": status_dia, "n_fontes": len(fontes)}


# ============================================================
# 4. ESTADO (para detectar mudancas entre execucoes)
# ============================================================
def carregar_estado():
    try:
        with open(ESTADO_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def salvar_estado(estado):
    with open(ESTADO_PATH, "w", encoding="utf-8") as f:
        json.dump(estado, f, ensure_ascii=False, indent=2)


# ============================================================
# 5. MONTAGEM DOS E-MAILS
# ============================================================
def _fmt_dia(d):
    return f"{DIAS_SEMANA[d.weekday()]} {d.strftime('%d/%m')}"


def _selo(confirmado):
    if confirmado:
        return '<span style="color:#c62828;font-weight:bold">FATO (2+ fontes)</span>'
    return '<span style="color:#f9a825;font-weight:bold">possivel (1 fonte)</span>'


def _detalhe_periodos(periodos):
    linhas = []
    for p in periodos:
        fontes_txt = ", ".join(
            f'{nome} {p["mm_por_fonte"].get(nome, 0):.1f}mm'
            for nome in NOMES_FONTES if nome in p["mm_por_fonte"]
        )
        apont_txt = ", ".join(p["apontam"])
        linhas.append(
            f'<li><b>{_rotulo_periodo(p["periodo"])}</b> - {_selo(p["confirmado"])}'
            f'<br><span style="color:#555;font-size:13px">apontam chuva: {apont_txt} '
            f'| medicoes: {fontes_txt}</span></li>'
        )
    return "<ul>" + "".join(linhas) + "</ul>"


_RODAPE = ('<p style="color:#888;font-size:12px;margin-top:16px">'
           'Fontes cruzadas: Yr / MET Norway + Open-Meteo + OpenWeatherMap. '
           '"FATO" = 2 ou mais fontes concordam no periodo; "possivel" = apenas 1. '
           'Dados: MET Norway, Open-Meteo (CC BY 4.0) e OpenWeatherMap.</p>')


def montar_relatorio_semanal(resultados, hoje):
    houve = any(r["dias"] for r in resultados.values())
    data_txt = hoje.strftime("%d/%m/%Y")
    if not houve:
        return ("Semana sem chuva - Unidades Cocal",
            f'<div style="font-family:Arial,sans-serif;font-size:15px;color:#1a3c34">'
            f'<h2>Boas noticias!</h2><p>O cruzamento das tres fontes nao indica chuva '
            f'confirmada nos proximos {DIAS_PREVISAO} dias em nenhuma unidade.</p>'
            f'<ul>{"".join(f"<li>{u}</li>" for u in resultados)}</ul>{_RODAPE}</div>')
    blocos = []
    for unidade, r in resultados.items():
        if not r["dias"]:
            blocos.append(f'<h3 style="margin:16px 0 4px">{unidade}</h3>'
                          f'<p style="color:#2e7d32;margin:0">Sem chuva prevista.</p>')
            continue
        partes = []
        for d in sorted(r["dias"]):
            partes.append(f'<p style="margin:8px 0 2px"><b>{_fmt_dia(d)}</b></p>'
                          f'{_detalhe_periodos(r["dias"][d])}')
        blocos.append(f'<h3 style="margin:16px 0 4px">{unidade}</h3>{"".join(partes)}')
    return (f"Relatorio de chuva da semana - Unidades Cocal ({data_txt})",
        f'<div style="font-family:Arial,sans-serif;font-size:15px;color:#1a3c34">'
        f'<h2>Relatorio da semana</h2>'
        f'<p>Panorama dos proximos {DIAS_PREVISAO} dias por unidade e periodo '
        f'(horario de Brasilia), cruzando as tres fontes:</p>{"".join(blocos)}{_RODAPE}</div>')


def montar_lembrete(resultados, amanha, unidades_alvo, tipo, detalhes_mudanca=None):
    """tipo: 'previsao' (a chuva se mantem) ou 'mudanca'."""
    if tipo == "previsao":
        titulo = f"Lembrete: chuva prevista amanha ({_fmt_dia(amanha)}) - Cocal"
        intro = (f"<p>Para amanha ({_fmt_dia(amanha)}) ha chuva prevista (confirmada por "
                 f"2+ fontes) nas unidades abaixo:</p>")
    else:
        titulo = f"Mudanca na previsao para amanha ({_fmt_dia(amanha)}) - Cocal"
        itens = "".join(f"<li>{u}: {msg}</li>" for u, msg in (detalhes_mudanca or {}).items())
        intro = (f"<p>Houve <b>mudanca</b> na previsao para amanha ({_fmt_dia(amanha)}) "
                 f"em relacao ao ultimo aviso:</p><ul>{itens}</ul>"
                 f"<p>Situacao atual das unidades com chuva confirmada:</p>")
    blocos = []
    for unidade in unidades_alvo:
        periodos = resultados[unidade]["dias"].get(amanha, [])
        confirmados = [p for p in periodos if p["confirmado"]]
        if confirmados:
            blocos.append(f'<h3 style="margin:12px 0 4px">{unidade}</h3>{_detalhe_periodos(confirmados)}')
    corpo_blocos = "".join(blocos) or "<p>Nenhuma unidade com chuva confirmada para amanha.</p>"
    return (titulo,
        f'<div style="font-family:Arial,sans-serif;font-size:15px;color:#1a3c34">'
        f'<h2>Aviso de vespera</h2>{intro}{corpo_blocos}{_RODAPE}</div>')


def enviar_email(assunto, corpo_html):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = assunto
    msg["From"] = formataddr(("Monitor de Chuva Cocal", EMAIL_REMETENTE))
    msg["To"] = EMAIL_DESTINATARIO
    msg.attach(MIMEText(corpo_html, "html", "utf-8"))
    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=30) as s:
        s.ehlo(); s.starttls(); s.ehlo()
        s.login(EMAIL_REMETENTE, EMAIL_SENHA)
        s.sendmail(EMAIL_REMETENTE, [EMAIL_DESTINATARIO], msg.as_string())


# ============================================================
# 6. COLETA GERAL + LOGICA DE DISPARO
# ============================================================
def coletar_tudo(hoje):
    resultados = {}
    for unidade, (lat, lon) in LOCAIS.items():
        buckets = {}
        for nome, coletor in (("Yr", coletar_yr),
                              ("Open-Meteo", coletar_openmeteo),
                              ("OpenWeatherMap", coletar_owm)):
            try:
                buckets[nome] = bucketizar(coletor(lat, lon), hoje)
            except Exception as e:
                buckets[nome] = None
                print(f"  aviso: fonte {nome} falhou em {unidade}: {e}")
            time.sleep(0.5)
        resultados[unidade] = cruzar(buckets, hoje)
        nf = resultados[unidade]["n_fontes"]
        nd = len(resultados[unidade]["dias"])
        print(f"OK {unidade}: {nf}/3 fontes, {nd} dia(s) com chuva apontada")
    return resultados


def decidir_envios(resultados, estado, agora):
    """Funcao pura: decide o que enviar e devolve (lista_de_emails, novo_estado).
    Cada e-mail e uma tupla (assunto, corpo)."""
    hoje = agora.date()
    amanha = hoje + timedelta(days=1)
    emails = []
    estado = dict(estado)  # copia

    # status confirmado de amanha, por unidade
    status_amanha = {u: r["status_dia"].get(amanha, "seco") for u, r in resultados.items()}
    unidades_chuva = [u for u, s in status_amanha.items() if s == "chuva"]

    # ---- 1. Relatorio semanal (segunda de manha) ----
    eh_segunda_manha = (agora.weekday() == 0 and agora.hour < 12)
    if (eh_segunda_manha and estado.get("relatorio_semana") != str(hoje)) or FORCAR_RESUMO:
        emails.append(montar_relatorio_semanal(resultados, hoje))
        estado["relatorio_semana"] = str(hoje)
        # inicializa o estado de amanha sem mandar lembrete separado (o relatorio ja cobre)
        estado["amanha"] = {"data": str(amanha), "status": status_amanha}
        return emails, estado

    # ---- 2 e 3. Lembrete / mudanca da vespera ----
    prev = estado.get("amanha")
    if not prev or prev.get("data") != str(amanha):
        # primeira avaliacao para este "amanha"
        if unidades_chuva:
            emails.append(montar_lembrete(resultados, amanha, unidades_chuva, "previsao"))
        estado["amanha"] = {"data": str(amanha), "status": status_amanha}
    else:
        # mesma data-alvo: compara para detectar mudancas
        prev_status = prev.get("status", {})
        mudancas = {}
        for u in resultados:
            antes = prev_status.get(u, "seco")
            agora_s = status_amanha[u]
            if antes != agora_s:
                if agora_s == "chuva":
                    mudancas[u] = "passou a ter chuva confirmada"
                else:
                    mudancas[u] = "nao ha mais chuva confirmada (deve parar)"
        if mudancas:
            alvo = unidades_chuva or list(mudancas.keys())
            emails.append(montar_lembrete(resultados, amanha, alvo, "mudanca", mudancas))
        estado["amanha"] = {"data": str(amanha), "status": status_amanha}

    return emails, estado


def main():
    if not EMAIL_SENHA:
        print("ERRO: variavel EMAIL_SENHA nao definida."); sys.exit(1)

    agora = datetime.now(FUSO_BR)
    hoje = agora.date()
    print(f"Execucao em {agora.strftime('%d/%m/%Y %H:%M')} (Brasilia)")

    resultados = coletar_tudo(hoje)
    if not any(r["n_fontes"] > 0 for r in resultados.values()):
        print("Nenhuma fonte respondeu. Encerrando sem enviar.")
        return

    estado = carregar_estado()
    emails, novo_estado = decidir_envios(resultados, estado, agora)

    for assunto, corpo in emails:
        enviar_email(assunto, corpo)
        print("E-mail enviado:", assunto)
    if not emails:
        print("Nada a enviar nesta execucao.")

    salvar_estado(novo_estado)
    print("Estado atualizado.")


if __name__ == "__main__":
    main()
