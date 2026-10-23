import type { Data } from "./types";

// Read either the bounded Web attention summary or the same native task result.
// These flags describe orders that need attention, never a successful payment.
export function purchaseEvidence(
  result: Data | null,
  resolved: (number: unknown) => boolean = () => false,
  depth = 0,
  budget = { remaining: 100 },
): { unknown: boolean; pending: boolean } {
  if (!result || typeof result !== "object")
    return { unknown: false, pending: false };
  if (depth > 3 || budget.remaining-- <= 0)
    return { unknown: true, pending: false };
  const rows = (value: unknown): Data[] => (Array.isArray(value) ? value : []);
  const attention = result.purchase_attention;
  const nestedResult = result.result;
  const pendingOrders = rows(result.pending_orders);
  const children = [
    ...rows(result.queue_jobs),
    ...rows(result.attempts),
    ...rows(result.jobs),
    ...(nestedResult ? [nestedResult] : []),
  ];
  const hasKnownOrder =
    !!result.order_number ||
    !!nestedResult?.order_number ||
    pendingOrders.length > 0 ||
    rows(nestedResult?.pending_orders).length > 0;
  const hasChildren = [result.queue_jobs, result.attempts, result.jobs].some(
    (value) => Array.isArray(value) && value.length > 0,
  );
  const nested = children
    .slice(0, 100)
    .map((child) => purchaseEvidence(child, resolved, depth + 1, budget));
  return {
    unknown:
      (["submission_unknown", "payment_unknown"].includes(result.status) &&
        !resolved(result.order_number)) ||
      (result.order_submitted === true && !hasKnownOrder && !hasChildren) ||
      attention?.unknown_unidentified === true ||
      rows(attention?.unknown_order_numbers).some(
        (number) => !resolved(number),
      ) ||
      children.length > 100 ||
      nested.some((child) => child.unknown),
    pending:
      (result.status === "pending_payment" && !resolved(result.order_number)) ||
      pendingOrders.some((row) => !resolved(row.order_number)) ||
      attention?.pending_unidentified === true ||
      rows(attention?.pending_order_numbers).some(
        (number) => !resolved(number),
      ) ||
      nested.some((child) => child.pending),
  };
}
