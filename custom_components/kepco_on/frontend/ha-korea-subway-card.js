/* HA for Korea subway map. Native iOS Live Activities are sent by the integration. */
const SVG_NS = "http://www.w3.org/2000/svg";
const NETWORK_URL = "/kepco_on/subway-network.json";

export function formatDuration(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return "시간 정보 없음";
  const minutes = Math.ceil(seconds / 60);
  return minutes >= 60
    ? `${Math.floor(minutes / 60)}시간${minutes % 60 ? ` ${minutes % 60}분` : ""}`
    : `${minutes}분`;
}

function stopId(value) {
  if (typeof value !== "string" || !value.trim() || value.length > 128) {
    throw new Error("역을 선택해 주세요.");
  }
  return value;
}

export function routeRequest(selection) {
  const origin = stopId(selection.origin);
  const destination = stopId(selection.destination);
  const via = selection.via ?? [];
  if (!Array.isArray(via) || via.length > 5) throw new Error("경유역은 5개까지 선택할 수 있습니다.");
  if (!["time", "transfers"].includes(selection.preference)) throw new Error("경로 기준을 확인해 주세요.");
  if (origin === destination) throw new Error("출발역과 도착역을 다르게 선택해 주세요.");
  const ids = via.map(stopId);
  if (new Set([origin, ...ids, destination]).size !== ids.length + 2) {
    throw new Error("같은 역을 중복해서 선택할 수 없습니다.");
  }
  return { type: "kepco_on/subway_route", origin, destination, via: ids, preference: selection.preference };
}

export function journeyRequest(command, options = {}) {
  if (!["start", "board", "next", "stop", "status"].includes(command)) throw new Error("잘못된 이동 명령입니다.");
  const request = { type: "kepco_on/subway_journey", command };
  if (command === "start") {
    const { type, ...route } = routeRequest(options);
    if (typeof options.notify_service !== "string" || !/^notify\.mobile_app_[a-z0-9_]+$/.test(options.notify_service)) {
      throw new Error("카드 설정에 iPhone의 notify.mobile_app 알림 서비스를 입력해 주세요.");
    }
    if (typeof options.url !== "string" || !options.url.startsWith("/") || options.url.startsWith("//") || /[\\\u0000-\u0020]/.test(options.url)) {
      throw new Error("대시보드 경로는 /로 시작하는 내부 주소여야 합니다.");
    }
    Object.assign(request, route, { notify_service: options.notify_service, url: options.url });
  }
  return request;
}

export function searchStations(stations, query, limit = 30) {
  const term = String(query).trim().replace(/\s/g, "").toLocaleLowerCase("ko");
  if (!term) return [];
  const seen = new Set();
  return stations.filter((station) => {
    const key = station.group || station.id;
    if (seen.has(key) || !station.name.replace(/\s/g, "").toLocaleLowerCase("ko").includes(term)) return false;
    seen.add(key);
    return true;
  }).sort((a, b) => Number(b.name === term) - Number(a.name === term) || a.name.localeCompare(b.name, "ko")).slice(0, limit);
}

export function networkBounds(stations, lines) {
  const points = stations.map(({ x, y }) => [x, y]);
  for (const line of lines) for (const path of line.paths ?? []) points.push(...path);
  const valid = points.filter(([x, y]) => Number.isFinite(x) && Number.isFinite(y));
  if (!valid.length) throw new Error("노선도 좌표가 없습니다.");
  const xs = valid.map(([x]) => x), ys = valid.map(([, y]) => y);
  const padding = 12;
  return { x: Math.min(...xs) - padding, y: Math.min(...ys) - padding,
    width: Math.max(...xs) - Math.min(...xs) + padding * 2,
    height: Math.max(...ys) - Math.min(...ys) + padding * 2 };
}

export function labelLayout(position) {
  const aliases = { EN: "NE", WN: "NW", ES: "SE", WS: "SW" };
  const normalized = String(position || "E").toUpperCase();
  const layouts = {
    N: { dx: 0, dy: -1.9, anchor: "middle" },
    S: { dx: 0, dy: 2, anchor: "middle" },
    E: { dx: 1.8, dy: 0, anchor: "start" },
    W: { dx: -1.8, dy: 0, anchor: "end" },
    NE: { dx: 1.5, dy: -1.5, anchor: "start" },
    NW: { dx: -1.5, dy: -1.5, anchor: "end" },
    SE: { dx: 1.5, dy: 1.5, anchor: "start" },
    SW: { dx: -1.5, dy: 1.5, anchor: "end" },
  };
  return layouts[aliases[normalized] || normalized] || layouts.E;
}

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text != null) node.textContent = String(text);
  if (className) node.className = className;
  return node;
}

