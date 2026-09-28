// DOM contract test for the complete generated offline script. No browser or network.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync(process.argv[2],'utf8');
class Element {
  constructor(id=''){this.id=id;this.value='';this.checked=false;this.innerHTML='';this.textContent='';this.children=[];this.listeners={};this.attributes={};this.dataset={};const active=new Set();this.classList={add:x=>active.add(x),remove:x=>active.delete(x),contains:x=>active.has(x)}}
  appendChild(child){this.children.push(child)}
  addEventListener(kind,callback){(this.listeners[kind]??=[]).push(callback)}
  setAttribute(key,value){this.attributes[key]=value}
  click(){this.onclick?.();for(const f of this.listeners.click||[])f({target:this})}
  scrollIntoView(){}
}
const elements=new Map();
for(const match of html.matchAll(/id="([^"]+)"[^>]*>/g)){const e=new Element(match[1]);e.checked=/\bchecked\b/.test(match[0]);elements.set(e.id,e)}
elements.get('portfolio-data').textContent=html.match(/<script id="portfolio-data" type="application\/json">([\s\S]*?)<\/script>/)[1];
const nav=[...html.matchAll(/data-tab="([^"]+)"/g)].map(m=>{const e=new Element();e.dataset.tab=m[1];return e});
const document={getElementById:id=>{assert.ok(elements.has(id),`missing element ${id}`);return elements.get(id)},createElement:()=>new Element(),listeners:{},addEventListener(kind,callback){(this.listeners[kind]??=[]).push(callback)},querySelectorAll:selector=>selector==='nav button'?nav:[...nav,...elements.values()],querySelector:selector=>nav.find(e=>selector.includes(`"${e.dataset.tab}"`))};
const sandbox=vm.createContext({document,console,setTimeout,clearTimeout,assert,performance});
const script=[...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].at(-1)[1];
vm.runInContext(script,sandbox,{timeout:10000});
vm.runInContext(`
assert.equal(displayedEdges.length,2);
assert.equal(displayedNodes.filter(n=>n.kind==='application').length,2);
assert(!displayedNodes.some(n=>n.kind==='runtime'));
networkApp.value='1';assert.equal(networkSelection().edges.length,2);
sharedToggle.checked=false;assert.equal(networkSelection().edges.length,3);
runtimeToggle.checked=true;assert.equal(networkSelection().edges.length,5);
const resource=net.nodes.find(n=>n.kind==='external_data');showResource(resource.id);
assert(document.getElementById('network-detail').innerHTML.includes('SQL line 1'));
assert(document.getElementById('network-detail').innerHTML.includes('READ'));
assert(document.getElementById('network-detail').innerHTML.includes('UPDATE'));
selectNetworkNode('app:1');assert(drawer.classList.contains('open'));
capabilityFilter.value='request intake';rows();
assert(document.getElementById('apps').innerHTML.includes('Request Tracker'));
assert(!document.getElementById('apps').innerHTML.includes('Other App'));
assert(capabilityLabels('1').includes('Record reconciliation'));
const oldZoom=zoom;zoomAt(1.3);assert(zoom>oldZoom);
assert(scene.attributes.transform.includes('scale('));
assert(document.getElementById('reuse-summary').innerHTML.includes('1 candidates'));
const many=Array.from({length:1000},(_,i)=>({id:'n'+i,kind:'application'}));
const links=many.slice(1).map(n=>({source:'n0',target:n.id}));
const layout=networkLayout(many,links);assert.equal(layout.size,1000);
assert([...layout.values()].every(p=>Number.isFinite(p.x)&&Number.isFinite(p.y)));
`,sandbox,{timeout:15000});
console.log('Offline report initialization, filters, drill-downs, zoom, and 1,000-node layout passed.');
