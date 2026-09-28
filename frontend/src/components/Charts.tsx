import { useQuery } from "@tanstack/react-query";
import {
  AreaSeries,
  CandlestickSeries,
  ColorType,
  createChart,
  createSeriesMarkers,
  LineStyle,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";
import { useEffect, useRef } from "react";
import { get, type BarsResponse, type EquityPoint } from "../api";
import { EXIT_REASONS, price } from "../format";

function cssVar(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

const axisTime = new Intl.DateTimeFormat("es-ES", { hour: "2-digit", minute: "2-digit" });
const axisDay = new Intl.DateTimeFormat("es-ES", { day: "2-digit", month: "short" });
const crosshairTime = new Intl.DateTimeFormat("es-ES", {
  weekday: "short",
  day: "2-digit",
  month: "short",
  hour: "2-digit",
  minute: "2-digit",
});

function toDate(time: Time): Date {
  return new Date((time as number) * 1000);
}

/** Gráfico con los colores del tema y las horas en la zona horaria local. */
function baseChart(element: HTMLElement): IChartApi {
  return createChart(element, {
    autoSize: true,
    layout: {
      background: { type: ColorType.Solid, color: "transparent" },
      textColor: cssVar("--muted"),
      fontFamily: "system-ui, -apple-system, Segoe UI, sans-serif",
      attributionLogo: true, // atribución a TradingView que pide la licencia de la librería
    },
    grid: {
      vertLines: { color: cssVar("--border") },
      horzLines: { color: cssVar("--border") },
    },
    rightPriceScale: { borderColor: cssVar("--border") },
    // La rueda del ratón y el arrastre vertical desplazan la página, no el gráfico:
    // el gráfico se mueve arrastrando en horizontal y se amplía pellizcando o con los ejes.
    handleScroll: { mouseWheel: false, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: false },
    handleScale: { mouseWheel: false, pinch: true, axisPressedMouseMove: true },
    timeScale: {
      borderColor: cssVar("--border"),
      timeVisible: true,
      secondsVisible: false,
      tickMarkFormatter: (time: Time, type: number) =>
        type <= 2 ? axisDay.format(toDate(time)) : axisTime.format(toDate(time)),
    },
    localization: {
      locale: "es-ES",
      timeFormatter: (time: Time) => crosshairTime.format(toDate(time)),
      priceFormatter: (value: number) => price(value),
    },
  });
}

export function PriceChart({ market }: { market: string }) {
  const container = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const series = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const markers = useRef<ISeriesMarkersPluginApi<Time> | null>(null);
  const lines = useRef<IPriceLine[]>([]);
  const fitted = useRef<string | null>(null);

  const { data, isLoading } = useQuery({
    queryKey: ["bars", market],
    queryFn: () => get<BarsResponse>(`/api/bars?market=${encodeURIComponent(market)}&limit=300`),
    refetchInterval: 30_000,
  });

  useEffect(() => {
    if (!container.current) return;
    const c = baseChart(container.current);
    series.current = c.addSeries(CandlestickSeries, {
      upColor: cssVar("--up"),
      downColor: cssVar("--down"),
      borderVisible: false,
      wickUpColor: cssVar("--up"),
      wickDownColor: cssVar("--down"),
    });
    markers.current = createSeriesMarkers(series.current, []);
    chart.current = c;
    return () => {
      c.remove();
      chart.current = null;
      series.current = null;
      markers.current = null;
      lines.current = [];
    };
  }, []);

  useEffect(() => {
    const s = series.current;
    if (!s || !data) return;
    s.setData(data.bars.map((b) => ({ ...b, time: b.time as UTCTimestamp })));

    const tf = data.tf_seconds;
    const items: SeriesMarker<Time>[] = data.fills.map((f) => {
      const t = Math.floor(new Date(f.ts).getTime() / 1000 / tf) * tf;
      const buy = f.side === "buy";
      const label = buy ? "Compra" : (EXIT_REASONS[f.reason] ?? "Venta");
      return {
        time: t as UTCTimestamp,
        position: buy ? "belowBar" : "aboveBar",
        shape: buy ? "arrowUp" : "arrowDown",
        color: buy ? cssVar("--up") : cssVar("--down"),
        text: `${label} ${price(f.price)}`,
      };
    });
    markers.current?.setMarkers(items.sort((a, b) => (a.time as number) - (b.time as number)));

    for (const line of lines.current) s.removePriceLine(line);
    lines.current = [];
    const p = data.position;
    if (p) {
      const add = (value: number | null, color: string, title: string) => {
        if (value !== null) {
          lines.current.push(
            s.createPriceLine({
              price: value,
              color,
              lineWidth: 1,
              lineStyle: LineStyle.Dashed,
              axisLabelVisible: true,
              title,
            }),
          );
        }
      };
      add(p.avg_price, cssVar("--info"), "Entrada");
      add(p.stop_loss, cssVar("--down"), "Stop");
      add(p.take_profit, cssVar("--up"), "Objetivo");
    }
    if (fitted.current !== market && data.bars.length) {
      chart.current?.timeScale().fitContent();
      fitted.current = market;
    }
  }, [data, market]);

  const empty = !isLoading && data && data.bars.length === 0;
  return (
    <div>
      <div className="chart" ref={container} style={{ display: empty ? "none" : undefined }} />
      {empty && (
        <p className="empty">
          Aún no hay velas guardadas de {market}. Aparecen cuando el motor está en marcha.
        </p>
      )}
      {!empty && (
        <div className="legend">
          <span className="up">▲ compra</span>
          <span className="down">▼ venta</span>
          <span style={{ color: "var(--info)" }}>
            <i />
            entrada
          </span>
          <span className="down">
            <i />
            stop
          </span>
          <span className="up">
            <i />
            objetivo
          </span>
          {data && <span>velas de {data.timeframe}</span>}
        </div>
      )}
    </div>
  );
}

export function EquityChart({ venue }: { venue: string }) {
  const container = useRef<HTMLDivElement>(null);
  const series = useRef<ISeriesApi<"Area"> | null>(null);
  const chart = useRef<IChartApi | null>(null);

  const { data } = useQuery({
    queryKey: ["equity", venue],
    queryFn: () => get<EquityPoint[]>(`/api/equity?venue=${encodeURIComponent(venue)}`),
    refetchInterval: 60_000,
  });

  useEffect(() => {
    if (!container.current) return;
    const c = baseChart(container.current);
    const accent = cssVar("--accent");
    series.current = c.addSeries(AreaSeries, {
      lineColor: accent,
      topColor: cssVar("--accent-soft"),
      bottomColor: "transparent",
      lineWidth: 2,
    });
    chart.current = c;
    return () => {
      c.remove();
      series.current = null;
      chart.current = null;
    };
  }, []);

  useEffect(() => {
    if (!series.current || !data) return;
    const seen = new Set<number>();
    const points = [];
    for (const p of data) {
      const t = Math.floor(new Date(p.ts).getTime() / 1000);
      if (!seen.has(t)) {
        seen.add(t);
        points.push({ time: t as UTCTimestamp, value: p.equity });
      }
    }
    series.current.setData(points);
    chart.current?.timeScale().fitContent();
  }, [data]);

  const empty = data && data.length < 2;
  return (
    <>
      <div className="chart-small" ref={container} style={{ display: empty ? "none" : undefined }} />
      {empty && <p className="empty">Todavía no hay historial de capital.</p>}
    </>
  );
}