function svgElement(tag, attributes = {}) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, String(value));
  return node;
}

const BaseElement = globalThis.HTMLElement ?? class {};
export class HaKoreaSubwayCard extends BaseElement {
  static getStubConfig() { return { title: "수도권 지하철", notify_service: "" }; }

  constructor() {
    super();
    if (!this.attachShadow) return;
    this.attachShadow({ mode: "open" });
    this._selection = { origin: "", destination: "", via: [], preference: "time" };
    this._pointers = new Map();
    this._journey = { active: false };
    this._routeVersion = 0;
    this._journeyVersion = 0;
    this._busy = false;
  }

  setConfig(config) {
    if (!config || typeof config !== "object") throw new Error("카드 설정이 필요합니다.");
    this._config = { title: "수도권 지하철", ...config };
    if (this._built) { this._title.textContent = this._config.title; this._renderRoute(); }
  }

  set hass(value) {
    this._hass = value;
    if (this.isConnected) this._initialize();
  }

  connectedCallback() {
    this._initialize();
    this._timer = setInterval(() => {
      if (this._journey.active && !this._busy) this._refreshStatus();
    }, 30000);
  }

  disconnectedCallback() {
    clearInterval(this._timer);
    this._abort?.abort();
    this._abort = null;
  }

  getCardSize() { return 12; }
  getGridOptions() { return { columns: 12, rows: 12, min_columns: 6, min_rows: 8 }; }

  async _initialize() {
    if (!this._built) this._build();
    if (!this._network && !this._loading) {
      this._loading = true;
      this._error.textContent = "노선도를 불러오는 중…";
      this._abort = new AbortController();
      try {
        const response = await fetch(NETWORK_URL, { credentials: "same-origin", signal: this._abort.signal });
        if (!response.ok) throw new Error("노선도를 불러오지 못했습니다. HA for Korea 업데이트와 재시작을 확인해 주세요.");
        const network = await response.json();
        if (!Array.isArray(network.stations) || !Array.isArray(network.lines)) throw new Error("노선도 형식을 확인해 주세요.");
        this._network = network;
        this._stations = new Map(network.stations.map((station) => [station.id, station]));
        this._bounds = networkBounds(network.stations, network.lines);
        this._view = { ...this._bounds };
        this._drawMap();
        this._error.textContent = "";
      } catch (error) {
        if (error.name !== "AbortError") this._error.textContent = error.message;
      } finally { this._loading = false; }
    }
    if (this._hass && !this._statusInitialized) {
      this._statusInitialized = true;
      await this._refreshStatus();
    }
  }

