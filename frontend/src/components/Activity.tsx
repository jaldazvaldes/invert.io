import type { Activity } from "../api";
import { EXIT_REASONS, price, qty, when } from "../format";

const ORDER_STATUS: Record<string, string> = {
  submitted: "pendiente",
  partially_filled: "parcial",
  filled: "ejecutada",
  canceled: "cancelada",
  rejected: "rechazada",
};

function Row({ item }: { item: Activity }) {
  const symbol = item.market.split(":")[1];
  if (item.kind === "fill") {
    const buy = item.side === "buy";
    return (
      <tr>
        <td className="num">{when(item.ts)}</td>
        <td>
          <strong>{symbol}</strong>
        </td>
        <td>
          <span className={`pill ${buy ? "ok" : "bad"}`}>
            {buy ? "Compra ejecutada" : `Venta · ${EXIT_REASONS[item.reason] ?? item.reason}`}
          </span>
        </td>
        <td className="num">
          {qty(item.quantity)} × {price(item.price)}
          <div className="cell-sub">
            comisión {price(item.fee)} {item.currency}
          </div>
        </td>
        <td className="muted">—</td>
      </tr>
    );
  }
  const buy = item.action === "buy";
  const decision =
    item.approved === null ? (
      <span className="muted">pendiente</span>
    ) : item.approved ? (
      <span className="up">✓ aprobada</span>
    ) : (
      <span className="down">✗ {item.decision}</span>
    );
  return (
    <tr>
      <td className="num">{when(item.ts)}</td>
      <td>
        <strong>{symbol}</strong>
      </td>
      <td>
        <span className="pill">{buy ? "Señal de compra" : "Señal de cierre"}</span>
        <div className="cell-sub" style={{ marginTop: 4 }}>
          {item.strategy}: {item.reason}
        </div>
      </td>
      <td className="num">
        {price(item.price)}
        {buy && (
          <div className="cell-sub">
            stop <span className="down">{price(item.stop_loss)}</span> · objetivo{" "}
            <span className="up">{item.take_profit ? price(item.take_profit) : "por señal"}</span>
          </div>
        )}
      </td>
      <td>
        {decision}
        {item.order_status && (
          <div className="cell-sub">
            orden {item.order_type === "limit" ? "límite" : "a mercado"}:{" "}
            {ORDER_STATUS[item.order_status] ?? item.order_status}
            {item.fill_price !== null && ` a ${price(item.fill_price)}`}
          </div>
        )}
      </td>
    </tr>
  );
}

export function ActivityCard({ items }: { items: Activity[] | undefined }) {
  return (
    <section className="card" aria-labelledby="activity-title">
      <div className="card-head">
        <h2 id="activity-title">Actividad</h2>
        <span className="card-note">cada señal, lo que decidió el riesgo y la ejecución</span>
      </div>
      {!items || items.length === 0 ? (
        <p className="empty">Todavía no hay señales. Aparecen aquí en cuanto la estrategia se active.</p>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Hora</th>
                <th>Mercado</th>
                <th>Evento</th>
                <th>Precio</th>
                <th>Riesgo · orden</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item, index) => (
                <Row key={`${item.kind}-${item.ts}-${index}`} item={item} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
