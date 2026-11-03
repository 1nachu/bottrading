"""
Bot de Trading: Ruptura Momentum + Gestión de Bankroll
Estrategia traducida de Pine Script a Python
Exchange: Binance Futures (USDⓈ-M)
"""

import ccxt
import pandas as pd
import pandas_ta as ta
import time
import logging
from datetime import datetime
import os

# =========================================================================
# CONFIGURACIÓN
# =========================================================================
API_KEY    = "TU_API_KEY_AQUI"
API_SECRET = "TU_API_SECRET_AQUI"

SYMBOL     = "BTC/USDT"          # Par a operar
TIMEFRAME  = "4h"                 # Temporalidad (ajusta según tu estrategia)
LEVERAGE   = 5                    # Apalancamiento (ajusta con cuidado)

# Parámetros de la estrategia (deben coincidir con los de TradingView)
RIESGO_POR_TRADE   = 0.02         # 2% del capital por operación
LONGITUD_RUPTURA   = 20           # Periodos Donchian
ADX_LONGITUD       = 14
ADX_UMBRAL         = 20           # Umbral mínimo ADX
ATR_LONGITUD       = 14
ATR_MULTIPLICADOR  = 2.4

# Control del loop
INTERVALO_SEGUNDOS = 60           # Revisar cada 60 segundos

# =========================================================================
# LOGGING
# =========================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("bot_trading.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)


# =========================================================================
# CONEXIÓN A BINANCE FUTURES
# =========================================================================
def crear_exchange():
    exchange = ccxt.binance({
        "apiKey": API_KEY,
        "secret": API_SECRET,
        "options": {
            "defaultType": "future",   # Futuros USDⓈ-M
        },
        "enableRateLimit": True,
    })
    # Configurar apalancamiento
    try:
        exchange.set_leverage(LEVERAGE, SYMBOL.replace("/", ""))
    except Exception as e:
        log.warning(f"No se pudo configurar apalancamiento: {e}")
    return exchange


# =========================================================================
# OBTENER DATOS DE MERCADO
# =========================================================================
def obtener_ohlcv(exchange, symbol=SYMBOL, timeframe=TIMEFRAME, limit=250):
    """Descarga velas OHLCV y devuelve un DataFrame."""
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df.set_index("timestamp", inplace=True)
    return df


# =========================================================================
# CÁLCULO DE INDICADORES (replica exacta del Pine Script)
# =========================================================================
def calcular_indicadores(df):
    # EMA 200
    df["ema200"] = ta.ema(df["close"], length=200)

    # ADX + DI+ / DI-
    adx_df = ta.adx(df["high"], df["low"], df["close"], length=ADX_LONGITUD)
    df["adx"]     = adx_df[f"ADX_{ADX_LONGITUD}"]
    df["di_plus"]  = adx_df[f"DMP_{ADX_LONGITUD}"]
    df["di_minus"] = adx_df[f"DMN_{ADX_LONGITUD}"]

    # Donchian Channel (máximo y mínimo local)
    df["maximo_local"] = df["high"].rolling(LONGITUD_RUPTURA).max()
    df["minimo_local"]  = df["low"].rolling(LONGITUD_RUPTURA).min()

    # ATR
    df["atr"] = ta.atr(df["high"], df["low"], df["close"], length=ATR_LONGITUD)

    return df.dropna()


# =========================================================================
# POSITION SIZING DINÁMICO (igual que Pine Script)
# =========================================================================
def calcular_tamano_posicion(equity, atr):
    """
    Fórmula Pine Script:
        distanciaStop  = ATR * multiplicador
        riesgoEnDolares = equity * riesgoPorTrade
        tamanoPosicion  = riesgoEnDolares / distanciaStop
    """
    distancia_stop  = atr * ATR_MULTIPLICADOR
    riesgo_dolares  = equity * RIESGO_POR_TRADE
    tamano          = riesgo_dolares / distancia_stop
    return tamano, distancia_stop


