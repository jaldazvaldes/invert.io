# invert.io — Arquitectura y planning

## Contexto

Queremos una app personal de trading automático por señales, para **acciones y cripto**, operando desde **España**. Requisitos acordados:

- **Capital inicial:** unos 100 € (luego lo que se llegue a tener).
- **Señales:** estrategias propias basadas en indicadores técnicos, probadas antes con backtesting.
- **Velocidad:** intradía, con velas de 1 a 15 minutos.
- **Interfaz:** panel web + bot de Telegram.
- **Proveedores:** un exchange de cripto muy barato y un bróker de acciones muy barato, por separado.

La carpeta `invert.io` está vacía: el proyecto empieza desde cero.

> Nota: no soy asesor financiero. Las estrategias de ejemplo sirven para validar el sistema, no son recomendaciones de inversión. El plan obliga a pasar por paper trading antes de usar dinero real, y el modo real solo lo activas tú.

---

## Decisiones clave (resultado de la investigación)

### Proveedores

| Uso | Elegido | Por qué | Alternativa |
|---|---|---|---|
| **Cripto** | **Revolut X** | 0 % maker / 0,09 % taker, comisión fija. Licencia MiCA (Revolut Digital Assets Europe, CySEC), disponible en España. Soportado por `ccxt` (`revolutx`): velas, libro de órdenes, órdenes limit/market/post-only/TPSL y balances. | **OKX EEA** (`myokx` en ccxt): 0,08 / 0,10 % solo si abres la cuenta X-Perps (sin ella 0,20 / 0,35 %). Tiene websockets y modo demo. Cambiar a OKX es cambiar la config. |
| **Acciones** | **Trading 212** (cuenta Invest) | 0 € de comisión, fracciones desde 1 €. API pública con órdenes en real, entorno demo, claves con permisos limitados y lista blanca de IPs. | Alpaca: la mejor API, pero no está confirmado que abra cuentas reales a residentes en España (hay que preguntar a soporte). |
| **Datos de acciones** | **Alpaca Market Data, plan gratuito** | La API de Trading 212 no da precios ni velas. La cuenta paper de Alpaca es gratis en cualquier país (basta un email) e incluye el feed IEX en tiempo real y velas históricas. | yfinance, como respaldo para backtests. |

**Descartados:**
- **Binance:** dejó de operar en la UE el 1 de julio de 2026 por no tener licencia MiCA.
- **Interactive Brokers:** en España solo existe la tarifa fija. En acciones cobra mínimo 1 $ o el 1 % del importe en órdenes fraccionadas, y en cripto mínimo 1,75 $ (con tope del 1 %). Con 100 € eso son ~2 € por operación de ida y vuelta.
- **Kraken:** 0,25–0,40 % de comisión, demasiado caro.

### Coste real con 100 € por operación (ida y vuelta, sin contar el spread)

| Venue | Ida y vuelta |
|---|---|
| Revolut X | **0 €** si ambas órdenes son maker, **0,18 €** si ambas son taker |
| OKX EEA con X-Perps | 0,16–0,20 € |
| Trading 212, acciones en EUR | **0 €** |
| Trading 212, acciones en USD (cuenta en EUR) | **0,30 €** (0,15 % de cambio de divisa por lado; la API no admite cuenta multidivisa) |
| IBKR (descartado) | ~2 € |

**Consecuencias para el diseño:**
- El motor preferirá órdenes límite *post-only* (maker, 0 % en Revolut X) cuando la estrategia lo permita.
- Los backtests descontarán la comisión, el spread y el deslizamiento de cada venue.
- Los informes mostrarán cuánto se ha pagado en comisiones.

### Framework: app propia en Python

Ningún framework maduro soporta los dos venues elegidos:
- **NautilusTrader:** tiene 18 integraciones, pero ni Trading 212 ni Revolut X.
- **Freqtrade:** solo cripto.
- **LEAN (QuantConnect):** no soporta Trading 212.

