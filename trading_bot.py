"""
Bot de Trading: Ruptura Momentum + Gestión de Bankroll
Estrategia traducida de Pine Script a Python
Exchange: Binance Futures (USDⓈ-M)

Variables de entorno:
    BINANCE_API_KEY      API key (permiso Futures, SIN retiros)
    BINANCE_API_SECRET   API secret
    BINANCE_TESTNET      "true" para usar el entorno de pruebas (por defecto "false")
"""

import logging
import os
import time

import ccxt
import pandas as pd
import pandas_ta as ta

# =========================================================================
# CONFIGURACIÓN
# =========================================================================
API_KEY = os.environ.get("BINANCE_API_KEY")
API_SECRET = os.environ.get("BINANCE_API_SECRET")
USAR_TESTNET = os.environ.get("BINANCE_TESTNET", "false").strip().lower() in ("1", "true", "yes", "si")

SYMBOL = "BTC/USDT:USDT"          # Símbolo unificado de ccxt para el perpetuo USDⓈ-M
TIMEFRAME = "4h"                  # Temporalidad (debe coincidir con TradingView)
LEVERAGE = 5                      # Apalancamiento (ajustar con cuidado)
VELAS_HISTORICO = 1000            # Velas a descargar (la EMA 200 necesita historial para converger)

# Parámetros de la estrategia (deben coincidir con los de TradingView)
RIESGO_POR_TRADE = 0.02           # 2% del capital por operación
LONGITUD_RUPTURA = 20             # Períodos Donchian
ADX_LONGITUD = 14
ADX_UMBRAL = 20                   # Umbral mínimo ADX
ATR_LONGITUD = 14
ATR_MULTIPLICADOR = 2.4
EMA_LONGITUD = 200

MARGEN_SEGURIDAD = 0.95           # Usa como máximo el 95% del nocional permitido por el apalancamiento

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
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


# =========================================================================
# CONEXIÓN A BINANCE FUTURES
# =========================================================================
def crear_exchange():
    if not API_KEY or not API_SECRET:
        raise SystemExit("Faltan BINANCE_API_KEY y/o BINANCE_API_SECRET en las variables de entorno.")

    exchange = ccxt.binance({
        "apiKey": API_KEY,
        "secret": API_SECRET,
        "options": {"defaultType": "future"},   # Futuros USDⓈ-M
        "enableRateLimit": True,
    })
    if USAR_TESTNET:
        exchange.set_sandbox_mode(True)
        log.warning("MODO TESTNET activado (dinero ficticio).")

    exchange.load_markets()
    if SYMBOL not in exchange.markets:
        raise SystemExit(f"El símbolo {SYMBOL} no existe en el exchange.")

    try:
        exchange.set_leverage(LEVERAGE, SYMBOL)
    except Exception as e:
        log.warning(f"No se pudo configurar apalancamiento: {e}")
    return exchange


# =========================================================================
# DATOS DE MERCADO
# =========================================================================
def obtener_ohlcv(exchange, symbol=SYMBOL, timeframe=TIMEFRAME, limit=VELAS_HISTORICO):
    """Descarga velas OHLCV y devuelve un DataFrame."""
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df.set_index("timestamp", inplace=True)
    return df


# =========================================================================
# INDICADORES (replica del Pine Script)
# =========================================================================
def calcular_indicadores(df):
    df = df.copy()

    # EMA 200
    df["ema200"] = ta.ema(df["close"], length=EMA_LONGITUD)

    # ADX + DI+ / DI-
    adx_df = ta.adx(df["high"], df["low"], df["close"], length=ADX_LONGITUD)
    df["adx"] = adx_df[f"ADX_{ADX_LONGITUD}"]
    df["di_plus"] = adx_df[f"DMP_{ADX_LONGITUD}"]
    df["di_minus"] = adx_df[f"DMN_{ADX_LONGITUD}"]

    # Canal Donchian de las N velas ANTERIORES (shift(1) excluye la vela actual,
    # igual que ta.highest(high, N)[1] en Pine Script). Sin el shift, el cierre
    # nunca podría superar el máximo que incluye a la propia vela.
    df["maximo_local"] = df["high"].rolling(LONGITUD_RUPTURA).max().shift(1)
    df["minimo_local"] = df["low"].rolling(LONGITUD_RUPTURA).min().shift(1)

    # ATR
    df["atr"] = ta.atr(df["high"], df["low"], df["close"], length=ATR_LONGITUD)

    return df.dropna()


