import type { Status } from "../api";

const STATE_LABEL = {
  running: { text: "En marcha", tone: "ok" },
  paused: { text: "En pausa", tone: "warn" },
  halted: { text: "Detenido", tone: "bad" },
} as const;

interface Props {
  status: Status | undefined;
  connected: boolean;
  busy: boolean;
  onPause: () => void;
  onResume: () => void;
  onPanic: () => void;
}

export function Header({ status, connected, busy, onPause, onResume, onPanic }: Props) {
  const running = status?.running ?? false;
  const state = status?.state ? STATE_LABEL[status.state] : null;
  return (
    <header className="topbar">
      <div className="brand">
        <span className="brand-mark" aria-hidden="true">
          <svg width="16" height="16" viewBox="0 0 32 32">
            <path
              d="M5 22l7-7 5 5 10-11"
              stroke="white"
              strokeWidth="4"
              fill="none"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
        </span>
        invert.io
      </div>
      <div className="topbar-status">
        {status && <span className="pill brand">{status.mode.toUpperCase()}</span>}
        {state ? (
          <span className={`pill ${state.tone}`}>
            <span className="dot" />
            {state.text}
          </span>
        ) : (
          <span className="pill">Motor parado</span>
        )}
        <span
          className={`pill ${connected ? "ok" : ""}`}
          title={connected ? "Recibiendo eventos en tiempo real" : "Sin conexión en tiempo real"}
        >
          <span className="dot" />
          {connected ? "En vivo" : "Sin conexión"}
        </span>
        {status && <span className="pill">Velas {status.timeframe}</span>}
      </div>
      <div className="topbar-actions">
        {status?.state === "running" ? (
          <button className="btn" onClick={onPause} disabled={!running || busy}>
            Pausar entradas
          </button>
        ) : (
          <button className="btn" onClick={onResume} disabled={!running || busy}>
            Reanudar
          </button>
        )}
        <button className="btn btn-danger" onClick={onPanic} disabled={!running || busy}>
          Pánico
        </button>
      </div>
    </header>
  );
}
