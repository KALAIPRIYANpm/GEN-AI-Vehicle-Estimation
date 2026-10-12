/* DentScan front-end behaviour. No framework; each block activates only when its markup exists. */
(function () {
  "use strict";
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const inr = (n) => "₹" + Math.round(n).toLocaleString("en-IN");
  const SVGNS = "http://www.w3.org/2000/svg";

  /* ---------- theme ---------- */
  $$("[data-theme-toggle]").forEach((btn) => btn.addEventListener("click", () => {
    const root = document.documentElement;
    const cur = root.dataset.theme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    const next = cur === "dark" ? "light" : "dark";
    root.dataset.theme = next;
    try { localStorage.setItem("theme", next); } catch (e) { /* storage unavailable */ }
  }));

  /* ---------- password show/hide ---------- */
  $$("[data-pw-toggle]").forEach((btn) => btn.addEventListener("click", () => {
    const input = document.getElementById(btn.dataset.pwToggle);
    const show = input.type === "password";
    input.type = show ? "text" : "password";
    btn.textContent = show ? "Hide" : "Show";
  }));

  /* ---------- confirm dialogs ---------- */
  $$("form[data-confirm]").forEach((f) => f.addEventListener("submit", (e) => {
    if (!confirm(f.dataset.confirm)) e.preventDefault();
  }));
  $$("button[data-confirm]").forEach((b) => b.addEventListener("click", (e) => {
    if (!confirm(b.dataset.confirm)) e.preventDefault();
  }));

  /* ---------- clickable table rows ---------- */
  $$("tr[data-href]").forEach((tr) => tr.addEventListener("click", (e) => {
    if (e.target.closest("a, button, input, select, label")) return;
    location.href = tr.dataset.href;
  }));

  /* ---------- password confirmation ---------- */
  $$("[data-match]").forEach((el) => {
    const other = document.getElementById(el.dataset.match);
    const check = () => el.setCustomValidity(el.value && el.value !== other.value ? "The passwords don't match." : "");
    el.addEventListener("input", check); other.addEventListener("input", check);
  });
  $$("form[data-validate]").forEach((f) => f.addEventListener("submit", (e) => {
    if (!f.checkValidity()) { e.preventDefault(); f.reportValidity(); }
  }));

  /* ---------- make / model picker ---------- */
  $$("[data-vehicle-picker]").forEach((wrap) => {
    const cat = JSON.parse(wrap.dataset.catalogue || "{}");
    const brand = $("select[name=carBrand]", wrap);
    const model = $("select[name=carModel]", wrap);
    const otherBrand = $("[data-other-brand]", wrap);
    const otherModel = $("[data-other-model]", wrap);
    const hint = $("[data-price-hint]", wrap);
    const current = model.dataset.current || "";
    function fill() {
      const b = brand.value;
      const models = cat[b] || [];
      model.innerHTML = "";
      const isOther = b === "__other";
      otherBrand.style.display = isOther ? "" : "none";
      otherBrand.required = isOther;
      if (isOther || !b) {
        model.style.display = isOther ? "none" : "";
        model.required = !isOther;
        model.add(new Option(b ? "" : "Choose a make first", ""));
        otherModel.style.display = isOther ? "" : "none";
        otherModel.required = isOther;
        hint.textContent = isOther ? "Standard prices are used for makes that aren't listed." : "";
        return;
      }
      model.style.display = ""; model.required = true;
      model.add(new Option("Choose a model", ""));
      models.forEach((m) => model.add(new Option(m, m, false, m.toLowerCase() === current.toLowerCase())));
      model.add(new Option("Another model", "__other"));
      if (current && !models.some((m) => m.toLowerCase() === current.toLowerCase())) model.value = "__other";
      toggleModel();
    }
    function toggleModel() {
      const isOther = model.value === "__other";
      otherModel.style.display = isOther ? "" : "none";
      otherModel.required = isOther;
      hint.textContent = isOther ? "Standard prices are used for models that aren't listed." : "";
    }
    brand.addEventListener("change", fill);
    model.addEventListener("change", toggleModel);
    fill();
  });

  /* ---------- scan upload ---------- */
  const scanForm = $("[data-scan-form]");
  if (scanForm) {
    const input = $("[data-file-input]", scanForm);
    const drop = $("[data-drop]", scanForm);
    const previews = $("[data-previews]", scanForm);
    const submit = $("[data-scan-submit]", scanForm);
    const note = $("[data-scan-note]", scanForm);
    const MAX = 6;
    let files = [];
    const ok = (f) => /^image\/(jpeg|png|webp|bmp)$/.test(f.type) || /\.(jpe?g|png|webp|bmp)$/i.test(f.name);

    function sync() {
      const dt = new DataTransfer();
      files.forEach((f) => dt.items.add(f));
      input.files = dt.files;
      previews.innerHTML = "";
      files.forEach((f, i) => {
        const fig = document.createElement("figure");
        const img = document.createElement("img");
        img.alt = ""; img.src = URL.createObjectURL(f);
        img.onload = () => URL.revokeObjectURL(img.src);
        const cap = document.createElement("figcaption"); cap.textContent = f.name;
        const rm = document.createElement("button");
        rm.type = "button"; rm.className = "rm"; rm.textContent = "×"; rm.setAttribute("aria-label", "Remove " + f.name);
        rm.onclick = () => { files.splice(i, 1); sync(); };
        fig.append(img, cap, rm); previews.append(fig);
      });
      submit.disabled = files.length === 0;
      note.textContent = files.length
        ? `${files.length} photo${files.length > 1 ? "s" : ""} ready. ${files.length < MAX ? "You can add " + (MAX - files.length) + " more." : "That's the maximum."}`
        : "Add at least one photo to continue.";
    }
    function add(list) {
      const incoming = Array.from(list).filter(ok);
      const skipped = list.length - incoming.length;
      files = files.concat(incoming).slice(0, MAX);
      sync();
      if (skipped) note.textContent += ` ${skipped} file${skipped > 1 ? "s were" : " was"} skipped (not an image).`;
    }
    input.addEventListener("change", () => { const l = Array.from(input.files); add(l); });
    ["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("is-over"); }));
    ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("is-over"); }));
    drop.addEventListener("drop", (e) => {
      // the native input also receives the drop in some browsers; handle it once here
      e.stopPropagation(); add(e.dataTransfer.files);
    });
    // Clicking the input replaces its FileList; keep our accumulated list instead.
    input.addEventListener("click", () => { input.value = ""; });

    const conf = $("[data-conf]", scanForm), out = $("[data-conf-out]", scanForm);
    if (conf) conf.addEventListener("input", () => { out.textContent = Math.round(conf.value * 100) + "%"; });

    scanForm.addEventListener("submit", () => {
      scanForm.classList.add("scanning");
      submit.disabled = true;
      submit.textContent = "Analysing photos…";
      note.textContent = "The first scan after starting the app can take a little longer while the model loads.";
    });
  }

  /* ---------- landing: part explorer ---------- */
  const pills = $("[data-part-pills]");
  if (pills) {
    const map = $("[data-part-map]");
    const setPart = (part) => {
      $$("button", pills).forEach((b) => b.setAttribute("aria-pressed", b.dataset.part === part ? "true" : "false"));
      $$(".panel[data-part]", map).forEach((p) => p.classList.toggle("is-hot", p.dataset.part === part));
    };
    $$("button", pills).forEach((b) => b.addEventListener("click", () => setPart(b.dataset.part)));
    $$(".panel[data-part]", map).forEach((p) => p.addEventListener("click", () => setPart(p.dataset.part)));
    setPart("Bumper");
  }

  /* ---------- highlighting a part across viewer, map and table ---------- */
  function hotPart(part) {
    $$("[data-claim-map] .panel[data-part]").forEach((p) => p.classList.toggle("is-hot", !!part && p.dataset.part === part));
    $$("tr[data-row]").forEach((r) => r.classList.toggle("is-hot", !!part && r.dataset.part === part));
    $$("[data-viewer-marks] g[data-part]").forEach((g) => g.classList.toggle("is-hot", !!part && g.dataset.part === part));
  }
  $$("tr[data-row]").forEach((r) => {
    r.addEventListener("mouseenter", () => hotPart(r.dataset.part));
    r.addEventListener("mouseleave", () => hotPart(null));
  });
  $$("[data-claim-map] .panel[data-part]").forEach((p) => {
    p.addEventListener("mouseenter", () => hotPart(p.dataset.part));
    p.addEventListener("mouseleave", () => hotPart(null));
  });

  /* ---------- photo viewer ---------- */
  const viewer = $("[data-viewer]");
  if (viewer) {
    const data = JSON.parse($("[data-viewer-data]").textContent || "[]");
    const positions = JSON.parse($("[data-viewer-urls]").textContent || "[]");
    const img = $("[data-viewer-img]", viewer);
    const svg = $("[data-viewer-marks]", viewer);
    const caption = $("[data-viewer-caption]", viewer);
    const dl = $("[data-viewer-download]", viewer);
    const toggle = $("[data-mark-toggle]", viewer);
    const base = img.src.replace(/\/file\/(orig|det)\/\d+$/, "/file/");
    let cur = 0;

    function draw() {
      const dets = data[cur] || [];
      svg.innerHTML = "";
      const W = img.naturalWidth || 1000, H = img.naturalHeight || 750;
      // Scale small photos up to the viewer width, capped at 560px tall; the overlay follows the image box.
      const shown = Math.min(viewer.clientWidth, 560 * W / H);
      img.parentElement.style.width = shown + "px";
      svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
      const fs = 13 * (W / shown); // ~13 screen pixels whatever the photo resolution
      dets.forEach((d, i) => {
        const [x1, y1, x2, y2] = [d.box[0] * W, d.box[1] * H, d.box[2] * W, d.box[3] * H];
        const g = document.createElementNS(SVGNS, "g");
        g.dataset.part = d.part;
        const r = document.createElementNS(SVGNS, "rect");
        Object.entries({ x: x1, y: y1, width: x2 - x1, height: y2 - y1, class: "mk" }).forEach(([k, v]) => r.setAttribute(k, v));
        const t = document.createElementNS(SVGNS, "title");
        t.textContent = `${d.part}: ${Math.round(d.confidence * 100)}% confidence`;
        r.append(t);
        const L = Math.max(10, Math.min(x2 - x1, y2 - y1) * 0.22);
        const corners = document.createElementNS(SVGNS, "path");
        corners.setAttribute("class", "mk-corner");
        corners.setAttribute("d",
          `M${x1} ${y1 + L}V${y1}H${x1 + L} M${x2 - L} ${y1}H${x2}V${y1 + L} M${x2} ${y2 - L}V${y2}H${x2 - L} M${x1 + L} ${y2}H${x1}V${y2 - L}`);
        const label = `${i + 1}  ${d.part}  ${Math.round(d.confidence * 100)}%`;
        const tagW = label.length * fs * 0.52 + fs * 0.8, tagH = fs * 1.5;
        const ty = y1 - tagH - 4 > 0 ? y1 - tagH - 4 : Math.min(H - tagH, y2 + 4);
        const lx = Math.max(0, Math.min(x1, W - tagW));
        const tag = document.createElementNS(SVGNS, "rect");
        Object.entries({ x: lx, y: ty, width: tagW, height: tagH, rx: 3, class: "mk-tag" }).forEach(([k, v]) => tag.setAttribute(k, v));
        const txt = document.createElementNS(SVGNS, "text");
        Object.entries({ x: lx + fs * 0.4, y: ty + tagH * 0.72, class: "mk-label" }).forEach(([k, v]) => txt.setAttribute(k, v));
        txt.style.fontSize = fs + "px";
        txt.textContent = label;
        g.append(r, corners, tag, txt);
        g.addEventListener("mouseenter", () => hotPart(d.part));
        g.addEventListener("mouseleave", () => hotPart(null));
        svg.append(g);
      });
    }
    function show(i) {
      cur = i;
      const pos = positions[i];
      const hasDets = (data[i] || []).length > 0;
      img.src = base + (hasDets ? "orig/" : "det/") + pos;
      dl.href = base + "det/" + pos;
      caption.textContent = `Photo ${i + 1} of ${positions.length}` + (hasDets ? ` · ${data[i].length} mark${data[i].length > 1 ? "s" : ""}` : "");
      if (toggle) toggle.hidden = !hasDets;
      $$("[data-thumb]").forEach((b) => b.setAttribute("aria-current", +b.dataset.thumb === i ? "true" : "false"));
      if (!hasDets) svg.innerHTML = "";
    }
    img.addEventListener("load", draw);
    window.addEventListener("resize", draw);
    if (img.complete) draw();
    $$("[data-thumb]").forEach((b) => b.addEventListener("click", () => show(+b.dataset.thumb)));
    if (toggle) $$("button", toggle).forEach((b) => b.addEventListener("click", () => {
      viewer.dataset.marks = b.dataset.v;
      $$("button", toggle).forEach((x) => x.setAttribute("aria-pressed", x === b ? "true" : "false"));
    }));
    caption.textContent = `Photo 1 of ${positions.length}` + ((data[0] || []).length ? ` · ${data[0].length} mark${data[0].length > 1 ? "s" : ""}` : "");
  }

  /* ---------- live estimate ---------- */
  const est = $("[data-estimate-form]");
  if (est && $("[data-action]", est)) {
    const n = (k) => parseFloat(est.dataset[k]) || 0;
    const totalsBox = $("[data-totals]");
    function recalc() {
      let parts = 0, labour = 0;
      $$("tr[data-row]", est).forEach((tr) => {
        const base = parseFloat(tr.dataset.base) || 0;
        const action = $("[data-action]", tr).value;
        const qty = Math.max(1, parseInt($("[data-qty]", tr).value, 10) || 1);
        const inc = $("[data-inc]", tr).checked;
        const unit = action === "replace" ? base : Math.round(base * n("repair") / 100);
        const lab = Math.round(base * n("labour") / 100);
        $("[data-c=unit]", tr).textContent = inr(unit);
        $("[data-c=labour]", tr).textContent = inr(lab);
        $("[data-c=total]", tr).innerHTML = "<strong>" + inr((unit + lab) * qty) + "</strong>";
        tr.classList.toggle("is-off", !inc);
        if (inc) { parts += unit * qty; labour += lab * qty; }
      });
      const gst = Math.round((parts + labour) * n("gst") / 100);
      const total = parts + labour + gst;
      const ins = total ? Math.max(0, Math.round(total * n("cov") / 100) - n("ded")) : 0;
      const set = (k, v) => { const el = $(`[data-t=${k}]`, totalsBox); if (el) el.textContent = inr(v); };
      set("parts", parts); set("labour", labour); set("gst", gst); set("total", total); set("ins", ins); set("you", total - ins);
      est.classList.add("is-dirty");
    }
    est.addEventListener("input", recalc);
    est.addEventListener("change", recalc);
    window.addEventListener("beforeunload", (e) => {
      if (est.classList.contains("is-dirty") && !est.dataset.submitting) { e.preventDefault(); e.returnValue = ""; }
    });
    est.addEventListener("submit", () => { est.dataset.submitting = "1"; });
  }
})();
