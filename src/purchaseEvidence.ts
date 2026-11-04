import type { Data } from "./types";

// Read either the bounded Web attention summary or the same native task result.
// These flags describe orders that need attention, never a successful payment.
export function purchaseEvidence(
  result: Data | null,
  resolved: (number: unknown) => boolean = () => false,
  depth = 0,
  budget = { remaining: 100 },
): { unknown: boolean; pending: boolean; known: boolean; unverified: boolean } {
  if (!result || typeof result !== "object")
    return { unknown: false, pending: false, known: false, unverified: false };
  if (depth > 3 || budget.remaining-- <= 0)
    return { unknown: true, pending: false, known: false, unverified: false };
  const rows = (value: unknown): Data[] => (Array.isArray(value) ? value : []);
  const hasNumber = (value: unknown) =>
    typeof value === "string" && value.trim().length > 0;
  const validVerificationNumber = (value: unknown) =>
    typeof value === "string" && /^W\d{6,20}$/.test(value);
  const boundVerificationNumber = (
    value: Data | undefined,
    level = 0,
  ): boolean =>
    !!value &&
    level <= 3 &&
    (validVerificationNumber(value.order_number) ||
      boundVerificationNumber(value.result, level + 1));
  const attention = result.purchase_attention;
  const nestedResult = result.result;
  const pendingOrders = rows(result.pending_orders);
  const children = [
    ...pendingOrders,
    ...rows(result.queue_jobs),
    ...rows(result.attempts),
    ...rows(result.jobs),
    ...(nestedResult ? [nestedResult] : []),
  ];
  const hasKnownOrder =
    hasNumber(result.order_number) ||
    hasNumber(nestedResult?.order_number) ||
    pendingOrders.some((order) => hasNumber(order.order_number)) ||
    rows(nestedResult?.pending_orders).some((order) =>
      hasNumber(order.order_number),
    );
  const nested = children
    .slice(0, 100)
    .map((child) => purchaseEvidence(child, resolved, depth + 1, budget));
  const verificationKnown =
    result.status === "verification_required" &&
    boundVerificationNumber(result);
  const summarizedKnown = rows(attention?.known_order_numbers).some(
    validVerificationNumber,
  );
  const known =
    hasKnownOrder || summarizedKnown || nested.some((child) => child.known);
  return {
    unknown:
      (["submission_unknown", "unknown_submission", "payment_unknown"].includes(
        result.status,
      ) &&
        !resolved(result.order_number)) ||
      (result.status === "verification_required" && !verificationKnown) ||
      ((result.order_submitted === true || result.submitted === true) &&
        !known) ||
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
    known,
    unverified: verificationKnown || nested.some((child) => child.unverified),
  };
}
