/* Progressive enhancement for the correction form.
 *
 * Without JavaScript the form still works (server-side precheck / apply with
 * flash messages).  With JavaScript we call the JSON API and render the plan
 * without a round-trip through HTML.
 */
(() => {
  const form = document.querySelector("form[data-correct-form]");
  if (!form || !window.fetch) return;

  const panel = document.getElementById("plan-panel");
  const out = document.getElementById("plan-output");
  const textarea = form.querySelector("textarea[name=batch]");
  const actionUrl = form.dataset.checkUrl;
  const applyUrl = form.dataset.applyUrl;
  const versionInput = form.querySelector("input[name=version]");

  const parseBatch = (text) =>
    text
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter(Boolean)
      .map((line) => {
        const parts = line.split(/[\s,;，；]+/).filter(Boolean);
        if (parts.length !== 5) throw new Error(`行格式应为“图幅号 x0 y0 x1 y1”：${line}`);
        const nums = parts.slice(1).map((v) => {
          if (!/^-?\d+$/.test(v)) throw new Error(`坐标必须是整数米：${line}`);
          return Number(v);
        });
        return { no: parts[0], x0: nums[0], y0: nums[1], x1: nums[2], y1: nums[3] };
      });

  const stats = (s) => `缺片 ${s.missing_count} 区 / ${s.missing_area} m²；重叠 ${s.overlap_count} 区 / ${s.overlap_area} m²`;

  function render(payload, applied) {
    const p = payload.plan;
    panel.hidden = false;
    const statusText = applied
      ? "已写入"
      : p.accepted
        ? "预检通过（尚未写入）"
        : p.status === "stale"
          ? "数据已过期（未写入）"
          : "已退回（未写入）";
    const cls = p.accepted ? "ok" : "error";
    out.innerHTML = "";
    const box = document.createElement("div");
    box.className = `plan ${cls}`;
    const rows = [
      ["结论", statusText],
      ["说明", (payload.message ? payload.message + "：" : "") + p.reasons.join("；")],
      ["更正前", stats(p.before)],
      ["更正后（预计）", stats(p.after)],
      ["消除旧问题", `缺片 ${p.eliminated.missing_area} m²；重叠 ${p.eliminated.overlap_area} m²`],
      ["新位置问题", `缺片 ${p.new_problems.missing_area} m²；重叠 ${p.new_problems.overlap_area} m²`],
      ["重算范围", p.window.map((r) => `[${r.join(" ")}]`).join(" ") || "无"],
      ["本批图幅", p.batch.map((b) => `${b.no} [${b.rect.join(" ")}]`).join("；")],
    ];
    for (const [k, v] of rows) {
      const row = document.createElement("div");
      row.className = "plan-row";
      const kk = document.createElement("span");
      kk.className = "k";
      kk.textContent = k;
      const vv = document.createElement("span");
      vv.className = "v";
      vv.textContent = v;
      row.append(kk, vv);
      box.append(row);
    }
    out.append(box);
    if (applied && payload.state_url) {
      setTimeout(() => window.location.assign(payload.state_url), 900);
    }
  }

  async function submit(kind) {
    let batch;
    try {
      batch = parseBatch(textarea.value);
    } catch (err) {
      panel.hidden = false;
      out.innerHTML = `<div class="plan error"><div class="plan-row"><span class="k">输入错误</span><span class="v">${err.message}</span></div></div>`;
      return;
    }
    const url = kind === "apply" ? applyUrl : actionUrl;
    const resp = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ batch, version: Number(versionInput.value) }),
    });
    const payload = await resp.json().catch(() => ({ error: "服务器返回了非 JSON 响应" }));
    if (!resp.ok && !payload.plan) {
      panel.hidden = false;
      out.innerHTML = `<div class="plan error"><div class="plan-row"><span class="k">未处理</span><span class="v">${payload.error || resp.statusText}</span></div></div>`;
      return;
    }
    render(payload, kind === "apply" && payload.applied === true);
  }

  form.addEventListener("submit", (event) => {
    const submitter = event.submitter;
    if (!submitter) return;
    event.preventDefault();
    const kind = submitter.value === "apply" ? "apply" : "check";
    if (kind === "apply" && !window.confirm("确认提交本批更正？")) return;
    submit(kind).catch((err) => {
      panel.hidden = false;
      out.textContent = `请求失败：${err.message}`;
    });
  });
})();