Por eso haremos una app propia apoyada en librerías probadas: `ccxt` para cripto y un adaptador propio para Trading 212. De NautilusTrader copiamos la idea central: **el mismo código de estrategia funciona en backtest, paper y real.**

---

## Arquitectura

Un **monolito modular**: un solo proceso Python asíncrono (asyncio) con un bus de eventos interno. No hace falta Kafka ni Redis porque solo hay un usuario.

```
 Revolut X (ccxt) ──┐                                                     ┌──► Revolut X
 Alpaca data ───────┤                                                     ├──► Trading 212
                    ▼                                                     │
 ┌─────────────────────────── invertio (Python, asyncio) ───────────────────────────┐
 │ DataFeeds ─► [Bar cerrado] ─► StrategyRunner ─► [Signal] ─► RiskManager            │
 │                                                                  │ aprobada/rechazada + motivo
 │                                                                  ▼                 │
 │ Portfolio / P&L ◄── [Fill] ◄── Brokers ◄── [OrderIntent] ◄── OMS (estado de órdenes)│
 │       │                                                                            │
 │       ├──► Persistence (SQLite: señales, decisiones, órdenes, fills, equity)       │
 │       ├──► Notifier (Telegram: avisos + /status /pausa /reanudar /panico)          │
 │       └──► API (FastAPI REST + WebSocket) ◄──── Panel web (React)                  │
 └────────────────────────────────────────────────────────────────────────────────────┘
```

### Modos de ejecución

El código de estrategia, riesgo y portfolio es el mismo en todos los modos. Solo cambian el origen de datos y el bróker.

| Modo | Datos | Bróker | Dinero |
|---|---|---|---|
| `backtest` | Históricos (Parquet) | `SimBroker`, que modela comisión, spread y deslizamiento por venue | — |
| `paper` | Precios reales en vivo | `SimBroker` | Ninguno. Revolut X no requiere claves para esto. |
| `demo` | Precios reales | Trading 212 demo (y OKX demo si se usa) | Ficticio |
| `live` | Precios reales | Revolut X / Trading 212 | **Real**. Exige `TRADING_MODE=live` y `LIVE_CONFIRM=yes` en `.env`, y lo activas tú. |

### Piezas centrales (interfaces)

```python
class Strategy(Protocol):  # recibe solo velas cerradas, así no puede "ver el futuro"
    id: str
    symbols: list[str]
    timeframe: str

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> list[Signal]: ...


@dataclass(frozen=True)
class Signal:  # todas las fuentes de señal acaban aquí
    strategy_id: str
    symbol: str
    side: Side  # BUY / SELL / CLOSE (solo largos)
    stop_loss: Decimal
    take_profit: Decimal | None
    ts: datetime


class Broker(Protocol):  # SimBroker, CcxtBroker (Revolut X / OKX), Trading212Broker
    async def submit(self, req: OrderRequest) -> OrderAck: ...  # con client_order_id idempotente
    async def cancel(self, order_id: str) -> None: ...
    async def open_orders(self) -> list[Order]: ...
    async def positions(self) -> list[Position]: ...
    async def balance(self) -> Balance: ...


class MarketDataFeed(Protocol):
    def bars(self, symbols: list[str], timeframe: str) -> AsyncIterator[Bar]: ...
```

- **Feeds en vivo:** Revolut X no tiene websockets. Con velas de 1 a 15 minutos basta con consultar por REST al cerrar cada vela (el límite público es 1 petición por segundo). Si se cambia a OKX, el adaptador usará websockets.
- **OMS (gestor de órdenes):**
  - Cada orden pasa por estos estados: NEW → SUBMITTED → PARTIAL → FILLED, o termina en CANCELED / REJECTED.
  - Usa `Decimal` y respeta la precisión y el importe mínimo de cada mercado (`amount_to_precision` en ccxt).
  - Al arrancar, y cada N segundos, se sincroniza con el bróker (órdenes abiertas, posiciones y saldo).
