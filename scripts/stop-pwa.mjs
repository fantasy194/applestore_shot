import { parseArgs } from "node:util";
import { localCall, pwaDirectory, serverConnection } from "./pwa-paths.mjs";

const { values } = parseArgs({ options: { "data-dir": { type: "string" } } });
const directory = pwaDirectory(values["data-dir"]);
try {
  const connection = await serverConnection(directory);
  const snapshot = await localCall(connection, "snapshot");
  const pending = snapshot.tasks?.filter((task) => ["running", "waiting", "queued", "scheduled"].includes(task.status)) || [];
  if (pending.length) console.log(`正在停止 ${pending.length} 个本地任务；已提交订单仍须核对官网状态。`);
  await localCall(connection, "shutdown");
  console.log("本地 PWA 正在正常退出，隧道将随启动器关闭。");
} catch (error) { console.error(error.message); process.exitCode = 1; }
