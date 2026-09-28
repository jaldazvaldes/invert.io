// Formato de números y fechas en español (1.234,56 · 28/09 15:10).

const SYMBOLS: Record<string, string> = { EUR: "€", USD: "$" };

const cache = new Map<string, Intl.NumberFormat>();

function formatter(min: number, max: number): Intl.NumberFormat {
  const key = `${min}-${max}`;
  let f = cache.get(key);
  if (!f) {
    f = new Intl.NumberFormat("es-ES", {
      minimumFractionDigits: min,
      maximumFractionDigits: max,
      useGrouping: "always",
    });
    cache.set(key, f);
  }
  return f;
}

export function num(value: number, decimals = 2): string {
  return formatter(decimals, decimals).format(value);
}

/** Precio con decimales según su magnitud (BTC 73.379,01 · XLM 0,1994 · JASMY 0,00449470). */
export function price(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  const magnitude = Math.abs(value);
  const decimals = magnitude >= 1 ? 2 : magnitude >= 0.01 ? 4 : 8;
  return num(value, decimals);
}

export function money(value: number, currency: string, signed = false): string {
  const sign = signed && value > 0 ? "+" : value < 0 ? "−" : "";
  return `${sign}${num(Math.abs(value))} ${SYMBOLS[currency] ?? currency}`;
}

export function pct(value: number, signed = true): string {
  const sign = signed && value > 0 ? "+" : value < 0 ? "−" : "";
  return `${sign}${num(Math.abs(value))} %`;
}

export function qty(value: number): string {
  return formatter(0, 8).format(value);
}

export function points(value: number): string {
  return formatter(0, 1).format(value);
}

const dateTime = new Intl.DateTimeFormat("es-ES", {
  day: "2-digit",
  month: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
});
const timeOnly = new Intl.DateTimeFormat("es-ES", { hour: "2-digit", minute: "2-digit" });

export function when(iso: string | null | undefined): string {
  return iso ? dateTime.format(new Date(iso)) : "—";
}

export function clock(date: Date): string {
  return timeOnly.format(date);
}

export function ago(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return "—";
  const seconds = Math.max(0, Math.round((now - new Date(iso).getTime()) / 1000));
  if (seconds < 60) return `hace ${seconds} s`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `hace ${minutes} min`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `hace ${hours} h`;
  return `hace ${Math.round(hours / 24)} días`;
}

export function tone(value: number): "up" | "down" | "flat" {
  return value > 0 ? "up" : value < 0 ? "down" : "flat";
}

export const FACTOR_LABELS: Record<string, string> = {
  tendencia: "Tendencia",
  momentum: "Momentum",
  macd: "MACD",
  rsi: "RSI",
  volumen: "Volumen",
  ruptura: "Ruptura",
};

export const EXIT_REASONS: Record<string, string> = {
  stop_loss: "Stop-loss",
  take_profit: "Objetivo",
  "señal de salida": "Señal",
  pánico: "Pánico",
  entrada: "Entrada",
};
