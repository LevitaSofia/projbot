"""Servidor do bot (Flask + Telegram + loop de trading)."""

SANDRA_PROMPT = """PROMPT DA IA – SANDRA MODE (modo professora com pau na mão)

Você é a Sandra, professora do Candinho. Ele ganha 11 dólares por trade hoje.
Tu vai ajustar só quando o histórico mandar:

- Entrada: RSI <35 no 5-min, preço ≤ banda inferior (1% tolerância).
    Se RSI cair <25 e volume subir >20% da média, aumenta aposta pra 22 dólares.
    Se RSI <20 e BTC cai >2% em 15 min, vai até 33 dólares.
    Nunca mais que isso — trava absoluta.

- Saída: Take profit dinâmico: +5% fixo se RSI subir lento;
    trailing stop 3% se subir rápido (ganho >8% em 5 min).
    Vende sempre em RSI >65, mesmo com lucro baixo — melhor garantir que ficar pelado.

- Proteção: Saldo cair 10% do pico? Aposta vira 8 dólares, RSI <30, stop -2%.
    Mercado sangrar 3 dias? Para de comprar até voltar.

- Ativos: Prioriza ADA, DOGE, XRP, LINK. BTC e ETH só se tudo mais estiver ruim.
    Ignora stablecoins.

- Relatório real: Todo trade: entrada, saída, taxas Binance (0.1% compra + venda),
    lucro líquido. Manda no Telegram tipo:
    'LINK: +$0.17 líquido (1.53%) depois das taxas'.
    Diariamente: total do dia + acúmulo.

Objetivo: não é ser o rei do lucro — é ser o rei da sobrevivência.
Ganha devagar, perde menos, repique gordo quando dá.
Se errar duas vezes seguidas, aperta tudo.
Se acertar quatro, mantém.
Sem drama. Sem ego. Só lucro real no bolso dela.
"""

import os
import json
import time
import random
import re
import threading
import tempfile
import copy
import queue
from datetime import datetime
from zoneinfo import ZoneInfo
from flask import Flask, jsonify, render_template, request, abort
from dotenv import load_dotenv
import ccxt
import numpy as np
import requests
import asyncio
from openai import OpenAI
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

import logging
import traceback
from logging.handlers import RotatingFileHandler

# Configuração de Logs (rotativo para não estourar disco)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

_root_logger = logging.getLogger()
_rot_handler = RotatingFileHandler(
    'sistema_trading.log',
    maxBytes=5_000_000,
    backupCount=5,
    encoding='utf-8'
)
_rot_handler.setLevel(logging.INFO)
_rot_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
_root_logger.addHandler(_rot_handler)

# Carrega variáveis de ambiente
load_dotenv()

app = Flask(__name__)

# Lock global para evitar race condition entre threads (Flask/trading/Telegram)
state_lock = threading.RLock()

# Lock dedicado para serializar chamadas no mesmo client CCXT (evita race/nonce/rate-limit)
exchange_lock = threading.RLock()


def ex(fn, *args, **kwargs):
    """Serializa chamadas no client CCXT compartilhado."""
    with exchange_lock:
        return fn(*args, **kwargs)


@app.errorhandler(401)
def _unauthorized(_err):
    return jsonify({'error': 'unauthorized'}), 401


# Token simples para proteger rotas perigosas (produção)
API_TOKEN = os.getenv('API_TOKEN', '').strip()
if os.getenv("ENV", "dev") == "prod" and not API_TOKEN:
    raise RuntimeError("API_TOKEN obrigatório em produção.")


def _require_api_token_if_configured():
    """Exige token apenas se API_TOKEN estiver definido no ambiente."""
    if not API_TOKEN:
        return

    provided = (
        request.headers.get('X-API-Token')
        or request.args.get('token')
        or ''
    ).strip()

    if not provided:
        auth = (request.headers.get('Authorization') or '').strip()
        if auth.lower().startswith('bearer '):
            provided = auth[7:].strip()

    if not provided or provided != API_TOKEN:
        abort(401)


@app.before_request
def protect_api():
    # Se API_TOKEN estiver configurado, protege tudo em /api/
    if request.path.startswith('/api/'):
        _require_api_token_if_configured()


# Cache TTL simples para chamadas privadas caras (evita rate-limit)
_ttl_cache_lock = threading.RLock()
_ttl_cache: dict[str, dict] = {}


def _ttl_cached_call(cache_key: str, ttl_s: float, fn):
    now_mono = time.monotonic()
    with _ttl_cache_lock:
        entry = _ttl_cache.get(cache_key)
        if entry and (now_mono - entry['ts']) <= ttl_s:
            return entry['value']

    try:
        value = fn()
    except Exception:
        # Se der erro, tenta devolver cache antigo (se existir)
        with _ttl_cache_lock:
            entry = _ttl_cache.get(cache_key)
            if entry:
                return entry['value']
        raise

    with _ttl_cache_lock:
        _ttl_cache[cache_key] = {'ts': now_mono, 'value': value}
    return value


_http_session = requests.Session()


def _http_get_json(url: str, params: dict | None = None, timeout: int = 10, retries: int = 2):
    """GET com retry simples para erros transitórios (Binance)."""
    backoff = 1.5
    last_err = None
    for attempt in range(retries + 1):
        try:
            response = _http_session.get(url, params=params, timeout=timeout)
            if response.status_code in (418, 429, 500, 502, 503, 504):
                last_err = RuntimeError(f"HTTP {response.status_code}: {response.text}")
            else:
                response.raise_for_status()
                return response.json()
        except Exception as e:
            last_err = e

        if attempt < retries:
            time.sleep(backoff ** attempt)

    raise last_err


def cached_fetch_balance(ttl_s: float = 3.0):
    if not exchange:
        raise RuntimeError('Exchange não conectada')
    return _ttl_cached_call('fetch_balance', ttl_s, lambda: ex(exchange.fetch_balance))


def cached_private_get_account(ttl_s: float = 10.0):
    if not exchange:
        raise RuntimeError('Exchange não conectada')
    return _ttl_cached_call('private_get_account', ttl_s, lambda: ex(exchange.private_get_account))


def get_public_snapshot() -> dict:
    """Snapshot consistente do estado para rotas de leitura (evita races)."""
    with state_lock:
        return copy.deepcopy(lab_state)


@app.after_request
def add_no_cache_headers(response):
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

# Configurações
API_KEY = os.getenv('BINANCE_API_KEY')
SECRET = os.getenv('BINANCE_SECRET')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')

if not API_KEY or API_KEY == 'sua_api_key_aqui':
    print("\n" + "="*50)
    print("❌ AVISO: CHAVES DE API NÃO ENCONTRADAS")
    print("👉 Edite o arquivo .env e coloque suas chaves da Binance")
    print("="*50 + "\n")

SYMBOL = os.getenv('SYMBOL', 'BTC/USDT')
AMOUNT_INVEST = float(os.getenv('AMOUNT_INVEST', 11.0))
FEE_RATE = 0.001  # 0.1%

# Configuração GPT (controle de uso)
OPENAI_MODEL = os.getenv('OPENAI_MODEL', 'gpt-4.1-mini')
ENABLE_GPT_TUNING = os.getenv('ENABLE_GPT_TUNING', 'false').lower() == 'true'

# Timezone padrão (evita relatórios fora do horário em servidor UTC)
TZ = ZoneInfo("America/Sao_Paulo")


def now_sp() -> datetime:
    return datetime.now(TZ)


def now_iso() -> str:
    return now_sp().isoformat()


def parse_iso_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ)
        return dt
    except Exception:
        return None

# Parâmetros de estratégia AJUSTÁVEIS pela IA
STRATEGY_PARAMS = {
    'RSI_TARGET': 35,        # RSI para compra
    'TOLERANCE': 0.01,       # Tolerância da banda (1%)
    'STOP_LOSS': -3.0,       # Stop loss em %
    'TAKE_PROFIT': 5.0,      # Take profit em %
}

# Configuração OpenAI (retry em falha temporária)
openai_client = None
_openai_last_fail = 0.0


def get_openai_client():
    """Inicializa OpenAI client sob demanda; re-tenta a cada 60s se falhar."""
    global openai_client, _openai_last_fail

    if openai_client:
        return openai_client

    if not OPENAI_API_KEY or OPENAI_API_KEY == 'your_openai_api_key_here':
        return None

    if time.time() - _openai_last_fail < 60:
        return None

    try:
        openai_client = OpenAI(api_key=OPENAI_API_KEY)
        print("🧠 OpenAI (GPT) Configurado")
        return openai_client
    except Exception as e:
        _openai_last_fail = time.time()
        print(f"⚠️ Erro ao configurar OpenAI: {e}")
        return None


def openai_text(
    instructions: str,
    user_input: str,
    max_output_tokens: int = 400,
    temperature: float = 0.3,
) -> str:
    client = get_openai_client()
    if not client:
        return "🧠 IA não configurada no servidor."

    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": instructions},
            {"role": "user", "content": user_input},
        ],
        max_tokens=max_output_tokens,
        temperature=temperature,
    )
    return (resp.choices[0].message.content or "").strip()

# App do Telegram (criado no main antes das threads)
telegram_app = None

# Estado Global
lab_state = {
    'strategies': {
        'aggressive': {'name': 'Trading Real 💰', 'balance': 0.0, 'trades': [], 'position': None}
    },
    'selected_strategy': 'aggressive',  # Única estratégia - Trading Real
    'is_live': True,  # Valor inicial (pode ser sobrescrito por lab_data.json e/ou rotas)
    'running': True,  # Valor inicial (pode ser sobrescrito por lab_data.json e/ou rotas)
    'real_balance': 0.0,
    'last_update': '',
    'current_price': 0.0,
    'current_symbol': '---', # Símbolo atual sendo analisado
    'status': 'Parado', # Status inicial
    'market_overview': {}, # Radar de Mercado (Todas as moedas)
    'indicators': { # Novos indicadores para o frontend
        'rsi': 0.0,
        'bb_lower': 0.0,
        'bb_upper': 0.0
    },
    'diagnostics': {},  # Diagnóstico por moeda (motivo de não comprar)
    'user_info': {
        'uid': '---',
        'type': '---',
        'can_trade': False,
        'balances': {},
        'total_brl': 0.0,
        'usdt_brl_rate': 0.0
    },
    'last_trade_time': 0,  # Cooldown para evitar trades em loop
    'pnl': {  # Sandra Mode: Tracking de lucro diário
        'date': now_sp().strftime('%Y-%m-%d'),
        'day_net': 0.0,
        'total_net': 0.0
    },
    'btc_red_days': 0,  # Contador de dias vermelhos consecutivos do BTC
    'streak': {'wins': 0, 'losses': 0, 'tight': False}  # Sandra streak tracking
}

# Exchange
exchange = None
try:
    # Primeiro, obtém a diferença de tempo com o servidor da Binance
    exchange_temp = ccxt.binance({'enableRateLimit': True})
    time_diff = 0
    for i in range(3):
        try:
            server_time = exchange_temp.fetch_time()
            local_time = int(time.time() * 1000)
            time_diff = server_time - local_time
            print(f"⏰ Sincronizando tempo: diferença de {time_diff}ms com servidor Binance")
            break
        except Exception as e:
            print(f"⚠️ Tentativa {i+1} de sincronizar tempo falhou: {e}")
            time.sleep(1)
    
    exchange_config = {
        'apiKey': API_KEY,
        'secret': SECRET,
        'enableRateLimit': True,
        'options': {
            'defaultType': 'spot',
            'adjustForTimeDifference': True,
            'recvWindow': 60000,  # 60 segundos de tolerância
            'timeDifference': time_diff  # Aplica correção de tempo
        }
    }
    
    # Configuração de Proxy (se existir)
    proxy_url = os.getenv('PROXY_URL')
    if proxy_url:
        exchange_config['proxies'] = {
            'http': proxy_url,
            'https': proxy_url
        }
        print(f"🌍 Usando Proxy configurado: {proxy_url}")

    exchange = ccxt.binance(exchange_config)
    public_exchange = ccxt.binance({'enableRateLimit': True}) # Instância pública para fallback

    # Carrega markets para suportar exchange.market(symbol)/limits (min notional, precisões, etc.)
    try:
        exchange.load_markets()
    except Exception as e:
        print(f"⚠️ Não foi possível carregar markets da Binance agora: {e}")
    
    # Força sincronização de tempo
    print("⏳ Sincronizando relógio com a Binance...")
    diff = exchange.load_time_difference()
    print(f"✅ Relógio sincronizado. Diferença: {diff}ms")
    
    print("✅ Exchange conectada")
except Exception as e:
    print(f"⚠️ Erro ao conectar Exchange: {e}")


def load_lab_data():
    """Carrega dados persistidos do laboratório."""
    try:
        with open('lab_data.json', 'r') as f:
            data = json.load(f)
            with state_lock:
                lab_state['strategies'] = data.get(
                    'strategies', lab_state['strategies'])
                lab_state['selected_strategy'] = data.get(
                    'selected_strategy', 'aggressive')

                # Valida se a strategy existe
                if lab_state['selected_strategy'] not in lab_state['strategies']:
                    print(f"⚠️ Strategy '{lab_state['selected_strategy']}' não existe, usando 'aggressive'")
                    lab_state['selected_strategy'] = 'aggressive'

                lab_state['is_live'] = data.get('is_live', False)
                lab_state['running'] = data.get('running', False)

                # Sandra Mode: persistência de PnL, streak e stats globais
                lab_state['pnl'] = data.get('pnl', lab_state.get('pnl', {}))
                lab_state['streak'] = data.get('streak', lab_state.get('streak', {}))
                gs = data.get('global_stats')
                if isinstance(gs, dict):
                    GLOBAL_STATS.update(gs)
            print("📂 Dados do laboratório carregados")
    except FileNotFoundError:
        print("📝 Criando novo laboratório")
        save_lab_data()


def save_lab_data():
    """Salva estado atual do laboratório."""
    with state_lock:
        max_trades = 2000
        for _sk, _s in lab_state.get('strategies', {}).items():
            trades = _s.get('trades', [])
            if len(trades) > max_trades:
                _s['trades'] = trades[-max_trades:]

        data = {
            'strategies': lab_state['strategies'],
            'selected_strategy': lab_state['selected_strategy'],
            'is_live': lab_state['is_live'],
            'running': lab_state['running'],
            'pnl': lab_state.get('pnl', {}),
            'streak': lab_state.get('streak', {}),
            'global_stats': GLOBAL_STATS,
            'last_save': now_iso()
        }

        tmp_fd, tmp_path = tempfile.mkstemp(prefix="lab_data_", suffix=".json")
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(tmp_path, "lab_data.json")  # atomic
        finally:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass


def calculate_rsi(prices, period=14):
    """Calcula RSI (Wilder)."""
    if len(prices) < period + 1:
        return 50

    deltas = np.diff(prices)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)

    avg_gain = float(np.mean(gains[:period]))
    avg_loss = float(np.mean(losses[:period]))

    for i in range(period, len(deltas)):
        avg_gain = ((avg_gain * (period - 1)) + gains[i]) / period
        avg_loss = ((avg_loss * (period - 1)) + losses[i]) / period

    if avg_loss == 0:
        return 100

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calculate_bollinger(prices, period=20):
    """Calcula Bandas de Bollinger."""
    if len(prices) < period:
        return prices[-1], prices[-1], prices[-1]

    sma = np.mean(prices[-period:])
    std = np.std(prices[-period:])

    upper = sma + (2 * std)
    lower = sma - (2 * std)

    return upper, sma, lower


