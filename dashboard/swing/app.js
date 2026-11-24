"use strict";

const el = (id) => document.getElementById(id);
const currency = new Intl.NumberFormat("ko-KR", {
  style: "currency", currency: "KRW", maximumFractionDigits: 3,
});
const percent = (value) => value == null ? "—" : `${value > 0 ? "+" : ""}${value.toFixed(2)}%`;
const exits = {
  STOP_GAP: "갭 하락 손절",
  STOP_FIRST_OR_COLLISION: "손절 · 동시 도달 시 손절 우선",
  TAKE: "목표가 도달",
  TAKE_GAP_CAPPED: "갭 상승 · 목표가 기준",
  TIME_EXIT: "보유기간 만료",
};
const marketReasons = {MARKET_CORRECTION: "코스피 조정장", MARKET_UNDER_PRESSURE: "코스피 상승세 약화"};
const tabs = document.querySelectorAll("[data-tab]");
const colors = getComputedStyle(document.documentElement);
const color = (key) => colors.getPropertyValue(key).trim();
let dataset;
let fills = [];
let prices = [];
let selected = null;

function node(tag, text, className) {
  const result = document.createElement(tag);
  if (text !== undefined) result.textContent = text;
  if (className) result.className = className;
  return result;
}

function metric(id, value) {
  const target = el(id);
  target.textContent = percent(value);
  target.className = value > 0 ? "positive" : value < 0 ? "negative" : "";
}

function selectFill(fill, reveal = false) {
  selected = fill?.id ?? null;
  for (const button of document.querySelectorAll("[data-marker]")) {
    button.setAttribute("aria-pressed", String(button.dataset.marker === selected));
  }
  for (const row of el("trades").querySelectorAll("[data-fill]")) {
    row.classList.toggle("selected", row.dataset.fill === selected);
  }
  const detail = el("detail");
  if (!fill) {
    detail.textContent = fills.length
      ? "화살표 또는 거래표의 날짜를 선택하면 체결 상세가 표시됩니다."
      : "이 시나리오에서 체결된 거래가 없습니다.";
    return;
  }
  detail.replaceChildren(
    node("strong", `${fill.date} · ${fill.label} · ${currency.format(fill.price)} / 주`),
    node("span", `${fill.reason}${fill.net == null ? "" : ` · 거래 순수익률 ${percent(fill.net)}`}`),
  );
  if (reveal) {
    const marker = el("markers").querySelector(`[data-marker="${selected}"]`);
    const scroll = el("plot").parentElement;
    if (marker) scroll.scrollLeft = marker.offsetLeft - scroll.clientWidth / 2;
  }
}