- **Robustez:**
  - Reintentos con espera creciente y limitador de peticiones (cabeceras `x-ratelimit-*` en Trading 212, `enableRateLimit` en ccxt).
  - Si los datos llegan con retraso, no se opera.
  - Las acciones solo se operan con el mercado de NYSE abierto (15:30–22:00, hora de España), usando `exchange_calendars`.

### Gestión de riesgo (obligatoria, configurable en YAML)

- **Qué se puede operar:** solo contado, sin apalancamiento ni posiciones cortas. Solo símbolos de una lista blanca.
- **Stop-loss obligatorio en cada entrada:**
  - Se coloca en el propio bróker cuando lo permite (TPSL en Revolut X, órdenes stop en Trading 212).
  - Si no lo permite, lo vigila la app y te avisa.
- **Límites:**
  - Tamaño de posición calculado por riesgo por operación, con un tope por posición.
  - Máximo de posiciones abiertas y de órdenes por hora.
  - Límite de pérdida diaria: si se alcanza, se detiene el trading hasta el día siguiente.
  - Pausa tras varias pérdidas seguidas.
  - No se entra si el spread supera un umbral.
- **Botón de pánico** (`/panico` en Telegram o botón en el panel): cancela todas las órdenes, cierra las posiciones y detiene el motor.
- **Registro:** cada señal guarda si el gestor de riesgo la aprobó o la rechazó, y por qué. Se puede ver en el panel.

### Seguridad

- Las claves van en `.env` (excluido de git), y las rellenas tú.
  - Revolut X usa un par de claves Ed25519 que se generan en tu equipo.
  - En Trading 212, la clave tendrá solo los permisos necesarios y la lista blanca de IPs.
  - Ninguna de las dos APIs permite retirar fondos.
- El panel solo escucha en `localhost`. Para acceder desde fuera se usará Tailscale.
- El bot de Telegram solo responde a tu `chat_id`.

### Stack

- **Backend:** Python 3.12+ con `uv`, `ccxt` (async), `httpx` (Trading 212), `alpaca-py` (solo datos), `polars`/`numpy`, indicadores incrementales propios (EMA, RSI, ATR, VWAP), `pydantic-settings` + YAML, SQLAlchemy 2 + SQLite (WAL) + Alembic, Parquet + DuckDB para los históricos, FastAPI + uvicorn, `aiogram` 3 (Telegram), `structlog`, `typer` (línea de comandos).
- **Frontend:** React + Vite + TypeScript + TanStack Query + `lightweight-charts` (la librería de gráficos open source de TradingView).
- **Calidad:** pytest + pytest-asyncio, `respx` (simula las respuestas HTTP), ruff, mypy.
- **Despliegue:** Docker Compose. Primero en local (Windows); después en un VPS en la UE para que funcione 24/7.

### Estructura del repositorio

El proyecto Python está en la raíz (no en `backend/`): así `uv run invertio …`, `.env`,
`config/` y `data/` comparten carpeta.

```
invert.io/
├─ pyproject.toml
├─ src/invertio/
│  ├─ core/         # modelos de dominio (Bar, Signal, Order, Fill, Position), eventos, bus, reloj
│  ├─ config/       # settings (.env) + YAML de venues y riesgo
│  ├─ data/         # feeds históricos y en vivo, almacenamiento Parquet, calendario de mercado
│  ├─ indicators/   # indicadores incrementales (idénticos en backtest y en vivo)
│  ├─ strategies/   # base.py + ejemplos (ema_cross.py, rsi_reversion.py)
│  ├─ risk/         # sizing, límites, circuit breakers, kill switch
│  ├─ execution/    # OMS + brokers/{sim.py, ccxt_broker.py, trading212.py}
│  ├─ portfolio/    # posiciones, P&L, equity
│  ├─ backtest/     # motor, modelos de costes por venue, métricas, informes
│  ├─ notify/       # bot de Telegram
│  ├─ api/          # REST + WebSocket para el panel
│  ├─ persistence/  # modelos SQLAlchemy + migraciones
│  └─ cli.py        # invertio download | backtest | run --mode paper|demo|live
├─ tests/
├─ frontend/
├─ config/            # app.yaml, risk.yaml, strategies/*.yaml
├─ data/              # parquet + sqlite (no se versiona)
├─ docker-compose.yml
└─ .env.example
```

