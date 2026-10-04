/* Local visual preview only. No Home Assistant connection or real notifications. */
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { resolve } from "node:path";

const FRONTEND = new URL("../custom_components/kepco_on/frontend/", import.meta.url);
const HTML = `<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>지하철 카드 미리보기</title><style>
  body{margin:0;background:#f1f4f8;font:15px system-ui,sans-serif;color:#172332}main{max-width:1120px;margin:24px auto;padding:0 16px}aside{padding:12px 16px;margin-bottom:14px;border-radius:10px;background:#fff2cf;line-height:1.6}ha-korea-subway-card{display:block}ha-card{border-radius:16px}
</style></head><body><main><aside><b>미리보기 · 알림 발송 없음</b><br>실제 수도권 노선도를 사용합니다. 경로와 이동 안내는 화랑대 → 태릉입구 구간의 모의 데이터입니다. Home Assistant나 iPhone에 연결하지 않습니다.</aside><ha-korea-subway-card></ha-korea-subway-card></main><script type="module">
import '/kepco_on/ha-korea-subway-card.js';
const network = await fetch('/kepco_on/subway-network.json').then(r=>r.json());
const card = document.querySelector('ha-korea-subway-card');
card.setConfig({title:'수도권 지하철 · 미리보기',notify_service:'notify.mobile_app_example_iphone',url:'/lovelace/subway'});
let journey={active:false}, route;
window.previewCalls=[];
card.hass={callWS:async(request)=>{
  previewCalls.push(request);
  if(request.type==='kepco_on/subway_route'||request.command==='start'){
    const origin=network.stations.find(s=>s.id===request.origin), destination=network.stations.find(s=>s.id===request.destination);
    if(origin?.name!=='화랑대'||destination?.name!=='태릉입구'||request.via?.length) throw new Error('미리보기 경로는 화랑대 → 태릉입구를 선택해 주세요.');
    const edge=network.edges.find(e=>e.from===origin.id&&e.to===destination.id&&!e.transfer);
    if(!edge) throw new Error('미리보기 경로를 찾지 못했습니다.');
    route={platforms:[origin.id,destination.id],seconds:edge.seconds,transfers:0,estimated:true,legs:[{line:origin.line,origin:origin.name,destination:destination.name,direction:edge.direction,station_ids:[origin.id,destination.id]}]};
    if(request.type==='kepco_on/subway_route')return route;
    journey={active:true,phase:'waiting',leg_index:0,legs:route.legs,message:'미리보기: 응암 방면 열차 3분 후 도착',arrival:'3분 후 도착'};
  }
  if(request.command==='board')journey={...journey,phase:'riding',message:'미리보기: 태릉입구까지 약 2분'};
  if(request.command==='next'||request.command==='stop')journey={active:false};
  return journey;
}};
</script></body></html>`;

export function createPreviewServer() {
  return createServer(async (request, response) => {
    try {
      const pathname = new URL(request.url, "http://127.0.0.1").pathname;
      if (pathname === "/") {
        response.setHeader("Content-Type", "text/html; charset=utf-8"); response.end(HTML);
      } else if (["/kepco_on/ha-korea-subway-card.js", "/kepco_on/subway-network.json"].includes(pathname)) {
        const name = pathname.split("/").at(-1);
        response.setHeader("Content-Type", name.endsWith(".js") ? "text/javascript; charset=utf-8" : "application/json; charset=utf-8");
        response.end(await readFile(new URL(name, FRONTEND)));
      } else { response.writeHead(404); response.end("Not found"); }
    } catch { response.writeHead(500); response.end("Preview unavailable"); }
  });
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const port = Number(process.env.SUBWAY_PREVIEW_PORT || 8124);
  if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error("Invalid preview port");
  createPreviewServer().listen(port, "127.0.0.1", () => console.log(`Subway preview (no notifications): http://127.0.0.1:${port}/`));
}