  _build() {
    this._built = true;
    const style = element("style");
    style.textContent = `
      :host{display:block;color:var(--primary-text-color,#172332)}
      *{box-sizing:border-box}ha-card{display:block;overflow:hidden;background:var(--card-background-color,#fff)}
      .content{padding:18px}.heading{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:14px}
      h2{font-size:21px;font-weight:600;margin:0}.caption{font-size:12px;color:var(--secondary-text-color,#667085);line-height:1.6}
      button,input,select{font:inherit}button{cursor:pointer;border:1px solid var(--divider-color,#dce1e8);border-radius:10px;padding:9px 12px;background:var(--card-background-color,#fff);color:inherit;min-height:42px}
      button:hover{background:var(--secondary-background-color,#f0f3f8)}button:disabled{cursor:default;opacity:.45}.primary{background:var(--primary-color,#1764d9);color:#fff;border-color:transparent}.primary:hover{filter:brightness(.94);background:var(--primary-color,#1764d9)}
      .search{position:relative;margin-bottom:12px}input{width:100%;padding:12px;border:1px solid var(--divider-color,#dce1e8);border-radius:10px;background:var(--card-background-color,#fff);color:inherit}
      .search-results{position:absolute;z-index:5;top:100%;width:100%;max-height:250px;overflow:auto;background:var(--card-background-color,#fff);box-shadow:0 6px 18px #0002;border-radius:10px}
      .search-results button{display:block;text-align:left;width:100%;border:0;border-radius:0}.empty{padding:12px}
      .stops{display:grid;grid-template-columns:1fr auto 1fr;gap:8px;align-items:stretch;margin-bottom:9px}.stop{text-align:left;min-width:0}.stop small{display:block;color:var(--secondary-text-color,#667085);font-size:11px}.stop strong{display:block;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
      .via{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px}.via button{font-size:12px;padding:5px 9px;min-height:32px}
      .options{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:12px}.options select{color:inherit;background:var(--card-background-color,#fff);border:1px solid var(--divider-color,#dce1e8);padding:9px;border-radius:10px;min-height:42px}
      .map{position:relative;border:1px solid var(--divider-color,#dce1e8);border-radius:12px;overflow:hidden;background:#fafcff;height:clamp(330px,55vh,560px)}
      svg{display:block;width:100%;height:100%;touch-action:none;user-select:none}.map-controls{position:absolute;right:10px;top:10px;display:flex;gap:5px}.map-controls button{min-width:40px;padding:6px;box-shadow:0 2px 5px #0001}
      .station{cursor:pointer}.station:focus{outline:none}.station:focus circle{stroke:#111;stroke-width:1.4}.label{paint-order:stroke;stroke:#fff;stroke-width:.6;stroke-linejoin:round;fill:#273246;font:1.5px sans-serif;pointer-events:none}
      .selection{margin-top:12px;padding:12px;background:var(--secondary-background-color,#f0f3f8);border-radius:10px}.selection-name{font-weight:600;margin-bottom:9px}.actions{display:flex;gap:8px;flex-wrap:wrap}.selection[hidden],.search-results[hidden]{display:none}
      .result{margin-top:14px}.route-summary{display:flex;gap:12px;align-items:baseline;margin-bottom:10px}.route-summary strong{font-size:24px}.leg{border-left:3px solid var(--primary-color,#1764d9);padding:5px 0 5px 12px;margin:8px 0;font-size:14px}.leg small{display:block;margin-top:4px;color:var(--secondary-text-color,#667085)}
      .journey{margin-top:14px;padding:14px;border-radius:12px;background:var(--secondary-background-color,#f0f3f8)}.journey strong{display:block;margin-bottom:8px}.journey p{margin:0 0 10px;font-size:14px;line-height:1.6}
      .error{color:var(--error-color,#bb2b2b);font-size:13px;line-height:1.5;margin-top:10px}.error:empty{display:none}.source{margin-top:10px;font-size:11px;color:var(--secondary-text-color,#667085);line-height:1.5}.source a{color:inherit}
      @media(max-width:450px){.content{padding:12px}.heading h2{font-size:19px}.options{flex-wrap:wrap}.map{height:380px}.stops{gap:5px}.stop{padding:8px}.caption{font-size:11px}}
    `;
    const card = element("ha-card");
    const body = element("div", null, "content");
    const heading = element("div", null, "heading");
    this._title = element("h2", this._config?.title ?? "수도권 지하철");
    heading.append(this._title, element("span", "역을 눌러 경로 선택", "caption"));
    const search = element("div", null, "search");
    this._search = element("input");
    this._search.type = "search";
    this._search.placeholder = "역 이름 검색";
    this._search.setAttribute("aria-label", "지하철역 검색");
    this._search.setAttribute("autocomplete", "off");
    this._searchResults = element("div", null, "search-results");
    this._searchResults.hidden = true;
    this._search.addEventListener("input", () => this._renderSearch());
    this._search.addEventListener("keydown", (event) => {
      if (event.key === "Escape") this._searchResults.hidden = true;
      if (event.key === "Enter") this._searchResults.querySelector("button")?.click();
    });
    search.append(this._search, this._searchResults);
    this._stops = element("div", null, "stops");
    this._via = element("div", null, "via");
    const options = element("div", null, "options");
    this._preference = element("select");
    this._preference.setAttribute("aria-label", "경로 검색 기준");
    for (const [value, title] of [["time", "예상 시간 우선"], ["transfers", "최소 환승"]]) {
      const option = element("option", title); option.value = value; this._preference.append(option);
    }
    this._preference.addEventListener("change", () => { this._selection.preference = this._preference.value; this._invalidateRoute(); });
    this._routeButton = this._button("경로 검색", () => this._findRoute(), "primary");
    options.append(this._preference, this._routeButton);
    const map = element("div", null, "map");
    this._svg = svgElement("svg", { role: "group", "aria-label": "수도권 지하철 노선도. 확대와 이동 후 역을 선택하세요." });
    const controls = element("div", null, "map-controls");
    controls.append(this._button("+", () => this._zoom(.7), "", "노선도 확대"),
      this._button("−", () => this._zoom(1.4), "", "노선도 축소"),
      this._button("전체", () => { this._view = { ...this._bounds }; this._updateView(); }));
    map.append(this._svg, controls);
    this._bindMapGestures();
    this._pickedPanel = element("div", null, "selection"); this._pickedPanel.hidden = true;
    this._result = element("div", null, "result");
    this._journeyPanel = element("div");
    this._error = element("div", null, "error"); this._error.setAttribute("role", "status");
    this._source = element("div", null, "source");
    body.append(heading, search, this._stops, this._via, options, map, this._pickedPanel, this._result, this._journeyPanel, this._error, this._source);
    card.append(body);
    this.shadowRoot.append(style, card);
    this._renderStops();
  }