---

## Planning por fases

| Fase | Contenido | Hecho cuando… | Duración orientativa |
|---|---|---|---|
| **0. Cimientos** | Repo con git, `uv`, estructura, configuración, modelos de dominio, bus de eventos, SQLite + Alembic, ruff/mypy/pytest | `pytest` pasa y `invertio --help` funciona | 2–3 días |
| **1. Datos + backtesting** | Descarga de históricos (Revolut X u OKX vía ccxt para cripto, Alpaca para acciones) a Parquet. Motor de backtest con costes de cada venue. Indicadores incrementales validados contra una implementación de referencia. Dos estrategias de ejemplo. Métricas: retorno, drawdown máximo, Sharpe, % de aciertos, profit factor y comisiones pagadas. | Informe de BTC/EUR 5m y de una acción en 5m, con curva de equity | 1–1,5 semanas |
| **2. Paper trading en vivo + Telegram** | Feeds en vivo, ejecución de estrategias, gestor de riesgo, OMS, SimBroker con precios reales, persistencia, sincronización con el bróker. Bot de Telegram con aviso en cada operación y comandos `/status`, `/pausa`, `/reanudar` y `/panico`. Las mismas estrategias corren en paper sobre cripto y acciones a la vez, con un **informe comparativo cripto vs acciones** neto de costes (ver sección abajo). | 72 h seguidas en paper sin caídas, botón de pánico probado e informe comparativo generado | 1,5–2 semanas |
| **3. Panel web** | Resumen (equity, P&L del día, posiciones). Gráfico de velas con marcas de entradas y salidas. Registro señal → decisión de riesgo → orden. Activar/desactivar estrategias, límites de riesgo, botón de pánico y visor de backtests. | Todo el estado se ve en vivo en el navegador | 1–1,5 semanas |
| **4. Conectores reales** | Adaptador ccxt para Revolut X (post-only, TPSL). Adaptador Trading 212, probado en su entorno demo. Guardas del modo live. | Orden de prueba ejecutada en Trading 212 demo; tests de los adaptadores con HTTP simulado | 1 semana |
| **5. Endurecimiento + despliegue + arranque gradual** | Docker Compose y VPS en la UE. Copias de seguridad de SQLite. Aviso por Telegram si la app se desconecta o deja de responder. Exportación CSV de operaciones para la declaración de la renta. Checklist de salida a real. Mínimo 2–4 semanas en paper y después importes mínimos en real, activados por ti. | Checklist completo y comparación real vs paper (deslizamiento real) | 1 semana + 2–4 semanas de observación |
| **6. Opcional** | OKX EEA con websockets, optimización walk-forward, más estrategias, reparto de capital entre estrategias, webhooks de TradingView. | — | Según interés |

**Total estimado:** unas 6–8 semanas hasta operar en real con importes pequeños.

**Orden de construcción:** primero cripto (se desarrolla y prueba mejor al funcionar 24/7). Los datos de acciones entran en la fase 1 y el adaptador de Trading 212 en la fase 4.

### Cripto vs acciones: se decide con datos

El informe comparativo (backtest en la fase 1, paper en las fases 2 y 5) muestra, para cada estrategia y mercado:
- Resultado neto después de comisiones, spread y deslizamiento.
- Drawdown máximo, número de operaciones y comisiones pagadas.
- Diferencia entre backtest y paper.

Con esos números decides tú dónde (y si) poner dinero real en la fase 5.

---

## Riesgos y puntos a verificar al implementar

