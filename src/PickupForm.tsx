import { useEffect, useState } from "react";
import type { Account, Action, Data } from "./types";

export function PickupForm({
  account,
  busy,
  result,
  start,
}: {
  account: Account;
  busy: boolean;
  result?: Data;
  start: (action: Action, params?: Data) => Promise<void>;
}) {
  const [pickup, setPickup] = useState({ search: "", store_name: "", id_last4: "" });
  const [contact, setContact] = useState<Data>({ emailAddress: account.email });
  const saved = JSON.stringify(result?.config);
  useEffect(() => {
    if (!result?.config) return;
    setPickup({
      search: result.config.pickup?.search || "",
      store_name: result.config.pickup?.store_name || "",
      id_last4: result.config.pickup?.id_last4 || "",
    });
    setContact({ emailAddress: account.email, ...result.config.contact });
  }, [saved, account.email]);
  return (
    <section className="section">
      <div className="section-title">
        <h2>自提门店与联系人</h2>
      </div>
      <p className="muted">
        “立即下单（自提）”自动选择此门店和取货联系人。门店以官网实时可取货状态为准，不可用时停止，不自动换店或改为送货。联系人与送货预设共用，已保存的信息会自动预填。搜索地区可填城市或门店邮编；官网不识别城市名时，请使用 6 位邮编。
      </p>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          start("address_save", { pickup_only: true, pickup, contact });
        }}
      >
        <div className="address-fields">
          <label>
            自提城市 / 邮编
            <input
              required
              disabled={busy}
              value={pickup.search}
              placeholder="例如：上海，或七宝门店邮编 201101"
              onChange={(e) => setPickup({ ...pickup, search: e.target.value })}
            />
          </label>
          <label>
            自提门店
            <input
              required
              disabled={busy}
              value={pickup.store_name}
              placeholder="官网门店名称，例如：七宝"
              onChange={(e) =>
                setPickup({ ...pickup, store_name: e.target.value })
              }
            />
          </label>
          <label>
            取货证件号码后四位
            <input
              type="password"
              aria-label="取货证件号码后四位"
              autoComplete="off"
              disabled={busy}
              maxLength={4}
              pattern="[A-Za-z0-9]{4}"
              value={pickup.id_last4}
              placeholder="仅后四位，不填完整证件号"
              onChange={(e) => setPickup({ ...pickup, id_last4: e.target.value })}
            />
            <span className="muted">官网要求时填写，须与取货时出示的证件一致；仅随自提联系人表单提交给 Apple。</span>
          </label>
          {(
            [
              ["lastName", "取货人姓氏"],
              ["firstName", "取货人名字"],
              ["fullDaytimePhone", "取货手机号"],
              ["emailAddress", "取货通知邮箱"],
            ] as const
          ).map(([key, label]) => (
            <label key={key}>
              {label}
              <input
                required
                disabled={busy}
                type={
                  key === "emailAddress"
                    ? "email"
                    : key === "fullDaytimePhone"
                      ? "tel"
                      : "text"
                }
                value={contact[key] || ""}
                onChange={(e) =>
                  setContact({ ...contact, [key]: e.target.value })
                }
              />
            </label>
          ))}
        </div>
        <button type="submit" className="primary" disabled={busy}>
          保存自提预设
        </button>
      </form>
    </section>
  );
}