# =========================================================================
# POSITION SIZING DINÁMICO
# =========================================================================
def calcular_tamano_posicion(exchange, equity, atr, precio):
    """
    distancia_stop  = ATR * multiplicador
    riesgo_dolares  = equity * riesgo_por_trade
    tamano          = riesgo_dolares / distancia_stop

    Se limita por el nocional máximo (equity * apalancamiento), se redondea a la
    precisión del exchange y se valida contra los mínimos del mercado.
    Devuelve (tamano, distancia_stop); tamano = 0.0 si no es operable.
    """
    distancia_stop = atr * ATR_MULTIPLICADOR
    riesgo_dolares = equity * RIESGO_POR_TRADE
    tamano = riesgo_dolares / distancia_stop

    max_nocional = equity * LEVERAGE * MARGEN_SEGURIDAD
    tamano = min(tamano, max_nocional / precio)
    tamano = float(exchange.amount_to_precision(SYMBOL, tamano))

    limites = exchange.market(SYMBOL).get("limits", {})
    minimo_cantidad = (limites.get("amount") or {}).get("min")
    minimo_costo = (limites.get("cost") or {}).get("min")
    if minimo_cantidad and tamano < minimo_cantidad:
        log.warning(f"Tamaño {tamano} por debajo del mínimo del exchange ({minimo_cantidad}).")
        return 0.0, distancia_stop
    if minimo_costo and tamano * precio < minimo_costo:
        log.warning(f"Nocional {tamano * precio:.2f} por debajo del mínimo del exchange ({minimo_costo}).")
        return 0.0, distancia_stop
    return tamano, distancia_stop


# =========================================================================
# SEÑALES
# =========================================================================
def evaluar_senal(df):
    """
    Devuelve ('long' | 'short' | None, ultima_vela).
        long  = close > ema200 AND adx > umbral
        short = close < ema200 AND adx > umbral
    La ruptura Donchian se verifica aparte, solo al entrar.
    """
    ultima = df.iloc[-1]
    close = ultima["close"]
    ema200 = ultima["ema200"]
    adx = ultima["adx"]

    if close > ema200 and adx > ADX_UMBRAL:
        return "long", ultima
    if close < ema200 and adx > ADX_UMBRAL:
        return "short", ultima
    return None, ultima


# =========================================================================
# CUENTA Y POSICIÓN
# =========================================================================
def obtener_posicion(exchange):
    """
    Devuelve (cantidad, precio_entrada): cantidad > 0 long, < 0 short, 0 sin posición.
    Devuelve None si hubo un error (NO se debe interpretar como 'sin posición').
    """
    try:
        for pos in exchange.fetch_positions([SYMBOL]):
            if pos.get("symbol") != SYMBOL:
                continue
            contratos = float(pos.get("contracts") or 0)
            if contratos == 0:
                return 0.0, None
            signo = 1 if pos.get("side") == "long" else -1
            entrada = pos.get("entryPrice")
            return contratos * signo, (float(entrada) if entrada else None)
        return 0.0, None
    except Exception as e:
        log.error(f"Error obteniendo posición: {e}")
        return None


def obtener_equity(exchange):
    """Devuelve el balance total en USDT, o None si hubo error."""
    try:
        balance = exchange.fetch_balance({"type": "future"})
        return float(balance["total"]["USDT"])
    except Exception as e:
        log.error(f"Error obteniendo equity: {e}")
        return None


# =========================================================================
# TRAILING STOP (estado)
# =========================================================================
class TrailingStop:
    """Guarda el último stop colocado en el exchange y calcula el siguiente."""

    def __init__(self):
        self.lado = None
        self.stop = None

    def iniciar(self, lado, stop):
        self.lado = lado
        self.stop = stop

    def candidato(self, precio, distancia):
        """Devuelve un stop nuevo solo si mejora al actual (long: sube, short: baja)."""
        if self.stop is None:
            return None
        if self.lado == "long":
            nuevo = precio - distancia
            return nuevo if nuevo > self.stop else None
        nuevo = precio + distancia
        return nuevo if nuevo < self.stop else None

    def reset(self):
        self.lado = None
        self.stop = None