# --- INTEGRAÇÃO TELEGRAM & GPT ---

_telegram_queue: "queue.Queue[str]" = queue.Queue(maxsize=1000)
_telegram_worker_lock = threading.Lock()
_telegram_worker_started = False


def _send_telegram_message_now(message: str) -> None:
    """Envia mensagem para o Telegram (chamada no worker)."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    def _escape_md_basic(text: str) -> str:
        # Markdown (Telegram): escapa caracteres que mais quebram mensagens
        # sem quebrar o uso atual de '*' para negrito.
        return re.sub(r"([_\[\]`])", r"\\\1", text)

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": _escape_md_basic(message),
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }

    try:
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code == 200:
            print("📨 Mensagem Telegram enviada com sucesso!")
            return
    except Exception:
        response = None

    # Se falhar com Markdown, tenta enviar em texto puro
    try:
        payload_no_md = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "disable_web_page_preview": True,
        }
        retry = requests.post(url, json=payload_no_md, timeout=10)
        if retry.status_code == 200:
            print("📨 Mensagem Telegram enviada (sem Markdown)")
        else:
            print(f"❌ Erro Telegram: {retry.text}")
    except Exception as e:
        if response is not None:
            print(f"❌ Erro Telegram: {response.text}")
        else:
            print(f"❌ Erro ao enviar Telegram: {e}")


def _telegram_worker() -> None:
    while True:
        message = _telegram_queue.get()
        try:
            _send_telegram_message_now(message)
        finally:
            _telegram_queue.task_done()


def _ensure_telegram_worker() -> None:
    global _telegram_worker_started
    with _telegram_worker_lock:
        if _telegram_worker_started:
            return
        thread = threading.Thread(target=_telegram_worker, daemon=True)
        thread.start()
        _telegram_worker_started = True


def send_telegram_message(message: str) -> None:
    """Envia mensagem para o Telegram (fila assíncrona)."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID or TELEGRAM_TOKEN == 'your_telegram_token_here':
        print("⚠️ Telegram não configurado. Mensagem não enviada.")
        return

    _ensure_telegram_worker()
    try:
        _telegram_queue.put_nowait(message)
    except queue.Full:
        print("⚠️ Fila do Telegram cheia, mensagem descartada.")


# ==================== SISTEMA DE RELATÓRIOS AUTOMÁTICOS ====================

# Horários para enviar relatórios (formato 24h)
REPORT_HOURS = [8, 12, 18, 22]  # 8h, 12h, 18h, 22h
last_report_hour = -1  # Controle para não repetir relatório na mesma hora

def generate_market_report():
    """Gera relatório completo de todas as moedas."""
    snap = get_public_snapshot()
    report_lines = []
    report_lines.append("📊 *RELATÓRIO DO BOT DE TRADING*")
    report_lines.append(f"🕐 {now_sp().strftime('%d/%m/%Y %H:%M')}")
    report_lines.append("")
    
    # Status do bot
    status = "🟢 ATIVO" if snap.get('running') else "🔴 PARADO"
    mode = "💰 REAL" if snap.get('is_live') else "🧪 SIMULAÇÃO"
    report_lines.append(f"*Status:* {status} | {mode}")
    
    # Saldo
    usdt = snap.get('real_balance', 0)
    report_lines.append(f"*Saldo USDT:* ${usdt:.2f}")
    report_lines.append("")
    
    # Posição atual
    selected = snap.get('selected_strategy', 'aggressive')
    strategy = snap.get('strategies', {}).get(selected, {})
    position = strategy.get('position')
    
    if position:
        pos_symbol = position.get('symbol', 'N/A')
        entry = position.get('entry_price', 0)
        report_lines.append(f"📍 *POSIÇÃO ABERTA:* {pos_symbol}")
        report_lines.append(f"   Entrada: ${entry:.2f}")
        report_lines.append("")
    else:
        report_lines.append("📍 *Sem posição aberta*")
        report_lines.append("")
    
    # Análise de cada moeda
    report_lines.append("*ANÁLISE DAS MOEDAS:*")
    report_lines.append("")
    
    opportunities = []
    close_opportunities = []
    
    market_cache = snap.get('market_overview', {}) or {}

    for symbol in WATCHLIST:
        try:
            data = market_cache.get(symbol)
            if not data:
                continue

            price = data.get('price')
            rsi = data.get('rsi')
            bb_lower = data.get('bb_lower')
            bb_upper = data.get('bb_upper')

            if price is not None and rsi is not None and bb_lower is not None:
                tolerance = bb_lower * SANDRA["ENTRY_TOL"]
                buy_limit = bb_lower + tolerance
                
                # Calcula distância do preço para a zona de compra
                dist_to_buy = ((price - buy_limit) / buy_limit) * 100
                
                # Determina emoji e status
                entry_rsi = SANDRA["ENTRY_RSI"]
                if rsi < entry_rsi and price <= buy_limit:
                    emoji = "🟢"
                    status = "COMPRA!"
                    opportunities.append(symbol)
                elif rsi < (entry_rsi + 5) or dist_to_buy < 2:
                    emoji = "🟡"
                    status = "QUASE"
                    close_opportunities.append((symbol, rsi, dist_to_buy))
                elif rsi > 70:
                    emoji = "🔴"
                    status = "RISCO"
                else:
                    emoji = "⚪"
                    status = "AGUARD"
                
                # Linha do relatório
                coin_name = symbol.replace('/USDT', '')
                report_lines.append(f"{emoji} *{coin_name}*: RSI={rsi:.0f} | ${price:.2f}")
                report_lines.append(f"   └ Limite compra: ${buy_limit:.2f} ({dist_to_buy:+.1f}%)")
        except Exception as e:
            print(f"Erro ao analisar {symbol}: {e}")
    
    report_lines.append("")
    
    # PnL do dia e total (Sandra Mode: dinheiro líquido)
    day_net = snap.get('pnl', {}).get('day_net', 0.0)
    total_net = snap.get('pnl', {}).get('total_net', 0.0)
    report_lines.append(f"💰 *PnL Hoje (líquido):* ${day_net:+.2f} | *Acúmulo:* ${total_net:+.2f}")
    report_lines.append("")
    
    # Resumo
    if opportunities:
        report_lines.append(f"🚨 *OPORTUNIDADES AGORA:* {', '.join(opportunities)}")
    elif close_opportunities:
        report_lines.append("⚠️ *MOEDAS PRÓXIMAS DE COMPRA:*")
        for sym, rsi, dist in close_opportunities:
            coin = sym.replace('/USDT', '')
            report_lines.append(f"   • {coin}: RSI={rsi:.0f}, falta {abs(dist):.1f}% p/ banda")
    else:
        report_lines.append("😴 *Nenhuma oportunidade no momento*")
        report_lines.append("   Aguardando RSI < 35 + preço na banda inferior")
    
    return "\n".join(report_lines)


def send_daily_report():
    """Envia relatório diário via Telegram."""
    try:
        report = generate_market_report()
        send_telegram_message(report)
        print(f"📨 Relatório enviado às {now_sp().strftime('%H:%M')}")
        logging.info("Relatório diário enviado via Telegram")
    except Exception as e:
        print(f"❌ Erro ao enviar relatório: {e}")
        logging.error(f"Erro ao enviar relatório: {e}")


def check_and_send_reports():
    """Verifica se está na hora de enviar relatório."""
    global last_report_hour
    current_hour = now_sp().hour
    
    # Só envia se mudou de hora e está em um dos horários programados
    if current_hour in REPORT_HOURS and current_hour != last_report_hour:
        last_report_hour = current_hour
        send_daily_report()


# ==================== FIM SISTEMA DE RELATÓRIOS ====================

# Controle para não spammar alertas
_last_opportunity_alert = {}

def send_opportunity_alert(symbol, price, rsi, bb_lower):
    """Envia alerta de oportunidade antes do gatilho disparar."""
    global _last_opportunity_alert
    
    # Evita spam: só alerta a cada 5 minutos por moeda
    current_time = time.time()
    last_alert = _last_opportunity_alert.get(symbol, 0)
    if current_time - last_alert < 300:  # 5 minutos
        return
    
    _last_opportunity_alert[symbol] = current_time
    
    # Calcula distância para a banda
    dist_to_band = ((price - bb_lower) / bb_lower) * 100
    
    # Determina o nível de proximidade
    entry_rsi = SANDRA["ENTRY_RSI"]
    if rsi < entry_rsi and dist_to_band <= 1:
        status = "🟢 SINAL FORTE - Pronto para comprar!"
    elif rsi < entry_rsi:
        status = f"🟡 RSI OK (precisa <{entry_rsi}), preço {dist_to_band:.1f}% acima da banda"
    elif dist_to_band <= 1:
        status = f"🟡 Preço OK, RSI={rsi:.1f} (precisa <{entry_rsi})"
    else:
        status = f"⏳ Quase... RSI={rsi:.1f} | {dist_to_band:.1f}% da banda"
    
    msg = (
        f"👀 *OPORTUNIDADE DETECTADA*\n\n"
        f"🪙 {symbol}\n"
        f"💵 Preço: ${price:.4f}\n"
        f"📊 RSI: {rsi:.1f}\n"
        f"📉 Banda Inferior: ${bb_lower:.4f}\n"
        f"📏 Distância: {dist_to_band:.1f}%\n\n"
        f"{status}"
    )
    
    print(f"👀 Oportunidade: {symbol} | RSI={rsi:.1f} | Dist={dist_to_band:.1f}%")
    send_telegram_message(msg)


def analyze_market_with_gpt(symbol, price, rsi, bb_lower, action_type):
    """IA que analisa histórico e ajusta estratégia automaticamente."""
    client = get_openai_client()
    if not client:
        return "🤖 IA não configurada."

    # Coleta histórico de trades para análise
    selected = lab_state['selected_strategy']
    trades = lab_state['strategies'][selected].get('trades', [])
    
    # Analisa últimos 5 trades
    ultimos_trades = trades[-5:] if len(trades) >= 5 else trades
    trades_perdidos = [t for t in ultimos_trades if t.get('profit_pct', 0) < -2]
    trades_ganhos = [t for t in ultimos_trades if t.get('profit_pct', 0) > 0]
    
    # Calcula RSI médio das operações
    rsi_medio = sum([t.get('rsi', 35) for t in ultimos_trades]) / len(ultimos_trades) if ultimos_trades else 35
    
    # Contexto do mercado atual
    market_context = f"RSI atual={rsi:.1f}, preço=${price:.2f}, banda=${bb_lower:.2f}"
    
    # Parâmetros atuais (Sandra Mode real)
    params_atuais = f"ENTRY_RSI={SANDRA['ENTRY_RSI']}, TOL={SANDRA['ENTRY_TOL']}, STOP_BASE={SANDRA['STOP_BASE']}%"
    
    prompt = f"""Você é chefe de estratégia agora, olha o último ciclo:
- Últimos 5 trades: {len(trades_perdidos)} perdidos acima de 2%, {len(trades_ganhos)} ganhos, RSI médio foi {rsi_medio:.1f}.
- Mercado: {market_context}
- Parâmetros atuais: {params_atuais}

Regras:
- Se perdeu 2 ou mais seguidos: diminui RSI pra 32, reduz tolerância pra 0.5%, stop loss pra -2.5%.
- Se ganhou fácil em RSI <35: mantém tudo, só diz 'segura firme'.
- Se RSI >70 por 3 dias: vira conservadora — RSI 38, venda no primeiro 2%.

Responde EXATAMENTE assim (duas linhas):
Ação: ajuste ou mantém
Telegram: uma frase curta tipo 'IA mudou o plano — agora mais esperta'

Nada de enrolação."""

    try:
        response = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": SANDRA_PROMPT},
                {"role": "system", "content": "Responda APENAS no formato pedido. Não invente dados. Se faltar informação, seja conservador."},
                {"role": "user", "content": prompt}
            ],
            max_tokens=100,
            temperature=0  # Sandra: determinístico
        )
        
        content = response.choices[0].message.content.strip()
        res = content.split('\n')
        
        # Parse da resposta
        acao = ""
        telegram_msg = ""
        
        for line in res:
            if line.lower().startswith('ação:') or line.lower().startswith('acao:'):
                acao = line.split(':', 1)[1].strip() if ':' in line else ''
            elif line.lower().startswith('telegram:'):
                telegram_msg = line.split(':', 1)[1].strip() if ':' in line else ''
        
        # Aplica ajustes se necessário
        if 'ajuste' in acao.lower() or 'ajustar' in acao.lower():
            with state_lock:
                SANDRA["ENTRY_RSI"] = 32
                SANDRA["ENTRY_TOL"] = 0.005
                SANDRA["STOP_BASE"] = -2.5
                lab_state.setdefault('streak', {'wins': 0, 'losses': 0, 'tight': False})
                lab_state['streak']['tight'] = True
            
            print(f"🤖 IA AJUSTOU SANDRA: ENTRY_RSI={SANDRA['ENTRY_RSI']}, TOL={SANDRA['ENTRY_TOL']}, STOP_BASE={SANDRA['STOP_BASE']}%")
            send_telegram_message(
                f"🤖 IA ajustou a Sandra\\n\\n{telegram_msg}\\n\\n"
                f"Novos params: RSI<{SANDRA['ENTRY_RSI']}, Tol {SANDRA['ENTRY_TOL']*100:.1f}%, Stop {SANDRA['STOP_BASE']}%"
            )
        else:
            if telegram_msg:
                send_telegram_message(f"🟢 {telegram_msg}")
        
        return content
        
    except Exception as e:
        print(f"❌ Erro GPT: {e}")
        return "🤖 Erro na análise de IA."

# ---------------------------------


# === PRIORIDADE SANDRA: ADA, DOGE, XRP, LINK primeiro; BTC/ETH só se tudo ruim ===
PRIORITY_COINS = ['ADA/USDT', 'DOGE/USDT', 'XRP/USDT', 'LINK/USDT']
SECONDARY_COINS = ['DOT/USDT', 'LTC/USDT', 'SOL/USDT', 'BNB/USDT']
LAST_RESORT = ['ETH/USDT', 'BTC/USDT']  # só se tudo mais estiver ruim

WATCHLIST = PRIORITY_COINS + SECONDARY_COINS + LAST_RESORT

# === MODO SANDRA: APOSTAS VARIÁVEIS ===
HIGH_VOLATILITY_COINS = ['DOGE/USDT', 'ADA/USDT', 'SOL/USDT', 'XRP/USDT', 'LINK/USDT']
GLOBAL_STATS = {'peak_balance': 0.0, 'drawdown_mode': False}

# Valor mínimo de ordem na Binance (em USDT) - 8.0 para permitir proteção $8
MIN_ORDER_VALUE = 8.0


