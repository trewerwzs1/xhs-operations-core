const bridge = document.getElementById("bridge");
const hint = document.getElementById("hint");

chrome.runtime.sendMessage({ type: "GET_STATUS" }, (response) => {
  const connected = !chrome.runtime.lastError && response?.success && response.status?.wsConnected;
  bridge.textContent = connected ? "已连接" : "未连接";
  bridge.className = connected ? "ok" : "err";
  if (!connected) {
    hint.textContent = "请先启动包内 bridge server。扩展不会采集 Cookie、请求头或网络日志。";
  }
});

chrome.runtime.onMessage.addListener((message) => {
  if (message.type !== "STATUS_CHANGED") return;
  const connected = Boolean(message.status?.wsConnected);
  bridge.textContent = connected ? "已连接" : "未连接";
  bridge.className = connected ? "ok" : "err";
});
