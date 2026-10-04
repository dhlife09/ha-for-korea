import assert from "node:assert/strict";
import test from "node:test";
import { formatDuration, routeRequest, journeyRequest, searchStations, networkBounds, labelLayout } from "../custom_components/kepco_on/frontend/ha-korea-subway-card.js";

const selection = { origin: "1006:화랑대", destination: "1006:태릉입구", via: [], preference: "time" };

test("route request preserves station IDs and ordered waypoints", () => {
  assert.deepEqual(routeRequest({ ...selection, via: ["1006:석계", "1001:동대문"] }), {
    type: "kepco_on/subway_route", ...selection, via: ["1006:석계", "1001:동대문"],
  });
  const via = ["1006:석계"];
  routeRequest({ ...selection, via }).via.push("extra");
  assert.deepEqual(via, ["1006:석계"]);
});

test("incomplete, duplicate and oversized routes are rejected before sending", () => {
  for (const value of [null, "", 12, "x".repeat(129)]) assert.throws(() => routeRequest({ ...selection, origin: value }));
  assert.throws(() => routeRequest({ ...selection, destination: selection.origin }));
  assert.throws(() => routeRequest({ ...selection, via: [selection.origin] }));
  assert.throws(() => routeRequest({ ...selection, via: ["a", "a"] }));
  assert.throws(() => routeRequest({ ...selection, via: "wrong" }));
  assert.throws(() => routeRequest({ ...selection, via: ["a", "b", "c", "d", "e", "f"] }));
  assert.throws(() => routeRequest({ ...selection, preference: "unsupported" }));
});

test("start requires a mobile app notification service and internal destination", () => {
  const options = { ...selection, notify_service: "notify.mobile_app_example_iphone", url: "/lovelace/subway" };
  assert.deepEqual(journeyRequest("start", options), { type: "kepco_on/subway_journey", command: "start", ...selection,
    notify_service: options.notify_service, url: options.url });
  for (const url of ["https://example.com", "//example.com", "/\\example.com", "/\n/evil", "javascript:alert(1)", null]) {
    assert.throws(() => journeyRequest("start", { ...options, url }));
  }
  for (const notify_service of ["notify.all", "notify.mobile_app_example;evil", "", null]) {
    assert.throws(() => journeyRequest("start", { ...options, notify_service }));
  }
  for (const command of ["status", "board", "next", "stop"]) {
    assert.deepEqual(journeyRequest(command), { type: "kepco_on/subway_journey", command });
  }
  assert.throws(() => journeyRequest("unknown"));
});

test("search deduplicates interchangeable platforms without merging different stations", () => {
  const stations = [
    { id: "a", name: "서울역", group: "seoul" }, { id: "b", name: "서울역", group: "seoul" },
    { id: "c", name: "서울숲", group: "forest" }, { id: "d", name: "서울역", group: "different" },
  ];
  assert.equal(searchStations(stations, " 서 울 ").length, 3);
  assert.deepEqual(searchStations(stations, "서울역").map((s) => s.id), ["a", "d"]);
  assert.deepEqual(searchStations(stations, ""), []);
  assert.equal(searchStations(stations, "서울", 1).length, 1);
});

test("map bounds include line bends and ignore nonnumeric coordinates", () => {
  assert.deepEqual(networkBounds([{ x: 0, y: 10 }, { x: 10, y: 0 }, { x: null, y: 400 }],
    [{ paths: [[[5, 30], [-10, 5]]] }]), { x: -22, y: -12, width: 44, height: 54 });
  assert.throws(() => networkBounds([], []));
});

test("travel durations round partial minutes without showing invalid values", () => {
  assert.equal(formatDuration(61), "2분");
  assert.equal(formatDuration(3600), "1시간");
  assert.equal(formatDuration(3661), "1시간 2분");
  for (const value of [undefined, -1, NaN, Infinity]) assert.equal(formatDuration(value), "시간 정보 없음");
});

test("station labels respect all map compass positions and original aliases", () => {
  for (const position of ["N", "S"]) assert.equal(labelLayout(position).anchor, "middle");
  for (const position of ["E", "NE", "SE"]) assert.equal(labelLayout(position).anchor, "start");
  for (const position of ["W", "NW", "SW"]) assert.equal(labelLayout(position).anchor, "end");
  assert.ok(labelLayout("N").dy < 0);
  assert.ok(labelLayout("S").dy > 0);
  assert.deepEqual(labelLayout("WS"), labelLayout("SW"));
  assert.deepEqual(labelLayout("wn"), labelLayout("NW"));
  assert.deepEqual(labelLayout("EN"), labelLayout("NE"));
  assert.deepEqual(labelLayout("unknown"), labelLayout("E"));
});