def get_min_notional_usdt(symbol: str, fallback: float = 10.0) -> float:
    """Retorna min notional (USDT) do par (Binance/CCXT).

    Observação: muitos pares exigem ~$10+; no modo proteção a Sandra NÃO deve
    "furar" aumentando aposta só para passar no mínimo.
    """
    try:
        if exchange:
            market = exchange.market(symbol)
            lim = (market.get('limits', {}) or {}).get('cost', {}) or {}
            m = lim.get('min', None)
            if m is not None:
                return float(m)

            info = market.get('info', {}) or {}
            filters = info.get('filters', []) or []
            for f in filters:
                if (f.get('filterType') or '').upper() in ('MIN_NOTIONAL', 'NOTIONAL'):
                    v = f.get('minNotional') or f.get('notional') or f.get('minNotionalValue')
                    if v is not None:
                        return float(v)
    except Exception:
        pass
    return float(fallback)

# === CONFIG SANDRA MODE CENTRALIZADO ===
SANDRA = {
    "BASE_BET": 11.0,
    "BET_STRONG": 22.0,
    "BET_GOLD": 33.0,
    "BET_DRAWDOWN": 8.0,
    "MAX_BET": 33.0,
    
    "ENTRY_RSI": 35,
    "ENTRY_TOL": 0.01,  # 1%
    "STRONG_RSI": 25,
    "GOLD_RSI": 20,
    "DRAWDOWN_RSI": 30,
    
    "SELL_RSI": 65,
    
    "STOP_BASE": -3.0,
    "STOP_DRAWDOWN": -2.0,
    
    "TP_SLOW": 5.0,
    "FAST_PROFIT": 8.0,
    "FAST_WINDOW_S": 300,
    "TRAIL_FAST": 3.0,
}

# Cache BTC (evita spam de API)
BTC_CACHE = {
    "dump15": {"ts": 0, "val": False},
    "bleed3d": {"ts": 0, "val": False},
}
btc_cache_lock = threading.RLock()

_market_cache_lock = threading.RLock()
_market_cache: dict[str, dict] = {}
MARKET_CACHE_TTL_S = 10


def btc_drop_15m():
    """Detecta se BTC caiu >2% nos últimos 15 minutos (3 candles de 5m)."""
    try:
        url = 'https://api.binance.com/api/v3/klines'
        params = {'symbol': 'BTCUSDT', 'interval': '5m', 'limit': 5}
        raw = _http_get_json(url, params=params, timeout=10, retries=2)
        close_now = float(raw[-1][4])
        close_15m = float(raw[-4][4])  # 3 candles atrás
        drop = (close_now - close_15m) / close_15m * 100
        return drop <= -2.0
    except Exception as e:
        print(f"⚠️ Erro ao verificar BTC -2%/15min: {e}")
        return False


def btc_bleeding_3days():
    """Detecta se BTC está sangrando (3 dias vermelhos consecutivos no diário)."""
    try:
        url = 'https://api.binance.com/api/v3/klines'
        params = {'symbol': 'BTCUSDT', 'interval': '1d', 'limit': 4}
        raw = _http_get_json(url, params=params, timeout=10, retries=2)
        
        # Verifica os últimos 3 dias fechados (ignora o dia atual)
        red_days = 0
        for candle in raw[-4:-1]:  # Últimos 3 dias (exclui hoje)
            open_price = float(candle[1])
            close_price = float(candle[4])
            if close_price < open_price:  # Dia vermelho
                red_days += 1
        
        return red_days >= 3
    except Exception as e:
        print(f"⚠️ Erro ao verificar BTC 3 dias sangrar: {e}")
        return False


def btc_drop_15m_cached(ttl=20):
    """Cache de btc_drop_15m para evitar spam de API (TTL 20s)."""
    now = time.time()
    with btc_cache_lock:
        ts = BTC_CACHE["dump15"]["ts"]
        val = BTC_CACHE["dump15"]["val"]
    if now - ts <= ttl:
        return val

    new_val = btc_drop_15m()
    with btc_cache_lock:
        BTC_CACHE["dump15"]["ts"] = now
        BTC_CACHE["dump15"]["val"] = new_val
    return new_val


def btc_bleeding_3days_cached(ttl=3600):
    """Cache de btc_bleeding_3days (TTL 1h - diário não muda rápido)."""
    now = time.time()
    with btc_cache_lock:
        ts = BTC_CACHE["bleed3d"]["ts"]
        val = BTC_CACHE["bleed3d"]["val"]
    if now - ts <= ttl:
        return val

    new_val = btc_bleeding_3days()
    with btc_cache_lock:
        BTC_CACHE["bleed3d"]["ts"] = now
        BTC_CACHE["bleed3d"]["val"] = new_val
    return new_val


def fetch_market_data(symbol, interval='5m', limit=60):
    """Busca dados de mercado no timeframe de sinal (5m) + volume."""
    cache_key = f"{symbol}:{interval}:{limit}"
    now_mono = time.monotonic()
    with _market_cache_lock:
        entry = _market_cache.get(cache_key)
        if entry and (now_mono - entry['ts']) <= MARKET_CACHE_TTL_S:
            return entry['value']
    try:
        url = 'https://api.binance.com/api/v3/klines'
        params = {'symbol': symbol.replace('/', ''), 'interval': interval, 'limit': limit}
        raw_data = _http_get_json(url, params=params, timeout=10, retries=2)
        
        closes = [float(candle[4]) for candle in raw_data]
        volumes = [float(candle[5]) for candle in raw_data]
        
        current_price = closes[-1]
        rsi = calculate_rsi(closes)
        upper, sma, lower = calculate_bollinger(closes)
        
        vol_now = volumes[-1]
        vol_avg = float(np.mean(volumes[-20:])) if len(volumes) >= 20 else float(np.mean(volumes))
        
        value = (current_price, rsi, lower, upper, vol_now, vol_avg)
        with _market_cache_lock:
            _market_cache[cache_key] = {'ts': now_mono, 'value': value}
        return value
    except Exception as e:
        print(f"❌ Erro ao buscar dados ({symbol}): {e}")
        with _market_cache_lock:
            entry = _market_cache.get(cache_key)
            if entry:
                return entry['value']
        return None, None, None, None, None, None


def check_strategy_signal(strategy_name, price, rsi, bb_lower, symbol, vol_now, vol_avg, btc_is_dumping_15m, btc_bleeding):
    """
    CÉREBRO SANDRA MODE com config centralizado.
    """
    # 0. Mercado sangrar 3 dias: PARA DE COMPRAR
    if btc_bleeding:
        print(f"🩸 MERCADO SANGRANDO 3 DIAS - Não compra até voltar")
        return 0.0
    
    # 1. Modo Proteção (drawdown 10%)
    with state_lock:
        drawdown_mode = bool(GLOBAL_STATS.get('drawdown_mode', False))

    if drawdown_mode:
        if rsi < SANDRA["DRAWDOWN_RSI"] and price <= bb_lower * (1 + SANDRA["ENTRY_TOL"]):
            return SANDRA["BET_DRAWDOWN"]
        return 0.0

    # 2. Regra base de entrada
    base_entry = (rsi < SANDRA["ENTRY_RSI"]) and (price <= bb_lower * (1 + SANDRA["ENTRY_TOL"]))
    
    if not base_entry:
        return 0.0
    
    # 3. $33: RSI <20 e BTC cai >2% em 15 min
    if rsi < SANDRA["GOLD_RSI"] and btc_is_dumping_15m:
        print(f"💎 SINAL EXCEPCIONAL em {symbol}! RSI={rsi:.1f} + BTC despencando (Apostando ${SANDRA['BET_GOLD']})")
        return SANDRA["BET_GOLD"]
    
    # 4. $22: RSI <25 e volume >20% acima da média
    if rsi < SANDRA["STRONG_RSI"] and vol_avg and (vol_now > 1.2 * vol_avg):
        print(f"🔥 SINAL FORTE em {symbol}. RSI={rsi:.1f} + Volume alto (Apostando ${SANDRA['BET_STRONG']})")
        return SANDRA["BET_STRONG"]
    
    # 5. $11: Padrão
    return SANDRA["BASE_BET"]


def update_sandra_streak(net_profit_usdt):
    """Ajusta Sandra baseado em streak (2 perdas = aperta, 2 wins = volta)."""
    tighten = False
    relax = False
    with state_lock:
        st = lab_state.setdefault("streak", {"wins": 0, "losses": 0, "tight": False})

        if net_profit_usdt < 0:
            st["losses"] += 1
            st["wins"] = 0
        else:
            st["wins"] += 1
            st["losses"] = 0

        # 2 perdas seguidas => aperta tudo
        if st["losses"] >= 2 and not st["tight"]:
            st["tight"] = True
            SANDRA["ENTRY_RSI"] = 32
            SANDRA["STOP_BASE"] = -2.5
            SANDRA["ENTRY_TOL"] = 0.005
            tighten = True

        # 4 wins seguidas => solta pro padrão
        if st["tight"] and st["wins"] >= 4:
            st["tight"] = False
            SANDRA["ENTRY_RSI"] = 35
            SANDRA["STOP_BASE"] = -3.0
            SANDRA["ENTRY_TOL"] = 0.01
            relax = True

    if tighten:
        send_telegram_message("⚠️ Sandra apertou: 2 losses seguidas. Agora RSI<32 e stop mais curto.")
        return
    if relax:
        send_telegram_message("🟢 Sandra relaxou: 4 wins seguidas. Voltou ao padrão.")


def get_diagnostic(strategy_name, price, rsi, bb_lower, position=None):
    """Gera diagnóstico legível explicando por que não está comprando/vendendo."""
    
    # Se tem posição aberta, calcula lucro
    if position:
        entry_price = position.get('entry_price', price)
        profit_pct = ((price - entry_price) / entry_price) * 100
        emoji = "📈" if profit_pct > 0 else "📉"
        return f"{emoji} COMPRADO (Lucro: {profit_pct:+.2f}%)"
    
    # Verifica saldo primeiro
    with state_lock:
        usdt_balance = lab_state.get('real_balance', 0.0)
    if usdt_balance < MIN_ORDER_VALUE:
        return f"💸 SALDO BAIXO (${usdt_balance:.2f} < ${MIN_ORDER_VALUE})"
    
    # Analisa condições de compra (ESTRATÉGIA EQUILIBRADA)
    issues = []
    with state_lock:
        if bool(GLOBAL_STATS.get('drawdown_mode', False)):
            issues.append("🛡️ Proteção ativa (drawdown 10%)")
    rsi_target = SANDRA["ENTRY_RSI"]
    tolerance = bb_lower * SANDRA["ENTRY_TOL"]
    
    # Se RSI E preço estão bons, é sinal forte
    if rsi < rsi_target and price <= bb_lower + tolerance:
        return f"🚨 RSI < {rsi_target} + BANDA INFERIOR! COMPRA!"
    
    # RSI baixo mas preço não está na banda
    if rsi < rsi_target:
        diff_pct = ((price - bb_lower) / bb_lower) * 100
        return f"⚠️ RSI bom ({rsi:.1f}) mas preço {diff_pct:.1f}% acima da banda"
    
    if rsi >= rsi_target:
        issues.append(f"RSI={rsi:.1f} (precisa <35)")
    if price > bb_lower + tolerance:
        diff_pct = ((price - bb_lower) / bb_lower) * 100
        issues.append(f"Preço {diff_pct:.1f}% acima da banda")
    
    if not issues:
        return "🎯 PRONTO PARA COMPRAR!"
    
    return "⏳ " + " | ".join(issues)


def check_exit_signal(position, current_price, rsi, bb_upper=None):
    """
    SAÍDA SANDRA MODE CORRETO com trailing PERSISTENTE.
    """
    entry_price = position['entry_price']

    # Sempre timezone-aware
    entry_time = parse_iso_dt(position.get('entry_time')) or now_sp()
    now = now_sp()

    profit_pct = ((current_price - entry_price) / entry_price) * 100
    
    # 1) REGRA DURA: RSI >= SELL_RSI vende sempre
    if rsi >= SANDRA["SELL_RSI"]:
        return True, f"RSI≥{SANDRA['SELL_RSI']} (garantir)"
    
    # 2) Stop loss dinâmico
    with state_lock:
        drawdown_mode = bool(GLOBAL_STATS.get('drawdown_mode', False))

    stop_limit = SANDRA["STOP_DRAWDOWN"] if drawdown_mode else SANDRA["STOP_BASE"]
    if profit_pct <= stop_limit:
        return True, f"STOP {stop_limit}%"

    # Se for mutar trailing/highest, isso deve acontecer sob state_lock
    # (o call-site do trading loop já garante isso)
    
    # 3) Ativa trailing se houve subida rápida (flag PERSISTENTE)
    elapsed = (now - entry_time).total_seconds()
    if (not position.get("trail_active", False)) and (elapsed <= SANDRA["FAST_WINDOW_S"]) and (profit_pct >= SANDRA["FAST_PROFIT"]):
        position["trail_active"] = True
        print(f"🎢 Trailing ativado! Lucro {profit_pct:.1f}% em {elapsed:.0f}s")
    
    # Atualiza máxima
    highest = position.get("highest_price", entry_price)
    if current_price > highest:
        highest = current_price
        position["highest_price"] = highest
    
    # 3b) Trailing persistente (não desliga após 5min)
    if position.get("trail_active", False):
        pullback = ((highest - current_price) / highest) * 100
        if pullback >= SANDRA["TRAIL_FAST"]:
            return True, f"TRAIL {SANDRA['TRAIL_FAST']}% (subida rápida)"
        return False, "Segurando (trailing ativo)"
    
    # 4) TP fixo (subida lenta)
    if profit_pct >= SANDRA["TP_SLOW"]:
        return True, f"TP {SANDRA['TP_SLOW']}% (subida lenta)"
    
    return False, "Segurando"


def convert_brl_to_usdt(min_brl=20):
    """Converte BRL para USDT automaticamente quando necessário."""
    try:
        balance = ex(exchange.fetch_balance)
        brl_balance = balance.get('free', {}).get('BRL', 0.0)
        usdt_balance = balance.get('free', {}).get('USDT', 0.0)
        
        # Se já tem USDT suficiente, não precisa converter
        if usdt_balance >= MIN_ORDER_VALUE:
            print(f"✅ Saldo USDT OK: ${usdt_balance:.2f}")
            return usdt_balance
        
        # Se não tem BRL suficiente para converter
        if brl_balance < min_brl:
            print(f"⚠️ Saldo BRL insuficiente para conversão: R${brl_balance:.2f} (mínimo R${min_brl})")
            return usdt_balance
        
        # Busca cotação USDT/BRL
        try:
            ticker = ex(exchange.fetch_ticker, 'USDT/BRL')
            usdt_price_brl = ticker['last']  # Preço de 1 USDT em BRL
            
            # Calcula quantidade de USDT a comprar (usando 95% do BRL para taxas)
            brl_to_use = brl_balance * 0.95
            usdt_qty = brl_to_use / usdt_price_brl
            
            print(f"🔄 Convertendo R${brl_to_use:.2f} para ~${usdt_qty:.2f} USDT...")
            
            # Executa ordem de compra de USDT com BRL
            order = ex(exchange.create_market_buy_order, 'USDT/BRL', usdt_qty)
            
            new_usdt = order['filled']
            total_usdt = usdt_balance + new_usdt
            print(f"✅ Conversão concluída! Recebido: ${new_usdt:.2f} USDT | Total: ${total_usdt:.2f}")
            
            # Notifica no Telegram
            msg = f"🔄 *CONVERSÃO BRL → USDT*\n\n💵 Convertido: R${brl_to_use:.2f}\n💰 Recebido: ${new_usdt:.2f} USDT\n📊 Saldo total: ${total_usdt:.2f} USDT"
            send_telegram_message(msg)
            
            # Atualiza saldo no estado
            with state_lock:
                lab_state['real_balance'] = total_usdt
                lab_state['brl_balance'] = brl_balance - brl_to_use
            
            return total_usdt
            
        except Exception as e:
            print(f"❌ Erro na conversão BRL->USDT: {e}")
            # Tenta par inverso BRL/USDT
            try:
                ticker = ex(exchange.fetch_ticker, 'BRL/USDT')
                # Vende BRL para obter USDT
                order = ex(exchange.create_market_sell_order, 'BRL/USDT', brl_balance * 0.95)
                new_usdt = order['cost']  # USDT recebido
                print(f"✅ Conversão alternativa concluída! Recebido: ${new_usdt:.2f} USDT")
                send_telegram_message(f"🔄 Conversão BRL→USDT: ${new_usdt:.2f}")
                with state_lock:
                    lab_state['real_balance'] = new_usdt
                return new_usdt
            except:
                return usdt_balance
            
    except Exception as e:
        print(f"❌ Erro ao verificar saldos para conversão: {e}")
        return 0.0


