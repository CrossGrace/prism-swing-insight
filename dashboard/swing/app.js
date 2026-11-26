"use strict";

const el = (id) => document.getElementById(id);
const currency = new Intl.NumberFormat("ko-KR", {
  style: "currency", currency: "KRW", maximumFractionDigits: 3,
});
const initialCash = 10_000_000;
const cashFormat = new Intl.NumberFormat("ko-KR", {
  style: "currency", currency: "KRW", maximumFractionDigits: 0,
});
const sharesText = (quantity) => `${quantity.toLocaleString("ko-KR")}주`;
const sharePct = (pct) => `${pct.toFixed(1)}%`;
const percent = (value) => value == null ? "—" : `${value > 0 ? "+" : ""}${value.toFixed(2)}%`;
const exits = {
  STOP: "손절",
  STOP_COLLISION: "손절·익절 동시 도달 · 손절 우선",
  ENTRY_BAR_STOP_ASSUMED: "돌파일 저가가 손절선 하회 · 순서 미확인으로 손절 가정",
  TRAIL_GAP: "갭 하락 · 추적손절",
  TRAIL_STOP: "추적손절",
  PARTIAL_TAKE: "목표가 도달 · 분할 익절",
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
    node("span", `${sharesText(fill.quantity)} (${sharePct(fill.quantityPct)}) · 체결 후 ${sharesText(fill.holding)} · 현금 ${cashFormat.format(fill.cash)} · ${fill.reason}${fill.net == null ? "" : ` · 거래 순수익률 ${percent(fill.net)}`}`),
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
  const plotted = [...series, ...fills.map((fill) => fill.price)];
  const min = Math.min(...plotted);
  const max = Math.max(...plotted);
  const step = Math.max(1000, 1000 * Math.ceil((max - min) / 6000));
  const lower = Math.floor((min - 1) / step) * step;
  const upper = Math.ceil((max + 1) / step) * step;
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
  const fillCounts = new Map();
  for (const {date} of fills) fillCounts.set(date, (fillCounts.get(date) ?? 0) + 1);
  const sellOffsets = new Map();
  for (const fill of fills) {
    const i = positions.get(fill.date);
    if (i === undefined) continue;
    const stem = y(fill.price);
    const top = stem + (fill.side === "buy" ? 18 : -18);
    const sellOffset = sellOffsets.get(fill.date) ?? 0;
    if (fill.side === "sell") sellOffsets.set(fill.date, sellOffset + 1);
    const offset = fillCounts.get(fill.date) > 1
      ? (fill.side === "buy" ? -14 : 14 + sellOffset * 24)
      : 0;
    const markerX = x(i) + offset;
    ctx.lineWidth = 1;
    ctx.strokeStyle = color(fill.side === "buy" ? "--buy" : "--sell");
    ctx.beginPath();
    ctx.moveTo(x(i), stem);
    ctx.lineTo(markerX, top);
    ctx.stroke();
    const marker = node("button", fill.side === "buy" ? "↑" : "↓", `marker ${fill.side}`);
    marker.type = "button";
    marker.style.left = `${markerX}px`;
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
    cell.colSpan = 8;
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
      node("td", `${sharesText(fill.quantity)} (${sharePct(fill.quantityPct)})`),
      node("td", sharesText(fill.holding)), node("td", cashFormat.format(fill.cash)),
      node("td", fill.reason), node("td", percent(fill.net), fill.net > 0 ? "positive" : fill.net < 0 ? "negative" : ""));
    body.append(row);
  }
}

function renderRules(scenario) {
  const model = scenario.model;
  const recovery = scenario.recovery;
  el("policy-mode").textContent = [el("strategy"), el("mode")]
    .map((select) => select.selectedOptions[0].textContent).join(" · ");
  const rows = [
    ["판단 시점", recovery
      ? `하락일 종가에 감시 시작 · 이후 ${recovery.wait_sessions}거래일 안에 직전 거래일 고가 도달 시 진입. 시가가 고가 이상이면 시가 적용.`
      : "확정 일봉의 종가까지 확인하고, 다음 거래일 시가에 가상 진입"],
    ["진입", `Wilder RSI(14) ≤ ${model.rsi_ceiling} · 3거래일 등락률 ≤ −${model.three_day_dip_pct}% · ${recovery ? "당일 종가 하락" : "당일 종가 반등"}`],
    ...(recovery?.gap_entry ? [["갭 진입", `감시 시작 다음 거래일 시가가 신호일 종가보다 ${model.three_day_dip_pct}% 이상 낮으면 시가 진입.`]] : []),
    ["시장 조건", el("mode").value === "strict" ? "실제 코스피 일봉으로 프리즘 시장 상태 계산 · 상승장에서만 신규 진입" : "비교용 시나리오 · 코스피를 상승장으로 가정"],
    ["청산", `손절 −${model.stop_loss_pct}% · 익절 +${model.take_profit_pct}% · ${recovery ? `목표가에서 ${recovery.first_sale_fraction * 100}% 청산 · 잔량은 이후 종가 기준 ${recovery.trail_pct}% 추적손절 · ` : ""}최대 보유 ${model.max_hold_days}거래일`],
    ["일봉 체결", recovery
      ? "돌파일 저가가 손절선 아래면 장중 순서를 알 수 없어 손절로 처리합니다. 추적손절은 종가 확정 후 다음 거래일부터 적용합니다."
      : "시가 진입 후 당일 청산 가능. 손절·익절이 모두 닿으면 손절 우선. 갭 하락은 시가, 익절 갭 상승은 목표가 기준."],
    ["종료일 보유", "미청산 매수는 종료일 종가로 평가하고 가정한 매도 비용을 차감합니다. 완료 거래와 승률에는 포함하지 않습니다."],
    ["비용 가정", `수수료 편도 ${model.fee_bps}bp · 매도세 ${model.sell_tax_bps}bp · 슬리피지 편도 ${model.slippage_bps}bp`],
    ["투입·수량", "종목별 모의 계좌에 1,000만원을 배정합니다. 수수료와 세금을 반영하며 거래 수량은 정수 주로 계산합니다."],
    ["재생 범위", "모든 정책은 같은 블라인드 일봉 엔진을 사용합니다. 확정된 봉을 날짜순으로 공개합니다. 과거 재생이며 신규 정책의 표본 외 성과는 미검증입니다."],
  ];
  el("rules").replaceChildren(...rows.flatMap(([label, value]) => [node("dt", label), node("dd", value)]));
  const rejected = scenario.market_guard_rejections;
  el("rejections").hidden = !rejected.length;
  el("rejection-list").replaceChildren(...rejected.map((row) =>
    node("li", `${row.date} · ${marketReasons[row.reason] ?? row.reason}`)));
}

