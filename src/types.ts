export interface Account {
  id: string;
  name: string;
  email: string;
  pending?: boolean;
  preset_sku?: string;
  preset_quantity?: number;
}
export type Action =
  | "enroll"
  | "login"
  | "session_maintain"
  | "me"
  | "bag"
  | "clear_bag"
  | "add"
  | "purchase"
  | "checkout"
  | "orders"
  | "cancel_order"
  | "catalog"
  | "inventory_settings_read"
  | "inventory_settings_save"
  | "store_search"
  | "inventory_check"
  | "inventory_monitor"
  | "address_read"
  | "address_save"
  | "region"
  | "import_auth"
  | "forget_password"
  | "qr";
export type Data = Record<string, any>;
export interface Prompt {
  id: string;
  kind:
    | "password"
    | "code"
    | "text"
    | "select"
    | "confirm"
    | "prepared_purchase";
  label: string;
  default?: any;
  options?: { value: any; label: string }[];
  actions?: { value: string; label: string }[];
  details?: Data;
  prepared?: {
    v: number;
    sku: string;
    quantity: number;
    fulfillment: "RETAIL";
    config_fingerprint: string;
    config_file_sha256: string;
    authorized_store_ids: string[];
    prepared_at_ms: number;
    expires_at_ms: number;
    preparation_stage: "review" | "fulfillment";
    review_verified: boolean;
    order_ready: boolean;
    checkout_details_prepared?: {
      status: "waiting_stock" | "details_accepted" | "unchanged";
      stage:
        | "Fulfillment"
        | "PickupContact"
        | "Shipping"
        | "Billing"
        | "Review"
        | "unknown";
      local_contact_ready: boolean;
      local_address_ready: boolean;
      contact_accepted: boolean;
      address_accepted: boolean;
      address_required?: boolean;
    };
  };
  account?: string;
  credential_storage?: {
    backend: "encrypted_file" | "windows" | "macos" | null;
    available: boolean;
    persistent: boolean;
    error?: string | null;
  };
}
export interface Task {
  /** Web summaries omit logs and large results; explicit task reads return full detail. */
  is_summary?: boolean;
  detail_revision?: string;
  log_count?: number;
  logs_included?: boolean;
  id: string;
  account_id: string;
  action: Action;
  /** Explicit login preheat may yield to a purchase; ordinary login may not. */
  warm_only?: boolean;
  prepared_handoff?: Data;
  status: string;
  stage: string;
  created_at: number;
  updated_at: number;
  prompt: Prompt | null;
  result: Data | null;
  order_submitted: boolean;
  logs: string[];
  queue_job_ids?: string[];
  execution_started_at_ms?: number;
  purchase_progress?: { current: string | null; completed: string[] };
  schedule?: {
    at_ms: number;
    sku: string;
    quantity: number;
    fulfillment: "HOME" | "RETAIL";
    concurrency?: number;
    retry_count?: number;
    triggered_at_ms?: number;
  };
}
export interface Snapshot {
  accounts: Account[];
  tasks: Task[];
  data_dir: string;
  defaults?: Account;
}
export const LOCAL_ID = "00000000-0000-0000-0000-000000000000";
export const localDefaults: Account = {
  id: LOCAL_ID,
  name: "通用预设",
  email: "",
  preset_sku: "MK2M4CH",
  preset_quantity: 1,
};
export const active = (task: Task) =>
  ["scheduled", "queued", "running", "waiting"].includes(task.status);
export const actionNames: Record<Action, string> = {
  enroll: "添加并登录账号",
  login: "登录账号",
  session_maintain: "会话维护",
  me: "检查登录",
  bag: "查询购物袋",
  clear_bag: "清空购物袋",
  add: "设置购买数量",
  purchase: "购买商品",
  checkout: "结算购物袋",
  orders: "查询订单",
  cancel_order: "取消订单",
  catalog: "查询商品",
  inventory_settings_read: "读取库存监控配置",
  inventory_settings_save: "保存库存监控配置",
  store_search: "查询自提门店",
  inventory_check: "检查门店库存",
  inventory_monitor: "自动监控库存",
  address_read: "读取地址预设",
  address_save: "保存地址预设",
  region: "读取当前地区",
  import_auth: "导入会话",
  forget_password: "忘记密码",
  qr: "获取付款二维码",
};
export const stateNames: Record<string, string> = {
  scheduled: "等待定时",
  queued: "排队中",
  running: "执行中",
  waiting: "需要操作",
  succeeded: "已完成",
  failed: "失败",
  cancelled: "已取消",
  interrupted: "已中断",
};
