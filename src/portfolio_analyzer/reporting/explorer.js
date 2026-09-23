// Embedded in the self-contained report: no CDN, network requests, or executable source data.
const groups=D.capability_groups||[];
function capabilityLabels(id){return groups.filter(g=>g.application_ids.includes(id)).map(g=>g.label)}
function matchesCapability(id){const key=document.getElementById('capability-filter').value;return !key||groups.some(g=>g.key===key&&g.application_ids.includes(id))}
const capabilityFilter=document.getElementById('capability-filter');
for(const g of groups){const o=document.createElement('option');o.value=g.key;o.textContent=g.label;capabilityFilter.appendChild(o)}
capabilityFilter.addEventListener('change',()=>rows(document.getElementById('search').value));
document.getElementById('capability-groups').innerHTML=groups.map(g=>`<article class="component"><button class="link" data-capability="${esc(g.key)}">${esc(g.label)}</button><p>${g.application_ids.length} application${g.application_ids.length===1?'':'s'} · interpretation</p><details><summary>Supporting interpretations</summary>${g.interpretations.map(i=>`<div class="finding">${appLink(i.app_id)}<p>${esc(i.description)}</p><small>${esc(i.method.replaceAll('_',' '))} · ${esc(i.confidence)} confidence · ${esc(i.review_status)}<br>Evidence: ${esc(i.evidence_ids.join(', ')||'none')}<br>Owner claims: ${esc(i.claim_ids.join(', ')||'none')}</small></div>`).join('')}</details></article>`).join('')||'<p class="muted">No supported business capability labels are available. Technical roles below describe how the applications work. Enable local model profiles with semantic-model-select to propose more specific capabilities from the saved evidence, then review them against owner knowledge.</p>';
document.addEventListener('click',e=>{const key=e.target.closest('[data-capability]')?.dataset.capability;if(key!==undefined){capabilityFilter.value=key;document.getElementById('search').value='';rows();document.querySelector('[data-tab="portfolio"]').click()}});
const reuseThemes=D.themes.filter(t=>t.affected_tool_ids.length>1);
const reused=new Set(reuseThemes.flatMap(t=>t.affected_tool_ids));
document.getElementById('reuse-summary').innerHTML=`<p><strong>${reuseThemes.length} candidates</strong> involving ${reused.size} applications. ${D.applications.length-reused.size} applications have no supported shared group. Matching role labels alone do not justify consolidation.</p>`;
function renderReuse(q=''){q=q.toLowerCase();document.getElementById('reuse-candidates').innerHTML=themeCards(reuseThemes.filter(t=>(JSON.stringify(t)+' '+t.affected_tool_ids.map(id=>D.names[id]).join(' ')).toLowerCase().includes(q)))}
renderReuse();document.getElementById('reuse-search').addEventListener('input',e=>renderReuse(e.target.value));
document.getElementById('reuse-pairs').innerHTML=D.edges.filter(e=>e.source_tool_id!==e.target_tool_id).map(e=>`<article class="panel">${appLink(e.source_tool_id)} ↔ ${appLink(e.target_tool_id)}<p>Weighted similarity: ${(100*e.overall_similarity).toFixed(0)}%</p><ul>${Object.entries(e.shared_features).filter(([k,v])=>v.length).map(([k,v])=>`<li>${esc(k.replaceAll('_',' '))}: ${esc(v.join(', '))}</li>`).join('')}</ul><p class="muted">Compare the cited application profiles and owner responsibilities before proposing shared implementation.</p></article>`).join('')||'<p class="muted">No qualifying application pairs were found.</p>';

