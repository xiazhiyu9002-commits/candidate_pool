import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./App";
import { createRuntimeApi } from "./api/client";


// 启动失败的兜底：把错误显示在页面上，避免白屏时无法定位问题。
function showBootError(message: string) {
  const el = document.getElementById("root");
  if (el) {
    el.innerHTML = `<pre style="color:#c00;padding:16px;font:14px/1.6 monospace">${message}</pre>`;
  }
}
window.addEventListener("unhandledrejection", (event) => {
  showBootError(`启动失败(unhandledrejection): ${String(event.reason)}`);
});
window.addEventListener("error", (event) => {
  showBootError(`启动失败(error): ${event.message}`);
});


function BootLoading() {
  return (
    <div style={{ display: "flex", alignItems: "center", justifyContent: "center", height: "100vh", color: "#6b7280", font: "14px/1.5 sans-serif" }}>
      正在启动服务…
    </div>
  );
}

async function start() {
  const root = createRoot(document.getElementById("root")!);
  // 先显示加载态，避免 sidecar 后台就绪期间白屏。
  root.render(<BootLoading />);
  const api = await createRuntimeApi();
  root.render(
    <StrictMode><App api={api} /></StrictMode>
  );
}

void start();