  _button(title, handler, className = "", ariaLabel) {
    const button = element("button", title, className); button.type = "button";
    if (ariaLabel) button.setAttribute("aria-label", ariaLabel);
    button.addEventListener("click", handler); return button;
  }

  _renderSearch() {
    this._searchResults.replaceChildren();
    const query = this._search.value;
    this._searchResults.hidden = !query.trim();
    const found = searchStations(this._network?.stations ?? [], query);
    for (const station of found) this._searchResults.append(this._button(`${station.name} · ${this._groupLines(station).join(" / ")}`, () => {
      this._pick(station.id); this._searchResults.hidden = true; this._search.value = ""; this._center(station);
    }));
    if (!found.length && query.trim()) this._searchResults.append(element("div", "검색된 역이 없습니다.", "empty"));
  }

  _groupLines(station) {
    const group = station.group || station.id;
    return [...new Set((this._network?.stations ?? []).filter((s) => (s.group || s.id) === group).map((s) => this._lineName(s.line)))];
  }

  _lineName(id) { return this._network?.lines.find((line) => line.id === id)?.name ?? id ?? ""; }
  _stationName(id) { return this._stations?.get(id)?.name ?? String(id ?? ""); }

  _renderStops() {
    this._stops.replaceChildren();
    for (const kind of ["origin", "destination"]) {
      const button = this._button("", () => { this._pickMode = kind; this._search.focus(); this._error.textContent = `${kind === "origin" ? "출발" : "도착"}역을 검색하거나 노선도에서 눌러 주세요.`; }, "stop");
      button.append(element("small", kind === "origin" ? "출발" : "도착"), element("strong", this._stationName(this._selection[kind]) || "역 선택"));
      this._stops.append(button);
      if (kind === "origin") this._stops.append(this._button("⇄", () => {
        [this._selection.origin, this._selection.destination] = [this._selection.destination, this._selection.origin];
        this._selection.via.reverse(); this._invalidateRoute(); this._renderStops(); this._markStations();
      }, "", "출발역과 도착역 바꾸기"));
    }
    this._via.replaceChildren();
    this._selection.via.forEach((id, index) => this._via.append(this._button(`경유 ${index + 1} · ${this._stationName(id)} ×`, () => {
      this._selection.via.splice(index, 1); this._invalidateRoute(); this._renderStops(); this._markStations();
    }, "", `${this._stationName(id)} 경유역 삭제`)));
    this._via.append(this._button("+ 경유역", () => { this._pickMode = "via"; this._search.focus(); this._error.textContent = "경유역을 검색하거나 노선도에서 눌러 주세요."; }));
  }

  _pick(id) {
    const station = this._stations?.get(id); if (!station) return;
    this._pickedPanel.hidden = false; this._pickedPanel.replaceChildren();
    this._pickedPanel.append(element("div", `${station.name} · ${this._groupLines(station).join(" / ")}`, "selection-name"));
    const actions = element("div", null, "actions");
    for (const [kind, label] of [["origin", "출발"], ["destination", "도착"], ["via", "경유"]]) {
      actions.append(this._button(label, () => this._selectStation(kind, id), this._pickMode === kind ? "primary" : ""));
    }
    actions.append(this._button("닫기", () => { this._pickedPanel.hidden = true; }));
    this._pickedPanel.append(actions);
  }