const net=D.network, nodeById=new Map(net.nodes.map(n=>[n.id,n]));
const networkApp=document.getElementById('network-app');
D.applications.slice().sort((a,b)=>a.name.localeCompare(b.name)).forEach(a=>{const o=document.createElement('option');o.value=a.id;o.textContent=a.name;networkApp.appendChild(o)});
const canvas=document.getElementById('network-canvas'),scene=document.getElementById('network-scene');
const sharedToggle=document.getElementById('network-shared'),runtimeToggle=document.getElementById('network-runtime');
const hiddenKinds=new Set(['runtime','analysis_artifact']);
const sharedKinds=new Set(['external_data','shared_file','endpoint']);
let displayedNodes=[],displayedEdges=[],positions=new Map(),zoom=1,pan={x:0,y:0},drag=null,dragMoved=false;
function networkSelection(){
  const focus=networkApp.value,q=document.getElementById('network-search').value.toLowerCase();
  // Focus shows the selected application's resources AND their other consumers.
  const focusedResources=new Set(net.edges.filter(e=>!focus||e.app_id===focus).map(e=>e.target));
  const resources=new Set(net.nodes.filter(n=>n.kind!=='application'&&focusedResources.has(n.id)&&
    (runtimeToggle.checked||!hiddenKinds.has(n.kind))&&(!sharedToggle.checked||n.consumer_count>1)&&
    (!q||n.label.toLowerCase().includes(q)||n.consumer_ids.some(id=>(D.names[id]||id).toLowerCase().includes(q)))).map(n=>n.id));
  const edges=net.edges.filter(e=>resources.has(e.target));
  const ids=new Set(edges.flatMap(e=>[e.source,e.target]));
  if(focus)ids.add('app:'+focus);
  return {nodes:net.nodes.filter(n=>ids.has(n.id)),edges};
}
function networkLayout(nodes,edges){
  const out=new Map(),count=nodes.length;
  const radius=Math.max(180,Math.sqrt(count)*36);
  nodes.forEach((n,i)=>{const angle=2*Math.PI*i/Math.max(count,1);out.set(n.id,{x:560+Math.cos(angle)*radius,y:340+Math.sin(angle)*radius})});
  // Bounded force layout. Larger networks use a spatial grid for local repulsion.
  for(let step=0;step<100;step++){
    const delta=new Map(nodes.map(n=>[n.id,{x:0,y:0}])),grid=new Map();
    for(const n of nodes){const p=out.get(n.id),key=`${Math.floor(p.x/100)},${Math.floor(p.y/100)}`;if(!grid.has(key))grid.set(key,[]);grid.get(key).push(n)}
    for(const n of nodes){const p=out.get(n.id),d=delta.get(n.id),gx=Math.floor(p.x/100),gy=Math.floor(p.y/100);
      for(let x=gx-1;x<=gx+1;x++)for(let y=gy-1;y<=gy+1;y++)for(const other of grid.get(`${x},${y}`)||[]){if(n.id===other.id)continue;const op=out.get(other.id),dx=p.x-op.x,dy=p.y-op.y,dist=Math.max(5,Math.hypot(dx,dy)),f=1500/(dist*dist);d.x+=dx/dist*f;d.y+=dy/dist*f}
      d.x+=(560-p.x)*.002;d.y+=(340-p.y)*.002;
    }
    for(const e of edges){const a=out.get(e.source),b=out.get(e.target),dx=b.x-a.x,dy=b.y-a.y,dist=Math.max(1,Math.hypot(dx,dy)),f=(dist-150)*.015;delta.get(e.source).x+=dx/dist*f;delta.get(e.source).y+=dy/dist*f;delta.get(e.target).x-=dx/dist*f;delta.get(e.target).y-=dy/dist*f}
    for(const n of nodes){const p=out.get(n.id),d=delta.get(n.id);p.x+=Math.max(-15,Math.min(15,d.x));p.y+=Math.max(-15,Math.min(15,d.y))}
  }
  return out;
}
function applyView(){scene.setAttribute('transform',`translate(${pan.x} ${pan.y}) scale(${zoom})`)}
function fitNetwork(){if(!positions.size){zoom=1;pan={x:0,y:0};applyView();return}const ps=[...positions.values()],xs=ps.map(p=>p.x),ys=ps.map(p=>p.y);const left=Math.min(...xs)-120,right=Math.max(...xs)+120,top=Math.min(...ys)-45,bottom=Math.max(...ys)+45;zoom=Math.min(1.8,1040/Math.max(1,right-left),600/Math.max(1,bottom-top));pan={x:560-(left+right)/2*zoom,y:340-(top+bottom)/2*zoom};applyView()}
function drawNetwork(){
  scene.innerHTML=displayedEdges.map((e,i)=>{const a=positions.get(e.source),b=positions.get(e.target),length=Math.max(1,Math.hypot(b.x-a.x,b.y-a.y)),end={x:b.x-(b.x-a.x)/length*16,y:b.y-(b.y-a.y)/length*16};return `<g><line class="network-link" data-net-edge="${i}" x1="${a.x}" y1="${a.y}" x2="${end.x}" y2="${end.y}" marker-end="url(#network-arrow)"><title>${esc(D.names[e.app_id])}: ${esc(e.operations.join(', '))} · ${esc(nodeById.get(e.target).label)}</title></line>${displayedEdges.length<=35?`<text class="network-edge-label" x="${(a.x+b.x)/2}" y="${(a.y+b.y)/2-4}" pointer-events="none">${esc(e.operations.join(', '))}</text>`:''}</g>`}).join('')+displayedNodes.map(n=>{const p=positions.get(n.id),app=n.kind==='application';return `<g class="network-node" data-net-node="${esc(n.id)}" transform="translate(${p.x} ${p.y})" tabindex="0" role="button" aria-label="${esc(n.label)}"><title>${esc(n.label)} · ${esc(n.kind.replaceAll('_',' '))}</title>${app?'<circle r="13" fill="#2f6f8f"/>':`<rect x="-11" y="-11" width="22" height="22" rx="3" fill="${sharedKinds.has(n.kind)?'#d4a24a':'#a6b2b9'}"/>`}<text x="17" y="4">${esc(n.label.length>32?n.label.slice(0,29)+'…':n.label)}</text></g>`}).join('');applyView();
}
function renderNetwork(){const selection=networkSelection();displayedNodes=selection.nodes;displayedEdges=selection.edges;positions=networkLayout(displayedNodes,displayedEdges);drawNetwork();fitNetwork();const appCount=displayedNodes.filter(n=>n.kind==='application').length;document.getElementById('network-count').textContent=`Showing ${appCount} applications, ${displayedNodes.length-appCount} resources and ${displayedEdges.length} aggregated connections. Complete dataset: ${net.raw_dependency_count} dependency records and ${net.datasource_count} datasource records. No first-N edge truncation.`;if(!displayedEdges.length)document.getElementById('network-count').textContent+=' No connections match these filters. Select an application and clear “Shared resources only” to inspect its local references.'}
function showResource(id,appId=null){const n=nodeById.get(id);if(!n)return;const edges=net.edges.filter(e=>e.target===id&&(!appId||e.app_id===appId));const allEdges=net.edges.filter(e=>e.target===id);const detailEl=document.getElementById('network-detail');detailEl.innerHTML=`<h3>${esc(n.label)}</h3><p>${esc(n.kind.replaceAll('_',' '))} · ${n.consumer_count} applications</p><p>${n.consumer_ids.map(appLink).join(' · ')}</p><p class="muted">An arrow means the application references this resource. Read/write direction is given by the recorded operation; UNKNOWN does not establish a read or write. Source coverage can be incomplete.</p><p><strong>Observed operations:</strong> ${allEdges.map(e=>`${esc(D.names[e.app_id])}: ${esc(e.operations.join(', '))}`).join(' · ')}</p><button data-net-focus="${esc(id)}">Focus this resource and its applications</button><div class="evidence-scroll"><table><thead><tr><th>Application / operation</th><th>Object and source location</th><th>Target / observation</th></tr></thead><tbody>${edges.flatMap(e=>(e.evidence.length?e.evidence:[{object:'Not recorded',artifact:'',location:'',evidence_id:'',target:n.label,observation:'No detailed evidence location was captured.'}]).map(r=>`<tr><td>${appLink(e.app_id)}<br>${esc(e.operations.join(', '))}</td><td>${esc(r.object_type)}: ${esc(r.object)}<br>${esc(r.artifact)}<br>${esc(r.location)}<br><small>${esc(r.evidence_id)}</small></td><td>${esc(r.target)}<br>${esc(r.observation)}</td></tr>`)).join('')}</tbody></table></div>`}
function selectNetworkNode(id){const n=nodeById.get(id);if(n?.kind==='application')openApp(n.app_id);else showResource(id)}
canvas.addEventListener('click',e=>{if(dragMoved){dragMoved=false;return}const id=e.target.closest('[data-net-node]')?.dataset.netNode,index=e.target.closest('[data-net-edge]')?.dataset.netEdge;if(id)selectNetworkNode(id);else if(index!==undefined){const edge=displayedEdges[Number(index)];showResource(edge.target,edge.app_id)}});
canvas.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){const id=e.target.closest('[data-net-node]')?.dataset.netNode;if(id){e.preventDefault();selectNetworkNode(id)}}});
function canvasPoint(e){const p=canvas.createSVGPoint();p.x=e.clientX;p.y=e.clientY;return p.matrixTransform(canvas.getScreenCTM().inverse())}
canvas.addEventListener('pointerdown',e=>{if(e.button!==0)return;const p=canvasPoint(e);drag={node:e.target.closest('[data-net-node]')?.dataset.netNode,start:p,last:p};dragMoved=false});
canvas.addEventListener('pointermove',e=>{if(!drag)return;const p=canvasPoint(e);if(Math.hypot(p.x-drag.start.x,p.y-drag.start.y)>3)dragMoved=true;if(!dragMoved)return;canvas.setPointerCapture(e.pointerId);if(drag.node){const n=positions.get(drag.node);n.x+=(p.x-drag.last.x)/zoom;n.y+=(p.y-drag.last.y)/zoom;drawNetwork()}else{pan.x+=p.x-drag.last.x;pan.y+=p.y-drag.last.y;applyView()}drag.last=p});
canvas.addEventListener('pointerup',e=>{drag=null;if(canvas.hasPointerCapture(e.pointerId))canvas.releasePointerCapture(e.pointerId)});canvas.addEventListener('pointercancel',()=>{drag=null});
function zoomAt(factor,p={x:560,y:340}){const old=zoom;zoom=Math.max(.03,Math.min(8,zoom*factor));pan={x:p.x-(p.x-pan.x)*zoom/old,y:p.y-(p.y-pan.y)*zoom/old};applyView()}
canvas.addEventListener('wheel',e=>{e.preventDefault();zoomAt(e.deltaY<0?1.15:1/1.15,canvasPoint(e))},{passive:false});
document.getElementById('network-in').onclick=()=>zoomAt(1.3);document.getElementById('network-out').onclick=()=>zoomAt(1/1.3);document.getElementById('network-reset').onclick=fitNetwork;
[networkApp,sharedToggle,runtimeToggle].forEach(el=>el.addEventListener('change',()=>{document.getElementById('network-detail').textContent='Select a resource or connection to inspect its evidence.';renderNetwork()}));
let networkSearchTimer;document.getElementById('network-search').addEventListener('input',()=>{clearTimeout(networkSearchTimer);networkSearchTimer=setTimeout(renderNetwork,180)});
document.getElementById('network-candidates').innerHTML=net.candidates.map(c=>`<article class="component"><button class="link" data-net-focus="${esc(c.resource_id)}">${esc(c.label)}</button><p>${esc(c.reason)}</p><p>${c.application_ids.map(appLink).join(' · ')}</p><p>${esc(c.next_step)}</p></article>`).join('')||'<p class="muted">No confirmed shared data, file or endpoint dependencies were found. Unresolved names and common runtime libraries do not create service candidates.</p>';
document.addEventListener('click',e=>{const id=e.target.closest('[data-net-focus]')?.dataset.netFocus;if(!id)return;displayedEdges=net.edges.filter(e=>e.target===id);const ids=new Set(displayedEdges.flatMap(e=>[e.source,e.target]));displayedNodes=net.nodes.filter(n=>ids.has(n.id));positions=networkLayout(displayedNodes,displayedEdges);drawNetwork();fitNetwork();showResource(id);document.getElementById('network-count').textContent=`Focused resource: ${nodeById.get(id).label}. ${displayedEdges.length} application connections. Change a filter to return to the wider network.`;canvas.scrollIntoView({block:'center'})});
renderNetwork();
