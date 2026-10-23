import { useState } from "react";
import type { Account } from "./types";

type PurchaseDraft = { sku: string; quantity: number };
const keyFor = (accountId: string) =>
  `duo.purchase-draft.v1:${encodeURIComponent(accountId)}`;
const validSku = (value: unknown): value is string =>
  typeof value === "string" && /^[A-Z0-9]{5,16}(?:\/A)?$/i.test(value);
const validQuantity = (value: unknown): value is number =>
  typeof value === "number" &&
  Number.isInteger(value) &&
  value >= 1 &&
  value <= 999;

function presetDraft(account?: Account): PurchaseDraft {
  return {
    sku: validSku(account?.preset_sku) ? account.preset_sku : "MK2M4CH",
    quantity: validQuantity(account?.preset_quantity)
      ? account.preset_quantity
      : 1,
  };
}

function loadDraft(accountId: string): PurchaseDraft | null {
  try {
    const raw = localStorage.getItem(keyFor(accountId));
    if (!raw || raw.length > 256) return null;
    const saved: unknown = JSON.parse(raw);
    if (
      typeof saved === "object" &&
      saved !== null &&
      Object.keys(saved).sort().join(",") === "quantity,sku,version" &&
      "version" in saved &&
      saved.version === 1 &&
      "sku" in saved &&
      validSku(saved.sku) &&
      "quantity" in saved &&
      validQuantity(saved.quantity)
    )
      return { sku: saved.sku, quantity: saved.quantity };
  } catch {
    // Browser storage is optional: private mode or corrupt data cannot block buying.
  }
  return null;
}

export function usePurchaseDraft(account?: Account) {
  const accountId = account?.id || "";
  const [drafts, setDrafts] = useState<Record<string, PurchaseDraft | null>>(
    {},
  );
  const initialized = Object.hasOwn(drafts, accountId);
  let saved = initialized ? drafts[accountId] : null;
  if (accountId && !initialized) {
    // Read storage once per account. Null means no explicit browser choice yet.
    saved = loadDraft(accountId);
    setDrafts({ ...drafts, [accountId]: saved });
  }
  // An untouched fallback follows saved account presets, including late loads.
  // Cached or edited choices take priority over every later snapshot update.
  const draft = saved ?? presetDraft(account);
  const update = (next: PurchaseDraft) => {
    setDrafts((current) => ({ ...current, [accountId]: next }));
    if (!accountId || !validSku(next.sku) || !validQuantity(next.quantity))
      return;
    try {
      localStorage.setItem(
        keyFor(accountId),
        JSON.stringify({ version: 1, ...next }),
      );
    } catch {
      // Keep the in-memory choice when storage is unavailable or full.
    }
  };
  return {
    ...draft,
    setSku: (sku: string) => update({ ...draft, sku }),
    setQuantity: (quantity: number) => update({ ...draft, quantity }),
  };
}