  _selectStation(kind, id) {
    const station = this._stations.get(id);
    const sameStation = (other) => { const s = this._stations.get(other); return s && (s.group || s.id) === (station.group || station.id); };
    const otherKind = kind === "origin" ? "destination" : "origin";
    if (sameStation(this._selection[otherKind]) || (kind === "via" && sameStation(this._selection.destination)) || this._selection.via.some(sameStation)) {
      this._error.textContent = "같은 역을 중복해서 선택할 수 없습니다."; return;
    }
    if (kind === "via") {
      if (this._selection.via.length >= 5) { this._error.textContent = "경유역은 5개까지 선택할 수 있습니다."; return; }
      this._selection.via.push(id);
    } else this._selection[kind] = id;
    this._pickMode = null; this._pickedPanel.hidden = true;
    this._invalidateRoute(); this._renderStops(); this._markStations();
  }

  _invalidateRoute() { this._routeVersion += 1; this._route = null; this._result.replaceChildren(); this._error.textContent = ""; this._highlight?.replaceChildren(); }

  _drawMap() {
    this._svg.replaceChildren();
    const tracks = svgElement("g", { fill: "none", "stroke-linecap": "round", "stroke-linejoin": "round" });
    for (const line of this._network.lines) {
      const color = /^#[0-9a-f]{3,8}$/i.test(line.color) ? line.color : "#687585";
      for (const path of line.paths ?? []) {
        if (!path.every((point) => Array.isArray(point) && point.length >= 2 && point.every(Number.isFinite))) continue;
        tracks.append(svgElement("polyline", { points: path.map((p) => p.join(",")).join(" "), stroke: color, "stroke-width": 1.1, opacity: .85 }));
      }
    }
    this._highlight = svgElement("g", { fill: "none", stroke: "#132946", "stroke-width": 2.1, "stroke-linecap": "round", "stroke-linejoin": "round", "pointer-events": "none" });
    const stationLayer = svgElement("g");
    const labelLayer = svgElement("g");
    const labeled = new Set();
    this._markers = [];
    for (const station of this._network.stations) {
      if (!Number.isFinite(station.x) || !Number.isFinite(station.y)) continue;
      const marker = svgElement("g", { class: "station", role: "button", tabindex: "0", "aria-label": `${station.name}, ${this._lineName(station.line)}` });
      marker.append(svgElement("circle", { cx: station.x, cy: station.y, r: 2.5, fill: "transparent", stroke: "none" }));
      const circle = svgElement("circle", { cx: station.x, cy: station.y, r: .9, fill: "#fff", stroke: "#5a6778", "stroke-width": .4 });
      marker.append(circle);
      marker.addEventListener("click", (event) => { if (!this._dragged) { event.stopPropagation(); this._pick(station.id); } });
      marker.addEventListener("keydown", (event) => { if (["Enter", " "].includes(event.key)) { event.preventDefault(); this._pick(station.id); } });
      stationLayer.append(marker); this._markers.push({ id: station.id, circle });
      const group = station.group || station.id;
      if (!labeled.has(group)) {
        labeled.add(group);
        const layout = labelLayout(station.labelPos);
        const label = svgElement("text", { x: station.x + layout.dx, y: station.y + layout.dy,
          "text-anchor": layout.anchor, "dominant-baseline": "middle", class: "label" }); label.textContent = station.name;
        labelLayer.append(label);
      }
    }
    this._svg.append(tracks, this._highlight, stationLayer, labelLayer);
    this._source.replaceChildren();
    const source = this._network.source;
    this._source.append(element("span", `노선도 자료: ${typeof source === "string" ? source : source?.attribution ?? source?.name ?? "서울교통공사"}${this._network.updated ? ` · ${this._network.updated}` : ""}. `));
    const sourceUrl = typeof source === "object" ? source?.url ?? source?.topology_page : null;
    if (typeof sourceUrl === "string" && /^https?:\/\//.test(sourceUrl)) {
      const link = element("a", "출처 보기"); link.href = sourceUrl; link.target = "_blank"; link.rel = "noopener noreferrer"; this._source.append(link);
    }
    this._source.append(element("span", "실시간 정보는 서울 열린데이터광장 제공. 운행 상황과 차이가 있을 수 있습니다."));
    this._updateView(); this._markStations();
  }