def execute_real_trade(action, price, symbol, reason=None, amount_usdt=None):
    """Executa trade REAL na Binance.
    
    Args:
        action: 'buy' ou 'sell'
        price: Preço atual
        symbol: Par de trading
        reason: Motivo da venda (para evitar mensagem duplicada no Telegram)
        amount_usdt: Valor desejado de compra (Sandra Mode: $11/$22/$33/$8)
    """
    if not exchange or not API_KEY or not SECRET:
        print("⚠️ Modo real desabilitado: sem chaves API")
        return False
    
    try:
        with state_lock:
            strategy_key = lab_state.get('selected_strategy', 'aggressive')
            strategy = lab_state['strategies'][strategy_key]
            rsi_snapshot = float(lab_state.get('indicators', {}).get('rsi', 0.0) or 0.0)
            last_trade_snapshot = lab_state.get('last_trade_time', 0)

        def _safe_amount(symbol: str, amount: float) -> float:
            try:
                return float(exchange.amount_to_precision(symbol, amount))
            except Exception:
                return float(amount)

        def market_buy_by_quote(symbol: str, quote_usdt: float, price_hint: float):
            """Compra tentando gastar exatamente quote_usdt, com fallback para qty."""
            # 1) Tenta create_market_buy_order com quoteOrderQty (algumas versões aceitam amount=0)
            try:
                return ex(
                    exchange.create_market_buy_order,
                    symbol,
                    0,
                    {"quoteOrderQty": float(quote_usdt)},
                )
            except Exception:
                pass

            # 2) Tenta create_order (fallback alternativo)
            try:
                return ex(
                    exchange.create_order,
                    symbol,
                    'market',
                    'buy',
                    0,
                    None,
                    {"quoteOrderQty": float(quote_usdt)},
                )
            except Exception:
                # 3) Fallback: compra por quantidade com haircut
                qty = (float(quote_usdt) / float(price_hint)) * 0.995
                try:
                    qty = float(exchange.amount_to_precision(symbol, qty))
                except Exception:
                    pass
                return ex(exchange.create_market_buy_order, symbol, qty)

        if action == 'buy':
            desired = float(amount_usdt if amount_usdt is not None else AMOUNT_INVEST)

            # trava absoluta
            desired = min(desired, SANDRA["MAX_BET"])

            # Min notional (Binance): evita erro de exchange e não fura proteção
            min_notional = get_min_notional_usdt(symbol, fallback=10.0)
            with state_lock:
                drawdown_mode = bool(GLOBAL_STATS.get('drawdown_mode', False))

            if drawdown_mode and desired < min_notional:
                print(f"🛡️ Proteção ativa: ordem ${desired:.2f} < mínimo ${min_notional:.2f}. Não opera.")
                send_telegram_message(
                    f"🛡️ Proteção ativa: mínimo do par é ${min_notional:.2f}. Sandra NÃO fura a proteção."
                )
                return False
            if desired < min_notional:
                print(f"⚠️ Ordem abaixo do mínimo (${desired:.2f} < ${min_notional:.2f}). Pulando.")
                return False

            # COOLDOWN: somente na COMPRA (venda sempre libera)
            TRADE_COOLDOWN = 60  # segundos
            current_time = time.time()
            if current_time - last_trade_snapshot < TRADE_COOLDOWN:
                remaining = int(TRADE_COOLDOWN - (current_time - last_trade_snapshot))
                print(f"⏳ Cooldown ativo: aguarde {remaining}s antes da próxima COMPRA")
                return False

            # BUSCA SALDO REAL DA BINANCE (não usa cache)
            balance = ex(exchange.fetch_balance)
            usdt_balance = balance.get('free', {}).get('USDT', 0.0)
            print(f"💳 Saldo REAL da Binance: ${usdt_balance:.2f} USDT")
            with state_lock:
                lab_state['real_balance'] = usdt_balance  # Atualiza cache
                try:
                    lab_state.setdefault('user_info', {})['usdt_total'] = float(balance.get('total', {}).get('USDT', usdt_balance) or usdt_balance)
                except Exception:
                    pass
            
            # precisa ter pelo menos (aposta + taxa)
            required = desired * (1 + FEE_RATE)

            # Se não tem USDT suficiente, tenta converter BRL para USDT
            if usdt_balance < required:
                print(f"⚠️ USDT insuficiente (${usdt_balance:.2f} < ${required:.2f}). Tentando converter BRL...")
                usdt_balance = convert_brl_to_usdt()

                if usdt_balance < required:
                    print(f"⚠️ Saldo insuficiente: ${usdt_balance:.2f} < ${required:.2f}")
                    return False

            invest_amount = desired

            # Ordem de compra REAL
            # Preferência: gastar exatamente o invest_amount (quoteOrderQty), com fallback robusto.
            order = market_buy_by_quote(symbol=symbol, quote_usdt=invest_amount, price_hint=price)
            
            buy_price = order['average'] or price
            buy_qty = order['filled']
            buy_total = buy_price * buy_qty
            rsi = rsi_snapshot

            with state_lock:
                # Trade padrão (sempre com side + timestamp)
                trade = {
                    'timestamp': now_iso(),
                    'side': 'buy',
                    'symbol': symbol,
                    'price': buy_price,
                    'qty': buy_qty,
                    'fees': buy_total * FEE_RATE,
                    'mode': 'REAL',
                    'rsi': rsi,

                    # Campos legados (para telas antigas)
                    'time': now_sp().strftime('%H:%M:%S'),
                    'type': f'BUY REAL ({symbol})',
                    'order_id': order.get('id', ''),
                    'profit_pct': 0,
                }
                strategy['trades'].append(trade)

                # Posição padrão (prepara trailing persistente + custo real para PnL)
                strategy['position'] = {
                    'symbol': symbol,
                    'entry_price': buy_price,
                    'qty': buy_qty,
                    'entry_time': now_iso(),
                    'highest_price': buy_price,
                    'trail_active': False,

                    # custo real pra PnL correto (rateado se vender parcial)
                    'entry_cost_usdt': buy_total,
                    'entry_fee_usdt': buy_total * FEE_RATE,
                }
            
            print(f"💰 [{strategy['name']}] COMPRA REAL: {buy_qty:.4f} {symbol} @ ${buy_price:.4f}")
            taxa_est = buy_total * FEE_RATE

            # === RELATÓRIO VISUAL DE COMPRA (RECIBO) ===
            msg = (
                f"🔵 *COMPRA EXECUTADA* | {symbol}\n\n"
                f"💵 *Preço:* ${buy_price:.4f}\n"
                f"📦 *Qtd:* {buy_qty:.4f}\n"
                f"📉 *RSI:* {rsi:.1f}\n\n"
                f"🧾 *Financeiro:*\n"
                f"Investido: ${buy_total:.2f}\n"
                f"Taxa (est.): -${taxa_est:.3f}"
            )
            send_telegram_message(msg)
            
            # Atualiza cooldown
            with state_lock:
                lab_state['last_trade_time'] = time.time()
            
            return True

        elif action == 'sell':
            # Busca posição aberta para saber quanto vender
            if strategy['position']:
                qty = strategy['position']['qty']
                entry_price_original = strategy['position']['entry_price']
                
                # Sandra Mode: Vende quando a estratégia mandar (sem bloqueios)
                
                # Verifica se realmente temos a moeda na carteira antes de vender
                try:
                    balance = ex(exchange.fetch_balance)
                    coin = symbol.split('/')[0]  # Ex: 'XRP' de 'XRP/USDT'
                    coin_balance = balance['free'].get(coin, 0)
                    
                    if coin_balance <= 0:
                        print(f"⚠️ Nenhum saldo de {coin} na carteira!")
                        strategy['position'] = None
                        send_telegram_message(f"⚠️ *POSIÇÃO LIMPA*\\n\\nNão há {coin} na carteira para vender.")
                        return False
                    
                    # DETECTA DUST: saldo muito pequeno para vender (< $2 ou < 0.001 para BNB)
                    coin_value_usdt = coin_balance * price
                    min_qty = 0.001 if coin == 'BNB' else 0.0001  # Mínimos do Binance
                    
                    if coin_balance < min_qty or coin_value_usdt < 2:
                        print(f"🧹 DUST DETECTADO: {coin_balance:.8f} {coin} (${coin_value_usdt:.4f})")
                        print(f"🧹 Limpando posição fantasma - muito pequeno para vender")
                        strategy['position'] = None
                        send_telegram_message(f"🧹 *DUST LIMPO*\\n\\n{coin_balance:.8f} {coin} (${coin_value_usdt:.4f})\\nMuito pequeno para vender.")
                        return False
                    
                    # Se o saldo real é menor que o registrado, vende o que tem
                    if coin_balance < qty:
                        print(f"⚠️ Saldo real de {coin} menor que registrado: {coin_balance:.8f} < {qty:.8f}")
                        print(f"📤 Vendendo o saldo disponível: {coin_balance:.8f} {coin}")
                        qty = coin_balance
                    
                except Exception as e:
                    print(f"⚠️ Erro ao verificar saldo: {e}")

                qty = _safe_amount(symbol, qty)
                if qty <= 0:
                    print(f"🧹 Qty arredondada virou 0 para {symbol}. Limpando posição.")
                    with state_lock:
                        strategy['position'] = None
                    send_telegram_message("🧹 *DUST LIMPO*\n\nQuantidade inválida após precisão. Posição removida.")
                    return False

                order = ex(exchange.create_market_sell_order, symbol, qty)
                
                # Aguarda Binance processar a ordem e atualiza saldo
                print("⏳ Aguardando confirmação da Binance...")
                time.sleep(5)
                
                # Salva dados da posição ANTES de limpar
                pos = strategy.get('position') or {}
                entry_price = pos.get('entry_price', price)
                entry_qty = float(pos.get('qty', qty) or qty)
                entry_time = pos.get('entry_time', 'N/A')
                
                sell_price = order['average'] or price
                sell_qty = order['filled']
                rsi = rsi_snapshot
                
                # === CÁLCULO SANDRA (LUCRO LÍQUIDO REAL COM TAXAS, COM RATEIO) ===
                ratio = min(1.0, sell_qty / entry_qty) if entry_qty > 0 else 1.0

                entry_cost_full = float(pos.get('entry_cost_usdt', entry_price * entry_qty))
                entry_fee_full = float(pos.get('entry_fee_usdt', entry_cost_full * FEE_RATE))

                entry_cost = entry_cost_full * ratio
                entry_fee = entry_fee_full * ratio

                sell_gross = sell_price * sell_qty
                sell_fee = sell_gross * FEE_RATE
                sell_net = sell_gross - sell_fee

                lucro_liquido_usdt = sell_net - (entry_cost + entry_fee)
                base = (entry_cost + entry_fee)
                lucro_liquido_pct = (lucro_liquido_usdt / base) * 100 if base > 0 else 0.0
                taxas_totais = entry_fee + sell_fee
                
                # Atualiza saldo real
                try:
                    balance = ex(exchange.fetch_balance)
                    usdt_free = balance.get('free', {}).get('USDT', 0.0)
                    with state_lock:
                        lab_state['real_balance'] = usdt_free
                        try:
                            lab_state.setdefault('user_info', {})['usdt_total'] = float(balance.get('total', {}).get('USDT', usdt_free) or usdt_free)
                        except Exception:
                            pass
                    print(f"✅ Saldo confirmado: ${usdt_free:.2f} USDT")
                except Exception as e:
                    # Fallback: estima saldo usando o líquido da venda (sell_net)
                    with state_lock:
                        lab_state['real_balance'] = float(lab_state.get('real_balance', 0.0)) + float(sell_net)
                        estimated = lab_state['real_balance']
                    print(f"⚠️ Erro Binance ao confirmar saldo: {e} | Saldo estimado: ${estimated:.2f} USDT")

                with state_lock:
                    trade = {
                        'timestamp': now_iso(),
                        'side': 'sell',
                        'symbol': symbol,
                        'entry_price': entry_price,
                        'exit_price': sell_price,
                        'qty': sell_qty,
                        'fees': taxas_totais,
                        'net_profit_usdt': lucro_liquido_usdt,
                        'net_profit_pct': lucro_liquido_pct,
                        'reason': reason or '',
                        'mode': 'REAL',
                        'rsi': rsi,

                        # Campos legados
                        'time': now_sp().strftime('%H:%M:%S'),
                        'type': f'SELL REAL ({symbol})',
                        'price': sell_price,
                        'order_id': order.get('id', ''),
                        'profit_pct': lucro_liquido_pct,
                    }
                    strategy['trades'].append(trade)
                    strategy['position'] = None  # Limpa posição
                
                print(f"💵 [{strategy['name']}] VENDA REAL: {sell_qty} {symbol} @ ${sell_price:.2f}")
                print(f"📊 Compra: ${entry_price:.4f} → Venda: ${sell_price:.4f}")
                print(f"💰 Lucro LÍQUIDO: ${lucro_liquido_usdt:+.2f} ({lucro_liquido_pct:+.2f}%) | Taxas: ${taxas_totais:.3f}")
                
                # Atualiza PnL diário (Sandra Mode)
                with state_lock:
                    today = now_sp().strftime('%Y-%m-%d')
                    if lab_state['pnl']['date'] != today:
                        lab_state['pnl']['date'] = today
                        lab_state['pnl']['day_net'] = 0.0

                    lab_state['pnl']['day_net'] += lucro_liquido_usdt
                    lab_state['pnl']['total_net'] += lucro_liquido_usdt

                # === RELATÓRIO VISUAL DE VENDA (RECIBO FISCAL) ===
                icon = "✅" if lucro_liquido_usdt > 0 else "🔻"
                msg = (
                    f"{icon} *VENDA FINALIZADA* | {symbol}\n"
                    f"Motivo: _{reason or 'Sinal de Saída'}_ \n\n"
                    f"📥 Comprou: ${entry_price:.4f}\n"
                    f"📤 Vendeu:  ${sell_price:.4f}\n\n"
                    f"🧾 *Contabilidade:*\n"
                    f"Valor Bruto:  ${sell_gross:.2f}\n"
                    f"(-) Custo:    ${entry_cost:.2f}\n"
                    f"(-) Taxas:    ${taxas_totais:.3f} (Compra+Venda)\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"💰 *LÍQUIDO: ${lucro_liquido_usdt:+.2f} ({lucro_liquido_pct:+.2f}%)*\n\n"
                    f"📅 Dia: ${lab_state['pnl']['day_net']:+.2f} | Total: ${lab_state['pnl']['total_net']:+.2f}"
                )
                send_telegram_message(msg)
                
                # Atualiza streak Sandra (2 perdas = aperta)
                update_sandra_streak(lucro_liquido_usdt)
                
                return True

    except Exception as e:
        print(f"❌ ERRO ORDEM REAL: {e}")
        send_telegram_message(f"❌ *ERRO CRÍTICO NA EXECUÇÃO*\\n\\n{str(e)}")
        return False


