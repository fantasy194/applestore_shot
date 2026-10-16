import { useEffect, useId, useRef, useState } from "react";
import { MapPin } from "lucide-react";
import { LOCAL_ID, type Account, type Action, type Data } from "./types";

const addressFields = [
  ["contact", "lastName", "姓氏", "例如：张", "family-name"],
  ["contact", "firstName", "名字", "例如：三", "given-name"],
  ["contact", "emailAddress", "通知邮箱", "例如：name@example.com", "email"],
  ["contact", "fullDaytimePhone", "手机号", "收件人的手机号码", "tel-national"],
  ["delivery", "state", "省 / 直辖市", "例如：四川", "address-level1"],
  ["delivery", "city", "城市", "例如：成都", "address-level2"],
  ["delivery", "district", "区 / 县", "例如：锦江区", "address-level3"],
] as const;
export function AddressForm({
  account,
  busy,
  result,
  region,
  start,
}: {
  account: Account;
  busy: boolean;
  result?: Data;
  region?: Data;
  start: (action: Action, params?: Data) => Promise<void>;
}) {
  const streetId = useId();
  const [form, setForm] = useState<Data>({
    contact: { emailAddress: account.email },
    delivery: {},
    address: {},
  });
  const addressRead = useRef("");
  useEffect(() => {
    if (busy || addressRead.current === account.id) return;
    addressRead.current = account.id;
    start("address_read");
  }, [account.id, busy]);
  const savedConfig = JSON.stringify(result?.config);
  useEffect(() => {
    if (result?.config)
      setForm({
        contact: { emailAddress: account.email, ...result.config.contact },
        delivery: result.config.delivery || {},
        address: result.config.address || {},
      });
  }, [savedConfig, account.email]);
  const savedRegion = JSON.stringify(region);
  useEffect(() => {
    if (region && Object.keys(region).length)
      setForm((f) => ({ ...f, delivery: { ...f.delivery, ...region } }));
  }, [savedRegion]);
  return (
    <section className="section">
      <div className="section-title">
        <h2>快捷送货地址</h2>
        {account.id !== LOCAL_ID && !account.pending && (
          <button disabled={busy} onClick={() => start("region")}>
            <MapPin size={15} />
            读取官网地区
          </button>
        )}
      </div>
      <p className="muted">
        {account.id === LOCAL_ID
          ? "新账号将继承此地址，已有账号不会被覆盖。"
          : "保存为此账号的本地预设。"}
        完整保存后，下单自动使用此地址、送货方式和官网默认配送服务，支付方式为支付宝。已启用自动锁单的商品会在核对型号、数量、地址和金额上限后直接创建未付款订单并显示二维码。
      </p>
      <p className="address-guidance">
        {result?.config?.address?.street
          ? "已预填保存的地址，可直接修改。修改后点击“保存地址预设”。"
          : "填写收件人和送货地点；输入框中的示例仅作提示，所有项目均需填写。"}
      </p>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          start("address_save", form);
        }}
      >
        <div className="address-fields">
          {addressFields.map(
            ([group, key, label, placeholder, autoComplete]) => (
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
                  value={form[group]?.[key] || ""}
                  placeholder={placeholder}
                  autoComplete={`shipping ${autoComplete}`}
                  onChange={(e) =>
                    setForm({
                      ...form,
                      [group]: { ...form[group], [key]: e.target.value },
                    })
                  }
                />
              </label>
            ),
          )}
          <div className="wide">
            <label htmlFor={streetId}>详细地址</label>
            <p id={`${streetId}-hint`} className="address-guidance">
              从街道开始填写，写清小区、楼栋、单元和门牌号。省市区填在上方即可。
            </p>
            <textarea
              id={streetId}
              aria-describedby={`${streetId}-hint`}
              required
              disabled={busy}
              rows={2}
              autoComplete="shipping street-address"
              placeholder="例如：中山路 88 号，阳光花园 3 栋 2 单元 601 室"
              value={form.address?.street || ""}
              onChange={(e) =>
                setForm({
                  ...form,
                  address: { ...form.address, street: e.target.value },
                })
              }
            />
          </div>
        </div>
        <button className="primary" disabled={busy} type="submit">
          保存地址预设
        </button>
      </form>
    </section>
  );
}
