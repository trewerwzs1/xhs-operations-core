(function (root, factory) {
  const api = factory();
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.TonyredbookTabSelection = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  function isXhsUrl(value) {
    if (!value) return false;
    try {
      const host = new URL(value).hostname.toLowerCase();
      return host === "xiaohongshu.com" || host.endsWith(".xiaohongshu.com");
    } catch {
      return false;
    }
  }

  function isHealthyContext(context) {
    return Boolean(
      context && context.origin && context.pathname !== "/404" &&
      Array.isArray(context.riskSignals) && context.riskSignals.length === 0
    );
  }

  function sortTabsByRecency(tabs) {
    return [...tabs].sort(
      (left, right) => Number(right.lastAccessed || 0) - Number(left.lastAccessed || 0)
    );
  }

  function chooseHealthyTab(tabs, contextsByTabId, activeTabId = null) {
    const candidates = tabs.filter((tab) => isXhsUrl(tab?.url));
    if (activeTabId !== null) {
      const active = candidates.find((tab) => tab.id === activeTabId);
      if (active && isHealthyContext(contextsByTabId.get(active.id))) return active;
    }
    return sortTabsByRecency(candidates).find(
      (tab) => isHealthyContext(contextsByTabId.get(tab.id))
    ) || null;
  }

  return { isXhsUrl, isHealthyContext, sortTabsByRecency, chooseHealthyTab };
});