def detect_existing_positions():
    """Detecta moedas já existentes na carteira e restaura posições."""
    if not exchange:
        return
    
    try:
        balance = ex(exchange.fetch_balance)
        with state_lock:
            selected = lab_state.get('selected_strategy', 'aggressive')
            # Se já tem posição registrada, não faz nada
            if lab_state['strategies'][selected]['position'] is not None:
                return
        
        # Procura por moedas na carteira que estão na WATCHLIST
        for symbol in WATCHLIST:
            coin = symbol.replace('/USDT', '')
            coin_balance = balance['total'].get(coin, 0.0)
            
            if coin_balance > 0:
                # Busca o preço atual
                ticker = ex(exchange.fetch_ticker, symbol)
                current_price = ticker['last']
                coin_value_usdt = coin_balance * current_price
                
                print(f"💰 Encontrado {coin}: {coin_balance:.8f} (${coin_value_usdt:.2f})")
                
                # Se tiver mais de $1 em valor, considera como posição aberta
                if coin_value_usdt >= 1:
                    # Estima o preço de entrada (usa o preço atual como fallback)
                    # Idealmente pegaria do histórico de trades
                    try:
                        trades = ex(exchange.fetch_my_trades, symbol, None, None, 5)
                        if trades:
                            # Pega o último trade de compra
                            buy_trades = [t for t in trades if t['side'] == 'buy']
                            if buy_trades:
                                entry_price = buy_trades[-1]['price']
                            else:
                                entry_price = current_price
                        else:
                            entry_price = current_price
                    except:
                        entry_price = current_price
                    
                    position = {
                        'entry_price': entry_price,
                        'qty': coin_balance,
                        'entry_time': now_iso(),
                        'symbol': symbol,
                        'highest_price': current_price,
                        'trail_active': False,
                        # estimativa (sem histórico completo) para manter PnL coerente
                        'entry_cost_usdt': float(current_price) * float(coin_balance),
                        'entry_fee_usdt': float(current_price) * float(coin_balance) * FEE_RATE,
                    }
                    with state_lock:
                        if lab_state['strategies'][selected]['position'] is None:
                            lab_state['strategies'][selected]['position'] = position
                    
                    profit_pct = ((current_price - entry_price) / entry_price) * 100
                    print(f"🔄 POSIÇÃO RESTAURADA: {coin_balance:.6f} {symbol} @ ${entry_price:.2f} (Lucro: {profit_pct:+.2f}%)")
                    # Não envia Telegram aqui para não spammar
                    return  # Só pode ter uma posição por vez
                    
    except Exception as e:
        print(f"⚠️ Erro ao detectar posições: {e}")


def rollover_pnl_if_new_day():
    """Zera PnL diário quando virar o dia, mesmo sem trades."""
    today = now_sp().strftime('%Y-%m-%d')
    pnl = lab_state.setdefault('pnl', {'date': today, 'day_net': 0.0, 'total_net': 0.0})
    if pnl.get('date') != today:
        pnl['date'] = today
        pnl['day_net'] = 0.0


def trading_loop():
    """Loop principal do sistema."""
    print("🚀 Loop de trading iniciado")
    load_lab_data()
    
    # Detecta posições existentes na carteira ao iniciar
    if lab_state['is_live'] and exchange:
        print("🔍 Verificando posições existentes na carteira...")
        detect_existing_positions()

    while True:
        try:
            rollover_pnl_if_new_day()

            # Define quais moedas vamos olhar nesta rodada
            # Se já tivermos uma posição aberta, focamos SÓ nela
            active_symbol = None
            
            # Verifica se tem posição real aberta
            with state_lock:
                is_live = lab_state.get('is_live', False)
                selected = lab_state.get('selected_strategy', 'aggressive')
                if is_live and lab_state['strategies'][selected]['position']:
                    active_symbol = lab_state['strategies'][selected]['position'].get('symbol', SYMBOL)
            
            # Verifica outras posições
            if not active_symbol:
                with state_lock:
                    for s_key in lab_state['strategies']:
                        if lab_state['strategies'][s_key]['position']:
                            active_symbol = lab_state['strategies'][s_key]['position'].get('symbol', SYMBOL)
                            break
            
            # === PRIORIDADE SANDRA: BTC/ETH só se tudo mais estiver ruim ===
            if active_symbol:
                target_coins = [active_symbol]
            else:
                # Começa com prioridade + secundárias
                target_coins = PRIORITY_COINS + SECONDARY_COINS
                
                # Só adiciona BTC/ETH se NENHUMA das outras estiver perto (RSI<40 e perto banda)
                cached_market = get_public_snapshot().get('market_overview', {}) or {}
                if cached_market:
                    any_near = False
                    for sym in target_coins:
                        data = cached_market.get(sym)
                        if not data:
                            continue
                        p = data.get('price')
                        r = data.get('rsi')
                        lb = data.get('bb_lower')
                        if p is not None and r is not None and lb is not None and (r < 40 and p <= lb * 1.02):
                            any_near = True
                            break
                    
                    if not any_near:
                        target_coins += LAST_RESORT
            
            # ATUALIZA SALDO ANTES de verificar sinais de compra
            if exchange and API_KEY:
                try:
                    balance = cached_fetch_balance(ttl_s=3.0)
                    usdt_free = balance.get('free', {}).get('USDT', 0.0)
                    usdt_total = balance.get('total', {}).get('USDT', 0.0)
                    with state_lock:
                        lab_state['real_balance'] = usdt_free
                        lab_state['brl_balance'] = balance.get('total', {}).get('BRL', 0.0)
                        lab_state.setdefault('user_info', {})
                        lab_state['user_info']['usdt_free'] = usdt_free
                        lab_state['user_info']['usdt_total'] = usdt_total
                except Exception as e:
                    print(f"⚠️ Erro ao atualizar saldo: {e}")

            for current_symbol in target_coins:
                # 1. Busca dados de mercado (agora inclui banda superior)
                price, rsi, bb_lower, bb_upper, vol_now, vol_avg = fetch_market_data(current_symbol, interval='5m', limit=60)
                
                # Alerta precoce — avisa antes de apertar o gatilho
                if price is not None and rsi is not None and bb_lower is not None:
                    if rsi < 40 and price <= bb_lower * 1.02:  # até 2% acima da banda
                        send_opportunity_alert(current_symbol, price, rsi, bb_lower)

                if price is not None:
                    with state_lock:
                        lab_state['current_price'] = price
                        lab_state['current_symbol'] = current_symbol # Atualiza o símbolo na interface
                        lab_state['last_update'] = datetime.now().strftime('%H:%M:%S')
                        # Hack para mostrar qual moeda está sendo analisada no frontend (usando status)
                        # lab_state['status'] = f'Analisando {current_symbol}...' 
                        
                        # Atualiza indicadores globais
                        lab_state['indicators']['rsi'] = rsi
                        lab_state['indicators']['bb_lower'] = bb_lower
                        lab_state['indicators']['bb_upper'] = bb_upper
                    
                    # Verifica BTC caindo >2% em 15min (cache 20s)
                    btc_is_dumping_15m = btc_drop_15m_cached()
                    
                    # Verifica BTC sangrando 3 dias (cache 1h)
                    btc_bleeding = btc_bleeding_3days_cached()
                    
                    # Atualiza Radar de Mercado + Diagnóstico
                    with state_lock:
                        selected_strategy = lab_state['selected_strategy']
                        strategy_position = lab_state['strategies'][selected_strategy]['position']
                    diagnostic = get_diagnostic(selected_strategy, price, rsi, bb_lower, strategy_position)

                    with state_lock:
                        lab_state['market_overview'][current_symbol] = {
                            'price': price,
                            'rsi': rsi,
                            'bb_lower': bb_lower,
                            'bb_upper': bb_upper,
                            'diagnostic': diagnostic,
                            'last_update': datetime.now().strftime('%H:%M:%S')
                        }
                        
                        # Atualiza diagnósticos separados por moeda
                        lab_state['diagnostics'][current_symbol] = diagnostic

                # 2. Lógica de Trading (Apenas se estiver RODANDO)
                with state_lock:
                    running = lab_state.get('running', False)
                if running:
                    with state_lock:
                        lab_state['status'] = f'Rodando 🚀 | {current_symbol}'

                    if price is not None:
                        # LOG DE ANÁLISE
                        with state_lock:
                            current_balance = lab_state.get('real_balance', 0.0)
                        print(f"🔎 {current_symbol}: RSI={rsi:.1f} | Preço=${price:.2f} | Saldo=${current_balance:.2f}")

                        # ========== 2.1 MODO REAL PRIMEIRO! ==========
                        with state_lock:
                            is_live = lab_state.get('is_live', False)
                            selected = lab_state.get('selected_strategy', 'aggressive')
                            strategy = lab_state['strategies'][selected]
                        if is_live:

                            if strategy['position'] is None:
                                # Sem posição - procura oportunidades de COMPRA (MODO SANDRA)
                                
                                # Atualiza controle de drawdown (perdeu 10% do topo?) usando EQUITY
                                with state_lock:
                                    usdt_total = float(lab_state.get('user_info', {}).get('usdt_total', lab_state.get('real_balance', 0.0)) or 0.0)
                                    equity = usdt_total  # Sem posição = só USDT (mais estável)
                                    if equity > GLOBAL_STATS['peak_balance']:
                                        GLOBAL_STATS['peak_balance'] = equity
                                        GLOBAL_STATS['drawdown_mode'] = False
                                    elif equity < GLOBAL_STATS['peak_balance'] * 0.9:
                                        GLOBAL_STATS['drawdown_mode'] = True
                                        print(f"🛡️ MODO PROTEÇÃO: Equity caiu 10% (${equity:.2f} < ${GLOBAL_STATS['peak_balance'] * 0.9:.2f})")
                                
                                # Obtém valor da aposta ($11, $22, $33 ou 0) com volume e BTC
                                invest_amount = check_strategy_signal(selected, price, rsi, bb_lower, current_symbol, vol_now, vol_avg, btc_is_dumping_15m, btc_bleeding)
                                
                                if invest_amount > 0 and current_balance >= invest_amount:
                                    print(f"🎯 SINAL DETECTADO: Investir ${invest_amount} em {current_symbol}!")

                                    result = execute_real_trade('buy', price, current_symbol, amount_usdt=invest_amount)

                                    if result:
                                        break  # Sai do loop de moedas após compra bem-sucedida
                                elif rsi < 45:
                                    print(f"⏸️ RSI baixo ({rsi:.1f}), aguardando condições de entrada...")
                            else:
                                # TEM POSIÇÃO - verifica VENDA
                                pos_symbol = strategy['position'].get('symbol', SYMBOL)
                                entry_price = strategy['position']['entry_price']
                                profit_pct = ((price - entry_price) / entry_price) * 100
                                
                                # Atualiza drawdown usando EQUITY (USDT + posição)
                                if pos_symbol == current_symbol:
                                    qty = strategy['position'].get('qty', 0)
                                    position_value = price * qty  # Valor atual da posição
                                    with state_lock:
                                        usdt_total = float(lab_state.get('user_info', {}).get('usdt_total', lab_state.get('real_balance', 0.0)) or 0.0)
                                        equity = usdt_total + position_value
                                        if equity > GLOBAL_STATS['peak_balance']:
                                            GLOBAL_STATS['peak_balance'] = equity
                                            GLOBAL_STATS['drawdown_mode'] = False
                                        elif equity < GLOBAL_STATS['peak_balance'] * 0.9:
                                            GLOBAL_STATS['drawdown_mode'] = True
                                            print(f"🛡️ MODO PROTEÇÃO: Equity caiu 10% (${equity:.2f} < ${GLOBAL_STATS['peak_balance'] * 0.9:.2f})")
                                
                                if pos_symbol == current_symbol:
                                    print(f"📍 POSIÇÃO ATIVA: {pos_symbol} | Entrada: ${entry_price:.2f} | Atual: ${price:.2f} | Lucro: {profit_pct:+.2f}%")
                                    
                                    # LOG DETALHADO antes de verificar venda
                                    bb_display = f"${bb_upper:.2f}" if bb_upper else "$0"
                                    print(f"🔍 [DEBUG] Verificando saída: RSI={rsi:.1f} | Lucro={profit_pct:+.2f}% | BB_Upper={bb_display}")
                                    
                                    # Passamos a posição inteira (strategy['position']) para Trailing Stop
                                    with state_lock:
                                        should_sell, reason = check_exit_signal(strategy['position'], price, rsi, bb_upper)
                                    
                                    if should_sell:
                                        # LOG COMPLETO ANTES DE VENDER
                                        print(f"⚠️ [VENDA AUTORIZADA]")
                                        print(f"   Moeda: {pos_symbol}")
                                        print(f"   Entrada: ${entry_price:.4f}")
                                        print(f"   Atual: ${price:.4f}")
                                        print(f"   Lucro: {profit_pct:+.2f}%")
                                        print(f"   RSI: {rsi:.1f}")
                                        print(f"   BB Upper: {bb_display}")
                                        
                                        # Salva RSI no estado para usar na mensagem
                                        with state_lock:
                                            lab_state['current_rsi'] = rsi
                                        
                                        # Venda com ticker "na hora" (evita slippage por preço defasado)
                                        price_now = price
                                        if exchange:
                                            try:
                                                ticker = ex(exchange.fetch_ticker, current_symbol)
                                                price_now = ticker.get('last') or price
                                            except Exception:
                                                pass

                                        # Passa reason para execute_real_trade (evita duplicação)
                                        execute_real_trade('sell', price_now, current_symbol, reason=reason)
                                        
                                        # Espera Binance processar antes de chamar IA
                                        print("⏳ Aguardando Binance estabilizar...")
                                        time.sleep(10)
                                        
                                        # IA só ajusta quando permitido (PROMPT Sandra: "só quando histórico mandar")
                                        if ENABLE_GPT_TUNING:
                                            print("🤖 IA analisando resultado para ajustar estratégia...")
                                            analyze_market_with_gpt(current_symbol, price, rsi, bb_lower, 'sell')

                else:
                    with state_lock:
                        lab_state['status'] = 'Em Standby (Monitorando...) zzz'
                
                # Pequena pausa entre moedas para não estourar limite da API
                time.sleep(2)

            # 3. Atualiza saldo real e informações da conta (SEMPRE, para o dashboard)
            if exchange and API_KEY:
                try:
                    # Busca informações detalhadas da conta (UID, Permissões)
                    # Nota: private_get_account é específico da Binance
                    account_info = cached_private_get_account(ttl_s=10.0)
                    uid = account_info.get('uid', 'Não informado')
                    account_type = account_info.get('accountType', 'SPOT')
                    can_trade = account_info.get('canTrade', False)

                    # Se estiver bloqueado, imprime aviso
                    if not can_trade:
                        print(f"⚠️ CONTA BLOQUEADA PELA BINANCE. Resposta: {account_info.get('canTrade')}")

                    # Busca saldos
                    balance = cached_fetch_balance(ttl_s=3.0)

                    # Tenta pegar saldo em USDT ou BRL
                    usdt_total = balance.get('total', {}).get('USDT', 0.0)
                    usdt_free = balance.get('free', {}).get('USDT', 0.0)
                    brl_balance = balance.get('total', {}).get('BRL', 0.0)

                    # Filtra saldos > 0 para exibir
                    relevant_balances = {}
                    total_brl = 0.0

                    # Pega cotação USDT/BRL para converter
                    try:
                        usdt_brl_ticker = ex(exchange.fetch_ticker, 'USDT/BRL')
                        usdt_brl_price = usdt_brl_ticker['last']
                    except:
                        usdt_brl_price = 5.50  # Fallback

                    for asset, amount in balance.get('total', {}).items():
                        if amount > 0:
                            relevant_balances[asset] = amount

                            # Calcula valor em BRL
                            if asset == 'BRL':
                                total_brl += amount
                            elif asset == 'USDT':
                                total_brl += amount * usdt_brl_price
                            else:
                                # Tenta buscar preço da moeda em USDT e converter para BRL
                                try:
                                    ticker = ex(exchange.fetch_ticker, f'{asset}/USDT')
                                    asset_usdt_price = ticker['last']
                                    total_brl += amount * asset_usdt_price * usdt_brl_price
                                except:
                                    pass  # Ignora se não conseguir

                    with state_lock:
                        lab_state.setdefault('user_info', {})
                        lab_state['user_info']['uid'] = uid
                        lab_state['user_info']['type'] = account_type
                        lab_state['user_info']['can_trade'] = can_trade
                        lab_state['user_info']['balances'] = relevant_balances
                        lab_state['user_info']['total_brl'] = total_brl
                        lab_state['user_info']['usdt_brl_rate'] = usdt_brl_price
                        lab_state['user_info']['usdt_free'] = usdt_free
                        lab_state['user_info']['usdt_total'] = usdt_total

                        # SEMPRE usa USDT livre como saldo principal para trading
                        lab_state['real_balance'] = usdt_free
                        lab_state['brl_balance'] = brl_balance

                except Exception as e:
                    # Em caso de erro, loga para diagnóstico
                    print(f"⚠️ Erro ao atualizar saldo da conta: {e}")
                    # Tenta atualizar pelo menos o saldo básico
                    try:
                        balance = ex(exchange.fetch_balance)
                        usdt_free = balance.get('free', {}).get('USDT', 0.0)
                        usdt_total = balance.get('total', {}).get('USDT', 0.0)
                        brl_total = balance.get('total', {}).get('BRL', 0.0)
                        with state_lock:
                            lab_state['real_balance'] = usdt_free
                            lab_state.setdefault('user_info', {})
                            lab_state['user_info']['balances'] = {
                                'USDT': usdt_total,
                                'BRL': brl_total
                            }
                            lab_state['user_info']['usdt_free'] = usdt_free
                            lab_state['user_info']['usdt_total'] = usdt_total
                    except Exception as e2:
                        print(f"❌ Erro crítico ao buscar saldo: {e2}")

            # 4. Salva estado
            save_lab_data()
            
            # 5. Verifica se está na hora de enviar relatório via Telegram
            check_and_send_reports()

            # time.sleep(5)  # Aguarda 5 segundos (Removido pois já tem sleep no loop de moedas)

        except Exception as e:
            print(f"❌ Erro no loop: {e}")
            time.sleep(10)


