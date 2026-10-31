// 自动排队排完后在右上角弹个提示；排队任务超过 200 个时说明：队列面板最多只显示 200 个，其余的照常执行。
import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

app.registerExtension({
  name: "Goudan.AutoQueue.Notice",
  setup() {
    api.addEventListener("goudan-autoqueue.queued", (event) => {
      const d = event.detail || {};
      let detail = `已自动排队 ${d.queued} 个，队列里现在共 ${d.waiting} 个等待中。`;
      if (d.failed) {
        detail += ` 有 ${d.failed} 个没排进去，详情看控制台。`;
      }
      if (d.waiting > d.ui_limit) {
        detail += ` ComfyUI 的队列面板最多只显示 ${d.ui_limit} 个，其余的都在队列里，不会丢，会照常按顺序执行。`;
      }
      const toast = app.extensionManager && app.extensionManager.toast;
      if (toast && toast.add) {
        toast.add({ severity: d.failed ? "warn" : "info", summary: d.tag || "自动排队", detail: detail, life: 10000 });
      } else {
        console.log(`[${d.tag}] ${detail}`);
      }
    });
  },
});
