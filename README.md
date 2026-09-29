# invert.io

App personal de trading intradía por señales, para **cripto** (Revolut X) y **acciones** (Trading 212).
Arquitectura y calendario completos en [docs/PLAN.md](docs/PLAN.md).

> No es asesoramiento financiero. Las estrategias de ejemplo sirven para validar el sistema.
> El flujo obligatorio es backtest → paper → demo → real, y el modo real solo lo activa el usuario.

## Oportunidades explicadas (análisis público)

```bash
uv run invertio analyze             # análisis + panel local + Telegram si está configurado
uv run invertio analyze --no-telegram
uv run invertio analyze --no-panel --no-telegram --cycles 10
```

Por defecto, **Análisis** consulta datos públicos de Revolut X (región EEA,
pares al contado en EUR). No utiliza el motor de paper, no crea órdenes y en este modo no
necesita claves de trading. Funciona mientras el proceso y el ordenador estén encendidos. El panel conserva
el acceso a **Paper y laboratorio**; `invertio run` sigue siendo el comando de simulación.

Cada minuto analiza hasta 20 monedas. Cada cinco minutos selecciona por volumen negociado
en EUR, excluyendo stablecoins, mercados inactivos y spreads superiores al 0,3 %. Las monedas
con una oportunidad abierta conservan su plaza. La carga inicial prepara los indicadores;
las siguientes consultas recuperan solo las velas nuevas. Un único cliente limita las
peticiones públicas a una por segundo, incluyendo recuperación y profundidad.

Para observar **todos los pares activos en EUR**, con las claves existentes configuradas:

```powershell
uv run invertio analyze --all-markets --simulate --compare-strategies
```

`--all-markets` equivale a `market_scope: all_eur` en `config/analysis.yaml`. Descarga y conserva
velas de todos los pares EUR disponibles, incluidos los que no cumplen los filtros de entrada.
El panel distingue mercados observados y aptos por liquidez, muestra motivos y permite buscar
monedas. La selección se revisa cada cinco minutos; las velas se actualizan cada minuto.
Las monedas inactivas dejan de incorporarse, salvo las que aún tienen seguimiento abierto.

