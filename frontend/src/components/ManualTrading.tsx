import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import {
  cancelManualOrder,
  confirmManualOrder,
  getManualMarkets,
  getManualStatus,
  previewManualOrder,
  refreshManualStatus,
  type ManualOrder,
  type ManualStatus,
} from "../manual-api";

const STATUS_LABEL: Record<ManualOrder["status"], string> = {
  preview: "Vista previa", submitting: "Enviando", unknown: "Por comprobar",
  pending: "Pendiente", filled: "Ejecutada", cancelled: "Cancelada",
  rejected: "Rechazada", expired: "Vista previa caducada",
};
const BLOCKING_STATUSES = new Set<ManualOrder["status"]>(["submitting", "unknown", "pending"]);
const time = (value: string) => new Date(value).toLocaleString("es-ES");
const normalize = (value: string) => value.trim().replace(",", ".");
const positiveDecimal = (value: string) => /^(?:\d+(?:\.\d*)?|\.\d+)$/.test(value) && /[1-9]/.test(value);

// Preserve the server's exact decimals when displaying price and quantity.
function decimal(value: string, minimumDecimals = 0): string {
  const match = /^(-?)(\d+)(?:\.(\d*))?$/.exec(value);
  if (!match) return value;
  const integer = match[2].replace(/\B(?=(\d{3})+(?!\d))/g, ".");
  const fraction = (match[3] ?? "").replace(/0+$/, "").padEnd(minimumDecimals, "0");
  return `${match[1]}${integer}${fraction ? `,${fraction}` : ""}`;
}
const euros = (value: string) => `${decimal(value, 2)} €`;

function exceeds(value: string, limit: string): boolean {
  if (!positiveDecimal(value) || !/^\d+(?:\.\d*)?$/.test(limit)) return false;
  const [whole = "0", fractional = ""] = value.split(".");
  const [limitWhole, limitFraction = ""] = limit.split(".");
  const scale = Math.max(fractional.length, limitFraction.length);
  return BigInt(`${whole || "0"}${fractional.padEnd(scale, "0")}`) >
    BigInt(`${limitWhole}${limitFraction.padEnd(scale, "0")}`);
}

function statusTone(status: ManualOrder["status"]) {
  if (status === "filled") return "ok";
  if (status === "unknown" || status === "rejected") return "bad";
  if (BLOCKING_STATUSES.has(status)) return "warn";
  return "";
}