# =========================================================================
# LÓGICA DE SEÑALES
# =========================================================================
def evaluar_señal(df):
    """
    Devuelve: 'long', 'short' o None
    Condiciones del Pine Script:
        condicionLong  = close > ema200 AND adx > umbral
        condicionShort = close < ema200 AND adx > umbral
    La entrada es por ruptura del máximo/mínimo local (stop order).
    """
    ultima = df.iloc[-1]
    close  = ultima["close"]
    ema200 = ultima["ema200"]
    adx    = ultima["adx"]

    condicion_long  = close > ema200 and adx > ADX_UMBRAL
    condicion_short = close < ema200 and adx > ADX_UMBRAL

    if condicion_long:
        return "long", ultima
    elif condicion_short:
        return "short", ultima
    return None, ultima


# =========================================================================
# OBTENER POSICIÓN ACTUAL
# =========================================================================
def obtener_posicion(exchange, symbol=SYMBOL):
    """Devuelve tamaño de posición actual (positivo=long, negativo=short, 0=flat)."""
    try:
        posiciones = exchange.fetch_positions([symbol])
        for pos in posiciones:
            if pos["symbol"] == symbol:
                return float(pos["contracts"]) * (1 if pos["side"] == "long" else -1)
    except Exception as e:
        log.error(f"Error obteniendo posición: {e}")
    return 0.0


def obtener_equity(exchange):
    """Devuelve el balance total en USDT de la cuenta de futuros."""
    try:
        balance = exchange.fetch_balance({"type": "future"})
        return float(balance["total"]["USDT"])
    except Exception as e:
        log.error(f"Error obteniendo equity: {e}")
        return 0.0


# =========================================================================
# GESTIÓN DE TRAILING STOP (replica Pine Script)
# =========================================================================
class TrailingStop:
    def __init__(self):
        self.stop_long  = None
        self.stop_short = None

    def actualizar_long(self, close, distancia_stop, precio_entrada=None):
        if self.stop_long is None and precio_entrada:
            self.stop_long = precio_entrada - distancia_stop
        elif self.stop_long is not None:
            nuevo_stop = close - distancia_stop
            self.stop_long = max(self.stop_long, nuevo_stop)
        return self.stop_long

    def actualizar_short(self, close, distancia_stop, precio_entrada=None):
        if self.stop_short is None and precio_entrada:
            self.stop_short = precio_entrada + distancia_stop
        elif self.stop_short is not None:
            nuevo_stop = close + distancia_stop
            self.stop_short = min(self.stop_short, nuevo_stop)
        return self.stop_short

    def reset(self):
        self.stop_long  = None
        self.stop_short = None


# =========================================================================
# EJECUTAR ÓRDENES
# =========================================================================
def abrir_orden(exchange, side, tamano, stop_price, symbol=SYMBOL):
    """Abre una orden de mercado + coloca stop loss."""
    try:
        log.info(f"Abriendo {side.upper()} | Tamaño: {tamano:.4f} BTC | Stop: {stop_price:.2f}")
        # Orden de mercado para entrar
        orden = exchange.create_order(
            symbol=symbol,
            type="market",
            side="buy" if side == "long" else "sell",
            amount=tamano,
        )
        log.info(f"Orden ejecutada: {orden['id']}")

        # Stop Loss
        exchange.create_order(
            symbol=symbol,
            type="stop_market",
            side="sell" if side == "long" else "buy",
            amount=tamano,
            params={"stopPrice": round(stop_price, 2), "reduceOnly": True},
        )
        log.info(f"Stop Loss colocado en: {stop_price:.2f}")
        return orden
    except Exception as e:
        log.error(f"Error al abrir orden: {e}")
        return None


def cerrar_posicion(exchange, posicion_actual, symbol=SYMBOL):
    """Cierra la posición abierta con orden de mercado."""
    try:
        side_cierre = "sell" if posicion_actual > 0 else "buy"
        tamano = abs(posicion_actual)
        log.info(f"Cerrando posición: {side_cierre.upper()} {tamano:.4f} BTC")
        exchange.create_order(
            symbol=symbol,
            type="market",
            side=side_cierre,
            amount=tamano,
            params={"reduceOnly": True},
        )
    except Exception as e:
        log.error(f"Error al cerrar posición: {e}")