- **API de Trading 212 en beta:**
  - No da datos de mercado (por eso usamos Alpaca).
  - No admite cuenta multidivisa, así que las acciones en USD pagan el 0,15 % de cambio de divisa por lado.
  - Tiene límites de peticiones por endpoint.
  - La documentación se contradice sobre qué tipos de orden funcionan en real (una página dice que solo órdenes a mercado). Se comprobará en la fase 4. Si falla, el stop lo gestionará la app.
- **Revolut X:**
  - No tiene sandbox ni websockets. El paper trading se hace con nuestro simulador sobre precios reales.
  - El spread y la liquidez se miden en la fase 2. Si no convencen, pasamos a OKX EEA.
- **Histórico de Revolut X:** si no tiene suficiente profundidad, los backtests usarán las velas públicas de OKX, que no requieren cuenta.
- **Con 100 €:** las comisiones y los importes mínimos de orden pesan mucho. Los informes de backtest y de paper mostrarán el coste real para decidir con datos.

---

## Verificación

1. `uv run pytest`: tests unitarios de indicadores, reglas de riesgo, estados de órdenes y cálculo de P&L. Los adaptadores se prueban con HTTP simulado (`respx`).
2. `uv run invertio backtest --strategy ema_cross --symbol BTC/EUR --tf 5m`:
   - Genera un informe con métricas y costes.
   - Hay un test que comprueba que no se usan datos del futuro: las señales solo usan velas cerradas y la orden se ejecuta en la vela siguiente.
3. `uv run invertio run --mode paper`: varias horas con precios reales.
   - Avisos en Telegram, panel en `localhost` (lo revisaré en el navegador integrado).
   - Botón de pánico probado.
4. Modo `demo` contra Trading 212 demo con la clave demo que pongas en `.env` (solo dinero ficticio, y con tu visto bueno).
5. El modo `live` lo activas únicamente tú, después de completar el checklist de la fase 5.

---

## Fuentes

- [Binance deja la UE por MiCA (CoinDesk)](https://www.coindesk.com/policy/2026/06/26/binance-tells-eu-users-it-will-no-longer-provide-services-after-failing-to-secure-mica-license)
- [Exchanges con MiCA en España (Datawallet)](https://www.datawallet.com/crypto/best-crypto-exchanges-spain)
- [Revolut X: comisiones](https://www.revolut.com/legal/crypto-exchange-fees/) · [API REST de Revolut X](https://developer.revolut.com/docs/x-api/revolut-x-crypto-exchange-rest-api) · [ccxt vs API de Revolut X](https://docs.ccxt.com/docs/comparisons/ccxt-vs-revolutx-api) · [Revolut X en España](https://www.revolut.com/es-ES/revolut-x/)
- [Comisiones de OKX para la EEA](https://www.okx.com/en-eu/help/what-are-the-new-trading-fees-for-eea-users) · [ccxt myokx](https://docs.ccxt.com/docs/comparisons/ccxt-vs-myokx-api)
- [API de Trading 212](https://docs.trading212.com/api) · [Limitaciones de la API](https://docs.trading212.com/api/section/general-information/api-limitations) · [Clave API de Trading 212](https://helpcentre.trading212.com/hc/en-us/articles/14584770928157-Trading-212-API-key) · [Comisión de cambio de divisa](https://wise.com/gb/blog/trading-212-fx-fees)
- [Datos de mercado de Alpaca](https://docs.alpaca.markets/us/docs/about-market-data-api) · [Paper trading de Alpaca](https://docs.alpaca.markets/us/docs/paper-trading)
- [Comisiones de IBKR (acciones)](https://www.interactivebrokers.com/en/pricing/commissions-stocks.php) · [Comisiones de IBKR Irlanda (cripto)](https://www.interactivebrokers.ie/en/pricing/commissions-crypto-assets.php)
- [Integraciones de NautilusTrader](https://nautilustrader.io/docs/latest/integrations/)
