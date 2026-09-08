"""A standalone reader with no remote resources or executable note content."""

import base64
import hashlib
import html
import json

CSS = """
:root{font-family:'Microsoft YaHei',system-ui,sans-serif;color:#273142;background:#f3f4f8;color-scheme:light}
*{box-sizing:border-box}body{margin:0}header{position:sticky;top:0;z-index:5;background:#ffffffed;padding:18px 28px;border-bottom:1px solid #e8eaf0;display:flex;gap:14px;align-items:center;flex-wrap:wrap}
h1{font-size:20px;margin:0 auto 0 0}input,button{font:inherit;border:1px solid #dfe3eb;border-radius:10px;padding:9px 12px;background:white;color:inherit}input[type=search]{min-width:240px;flex:1}button{cursor:pointer}button:disabled{opacity:.4;cursor:default}
.layout{display:grid;grid-template-columns:285px minmax(0,1fr);max-width:1400px;margin:auto}nav{position:sticky;top:91px;max-height:calc(100vh - 125px);overflow:auto;padding:16px}nav button{display:block;width:100%;text-align:left;margin-bottom:8px;padding:13px}nav button.active{border-color:#8173ec;background:#f0edff}nav small{display:block;color:#7c8495;margin-top:6px;line-height:1.5}
main{padding:24px;min-width:0}article{background:white;border:1px solid #e8eaf0;border-radius:18px;padding:32px;line-height:1.85;overflow-wrap:anywhere;min-height:50vh}article h2{margin-top:0;font-size:26px}.meta{font-size:12px;color:#7c8495;margin-bottom:24px}.warning{color:#9d5a1a;background:#fff4df;padding:10px;border-radius:8px}
img,video{max-width:100%;height:auto;border-radius:8px}audio{max-width:100%}figure{margin:18px 0}figcaption{font-size:12px;color:#7c8495}blockquote{border-left:3px solid #9a8bf3;padding-left:18px;margin-left:0;color:#626b7c}pre{background:#f5f6fa;padding:16px;overflow:auto;white-space:pre-wrap}code{font-family:Consolas,monospace}table{border-collapse:collapse;max-width:100%;display:block;overflow:auto}td{border:1px solid #dfe3eb;padding:8px 12px}mark{background:#fff0a1;color:inherit}mark.hit{background:#ffe287;outline:1px solid #efbf40}a{color:#6556cc}hr{border:0;border-top:1px solid #e3e6ee;margin:24px 0}
.pager{display:flex;align-items:center;gap:12px;margin:20px 0;flex-wrap:wrap}.pager input{flex:1;min-width:100px}#empty{padding:70px 20px;text-align:center;color:#788194}#count{font-size:12px;color:#788194}.summary{font-size:12px}.single{max-width:900px;margin:25px auto}
@media(max-width:760px){header{padding:14px}.layout{grid-template-columns:1fr}nav{position:static;max-height:240px;display:flex;gap:8px}nav button{min-width:200px;width:200px}main{padding:12px}article{padding:20px}}
@media print{header,nav,.pager{display:none}.layout{display:block}main{padding:0}article{border:0}}
"""

