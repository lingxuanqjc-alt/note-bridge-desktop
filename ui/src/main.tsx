import {StrictMode, useCallback, useEffect, useRef, useState} from 'react';
import {createRoot} from 'react-dom/client';
import {invoke} from './api';
import {formats, platforms, type Platform} from './platforms';
import type {AppState, Format, MigrationPreview, MigrationReview, PlatformId, PlatformState, TaskReport} from './types';
import './style.css';

type IconName = 'export'|'transfer'|'download'|'folder'|'package'|'book'|'close'|'minus'|'square'|'arrow'|'check'|'info';
function Icon({name, size=17}: {name:IconName;size?:number}) {
  const paths:Record<IconName,React.ReactNode>={
    export:<><path d="M12 15V3m-4 4 4-4 4 4"/><path d="M5 12v7a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-7"/></>,
    download:<><path d="M12 3v12m-4-4 4 4 4-4"/><path d="M5 14v5a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-5"/></>,
    transfer:<><path d="M4 7h15m-4-4 4 4-4 4M20 17H5m4-4-4 4 4 4"/></>,
    folder:<path d="M3 6a2 2 0 0 1 2-2h4l2 3h8a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z"/>,
    package:<><path d="m12 3 9 5-9 5-9-5 9-5Zm-9 5v10l9 5 9-5V8M12 13v10M7.5 5.5l9 5"/></>,
    book:<><path d="M3 4h7l2 2 2-2h7v16h-7l-2 2-2-2H3ZM12 6v16"/></>,
    close:<path d="m6 6 12 12M6 18 18 6"/>,minus:<path d="M5 12h14"/>,square:<rect x="5" y="5" width="14" height="14" rx="2"/>,
    arrow:<path d="M4 12h16m-6-6 6 6-6 6"/>,check:<path d="m4 12 5 5L20 6"/>,
    info:<><circle cx="12" cy="12" r="9"/><path d="M12 11v6m0-10v1"/></>
  };
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name]}</svg>;
}
function Brand(){return <div className="brand-mark" aria-hidden="true"><svg width="26" height="26" viewBox="0 0 30 30" fill="none"><rect x="3" y="3" width="17" height="21" rx="4" fill="#eeeaff" stroke="#a99beb"/><rect x="10" y="7" width="17" height="21" rx="4" fill="white" stroke="#8878df"/><path d="M14 13h9m-3-3 3 3-3 3M23 22h-9m3-3-3 3 3 3" stroke="#7764db" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"/></svg></div>}
const initial:AppState={name:'笔记互迁',version:'1.0.0',platforms:platforms.map(p=>({id:p.id,logged_in:false,logging_in:false,count:0,complete:false,capability:'pending'})),current_task:null,has_export:false,release_channel:'stable',release_ready:false};
const terminal = new Set(['succeeded','partial','failed','cancelled','needs_review']);
const statusLabels:Record<string,string>={queued:'准备开始',running:'正在处理',succeeded:'已完成',partial:'部分完成',failed:'操作失败',cancelled:'已取消',needs_review:'需要核对'};

function Modal({title,children,onClose,wide=false,platform}:{title:string;children:React.ReactNode;onClose:()=>void;wide?:boolean;platform?:Platform}){
  const dialog=useRef<HTMLDialogElement>(null);
  useEffect(()=>{dialog.current?.showModal();return()=>dialog.current?.close()},[]);
  const closeButton=<button className={`icon-button${platform?' login-close':''}`} aria-label="关闭弹窗" onClick={onClose}><Icon name="close"/></button>;
  return <dialog ref={dialog} aria-label={title} className={`modal ${wide?'wide':''} ${platform?'login-modal':''}`} onCancel={e=>{e.preventDefault();onClose()}} onClick={e=>{if(e.target===dialog.current)onClose()}}>
    {platform&&closeButton}
    <div className="modal-heading">{platform?<span className="login-platform-mark" style={{color:platform.color}} aria-hidden="true">{platform.glyph}</span>:<span className="modal-icon"><Icon name="book"/></span>}<div><h2>{title}</h2>{!platform&&<small>NOTE BRIDGE</small>}</div>{!platform&&closeButton}</div>
    <div className="modal-body">{children}</div>
  </dialog>
}

function PlatformCard({platform,state,selected,onSelect,onLogin,compact,disabled}: {platform:Platform;state:PlatformState;selected:boolean;onSelect:()=>void;onLogin:()=>void;compact?:boolean;disabled?:boolean}){
  return <div className={`platform-card ${selected?'selected':''} ${compact?'compact':''}`}>
    <button className="card-select" aria-label={`选择${platform.name}`} aria-pressed={selected} disabled={disabled} onClick={onSelect}>
      <span className={`platform-logo logo-${platform.id}`} style={{color:platform.color}}>{platform.glyph}</span><span className="platform-name">{platform.name}</span>
      {selected&&<span className="selection-check"><Icon name="check" size={10}/></span>}
    </button>
    <button className={`login ${state.logged_in?'connected':''}`} onClick={onLogin} disabled={disabled||state.logging_in} aria-label={`${platform.name}${state.logged_in?'已登录':'登录'}`}>
      {state.logging_in?<span className="spinner small"/>:state.logged_in?<><Icon name="check" size={12}/>已登录</>:'登录'}
    </button>
    {!compact&&platform.hint&&<span className="platform-hint">{platform.hint}</span>}
  </div>
}

function MigrationPicker({preview,onClose,onConfirm}:{preview:MigrationPreview;onClose:()=>void;onConfirm:(ids:string[])=>void}){
  const [selected,setSelected]=useState(()=>new Set(preview.items.map(item=>item.id)));
  const incompatible=preview.items.filter(item=>selected.has(item.id)&&!item.compatible).length;
  const sourceName=platforms.find(p=>p.id===preview.source)!.name,targetName=platforms.find(p=>p.id===preview.target)!.name;
  return <Modal title="选择要迁移的笔记" wide onClose={onClose}>
    <div className="migration-picker">
      <p className="migration-scope">{sourceName} → {targetName}</p>
      <p className="muted">迁移本次读取的笔记。默认全选；取消选择的笔记不会迁移，源平台内容保持不变。如需读取云端的新改动，请取消后重新开始。</p>
      {!preview.write_available&&<p className="selection-warning" role="status">{preview.blocked_reason}</p>}
      <div className="selection-tools"><button className="secondary" onClick={()=>setSelected(new Set(preview.items.map(item=>item.id)))}>全选</button><button className="secondary" onClick={()=>setSelected(new Set(preview.items.filter(item=>item.compatible).map(item=>item.id)))}>仅选兼容笔记</button><span aria-live="polite">共 {preview.total} 条 · 已选 {selected.size} 条 · 本次不迁移 {preview.total-selected.size} 条</span></div>
      <div className="selection-list" aria-label="笔记兼容性列表">{preview.items.map(item=><label className={`selection-row ${item.compatible?'':'incompatible'}`} key={item.id}>
        <input type="checkbox" checked={selected.has(item.id)} aria-label={`迁移 ${item.title}`} onChange={event=>setSelected(previous=>{const next=new Set(previous);if(event.target.checked)next.add(item.id);else next.delete(item.id);return next})}/>
        <span><strong>{item.title}</strong>{item.summary&&<small>{item.summary}</small>}{!item.compatible?<em>{item.reason}</em>:item.already_migrated?<em className="selection-confirmed">已确认迁入；再次执行会跳过</em>:null}{item.warnings.map((warning,index)=><small key={index}>{warning}</small>)}</span>
      </label>)}</div>
      {incompatible>0&&<p className="selection-warning" role="status">已选 {incompatible} 条不兼容或待核对笔记，请取消选择后继续；不会自动删除其中的附件。</p>}
      <div className="modal-actions"><button className="secondary" onClick={onClose}>取消</button><button className="primary" disabled={!selected.size||incompatible>0||!preview.write_available} onClick={()=>onConfirm(preview.items.filter(item=>selected.has(item.id)).map(item=>item.id))}>确认迁移 {selected.size} 条</button></div>
    </div>
  </Modal>
}

function MigrationProgress({task}:{task:TaskReport}){
  const percent=task.total>0?Math.max(0,Math.min(100,task.completed/task.total*100)):0;
  return <div className="migration-progress">
    <div className="migration-progress-status" role="status"><span className="spinner" aria-hidden="true"/><span>{task.stage||'正在准备迁移。'}</span></div>
    <div className="migration-progress-track" role="progressbar" aria-label="迁移进度" aria-valuemin={0} aria-valuemax={100} aria-valuenow={task.total>0?percent:undefined} aria-valuetext={task.total>0?`已处理 ${task.completed} / ${task.total} 条`:'正在准备，尚未确定总数'}><div style={{width:`${percent}%`}}/></div>
  </div>
}

function ReceiptReview({review,onClose}:{review:MigrationReview;onClose:()=>void}){
  const labels:Record<string,string>={confirmed:'已收到写入成功回执',sending:'写入未结束，待核对',uncertain:'写入结果不明，待核对',rejected:'创建被拒绝'};
  const resources:Record<string,string>={allocated:'已分配',uploaded:'已上传',linked:'已关联'};
  return <Modal title="迁移记录 · 只读查看" wide onClose={onClose}>
    <div className="receipt-review">
      <p className="migration-scope">{platforms.find(p=>p.id===review.source)!.name} → {platforms.find(p=>p.id===review.target)!.name} · 当前登录的两个账号</p>
      <p className="selection-warning">本页只读取电脑里的记录，未请求云端。成功回执及目标缓存命中均不能证明正文、图片和分组完整；请在目标官方页面或手机上逐项核对。</p>
      <p className="muted">当前来源缓存 {review.cached_notes} 条，其中 {review.cached_notes-review.unmatched_cached_notes} 条有关联回执，另有 {review.unmatched_cached_notes} 条没有匹配回执；共显示 {review.items.length} 条版本记录。已保存来源关联的旧版本待核对记录也会显示；缺少关联的旧回执仍可能无法匹配，列表为空也不代表从未迁移。</p>
      {(!review.source_cache_complete||!review.target_cache_complete)&&<p className="muted">{!review.source_cache_complete?'来源缓存不完整。':''}{!review.target_cache_complete?'目标缓存不完整。':''}本页不会自动重新获取笔记。</p>}
      {review.items.length?<div className="receipt-list">{review.items.map((item,index)=><article className="receipt-row" key={`${item.id}:${index}`}>
        <h3>{item.version==='source_missing'?'来源未缓存，旧标题未保存':`${item.version==='previous'?'当前来源标题：':''}${item.title}`}</h3><p className="receipt-status">{labels[item.status]||'状态未知，待核对'}</p>
        <small>{item.version==='previous'?'旧版本回执：与当前缓存正文或元数据不同，旧标题及正文未保存。':item.version==='source_missing'?'来源不在当前缓存，无法判断是否已删除；仅展示保存的来源标识。':'与当前缓存版本对应。'}未进行云端核对。</small>
        <small>来源标识：{item.id}</small>
        {item.remote_ids.length?<><small>已记录目标标识：{item.remote_ids.join('、')}</small><small>目标本地缓存命中 {item.cached_remote_ids.length} / {item.remote_ids.length} 条；未命中不能推断云端不存在。</small></>:<small>未取得目标标识，请结合原任务记录在目标官方页面查找，并核对正文、图片及分组。</small>}
        <small>云端资源记录：{item.resource_states.length?Object.entries(resources).map(([key,label])=>`${label} ${item.resource_states.filter(value=>value===key).length}`).join(' · '):'无记录'}{item.resource_states.some(value=>!resources[value])?' · 存在未知状态':''}</small>
      </article>)}</div>:<p className="muted">当前范围没有可关联的迁移回执。</p>}
      <p className="muted">一张图片可能对应多条资源记录；“已上传”不代表已加入笔记，“已关联”也不是图片显示完整的证明。待核对项保持阻断，本页不提供重试、删除记录或标记成功。请保留任务日志与原始笔记。</p>
      <div className="modal-actions"><button className="primary" onClick={onClose}>关闭</button></div>
    </div>
  </Modal>
}

function App(){
  const [state,setState]=useState(initial),[tab,setTab]=useState<'export'|'migrate'>('export');
  const [source,setSource]=useState<PlatformId|null>(null),[target,setTarget]=useState<PlatformId|null>(null);
  const [format,setFormat]=useState<Format>('docx'),[multi,setMulti]=useState(false),[loginPrompt,setLoginPrompt]=useState<Platform|null>(null);
  const [help,setHelp]=useState(false),[about,setAbout]=useState(false),[message,setMessage]=useState<{title:string;text:string}|null>(null);
  const [task,setTask]=useState<TaskReport|null>(null),[taskOpen,setTaskOpen]=useState(false),[pending,setPending]=useState(false),[native,setNative]=useState(!!window.pywebview?.api);
  const [preparingMigration,setPreparingMigration]=useState<{source:PlatformId;target:PlatformId;taskId:string}|null>(null);
  const [migrationPreview,setMigrationPreview]=useState<MigrationPreview|null>(null),[previewBusy,setPreviewBusy]=useState(false);
  const [migrationReview,setMigrationReview]=useState<MigrationReview|null>(null);
  const sourceState=state.platforms.find(p=>p.id===source);const busy=pending||previewBusy||!!task&&!terminal.has(task.status);
  const loginState=state.platforms.find(p=>p.id===loginPrompt?.id);
  const refresh=useCallback(async()=>{
    if(!window.pywebview?.api)return;
    try{const next=await invoke<AppState>('get_app_state');setState(next);setNative(true);if(next.current_task)setTask(next.current_task)}catch{/* Poll failures do not turn a running operation into success. */}
  },[]);
  useEffect(()=>{
    const ready=()=>{setNative(true);void refresh()};window.addEventListener('pywebviewready',ready);void refresh();
    const timer=setInterval(()=>void refresh(),2000);
    window.onNoteBridgeTask=(next)=>{setTask(next);if(terminal.has(next.status))void refresh()};
    return()=>{clearInterval(timer);window.removeEventListener('pywebviewready',ready);delete window.onNoteBridgeTask}
  },[refresh]);
  const act=async<T,>(method:string,...args:unknown[]):Promise<T|undefined>=>{
    try{return await invoke<T>(method,...args)}catch(error){setMessage({title:'操作未完成',text:error instanceof Error?error.message:'请稍后重试。'});return undefined}
  };
  const startTask=async(operation:string,payload:unknown)=>{
    setPending(true);const result=await act<TaskReport>(operation,payload);setPending(false);
    if(result){setTask(result);setTaskOpen(true);await refresh()}
    return result;
  };
  const fetchNotes=async()=>{
    if(!source)return setMessage({title:'请选择笔记平台',text:'先点击要导出的平台卡片，再登录对应账号。'});
    if(!sourceState?.logged_in)return setLoginPrompt(platforms.find(p=>p.id===source)!);
    await startTask('fetch_notes',{platform:source});
  };
  const exportNotes=async()=>{
    if(!source||!sourceState?.count)return setMessage({title:'请先获取笔记',text:'获取完成后即可选择格式，将笔记导出到电脑。'});
    await startTask('export_notes',{platform:source,format,multi_file:multi});
  };
  const migrate=async()=>{
    if(!source||!target)return setMessage({title:'请选择迁移方向',text:'分别选择迁出平台和迁入平台。'});
    if(source===target)return setMessage({title:'迁移方向相同',text:'迁出与迁入平台需要不同。'});
    const missing=[source,target].find(id=>!state.platforms.find(p=>p.id===id)?.logged_in);
    if(missing)return setLoginPrompt(platforms.find(p=>p.id===missing)!);
    const preparation=await startTask('fetch_notes',{platform:source});
    if(preparation)setPreparingMigration({source,target,taskId:preparation.id});
  };
  const reviewMigration=async()=>{
    setTaskOpen(false);setTab('migrate');
    if(!source||!target||source===target)return setMessage({title:'选择要核对的迁移方向',text:'请在迁移页选择原迁移的来源和目标，登录同一对账号后点击底部“迁移记录”。'});
    setPending(true);const result=await act<MigrationReview>('review_migration',{source,target});setPending(false);
    if(result)setMigrationReview(result);
  };
  useEffect(()=>{
    if(!preparingMigration||task?.id!==preparingMigration.taskId||!terminal.has(task.status))return;
    setPreparingMigration(null);
    if(!['succeeded','partial'].includes(task.status))return;
    setTaskOpen(false);setPreviewBusy(true);
    void invoke<MigrationPreview>('preview_migration',{source:preparingMigration.source,target:preparingMigration.target})
      .then(setMigrationPreview)
      .catch(error=>setMessage({title:'预检未完成',text:error instanceof Error?error.message:'请重新读取笔记后预检。'}))
      .finally(()=>setPreviewBusy(false));
  },[preparingMigration,task]);
  const card=(p:Platform,side:'source'|'target',compact=false)=><PlatformCard key={p.id} platform={p} state={state.platforms.find(s=>s.id===p.id)!} selected={(side==='source'?source:target)===p.id} compact={compact} disabled={busy} onSelect={()=>side==='source'?setSource(p.id):setTarget(p.id)} onLogin={()=>setLoginPrompt(p)}/>;
  return <div className="app-shell">
    <header className="titlebar pywebview-drag-region"><div className="identity"><Brand/><div><h1>笔记互迁</h1><p>笔记导出与跨平台迁移</p></div></div><div className="title-actions"><button className="open-source" onClick={()=>setAbout(true)}><span className="status-dot"/>免费 · 开源 · 本地处理</button><div className="window-actions"><button aria-label="最小化" onClick={()=>void act('window_action','minimize')}><Icon name="minus" size={12}/></button><button aria-label="最大化" onClick={()=>void act('window_action','maximize')}><Icon name="square" size={12}/></button><button aria-label="关闭应用" onClick={()=>void act('window_action','close')}><Icon name="close" size={12}/></button></div></div></header>
    <nav className="tabs" aria-label="笔记操作"><button className={tab==='export'?'active':''} aria-selected={tab==='export'} onClick={()=>setTab('export')}><Icon name="export" size={13}/>导出笔记</button><button className={tab==='migrate'?'active':''} aria-selected={tab==='migrate'} onClick={()=>setTab('migrate')}><Icon name="transfer" size={14}/>迁移笔记</button></nav>
    <main>
      {tab==='export'?<section className="panel export-panel" key="export"><div className="section-label">第一步：选择笔记平台</div><div className="platform-grid">{platforms.map(p=>card(p,'source'))}</div><div className="panel-action"><button className="primary" disabled={busy} onClick={()=>void fetchNotes()}><Icon name="download" size={16}/>{busy?'正在处理':'获取笔记'}</button>{sourceState?.count?<span className="cached-count">已获取 {sourceState.count} 条{sourceState.complete?'':' · 部分内容待补齐'}</span>:null}</div>
      {sourceState&&sourceState.count>0&&<div className="export-options"><div className="options-top"><div className="section-label">第二步：选择导出格式</div><div className="directory-actions"><button title="打开笔记导出目录" onClick={()=>void act('open_directory','exports')}><Icon name="folder"/>笔记</button><button title="打开笔记附件下载目录" onClick={()=>void act('open_directory','resources')}><Icon name="folder"/>附件</button><button title="将最近一次导出及附件打包到指定目录" disabled={!state.has_export||busy} onClick={()=>void startTask('package_notes',{})}><Icon name="package"/>打包</button></div></div><div className="format-grid">{formats.map(f=><button className={`format-card ${format===f.id?'selected':''}`} key={f.id} aria-pressed={format===f.id} onClick={()=>setFormat(f.id)}><span className={`format-logo format-${f.id}`}>{f.glyph}</span><strong>{f.label}</strong><small>{f.detail}</small></button>)}</div><div className="export-bottom"><div className="mode-switch" aria-label="文件模式"><button aria-pressed={!multi} className={!multi?'active':''} onClick={()=>setMulti(false)}>单文件</button><button aria-pressed={multi} className={multi?'active':''} onClick={()=>setMulti(true)}>多文件</button></div><button className="primary" onClick={()=>void exportNotes()} disabled={busy}><Icon name="export"/>导出笔记</button></div></div>}
      </section>:<section className="panel migration-panel" key="migrate"><div className="section-label">选择迁移来源与目标</div><div className="migration-columns"><div className="migration-side"><h2><Icon name="export" size={12}/>迁出平台</h2><div className="platform-list">{platforms.map(p=>card(p,'source',true))}</div></div><div className="direction"><Icon name="arrow" size={35}/></div><div className="migration-side"><h2><Icon name="download" size={12}/>迁入平台</h2><div className="platform-list">{platforms.map(p=>card(p,'target',true))}</div></div></div><div className="panel-action"><button className="primary" onClick={()=>void migrate()} disabled={busy}><Icon name="transfer"/>开始迁移</button></div>{task?.operation==='migrate'&&!terminal.has(task.status)&&<MigrationProgress task={task}/>}</section>}
      {previewBusy&&<div className="task-strip" role="status"><span><span className="spinner small"/>正在检查正文格式与本地附件，不会创建云端笔记。</span></div>}
      {task&&!taskOpen&&!previewBusy&&<button className={`task-strip ${task.status}`} onClick={()=>setTaskOpen(true)}><span>{!terminal.has(task.status)&&<span className="spinner small"/>}{statusLabels[task.status]} · {task.stage}</span><span>查看结果 →</span></button>}
    </main>
    <footer><span className="version">V {state.version}</span><button onClick={()=>setAbout(true)}>关于</button><button onClick={()=>setHelp(true)}>使用指南</button><button onClick={()=>void act('open_directory','logs')}>日志</button><button disabled={busy} onClick={()=>void reviewMigration()}>迁移记录</button><div className="footer-status">{!native?'界面预览 · 未连接桌面服务':state.release_channel==='stable'?'正式版 · 本地处理，数据由你掌握':'开发预览版'}</div></footer>
    {loginPrompt&&<Modal platform={loginPrompt} title={`${loginPrompt.name}登录提示`} onClose={()=>setLoginPrompt(null)}><div className="login-explainer"><span className="large-platform" style={{color:loginPrompt.color}}>{loginPrompt.glyph}</span><p>{loginPrompt.loginHint}</p><ol><li>在官方网页中完成登录。</li><li>按官方提示完成二次验证，并进入笔记页面。</li><li>回到这里点击“检查账号”，确认笔记访问权限。</li></ol>{loginState?.message&&<p className="login-notice">{loginState.message}</p>}<p className="muted">密码和验证码只在官方窗口输入。关闭软件后，本次登录会话会清除。</p></div><div className="modal-actions"><button className="secondary" disabled={pending} onClick={async()=>{if(loginState?.login_open)await act('cancel_login',loginPrompt.id);setLoginPrompt(null);await refresh()}}>取消登录</button>{loginState?.login_open&&<button className="secondary" disabled={pending} onClick={()=>void act('login_platform',loginPrompt.id)}>显示官方窗口</button>}<button className="primary" disabled={pending} onClick={async()=>{const p=loginPrompt;setPending(true);const checking=!!loginState?.login_open;const result=await act(checking?'complete_login':'login_platform',p.id);setPending(false);if(result!==undefined&&checking)setLoginPrompt(null);await refresh()}}>{pending?'正在检查…':loginState?.login_open?'已完成登录，检查账号':'确定并前往登录'}<Icon name="arrow" size={15}/></button></div></Modal>}
    {migrationPreview&&<MigrationPicker key={migrationPreview.token} preview={migrationPreview} onClose={()=>setMigrationPreview(null)} onConfirm={ids=>{const preview=migrationPreview;setMigrationPreview(null);void startTask('migrate_notes',{source:preview.source,target:preview.target,note_ids:ids,preview_token:preview.token})}}/>}
    {migrationReview&&<ReceiptReview review={migrationReview} onClose={()=>setMigrationReview(null)}/>}
    {message&&<Modal title={message.title} onClose={()=>setMessage(null)}><p className="message-text">{message.text}</p><div className="modal-actions"><button className="primary" onClick={()=>setMessage(null)}>知道了</button></div></Modal>}
    {taskOpen&&task&&<Modal title={statusLabels[task.status]} onClose={()=>setTaskOpen(false)}><div className={`task-state ${task.status}`}><span className="task-symbol">{task.status==='succeeded'?<Icon name="check" size={26}/>:terminal.has(task.status)?<Icon name="info" size={26}/>:<span className="spinner"/>}</span><h3>{task.stage}</h3><p>{task.total?`${task.completed} / ${task.total} 条`:'正在准备任务'}</p></div><progress max={task.total||1} value={task.total?task.completed:undefined}/><div className="task-counts"><span>成功 <b>{task.succeeded}</b></span><span>已跳过 <b>{task.skipped}</b></span><span>提示 <b>{task.issues.length}</b></span></div>{task.issues.length>0&&<div className="issue-list">{task.issues.map((issue,i)=><p key={i}><Icon name="info" size={14}/><span>{issue.message}</span></p>)}</div>}{task.status==='needs_review'&&<p className="selection-warning">请在目标官方页面核对笔记。关闭此窗口后，选择原迁移的双方账号，点击底部“迁移记录”查看本地回执；核对前不要重复创建。</p>}{task.output_path&&<div className="output-path">{task.output_path}</div>}<div className="modal-actions">{!terminal.has(task.status)&&<button className="secondary" onClick={()=>void act('cancel_task',task.id)}>取消任务</button>}{task.output_path&&<button className="secondary" onClick={()=>void act('open_task_output',task.id)}><Icon name="folder"/>打开目录</button>}<button className="primary" onClick={()=>setTaskOpen(false)}>{terminal.has(task.status)?'完成':'后台继续'}</button></div></Modal>}
    {help&&<Modal title="使用说明" wide onClose={()=>setHelp(false)}><div className="guide"><Guide n="1" title="开始之前">请先在手机上完成笔记云同步。仅云端已有的内容能够获取；迁移前检查目标云空间容量。各平台的密码、短信和设备验证都在官方网页中完成。</Guide><Guide n="2" title="导出笔记">选择平台 → 登录账号 → 获取笔记 → 选择 TXT、Word、Markdown 或 HTML → 选择单文件或多文件 → 导出到你指定的位置。导出文件、附件与说明可以一起打包。</Guide><Guide n="3" title="跨平台迁移">在左侧选择来源，右侧选择目标，分别登录后开始迁移。迁移向目标新增笔记，保留源平台的原始数据。成功后在目标设备开启云同步。</Guide><Guide n="4" title="格式与附件">常见正文样式及图片按平台能力转换。目标无法设置原始时间时，会在正文附注时间。专有手写、涂鸦等内容可能不支持；缺失附件或转换差异会写入结果报告。请核对报告后再处理原始笔记。</Guide><Guide n="5" title="离线 HTML 阅读">合并 HTML 提供目录、摘要、全文搜索和创建月份筛选。多个关键词用空格分隔，匹配同时包含这些词的笔记。Enter 跳转下一个结果，Shift + Enter 跳转上一个，目录滚轮和滑块也可切换。</Guide><Guide n="6" title="中断与重试">任务支持取消。已确认写入的条目会保留回执；结果不明时停止重复创建。选择原迁移的双方账号后，可点击底部“迁移记录”只读查看当前缓存对应的回执和资源状态，再到目标官方页面核对。旧版本或已删除来源的记录可能无法关联；本地缓存命中不等于云端完整确认。</Guide></div></Modal>}
    {about&&<Modal title="关于笔记互迁" onClose={()=>setAbout(false)}><div className="about"><Brand/><h3>让笔记随你，轻松换平台。</h3><p>七个平台的笔记导出与迁移，免费开源，本地处理。</p><p className="muted">版本 {state.version} · {state.release_channel==='stable'?'正式版':'开发预览版'} · MIT License<br/>各平台支持范围与格式差异请查看兼容性说明。</p></div><div className="modal-actions"><button className="primary" onClick={()=>setAbout(false)}>知道了</button></div></Modal>}
  </div>
}
function Guide({n,title,children}:{n:string;title:string;children:React.ReactNode}){return <section><span>{n}</span><div><h3>{title}</h3><p>{children}</p></div></section>}
createRoot(document.getElementById('root')!).render(<StrictMode><App/></StrictMode>);