function ManualConfirmation({ order, cancelling, enabled, blocked, busy, onClose, onConfirm }: {
  order: ManualOrder;
  cancelling: boolean;
  enabled: boolean;
  blocked: boolean;
  busy: boolean;
  onClose: () => void;
  onConfirm: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const back = useRef<HTMLButtonElement>(null);
  const [now, setNow] = useState(Date.now);
  const seconds = Math.max(0, Math.ceil((Date.parse(order.expires_at) - now) / 1000));
  const expired = !Number.isFinite(seconds) || seconds <= 0 || order.status !== "preview";
  useEffect(() => {
    const element = dialog.current;
    element?.showModal();
    back.current?.focus();
    const timer = window.setInterval(() => setNow(Date.now()), 250);
    return () => { window.clearInterval(timer); element?.close(); };
  }, []);
  const mayConfirm = enabled && !blocked && !busy && (cancelling ? order.status === "pending" : !expired);
  return <dialog ref={dialog} className="manual-dialog" aria-labelledby="manual-confirm-title"
    onCancel={(event) => { event.preventDefault(); if (!busy) onClose(); }}>
    <h2 id="manual-confirm-title">{cancelling ? "Cancelar orden real" : order.side === "buy" ? "Confirmar compra real" : "Confirmar venta real"}</h2>
    <p className="manual-confirm-market">{order.symbol}</p>
    <p>{cancelling ? "La cancelación se solicitará a Revolut X. La parte ya ejecutada no se revierte." : "Al confirmar enviarás esta orden límite a tu cuenta de Revolut X."}</p>
    <dl className="manual-preview-values">
      <div><dt>Operación</dt><dd>{order.side === "buy" ? "Compra" : "Venta"}</dd></div>
      <div><dt>Cantidad</dt><dd>{decimal(order.quantity)}</dd></div>
      <div><dt>Precio límite</dt><dd>{euros(order.limit_price)}</dd></div>
      <div><dt>Importe de la orden</dt><dd>{euros(order.notional_eur)}</dd></div>
      <div><dt>Reserva para comisión</dt><dd>{euros(order.fee_reserve_eur)}</dd></div>
      <div className="manual-preview-total"><dt>{order.side === "buy" ? "Débito máximo" : "Ingreso neto estimado"}</dt><dd>{euros(order.side === "buy" ? order.max_debit_eur : order.estimated_proceeds_eur)}</dd></div>
    </dl>
    {!cancelling && !busy && <p className={expired ? "manual-warning" : "muted"} role="status">
      {expired ? "Esta vista previa ha caducado. Ciérrala y solicita una nueva." : `Vista previa válida durante ${seconds} s. Los valores de esta confirmación están fijados.`}
    </p>}
    {!enabled && <p className="manual-warning">La confirmación no está disponible. Esta vista previa no se puede enviar.</p>}
    {enabled && blocked && !busy && <p className="manual-warning">{cancelling ? "Esta orden ya no figura como pendiente. Actualiza su estado antes de continuar." : "Hay una orden pendiente o por comprobar. Esta vista previa no se puede enviar."}</p>}
    <p className="manual-protection-note">Stop y objetivo del análisis no son órdenes de protección. La venta es manual.</p>
    {busy && <p className="muted" role="status">Comprobando el resultado de una única solicitud…</p>}
    <div className="dialog-actions">
      <button className="btn" ref={back} onClick={onClose} disabled={busy}>Volver sin enviar</button>
      <button className="btn btn-danger" onClick={onConfirm} disabled={!mayConfirm}>
        {busy ? "Procesando…" : cancelling ? "Confirmar cancelación" : order.side === "buy" ? "Confirmar compra real" : "Confirmar venta real"}
      </button>
    </div>
  </dialog>;
}

export function ManualTradingPanel() {
  const client = useQueryClient();
  const statusQuery = useQuery({ queryKey: ["manual", "status"], queryFn: getManualStatus, refetchInterval: 5_000, retry: false });
  const status = statusQuery.data;
  const available = !!status?.available;
  const configured = !!status?.configured;
  const submissionEnabled = available && configured && !!status?.enabled;
  const markets = useQuery({ queryKey: ["manual", "markets"], queryFn: getManualMarkets, enabled: available && configured, retry: false, staleTime: 60_000 });
  const [side, setSide] = useState<"buy" | "sell">("buy");
  const [symbol, setSymbol] = useState("");
  const [amount, setAmount] = useState("");
  const [quantity, setQuantity] = useState("");
  const [busy, setBusy] = useState<"preview" | "confirm" | "cancel" | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [refreshError, setRefreshError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [preview, setPreview] = useState<ManualOrder | null>(null);
  const [cancellation, setCancellation] = useState<ManualOrder | null>(null);
  const [reconcileRequired, setReconcileRequired] = useState(false);
  const operationLock = useRef(false);
  const refreshLock = useRef(false);

  const refreshAccount = useCallback(async (): Promise<ManualStatus | null> => {
    if (refreshLock.current) return null;
    refreshLock.current = true;
    setRefreshing(true);
    try {
      const updated = await refreshManualStatus();
      client.setQueryData(["manual", "status"], updated);
      setRefreshError(null);
      if (!updated.orders.some((order) => order.status === "submitting" || order.status === "unknown")) setReconcileRequired(false);
      return updated;
    } catch (failure) {
      setRefreshError(failure instanceof Error ? failure.message : "No se pudo actualizar la cuenta.");
      return null;
    } finally {
      refreshLock.current = false;
      setRefreshing(false);
    }
  }, [client]);

  useEffect(() => {
    if (!available || !configured) return;
    void refreshAccount();
    const timer = window.setInterval(() => { if (!operationLock.current) void refreshAccount(); }, 30_000);
    return () => window.clearInterval(timer);
  }, [available, configured, refreshAccount]);

  const positions = status?.positions.filter((position) => positiveDecimal(position.quantity)) ?? [];
  const availableEur = status?.balances.find((balance) => balance.currency === "EUR")?.available ?? "0";
  const symbols = side === "buy" ? markets.data?.symbols ?? [] : positions.map((position) => position.symbol);
  const heldQuantity = positions.find((position) => position.symbol === symbol)?.quantity ?? "0";
  const activeOrders = status?.orders.filter((order) => BLOCKING_STATUSES.has(order.status)) ?? [];
  const blocked = activeOrders.length > 0 || reconcileRequired;
  const input = normalize(side === "buy" ? amount : quantity);
  const tooLarge = side === "buy" ? exceeds(input, status?.remaining_eur ?? "0") : exceeds(input, heldQuantity);
  const formValid = !!symbol && symbols.includes(symbol) && positiveDecimal(input) && !tooLarge;
  const canPreview = available && configured && formValid && !blocked && !busy && !refreshing;

  function changeSide(next: "buy" | "sell") {
    setSide(next);
    setSymbol("");
    setError(null);
    setNotice(null);
  }

  async function makePreview(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canPreview || operationLock.current) return;
    operationLock.current = true;
    setBusy("preview");
    setError(null);
    setNotice(null);
    try {
      const result = await previewManualOrder(side === "buy" ? { symbol, side, budget_eur: input } : { symbol, side, quantity: input });
      setPreview(result);
      void client.invalidateQueries({ queryKey: ["manual", "status"] });
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "No se pudo preparar la vista previa.");
      await refreshAccount();
    } finally {
      operationLock.current = false;
      setBusy(null);
    }
  }

  async function submitOnce(order: ManualOrder, cancelling = false) {
    const expired = !Number.isFinite(Date.parse(order.expires_at)) || Date.now() >= Date.parse(order.expires_at);
    const stillPending = status?.orders.some((item) => item.id === order.id && item.status === "pending");
    if (operationLock.current || !submissionEnabled || (!cancelling && (blocked || expired || order.status !== "preview")) || (cancelling && !stillPending)) return;
    operationLock.current = true;
    setBusy(cancelling ? "cancel" : "confirm");
    setError(null);
    setNotice(null);
    try {
      const result = cancelling ? await cancelManualOrder(order.id) : await confirmManualOrder(order.id);
      setNotice(`${result.symbol}: ${STATUS_LABEL[result.status]}.${result.reason ? ` ${result.reason}` : ""}`);
      if (result.status === "unknown" || result.status === "submitting") setReconcileRequired(true);
    } catch (failure) {
      setReconcileRequired(true);
      setError(`${failure instanceof Error ? failure.message : "No se pudo comprobar la respuesta."} Se actualizará la cuenta para comprobar la orden original. No se ha reenviado.`);
    } finally {
      setPreview(null);
      setCancellation(null);
      await refreshAccount();
      void client.invalidateQueries({ queryKey: ["manual", "status"] });
      operationLock.current = false;
      setBusy(null);
    }
  }

  return <main className="manual-panel">
    <header className="topbar manual-topbar">
      <div className="brand"><span className="brand-mark" aria-hidden="true">↗</span>invert.io</div>
      <div className="topbar-status"><span className="pill warn">DINERO REAL</span><span className="pill">Revolut X · EUR</span><span className={`pill ${submissionEnabled ? "ok" : ""}`}>{submissionEnabled ? "Confirmación habilitada" : "Confirmación desactivada"}</span></div>
      <button className="btn manual-refresh" onClick={() => void refreshAccount()} disabled={!available || !configured || refreshing || !!busy}>{refreshing ? "Actualizando cuenta…" : "Actualizar cuenta"}</button>
    </header>
    <div className="manual-intro"><h1>Operaciones manuales</h1><p className="muted">Revisa cada orden antes de enviarla a tu cuenta. Una señal de análisis no compra ni vende por ti.</p></div>
    <p className="manual-protection-note">Stop y objetivo del análisis no son órdenes de protección. La venta es manual.</p>
    {statusQuery.isPending && <p className="empty" role="status">Consultando el estado de operaciones manuales…</p>}
    {statusQuery.isError && <p className="banner bad" role="alert">No se pudo cargar el estado: {statusQuery.error.message}</p>}
    {status && !available && <p className="banner" role="status">Las operaciones manuales no están disponibles en este proceso.</p>}
    {status && !configured && <div className="banner" role="status"><span>Configura <code>REVOLUTX_API_KEY</code> y <code>REVOLUTX_PRIVATE_KEY_PATH</code> en tu archivo <code>.env</code>. La clave debe permitir consultar la cuenta y operar en Revolut X. Reinicia el proceso después de configurarla.</span></div>}
    {status && !status.enabled && <div className="banner" role="status"><span>Las órdenes reales están desactivadas. Arranca análisis con <code>--manual-orders</code> para habilitar la confirmación. Puedes preparar vistas previas si la cuenta está configurada.</span></div>}
    {status?.error && <p className="banner bad" role="alert">{status.error}</p>}
    {refreshError && <p className="banner bad" role="alert">Actualización de cuenta: {refreshError}</p>}
    {error && <p className="banner bad" role="alert">{error}</p>}
    {notice && <p className="manual-notice" role="status">{notice}</p>}

    {status && <div className="kpis manual-kpis">
      <section className="card"><div className="kpi-label">Presupuesto acumulado de compra</div><div className="kpi-value">{euros(status.budget_eur)}</div><div className="kpi-sub">Incluye la reserva para comisiones</div></section>
      <section className="card"><div className="kpi-label">Comprometido</div><div className="kpi-value">{euros(status.committed_eur)}</div><div className="kpi-sub">Importe contabilizado por el control de presupuesto</div></section>
      <section className="card"><div className="kpi-label">Disponible para nuevas compras</div><div className="kpi-value">{euros(status.remaining_eur)}</div><div className="kpi-sub">Las ventas no reponen este presupuesto</div></section>
    </div>}
    <p className="manual-help">El límite de compra es acumulado y conservador, incluidas las comisiones. Vender no lo reinicia. Solo puedes vender las cantidades adquiridas desde esta aplicación.</p>
    {blocked && <p className="manual-warning" role="status">{reconcileRequired ? "Hay una solicitud cuyo resultado debe comprobarse. Actualiza la cuenta y revisa el historial antes de preparar otra orden." : "Hay una orden pendiente o por comprobar. Resuélvela antes de preparar otra orden."}</p>}

    <div className="grid grid-half section">
      <section className="card" aria-labelledby="manual-form-title">
        <div className="card-head"><h2 id="manual-form-title">Preparar una orden límite</h2><div className="tabs" role="group" aria-label="Tipo de operación"><button className={`tab ${side === "buy" ? "active" : ""}`} aria-pressed={side === "buy"} disabled={!!busy} onClick={() => changeSide("buy")}>Comprar</button><button className={`tab ${side === "sell" ? "active" : ""}`} aria-pressed={side === "sell"} disabled={!!busy} onClick={() => changeSide("sell")}>Vender</button></div></div>
        <form className="manual-form" onSubmit={(event) => void makePreview(event)}>
          <label htmlFor="manual-symbol">Mercado en EUR</label>
          <select id="manual-symbol" className="select" value={symbol} onChange={(event) => { setSymbol(event.target.value); setQuantity(""); }} disabled={!configured || !!busy} required>
            <option value="">Elige un mercado</option>{symbols.map((item) => <option key={item} value={item}>{item}</option>)}
          </select>
          {markets.isError && side === "buy" && <p className="down">No se pudieron cargar los mercados: {markets.error.message}</p>}
          {side === "buy" ? <><label htmlFor="manual-amount">Importe máximo de compra, con comisiones (€)</label><input id="manual-amount" type="text" inputMode="decimal" autoComplete="off" value={amount} onChange={(event) => setAmount(event.target.value)} placeholder="Ej. 10,00" disabled={!!busy} required /><p className="manual-help">La vista previa ajustará la cantidad a los mínimos y la precisión del mercado.</p></> : <><label htmlFor="manual-quantity">Cantidad de cripto que quieres vender</label><div className="manual-quantity-input"><input id="manual-quantity" type="text" inputMode="decimal" autoComplete="off" value={quantity} onChange={(event) => setQuantity(event.target.value)} placeholder="Cantidad" disabled={!!busy} required /><button className="btn" type="button" onClick={() => setQuantity(heldQuantity)} disabled={!symbol || !!busy || !positiveDecimal(heldQuantity)}>Usar disponible</button></div><p className="manual-help">Adquirido desde esta app: {decimal(heldQuantity)} {symbol.split("/")[0] || "unidades"}.</p>{!positions.length && <p className="muted">No hay posiciones adquiridas desde esta aplicación para vender.</p>}</>}
          {!!input && !positiveDecimal(input) && <p className="down">Introduce un número decimal mayor que cero.</p>}
          {tooLarge && <p className="down">{side === "buy" ? "El importe supera el presupuesto restante." : "La cantidad supera lo adquirido desde esta aplicación."}</p>}
          <button className="btn btn-primary" type="submit" disabled={!canPreview}>{busy === "preview" ? "Preparando vista previa…" : "Revisar vista previa"}</button>
          <p className="manual-help">Este paso no envía una orden. La confirmación muestra cantidad, precio límite y {side === "buy" ? "débito máximo" : "ingreso neto estimado"} antes del envío.</p>
        </form>
      </section>
      <section className="card" aria-labelledby="manual-account-title"><div className="card-head"><h2 id="manual-account-title">Saldos y posiciones</h2><span className="pill" title={status?.balances_updated_at ? `Consultado: ${time(status.balances_updated_at)}` : undefined}>{status?.balances_updated_at ? `EUR disponible: ${euros(availableEur)}` : "Saldo EUR sin consultar"}</span><span className="card-note">La cuenta se consulta cada 30 s</span></div>
        {!status?.balances.length ? <p className="empty">No hay saldos consultados todavía.</p> : <div className="table-wrap"><table><thead><tr><th>Activo</th><th className="r">Disponible</th><th className="r">Reservado</th><th className="r">Total</th></tr></thead><tbody>{status.balances.map((balance) => <tr key={balance.currency}><td>{balance.currency}</td><td className="r num">{decimal(balance.available)}</td><td className="r num">{decimal(balance.reserved)}</td><td className="r num">{decimal(balance.total)}</td></tr>)}</tbody></table></div>}
        <h3 className="manual-subtitle">Cantidades adquiridas desde esta app</h3>
        {!positions.length ? <p className="muted">Sin posiciones registradas.</p> : <ul className="manual-positions">{positions.map((position) => <li key={position.symbol}><strong>{position.symbol}</strong><span className="num">{decimal(position.quantity)}</span></li>)}</ul>}
      </section>
    </div>

    <section className="card section" aria-labelledby="manual-orders-title"><div className="card-head"><h2 id="manual-orders-title">Órdenes manuales y vistas previas</h2><span className="card-note">Las órdenes por comprobar bloquean nuevos envíos</span></div>
      {!status?.orders.length ? <p className="empty">Aún no has preparado ninguna orden manual.</p> : <div className="table-wrap"><table className="manual-orders"><thead><tr><th>Fecha</th><th>Mercado</th><th>Lado</th><th>Estado</th><th>Cantidad / ejecutada</th><th>Precio límite</th><th>Débito máx. / ingreso est.</th><th>Acción</th></tr></thead><tbody>{status.orders.map((order) => <tr key={order.id}><td>{time(order.created_at)}</td><td>{order.symbol}<span className="cell-sub manual-order-id" title={order.exchange_id ?? order.id}>{order.exchange_id ?? order.id}</span></td><td>{order.side === "buy" ? "Compra" : "Venta"}</td><td><span className={`pill ${statusTone(order.status)}`}>{STATUS_LABEL[order.status]}</span>{order.exchange_status && <span className="cell-sub">{order.exchange_status}</span>}{order.reason && <span className="cell-sub manual-order-reason">{order.reason}</span>}</td><td className="num">{decimal(order.quantity)}<span className="cell-sub">Ejecutada: {decimal(order.filled_quantity)}</span></td><td className="num">{euros(order.limit_price)}</td><td className="num">{euros(order.side === "buy" ? order.max_debit_eur : order.estimated_proceeds_eur)}</td><td>{order.status === "pending" ? <button className="btn" onClick={() => setCancellation(order)} disabled={!status.enabled || !!busy || refreshing}>Cancelar orden</button> : "—"}</td></tr>)}</tbody></table></div>}
    </section>
    {(preview || cancellation) && <ManualConfirmation key={(preview ?? cancellation)!.id} order={(preview ?? cancellation)!} cancelling={!!cancellation} enabled={submissionEnabled} blocked={cancellation ? !status?.orders.some((item) => item.id === cancellation.id && item.status === "pending") : blocked} busy={busy === "confirm" || busy === "cancel"} onClose={() => { setPreview(null); setCancellation(null); }} onConfirm={() => void submitOnce((preview ?? cancellation)!, !!cancellation)} />}
  </main>;
}
