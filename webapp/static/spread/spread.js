// Spread map: cumulative earliest eligible record per US county, by year.
// Data is embedded in the page as inert JSON (#spread-data); geometry is a shared static
// TopoJSON. All data-derived text goes through textContent / d3 .text(), never innerHTML.
// The server already drops territories, future dates and unpublished/unlinked sources.
(function () {
  "use strict";
  const D = JSON.parse(document.getElementById("spread-data").textContent);
  const TOPO_URL = document.currentScript.dataset.topo;
  const $ = id => document.getElementById(id);

  const SOURCES = [
    { key: "inat", label: "iNaturalist", url: id => "https://www.inaturalist.org/observations/" + encodeURIComponent(id) },
    { key: "eddmaps", label: "EDDMapS", url: id => "https://www.eddmaps.org/distribution/point.cfm?id=" + encodeURIComponent(id) },
    { key: "gbif", label: "GBIF specimens", url: id => "https://www.gbif.org/occurrence/" + encodeURIComponent(id) },
  ];
  const BY_KEY = Object.fromEntries(SOURCES.map(s => [s.key, s]));
  const STATUS_TEXT = {
    ok: s => "updated " + s.last_success,
    stale: s => "latest refresh did not finish; showing data from " + s.last_success,
    pending: () => "not built yet",
    unavailable: () => "not available yet",
    not_linked: () => "species not found in this source",
  };
  const published = SOURCES.filter(s => ["ok", "stale"].includes((D.sources[s.key] || {}).status));
  const checked = new Set(published.map(s => s.key));

  function el(tag, text) { const e = document.createElement(tag); if (text != null) e.textContent = text; return e; }
  function recordLink(sourceKey, id, label) {
    const a = el("a", label); a.href = BY_KEY[sourceKey].url(id); a.target = "_blank"; a.rel = "noopener";
    return a;
  }

  // ---- source toggles -----------------------------------------------------------
  SOURCES.forEach(s => {
    const st = D.sources[s.key] || { status: "not_linked" };
    const lab = el("label"), box = el("input");
    box.type = "checkbox";
    box.checked = checked.has(s.key);
    box.disabled = !published.includes(s);
    box.addEventListener("change", () => { box.checked ? checked.add(s.key) : checked.delete(s.key); recompute(); });
    lab.append(box, el("span", s.label), el("small", (STATUS_TEXT[st.status] || STATUS_TEXT.unavailable)(st)));
    $("sources").append(lab);
  });

  // ---- merged data for the current selection --------------------------------------
  const year = d => +d.slice(0, 4);
  let merged = new Map(); // fips -> {date, y, by: [source keys tied on the earliest date]}
  function mergeSelection() {
    merged = new Map();
    for (const [fips, recs] of Object.entries(D.counties)) {
      let best = null, by = [];
      for (const s of SOURCES) {
        if (!checked.has(s.key) || !recs[s.key]) continue;
        const d = recs[s.key][0];
        if (best === null || d < best) { best = d; by = [s.key]; }
        else if (d === best) by.push(s.key);
      }
      if (best !== null) merged.set(fips, { date: best, y: year(best), by });
    }
  }
  // The year range covers every published record (so toggling sources never moves the
  // slider) and ends at the server's current year.
  const allYears = Object.values(D.counties).flatMap(r => Object.values(r).map(v => year(v[0])));
  const Y1 = D.year;
  const Y0 = allYears.length ? Math.min(...allYears) : null;
  const slider = $("year"), btn = $("play"), notice = $("notice");
  if (Y0 !== null) {
    slider.min = Y0; slider.max = Y1; slider.value = Y1;
    $("r0").textContent = Y0; $("r1").textContent = Y1;
  }
  const lo = Y0 ?? Y1, hi = Math.max(Y1, lo + 1);
  const color = d3.scaleSequential(d3.interpolateRgb("#0d366b", "#9cc3f2")).domain([lo, hi]);
  $("rampbar").style.background = "linear-gradient(90deg," + d3.range(0, 1.01, 0.1).map(t => color(lo + t * (hi - lo))).join(",") + ")";

  // ---- map ------------------------------------------------------------------------
  const W = 975, H = 610;
  const svg = d3.select("#map").attr("viewBox", `0 0 ${W} ${H}`);
  let countyPaths = null, selected = null;
  const names = new Map();

  fetch(TOPO_URL).then(r => { if (!r.ok) throw new Error(r.status); return r.json(); }).then(topo => {
    const counties = topojson.feature(topo, topo.objects.counties);
    counties.features.forEach(f => names.set(f.id, f.properties));
    const proj = d3.geoAlbersUsa().fitExtent([[10, 10], [W - 10, H - 10]], counties);
    const path = d3.geoPath(proj);
    countyPaths = svg.append("g").selectAll("path").data(counties.features).join("path")
      .attr("class", "county").attr("d", path);
    svg.append("path").attr("class", "states")
      .attr("d", path(topojson.mesh(topo, topo.objects.counties, (a, b) => a.id.slice(0, 2) !== b.id.slice(0, 2))));
    const tip = $("tip"), box = $("mapbox");
    countyPaths
      .on("pointermove", (ev, f) => {
        const m = merged.get(f.id), y = +slider.value;
        tip.textContent = `${f.properties.name}, ${f.properties.st}` + (!cumul.length ? "" :
          ": " + (m && m.y <= y ? "first record " + m.date : "no record by " + y));
        tip.hidden = false;
        const r = box.getBoundingClientRect();
        tip.style.left = Math.max(4, Math.min(ev.clientX - r.left + 12, r.width - tip.offsetWidth - 4)) + "px";
        tip.style.top = Math.max(4, ev.clientY - r.top - tip.offsetHeight - 10) + "px";
      })
      .on("pointerleave", () => { tip.hidden = true; })
      .on("click", (ev, f) => { selected = f.id; showDetail(); paint(); });
    renderTables(); // county names are now available
    show();
  }).catch(() => {
    $("mapbox").textContent = "Map unavailable. The tables below still list every county.";
  });

  function paint() {
    if (!countyPaths) return;
    const y = +slider.value;
    countyPaths
      .style("fill", f => { const m = merged.get(f.id); return m && m.y <= y ? color(m.y) : null; })
      .classed("new", f => { const m = merged.get(f.id); return !!m && m.y === y; })
      .classed("sel", f => f.id === selected);
    countyPaths.filter(".new, .sel").raise(); // keep outlined counties on top
  }

  // ---- detail panel (persistent, with links) ----------------------------------------
  function countyLabel(fips) { const p = names.get(fips); return p ? `${p.name}, ${p.st}` : `County ${fips}`; }
  function showDetail() {
    const det = $("detail");
    if (!selected) return;
    det.replaceChildren();
    const recs = D.counties[selected] || {}, m = merged.get(selected), y = +slider.value;
    det.append(el("strong", countyLabel(selected)));
    det.append(el("div", m ? `Earliest selected record: ${m.date} (${m.by.map(k => BY_KEY[k].label).join(", ")})`
                          : "No record from the selected sources."));
    const ul = el("ul");
    SOURCES.forEach(s => {
      if (!recs[s.key]) return;
      const [date, id] = recs[s.key];
      const notes = [];
      if (!checked.has(s.key)) notes.push("source not selected");
      else if (m && m.by.includes(s.key)) notes.push("earliest");
      else notes.push("later record");
      if (year(date) > y) notes.push("after " + y);
      const li = el("li");
      li.append(el("span", `${s.label}: first record ${date} — `), recordLink(s.key, id, "view record"),
                el("span", ` (${notes.join(", ")})`));
      if (!checked.has(s.key)) li.className = "off";
      ul.append(li);
    });
    if (ul.children.length) det.append(ul);
  }

  // ---- chart + tables -------------------------------------------------------------
  const cw = 560, ch = 280, mg = { t: 12, r: 44, b: 28, l: 44 };
  const cs = d3.select("#chart").attr("viewBox", `0 0 ${cw} ${ch}`);
  let cumul = [];  // one entry per year Y0..Y1, so cumul[y - Y0] is year y
  function computeCumulative() {
    cumul = [];
    if (Y0 === null || !merged.size) return;
    const by = d3.rollup([...merged.values()], v => v.length, m => m.y);
    let acc = 0;
    cumul = d3.range(Y0, Y1 + 1).map(y => ({ y, n: by.get(y) || 0, v: (acc += by.get(y) || 0) }));
  }
  let cursor = null, dot = null, xS = null, yS = null;
  function renderChart() {
    cs.selectAll("*").remove(); cursor = dot = null;
    if (!cumul.length) return;
    xS = d3.scaleLinear().domain([Y0, Math.max(Y1, Y0 + 1)]).range([mg.l, cw - mg.r]);
    yS = d3.scaleLinear().domain([0, Math.max(1, cumul[cumul.length - 1].v)]).nice().range([ch - mg.b, mg.t]);
    const step = Math.ceil((Y1 - Y0 + 1) / 8);
    const ticks = d3.range(Y0, Y1 + 1).filter(y => (Y1 - y) % step === 0);
    cs.append("g").attr("class", "gridl").attr("transform", `translate(${mg.l},0)`)
      .call(d3.axisLeft(yS).ticks(5).tickSize(-(cw - mg.l - mg.r)).tickFormat("")).call(g => g.select(".domain").remove());
    cs.append("g").attr("class", "axis").attr("transform", `translate(0,${ch - mg.b})`)
      .call(d3.axisBottom(xS).tickValues(ticks).tickFormat(d3.format("d")));
    cs.append("g").attr("class", "axis").attr("transform", `translate(${mg.l},0)`).call(d3.axisLeft(yS).ticks(5)).call(g => g.select(".domain").remove());
    cs.append("path").datum(cumul).attr("fill", "none").attr("stroke", "#2a6fbe").attr("stroke-width", 2)
      .attr("d", d3.line().x(d => xS(d.y)).y(d => yS(d.v)));
    const last = cumul[cumul.length - 1];
    cs.append("text").attr("x", xS(last.y) + 6).attr("y", yS(last.v) + 4).attr("font-size", 12).text(last.v);
    cursor = cs.append("line").attr("stroke", "#888").attr("stroke-dasharray", "3 3").attr("y1", mg.t).attr("y2", ch - mg.b);
    dot = cs.append("circle").attr("r", 4.5).attr("fill", "#2a6fbe").attr("stroke", "#fff").attr("stroke-width", 2);
    cs.append("rect").attr("x", mg.l).attr("y", mg.t).attr("width", cw - mg.l - mg.r).attr("height", ch - mg.t - mg.b)
      .attr("fill", "transparent").style("cursor", "crosshair")
      .on("pointermove", ev => {
        stop();
        slider.value = Math.min(Y1, Math.max(Y0, Math.round(xS.invert(d3.pointer(ev)[0]))));
        show();
      });
  }
  function renderTables() {
    const yb = $("yeartbl").tBodies[0]; yb.replaceChildren();
    cumul.forEach(d => { const tr = yb.insertRow(); [d.y, d.n, d.v].forEach((v, i) => { const c = tr.insertCell(); c.textContent = v; if (i) c.className = "n"; }); });
    const cb = $("countytbl").tBodies[0]; cb.replaceChildren();
    [...merged.entries()].sort((a, b) => a[1].date.localeCompare(b[1].date) || a[0].localeCompare(b[0])).forEach(([fips, m]) => {
      const tr = cb.insertRow();
      tr.insertCell().textContent = countyLabel(fips);
      tr.insertCell().textContent = m.date;
      const c = tr.insertCell();
      m.by.forEach((k, i) => { if (i) c.append(", "); c.append(recordLink(k, D.counties[fips][k][1], BY_KEY[k].label)); });
    });
  }

  // ---- state updates ----------------------------------------------------------------
  function show() {
    const y = +slider.value;
    const has = cumul.length > 0;
    $("yrlabel").textContent = Y0 === null ? "–" : y;
    $("stYr").textContent = Y0 === null ? "–" : y;
    const c = has ? cumul[y - Y0] : null;
    const states = new Set();
    merged.forEach((m, fips) => { if (m.y <= y) states.add(fips.slice(0, 2)); });
    // A measured zero shows 0; no data or no selection shows a dash.
    const measured = checked.size > 0;  // checked only ever holds published sources
    $("stCum").textContent = c ? c.v : (measured ? 0 : "–");
    $("stNew").textContent = c ? c.n : (measured ? 0 : "–");
    $("stSt").textContent = has ? states.size : (measured ? 0 : "–");
    if (cursor && c) { cursor.attr("x1", xS(y)).attr("x2", xS(y)); dot.attr("cx", xS(y)).attr("cy", yS(c.v)); }
    showDetail();
    paint();
  }
  function recompute() {
    stop();
    mergeSelection(); computeCumulative(); renderChart(); renderTables();
    if (!published.length) notice.textContent = "No data has been published for this species yet.";
    else if (!checked.size) notice.textContent = "Select at least one source.";
    else if (!merged.size) notice.textContent = "The selected sources have no eligible records in US counties.";
    else notice.textContent = "";
    notice.hidden = !notice.textContent;
    const playable = cumul.length > 1;
    btn.disabled = !playable;
    slider.disabled = !cumul.length;
    show();
  }

  // ---- playback ---------------------------------------------------------------------
  let timer = null;
  const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;
  function stop() { clearInterval(timer); timer = null; btn.textContent = "Play"; }
  btn.addEventListener("click", () => {
    if (timer) return stop();
    let y = +slider.value >= Y1 ? Y0 : +slider.value;
    btn.textContent = "Pause"; slider.value = y; show();
    timer = setInterval(() => { if (++y > Y1) return stop(); slider.value = y; show(); }, reduceMotion ? 1600 : 800);
  });
  slider.addEventListener("input", () => { stop(); show(); });

  recompute();
})();
