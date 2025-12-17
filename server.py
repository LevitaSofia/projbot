import os
import json
import time
import random
import threading
from datetime import datetime
from flask import Flask, jsonify, render_template, request
from dotenv import load_dotenv
import ccxt
import numpy as np
import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
import asyncio
from openai import OpenAI
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

import logging
import traceback

# Configuração de Logs
logging.basicConfig(
    filename='sistema_trading.log',
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    encoding='utf-8'
)

# Carrega variáveis de ambiente
load_dotenv()

app = Flask(__name__)


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

# Parâmetros de estratégia AJUSTÁVEIS pela IA
STRATEGY_PARAMS = {
    'RSI_TARGET': 35,        # RSI para compra
    'TOLERANCE': 0.01,       # Tolerância da banda (1%)
    'STOP_LOSS': -3.0,       # Stop loss em %
    'TAKE_PROFIT': 5.0,      # Take profit em %
}

# Configuração OpenAI (inicialização lazy para evitar bloqueio SSL)
openai_client = None
_openai_initialized = False

def get_openai_client():
    """Inicializa OpenAI client sob demanda"""
    global openai_client, _openai_initialized
    if _openai_initialized:
        return openai_client
    _openai_initialized = True
    if OPENAI_API_KEY and OPENAI_API_KEY != 'your_openai_api_key_here':
        try:
            openai_client = OpenAI(api_key=OPENAI_API_KEY)
            print("🧠 OpenAI (GPT) Configurado")
        except Exception as e:
            print(f"⚠️ Erro ao configurar OpenAI: {e}")
            openai_client = None
    return openai_client

# Estado Global
lab_state = {
    'strategies': {
        'aggressive': {'name': 'Trading Real 💰', 'balance': 0.0, 'trades': [], 'position': None}
    },
    'selected_strategy': 'aggressive',  # Única estratégia - Trading Real
    'is_live': True,  # MODO REAL SEMPRE ATIVADO
    'running': True,  # Controle Mestre (ON/OFF) - Inicia LIGADO
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
    'last_trade_time': 0  # Cooldown para evitar trades em loop
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
            lab_state['strategies'] = data.get(
                'strategies', lab_state['strategies'])
            lab_state['selected_strategy'] = data.get(
                'selected_strategy', 'conservative')
            lab_state['is_live'] = data.get('is_live', False)
            lab_state['running'] = data.get('running', False)
            print("📂 Dados do laboratório carregados")
    except FileNotFoundError:
        print("📝 Criando novo laboratório")
        save_lab_data()


def save_lab_data():
    """Salva estado atual do laboratório."""
    data = {
        'strategies': lab_state['strategies'],
        'selected_strategy': lab_state['selected_strategy'],
        'is_live': lab_state['is_live'],
        'running': lab_state['running'],
        'last_save': datetime.now().isoformat()
    }
    with open('lab_data.json', 'w') as f:
        json.dump(data, f, indent=2)


def calculate_rsi(prices, period=14):
    """Calcula RSI."""
    if len(prices) < period:
        return 50

    deltas = np.diff(prices)
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)

    avg_gain = np.mean(gains[-period:])
    avg_loss = np.mean(losses[-period:])

    if avg_loss == 0:
        return 100

    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi


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

def send_telegram_message(message):
    """Envia mensagem para o Telegram."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID or TELEGRAM_TOKEN == 'your_telegram_token_here':
        print("⚠️ Telegram não configurado. Mensagem não enviada.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    
    try:
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code == 200:
            print("📨 Mensagem Telegram enviada com sucesso!")
        else:
            print(f"❌ Erro Telegram: {response.text}")
    except Exception as e:
        print(f"❌ Erro ao enviar Telegram: {e}")


# ==================== SISTEMA DE RELATÓRIOS AUTOMÁTICOS ====================

# Horários para enviar relatórios (formato 24h)
REPORT_HOURS = [8, 12, 18, 22]  # 8h, 12h, 18h, 22h
last_report_hour = -1  # Controle para não repetir relatório na mesma hora

def generate_market_report():
    """Gera relatório completo de todas as moedas."""
    report_lines = []
    report_lines.append("📊 *RELATÓRIO DO BOT DE TRADING*")
    report_lines.append(f"🕐 {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    report_lines.append("")
    
    # Status do bot
    status = "🟢 ATIVO" if lab_state.get('running') else "🔴 PARADO"
    mode = "💰 REAL" if lab_state.get('is_live') else "🧪 SIMULAÇÃO"
    report_lines.append(f"*Status:* {status} | {mode}")
    
    # Saldo
    usdt = lab_state.get('real_balance', 0)
    report_lines.append(f"*Saldo USDT:* ${usdt:.2f}")
    report_lines.append("")
    
    # Posição atual
    selected = lab_state.get('selected_strategy', 'aggressive')
    strategy = lab_state.get('strategies', {}).get(selected, {})
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
    
    for symbol in WATCHLIST:
        try:
            price, rsi, bb_lower, bb_upper = fetch_market_data(symbol)
            if price and rsi and bb_lower:
                tolerance = bb_lower * 0.01
                buy_limit = bb_lower + tolerance
                
                # Calcula distância do preço para a zona de compra
                dist_to_buy = ((price - buy_limit) / buy_limit) * 100
                
                # Determina emoji e status
                if rsi < 35 and price <= buy_limit:
                    emoji = "🟢"
                    status = "COMPRA!"
                    opportunities.append(symbol)
                elif rsi < 40 or dist_to_buy < 2:
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
        print(f"📨 Relatório enviado às {datetime.now().strftime('%H:%M')}")
        logging.info("Relatório diário enviado via Telegram")
    except Exception as e:
        print(f"❌ Erro ao enviar relatório: {e}")
        logging.error(f"Erro ao enviar relatório: {e}")


def send_opportunity_alert(symbol, price, rsi, bb_lower):
    """Envia alerta quando uma moeda está próxima de compra."""
    tolerance = bb_lower * 0.01
    buy_limit = bb_lower + tolerance
    dist = ((price - buy_limit) / buy_limit) * 100
    
    msg = f"""
🔔 *ALERTA DE OPORTUNIDADE*

*{symbol}* está se aproximando da zona de compra!

📊 RSI: {rsi:.1f} (precisa < 35)
💰 Preço: ${price:.2f}
📉 Banda Inferior: ${bb_lower:.2f}
🎯 Limite compra: ${buy_limit:.2f}
📏 Distância: {dist:+.1f}%

{"🟢 *CONDIÇÕES OK!*" if rsi < 35 and dist <= 0 else "⏳ Aguardando..."}
"""
    send_telegram_message(msg)


def check_and_send_reports():
    """Verifica se está na hora de enviar relatório."""
    global last_report_hour
    current_hour = datetime.now().hour
    
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
    if rsi < 35 and dist_to_band <= 1:
        status = "🟢 SINAL FORTE - Pronto para comprar!"
    elif rsi < 35:
        status = f"🟡 RSI OK, preço {dist_to_band:.1f}% acima da banda"
    elif dist_to_band <= 1:
        status = f"🟡 Preço OK, RSI={rsi:.1f} (precisa <35)"
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
    global STRATEGY_PARAMS
    
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
    
    # Parâmetros atuais
    params_atuais = f"RSI_TARGET={STRATEGY_PARAMS['RSI_TARGET']}, STOP={STRATEGY_PARAMS['STOP_LOSS']}%"
    
    prompt = f"""Você é chefe de estratégia agora, olha o último ciclo:
- Últimos 5 trades: {len(trades_perdidos)} perdidos acima de 2%, {len(trades_ganhos)} ganhos, RSI médio foi {rsi_medio:.1f}.
- Mercado: {market_context}
- Parâmetros atuais: {params_atuais}

Regras:
- Se perdeu 2 ou mais seguidos: diminui RSI pra 32, aumenta tolerância pra 0.5%, stop loss pra -2.5%.
- Se ganhou fácil em RSI <35: mantém tudo, só diz 'segura firme'.
- Se RSI >70 por 3 dias: vira conservadora — RSI 38, venda no primeiro 2%.

Responde EXATAMENTE assim (duas linhas):
Ação: ajuste ou mantém
Telegram: uma frase curta tipo 'IA mudou o plano — agora mais esperta'

Nada de enrolação."""

    try:
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "Você é um gestor de risco de trading. Responda APENAS no formato pedido."},
                {"role": "user", "content": prompt}
            ],
            max_tokens=100
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
            STRATEGY_PARAMS['RSI_TARGET'] = 32
            STRATEGY_PARAMS['TOLERANCE'] = 0.005
            STRATEGY_PARAMS['STOP_LOSS'] = -2.5
            
            print(f"🤖 IA AJUSTOU ESTRATÉGIA: RSI={STRATEGY_PARAMS['RSI_TARGET']}, Stop={STRATEGY_PARAMS['STOP_LOSS']}%")
            send_telegram_message(f"🤖 *IA ajustou estratégia*\\n\\n{telegram_msg}\\n\\nNovos params: RSI<{STRATEGY_PARAMS['RSI_TARGET']}, Stop {STRATEGY_PARAMS['STOP_LOSS']}%")
        else:
            if telegram_msg:
                send_telegram_message(f"🟢 {telegram_msg}")
        
        return content
        
    except Exception as e:
        print(f"❌ Erro GPT: {e}")
        return "🤖 Erro na análise de IA."

# ---------------------------------


# Lista de moedas para monitorar (ordenadas por preço aproximado - mais baratas primeiro)
WATCHLIST = [
    'XRP/USDT',    # ~$2
    'ADA/USDT',    # ~$1
    'DOGE/USDT',   # ~$0.40
    'DOT/USDT',    # ~$8
    'LINK/USDT',   # ~$25
    'LTC/USDT',    # ~$100
    'SOL/USDT',    # ~$200
    'BNB/USDT',    # ~$700
    'ETH/USDT',    # ~$4000
    'BTC/USDT',    # ~$90000
]

# Valor mínimo de ordem na Binance (em USDT)
MIN_ORDER_VALUE = 11.0

def fetch_market_data(symbol):
    """Busca dados de mercado para análise."""
    try:
        # Busca últimas 100 velas de 1 HORA (mais confiável)
        # Usando requests para evitar erro de chave
        url = 'https://api.binance.com/api/v3/klines'
        params = {'symbol': symbol.replace('/', ''), 'interval': '1h', 'limit': 100}
        r = requests.get(url, params=params, timeout=10, verify=False)
        r.raise_for_status()
        raw_data = r.json()
        
        closes = [float(candle[4]) for candle in raw_data]
        current_price = closes[-1]

        rsi = calculate_rsi(closes)
        upper, sma, lower = calculate_bollinger(closes)

        return current_price, rsi, lower, upper
    except Exception as e:
        print(f"❌ Erro ao buscar dados ({symbol}): {e}")
        return None, None, None, None


def check_strategy_signal(strategy_name, price, rsi, bb_lower):
    """Verifica se dá sinal de compra - USA PARÂMETROS DINÂMICOS DA IA."""
    # Pega parâmetros ajustáveis (podem ser modificados pela IA)
    rsi_target = STRATEGY_PARAMS['RSI_TARGET']
    tolerance_pct = STRATEGY_PARAMS['TOLERANCE']
    
    tolerance = bb_lower * tolerance_pct
    
    # CONDIÇÃO 1: RSI baixo
    rsi_low = rsi < rsi_target
    
    # CONDIÇÃO 2: Preço DEVE estar na banda inferior ou abaixo
    price_at_bottom = price <= bb_lower + tolerance
    
    # SÓ COMPRA SE AMBAS AS CONDIÇÕES FOREM VERDADEIRAS
    should_buy = rsi_low and price_at_bottom
    
    if should_buy:
        print(f"🎯 SINAL DE COMPRA: RSI={rsi:.1f} (<{rsi_target}) + Preço na banda inferior!")
    
    return should_buy


def get_diagnostic(strategy_name, price, rsi, bb_lower, position=None):
    """Gera diagnóstico legível explicando por que não está comprando/vendendo."""
    
    # Se tem posição aberta, calcula lucro
    if position:
        entry_price = position.get('entry_price', price)
        profit_pct = ((price - entry_price) / entry_price) * 100
        emoji = "📈" if profit_pct > 0 else "📉"
        return f"{emoji} COMPRADO (Lucro: {profit_pct:+.2f}%)"
    
    # Verifica saldo primeiro
    usdt_balance = lab_state.get('real_balance', 0.0)
    if usdt_balance < MIN_ORDER_VALUE:
        return f"💸 SALDO BAIXO (${usdt_balance:.2f} < ${MIN_ORDER_VALUE})"
    
    # Analisa condições de compra (ESTRATÉGIA EQUILIBRADA)
    issues = []
    rsi_target = 35  # Equilibrado
    tolerance = bb_lower * 0.01  # 1% tolerância
    
    # Se RSI E preço estão bons, é sinal forte
    if rsi < rsi_target and price <= bb_lower + tolerance:
        return "🚨 RSI < 35 + BANDA INFERIOR! COMPRA!"
    
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


def check_exit_signal(entry_price, current_price, rsi, bb_upper=None):
    """
    Verifica sinal de saída - CONSERVADOR para não vender precipitadamente.
    
    REGRAS:
    1. NUNCA vende com RSI baixo (< 40) - mercado sobrevendido, pode subir
    2. NUNCA vende com prejuízo EXCETO stop loss
    3. Stop Loss só em -3% (mais tolerante)
    4. Take Profit precisa de lucro REAL + RSI alto
    """
    profit_pct = ((current_price - entry_price) / entry_price) * 100
    
    # === PROTEÇÃO ANTI-PRECIPITAÇÃO ===
    # Se RSI está baixo, mercado pode subir - NÃO VENDE (exceto stop loss grave)
    if rsi < 40 and profit_pct > -5:
        print(f"⏳ RSI baixo ({rsi:.1f}) - Aguardando recuperação...")
        return False
    
    # Tolerância para banda superior
    price_at_upper = False
    if bb_upper:
        tolerance = bb_upper * 0.01
        price_at_upper = current_price >= bb_upper - tolerance

    should_sell = False
    reason = []
    
    # === TAKE PROFIT (só com RSI ALTO = confirmação de topo) ===
    # Taxa Binance: 0.1% compra + 0.1% venda = 0.2% total
    
    if profit_pct >= 5.0:  # 5% = vende sempre (lucro excelente)
        should_sell = True
        reason.append(f"🎯 LUCRO FORTE {profit_pct:.1f}%!")
    elif profit_pct >= 3.5 and rsi > 55:  # 3.5% + RSI subindo
        should_sell = True
        reason.append(f"📈 Lucro {profit_pct:.1f}% + RSI ({rsi:.0f})")
    elif profit_pct >= 2.5 and rsi > 60:  # 2.5% + RSI alto
        should_sell = True
        reason.append(f"💰 Lucro {profit_pct:.1f}% + RSI bom ({rsi:.0f})")
    elif profit_pct >= 2.0 and rsi > 65:  # 2% + RSI muito alto
        should_sell = True
        reason.append(f"📊 Lucro {profit_pct:.1f}% + RSI forte ({rsi:.0f})")
    elif profit_pct >= 1.5 and rsi > 70:  # 1.5% + RSI sobrecomprado
        should_sell = True
        reason.append(f"🔥 Lucro {profit_pct:.1f}% + RSI extremo ({rsi:.0f})")
    elif price_at_upper and profit_pct >= 1.5 and rsi > 55:  # Banda superior + lucro + RSI ok
        should_sell = True
        reason.append(f"🔴 BANDA SUPERIOR + Lucro {profit_pct:.1f}%")
    
    # === STOP LOSS (usa parâmetro dinâmico da IA) ===
    stop_loss = STRATEGY_PARAMS['STOP_LOSS']
    if profit_pct <= stop_loss:
        should_sell = True
        reason.append(f"🛑 STOP LOSS {profit_pct:.1f}% (limite: {stop_loss}%)")
    
    # === STOP LOSS DE EMERGÊNCIA ===
    # Se perdendo muito, vende independente de RSI
    if profit_pct <= -5.0:
        should_sell = True
        reason.append(f"🚨 EMERGÊNCIA {profit_pct:.1f}% - Saindo!")
    
    if should_sell:
        print(f"🔔 SINAL DE VENDA: {', '.join(reason)}")
    else:
        # Log para debug quando NÃO vende
        if profit_pct < 0:
            print(f"⏳ Aguardando: Prejuízo {profit_pct:.1f}% (Stop em {stop_loss}%) | RSI={rsi:.1f}")
        elif profit_pct > 0 and profit_pct < 1.5:
            print(f"⏳ Aguardando: Lucro {profit_pct:.1f}% (Meta mínima 1.5%) | RSI={rsi:.1f}")
    
    return should_sell


def convert_brl_to_usdt(min_brl=20):
    """Converte BRL para USDT automaticamente quando necessário."""
    try:
        balance = exchange.fetch_balance()
        brl_balance = balance['total'].get('BRL', 0.0)
        usdt_balance = balance['total'].get('USDT', 0.0)
        
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
            ticker = exchange.fetch_ticker('USDT/BRL')
            usdt_price_brl = ticker['last']  # Preço de 1 USDT em BRL
            
            # Calcula quantidade de USDT a comprar (usando 95% do BRL para taxas)
            brl_to_use = brl_balance * 0.95
            usdt_qty = brl_to_use / usdt_price_brl
            
            print(f"🔄 Convertendo R${brl_to_use:.2f} para ~${usdt_qty:.2f} USDT...")
            
            # Executa ordem de compra de USDT com BRL
            order = exchange.create_market_buy_order('USDT/BRL', usdt_qty)
            
            new_usdt = order['filled']
            total_usdt = usdt_balance + new_usdt
            print(f"✅ Conversão concluída! Recebido: ${new_usdt:.2f} USDT | Total: ${total_usdt:.2f}")
            
            # Notifica no Telegram
            msg = f"🔄 *CONVERSÃO BRL → USDT*\n\n💵 Convertido: R${brl_to_use:.2f}\n💰 Recebido: ${new_usdt:.2f} USDT\n📊 Saldo total: ${total_usdt:.2f} USDT"
            send_telegram_message(msg)
            
            # Atualiza saldo no estado
            lab_state['real_balance'] = total_usdt
            lab_state['brl_balance'] = brl_balance - brl_to_use
            
            return total_usdt
            
        except Exception as e:
            print(f"❌ Erro na conversão BRL->USDT: {e}")
            # Tenta par inverso BRL/USDT
            try:
                ticker = exchange.fetch_ticker('BRL/USDT')
                # Vende BRL para obter USDT
                order = exchange.create_market_sell_order('BRL/USDT', brl_balance * 0.95)
                new_usdt = order['cost']  # USDT recebido
                print(f"✅ Conversão alternativa concluída! Recebido: ${new_usdt:.2f} USDT")
                send_telegram_message(f"🔄 Conversão BRL→USDT: ${new_usdt:.2f}")
                lab_state['real_balance'] = new_usdt
                return new_usdt
            except:
                return usdt_balance
            
    except Exception as e:
        print(f"❌ Erro ao verificar saldos para conversão: {e}")
        return 0.0


def execute_real_trade(action, price, symbol):
    """Executa trade REAL na Binance."""
    if not exchange or not API_KEY or not SECRET:
        print("⚠️ Modo real desabilitado: sem chaves API")
        return False
    
    # COOLDOWN: Espera 60 segundos entre trades para evitar loop
    TRADE_COOLDOWN = 60  # segundos
    current_time = time.time()
    last_trade = lab_state.get('last_trade_time', 0)
    
    if current_time - last_trade < TRADE_COOLDOWN:
        remaining = int(TRADE_COOLDOWN - (current_time - last_trade))
        print(f"⏳ Cooldown ativo: aguarde {remaining}s antes do próximo trade")
        return False

    try:
        strategy_key = lab_state['selected_strategy']
        strategy = lab_state['strategies'][strategy_key]

        if action == 'buy':
            # BUSCA SALDO REAL DA BINANCE (não usa cache)
            balance = exchange.fetch_balance()
            usdt_balance = balance['USDT']['free']
            print(f"💳 Saldo REAL da Binance: ${usdt_balance:.2f} USDT")
            lab_state['real_balance'] = usdt_balance  # Atualiza cache
            
            # Se não tem USDT suficiente, tenta converter BRL para USDT
            if usdt_balance < 10.5:  # Margem pra taxa
                print(f"⚠️ Saldo USDT baixo (${usdt_balance:.2f}). Tentando converter BRL...")
                usdt_balance = convert_brl_to_usdt()
                
                # Se ainda não tem saldo após conversão - apenas loga, não envia Telegram repetido
                if usdt_balance < 10.5:
                    print(f"⚠️ Saldo insuficiente: ${usdt_balance:.2f} < $11.00")
                    return False
            
            # Usa o valor disponível (máximo de AMOUNT_INVEST ou saldo disponível)
            invest_amount = min(AMOUNT_INVEST, usdt_balance * 0.95)  # 95% para taxa
            print(f"💰 AMOUNT_INVEST={AMOUNT_INVEST}, usdt_balance*0.95={usdt_balance * 0.95:.2f}, invest_amount={invest_amount:.2f}")
            
            if invest_amount < 10.5:  # Margem pra taxa
                print(f"⚠️ Valor de investimento muito baixo: ${invest_amount:.2f}")
                return False
            
            qty = invest_amount / price
            
            # Ordem de compra REAL
            order = exchange.create_market_buy_order(symbol, qty)
            
            buy_price = order['average'] or price
            buy_qty = order['filled']
            buy_total = buy_price * buy_qty
            rsi = lab_state['indicators']['rsi']

            trade = {
                'time': datetime.now().strftime('%H:%M:%S'),
                'type': f'BUY REAL ({symbol})',
                'price': buy_price,
                'qty': buy_qty,
                'order_id': order['id'],
                'mode': 'REAL',
                'rsi': rsi,  # Salva RSI para análise da IA
                'profit_pct': 0  # Será atualizado na venda
            }
            strategy['trades'].append(trade)
            # Salva o símbolo na posição para saber o que vender depois
            strategy['position'] = {
                'entry_price': buy_price, 
                'qty': buy_qty, 
                'entry_time': datetime.now().isoformat(), 
                'symbol': symbol
            }
            
            print(f"💰 [{strategy['name']}] COMPRA REAL: {buy_qty:.4f} {symbol} @ ${buy_price:.4f}")
            
            # Notificação Telegram COMPLETA
            msg = (
                f"💰 *COMPRA REAL*\\n\\n"
                f"🪙 Moeda: {symbol}\\n"
                f"💵 Preço: ${buy_price:.4f}\\n"
                f"📊 Qtd: {buy_qty:.4f}\\n"
                f"💰 Total: ${buy_total:.2f}\\n"
                f"📈 RSI: {rsi:.1f}"
            )
            send_telegram_message(msg)
            
            # Atualiza cooldown
            lab_state['last_trade_time'] = time.time()
            
            return True

        elif action == 'sell':
            # Busca posição aberta para saber quanto vender
            if strategy['position']:
                qty = strategy['position']['qty']
                entry_price_original = strategy['position']['entry_price']
                
                # === PROTEÇÃO ANTI-PRECIPITAÇÃO NA VENDA ===
                current_rsi = lab_state['indicators'].get('rsi', 50)
                profit_check = ((price - entry_price_original) / entry_price_original) * 100
                
                # BLOQUEIO: RSI < 40 = mercado sobrevendido, NÃO VENDE (exceto emergência)
                if current_rsi < 40 and profit_check > -5:
                    print(f"🛡️ VENDA BLOQUEADA! RSI={current_rsi:.1f} (muito baixo)")
                    print(f"   O mercado está sobrevendido, pode subir!")
                    print(f"   Lucro atual: {profit_check:.2f}%")
                    send_telegram_message(
                        f"🛡️ *VENDA BLOQUEADA*\\n\\n"
                        f"RSI muito baixo: {current_rsi:.1f}\\n"
                        f"Mercado pode subir!\\n"
                        f"Lucro atual: {profit_check:+.2f}%"
                    )
                    return False
                
                # BLOQUEIO: Prejuízo < 3% e não é emergência
                if profit_check < 0 and profit_check > -3:
                    print(f"🛡️ VENDA BLOQUEADA! Prejuízo {profit_check:.2f}% < Stop Loss (-3%)")
                    print(f"   Aguardando recuperação ou stop loss...")
                    return False
                
                # Verifica se realmente temos a moeda na carteira antes de vender
                try:
                    balance = exchange.fetch_balance()
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
                
                order = exchange.create_market_sell_order(symbol, qty)
                
                # Aguarda Binance processar a ordem e atualiza saldo
                print("⏳ Aguardando confirmação da Binance...")
                time.sleep(5)
                
                # Salva dados da posição ANTES de limpar
                entry_price = strategy['position']['entry_price'] if strategy.get('position') else price
                entry_qty = strategy['position'].get('qty', qty) if strategy.get('position') else qty
                entry_time = strategy['position'].get('entry_time', 'N/A') if strategy.get('position') else 'N/A'
                
                sell_price = order['average'] or price
                profit_pct = ((sell_price - entry_price) / entry_price) * 100 if entry_price > 0 else 0
                profit_usdt = (sell_price - entry_price) * order['filled']
                rsi = lab_state['indicators']['rsi']
                
                # Confirma saldo atualizado (com fallback local)
                try:
                    balance = exchange.fetch_balance()
                    lab_state['real_balance'] = balance['total'].get('USDT', 0.0)
                    print(f"✅ Saldo confirmado: ${lab_state['real_balance']:.2f} USDT")
                except Exception as e:
                    # Fallback: calcula saldo local
                    usdt_recebido = sell_price * order['filled']
                    lab_state['real_balance'] = lab_state.get('real_balance', 0) + usdt_recebido
                    print(f"⚠️ Erro Binance: {e} | Saldo estimado: ${lab_state['real_balance']:.2f} USDT")

                trade = {
                    'time': datetime.now().strftime('%H:%M:%S'),
                    'type': f'SELL REAL ({symbol})',
                    'price': sell_price,
                    'qty': order['filled'],
                    'order_id': order['id'],
                    'mode': 'REAL',
                    'entry_price': entry_price,  # Salva preço de compra
                    'profit_pct': profit_pct,    # Salva % lucro
                    'profit_usdt': profit_usdt,  # Salva lucro em USDT
                    'rsi': rsi  # Salva RSI para análise da IA
                }
                strategy['trades'].append(trade)
                strategy['position'] = None # Limpa posição
                
                print(f"💵 [{strategy['name']}] VENDA REAL: {order['filled']} {symbol} @ ${sell_price:.2f}")
                print(f"📊 Compra: ${entry_price:.4f} → Venda: ${sell_price:.4f} = {profit_pct:+.2f}% (${profit_usdt:+.4f})")
                
                # Notificação Telegram COMPLETA
                msg = (
                    f"💵 *VENDA REAL*\\n\\n"
                    f"🪙 Moeda: {symbol}\\n"
                    f"📥 Compra: ${entry_price:.4f}\\n"
                    f"📤 Venda: ${sell_price:.4f}\\n"
                    f"📊 Qtd: {order['filled']:.4f}\\n"
                    f"{'🟢' if profit_pct >= 0 else '🔴'} Lucro: {profit_pct:+.2f}% (${profit_usdt:+.2f})\\n"
                    f"📈 RSI: {rsi:.1f}"
                )
                send_telegram_message(msg)
                
                # Atualiza cooldown
                lab_state['last_trade_time'] = time.time()
                
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
        balance = exchange.fetch_balance()
        selected = lab_state['selected_strategy']
        strategy = lab_state['strategies'][selected]
        
        # Se já tem posição registrada, não faz nada
        if strategy['position'] is not None:
            return
        
        # Procura por moedas na carteira que estão na WATCHLIST
        for symbol in WATCHLIST:
            coin = symbol.replace('/USDT', '')
            coin_balance = balance['total'].get(coin, 0.0)
            
            if coin_balance > 0:
                # Busca o preço atual
                ticker = exchange.fetch_ticker(symbol)
                current_price = ticker['last']
                coin_value_usdt = coin_balance * current_price
                
                print(f"💰 Encontrado {coin}: {coin_balance:.8f} (${coin_value_usdt:.2f})")
                
                # Se tiver mais de $1 em valor, considera como posição aberta
                if coin_value_usdt >= 1:
                    # Estima o preço de entrada (usa o preço atual como fallback)
                    # Idealmente pegaria do histórico de trades
                    try:
                        trades = exchange.fetch_my_trades(symbol, limit=5)
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
                    
                    strategy['position'] = {
                        'entry_price': entry_price,
                        'qty': coin_balance,
                        'entry_time': datetime.now().isoformat(),
                        'symbol': symbol
                    }
                    
                    profit_pct = ((current_price - entry_price) / entry_price) * 100
                    print(f"🔄 POSIÇÃO RESTAURADA: {coin_balance:.6f} {symbol} @ ${entry_price:.2f} (Lucro: {profit_pct:+.2f}%)")
                    # Não envia Telegram aqui para não spammar
                    return  # Só pode ter uma posição por vez
                    
    except Exception as e:
        print(f"⚠️ Erro ao detectar posições: {e}")


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
            # Define quais moedas vamos olhar nesta rodada
            # Se já tivermos uma posição aberta, focamos SÓ nela
            active_symbol = None
            
            # Verifica se tem posição real aberta
            if lab_state['is_live']:
                selected = lab_state['selected_strategy']
                if lab_state['strategies'][selected]['position']:
                    active_symbol = lab_state['strategies'][selected]['position'].get('symbol', SYMBOL)
            
            # Verifica outras posições
            if not active_symbol:
                 for s_key in lab_state['strategies']:
                     if lab_state['strategies'][s_key]['position']:
                         active_symbol = lab_state['strategies'][s_key]['position'].get('symbol', SYMBOL)
                         break
            
            target_coins = [active_symbol] if active_symbol else WATCHLIST
            
            # ATUALIZA SALDO ANTES de verificar sinais de compra
            if exchange and API_KEY:
                try:
                    balance = exchange.fetch_balance()
                    lab_state['real_balance'] = balance['total'].get('USDT', 0.0)
                    lab_state['brl_balance'] = balance['total'].get('BRL', 0.0)
                except Exception as e:
                    print(f"⚠️ Erro ao atualizar saldo: {e}")

            for current_symbol in target_coins:
                # 1. Busca dados de mercado (agora inclui banda superior)
                price, rsi, bb_lower, bb_upper = fetch_market_data(current_symbol)
                
                # Alerta precoce — avisa antes de apertar o gatilho
                if price is not None and rsi is not None and bb_lower is not None:
                    if rsi < 40 and price <= bb_lower * 1.02:  # até 2% acima da banda
                        send_opportunity_alert(current_symbol, price, rsi, bb_lower)

                if price is not None:
                    lab_state['current_price'] = price
                    lab_state['current_symbol'] = current_symbol # Atualiza o símbolo na interface
                    lab_state['last_update'] = datetime.now().strftime('%H:%M:%S')
                    # Hack para mostrar qual moeda está sendo analisada no frontend (usando status)
                    # lab_state['status'] = f'Analisando {current_symbol}...' 
                    
                    # Atualiza indicadores globais
                    lab_state['indicators']['rsi'] = rsi
                    lab_state['indicators']['bb_lower'] = bb_lower
                    lab_state['indicators']['bb_upper'] = bb_upper
                    
                    # Atualiza Radar de Mercado + Diagnóstico
                    selected_strategy = lab_state['selected_strategy']
                    strategy_position = lab_state['strategies'][selected_strategy]['position']
                    diagnostic = get_diagnostic(selected_strategy, price, rsi, bb_lower, strategy_position)
                    
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
                if lab_state['running']:
                    lab_state['status'] = f'Rodando 🚀 | {current_symbol}'

                    if price is not None:
                        # LOG DE ANÁLISE
                        current_balance = lab_state.get('real_balance', 0.0)
                        print(f"🔎 {current_symbol}: RSI={rsi:.1f} | Preço=${price:.2f} | Saldo=${current_balance:.2f}")

                        # ========== 2.1 MODO REAL PRIMEIRO! ==========
                        if lab_state['is_live']:
                            selected = lab_state['selected_strategy']
                            strategy = lab_state['strategies'][selected]

                            if strategy['position'] is None:
                                # Sem posição - procura oportunidades de COMPRA
                                tolerance = bb_lower * 0.01  # 1% de tolerância (igual check_strategy_signal)
                                price_ok = price <= bb_lower + tolerance
                                rsi_ok = rsi < 45
                                signal = check_strategy_signal(selected, price, rsi, bb_lower)
                                
                                # Mostra debug para TODAS moedas com RSI < 45
                                if rsi_ok:
                                    print(f"📊 {current_symbol} | RSI={rsi:.1f}✅ | Preço=${price:.2f} | BB=${bb_lower:.2f} | Limite=${bb_lower + tolerance:.2f} | PrçOK={price_ok} | SINAL={signal}")
                                
                                if signal:
                                    print(f"🎯 SINAL DE COMPRA DETECTADO para {current_symbol}!")
                                    result = execute_real_trade('buy', price, current_symbol)
                                    if result:
                                        break # Sai do loop de moedas após compra bem-sucedida
                            else:
                                # TEM POSIÇÃO - verifica VENDA
                                pos_symbol = strategy['position'].get('symbol', SYMBOL)
                                entry_price = strategy['position']['entry_price']
                                profit_pct = ((price - entry_price) / entry_price) * 100
                                
                                if pos_symbol == current_symbol:
                                    print(f"📍 POSIÇÃO ATIVA: {pos_symbol} | Entrada: ${entry_price:.2f} | Atual: ${price:.2f} | Lucro: {profit_pct:+.2f}%")
                                    
                                    # LOG DETALHADO antes de verificar venda
                                    bb_display = f"${bb_upper:.2f}" if bb_upper else "$0"
                                    print(f"🔍 [DEBUG] Verificando saída: RSI={rsi:.1f} | Lucro={profit_pct:.2f}% | BB_Upper={bb_display}")
                                    
                                    should_sell = check_exit_signal(entry_price, price, rsi, bb_upper)
                                    
                                    if should_sell:
                                        # LOG COMPLETO ANTES DE VENDER
                                        print(f"⚠️ [VENDA AUTORIZADA]")
                                        print(f"   Moeda: {pos_symbol}")
                                        print(f"   Entrada: ${entry_price:.4f}")
                                        print(f"   Atual: ${price:.4f}")
                                        print(f"   Lucro: {profit_pct:+.2f}%")
                                        print(f"   RSI: {rsi:.1f}")
                                        print(f"   BB Upper: {bb_display}")
                                        
                                        # Envia alerta Telegram ANTES de executar
                                        send_telegram_message(
                                            f"⚠️ *INICIANDO VENDA*\\n\\n"
                                            f"🪙 {pos_symbol}\\n"
                                            f"📥 Compra: ${entry_price:.4f}\\n"
                                            f"📤 Venda: ${price:.4f}\\n"
                                            f"📊 Lucro: {profit_pct:+.2f}%\\n"
                                            f"📈 RSI: {rsi:.1f}"
                                        )
                                        
                                        execute_real_trade('sell', price, current_symbol)
                                        
                                        # Espera Binance processar antes de chamar IA
                                        print("⏳ Aguardando Binance estabilizar...")
                                        time.sleep(10)
                                        
                                        # IA analisa após a venda e ajusta estratégia se necessário
                                        print("🤖 IA analisando resultado para ajustar estratégia...")
                                        analyze_market_with_gpt(current_symbol, price, rsi, bb_lower, 'sell')

                else:
                    lab_state['status'] = 'Em Standby (Monitorando...) zzz'
                
                # Pequena pausa entre moedas para não estourar limite da API
                time.sleep(2)

            # 3. Atualiza saldo real e informações da conta (SEMPRE, para o dashboard)
            if exchange and API_KEY:
                try:
                    # Busca informações detalhadas da conta (UID, Permissões)
                    # Nota: private_get_account é específico da Binance
                    account_info = exchange.private_get_account()
                    
                    lab_state['user_info']['uid'] = account_info.get('uid', 'Não informado')
                    lab_state['user_info']['type'] = account_info.get('accountType', 'SPOT')
                    lab_state['user_info']['can_trade'] = account_info.get('canTrade', False)
                    
                    # Se estiver bloqueado, imprime aviso
                    if not lab_state['user_info']['can_trade']:
                         print(f"⚠️ CONTA BLOQUEADA PELA BINANCE. Resposta: {account_info.get('canTrade')}")

                    # Busca saldos
                    balance = exchange.fetch_balance()
                    
                    # Tenta pegar saldo em USDT ou BRL
                    usdt_balance = balance['total'].get('USDT', 0.0)
                    brl_balance = balance['total'].get('BRL', 0.0)
                    
                    # SEMPRE usa USDT como saldo principal para trading
                    # BRL precisa ser convertido para USDT antes de comprar cripto
                    lab_state['real_balance'] = usdt_balance
                    lab_state['brl_balance'] = brl_balance
                    
                    # Filtra saldos > 0 para exibir
                    relevant_balances = {}
                    total_brl = 0.0
                    
                    # Pega cotação USDT/BRL para converter
                    try:
                        usdt_brl_ticker = exchange.fetch_ticker('USDT/BRL')
                        usdt_brl_price = usdt_brl_ticker['last']
                    except:
                        usdt_brl_price = 5.50  # Fallback
                    
                    for asset, amount in balance['total'].items():
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
                                    ticker = exchange.fetch_ticker(f'{asset}/USDT')
                                    asset_usdt_price = ticker['last']
                                    total_brl += amount * asset_usdt_price * usdt_brl_price
                                except:
                                    pass  # Ignora se não conseguir
                    
                    lab_state['user_info']['balances'] = relevant_balances
                    lab_state['user_info']['total_brl'] = total_brl
                    lab_state['user_info']['usdt_brl_rate'] = usdt_brl_price

                except Exception as e:
                    # Em caso de erro (ex: IP bloqueado), mantém os dados anteriores ou mostra erro
                    # print(f"⚠️ Erro ao atualizar conta: {e}") # Comentado para não poluir log se for erro temporário
                    pass

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
        selected = lab_state['selected_strategy']
        trades = lab_state['strategies'][selected].get('trades', [])
        
        # Estatísticas básicas
        total_trades = len(trades)
        
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
        
        # Calcula métricas
        winning_trades = []
        losing_trades = []
        accumulated = []
        cumulative = 0
        
        for trade in trades:
            profit = trade.get('profit_pct', 0)
            if profit >= 0:
                winning_trades.append(trade)
            else:
                losing_trades.append(trade)
            
            cumulative += profit
            accumulated.append({
                'time': trade.get('exit_time', trade.get('time', '')),
                'profit': round(cumulative, 2)
            })
        
        profits = [t.get('profit_pct', 0) for t in trades]
        
        total_profit_pct = sum(profits)
        best_trade = max(profits) if profits else 0
        worst_trade = min(profits) if profits else 0
        avg_trade = total_profit_pct / total_trades if total_trades > 0 else 0
        win_rate = (len(winning_trades) / total_trades * 100) if total_trades > 0 else 0
        
        # Calcula lucro em BRL baseado no patrimônio atual
        try:
            usdt_balance = 0
            if exchange:
                balance = exchange.fetch_balance()
                usdt_balance = balance.get('USDT', {}).get('total', 0) or 0
            
            # Estima lucro em BRL
            usd_brl = 6.0
            total_profit_brl = (usdt_balance * total_profit_pct / 100) * usd_brl
        except:
            total_profit_brl = 0
        
        # Prepara trades para exibição (últimas 50)
        trades_display = []
        for t in trades[-50:]:
            trades_display.append({
                'symbol': t.get('symbol', ''),
                'type': t.get('action', t.get('type', '')),
                'entry_price': t.get('entry_price', 0),
                'exit_price': t.get('exit_price', 0),
                'profit_pct': t.get('profit_pct', 0),
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
    return jsonify(lab_state)


@app.route('/api/position')
def get_position():
    """Retorna informações da posição ativa com lucro em tempo real."""
    try:
        selected = lab_state['selected_strategy']
        position = lab_state['strategies'][selected].get('position')
        
        if not position:
            return jsonify({'has_position': False})
        
        symbol = position.get('symbol', SYMBOL)
        entry_price = position.get('entry_price', 0)
        qty = position.get('qty', 0)
        entry_time = position.get('entry_time', '')
        
        # Busca preço atual
        current_price = lab_state.get('current_price', entry_price)
        
        # Tenta pegar preço atualizado da API
        if exchange:
            try:
                ticker = exchange.fetch_ticker(symbol)
                current_price = ticker['last']
            except:
                pass
        
        # Calcula lucro/prejuízo
        profit_pct = ((current_price - entry_price) / entry_price) * 100 if entry_price > 0 else 0
        profit_value = (current_price - entry_price) * qty
        
        # Calcula metas (ESTRATÉGIA OTIMIZADA)
        take_profit_price = entry_price * 1.03   # +3% bruto = ~2.8% líquido
        stop_loss_price = entry_price * 0.985    # -1.5%
        
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
    try:
        selected = lab_state['selected_strategy']
        strategy = lab_state['strategies'][selected]
        
        old_position = strategy.get('position')
        strategy['position'] = None
        
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
            r = requests.get(url, params=params, timeout=10, verify=False)
            r.raise_for_status()
            raw_data = r.json()
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
    return jsonify({
        'watchlist': WATCHLIST,
        'market_overview': lab_state.get('market_overview', {})
    })


@app.route('/api/select_strategy', methods=['POST'])
def select_strategy():
    """Seleciona qual estratégia usar no modo real."""
    data = request.json
    strategy_key = data.get('strategy')

    if strategy_key in lab_state['strategies']:
        lab_state['selected_strategy'] = strategy_key
        save_lab_data()
        return jsonify({'success': True, 'selected': strategy_key})

    return jsonify({'success': False, 'error': 'Estratégia inválida'}), 400


@app.route('/api/toggle_live', methods=['POST'])
def toggle_live():
    """Liga/Desliga o modo real."""
    data = request.json
    is_live = data.get('is_live', False)

    if is_live and (not API_KEY or not SECRET):
        return jsonify({'success': False, 'error': 'Chaves API não configuradas'}), 400

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
    data = request.json
    running = data.get('running', False)
    
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
        balance = exchange.fetch_balance()
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
    if not exchange or not API_KEY or not SECRET:
        return jsonify({'success': False, 'error': '❌ Chaves API não configuradas!'}), 400
    
    data = request.json
    symbol = data.get('symbol', 'BTC/USDT')  # Padrão BTC/USDT
    amount_usd = 11.0  # Valor mínimo para teste
    
    try:
        print(f"{'='*60}")
        print(f"⚡ COMPRA FORÇADA INICIADA - {symbol}")
        print(f"{'='*60}")
        
        # Busca preço atual
        ticker = exchange.fetch_ticker(symbol)
        current_price = ticker['last']
        
        # Calcula quantidade
        qty = amount_usd / current_price
        
        # Executa ordem de mercado
        order = exchange.create_market_buy_order(symbol, qty)
        
        print(f"✅ ORDEM EXECUTADA!")
        print(f"   ID: {order['id']}")
        print(f"   Preço: ${order.get('average', current_price):.2f}")
        print(f"   Quantidade: {order['filled']}")
        
        # Notifica no Telegram
        msg = f"⚡ *COMPRA FORÇADA (TESTE)*\n\n🪙 Moeda: {symbol}\n💰 Preço: ${current_price:.2f}\n📦 Qtd: {order['filled']}\n🆔 Order ID: {order['id']}"
        send_telegram_message(msg)
        
        # Registra na estratégia ativa
        strategy_key = lab_state['selected_strategy']
        trade = {
            'time': datetime.now().strftime('%H:%M:%S'),
            'type': f'⚡ FORCE BUY ({symbol})',
            'price': order.get('average', current_price),
            'qty': order['filled'],
            'order_id': order['id'],
            'mode': 'REAL (TESTE)'
        }
        lab_state['strategies'][strategy_key]['trades'].append(trade)
        
        # SALVA POSIÇÃO para acompanhamento
        lab_state['strategies'][strategy_key]['position'] = {
            'entry_price': order.get('average', current_price),
            'qty': order['filled'],
            'entry_time': datetime.now().isoformat(),
            'symbol': symbol
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
    if not exchange or not API_KEY or not SECRET:
        return jsonify({'error': 'API não configurada'}), 400

    try:
        # 1. Informações da Conta (Saldo detalhado)
        account_balance = exchange.fetch_balance()
        
        # 1.1 Informações da Conta (Dados brutos da Binance - Permissões, Comissões, etc)
        account_details = exchange.private_get_account()

        # 2. Histórico de Trades (Últimos trades do símbolo atual)
        trades = exchange.fetch_my_trades(SYMBOL)
        
        # 3. Ordens Abertas
        open_orders = exchange.fetch_open_orders(SYMBOL)
        
        # 4. Todas as Ordens (Histórico)
        all_orders = exchange.fetch_orders(SYMBOL)
        
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
        "👋 Olá! Sou o Bot do Laboratório de Trading.\n\n"
        "📋 *COMANDOS DISPONÍVEIS:*\n\n"
        "📊 /status - Ver situação atual do bot\n"
        "💰 /saldo - Ver saldo da conta\n"
        "📈 /posicao - Ver posição aberta\n"
        "🔍 /moedas - Ver análise de todas moedas\n"
        "📑 /relatorio - Relatório completo\n"
        "⚡ /comprar XRP - Forçar compra\n"
        "💵 /converter - Converter BRL para USDT\n"
        "🔔 /ligar - Ligar o bot\n"
        "🔕 /desligar - Desligar o bot\n"
        "❓ /ajuda - Ver ajuda",
        parse_mode='Markdown'
    )

async def telegram_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *COMANDOS DO BOT:*\n\n"
        "*Informações:*\n"
        "/status - Mostra o que o bot está analisando\n"
        "/saldo - Mostra seu saldo em BRL e USDT\n"
        "/posicao - Mostra posição aberta (se houver)\n"
        "/moedas - Análise de todas as 10 moedas\n"
        "/relatorio - Relatório completo do mercado\n"
        "/ia - Parâmetros da IA (reset para resetar)\n\n"
        "*Ações:*\n"
        "/comprar XRP - Força compra de uma moeda\n"
        "/converter - Converte BRL para USDT\n"
        "/ligar - Liga o bot automático\n"
        "/desligar - Desliga o bot automático\n\n"
        "*Chat:*\n"
        "Envie qualquer mensagem para conversar com a IA!",
        parse_mode='Markdown'
    )

async def telegram_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        msg = "📊 *STATUS DO MERCADO*\n\n"
        msg += f"🪙 *Moeda:* {lab_state['current_symbol']}\n"
        msg += f"💰 *Preço:* ${lab_state['current_price']:.2f}\n"
        msg += f"📉 *RSI:* {lab_state['indicators']['rsi']:.2f}\n"
        msg += f"🛡️ *Bandas:* {lab_state['indicators']['bb_lower']:.2f}\n\n"
        
        msg += f"⚙️ *Configuração:*\n"
        msg += f"Estratégia: {lab_state['selected_strategy']}\n"
        msg += f"Modo: Trading Real 💰\n"
        msg += f"Status: {lab_state['status']}"
        
        await update.message.reply_text(msg, parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"❌ Erro ao buscar status: {str(e)}")

async def telegram_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        balances = lab_state['user_info'].get('balances', {})
        msg = "💰 *SEU SALDO*\n\n"
        if not balances:
            msg += "Nenhum saldo encontrado ou API desconectada."
        else:
            for coin, amount in balances.items():
                if amount > 0.0001:  # Só mostra saldos relevantes
                    msg += f"• *{coin}:* {amount:.4f}\n"
            
            # Total em BRL
            total_brl = lab_state['user_info'].get('total_brl', 0)
            msg += f"\n📊 *Total em BRL:* R${total_brl:.2f}"
        
        await update.message.reply_text(msg, parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"❌ Erro ao buscar saldo: {str(e)}")


async def telegram_position(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mostra posição aberta atual."""
    try:
        strategy_key = lab_state['selected_strategy']
        position = lab_state['strategies'][strategy_key].get('position')
        
        if not position:
            await update.message.reply_text("📍 *Nenhuma posição aberta no momento.*\n\nO bot está aguardando oportunidade de compra.", parse_mode='Markdown')
            return
        
        symbol = position.get('symbol', 'N/A')
        entry_price = position.get('entry_price', 0)
        qty = position.get('qty', 0)
        entry_time = position.get('entry_time', 'N/A')
        
        # Busca preço atual
        current_price = lab_state.get('current_price', entry_price)
        
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
        
        diagnostics = lab_state.get('diagnostics', {})
        market = lab_state.get('market_overview', {})
        
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
        
        msg += "\n"
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
        report = generate_market_report()
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
        
        await update.message.reply_text(f"⚡ Executando compra de {symbol}...")
        
        # Executa a compra
        if not exchange:
            await update.message.reply_text("❌ API não conectada!")
            return
        
        # Busca saldo REAL da Binance (não usa cache)
        balance = exchange.fetch_balance()
        usdt_balance = balance['USDT']['free']
        lab_state['real_balance'] = usdt_balance  # Atualiza cache
        
        ticker = exchange.fetch_ticker(symbol)
        current_price = ticker['last']
        
        # Calcula quantidade
        amount = min(AMOUNT_INVEST, usdt_balance * 0.95)
        if usdt_balance < 10.5:  # Margem pra taxa
            await update.message.reply_text(f"❌ Saldo insuficiente! Precisa de $11.00, tem ${usdt_balance:.2f}")
            return
        
        qty = amount / current_price
        order = exchange.create_market_buy_order(symbol, qty)
        
        # Atualiza saldo com valor REAL gasto
        usdt_gasto = order.get('cost', amount)
        lab_state['real_balance'] = usdt_balance - usdt_gasto
        
        # Salva posição
        strategy_key = lab_state['selected_strategy']
        lab_state['strategies'][strategy_key]['position'] = {
            'entry_price': order.get('average', current_price),
            'qty': order['filled'],
            'entry_time': datetime.now().isoformat(),
            'symbol': symbol
        }
        
        # Aguarda Binance refletir a ordem
        time.sleep(3)
        
        # Busca saldo atualizado da Binance
        try:
            balance = exchange.fetch_balance()
            lab_state['real_balance'] = balance['USDT']['free']
        except:
            pass  # Já atualizou antes
        
        print(f"💸 Gasto: ${usdt_gasto:.2f} USDT | Saldo restante: ${lab_state['real_balance']:.2f}")
        
        save_lab_data()
        
        await update.message.reply_text(
            f"✅ *COMPRA EXECUTADA!*\n\n"
            f"🪙 {symbol}\n"
            f"💵 ${order.get('average', current_price):.4f}\n"
            f"📦 {order['filled']:.4f}",
            parse_mode='Markdown'
        )
        
    except Exception as e:
        await update.message.reply_text(f"❌ Erro na compra: {str(e)}")