function applyAccount(fills, model, lastClose) {
  const buyFee = 1 + model.fee_bps / 10000;
  const sellFactor = 1 - (model.fee_bps + model.sell_tax_bps) / 10000;
  const closingProceeds = lastClose * sellFactor * (1 - model.slippage_bps / 10000);
  let cash = initialCash;
  let holding = 0;
  let originalQuantity = 0;
  const executed = [];
  for (const fill of fills) {
    let quantity;
    let quantityPct;
    if (fill.side === "buy") {
      const perShare = fill.price * buyFee;
      quantity = Math.floor(cash * fill.fraction / perShare);
      if (!quantity) continue;
      const cost = quantity * perShare;
      quantityPct = 100 * cost / cash;
      cash -= cost;
      holding = quantity;
      originalQuantity = quantity;
    } else {
      quantity = fill.final ? holding : Math.min(holding, Math.floor(originalQuantity * fill.fraction));
      if (!quantity) continue;
      quantityPct = 100 * quantity / holding;
      cash += quantity * fill.price * sellFactor;
      holding -= quantity;
    }
    executed.push({...fill, quantity, quantityPct, cash, holding});
  }
  const marketValue = holding * lastClose;
  const returnPct = (100 * (cash + holding * closingProceeds - initialCash)) / initialCash;
  return {fills: executed, cash, holding, marketValue, returnPct};
}

function render() {
  const instrument = dataset.instruments.find((item) => item.ticker === el("ticker").value);
  const variants = {
    baseline: instrument.variants,
    recovery: instrument.recovery_variants,
    gap: instrument.gap_variants,
  }[el("strategy").value];
  const scenario = variants[el("mode").value];
  prices = instrument.prices;
  el("chart-title").textContent = `${instrument.name} 일봉 가격 흐름`;
  const entryFill = (trade, id, status) => ({
    id, date: trade.entry_date, side: "buy", label: "매수",
    price: trade.entry_price, fraction: trade.position_fraction_of_sleeve ?? 1,
    reason: `${trade.signal_date} 종가 신호 · ${{
      RECOVERY_GAP: "하락 갭 시가 진입",
      RECOVERY_OPEN: "시가 돌파 진입",
      RECOVERY_BREAKOUT: "직전 고가 돌파 진입",
    }[trade.entry_basis] ?? "다음 거래일 시가 진입"}${status}`,
  });
  const sellFills = (trade, id, legs, closed = true) => legs.map((leg, i) => ({
    id: `${id}-sell-${i}`, date: leg.exit_date, side: "sell",
    label: "매도", fraction: leg.fraction ?? 1, final: closed && i === legs.length - 1,
    price: leg.exit_price, net: leg.net_return_pct,
    reason: `${trade.entry_date === leg.exit_date ? "당일 청산 · " : ""}${exits[leg.exit_reason] ?? leg.exit_reason}`,
  }));
  fills = scenario.trades.flatMap((trade, i) => [
    entryFill(trade, `${i}-buy`, ""),
    ...sellFills(trade, i, trade.exit_legs ?? [trade]),
  ]);
  if (scenario.open_position) {
    const position = scenario.open_position;
    fills.push(entryFill(position, "open-buy", ` · 종료일 미청산 · 평가 ${percent(position.marked_net_return_pct)}`),
      ...sellFills(position, "open", position.exit_legs ?? [], false));
  }
  fills.sort((a, b) => a.date.localeCompare(b.date));
  const account = applyAccount(fills, scenario.model, prices.at(-1).close);
  fills = account.fills;
  el("cash").textContent = cashFormat.format(account.cash);
  el("holding").textContent = sharesText(account.holding);
  el("holding-value").textContent = cashFormat.format(account.marketValue);
  metric("result", account.returnPct);
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
el("strategy").addEventListener("change", render);
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
