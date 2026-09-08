import type {PlatformId} from './types';

export interface Platform {id: PlatformId; name: string; glyph: string; color: string; hint?: string; loginHint: string}
export const platforms: Platform[] = [
  {id:'xiaomi',name:'小米笔记',glyph:'mi',color:'#ff7b24',loginHint:'在小米官方页面登录，并按页面要求完成短信等二次验证。确认手机笔记已同步到云端。'},
  {id:'oppo',name:'OPPO笔记',glyph:'oppo',color:'#24835e',hint:'一加 / Realme 用户在此登录',loginHint:'使用 OPPO / 欢太账号登录官方云服务，并完成受信任设备或短信验证。'},
  {id:'vivo',name:'vivo笔记',glyph:'vivo',color:'#7463de',hint:'iQOO 用户在此登录',loginHint:'在 vivo 官方页面登录，完成短信等验证。iQOO 用户使用对应 vivo 账号。'},
  {id:'huawei',name:'华为备忘录',glyph:'华为',color:'#d75162',hint:'请注意为华为备忘录，而非华为笔记',loginHint:'登录华为云空间并完成二次验证。本入口对应“备忘录”；独立的“华为笔记”数据不属于这个入口。'},
  {id:'honor',name:'荣耀笔记',glyph:'HONOR',color:'#414459',hint:'旧版备忘录可能需要华为入口',loginHint:'使用独立荣耀账号登录并完成二次验证。旧设备若仍将备忘录同步到华为账号，请使用华为备忘录入口。'},
  {id:'meizu',name:'魅族笔记',glyph:'魅族',color:'#21a4c6',loginHint:'在 Flyme 官方页面登录魅族账号，确认便签已经同步至云端。'},
  {id:'wps',name:'WPS便签',glyph:'WPS',color:'#e55d65',loginHint:'在 WPS 便签官方页面登录，使用与手机便签一致的 WPS 账号。'}
];
export const formats = [
  {id:'txt',label:'TXT',detail:'纯文本，轻便易读',glyph:'T'},
  {id:'docx',label:'Word',detail:'保留样式，自动目录',glyph:'W'},
  {id:'md',label:'Markdown',detail:'通用笔记格式',glyph:'M'},
  {id:'html',label:'HTML',detail:'离线阅读与精准搜索',glyph:'H'}
] as const;

