// User-provided presets. Prices are reference values, never checkout quotes.
export const duoProducts = [
  { sku: "MK2M4CH", name: "iPhone Duo 256GB Star White", price: 15999 },
  { sku: "MK2P4CH", name: "iPhone Duo 512GB Star White", price: 17999 },
];
export const presetProducts = [
  ...duoProducts,
  { sku: "MJT84CH", name: "iPhone 18 Pro 256GB 银色", price: 9999 },
  { sku: "MLH74FE", name: "抛光布", price: 69 },
];
export const priceText = (value: unknown) =>
  value != null && value !== "" && Number.isFinite(Number(value))
    ? `${Number(value).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })} CNY`
    : "未知";
