import assert from "node:assert/strict";
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { chromium } from "playwright";
import { createPreviewServer } from "./subway-card-preview.mjs";

const network = {
  stations: [
    { id: "a", name: "첫역", line: "6", group: "a", x: 10, y: 10 },
    { id: "b", name: "환승역", line: "6", group: "b", x: 40, y: 40 },
    { id: "c", name: "끝역", line: "6", group: "c", x: 70, y: 70 },
    { id: "x", name: "<img src=x onerror=alert(1)>", line: "6", group: "x", x: 90, y: 70 },
  ],
  lines: [{ id: "6", name: "6호선", color: "#cd7c2f", paths: [[[10, 10], [40, 40], [70, 70]]] }],
  source: { name: "시험 자료", url: "javascript:alert(1)" }, updated: "2026-10-05",
};

test("card selects a route and manually controls journey without unsafe markup", async () => {
  const source = await readFile(new URL("../custom_components/kepco_on/frontend/ha-korea-subway-card.js", import.meta.url));
  const server = createServer((request, response) => {
    if (request.url === "/kepco_on/subway-network.json") {
      response.setHeader("Content-Type", "application/json"); response.end(JSON.stringify(network));
    } else if (request.url === "/card.js") {
      response.setHeader("Content-Type", "text/javascript"); response.end(source);
    } else {
      response.setHeader("Content-Type", "text/html; charset=utf-8");
      response.end(`<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width"><body style="margin:0"><ha-korea-subway-card></ha-korea-subway-card><script type="module">
        import '/card.js';
        const card = document.querySelector('ha-korea-subway-card');
        card.setConfig({notify_service:'notify.mobile_app_example_iphone',url:'/lovelace/subway'});
        window.calls = [];
        let journey = {active:false};
        const legs = [{line:'6',origin:'a',destination:'c',direction:'응암 방면',station_ids:['a','b','c']}];
        card.hass = {callWS:async (request) => {
          calls.push(request);
          if(request.type==='kepco_on/subway_route') return {platforms:['a','b','c'],seconds:300,transfers:0,legs,estimated:true};
          if(request.command==='start') journey={active:true,phase:'waiting',leg_index:0,legs,message:'3분 후 도착'};
          if(request.command==='board') journey={...journey,phase:'riding',message:'목적지까지 약 5분'};
          if(request.command==='stop'||request.command==='next') journey={active:false};
          return journey;
        }};
      </script>`);
    }
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  let browser;
  try {
    browser = await chromium.launch({ channel: process.env.PLAYWRIGHT_CHANNEL || "chrome", headless: true });
    const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    const card = page.locator("ha-korea-subway-card");
    await card.getByRole("button", { name: "첫역, 6호선", exact: true }).click();
    await card.getByRole("button", { name: "출발", exact: true }).click();
    await card.getByRole("searchbox", { name: "지하철역 검색" }).fill("끝역");
    await card.getByRole("button", { name: "끝역 · 6호선", exact: true }).click();
    await card.getByRole("button", { name: "도착", exact: true }).click();
    await card.getByRole("button", { name: "경로 검색", exact: true }).click();
    await card.getByRole("button", { name: "iPhone 이동 안내 시작", exact: true }).click();
    await card.getByText("3분 후 도착", { exact: true }).waitFor({ timeout: 3000 });
    assert.equal(await card.getByText("3분 후 도착", { exact: true }).count(), 1);
    await card.getByRole("button", { name: "탑승 완료", exact: true }).click();
    assert.equal(await card.getByText("목적지까지 약 5분", { exact: true }).count(), 1);
    await card.getByRole("button", { name: "목적지 도착", exact: true }).click();
    assert.equal(await card.getByRole("button", { name: "안내 종료", exact: true }).count(), 0);
    const calls = await page.evaluate(() => window.calls);
    assert.deepEqual(calls.find((call) => call.type === "kepco_on/subway_route"), {
      type: "kepco_on/subway_route", origin: "a", destination: "c", via: [], preference: "time",
    });
    assert.deepEqual(calls.filter((call) => call.command && call.command !== "status").map((call) => call.command), ["start", "board", "next"]);
    assert.equal(await card.locator("img").count(), 0);
    assert.equal(await card.locator('a[href^="javascript:"]').count(), 0);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
    assert.deepEqual(errors, []);
  } finally { await browser?.close(); await new Promise((resolve) => server.close(resolve)); }
});

test("bundled 799-platform map selects Hwarangdae toward Taereung and shows attribution", async () => {
  const actualNetwork = JSON.parse(await readFile(new URL("../custom_components/kepco_on/frontend/subway-network.json", import.meta.url), "utf8"));
  const server = createPreviewServer();
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  let browser;
  try {
    browser = await chromium.launch({ channel: process.env.PLAYWRIGHT_CHANNEL || "chrome", headless: true });
    const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    const card = page.locator("ha-korea-subway-card");
    await card.getByRole("link", { name: "출처 보기" }).waitFor();
    assert.equal(await card.locator(".station").count(), actualNetwork.stations.length);
    assert.ok(actualNetwork.stations.length > 750);
    assert.equal(await card.getByRole("link", { name: "출처 보기" }).getAttribute("href"), actualNetwork.source.topology_page);
    const search = card.getByRole("searchbox", { name: "지하철역 검색" });
    await search.fill("화랑대");
    await card.getByRole("button", { name: "화랑대 · 6호선", exact: true }).click();
    await card.getByRole("button", { name: "출발", exact: true }).click();
    await search.fill("태릉입구");
    await card.locator(".search-results button").click();
    await card.getByRole("button", { name: "도착", exact: true }).click();
    await card.getByRole("button", { name: "경로 검색", exact: true }).click();
    await card.getByText("상행 · 1개 역 이동", { exact: true }).waitFor();
    await card.getByRole("button", { name: "iPhone 이동 안내 시작", exact: true }).click();
    await card.getByText("미리보기: 응암 방면 열차 3분 후 도착", { exact: true }).waitFor();
    await card.getByRole("button", { name: "안내 종료", exact: true }).click();
    const calls = await page.evaluate(() => window.previewCalls);
    const request = calls.find((call) => call.type === "kepco_on/subway_route");
    assert.equal(actualNetwork.stations.find((station) => station.id === request.origin).name, "화랑대");
    assert.equal(actualNetwork.stations.find((station) => station.id === request.destination).name, "태릉입구");
    const before = await card.locator("svg").getAttribute("viewBox");
    await card.getByRole("button", { name: "노선도 확대", exact: true }).click();
    assert.notEqual(await card.locator("svg").getAttribute("viewBox"), before);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
    assert.deepEqual(errors, []);
  } finally { await browser?.close(); await new Promise((resolve) => server.close(resolve)); }
});