SCRIPT = """
(() => {
 'use strict';
 const notes = JSON.parse(document.getElementById('note-data').textContent);
 const query=document.getElementById('query'), month=document.getElementById('month');
 const nav=document.getElementById('notes'), article=document.getElementById('content');
 const slider=document.getElementById('slider'), counter=document.getElementById('page');
 const previous=document.getElementById('previous'), next=document.getElementById('next');
 let matching=notes.map((_,i)=>i), index=0;
 const terms=()=>query.value.trim().toLocaleLowerCase().split(/\\s+/).filter(Boolean);
 function highlight(root, words) {
   if(!words.length)return;
   const walker=document.createTreeWalker(root,NodeFilter.SHOW_TEXT), nodes=[];
   while(walker.nextNode())nodes.push(walker.currentNode);
   for(const node of nodes){
     if(node.parentElement.closest('script,style'))continue;
     const text=node.nodeValue, lower=text.toLocaleLowerCase(), ranges=[];
     for(const word of words){let at=0;while((at=lower.indexOf(word,at))!==-1){ranges.push([at,at+word.length]);at+=word.length;}}
     ranges.sort((a,b)=>a[0]-b[0]);const merged=[];
     for(const r of ranges){const last=merged.at(-1);if(last&&r[0]<=last[1])last[1]=Math.max(last[1],r[1]);else merged.push([...r]);}
     if(!merged.length)continue;
     const fragment=document.createDocumentFragment();let cursor=0;
     for(const [a,b] of merged){fragment.append(document.createTextNode(text.slice(cursor,a)));const mark=document.createElement('mark');mark.className='hit';mark.textContent=text.slice(a,b);fragment.append(mark);cursor=b;}
     fragment.append(document.createTextNode(text.slice(cursor)));node.replaceWith(fragment);
   }
 }
 function display(){
   nav.replaceChildren();
   matching.forEach((original,j)=>{const note=notes[original],button=document.createElement('button');button.className=j===index?'active':'';button.setAttribute('aria-current',j===index?'true':'false');button.append(document.createTextNode(note.title));const small=document.createElement('small');small.textContent=note.summary;button.append(small);button.onclick=()=>{index=j;display();};nav.append(button);});
   document.getElementById('count').textContent=`匹配 ${matching.length} / 共 ${notes.length} 条`;
   previous.disabled=index<=0;next.disabled=index>=matching.length-1;slider.max=String(Math.max(0,matching.length-1));slider.value=String(index);slider.disabled=matching.length===0;
   counter.textContent=matching.length?`${index+1} / ${matching.length}`:'0 / 0';
   if(!matching.length){article.innerHTML='<div id="empty">没有符合条件的笔记</div>';return;}
   // html is generated solely by the typed, escaping Python renderer, never raw provider HTML.
   article.innerHTML=notes[matching[index]].html;
   highlight(article,terms());
   nav.querySelector('.active')?.scrollIntoView({block:'nearest',inline:'nearest'});
 }
 function filter(){const words=terms();matching=notes.map((_,i)=>i).filter(i=>{const n=notes[i],hay=(n.title+' '+n.text+' '+n.created).toLocaleLowerCase();return(!month.value||n.created.startsWith(month.value))&&words.every(w=>hay.includes(w));});index=0;display();}
 query.addEventListener('input',filter);month.addEventListener('input',filter);
 previous.onclick=()=>{if(index>0){index--;display();}};next.onclick=()=>{if(index<matching.length-1){index++;display();}};
 slider.oninput=()=>{index=Number(slider.value);display();};
 query.addEventListener('keydown',e=>{if(e.key==='Enter'&&matching.length){e.preventDefault();index=(index+(e.shiftKey?-1:1)+matching.length)%matching.length;display();}});
 nav.addEventListener('wheel',e=>{if(!matching.length)return;e.preventDefault();index=Math.max(0,Math.min(matching.length-1,index+(e.deltaY>0?1:-1)));display();},{passive:false});
 display();
})();
"""


def reader_html(title: str, notes: list[dict], single: bool = False) -> str:
    safe_title = html.escape(title)
    script_hash = base64.b64encode(hashlib.sha256(SCRIPT.encode()).digest()).decode()
    csp = f"default-src 'none'; img-src 'self' data:; media-src 'self'; style-src 'unsafe-inline'; script-src 'sha256-{script_hash}'; base-uri 'none'; form-action 'none'"
    head = f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="{csp}"><title>{safe_title}</title><style>{CSS}</style></head>'
    if single:
        return (
            head
            + '<body><main class="single"><article>'
            + notes[0]["html"]
            + "</article></main></body></html>"
        )
    data = (
        json.dumps(notes, ensure_ascii=False)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )
    return (
        head
        + f"""<body><header><h1>{safe_title}</h1><input id="query" type="search" placeholder="搜索标题或内容，多词用空格分隔" aria-label="搜索笔记"><input id="month" type="month" aria-label="创建月份"><span id="count"></span></header>
<div class="layout"><nav id="notes" aria-label="笔记目录"></nav><main><article id="content"></article><div class="pager"><button id="previous">上一条</button><input type="range" min="0" id="slider" aria-label="跳转笔记"><span id="page"></span><button id="next">下一条</button></div></main></div>
<script id="note-data" type="application/json">{data}</script><script>{SCRIPT}</script></body></html>"""
    )
