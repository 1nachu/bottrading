# 🤖 Bot de Trading BTC — Ruptura Momentum + Gestión de Bankroll

Bot de trading automático en **Python** para operar **BTC/USDT** en **Binance Futures (USDⓈ-M)** sobre velas de **4 horas**. Replica una estrategia desarrollada y validada en TradingView (Pine Script), combinando tres filtros independientes con position sizing dinámico y trailing stop basado en ATR.

| Par | Temporalidad | Riesgo por operación | Stop Loss |
| --- | --- | --- | --- |
| BTC/USDT | 4H | 2% del capital | Trailing ATR × 2.4 |

> ⚠️ **Aviso de riesgo:** el trading de criptomonedas con apalancamiento implica un riesgo significativo de pérdida de capital. Este proyecto es una herramienta de automatización con fines educativos, **no es asesoramiento financiero**. Los resultados de backtesting no garantizan resultados futuros.

---

## 📑 Índice

- [Estrategia](#-estrategia)
- [Condiciones de entrada](#-condiciones-de-entrada)
- [Gestión de riesgo](#-gestión-de-riesgo)
- [Arquitectura](#-arquitectura)
- [Instalación](#-instalación)
- [Configuración](#-configuración)
- [Uso](#-uso)
- [Logs y monitoreo](#-logs-y-monitoreo)
- [Seguridad](#-seguridad)
- [Recomendaciones](#-recomendaciones)

---

## 📈 Estrategia

Sistema de seguimiento de tendencia basado en tres pilares:

| Pilar | Indicador | Función |
| --- | --- | --- |
| Tendencia macro | **EMA 200** | Solo opera en la dirección del mercado dominante |
| Fuerza del movimiento | **ADX** | Evita mercados laterales y sin dirección |
| Ruptura de precio | **Canal Donchian 20** | Confirma momentum al superar un nivel significativo reciente |

Además se usa el **ATR 14** para medir la volatilidad: define la distancia del stop y el tamaño de la posición.

### Interpretación del ADX

| ADX | Interpretación | Acción del bot |
| --- | --- | --- |
| < 20 | Mercado lateral | No opera |
| 20 – 40 | Tendencia moderada | Opera si la EMA coincide |
| 40 – 60 | Tendencia fuerte | Opera con alta confianza |
| > 60 | Tendencia muy fuerte | Opera con máxima confianza |

---

## 🎯 Condiciones de entrada

Se requieren las **tres condiciones simultáneas**.

**Long**
- Precio **>** EMA 200
- ADX **>** 20
- Cierre de la vela **≥** máximo Donchian 20 (máximo de las 20 velas de 4H **anteriores**, sin incluir la actual)

**Short**
- Precio **<** EMA 200
- ADX **>** 20
- Cierre de la vela **≤** mínimo Donchian 20 (mínimo de las 20 velas anteriores)

**Velas cerradas:** las señales (EMA, ADX, Donchian, ATR) se calculan solo con velas **cerradas**; la vela en curso se descarta. La entrada se evalúa **una sola vez por vela cerrada** y se ejecuta a mercado, lo que además evita reentrar en la misma vela después de que salte el stop.

**Salida adicional:** el bot cierra la posición a mercado si deja de cumplirse la condición de entrada (el precio cruza la EMA 200 en contra, o el ADX cae por debajo de 20).

---

## 🛡️ Gestión de riesgo

### Position sizing dinámico

```
Distancia al stop = ATR × 2.4
Riesgo en USD     = Capital total × 2%
Tamaño posición   = Riesgo en USD ÷ Distancia al stop
```

Ejemplo: capital $1,000 · ATR $500 → distancia $1,200 · riesgo $20 → tamaño `0.0167 BTC`.

A mayor volatilidad, menor tamaño: el riesgo absoluto se mantiene constante.

Además, el tamaño se limita al nocional máximo permitido por el apalancamiento (`equity × LEVERAGE × 0.95`), se redondea a la precisión del exchange y no se opera si queda por debajo de los mínimos de Binance (cantidad y nocional mínimo).

### Trailing stop

- **Long:** stop inicial `entrada − ATR×2.4`; en cada ciclo `stop = max(stop_anterior, precio − distancia)` (solo sube).
- **Short:** stop inicial `entrada + ATR×2.4`; en cada ciclo `stop = min(stop_anterior, precio + distancia)` (solo baja).

El trailing usa el **precio en tiempo real** y solo se actualiza en el exchange cuando el nivel mejora. El stop nuevo se coloca **antes** de cancelar el anterior, así la posición nunca queda sin stop.

Ejemplo Long (entrada $65,000 · ATR $800 · distancia $1,920):

| Evento | Stop |
| --- | --- |
| Apertura | $63,080 |
| BTC sube a $67,000 | $65,080 ↑ |
| BTC sube a $70,000 | $68,080 ↑ |
| BTC baja a $69,000 | $68,080 (se mantiene) |
| BTC baja a $68,080 | Stop activado → cierre con +$3,080 |

---

## 🏗️ Arquitectura

El bot se compone de módulos independientes que corren en un loop continuo:

| Módulo | Función |
| --- | --- |
| Conexión (`ccxt`) | Comunicación con Binance Futures: datos, órdenes, balance y posiciones |
| Datos de mercado | Descarga las últimas 1000 velas de 4H, descarta la vela en curso y las carga en un DataFrame de `pandas` |
| Indicadores | EMA 200, ADX/DI+/DI−, Donchian 20 y ATR 14 (replicando Pine Script) |
| Evaluación de señal | Devuelve `long`, `short` o ninguna señal |
| Position sizing | Calcula el tamaño según capital, riesgo y distancia al stop |
| Gestión de órdenes | Abre/cierra posiciones y coloca/actualiza el stop loss; si el stop no se puede colocar, cierra la posición |
| Trailing stop | Actualiza el stop a favor de la posición; tras un reinicio recupera el estado desde el exchange |
| Loop principal | Ejecuta el ciclo cada 60 s y gestiona errores |

### Flujo de ejecución

```mermaid
flowchart TD
    A[Descargar velas 4H y descartar la vela en curso] --> B[Calcular indicadores]
    B --> C[Consultar equity y posición]
    C --> D{¿Posición abierta?}
    D -- No --> E{¿Vela nueva + señal + ruptura?}
    E -- Sí --> F[Abrir posición + stop inicial]
    E -- No --> H
    D -- Sí --> G{¿Condición vigente?}
    G -- Sí --> G1[Actualizar trailing stop si mejora]
    G -- No --> G2[Cerrar a mercado]
    F --> H[Registrar en log]
    G1 --> H
    G2 --> H
    H --> I[Esperar 60 s] --> A
```

### Reinicios

Si el bot se reinicia con una posición abierta, recupera el stop existente desde Binance (o coloca uno nuevo si no hay) y continúa con el trailing.

### Manejo de errores

| Error | Comportamiento |
| --- | --- |
| `NetworkError` | Espera 30 s y reintenta |
| `ExchangeError` | Espera 60 s y reintenta |
| Error inesperado | Lo registra en el log y espera 60 s |

El bot **nunca se detiene** por un error temporal. Si no puede leer el balance o la posición, **salta el ciclo** (nunca asume que no hay posición abierta).

---

## ⚙️ Instalación

### Requisitos

- Python **3.9+**
- Cuenta de Binance con el mercado de **Futuros USDⓈ-M** activado
- API Key con permiso de **Futures Trading** (⚠️ nunca habilitar retiros)
- Capital recomendado: mínimo **200 USDT** en la cuenta de futuros. Binance exige un nocional mínimo por orden (≈100 USDT en BTC/USDT); si el tamaño calculado queda por debajo, el bot no opera y lo avisa en el log

### Dependencias

```bash
git clone https://github.com/1nachu/bottrading.git
cd bottrading
pip install ccxt pandas pandas-ta
```

---

## 🔧 Configuración

Los parámetros están al inicio de `trading_bot.py` y se pueden editar sin tocar la lógica:

| Parámetro | Default | Descripción |
| --- | --- | --- |
| `SYMBOL` | `"BTC/USDT:USDT"` | Perpetuo USDⓈ-M (símbolo unificado de ccxt para futuros) |
| `TIMEFRAME` | `"4h"` | Temporalidad — debe coincidir con TradingView |
| `LEVERAGE` | `5` | Apalancamiento en Binance Futures |
| `VELAS_HISTORICO` | `1000` | Velas descargadas (la EMA 200 necesita historial para converger) |
| `RIESGO_POR_TRADE` | `0.02` (2%) | Porcentaje del capital en riesgo por operación |
| `LONGITUD_RUPTURA` | `20` | Períodos del canal Donchian |
| `ADX_LONGITUD` | `14` | Períodos del ADX |
| `ADX_UMBRAL` | `20` | ADX mínimo para una señal válida |
| `ATR_LONGITUD` | `14` | Períodos del ATR |
| `ATR_MULTIPLICADOR` | `2.4` | Distancia del stop = ATR × multiplicador |
| `EMA_LONGITUD` | `200` | Períodos de la EMA de tendencia |
| `INTERVALO_SEGUNDOS` | `60` | Frecuencia del loop, en segundos |

### Credenciales

Preferí **variables de entorno** en lugar de escribir las claves en el código:

El bot lee las claves de variables de entorno y no arranca si faltan:

```bash
export BINANCE_API_KEY="tu_api_key"
export BINANCE_API_SECRET="tu_api_secret"
```

---

## ▶️ Uso

```bash
python trading_bot.py
```

Para dejarlo corriendo en segundo plano (Linux/Mac):

```bash
nohup python trading_bot.py &
# o con screen / tmux
```

En Windows, usá una tarea programada o `pythonw trading_bot.py`. Para detenerlo en primer plano: `Ctrl + C`.

### Testnet (recomendado antes de operar con dinero real)

Activá el modo de pruebas con una variable de entorno (usa `set_sandbox_mode` de ccxt) para validar la lógica con dinero ficticio:

```bash
export BINANCE_TESTNET=true
```

El testnet de Binance Futures requiere **claves propias del testnet**, distintas de las de tu cuenta real. Verificá en testnet que se abran las posiciones y los stops antes de operar con capital real, y mantené `ccxt` actualizado (`pip install -U ccxt`), ya que Binance cambia su API de órdenes condicionales con cierta frecuencia.

---

## 📊 Logs y monitoreo

El bot escribe `bot_trading.log` en la carpeta de ejecución. Para verlo en vivo:

```bash
tail -f bot_trading.log
```

Ejemplo:

```
2025-03-12 14:00:05 [INFO] BTC: 83450.00 | ADX: 31.2 | EMA200: 79200.00 | Señal: long | Posición: 0.0000 | Equity: 1000.00 USDT
2025-03-12 14:00:06 [INFO] Abriendo LONG | Tamaño: 0.0120 BTC | Stop: 81530.00
2025-03-12 14:01:05 [INFO] Trailing Stop actualizado a: 81650.00
```

| Mensaje | Significado |
| --- | --- |
| `Señal: long / short` | Se detectaron condiciones de entrada válidas |
| `Señal: ninguna` | Sin condiciones activas; el bot espera |
| `Abriendo LONG/SHORT` | Se está enviando una orden de entrada |
| `Trailing Stop actualizado` | El stop se movió a favor de la posición |
| `Condición desactivada` | La condición de entrada ya no se cumple; cierre próximo |
| `Error de red / Error del exchange` | Problema temporal; reintento automático |
| `Posición ... sin stop — colocando uno nuevo` | Tras un reinicio, el bot encontró una posición sin stop y colocó uno |
| `cerrando la posición por seguridad` | No se pudo colocar el stop inicial, así que se cerró la posición |

---

## 🔒 Seguridad

- **Nunca** compartas tu API Secret.
- **Nunca** habilites permisos de **retiro** en la API key del bot.
- Restringí la API key por **IP** en Binance.
- **No subas claves reales a GitHub.** El código las lee de variables de entorno; si usás un archivo `.env`, agregalo a `.gitignore` (junto con `bot_trading.log`).

---

## 💡 Recomendaciones

- Probá siempre en **testnet** antes de usar capital real.
- Monitoreá el bot con frecuencia, sobre todo en las primeras semanas.
- Operá solo con capital que puedas permitirte perder.

### Diferencias posibles frente a TradingView

- El bot decide con velas cerradas y entra a mercado cuando detecta la vela nueva, por lo que el precio de entrada puede diferir del cierre simulado en TradingView.
- El slippage real de las órdenes a mercado difiere del simulado en TradingView (3 pips en el Pine Script).
- El bot contempla las comisiones reales de Binance Futures (~0.04% maker / 0.06% taker).

---

## 📌 Referencia rápida

| Acción | Comando / Valor |
| --- | --- |
| Instalar dependencias | `pip install ccxt pandas pandas-ta` |
| Ejecutar | `python trading_bot.py` |
| Ver log | `tail -f bot_trading.log` |
| Detener | `Ctrl + C` |
| Par / Temporalidad | BTC/USDT · 4h |
| Riesgo / Stop | 2% por operación · Trailing ATR × 2.4 |
| Filtros | EMA 200 · ADX > 20 · Donchian 20 |

---

## 📄 Licencia

Este proyecto está bajo la licencia [MIT](LICENSE).
