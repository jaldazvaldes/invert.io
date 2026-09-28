# invert.io

App personal de trading intradía por señales, para **cripto** (Revolut X) y **acciones** (Trading 212).
Arquitectura y calendario completos en [docs/PLAN.md](docs/PLAN.md).

> No es asesoramiento financiero. Las estrategias de ejemplo sirven para validar el sistema.
> El flujo obligatorio es backtest → paper → demo → real, y el modo real solo lo activa el usuario.

## Requisitos

- [uv](https://docs.astral.sh/uv/) (instala Python 3.12 automáticamente)

## Primeros pasos

```bash
uv sync                      # crea el entorno e instala dependencias
cp .env.example .env         # rellena solo lo que vayas a usar
uv run invertio --help       # comandos disponibles
uv run invertio config check # valida .env + config/*.yaml
uv run invertio db upgrade   # crea/actualiza la base de datos SQLite en data/
```

## Datos y backtesting

```bash
# 1 año de velas de 5 min (cripto: datos públicos de OKX, sin claves)
uv run invertio data download -m revolutx:BTC/EUR -m revolutx:ETH/EUR --days 365
# acciones: necesita ALPACA_API_KEY / ALPACA_API_SECRET (cuenta paper gratuita)
uv run invertio data download -m trading212:AAPL --days 365
uv run invertio data list

# misma estrategia sobre varios mercados, con 100 de capital en cada uno
uv run invertio backtest -s ema_cross -m revolutx:BTC/EUR -m trading212:AAPL --days 365 --capital 100
```

Cada backtest guarda en `data/reports/<fecha>_<estrategia>/` un `report.html` (curvas de
equity y métricas), `summary.json`, `trades.csv` y `equity.csv`. Los resultados son netos de
comisiones, spread y deslizamiento. Las reglas de ejecución simulada están en
[src/invertio/execution/sim.py](src/invertio/execution/sim.py).

Estrategias disponibles (parámetros en `config/strategies/`): `ema_cross`, `rsi_reversion`
y `puntuacion`.

## Puntuación y escáner

La estrategia `puntuacion` da a cada mercado una nota de 0 a 100 que suma seis factores:
tendencia 25, momentum 20, MACD 15, RSI 15, volumen 10 y ruptura 15. Compra cuando la nota
cruza hacia arriba el umbral de entrada (70), siempre que el movimiento típico del mercado cubra
los costes. Cierra cuando la nota baja a 40 o salta el stop o el objetivo. Pesos y umbrales en
`config/strategies/puntuacion.yaml`.

```bash
uv run invertio scan --tf 1h          # nota actual de los mercados de app.yaml
uv run invertio scan --tf 1h --all    # todos los pares en EUR de Revolut X (sin stablecoins)
```

El escáner muestra el ranking con el desglose por factor, si el movimiento cubre costes y la
entrada, el stop y el objetivo. Es el resultado de unas reglas fijas, no una recomendación.

## Paper trading en vivo

Precios reales y operaciones simuladas (sin dinero). Qué mercados se vigilan y con qué
estrategia se define en `config/live.yaml`.

```bash
uv run invertio run            # arranca; Ctrl+C para parar (las posiciones se conservan)
uv run invertio paper report   # resultados por mercado, netos de costes (cripto vs acciones)
uv run invertio paper reset    # borra el historial de paper para empezar de cero
```

En cada cierre de vela la app pide los datos y ejecuta en el simulador las órdenes y los
stops. Después pasa la vela a la estrategia; si hay señal, el gestor de riesgo la aprueba o la
rechaza. Todo queda en `data/invertio.db`. Al reiniciar, recupera las posiciones y aplica los
stops que se hubieran tocado mientras estaba parada.

### Avisos y control por Telegram

1. En Telegram, habla con **@BotFather**, crea un bot (`/newbot`) y copia el token en
   `TELEGRAM_BOT_TOKEN` de `.env`.
2. Abre tu bot y envíale `/start`.
3. Ejecuta `uv run invertio telegram chat-id` y copia el número en `TELEGRAM_CHAT_ID`.
4. Comprueba con `uv run invertio telegram test`.

Recibirás cada señal (entrada, stop, objetivo, tamaño y pérdida máxima), las compras y ventas
ejecutadas con su resultado, y avisos si faltan datos. Comandos: `/status`, `/posiciones`,
`/hoy`, `/puntos`, `/pausa`, `/reanudar` y `/panico`, que pide confirmación antes de
cerrar todo. El bot
solo responde a tu `chat_id`. Sin Telegram configurado, los avisos salen por la consola.

## Panel web

`invertio run` arranca también el panel en **http://localhost:8000**. El panel muestra:
- estado del motor y botones de pausa, reanudar y pánico (con confirmación);
- capital y posiciones con su stop y su objetivo;
- gráfico de velas con las compras y ventas marcadas;
- nota de puntuación de cada mercado con su desglose;
- actividad (señal → decisión del riesgo → orden → ejecución) y operaciones cerradas;
- evolución del capital, escáner y backtests.

Se actualiza solo en tiempo real.

```bash
npm --prefix frontend install     # solo la primera vez
npm --prefix frontend run build   # genera frontend/dist, que sirve el motor
uv run invertio panel             # panel en solo lectura, sin arrancar el motor
```

El panel solo escucha en `127.0.0.1`. Las acciones exigen un token de sesión y se rechazan
las cabeceras Host y los Origin ajenos, así que otra web abierta en el navegador no puede
pulsar botones por ti. Para desarrollar el frontend con recarga en caliente:
`npm --prefix frontend run dev` (http://localhost:5173, con el motor arrancado).

## Desarrollo

```bash
uv run pytest               # tests
uv run ruff check .         # lint
uv run ruff format .        # formato
uv run mypy src             # tipos
```

## Estructura

```
src/invertio/
  core/         modelos de dominio, eventos, bus de eventos, reloj
  config/       settings (.env) + config YAML (venues, riesgo)
  data/         descarga de históricos (ccxt, Alpaca) y almacén Parquet
  indicators/   indicadores incrementales (SMA, EMA, RSI, ATR)
  strategies/   contrato de estrategia + ejemplos
  risk/         gestor de riesgo (tamaño de posición y límites)
  execution/    enrutado de órdenes + bróker simulado
  portfolio/    efectivo, posiciones, equity, operaciones cerradas
  backtest/     motor, métricas e informes
  live/         motor en vivo, feeds, control (pausa/pánico), informe de paper
  notify/       avisos (Telegram o consola) y comandos del bot
  api/          API del panel web (FastAPI + WebSocket)
  persistence/  SQLAlchemy + migraciones Alembic
  cli.py        línea de comandos (typer)
config/         app.yaml (venues), risk.yaml (riesgo), live.yaml (modo en vivo), strategies/*.yaml
data/           SQLite + históricos (no se versiona)
tests/
frontend/       panel web (React + Vite + TypeScript + lightweight-charts)
```

## Estado

| Fase | Estado |
|---|---|
| 0. Cimientos | ✅ |
| 1. Datos + backtesting | ✅ cripto · acciones listas a falta de claves de Alpaca |
| 2. Paper trading + Telegram | ✅ código · pendiente la prueba de 72 h en tu equipo |
| 3. Panel web | ✅ |
| 4. Conectores reales | pendiente |
| 5. Endurecimiento + despliegue | pendiente |