# =========================================================================
# ÓRDENES
# =========================================================================
def es_stop(orden):
    return str(orden.get("type", "")).lower() in ("stop_market", "stop")


def crear_stop(exchange, lado_posicion, cantidad, stop_price):
    """Coloca un stop_market reduceOnly para la posición indicada."""
    return exchange.create_order(
        symbol=SYMBOL,
        type="stop_market",
        side="sell" if lado_posicion == "long" else "buy",
        amount=abs(cantidad),
        params={
            "stopPrice": float(exchange.price_to_precision(SYMBOL, stop_price)),
            "reduceOnly": True,
        },
    )


def cerrar_posicion(exchange, cantidad):
    """Cierra la posición a mercado y cancela las órdenes pendientes. Devuelve True si cerró."""
    try:
        side_cierre = "sell" if cantidad > 0 else "buy"
        log.info(f"Cerrando posición: {side_cierre.upper()} {abs(cantidad):.4f} BTC")
        exchange.create_order(
            symbol=SYMBOL,
            type="market",
            side=side_cierre,
            amount=abs(cantidad),
            params={"reduceOnly": True},
        )
    except Exception as e:
        log.error(f"Error al cerrar posición: {e}")
        return False

    try:
        exchange.cancel_all_orders(SYMBOL)
    except Exception as e:
        log.warning(f"No se pudieron cancelar las órdenes pendientes: {e}")
    return True


def abrir_orden(exchange, lado, tamano, stop_price):
    """
    Abre una orden de mercado y coloca el stop loss.
    Si el stop no se puede colocar, cierra la posición de inmediato
    (nunca se deja una posición abierta sin stop). Devuelve True si quedó abierta con stop.
    """
    log.info(f"Abriendo {lado.upper()} | Tamaño: {tamano:.4f} BTC | Stop: {stop_price:.2f}")
    try:
        orden = exchange.create_order(
            symbol=SYMBOL,
            type="market",
            side="buy" if lado == "long" else "sell",
            amount=tamano,
        )
        log.info(f"Orden ejecutada: {orden.get('id')}")
    except Exception as e:
        log.error(f"Error al abrir orden: {e}")
        return False

    try:
        crear_stop(exchange, lado, tamano, stop_price)
        log.info(f"Stop Loss colocado en: {stop_price:.2f}")
        return True
    except Exception as e:
        log.critical(f"No se pudo colocar el stop ({e}) — cerrando la posición por seguridad.")
        cerrar_posicion(exchange, tamano if lado == "long" else -tamano)
        return False


def actualizar_stop_loss(exchange, stop_price, cantidad):
    """
    Trailing: coloca primero el stop nuevo y después cancela los anteriores,
    así la posición nunca queda sin stop. Devuelve True si se actualizó.
    """
    lado = "long" if cantidad > 0 else "short"
    try:
        nueva = crear_stop(exchange, lado, cantidad, stop_price)
        for orden in exchange.fetch_open_orders(SYMBOL):
            if es_stop(orden) and orden.get("id") != nueva.get("id"):
                exchange.cancel_order(orden["id"], SYMBOL)
        log.info(f"Trailing Stop actualizado a: {stop_price:.2f}")
        return True
    except Exception as e:
        log.error(f"Error actualizando Stop Loss: {e}")
        return False


def sincronizar_trailing(exchange, trailing, lado, cantidad, precio_entrada, precio, distancia):
    """
    Reconstruye el estado del trailing cuando hay una posición abierta y el bot
    no lo tiene (por ejemplo, tras un reinicio): usa el stop que ya está en el
    exchange o, si no hay ninguno, coloca uno nuevo.
    """
    stops = []
    for orden in exchange.fetch_open_orders(SYMBOL):
        if es_stop(orden):
            p = orden.get("stopPrice") or (orden.get("info") or {}).get("stopPrice")
            if p:
                stops.append(float(p))

    if stops:
        stop = max(stops) if lado == "long" else min(stops)
        log.info(f"Estado recuperado: {lado.upper()} abierto con stop existente en {stop:.2f}")
    else:
        base = precio_entrada or precio
        stop = base - distancia if lado == "long" else base + distancia
        log.warning(f"Posición {lado.upper()} sin stop — colocando uno nuevo en {stop:.2f}")
        crear_stop(exchange, lado, cantidad, stop)

    trailing.iniciar(lado, stop)