def actualizar_stop_loss(exchange, stop_price, posicion_actual, symbol=SYMBOL):
    """Cancela SL anterior y coloca uno nuevo (trailing)."""
    try:
        # Cancelar órdenes abiertas de stop
        ordenes = exchange.fetch_open_orders(symbol)
        for orden in ordenes:
            if orden["type"] in ("stop_market", "stop"):
                exchange.cancel_order(orden["id"], symbol)
        # Colocar nuevo stop
        side_cierre = "sell" if posicion_actual > 0 else "buy"
        exchange.create_order(
            symbol=symbol,
            type="stop_market",
            side=side_cierre,
            amount=abs(posicion_actual),
            params={"stopPrice": round(stop_price, 2), "reduceOnly": True},
        )
        log.info(f"Trailing Stop actualizado a: {stop_price:.2f}")
    except Exception as e:
        log.error(f"Error actualizando Stop Loss: {e}")


# =========================================================================
# LOOP PRINCIPAL
# =========================================================================
def main():
    log.info("=" * 60)
    log.info("Bot iniciado — Estrategia: Ruptura Momentum + Bankroll")
    log.info("=" * 60)

    exchange      = crear_exchange()
    trailing      = TrailingStop()
    precio_entrada = None

    while True:
        try:
            # 1. Obtener datos y calcular indicadores
            df      = obtener_ohlcv(exchange)
            df      = calcular_indicadores(df)
            señal, ultima = evaluar_señal(df)

            close          = ultima["close"]
            atr            = ultima["atr"]
            maximo_local   = ultima["maximo_local"]
            minimo_local   = ultima["minimo_local"]
            equity         = obtener_equity(exchange)
            posicion_actual = obtener_posicion(exchange)
            tamano, dist_stop = calcular_tamano_posicion(equity, atr)

            log.info(
                f"[{datetime.now().strftime('%H:%M:%S')}] "
                f"BTC: {close:.2f} | ADX: {ultima['adx']:.1f} | "
                f"EMA200: {ultima['ema200']:.2f} | Señal: {señal or 'ninguna'} | "
                f"Posición: {posicion_actual:.4f} | Equity: {equity:.2f} USDT"
            )

            # 2. Sin posición abierta → buscar entrada
            if posicion_actual == 0:
                trailing.reset()
                precio_entrada = None

                if señal == "long":
                    # Entrada breakout: se ejecuta si close ya rompió el máximo
                    if close >= maximo_local:
                        stop_inicial = close - dist_stop
                        abrir_orden(exchange, "long", tamano, stop_inicial)
                        precio_entrada = close
                        trailing.actualizar_long(close, dist_stop, precio_entrada)

                elif señal == "short":
                    if close <= minimo_local:
                        stop_inicial = close + dist_stop
                        abrir_orden(exchange, "short", tamano, stop_inicial)
                        precio_entrada = close
                        trailing.actualizar_short(close, dist_stop, precio_entrada)

            # 3. Con posición abierta → gestionar trailing stop
            elif posicion_actual > 0:  # Long activo
                if señal != "long":
                    log.info("Condición Long desactivada — cerrando posición.")
                    cerrar_posicion(exchange, posicion_actual)
                    trailing.reset()
                else:
                    nuevo_stop = trailing.actualizar_long(close, dist_stop)
                    actualizar_stop_loss(exchange, nuevo_stop, posicion_actual)

            elif posicion_actual < 0:  # Short activo
                if señal != "short":
                    log.info("Condición Short desactivada — cerrando posición.")
                    cerrar_posicion(exchange, posicion_actual)
                    trailing.reset()
                else:
                    nuevo_stop = trailing.actualizar_short(close, dist_stop)
                    actualizar_stop_loss(exchange, nuevo_stop, posicion_actual)

        except ccxt.NetworkError as e:
            log.warning(f"Error de red: {e} — reintentando en 30s")
            time.sleep(30)
            continue
        except ccxt.ExchangeError as e:
            log.error(f"Error del exchange: {e}")
            time.sleep(60)
            continue
        except Exception as e:
            log.error(f"Error inesperado: {e}", exc_info=True)
            time.sleep(60)
            continue

        time.sleep(INTERVALO_SEGUNDOS)


if __name__ == "__main__":
    main()