Este modo emplea únicamente consultas GET autenticadas para velas y libros, con un cliente
compartido entre las simulaciones y un máximo conservador de cinco peticiones por segundo.
La configuración y los tickers públicos siguen limitados a una petición por segundo. Verifica
que los pares y sus tamaños coincidan con el catálogo EEA antes de usar los datos autenticados.
Los límites publicados de estos endpoints están en la
[API oficial de Revolut X](https://developer.revolut.com/docs/api/revolut-x-crypto-exchange).
Activar la observación completa no habilita órdenes reales. Si faltan claves, el arranque
explica la configuración necesaria; no intenta sobrecargar la API pública.

Observar una moneda no permite comprarla automáticamente: se mantienen las exclusiones de
stablecoins, volumen, spread, profundidad y datos atrasados en cada estrategia. Ampliar el
universo conserva los saldos e historiales; las cuatro carteras incorporan las mismas monedas
en el mismo ciclo y registran los cambios en `universe_history`. Los datos anteriores se usan
para preparar indicadores, sin reconstruir compras pasadas. El ritmo de un minuto requiere
que Revolut responda a tiempo; los errores o retrasos se muestran y no relajan los filtros.

El ranking desglosa tendencia, momentum, MACD, RSI, volumen y ruptura. **70 puntos no significa
un 70 % de probabilidad de ganar.** Para publicar una oportunidad también exige datos recientes,
volumen negociado y profundidad suficiente para una referencia de 100 €, deslizamiento máximo
del 0,1 % por lado y objetivo neto positivo después de costes. Este importe no representa tu
capital ni una recomendación de tamaño de compra.

La entrada se estima recorriendo las ventas del libro; el coste de ida y vuelta usa la compra
y la venta de la misma cantidad, incluyendo comisión taker del 0,09 % por lado. Spread y
deslizamiento ya están incorporados en esos precios. Los retornos futuros proyectan la fricción
de salida observada al crear la señal; **son hipotéticos, no ejecuciones ni precios garantizados**.

Los niveles originales quedan congelados: stop a 2 ATR y objetivo a 3 ATR desde la entrada.
El seguimiento termina por esos niveles, nota de 40 o menos, filtros, pérdida de datos o cuatro
horas. Una vela que toca ambos niveles se clasifica como ambigua; los huecos de datos y una
caducidad sin precio exacto no reciben un retorno supuesto. Se omiten los máximos y mínimos de
la vela parcial en que se creó el aviso porque pueden preceder a su creación. Para rearmar una
moneda, la condición de entrada debe dejar de cumplirse con datos válidos.

El panel incluye antigüedad de cotizaciones y velas, motivos de exclusión, detalle, historial,
resumen por periodo y versión de reglas, y exportación CSV. Conserva evolución a 15 minutos,
una hora y cuatro horas, además de los extremos observados durante esas cuatro horas aunque
la señal termine antes. Los datos incompletos se distinguen de las pérdidas y los aciertos.
Las nuevas tablas de análisis y el historial paper son independientes; `paper reset` no borra
el análisis. Los históricos se guardan bajo la fuente `revolutx`, sin mezclar fuentes.

La configuración está en `config/analysis.yaml`; son umbrales experimentales. Si cambia mientras
hay señales abiertas, al reiniciar se interrumpe su seguimiento en vez de aplicarles otras reglas.
Un reinicio recupera el historial sin emitir oportunidades históricas como nuevas.

Telegram utiliza `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID`, descritos más abajo. Envía inicio y
desenlace, nunca el ranking cada minuto. Guarda el estado de entrega, reintenta fallos y caduca
avisos iniciales antiguos. La entrega se confirma solo después de responder Telegram; si la
respuesta se pierde tras aceptar el mensaje, su API no permite garantizar ausencia absoluta de
duplicados. Sin Telegram, el análisis y el panel siguen funcionando.

Para consultar datos guardados con el servicio detenido: `uv run invertio panel`.
El análisis expone lecturas bajo `/api/analysis`: `status`, `ranking`, `opportunities`,
`opportunities/{id}`, `summary` y `export.csv`. Las fechas `since`/`until` llevan zona horaria;
el historial devuelve hasta 500 filas por defecto (máximo 5000), y el CSV exporta todo el periodo.
La prueba de funcionamiento comprueba recogida, cálculos y seguimiento; no demuestra rentabilidad.

## Simulación automática con 50 € ficticios

```powershell
$env:PANEL_PORT = '8001'
uv run invertio analyze --simulate
# Añade --manual-orders solo si también quieres habilitar confirmaciones reales en Operar.
```

La pestaña **Simulación** enseña qué harían las reglas con una cartera ficticia. Se ejecuta
en el mismo proceso que el análisis, comparte cliente y limitador y no crea órdenes reales.
**Paper clásico** conserva el simulador anterior y su historial: su estado «detenido» no
significa que la recogida de análisis ni la nueva simulación estén paradas.

La cuenta empieza con 50 €, permite hasta cinco posiciones y asigna como máximo 10 € por
entrada, incluyendo comisión. Recicla el efectivo de las ventas, sin añadir capital. Compra
solo ante una oportunidad nueva desde su activación, nota de 70 o más y filtros de datos,
spread, liquidez y objetivo neto. Ajusta cantidades a la precisión y mínimos del mercado.
Registra también oportunidades descartadas por falta de efectivo o plazas; no las compra
retroactivamente cuando queda saldo libre. Al reiniciar conserva cartera e identificadores.

Las decisiones se toman al finalizar cada ciclo, usando libros recientes. Recalcula la
entrada para el importe ficticio real; no escala la estimación de análisis de 100 € ni
compra al precio histórico de creación de una señal. Descuenta la comisión de cada lado y
usa las ventas/compras del libro; no suma otra vez su spread o deslizamiento.

Fija stop a 2 ATR y objetivo a 3 ATR desde la entrada simulada. Cada minuto comprueba esos
niveles, nota de 40 o menos, fin de señal/filtros y cuatro horas de duración. **La salida se
calcula al libro disponible cuando se detecta, no garantiza el precio del nivel ni reconstruye
una ejecución dentro de una vela.** Si una señal fue ambigua, puede decidir salir ahora,
sin afirmar qué nivel se tocó antes. Por eso su resultado se distingue del seguimiento
hipotético de niveles en Análisis.

El seguimiento comprueba la viabilidad neta del objetivo original con la fricción vendedora
actual y la cantidad original. Un objetivo distinto calculado para una nueva entrada no
cierra por sí solo una oportunidad existente. Esta corrección se identifica como
`score-1m-v3`; los resultados anteriores conservan su versión y no se recalculan.

Sin un libro reciente con profundidad suficiente conserva la posición y el efectivo
comprometido, mostrando valoración desconocida. Cuando recupera datos, calcula una salida
al libro actual y registra la interrupción. No atribuye una ganancia/pérdida a precios ausentes.

El panel muestra efectivo, valor de liquidación estimado, beneficio realizado/no realizado,
curva, muestra de operaciones, comisiones, motivos y decisiones. Exporta el historial completo
en CSV; las tablas de pantalla limitan las últimas filas. Configuración: `config/simulation.yaml`;
checkpoint independiente en `simulation_state`. Un cambio de configuración no resetea la cuenta
automáticamente. Los resultados siguen siendo simulados: prueban el comportamiento del programa.

## Comparación prospectiva de cuatro estrategias

```powershell
$env:PANEL_PORT = '8001'
uv run invertio analyze --simulate --compare-strategies
```

La pestaña **Estrategias** compara cuatro carteras independientes de 50 € ficticios cada
una (200 € simulados en total). Todas comienzan al mismo tiempo, con asignaciones de hasta
10 € y cinco posiciones por cartera. La simulación anterior conserva su saldo e historial
en su propia pestaña; no se incorpora a la clasificación porque comenzó antes.

| Estrategia | Entrada | Salida por indicador | Stop / objetivo |
| --- | --- | --- | --- |
| Puntuación base | Fórmula de puntuación original ≥ 70 | Nota ≤ 40 | 2 / 3 ATR |
| Cruce de tendencia | EMA 20 cruza EMA 50 al alza y MACD positivo | EMA 20 < EMA 50 | 2 / 4 ATR |
| Ruptura con volumen | Nuevo máximo de 20 velas, volumen ≥ 1,5 × media anterior y EMA 20 > EMA 50 | Cierre < EMA 20 | 2 / 4 ATR |
| Rebote de RSI | RSI 14 cruza 30 al alza y sube el cierre | RSI ≥ 55 | 2 / 3 ATR |

Los periodos son velas cerradas de un minuto; ATR de 14 periodos. RSI caduca a las dos
horas y las demás a las cuatro. Las velas sin operaciones permiten mantener los indicadores
en el calendario, pero no producen entradas ni salidas nuevas por indicador en este
experimento. Los stops y objetivos se vigilan con el libro reciente. Las posiciones que
pierden datos quedan señaladas y solo se liquidan hipotéticamente con un libro válido.

Las fuentes educativas de [Fidelity sobre EMA](https://www.fidelity.com/learning-center/trading-investing/technical-analysis/technical-indicator-guide/ema),
[Fidelity sobre RSI/MACD](https://www.fidelity.com/learning-center/trading-investing/technical-trading)
y [Schwab sobre volumen](https://www.schwab.com/learn/story/ways-volume-can-help-confirm-price-trends)
explican los enfoques. Estos parámetros de un minuto son hipótesis propias, sin garantía
de rentabilidad ni optimización retrospectiva sobre la sesión actual.

El experimento reutiliza las mismas velas y una sola observación del libro por símbolo
para las cuatro carteras. Aplica comisiones, mínimos, precisión, spread y profundidad al
importe de cada compra. No multiplica las consultas al exchange por cuatro. Cada cartera
es un escenario hipotético independiente y no consume liquidez del libro de las otras.
Si varias señales coinciden y faltan plazas, todas usan el mismo orden de símbolos.

No reproduce operaciones anteriores a su activación ni a una vela ya procesada. Las reglas
se evalúan sobre la historia cerrada disponible, con su versión guardada; cambiar el
catálogo no borra automáticamente el experimento. Un checkpoint atómico en SQLite
(`simulation_state`, clave `comparison-v1`) guarda las cuatro cuentas y sus cursores juntos.
Al reiniciar recupera el capital y evita duplicados, sin tocar órdenes reales ni paper.

La clasificación utiliza el valor de liquidación neto (incluye posiciones abiertas),
con referencia a conservar 50 € en efectivo. Muestra resultado realizado, comisiones
pagadas, cantidad de operaciones, porcentaje positivo y mayor caída observada. Una
valoración desconocida no se sustituye por cero ni recibe puesto. Una muestra corta o
una cartera sin operaciones no acreditan una estrategia ganadora. El panel consulta
`GET /api/experiments/status` y exporta el historial completo con
`GET /api/experiments/export.csv` (`strategy_id` opcional).

## Prueba de costes: frecuencia y compras maker

```powershell
$env:PANEL_PORT = '8001'
uv run invertio analyze --all-markets --simulate --compare-strategies --execution-trial
```

En **Estrategias → Prueba de costes** empieza un experimento independiente con cuatro cuentas
de 50 € ficticios, activadas juntas. **Comparación original** sigue conservando sus carteras,
reglas e historial. No se mezclan clasificaciones de sesiones con distinta fecha de inicio.

| Variante | Señal | Compra | Venta | Espera después de vender |
| --- | --- | --- | --- | --- |
| RSI 1 min · taker | Velas cerradas 1m | Inmediata, 0,09 % | Inmediata, 0,09 % | Nuevo cruce |
| RSI 1 min · maker | La misma señal 1m | Límite pendiente, 0 % si se modela ejecución | Inmediata, 0,09 % | Nuevo cruce |
| RSI 5 min · taker | Velas completas 5m | Inmediata, 0,09 % | Inmediata, 0,09 % | 15 min y nuevo cruce |
| RSI 5 min · maker | La misma señal 5m | Límite pendiente, 0 % si se modela ejecución | Inmediata, 0,09 % | 15 min y nuevo cruce |

Las señales usan RSI 14 recuperando 30 con cierre ascendente; salen por RSI ≥ 55, stop a 2 ATR,
objetivo a 3 ATR o filtros. ATR también usa el periodo de su variante. Caducidad de posiciones:
120 minutos en 1m y 240 en 5m. Cada cuenta asigna hasta 10 € por compra y admite cinco plazas,
incluidas las compras pendientes. El efectivo sin invertir sirve de referencia de 50 €.
La variante 5m combina menor frecuencia con espera tras cerrar; no permite atribuir todos los
cambios únicamente al tamaño de vela. Son hipótesis prospectivas, no reglas optimizadas para
la sesión anterior ni una promesa de rentabilidad.

Las velas 5m se agregan de las mismas velas 1m de Revolut X: cinco minutos consecutivos y
alineados a UTC, sin rellenar huecos ni incluir bloques abiertos. Las entradas solo usan
nuevas velas de señal cerradas desde la activación. Los libros y cotizaciones deben seguir
siendo recientes incluso entre cierres 5m. Todas las cuentas se valoran cada minuto usando
el libro disponible y coste de venta taker, también las variantes maker.

**Modelo maker:** coloca hipotéticamente una compra al mejor bid, redondeada al paso del
mercado, reservando los 10 €. No se ejecuta con la instantánea que la creó. Solo un libro
posterior, dentro de su vigencia, con cantidad suficiente estrictamente por debajo del límite
permite modelar la compra completa al precio límite original. Tocar el precio o no tener
profundidad suficiente deja la orden pendiente; no se simulan ejecuciones parciales. La orden
caduca a los 5 minutos (1m) o 15 (5m). Los cambios de señal o filtros pueden cancelarla; si el
libro posterior ya cumple el cruce, se modela primero esa entrada comprometida y después se
evalúa la salida, sin eliminar compras desfavorables a posteriori.

Este modelo de instantáneas **no conoce la prioridad de la cola ni acredita una ejecución
real** y puede omitir ejecuciones entre consultas. Los huecos de datos se registran como
incompletos; no se inventan fills durante un apagado. Una reserva pendiente no reduce el valor
de la cuenta, pero limita su efectivo libre. Las cancelaciones/caducidades liberan la reserva.
Las posiciones ya adquiridas mantienen los controles de interrupción del simulador original.

El panel separa resultados cerrados antes de comisiones, comisiones y neto; muestra reservas,
pendientes, ejecuciones modeladas, caducidades y cancelaciones. El resultado antes de comisiones
conserva el spread/deslizamiento de los precios simulados; no equivale a una estrategia maker.
Todos los cambios de las cuatro cuentas se guardan juntos bajo `simulation_state`, clave
`execution-comparison-v1`, conservando reglas, fechas, órdenes, cursores y evidencia del libro.
Reiniciar con el mismo comando continúa la prueba y no vuelve a aportar 50 €.

Lecturas: `/api/execution-experiment/status`, `/export.csv` (posiciones y operaciones) y
`/maker-orders.csv` (compras pendientes y finalizadas), estas dos últimas bajo el mismo prefijo.
Los CSV aceptan `strategy_id`. El ensayo no envía órdenes al exchange ni cambia el historial paper.

## Estrategias con velas de 10 minutos y 1 hora

```powershell
$env:PANEL_PORT = '8001'
uv run invertio analyze --all-markets --simulate --compare-strategies --execution-trial --timeframe-trial
```

En **Estrategias → 10 min y 1 hora** corren las cuatro estrategias de la comparación original
(puntuación base, cruce de tendencia, ruptura con volumen y rebote de RSI) con **las mismas
reglas**, sobre velas de 10 minutos y de 1 hora: ocho cuentas independientes de 50 € ficticios
activadas juntas. Las cuentas de 1 minuto no cambian ni reinician su historial.

| Periodo | Velas | ATR (stop y objetivo) | Caducidad |
| --- | --- | --- | --- |
| 10 min | Dos velas de 5 min de Revolut X unidas en bloques UTC completos | 14 velas de 10 min | 240 velas (40 h); rebote de RSI 120 velas (20 h) |
| 1 hora | Velas nativas de 1 h | 14 velas de 1 h | 240 velas (10 días); rebote de RSI 120 velas (5 días) |

- Las entradas y las salidas por señal se deciden al cerrar cada vela; una señal vista más de
  5 minutos después de su cierre no abre posición.
- Stop, objetivo, caducidad y calidad de los datos se revisan **cada minuto** con el libro de
  órdenes, igual que en el resto de carteras.
- Al arrancar se descargan 1000 velas de 5 min y de 1 h por mercado (se guardan en
  `data/bars/revolutx/`), así que las estrategias deciden desde el primer momento. Después, las
  velas largas se construyen con las velas de 1 minuto que el análisis ya descarga (coinciden con
  las nativas); solo se piden de nuevo a Revolut X tras un hueco, con un tope de 15 s por ciclo.

## Estrategias aprobadas en el laboratorio

```powershell
$env:PANEL_PORT = '8001'
uv run invertio analyze --all-markets --simulate --compare-strategies --execution-trial --timeframe-trial --lab-trial
```

En **Estrategias → Del laboratorio** corren, con 50 € ficticios cada una, las cinco
combinaciones que aprobaron el laboratorio del 29/09/2026 (test: el último año, con las
altcoins cayendo un 33 % de mediana). Usan las mismas clases y parámetros que el laboratorio:

| Cuenta | Velas | Filtro de BTC | Test en el laboratorio (mediana por moneda · % en positivo · ops/moneda) |
| --- | --- | --- | --- |
| Puntuación 4 h | 4 h | — | +0,7 % · 60 % · 25 |
| Puntuación 4 h · BTC 50 días | 4 h | media de 50 días | +2,0 % · 62 % · 25 |
| Puntuación 4 h · BTC 200 días | 4 h | media de 200 días | +1,7 % · 73 % · 7 |
| Ruptura 1 h · BTC 50 días | 1 h | media de 50 días | +2,9 % · 60 % · 30 |
| Ruptura 1 h · BTC 200 días | 1 h | media de 200 días | +2,1 % · 73 % · 7 |

- Como en el laboratorio: compra con orden límite pasiva (0 %) que caduca a las dos velas, venta
  inmediata (0,09 %), stop por ATR, salida por la señal de la estrategia, sin objetivo ni
  caducidad. Stop y datos se revisan cada minuto con el libro de órdenes.
- El filtro de BTC usa su cierre diario, formado con sus seis velas de 4 h de cada día UTC: las
  velas diarias de Revolut X no se usan porque dejaron de actualizarse el 29/03/2026.
- Al arrancar se cargan 1500 velas de 4 h y de 1 h por mercado (250 y 62 días); después se forman
  con las velas de 1 minuto del análisis.
- En el laboratorio la ruptura de 1 h sin filtro suspendía (−1,9 %) y con filtro aprueba: el
  filtro evita comprar mientras BTC cae. Se probaron muchas combinaciones, así que alguna puede
  haber aprobado por suerte: esta observación en vivo es la que decide.

La rotación semanal por momento (`invertio lab rotation`) no aprobó: ganó en entrenamiento
(+69 %, por debajo de mantener BTC, +104 %) y perdió un 42 % en el test (BTC: −25 %). Con el
filtro de BTC salía peor. No está en esta pestaña.

## Prueba manual con 50 € en Revolut X

La pestaña **Operar** consulta la cuenta usando `REVOLUTX_API_KEY` y
`REVOLUTX_PRIVATE_KEY_PATH` de `.env`. Las claves permanecen en el backend local. El análisis
de mercados sigue funcionando sin estas claves. Con el comando normal se pueden consultar
saldos y preparar vistas previas; los envíos están desactivados. Para habilitar los botones
de confirmación, inicia explícitamente:

```powershell
$env:PANEL_PORT = '8001'  # si el panel paper ya ocupa el 8000
uv run invertio analyze --manual-orders
```

Cada compra o venta requiere elegir mercado e importe/cantidad, revisar una vista previa de
30 segundos y confirmar. Se envía una **orden límite IOC**: ejecuta inmediatamente la parte
disponible dentro del límite y cancela el resto; puede no ejecutarse o hacerlo parcialmente.
El precio límite permite hasta un 0,1 % respecto al mejor precio visible. Se comprueban spread,
profundidad, frescura, incrementos, mínimos y saldo tanto al preparar como al confirmar.

La prueba permite **50 € acumulados en compras**, incluida una reserva conservadora para la
comisión taker del 0,09 %. Una venta no repone ese límite. Solo permite vender cantidades
compradas por este panel, descontando comisiones cobradas en cripto y comprobando el saldo
disponible. El presupuesto de análisis de 100 € sigue siendo una referencia hipotética separada.

**Stop y objetivo del análisis no son órdenes de protección: la venta es manual.** No se
envían órdenes al arrancar, ante señales, por Telegram ni por vencimiento de una oportunidad.
No se ha validado ninguna operación real por el hecho de superar pruebas simuladas o leer saldos.

El historial real se guarda en `manual_orders`, separado de paper y de oportunidades hipotéticas.
Antes del envío queda persistido un identificador único. Ante doble clic, reinicio o respuesta
perdida no se repite la orden: se consulta su estado. Una orden pendiente o incierta bloquea
nuevas órdenes. Si la API no permite aclararla, hay que comprobarla en Revolut X; nunca se
da por rechazada solo porque no aparezca en una consulta. Cambiar la clave con historial de
órdenes también bloquea nuevos envíos hasta conciliar la identidad; no reinicia los 50 €.

La recogida nocturna necesita el proceso abierto, Internet y el ordenador sin suspensión.
Al regresar, revisa antigüedad, huecos y resultados completos antes de evaluar las señales.

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

## Laboratorio de estrategias

Busca qué reglas funcionan de verdad. Hay dos periodos:
- **Entrenamiento:** hasta 3 años de historia de 1 h de ~55 criptos, remuestreada a 4 h y 1 día.
  Ahí se eligen los parámetros de cada estrategia (los mismos para todas las monedas).
- **Test:** el último año, que la estrategia no ha visto. Ahí se juzga.

Los costes son los reales de Revolut X: comisiones, el spread de cada moneda y su precisión.

```bash
uv run invertio lab download   # historia de las criptos comunes a Revolut X y OKX (~25 min
                               # la primera vez; después solo lo nuevo; --full para repetir)
uv run invertio lab run        # rejillas de config/lab.yaml en todos los núcleos
uv run invertio lab rotation   # rotación semanal por momento (una cartera con las más fuertes)
```

**Filtro de BTC** (`btc_filter` en `config/lab.yaml`): cualquier estrategia puede probarse
comprando solo cuando el último cierre diario de BTC está por encima de su media de N días
(las ventas nunca se bloquean). Cada valor compite por separado, así que el informe muestra
cada estrategia con y sin filtro.

**Rotación semanal:** cada lunes se queda con las `top` monedas de mayor rentabilidad en
`lookback_days` días (solo si es positiva) y vende las que salen del grupo; con el filtro de BTC
apagado, todo a euros. Aprueba si gana en entrenamiento y en test y, en test, supera a mantener
BTC. Solo incluye monedas que hoy cotizan en Revolut X (sesgo de supervivencia a su favor).

Los criterios de aprobado están fijados de antemano en `config/lab.yaml`:
- mediana de retorno neto positiva en el test;
- al menos un 55 % de monedas en positivo;
- 5 o más operaciones por moneda;
- entrenamiento también positivo.

El informe se guarda en `data/lab/<fecha>/` (HTML, JSON y CSV por moneda) y el panel muestra
el último resultado. Estrategias disponibles: `tendencia`, `ruptura`, `puntuacion`,
`ema_cross` y `rsi_reversion`.

## Paper trading en vivo

Precios reales y operaciones simuladas (sin dinero). Qué mercados se vigilan y con qué
estrategia se define en `config/live.yaml`. Con `universe`, la app vigila **todos** los pares
en EUR de Revolut X (sin stablecoins), con la precisión real de cada uno. Los spreads de todos
se piden en una sola llamada por vela.

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
  live/         motor en vivo, feeds, control (pausa/pánico), escáner, informe de paper
  lab/          laboratorio: descarga masiva, rejillas, entrenamiento/test y veredicto
  notify/       avisos (Telegram o consola) y comandos del bot
  api/          API del panel web (FastAPI + WebSocket)
  persistence/  SQLAlchemy + migraciones Alembic
  cli.py        línea de comandos (typer)
config/         app.yaml (venues), risk.yaml (riesgo), live.yaml (modo en vivo), lab.yaml, strategies/*.yaml
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
| 4. Conectores reales | Revolut X manual implementado; operación real pendiente de validación |
| 5. Endurecimiento + despliegue | pendiente |