async def telegram_convert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Converte BRL para USDT."""
    try:
        await update.message.reply_text("🔄 Convertendo BRL para USDT...")
        
        result = convert_brl_to_usdt(min_brl=10)
        
        if result > 0:
            await update.message.reply_text(f"✅ Conversão concluída!\n\n💰 Saldo USDT: ${result:.2f}")
        else:
            await update.message.reply_text("❌ Não foi possível converter. Verifique seu saldo BRL.")
    except Exception as e:
        await update.message.reply_text(f"❌ Erro: {str(e)}")


async def telegram_start_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Liga o bot automático."""
    lab_state['running'] = True
    save_lab_data()
    await update.message.reply_text("🟢 *Bot LIGADO!*\n\nAgora monitorando o mercado e executando trades automaticamente.", parse_mode='Markdown')


async def telegram_stop_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Desliga o bot automático."""
    lab_state['running'] = False
    save_lab_data()
    await update.message.reply_text("🔴 *Bot DESLIGADO!*\n\nO bot parou de monitorar. Use /ligar para reativar.", parse_mode='Markdown')


async def telegram_ia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mostra parâmetros da IA ou reseta para padrão."""
    global STRATEGY_PARAMS
    
    args = context.args if context.args else []
    
    if args and args[0].lower() == 'reset':
        # Reseta para valores padrão
        STRATEGY_PARAMS = {
            'RSI_TARGET': 35,
            'TOLERANCE': 0.01,
            'STOP_LOSS': -3.0,
            'TAKE_PROFIT': 5.0,
        }
        await update.message.reply_text(
            "🔄 *Parâmetros resetados!*\n\n"
            f"📊 RSI Target: {STRATEGY_PARAMS['RSI_TARGET']}\n"
            f"📏 Tolerância: {STRATEGY_PARAMS['TOLERANCE']*100:.1f}%\n"
            f"🛑 Stop Loss: {STRATEGY_PARAMS['STOP_LOSS']}%\n"
            f"🎯 Take Profit: {STRATEGY_PARAMS['TAKE_PROFIT']}%",
            parse_mode='Markdown'
        )
    else:
        # Mostra parâmetros atuais
        await update.message.reply_text(
            "🤖 *Parâmetros da IA*\n\n"
            f"📊 RSI Target: `{STRATEGY_PARAMS['RSI_TARGET']}`\n"
            f"📏 Tolerância: `{STRATEGY_PARAMS['TOLERANCE']*100:.1f}%`\n"
            f"🛑 Stop Loss: `{STRATEGY_PARAMS['STOP_LOSS']}%`\n"
            f"🎯 Take Profit: `{STRATEGY_PARAMS['TAKE_PROFIT']}%`\n\n"
            "💡 Use `/ia reset` para voltar aos valores padrão.",
            parse_mode='Markdown'
        )