  _markStations() {
    const kinds = [[this._selection.origin, "#16865c"], [this._selection.destination, "#d43d43"], ...this._selection.via.map((id) => [id, "#e59212"])];
    for (const { id, circle } of this._markers ?? []) {
      const station = this._stations.get(id);
      const chosen = kinds.find(([chosenId]) => {
        const s = this._stations.get(chosenId); return s && (s.group || s.id) === (station.group || station.id);
      });
      circle.setAttribute("fill", chosen?.[1] ?? "#fff"); circle.setAttribute("r", chosen ? "1.5" : ".9");
    }
  }

  _updateView() { if (this._view) this._svg.setAttribute("viewBox", `${this._view.x} ${this._view.y} ${this._view.width} ${this._view.height}`); }

  _zoom(factor, point) {
    if (!this._view || !Number.isFinite(factor)) return;
    const min = this._bounds.width / 16, max = this._bounds.width * 1.2;
    const width = Math.max(min, Math.min(max, this._view.width * factor));
    const actual = width / this._view.width;
    const center = point ?? { x: this._view.x + this._view.width / 2, y: this._view.y + this._view.height / 2 };
    this._view.x = center.x - (center.x - this._view.x) * actual;
    this._view.y = center.y - (center.y - this._view.y) * actual;
    this._view.width = width; this._view.height *= actual; this._updateView();
  }

  _mapPoint(clientX, clientY) {
    const matrix = this._svg.getScreenCTM();
    if (!matrix) return null;
    return new DOMPoint(clientX, clientY).matrixTransform(matrix.inverse());
  }

  _center(station) {
    const width = Math.min(this._view.width, 65);
    this._view = { x: station.x - width / 2, y: station.y - width / 2, width, height: width };
    this._updateView();
  }

  _bindMapGestures() {
    this._svg.addEventListener("wheel", (event) => {
      event.preventDefault(); this._zoom(event.deltaY > 0 ? 1.12 : .89, this._mapPoint(event.clientX, event.clientY));
    }, { passive: false });
    this._svg.addEventListener("pointerdown", (event) => {
      this._dragged = this._pointers.size > 0;
      const capture = event.target.closest(".station") ?? this._svg;
      this._pointers.set(event.pointerId, { x: event.clientX, y: event.clientY, originX: event.clientX, originY: event.clientY, capture });
      capture.setPointerCapture(event.pointerId);
    });
    this._svg.addEventListener("pointermove", (event) => {
      const previous = this._pointers.get(event.pointerId); if (!previous || !this._view) return;
      const current = { ...previous, x: event.clientX, y: event.clientY };
      if (Math.hypot(current.x - previous.originX, current.y - previous.originY) > 3) this._dragged = true;
      if (this._pointers.size === 2) {
        const other = [...this._pointers.entries()].find(([id]) => id !== event.pointerId)[1];
        const oldDistance = Math.hypot(previous.x - other.x, previous.y - other.y);
        const newDistance = Math.hypot(current.x - other.x, current.y - other.y);
        if (newDistance > 5 && oldDistance > 5) this._zoom(oldDistance / newDistance, this._mapPoint((current.x + other.x) / 2, (current.y + other.y) / 2));
      } else {
        const before = this._mapPoint(previous.x, previous.y), after = this._mapPoint(current.x, current.y);
        if (before && after) { this._view.x += before.x - after.x; this._view.y += before.y - after.y; this._updateView(); }
      }
      this._pointers.set(event.pointerId, current);
    });
    const release = (event) => {
      const previous = this._pointers.get(event.pointerId);
      this._pointers.delete(event.pointerId);
      if (previous?.capture.hasPointerCapture(event.pointerId)) previous.capture.releasePointerCapture(event.pointerId);
    };
    this._svg.addEventListener("pointerup", release); this._svg.addEventListener("pointercancel", release);
  }

  async _findRoute() {
    if (this._busy) return;
    try {
      const request = routeRequest(this._selection);
      const version = this._routeVersion;
      this._busy = true; this._routeButton.disabled = true; this._error.textContent = "경로를 찾는 중…";
      const route = await this._hass.callWS(request);
      if (version !== this._routeVersion) return;
      this._route = route; this._error.textContent = "";
      this._renderRoute(); this._drawRoute();
    } catch (error) { this._error.textContent = error.message ?? "경로를 찾지 못했습니다."; }
    finally { this._busy = false; this._routeButton.disabled = false; this._renderRoute(); }
  }