# =========================================================================
# LOOP PRINCIPAL
# =========================================================================
def main():
    log.info("=" * 60)
    log.info("Bot iniciado — Estrategia: Ruptura Momentum + Bankroll")
    log.info("=" * 60)

    exchange = crear_exchange()
    trailing = TrailingStop()
    ultima_vela_evaluada = None   # una sola evaluación de entrada por vela cerrada

    while True:
        try:
            # 1. Datos: se descarta la última vela (en curso) y se usan solo velas cerradas
            df = obtener_ohlcv(exchange).iloc[:-1]
            df = calcular_indicadores(df)
            if df.empty:
                log.warning("Sin datos suficientes para calcular indicadores.")
                time.sleep(INTERVALO_SEGUNDOS)
                continue

            senal, ultima = evaluar_senal(df)
            vela_ts = ultima.name
            close_vela = ultima["close"]
            atr = ultima["atr"]
            maximo_local = ultima["maximo_local"]
            minimo_local = ultima["minimo_local"]

            # 2. Estado de la cuenta (si algo falla, se salta el ciclo; nunca se asume "sin posición")
            equity = obtener_equity(exchange)
            posicion = obtener_posicion(exchange)
            if equity is None or posicion is None:
                log.warning("No se pudo leer el estado de la cuenta — se reintenta en el próximo ciclo.")
                time.sleep(INTERVALO_SEGUNDOS)
                continue
            cantidad, precio_entrada = posicion
            precio = float(exchange.fetch_ticker(SYMBOL)["last"])

            log.info(
                f"BTC: {precio:.2f} | ADX: {ultima['adx']:.1f} | "
                f"EMA200: {ultima['ema200']:.2f} | Señal: {senal or 'ninguna'} | "
                f"Posición: {cantidad:.4f} | Equity: {equity:.2f} USDT"
            )

            # 3. Sin posición → buscar entrada (una vez por vela cerrada)
            if cantidad == 0:
                if trailing.stop is not None:
                    # La posición se cerró (por el stop, por ejemplo): limpiar órdenes huérfanas
                    try:
                        exchange.cancel_all_orders(SYMBOL)
                    except Exception as e:
                        log.warning(f"No se pudieron cancelar órdenes pendientes: {e}")
                    trailing.reset()

                if vela_ts != ultima_vela_evaluada:
                    lado = None
                    if senal == "long" and close_vela >= maximo_local:
                        lado = "long"
                    elif senal == "short" and close_vela <= minimo_local:
                        lado = "short"

                    if lado:
                        tamano, dist_stop = calcular_tamano_posicion(exchange, equity, atr, precio)
                        if tamano > 0:
                            stop_inicial = precio - dist_stop if lado == "long" else precio + dist_stop
                            if abrir_orden(exchange, lado, tamano, stop_inicial):
                                trailing.iniciar(lado, stop_inicial)
                    ultima_vela_evaluada = vela_ts

            # 4. Con posición abierta → salida por condición o trailing stop
            else:
                lado = "long" if cantidad > 0 else "short"
                dist_stop = atr * ATR_MULTIPLICADOR

                if trailing.stop is None:
                    sincronizar_trailing(exchange, trailing, lado, cantidad, precio_entrada, precio, dist_stop)

                if senal != lado:
                    log.info(f"Condición {lado.capitalize()} desactivada — cerrando posición.")
                    if cerrar_posicion(exchange, cantidad):
                        trailing.reset()
                else:
                    nuevo_stop = trailing.candidato(precio, dist_stop)
                    if nuevo_stop is not None and actualizar_stop_loss(exchange, nuevo_stop, cantidad):
                        trailing.stop = nuevo_stop

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