# Rotas da API
@app.route('/')
def index():
    return render_template('index.html')


@app.route('/charts')
def charts_page():
    """Página de gráficos das moedas."""
    return render_template('charts.html')


@app.route('/performance')
def performance_page():
    """Página de acompanhamento de performance."""
    return render_template('performance.html')


@app.route('/api/performance')
def get_performance():
    """Retorna estatísticas de performance das trades."""
    try:
        snap = get_public_snapshot()
        selected = snap.get('selected_strategy', 'aggressive')
        trades = (snap.get('strategies', {}).get(selected, {}) or {}).get('trades', [])
        
        def _is_sell_trade(t: dict) -> bool:
            side = t.get('side')
            if side:
                return side == 'sell'
            legacy_type = (t.get('type') or '').upper()
            return legacy_type.startswith('SELL')

        # Estatísticas básicas (SOMENTE VENDAS)
        sell_trades_list = [t for t in trades if _is_sell_trade(t)]
        total_trades = len(sell_trades_list)
        
        if total_trades == 0:
            return jsonify({
                'total_trades': 0,
                'winning_trades': 0,
                'losing_trades': 0,
                'win_rate': 0,
                'total_profit_pct': 0,
                'total_profit_brl': 0,
                'best_trade_pct': 0,
                'worst_trade_pct': 0,
                'avg_trade_pct': 0,
                'accumulated_profit': [],
                'trades': [],
                'goal_current': 0,
                'goal_target': 100
            })
        
        # Calcula métricas (SOMENTE VENDAS - BUY não conta)
        sell_trades = sell_trades_list

        def _to_float(v, default=0.0) -> float:
            try:
                return float(v)
            except Exception:
                return float(default)

        def _profit_usdt(t: dict) -> float:
            if t.get('net_profit_usdt') is not None:
                return _to_float(t.get('net_profit_usdt'), 0.0)
            return 0.0

        def _profit_pct(t: dict) -> float:
            if t.get('net_profit_pct') is not None:
                return _to_float(t.get('net_profit_pct'), 0.0)
            return _to_float(t.get('profit_pct', 0.0), 0.0)

        winning_trades = []
        losing_trades = []
        accumulated = []
        cumulative_usdt = 0.0

        profits_usdt = []
        profits_pct = []

        for trade in sell_trades:
            p_usdt = _profit_usdt(trade)
            p_pct = _profit_pct(trade)
            profits_usdt.append(p_usdt)
            profits_pct.append(p_pct)

            if p_usdt > 0:
                winning_trades.append(trade)
            else:
                losing_trades.append(trade)

            cumulative_usdt += p_usdt
            accumulated.append({
                'time': trade.get('exit_time', trade.get('time', '')),
                'profit': round(cumulative_usdt, 4)
            })

        total_profit_pct = sum(profits_pct)
        best_trade = max(profits_pct) if profits_pct else 0
        worst_trade = min(profits_pct) if profits_pct else 0
        avg_trade = total_profit_pct / total_trades if total_trades > 0 else 0
        win_rate = (len(winning_trades) / total_trades * 100) if total_trades > 0 else 0

        total_profit_usdt = sum(profits_usdt)
        usdt_brl = _to_float((snap.get('user_info', {}) or {}).get('usdt_brl_rate', 0.0), 0.0)
        total_profit_brl = (total_profit_usdt * usdt_brl) if usdt_brl > 0 else 0.0
        
        # Prepara trades para exibição (últimas 50 vendas)
        trades_display = []
        for t in sell_trades[-50:]:
            trades_display.append({
                'symbol': t.get('symbol', ''),
                'type': t.get('action', t.get('type', '')),
                'entry_price': t.get('entry_price', 0),
                'exit_price': t.get('exit_price', 0),
                'profit_pct': t.get('net_profit_pct', t.get('profit_pct', 0)),
                'profit_usdt': t.get('net_profit_usdt', 0),
                'entry_time': t.get('entry_time', t.get('time', '')),
                'exit_time': t.get('exit_time', ''),
                'reason': t.get('reason', '')
            })
        
        return jsonify({
            'total_trades': total_trades,
            'winning_trades': len(winning_trades),
            'losing_trades': len(losing_trades),
            'win_rate': round(win_rate, 1),
            'total_profit_pct': round(total_profit_pct, 2),
            'total_profit_brl': round(total_profit_brl, 2),
            'best_trade_pct': round(best_trade, 2),
            'worst_trade_pct': round(worst_trade, 2),
            'avg_trade_pct': round(avg_trade, 2),
            'accumulated_profit': accumulated,
            'trades': trades_display,
            'goal_current': round(total_profit_brl, 2),
            'goal_target': 100
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/send_report', methods=['POST'])
def send_report_now():
    """Envia relatório imediatamente via Telegram."""
    try:
        _require_api_token_if_configured()
        send_daily_report()
        return jsonify({'success': True, 'message': 'Relatório enviado!'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/report')
def get_report():
    """Retorna relatório em formato texto para visualização."""
    try:
        report = generate_market_report()
        return jsonify({'report': report})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/status')
def get_status():
    """Retorna estado completo do laboratório."""
    _require_api_token_if_configured()
    return jsonify(get_public_snapshot())


@app.route('/api/position')
def get_position():
    """Retorna informações da posição ativa com lucro em tempo real."""
    try:
        with state_lock:
            selected = lab_state.get('selected_strategy')
            position = copy.deepcopy(lab_state.get('strategies', {}).get(selected, {}).get('position'))
            cached_price = lab_state.get('current_price')
            is_drawdown = bool(GLOBAL_STATS.get('drawdown_mode', False))
        
        if not position:
            return jsonify({'has_position': False})
        
        symbol = position.get('symbol', SYMBOL)
        entry_price = position.get('entry_price', 0)
        qty = position.get('qty', 0)
        entry_time = position.get('entry_time', '')
        
        # Busca preço atual
        current_price = cached_price if cached_price is not None else entry_price
        
        # Tenta pegar preço atualizado da API
        if exchange:
            try:
                ticker = ex(exchange.fetch_ticker, symbol)
                current_price = ticker['last']
            except:
                pass
        
        # Calcula lucro/prejuízo
        profit_pct = ((current_price - entry_price) / entry_price) * 100 if entry_price > 0 else 0
        profit_value = (current_price - entry_price) * qty
        
        # Calcula metas (CONFIGURAÇÃO SANDRA MODE REAL)
        take_profit_price = entry_price * (1 + SANDRA["TP_SLOW"] / 100)  # TP_SLOW = 5%
        stop_pct = SANDRA["STOP_DRAWDOWN"] if is_drawdown else SANDRA["STOP_BASE"]  # -2% ou -3%
        stop_loss_price = entry_price * (1 + stop_pct / 100)
        
        # Valor da posição
        position_value = current_price * qty
        entry_value = entry_price * qty
        
        return jsonify({
            'has_position': True,
            'symbol': symbol,
            'entry_price': entry_price,
            'current_price': current_price,
            'qty': qty,
            'entry_time': entry_time,
            'profit_pct': profit_pct,
            'profit_value': profit_value,
            'take_profit_price': take_profit_price,
            'stop_loss_price': stop_loss_price,
            'position_value': position_value,
            'entry_value': entry_value,
            'distance_to_tp': ((take_profit_price - current_price) / current_price) * 100,
            'distance_to_sl': ((current_price - stop_loss_price) / current_price) * 100
        })
        
    except Exception as e:
        return jsonify({'has_position': False, 'error': str(e)})


@app.route('/api/clear-position', methods=['POST'])
def clear_position():
    """Limpa posição manualmente (para emergências como dust)."""
    _require_api_token_if_configured()
    try:
        with state_lock:
            selected = lab_state['selected_strategy']
            strategy = lab_state['strategies'][selected]

            old_position = strategy.get('position')
            strategy['position'] = None

        save_lab_data()
        
        if old_position:
            symbol = old_position.get('symbol', 'N/A')
            qty = old_position.get('qty', 0)
            print(f"🧹 POSIÇÃO LIMPA MANUALMENTE: {qty} {symbol}")
            send_telegram_message(f"🧹 *POSIÇÃO LIMPA MANUALMENTE*\\n\\n{qty} {symbol}")
            return jsonify({'success': True, 'message': f'Posição limpa: {qty} {symbol}'})
        else:
            return jsonify({'success': True, 'message': 'Nenhuma posição ativa para limpar'})
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/chart/<symbol>')
def get_chart_data(symbol):
    """Retorna dados de velas e indicadores para gráfico."""
    try:
        # Converte símbolo (BTC-USDT -> BTC/USDT)
        symbol_clean = symbol.replace('-', '/')
        
        if not exchange:
            return jsonify({'error': 'Exchange não conectada'}), 500
        
        # Busca últimas 100 velas de 5 minutos
        # USANDO REQUESTS DIRETAMENTE (API PÚBLICA) PARA EVITAR ERRO DE CHAVE
        try:
            url = 'https://api.binance.com/api/v3/klines'
            params = {'symbol': symbol_clean.replace('/', ''), 'interval': '5m', 'limit': 100}
            raw_data = _http_get_json(url, params=params, timeout=10, retries=2)
            # Converte formato da API (strings) para formato CCXT (floats)
            ohlcv = []
            for row in raw_data:
                ohlcv.append([
                    row[0],          # Time
                    float(row[1]),   # Open
                    float(row[2]),   # High
                    float(row[3]),   # Low
                    float(row[4]),   # Close
                    float(row[5])    # Volume
                ])
        except Exception as e:
            print(f"❌ Erro ao buscar dados públicos para {symbol_clean}: {e}")
            raise e
        
        # Formata dados
        candles = []
        
        # Formata dados
        candles = []
        closes = []
        for candle in ohlcv:
            candles.append({
                'time': candle[0],  # timestamp
                'open': candle[1],
                'high': candle[2],
                'low': candle[3],
                'close': candle[4],
                'volume': candle[5]
            })
            closes.append(candle[4])
        
        # Calcula indicadores
        rsi = calculate_rsi(closes)
        upper, sma, lower = calculate_bollinger(closes)
        
        # Calcula RSI histórico (últimos 50 pontos)
        rsi_history = []
        for i in range(50, len(closes)):
            rsi_val = calculate_rsi(closes[:i+1])
            rsi_history.append({
                'time': ohlcv[i][0],
                'value': rsi_val
            })
        
        # Calcula Bollinger histórico
        bb_history = []
        for i in range(20, len(closes)):
            u, m, l = calculate_bollinger(closes[:i+1])
            bb_history.append({
                'time': ohlcv[i][0],
                'upper': u,
                'middle': m,
                'lower': l
            })
        
        return jsonify({
            'symbol': symbol_clean,
            'candles': candles[-50:],  # Últimas 50 velas
            'current_price': closes[-1],
            'rsi': {
                'current': rsi,
                'history': rsi_history[-50:]
            },
            'bollinger': {
                'upper': upper,
                'middle': sma,
                'lower': lower,
                'history': bb_history[-50:]
            },
            'last_update': datetime.now().strftime('%H:%M:%S')
        })
        
    except Exception as e:
        logging.error(f"Erro em get_chart_data: {e}")
        logging.error(traceback.format_exc())
        return jsonify({'error': str(e)}), 500


@app.route('/api/watchlist')
def get_watchlist():
    """Retorna lista de moedas monitoradas."""
    snapshot = get_public_snapshot()
    return jsonify({
        'watchlist': WATCHLIST,
        'market_overview': snapshot.get('market_overview', {})
    })


@app.route('/api/select_strategy', methods=['POST'])
def select_strategy():
    """Seleciona qual estratégia usar no modo real."""
    data = request.json
    strategy_key = data.get('strategy')

    if strategy_key in lab_state['strategies']:
        with state_lock:
            lab_state['selected_strategy'] = strategy_key
        save_lab_data()
        return jsonify({'success': True, 'selected': strategy_key})

    return jsonify({'success': False, 'error': 'Estratégia inválida'}), 400


@app.route('/api/toggle_live', methods=['POST'])
def toggle_live():
    """Liga/Desliga o modo real."""
    _require_api_token_if_configured()
    data = request.json
    is_live = data.get('is_live', False)

    if is_live and (not API_KEY or not SECRET):
        return jsonify({'success': False, 'error': 'Chaves API não configuradas'}), 400

    with state_lock:
        lab_state['is_live'] = is_live
    save_lab_data()

    status_text = "ATIVADO ✅" if is_live else "DESATIVADO 🔴"
    print(f"{'='*60}")
    print(f"🔥 MODO REAL {status_text}")
    print(f"{'='*60}")

    return jsonify({'success': True, 'is_live': is_live})


@app.route('/api/toggle_running', methods=['POST'])
def toggle_running():
    """Liga/Desliga o robô (Master Switch)."""
    _require_api_token_if_configured()
    data = request.json
    running = data.get('running', False)
    
    with state_lock:
        lab_state['running'] = running
    save_lab_data()
    
    print(f"🤖 ROBÔ {'LIGADO' if running else 'DESLIGADO'}")
    return jsonify({'success': True, 'running': running})


@app.route('/api/convert_brl', methods=['POST'])
def convert_brl_endpoint():
    """🔄 Converte BRL para USDT manualmente."""
    if not exchange or not API_KEY or not SECRET:
        return jsonify({'success': False, 'error': '❌ Chaves API não configuradas!'}), 400
    
    try:
        # Busca saldos atuais
        balance = ex(exchange.fetch_balance)
        brl_before = balance['total'].get('BRL', 0.0)
        usdt_before = balance['total'].get('USDT', 0.0)
        
        if brl_before < 10:
            return jsonify({'success': False, 'error': f'Saldo BRL muito baixo: R${brl_before:.2f}'}), 400
        
        # Converte
        new_usdt = convert_brl_to_usdt(min_brl=10)
        
        return jsonify({
            'success': True,
            'message': f'✅ Conversão realizada!',
            'brl_before': brl_before,
            'usdt_before': usdt_before,
            'usdt_after': new_usdt
        })
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/force_buy', methods=['POST'])
def force_buy():
    """⚡ COMPRA FORÇADA - Ignora indicadores, testa conexão com Binance."""
    _require_api_token_if_configured()
    if not exchange or not API_KEY or not SECRET:
        return jsonify({'success': False, 'error': '❌ Chaves API não configuradas!'}), 400
    
    data = request.json
    symbol = data.get('symbol', 'BTC/USDT')  # Padrão BTC/USDT
    amount_usd = 11.0  # Valor mínimo para teste
    
    try:
        # Regra Sandra: BTC sangrando 3 dias = não compra em lugar nenhum
        if btc_bleeding_3days_cached():
            return jsonify({'success': False, 'error': '🩸 BTC sangrando 3 dias. Sandra NÃO compra até voltar.'}), 400

        # Sandra: posição única
        with state_lock:
            strategy_key = lab_state['selected_strategy']
            if lab_state['strategies'][strategy_key].get('position'):
                return jsonify({'success': False, 'error': '📍 Já existe posição aberta. Sandra não abre duas.'}), 400

        print(f"{'='*60}")
        print(f"⚡ COMPRA FORÇADA INICIADA - {symbol}")
        print(f"{'='*60}")
        
        # Busca preço atual
        ticker = ex(exchange.fetch_ticker, symbol)
        current_price = ticker['last']
        
        # Calcula quantidade
        qty = amount_usd / current_price
        
        # Executa ordem de mercado
        order = ex(exchange.create_market_buy_order, symbol, qty)
        
        print(f"✅ ORDEM EXECUTADA!")
        print(f"   ID: {order['id']}")
        print(f"   Preço: ${order.get('average', current_price):.2f}")
        print(f"   Quantidade: {order['filled']}")
        
        # Notifica no Telegram
        msg = f"⚡ *COMPRA FORÇADA (TESTE)*\n\n🪙 Moeda: {symbol}\n💰 Preço: ${current_price:.2f}\n📦 Qtd: {order['filled']}\n🆔 Order ID: {order['id']}"
        send_telegram_message(msg)
        
        # Registra na estratégia ativa (padrão persistente)
        with state_lock:
            trade = {
                'timestamp': now_iso(),
                'side': 'buy',
                'symbol': symbol,
                'price': order.get('average', current_price),
                'qty': order['filled'],
                'fees': float(order.get('cost', amount_usd)) * FEE_RATE,
                'mode': 'REAL (TESTE)',
                'rsi': lab_state.get('indicators', {}).get('rsi', 0.0),

                'time': now_sp().strftime('%H:%M:%S'),
                'type': f'⚡ FORCE BUY ({symbol})',
                'order_id': order.get('id', ''),
                'profit_pct': 0,
            }
            lab_state['strategies'][strategy_key]['trades'].append(trade)

            buy_price = order.get('average', current_price)
            buy_total = float(order.get('cost', buy_price * float(order['filled'])))
            lab_state['strategies'][strategy_key]['position'] = {
                'symbol': symbol,
                'entry_price': buy_price,
                'qty': order['filled'],
                'entry_time': now_iso(),
                'highest_price': buy_price,
                'trail_active': False,
                'entry_cost_usdt': buy_total,
                'entry_fee_usdt': buy_total * FEE_RATE,
            }
        save_lab_data()
        
        return jsonify({
            'success': True,
            'message': f'✅ Compra executada! {order["filled"]} {symbol}',
            'order_id': order['id'],
            'price': order.get('average', current_price),
            'qty': order['filled']
        })
        
    except Exception as e:
        error_msg = str(e)
        print(f"❌ ERRO NA COMPRA FORÇADA: {error_msg}")
        send_telegram_message(f"❌ *ERRO NA COMPRA FORÇADA*\n\n{error_msg}")
        return jsonify({'success': False, 'error': error_msg}), 500


@app.route('/api/export_data')
def export_data():
    """Exporta todos os dados do usuário da Binance."""
    _require_api_token_if_configured()
    if not exchange or not API_KEY or not SECRET:
        return jsonify({'error': 'API não configurada'}), 400

    try:
        # 1. Informações da Conta (Saldo detalhado)
        account_balance = ex(exchange.fetch_balance)
        
        # 1.1 Informações da Conta (Dados brutos da Binance - Permissões, Comissões, etc)
        account_details = ex(exchange.private_get_account)

        # 2. Histórico de Trades (Últimos trades do símbolo atual)
        trades = ex(exchange.fetch_my_trades, SYMBOL)
        
        # 3. Ordens Abertas
        open_orders = ex(exchange.fetch_open_orders, SYMBOL)
        
        # 4. Todas as Ordens (Histórico)
        all_orders = ex(exchange.fetch_orders, SYMBOL)
        
        export_package = {
            'timestamp': datetime.now().isoformat(),
            'symbol': SYMBOL,
            'account_details_binance': account_details, # Dados brutos da conta
            'account_balance': account_balance,
            'my_trades': trades,
            'open_orders': open_orders,
            'order_history': all_orders,
            'note': 'Dados exportados via API Binance (CCXT)'
        }
        
        return jsonify(export_package)

    except Exception as e:
        print(f"❌ Erro ao exportar dados: {e}")
        # Retorna erro mas tenta enviar o que conseguiu ou mensagem clara
        return jsonify({'error': str(e)}), 500


# --- TELEGRAM BOT LISTENER (COMANDOS) ---

async def telegram_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *Bot Modo Sandra - Ativo!*\n\n"
        "Olá! Sou sua guarda-costas com cérebro de trader. "
        "Vou operar com sabedoria: ganhar devagar, perder menos, e fazer repique gordo quando der!\n\n"
        "💡 *Como posso te ajudar?*\n"
        "• Use /ajuda para ver todos os comandos\n"
        "• Use /status para ver o que estou analisando\n"
        "• Use /relatorio para análise completa do mercado\n"
        "• Ou apenas converse comigo digitando qualquer mensagem!\n\n"
        "📊 Modo: Apostas variáveis ($11/$22/$33)\n"
        "🛡️ Proteção: Trailing Stop ativo\n"
        "💰 Cálculo: Lucro líquido com taxas reais",
        parse_mode='Markdown'
    )

async def telegram_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *BOT MODO SANDRA - COMANDOS*\n\n"
        "📊 *Informações do Sistema:*\n"
        "/status - O que estou analisando agora\n"
        "/saldo - Seu saldo em BRL e USDT\n"
        "/posicao - Posição aberta (se houver)\n"
        "/moedas - Análise das 10 moedas\n"
        "/relatorio - Relatório completo do mercado\n"
        "/ia - Parâmetros da IA (use 'reset' para resetar)\n\n"
        "⚡ *Ações de Trading:*\n"
        "/comprar XRP - Força compra de uma moeda\n"
        "/converter - Converte BRL para USDT\n"
        "/ligar - Liga o bot automático\n"
        "/desligar - Desliga o bot automático\n\n"
        "💬 *Conversa com IA:*\n"
        "Envie qualquer mensagem para conversar comigo!\n"
        "Pergunte sobre o mercado, estratégias ou qualquer dúvida.\n\n"
        "🎯 *Modo Sandra Ativo:*\n"
        "• Apostas: $11 (normal), $22 (forte), $33 (ouro)\n"
        "• Trailing Stop: Deixa lucro correr acima de 5%\n"
        "• Proteção: Reduz aposta se perder 10%\n"
        "• Taxas: Calcula lucro líquido real (0.2%)",
        parse_mode='Markdown'
    )

async def telegram_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        snap = get_public_snapshot()
        msg = "📊 *STATUS DO MERCADO*\n\n"
        msg += f"🪙 *Moeda:* {snap.get('current_symbol', '---')}\n"
        msg += f"💰 *Preço:* ${float(snap.get('current_price', 0.0) or 0.0):.2f}\n"
        indicators = snap.get('indicators', {}) or {}
        msg += f"📉 *RSI:* {float(indicators.get('rsi', 0.0) or 0.0):.2f}\n"
        msg += f"🛡️ *Bandas:* {float(indicators.get('bb_lower', 0.0) or 0.0):.2f}\n\n"
        
        msg += f"⚙️ *Configuração:*\n"
        msg += f"Estratégia: {snap.get('selected_strategy', 'aggressive')}\n"
        msg += f"Modo: Trading Real 💰\n"
        msg += f"Status: {snap.get('status', '')}"
        
        await update.message.reply_text(msg, parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"❌ Erro ao buscar status: {str(e)}")

async def telegram_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        snap = get_public_snapshot()
        balances = (snap.get('user_info', {}) or {}).get('balances', {}) or {}
        msg = "💰 *SEU SALDO*\n\n"
        
        # Se não tem saldo no cache, busca direto da API
        if not balances:
            try:
                balance = await asyncio.to_thread(cached_fetch_balance, 5.0)
                balances = {}
                for asset, amount in balance['total'].items():
                    if amount > 0.0001:
                        balances[asset] = amount
                
                # Atualiza cache
                with state_lock:
                    lab_state['user_info']['balances'] = balances
            except Exception as e:
                msg += f"❌ Erro ao buscar saldo da Binance: {str(e)}\n\n"
                msg += "Verifique se a API está ativa e tem permissões de leitura."
                await update.message.reply_text(msg, parse_mode='Markdown')
                return
        
        if not balances:
            msg += "Nenhum saldo encontrado na conta."
        else:
            for coin, amount in balances.items():
                if amount > 0.0001:  # Só mostra saldos relevantes
                    msg += f"• *{coin}:* {amount:.4f}\n"
            
            # Total em BRL
            total_brl = float((snap.get('user_info', {}) or {}).get('total_brl', 0) or 0)
            if total_brl > 0:
                msg += f"\n📊 *Total em BRL:* R${total_brl:.2f}"
        
        await update.message.reply_text(msg, parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"❌ Erro ao buscar saldo: {str(e)}")


async def telegram_position(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mostra posição aberta atual."""
    try:
        snap = get_public_snapshot()
        strategy_key = snap.get('selected_strategy', 'aggressive')
        position = (snap.get('strategies', {}) or {}).get(strategy_key, {}).get('position')
        
        if not position:
            await update.message.reply_text("📍 *Nenhuma posição aberta no momento.*\n\nO bot está aguardando oportunidade de compra.", parse_mode='Markdown')
            return
        
        symbol = position.get('symbol', 'N/A')
        entry_price = position.get('entry_price', 0)
        qty = position.get('qty', 0)
        entry_time = position.get('entry_time', 'N/A')
        
        # Busca preço atual
        current_price = float(snap.get('current_price', entry_price) or entry_price)
        
        # Calcula lucro
        profit_pct = ((current_price - entry_price) / entry_price) * 100 if entry_price > 0 else 0
        profit_usd = (current_price - entry_price) * qty
        
        emoji = "📈" if profit_pct > 0 else "📉"
        
        msg = f"📍 *POSIÇÃO ABERTA*\n\n"
        msg += f"🪙 *Moeda:* {symbol}\n"
        msg += f"💵 *Entrada:* ${entry_price:.4f}\n"
        msg += f"📊 *Atual:* ${current_price:.4f}\n"
        msg += f"📦 *Quantidade:* {qty:.4f}\n"
        msg += f"{emoji} *Lucro:* {profit_pct:+.2f}% (${profit_usd:+.2f})\n"
        msg += f"🕐 *Desde:* {entry_time[:16] if len(entry_time) > 16 else entry_time}"
        
        await update.message.reply_text(msg, parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"❌ Erro: {str(e)}")


async def telegram_coins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mostra análise de todas as moedas."""
    try:
        msg = "🔍 *ANÁLISE DAS MOEDAS*\n\n"
        snap = get_public_snapshot()
        diagnostics = snap.get('diagnostics', {})
        market = snap.get('market_overview', {})
        
        if not market:
            await update.message.reply_text("⏳ Aguardando dados do mercado...")
            return
        
        opportunities = []
        
        for symbol in WATCHLIST:
            data = market.get(symbol, {})
            diag = diagnostics.get(symbol, "")
            
            if not data:
                continue
                
            price = data.get('price', 0)
            rsi = data.get('rsi', 0)
            bb_lower = data.get('bb_lower', 0)
            
            # Determina emoji
            if "COMPRA" in diag:
                emoji = "🟢"
                opportunities.append(symbol.replace('/USDT', ''))
            elif rsi < 40:
                emoji = "🟡"
            elif rsi > 70:
                emoji = "🔴"
            else:
                emoji = "⚪"
            
            coin = symbol.replace('/USDT', '')
            msg += f"{emoji} *{coin}*: RSI={rsi:.0f} | ${price:.2f}\n"
        
        # Adiciona PnL do dia e total
        selected = snap.get('selected_strategy', 'aggressive')
        trades = (snap.get('strategies', {}).get(selected, {}) or {}).get('trades', [])
        
        # PnL hoje
        today_start = now_sp().replace(hour=0, minute=0, second=0, microsecond=0)
        today_trades = []
        for t in trades:
            if t.get('side') != 'sell':
                continue
            dt = parse_iso_dt(t.get('timestamp'))
            if dt and dt >= today_start:
                today_trades.append(t)
        pnl_today = sum(t.get('profit_pct', 0) for t in today_trades)
        
        # PnL total
        def _is_sell_trade(t: dict) -> bool:
            side = t.get('side')
            if side:
                return side == 'sell'
            legacy_type = (t.get('type') or '').upper()
            return legacy_type.startswith('SELL')

        pnl_total = sum(t.get('profit_pct', 0) for t in trades if _is_sell_trade(t))
        
        msg += f"\n💰 PnL Hoje: {pnl_today:+.2f}% | Total: {pnl_total:+.2f}%\n\n"
        
        if opportunities:
            msg += f"🚨 *Oportunidades:* {', '.join(opportunities)}"
        else:
            msg += "😴 Nenhuma oportunidade agora"
        
        await update.message.reply_text(msg, parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"❌ Erro: {str(e)}")


async def telegram_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Envia relatório completo."""
    try:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action='typing')
        report = await asyncio.to_thread(generate_market_report)
        await update.message.reply_text(report, parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"❌ Erro: {str(e)}")


async def telegram_buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Força compra de uma moeda específica."""
    try:
        # Pega o argumento (moeda)
        args = context.args
        if not args:
            await update.message.reply_text("⚠️ Use: /comprar XRP (ou BTC, ETH, etc)")
            return
        
        coin = args[0].upper()
        symbol = f"{coin}/USDT"
        
        # Verifica se é uma moeda válida
        if symbol not in WATCHLIST:
            await update.message.reply_text(f"❌ Moeda inválida: {coin}\n\nMoedas disponíveis: XRP, ADA, DOGE, DOT, LINK, LTC, SOL, BNB, ETH, BTC")
            return

        # Regra Sandra: BTC sangrando 3 dias = não compra em lugar nenhum
        if await asyncio.to_thread(btc_bleeding_3days_cached):
            await update.message.reply_text("🩸 BTC sangrando 3 dias. Sandra NÃO compra até voltar.")
            return

        if not exchange:
            await update.message.reply_text("❌ API não conectada!")
            return

        # Sandra: posição única
        with state_lock:
            strategy_key = lab_state['selected_strategy']
            if lab_state['strategies'][strategy_key].get('position'):
                await update.message.reply_text("📍 Já tem posição aberta. Sandra não abre duas.")
                return

        # Dados do mercado (para respeitar SANDRA)
        price, rsi, bb_lower, bb_upper, vol_now, vol_avg = await asyncio.to_thread(
            fetch_market_data, symbol, '5m', 60
        )
        if price is None or rsi is None or bb_lower is None:
            await update.message.reply_text("⚠️ Não consegui puxar dados agora. Tenta de novo.")
            return

        # Atualiza indicadores do estado para o trade não herdar RSI de outra moeda
        with state_lock:
            lab_state.setdefault('indicators', {})
            lab_state['indicators']['rsi'] = rsi
            lab_state['indicators']['bb_lower'] = bb_lower
            lab_state['indicators']['bb_upper'] = bb_upper

        btc_is_dumping_15m = await asyncio.to_thread(btc_drop_15m_cached)
        btc_bleeding = await asyncio.to_thread(btc_bleeding_3days_cached)

        invest_amount = check_strategy_signal(
            strategy_key, price, rsi, bb_lower, symbol, vol_now, vol_avg, btc_is_dumping_15m, btc_bleeding
        )

        if invest_amount <= 0:
            diag = get_diagnostic(strategy_key, price, rsi, bb_lower, position=None)
            await update.message.reply_text(f"🙅‍♀️ Sem sinal pra {coin} agora.\n{diag}")
            return

        ok = await asyncio.to_thread(execute_real_trade, 'buy', price, symbol, None, invest_amount)
        if ok:
            await update.message.reply_text(f"✅ Compra enviada no padrão Sandra (${invest_amount:.0f}).")
        else:
            await update.message.reply_text("❌ Compra não executada (mínimo/saldo/proteção/cooldown).")
        
    except Exception as e:
        await update.message.reply_text(f"❌ Erro na compra: {str(e)}")


async def telegram_convert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Converte BRL para USDT."""
    try:
        await update.message.reply_text("🔄 Convertendo BRL para USDT...")
        
        result = await asyncio.to_thread(convert_brl_to_usdt, 10)
        
        if result > 0:
            await update.message.reply_text(f"✅ Conversão concluída!\n\n💰 Saldo USDT: ${result:.2f}")
        else:
            await update.message.reply_text("❌ Não foi possível converter. Verifique seu saldo BRL.")
    except Exception as e:
        await update.message.reply_text(f"❌ Erro: {str(e)}")


async def telegram_start_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Liga o bot automático."""
    with state_lock:
        lab_state['running'] = True
    save_lab_data()
    await update.message.reply_text("🟢 Bot LIGADO! Agora monitorando o mercado e executando trades automaticamente.")


async def telegram_stop_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Desliga o bot automático."""
    with state_lock:
        lab_state['running'] = False
    save_lab_data()
    await update.message.reply_text("🔴 Bot DESLIGADO! Use /ligar para reativar.")


async def telegram_ia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mostra parâmetros reais do Sandra Mode (SANDRA) ou reseta para padrão."""
    
    args = context.args if context.args else []
    
    if args and args[0].lower() == 'reset':
        with state_lock:
            # Reseta apenas o que é ajustável na prática (o resto é regra fixa)
            SANDRA["ENTRY_RSI"] = 35
            SANDRA["ENTRY_TOL"] = 0.01
            SANDRA["STOP_BASE"] = -3.0
            SANDRA["TP_SLOW"] = 5.0
            lab_state.setdefault('streak', {'wins': 0, 'losses': 0, 'tight': False})
            lab_state['streak']['tight'] = False

        await update.message.reply_text(
            "🔄 Parâmetros da Sandra resetados.\n"
            f"ENTRY_RSI={SANDRA['ENTRY_RSI']} | TOL={SANDRA['ENTRY_TOL']*100:.1f}% | STOP_BASE={SANDRA['STOP_BASE']}% | TP={SANDRA['TP_SLOW']}%"
        )
    else:
        # Mostra parâmetros atuais (reais)
        await update.message.reply_text(
            "🤖 Sandra Mode (parâmetros reais):\n"
            f"ENTRY_RSI={SANDRA['ENTRY_RSI']}\n"
            f"ENTRY_TOL={SANDRA['ENTRY_TOL']*100:.1f}%\n"
            f"STOP_BASE={SANDRA['STOP_BASE']}%\n"
            f"STOP_DRAWDOWN={SANDRA['STOP_DRAWDOWN']}%\n"
            f"TP_SLOW={SANDRA['TP_SLOW']}%\n"
            f"TRAIL_FAST={SANDRA['TRAIL_FAST']}%\n"
            f"MAX_BET=${SANDRA['MAX_BET']:.0f}\n\n"
            "Use /ia reset para voltar ao padrão."
        )


async def telegram_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Responde a mensagens de texto usando GPT com contexto do mercado."""
    user_message = update.message.text
    print(f"📩 Mensagem recebida de {update.effective_user.first_name}: {user_message}")

    if not get_openai_client():
        await update.message.reply_text("🧠 IA não configurada no servidor.")
        return

    try:
        # Envia "Digitando..."
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action='typing')

        snap = get_public_snapshot()
        
        # Constrói contexto do mercado atual
        market_context = "DADOS ATUAIS DO MERCADO (Use isso para responder):\n"
        market_overview = snap.get('market_overview', {}) or {}
        if market_overview:
            for symbol, data in market_overview.items():
                market_context += f"- {symbol}: Preço=${data['price']:.2f} | RSI={data['rsi']:.1f} | BB_Lower=${data['bb_lower']:.2f}\n"
        else:
            market_context += "Nenhum dado de mercado coletado ainda.\n"
            
        market_context += f"\nSaldo do Usuário: {float(snap.get('real_balance', 0) or 0):.2f}\n"
        market_context += f"Estratégia Ativa: {snap.get('selected_strategy', 'aggressive')}\n"
        
        # Adiciona regras da estratégia (SANDRA MODE)
        strategy_key = snap.get('selected_strategy', 'aggressive')
        is_live = bool(snap.get('is_live', False))
        
        # Regras reais do SANDRA
        strategy_rules = (
            f"Entrada: RSI<{SANDRA['ENTRY_RSI']} (5m) e preço ≤ BB lower +{SANDRA['ENTRY_TOL']*100:.0f}%.\n"
            f"$22: RSI<{SANDRA['STRONG_RSI']} e volume >20% da média.\n"
            f"$33: RSI<{SANDRA['GOLD_RSI']} e BTC -2%/15min.\n"
            f"Saída: vende RSI≥{SANDRA['SELL_RSI']} | TP {SANDRA['TP_SLOW']}% | trailing {SANDRA['TRAIL_FAST']}%."
        )
            
        market_context += f"Modo: Trading Real 🚀\n"
        market_context += f"Regras da Estratégia Atual: {strategy_rules}\n"

        system_prompt = (
            "Você é um assistente de trading experiente e útil conectado a um bot em tempo real.\n"
            "Você TEM acesso aos dados atuais do mercado fornecidos abaixo.\n"
            "Use esses dados para responder perguntas sobre preços, tendências e se vale a pena comprar/vender.\n"
            "IMPORTANTE: Se o usuário perguntar 'por que não comprou nada' ou 'por que não tem operações', "
            "verifique se o RSI atual atende às regras da estratégia. Se o RSI estiver alto (ex: > 30 ou > 45), "
            "explique que o mercado não está em ponto de compra segundo a estratégia.\n"
            "Também verifique se o Modo Real está ativado.\n"
            "Responda de forma concisa, direta e use emojis.\n\n"
            f"{market_context}"
        )
        
        async def _openai_chat_sync(system_prompt_text: str, user_message_text: str) -> str:
            def _call() -> str:
                return openai_text(
                    instructions=system_prompt_text,
                    user_input=user_message_text,
                    max_output_tokens=400,
                    temperature=0.3,
                )

            return await asyncio.to_thread(_call)

        reply = await _openai_chat_sync(system_prompt, user_message)
        await update.message.reply_text(reply)
    except Exception as e:
        print(f"❌ Erro na IA: {e}")
        await update.message.reply_text(f"❌ Erro na IA: {str(e)}")

def run_telegram_bot():
    """Inicia o bot do Telegram em modo de escuta (Polling)."""
    global telegram_app
    
    if not telegram_app:
        print("⚠️ Telegram app não inicializado")
        return
    
    print("Telegram Bot iniciando polling...")
    try:
        # IMPORTANTE: run_polling precisa rodar na thread principal (usa sinais)
        telegram_app.run_polling(allowed_updates=Update.ALL_TYPES)
    except Exception as e:
        print(f"Erro fatal no Telegram Bot: {e}")


if __name__ == '__main__':
    try:
        print("="*60)
        print("🏗️  LABORATÓRIO DE TRADING HÍBRIDO")
        print("="*60)
        print(f"API Key: {API_KEY[:8] + '...' if API_KEY else 'NÃO CONFIGURADO'}")
        print(f"Secret: {'✓ Configurado' if SECRET else '✗ Não configurado'}")
        print(f"Símbolo: {SYMBOL}")
        print("="*60)
        
        print("🌐 Iniciando servidor Flask na porta 5000...")
        
        # Flask em thread separada
        def run_flask():
            app.run(host='0.0.0.0', debug=False, port=5000, use_reloader=False, threaded=True)
        
        flask_thread = threading.Thread(target=run_flask, daemon=True)
        flask_thread.start()
        
        print("✅ Servidor Flask iniciado!")

        # Inicia thread de trading
        thread = threading.Thread(target=trading_loop, daemon=True)
        thread.start()

        # Se Telegram estiver configurado, roda no MAIN (necessário para polling/sinais)
        if TELEGRAM_TOKEN and TELEGRAM_TOKEN != 'your_telegram_token_here':
            print("Inicializando Telegram Bot...")
            telegram_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

            telegram_app.add_handler(CommandHandler("start", telegram_start))
            telegram_app.add_handler(CommandHandler("ajuda", telegram_help))
            telegram_app.add_handler(CommandHandler("help", telegram_help))
            telegram_app.add_handler(CommandHandler("status", telegram_status))
            telegram_app.add_handler(CommandHandler("saldo", telegram_balance))
            telegram_app.add_handler(CommandHandler("posicao", telegram_position))
            telegram_app.add_handler(CommandHandler("position", telegram_position))
            telegram_app.add_handler(CommandHandler("moedas", telegram_coins))
            telegram_app.add_handler(CommandHandler("coins", telegram_coins))
            telegram_app.add_handler(CommandHandler("relatorio", telegram_report))
            telegram_app.add_handler(CommandHandler("report", telegram_report))
            telegram_app.add_handler(CommandHandler("comprar", telegram_buy))
            telegram_app.add_handler(CommandHandler("buy", telegram_buy))
            telegram_app.add_handler(CommandHandler("converter", telegram_convert))
            telegram_app.add_handler(CommandHandler("convert", telegram_convert))
            telegram_app.add_handler(CommandHandler("ligar", telegram_start_bot))
            telegram_app.add_handler(CommandHandler("on", telegram_start_bot))
            telegram_app.add_handler(CommandHandler("desligar", telegram_stop_bot))
            telegram_app.add_handler(CommandHandler("off", telegram_stop_bot))
            telegram_app.add_handler(CommandHandler("ia", telegram_ia))
            telegram_app.add_handler(CommandHandler("ai", telegram_ia))
            telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, telegram_chat))

            print("Telegram pronto. Comandos ativos: /start /ajuda /status /saldo /posicao /moedas /relatorio /comprar /converter /ligar /desligar /ia")

            # Bloqueia aqui (main thread) — Flask + trading seguem em threads
            run_telegram_bot()
        else:
            print("Telegram desabilitado (token inválido)")

            # Mantém o processo principal vivo
            while True:
                time.sleep(60)
            
    except KeyboardInterrupt:
        print("\n⛔ Servidor interrompido pelo usuário")
    except Exception as e:
        logging.error(f"Erro fatal no main: {e}")
        logging.error(traceback.format_exc())
        print(f"❌ Erro fatal: {e}")
        import traceback as tb
        tb.print_exc()
        input("Pressione ENTER para sair...")