function draw() {
  if (!prices.length || el("overview").hidden) return;
  const canvas = el("chart");
  const {width, height} = el("plot").getBoundingClientRect();
  if (!width || !height) return;
  const scale = window.devicePixelRatio || 1;
  canvas.width = Math.round(width * scale);
  canvas.height = Math.round(height * scale);
  const ctx = canvas.getContext("2d");
  ctx.scale(scale, scale);
  const inset = {left: 64, right: 48, top: 37, bottom: 42};
  const last = prices.length - 1;
  const series = prices.map((day) => day.close);
  const step = Math.max(1000, 1000 * Math.ceil((Math.max(...series) - Math.min(...series)) / 6000));
  const lower = Math.floor((Math.min(...series) - 1) / step) * step;
  const upper = Math.ceil((Math.max(...series) + 1) / step) * step;
  const x = (i) => inset.left + i / Math.max(last, 1) * (width - inset.left - inset.right);
  const y = (value) => inset.top + (upper - value) / (upper - lower) * (height - inset.top - inset.bottom);
  const positions = new Map(prices.map((day, i) => [day.date, i]));

  ctx.font = "11px system-ui";
  ctx.textAlign = "right";
  ctx.textBaseline = "middle";
  for (let i = 0; i <= 4; i += 1) {
    const level = upper - (upper - lower) * i / 4;
    const top = y(level);
    ctx.strokeStyle = color("--line");
    ctx.beginPath();
    ctx.moveTo(inset.left, top);
    ctx.lineTo(width - inset.right, top);
    ctx.stroke();
    ctx.fillStyle = color("--muted");
    ctx.fillText(Math.round(level).toLocaleString("ko-KR"), inset.left - 9, top);
  }

  ctx.beginPath();
  series.forEach((close, i) => {
    if (i === 0) ctx.moveTo(x(i), y(close));
    else ctx.lineTo(x(i), y(close));
  });
  ctx.strokeStyle = color("--accent");
  ctx.lineWidth = 2;
  ctx.stroke();
  ctx.lineTo(x(last), height - inset.bottom);
  ctx.lineTo(x(0), height - inset.bottom);
  ctx.closePath();
  const gradient = ctx.createLinearGradient(0, inset.top, 0, height - inset.bottom);
  gradient.addColorStop(0, "#7fdeb52c");
  gradient.addColorStop(1, "#7fdeb504");
  ctx.fillStyle = gradient;
  ctx.fill();

  ctx.textAlign = "center";
  ctx.fillStyle = color("--muted");
  for (const i of new Set(Array.from({length: 7}, (_, tick) => Math.round(last * tick / 6)))) {
    ctx.fillText(prices[i].date.slice(5).replace("-", "."), x(i), height - 13);
  }

  const layer = el("markers");
  const prior = document.activeElement?.dataset.marker;
  layer.replaceChildren();
  for (const fill of fills) {
    const i = positions.get(fill.date);
    if (i === undefined) continue;
    const stem = y(series[i]);
    const top = stem + (fill.side === "buy" ? 20 : -20);
    ctx.lineWidth = 1;
    ctx.strokeStyle = color(fill.side === "buy" ? "--buy" : "--sell");
    ctx.beginPath();
    ctx.moveTo(x(i), stem);
    ctx.lineTo(x(i), top);
    ctx.stroke();
    const marker = node("button", fill.side === "buy" ? "↑" : "↓", `marker ${fill.side}`);
    marker.type = "button";
    marker.style.left = `${x(i)}px`;
    marker.style.top = `${top}px`;
    marker.dataset.marker = fill.id;
    marker.setAttribute("aria-label", `${fill.date} ${fill.label} ${currency.format(fill.price)}`);
    marker.setAttribute("aria-pressed", String(selected === fill.id));
    marker.addEventListener("click", () => selectFill(fill));
    layer.append(marker);
    if (prior === fill.id) marker.focus({preventScroll: true});
  }
}

function renderTable() {
  el("row-count").textContent = `${fills.length}개 체결 표시`;
  const body = el("trades");
  body.replaceChildren();
  if (!fills.length) {
    const cell = node("td", "이 기간에 표시할 모의 체결이 없습니다. 차단된 신호는 적용 정책 탭에서 확인할 수 있습니다.", "empty");
    cell.colSpan = 5;
    const row = node("tr");
    row.append(cell);
    body.append(row);
  }
  for (const fill of fills) {
    const row = node("tr");
    row.dataset.fill = fill.id;
    const date = node("td");
    const button = node("button", fill.date);
    button.type = "button";
    button.setAttribute("aria-label", `${fill.date} ${fill.label} 체결 상세`);
    button.addEventListener("click", () => selectFill(fill, true));
    date.append(button);
    row.append(date, node("td", fill.label, fill.side), node("td", currency.format(fill.price)),
      node("td", fill.reason), node("td", percent(fill.net), fill.net > 0 ? "positive" : fill.net < 0 ? "negative" : ""));
    body.append(row);
  }
}