  _renderRoute() {
    this._result.replaceChildren(); if (!this._route) return;
    const summary = element("div", null, "route-summary");
    summary.append(element("strong", formatDuration(this._route.seconds)), element("span", `환승 ${this._route.transfers}회`));
    this._result.append(summary);
    if (this._route.estimated) this._result.append(element("div", "예상 소요시간 · 대기시간과 운행 지연은 포함하지 않습니다.", "caption"));
    for (const leg of this._route.legs ?? []) {
      const item = element("div", `${this._lineName(leg.line)} · ${this._stationName(leg.origin)} → ${this._stationName(leg.destination)}`, "leg");
      item.append(element("small", `${leg.direction || "방향 정보 없음"}${leg.station_ids ? ` · ${Math.max(0, leg.station_ids.length - 1)}개 역 이동` : ""}`));
      this._result.append(item);
    }
    const button = this._button(this._journey.active ? "새 경로로 안내 시작" : "iPhone 이동 안내 시작", () => this._journeyCommand("start"), "primary");
    button.disabled = !this._config.notify_service || this._busy;
    this._result.append(button);
    if (!this._config.notify_service) this._result.append(element("div", "iPhone 알림 서비스는 카드의 notify_service에 설정하세요.", "caption"));
  }

  _drawRoute() {
    this._highlight.replaceChildren();
    const platforms = this._route?.platforms ?? [];
    const points = platforms.map((id) => this._stations.get(id)).filter(Boolean);
    if (points.length > 1) this._highlight.append(svgElement("polyline", { points: points.map((s) => `${s.x},${s.y}`).join(" ") }));
  }

  async _journeyCommand(command) {
    if (this._busy || !this._hass) return;
    try {
      const request = journeyRequest(command, { ...this._selection, notify_service: this._config.notify_service,
        url: this._config.url || `${location.pathname}${location.search}` });
      this._journeyVersion += 1;
      this._busy = true; this._error.textContent = "";
      this._journeyPanel.querySelectorAll("button").forEach((button) => { button.disabled = true; });
      this._journey = await this._hass.callWS(request); this._renderJourney();
    } catch (error) { this._error.textContent = error.message ?? "이동 안내를 변경하지 못했습니다."; }
    finally { this._busy = false; this._renderJourney(); this._renderRoute(); }
  }

  async _refreshStatus() {
    if (!this._hass || !this.isConnected || this._statusLoading) return;
    this._statusLoading = true;
    const version = this._journeyVersion;
    try {
      const journey = await this._hass.callWS(journeyRequest("status"));
      if (version === this._journeyVersion) { this._journey = journey; this._renderJourney(); }
    }
    catch (error) { if (this._journey.active) this._error.textContent = error.message ?? "이동 안내 상태를 확인하지 못했습니다."; }
    finally { this._statusLoading = false; }
  }

  _renderJourney() {
    this._journeyPanel.replaceChildren(); if (!this._journey?.active) return;
    const panel = element("div", null, "journey");
    const leg = this._journey.legs?.[this._journey.leg_index];
    panel.append(element("strong", this._journey.phase === "riding" ? "열차 이동 안내" : "열차 도착 안내"));
    if (leg) panel.append(element("p", `${this._stationName(leg.origin)} → ${this._stationName(leg.destination)} · ${leg.direction || "방향 정보 없음"}`));
    panel.append(element("p", this._journey.message || "실시간 정보를 확인하는 중입니다."));
    const actions = element("div", null, "actions");
    if (this._journey.phase !== "riding") actions.append(this._button("탑승 완료", () => this._journeyCommand("board"), "primary"));
    else actions.append(this._button(this._journey.leg_index < this._journey.legs.length - 1 ? "환승역 도착" : "목적지 도착", () => this._journeyCommand("next"), "primary"));
    actions.append(this._button("안내 종료", () => this._journeyCommand("stop")));
    for (const button of actions.children) button.disabled = this._busy;
    panel.append(actions, element("div", "현재 위치를 자동 감지하지 않습니다. 탑승과 도착을 직접 눌러 주세요.", "caption"));
    this._journeyPanel.append(panel);
  }
}

if (globalThis.customElements && !customElements.get("ha-korea-subway-card")) {
  customElements.define("ha-korea-subway-card", HaKoreaSubwayCard);
  globalThis.customCards ??= [];
  globalThis.customCards.push({ type: "ha-korea-subway-card", name: "HA for Korea 지하철", description: "수도권 노선도와 iPhone 이동 안내" });
}