async def telegram_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Responde a mensagens de texto usando GPT com contexto do mercado."""
    user_message = update.message.text
    print(f"📩 Mensagem recebida de {update.effective_user.first_name}: {user_message}")
    
    client = get_openai_client()
    if not client:
        await update.message.reply_text("🧠 IA não configurada no servidor.")
        return

    try:
        # Envia "Digitando..."
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action='typing')
        
        # Constrói contexto do mercado atual
        market_context = "DADOS ATUAIS DO MERCADO (Use isso para responder):\n"
        if lab_state['market_overview']:
            for symbol, data in lab_state['market_overview'].items():
                market_context += f"- {symbol}: Preço=${data['price']:.2f} | RSI={data['rsi']:.1f} | BB_Lower=${data['bb_lower']:.2f}\n"
        else:
            market_context += "Nenhum dado de mercado coletado ainda.\n"
            
        market_context += f"\nSaldo do Usuário: {lab_state.get('real_balance', 0):.2f}\n"
        market_context += f"Estratégia Ativa: {lab_state['selected_strategy']}\n"
        
        # Adiciona regras da estratégia e estado do modo real
        strategy_key = lab_state['selected_strategy']
        is_live = lab_state['is_live']
        
        strategy_rules = "Desconhecida"
        if strategy_key == 'conservative':
            strategy_rules = "Comprar APENAS quando RSI < 30 e Preço < Banda Inferior."
        elif strategy_key == 'aggressive':
            strategy_rules = "Comprar quando RSI < 45 e Preço < Banda Inferior."
        elif strategy_key == 'rsi_pure':
            strategy_rules = "Comprar quando RSI < 30."
            
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
        
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message}
            ],
            max_tokens=400
        )
        reply = response.choices[0].message.content.strip()
        await update.message.reply_text(reply)
    except Exception as e:
        print(f"❌ Erro na IA: {e}")
        await update.message.reply_text(f"❌ Erro na IA: {str(e)}")

def run_telegram_bot():
    """Inicia o bot do Telegram em modo de escuta (Polling)."""
    if not TELEGRAM_TOKEN or TELEGRAM_TOKEN == 'your_telegram_token_here':
        print("⚠️ Telegram Listener não iniciado (Token inválido)")
        return

    # Cria novo loop de eventos para esta thread
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    print("🤖 Iniciando Telegram Bot Listener...")
    
    try:
        app_bot = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
        
        # Tenta enviar mensagem de boas-vindas para confirmar conexão
        if TELEGRAM_CHAT_ID:
            try:
                print(f"📨 Tentando enviar mensagem de teste para ID: {TELEGRAM_CHAT_ID}")
                loop.run_until_complete(app_bot.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text="🤖 *Bot Reiniciado!* Estou online e pronto para conversar.", parse_mode='Markdown'))
                print("✅ Mensagem de teste enviada com sucesso!")
            except Exception as e:
                print(f"❌ Falha ao enviar mensagem de teste: {e}")

        app_bot.add_handler(CommandHandler("start", telegram_start))
        app_bot.add_handler(CommandHandler("ajuda", telegram_help))
        app_bot.add_handler(CommandHandler("help", telegram_help))
        app_bot.add_handler(CommandHandler("status", telegram_status))
        app_bot.add_handler(CommandHandler("saldo", telegram_balance))
        
        # Novos comandos
        app_bot.add_handler(CommandHandler("posicao", telegram_position))
        app_bot.add_handler(CommandHandler("position", telegram_position))
        app_bot.add_handler(CommandHandler("moedas", telegram_coins))
        app_bot.add_handler(CommandHandler("coins", telegram_coins))
        app_bot.add_handler(CommandHandler("relatorio", telegram_report))
        app_bot.add_handler(CommandHandler("report", telegram_report))
        app_bot.add_handler(CommandHandler("comprar", telegram_buy))
        app_bot.add_handler(CommandHandler("buy", telegram_buy))
        app_bot.add_handler(CommandHandler("converter", telegram_convert))
        app_bot.add_handler(CommandHandler("convert", telegram_convert))
        app_bot.add_handler(CommandHandler("ligar", telegram_start_bot))
        app_bot.add_handler(CommandHandler("on", telegram_start_bot))
        app_bot.add_handler(CommandHandler("desligar", telegram_stop_bot))
        app_bot.add_handler(CommandHandler("off", telegram_stop_bot))
        app_bot.add_handler(CommandHandler("ia", telegram_ia))
        app_bot.add_handler(CommandHandler("ai", telegram_ia))
        
        # Handler para mensagens de texto (Chat com GPT)
        app_bot.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, telegram_chat))
        
        # stop_signals=None é necessário quando roda em uma thread secundária
        print("🤖 Telegram Bot ouvindo...")
        app_bot.run_polling(stop_signals=None, close_loop=False)
    except Exception as e:
        print(f"❌ Erro fatal no Telegram Bot: {e}")

# Inicia thread do Telegram Listener
telegram_thread = threading.Thread(target=run_telegram_bot, daemon=True)
telegram_thread.start()

# Inicia thread de trading
thread = threading.Thread(target=trading_loop, daemon=True)
thread.start()


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
        print("🤖 Bot rodando... Pressione Ctrl+C para parar.")
        
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