function renderRules(scenario) {
  const model = scenario.model;
  el("policy-mode").textContent = el("mode").selectedOptions[0].textContent;
  const rows = [
    ["판단 시점", "확정 일봉의 종가까지 확인하고, 다음 거래일 시가에 가상 진입"],
    ["진입", `Wilder RSI(14) ≤ ${model.rsi_ceiling} · 3거래일 등락률 ≤ −${model.three_day_dip_pct}% · 당일 종가 반등`],
    ["시장 조건", el("mode").value === "strict" ? "실제 코스피 일봉으로 프리즘 시장 상태 계산 · 상승장에서만 신규 진입" : "비교용 시나리오 · 코스피를 상승장으로 가정"],
    ["청산", `손절 −${model.stop_loss_pct}% · 익절 +${model.take_profit_pct}% · 최대 보유 ${model.max_hold_days}거래일`],
    ["일봉 체결", "같은 날 손절·익절이 모두 닿으면 손절 우선. 갭 하락은 시가, 익절 갭 상승은 목표가 기준."],
    ["비용 가정", `수수료 편도 ${model.fee_bps}bp · 매도세 ${model.sell_tax_bps}bp · 슬리피지 편도 ${model.slippage_bps}bp`],
    ["투입·수량", "종목별 한 포지션을 가정. 가상 체결은 주당 가격으로 기록하며 주문 수량·총 금액은 기록하지 않음."],
    ["재생 범위", "가격과 거래량 기반의 과거 연구 재생. LLM 판단, 분할 주문, 증권사 체결은 포함하지 않음."],
  ];
  el("rules").replaceChildren(...rows.flatMap(([label, value]) => [node("dt", label), node("dd", value)]));
  const rejected = scenario.market_guard_rejections;
  el("rejections").hidden = !rejected.length;
  el("rejection-list").replaceChildren(...rejected.map((row) =>
    node("li", `${row.date} · ${marketReasons[row.reason] ?? row.reason}`)));
}

function render() {
  const instrument = dataset.instruments.find((item) => item.ticker === el("ticker").value);
  const scenario = instrument.variants[el("mode").value];
  prices = instrument.prices;
  el("chart-title").textContent = `${instrument.name} 일봉 가격 흐름`;
  fills = scenario.trades.flatMap((trade, i) => [{
    id: `${i}-buy`, date: trade.entry_date, side: "buy", label: "매수",
    price: trade.entry_price, reason: `다음 거래일 시가 · 신호일 RSI ${trade.rsi14_signal}`,
  }, {
    id: `${i}-sell`, date: trade.exit_date, side: "sell", label: "매도",
    price: trade.exit_price, reason: exits[trade.exit_reason] ?? trade.exit_reason, net: trade.net_return_pct,
  }]).sort((a, b) => a.date.localeCompare(b.date));
  metric("result", scenario.compounded_net_sleeve_pct);
  metric("benchmark", scenario.buyhold_close_to_close_pct);
  el("count").textContent = `${scenario.count}건`;
  el("win").textContent = scenario.win_rate_pct == null ? "—" : `${scenario.win_rate_pct.toFixed(0)}%`;
  renderTable();
  selectFill(null);
  renderRules(scenario);
  draw();
}

for (const tab of tabs) {
  tab.addEventListener("click", () => {
    for (const other of tabs) other.removeAttribute("aria-current");
    tab.setAttribute("aria-current", "page");
    el("overview").hidden = tab.dataset.tab !== "overview";
    el("policy").hidden = tab.dataset.tab !== "policy";
    draw();
  });
}
el("ticker").addEventListener("change", render);
el("mode").addEventListener("change", render);
new ResizeObserver(draw).observe(el("plot"));

async function init() {
  try {
    const response = await fetch("./replay.json");
    if (!response.ok) throw new Error("replay load failed");
    dataset = await response.json();
    for (const instrument of dataset.instruments) {
      el("ticker").add(new Option(`${instrument.name} · ${instrument.ticker}`, instrument.ticker));
    }
    el("period").textContent = `${dataset.start} – ${dataset.end}`;
    el("workspace").hidden = false;
    render();
  } catch {
    el("error").textContent = "일봉 재생 데이터를 불러오지 못했습니다.";
    el("error").hidden = false;
  }
}
void init();
