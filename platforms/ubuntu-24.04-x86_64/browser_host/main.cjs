'use strict';
const {app, BrowserWindow, session} = require('electron');
const readline = require('node:readline');
const path = require('node:path');
const {pathToFileURL} = require('node:url');
const {official, relevant} = require('./matching.cjs');
app.commandLine.appendSwitch('ozone-platform', 'x11');
app.commandLine.appendSwitch('disable-features', 'PasswordManagerEnabled,AutofillServerCommunication');
app.setName('XDownloader');
let config, win, ready = false, closing = false;
const send = data => process.stdout.write(JSON.stringify(data) + '\n');
const lines = readline.createInterface({input:process.stdin, terminal:false});
function shutdown() { if (!closing) { closing = true; app.quit(); } }
lines.on('close', shutdown);
lines.on('line', async line => {
  if (line.length > 65536) return;
  try {
    const input = JSON.parse(line);
    if (!config) { config = input; app.setPath('userData', config.profile); await initialize(); }
    else if (input.op === 'close') shutdown();
    else if (ready) await command(input);
  } catch (e) { send({type:'error',code:'browser_command',detail:e.name}); }
});
app.on('window-all-closed', shutdown);
async function initialize() {
  await app.whenReady();
  const profile = session.defaultSession;
  const proxy = config.proxy_mode === 'manual'
    ? {mode:'fixed_servers',proxyRules:config.proxy.replace('socks5h://','socks5://')}
    : {mode:config.proxy_mode === 'direct' ? 'direct' : 'system'};
  await profile.setProxy(proxy);
  profile.setPermissionRequestHandler((_wc,_permission,callback)=>callback(false));
  profile.setPermissionCheckHandler(()=>false);
  profile.on('will-download', (event)=>event.preventDefault());
  for (const [name,value] of Object.entries(config.cookies || {})) {
    if (['auth_token','ct0','twid'].includes(name) && typeof value === 'string')
      await profile.cookies.set({url:'https://x.com',domain:'.x.com',path:'/',name,value,secure:true,httpOnly:name==='auth_token'});
  }
  win = new BrowserWindow({width:1000,height:700,show:false,frame:false,skipTaskbar:true,
    backgroundColor:'#ffffff',webPreferences:{nodeIntegration:false,contextIsolation:true,
      sandbox:true,webSecurity:true,allowRunningInsecureContent:false,backgroundThrottling:false,
      devTools:false,spellcheck:false}});
  const wc = win.webContents;
  wc.setWindowOpenHandler(({url})=>{ if (official(url)) wc.loadURL(url); return {action:'deny'}; });
  wc.on('will-navigate',(event,url)=>{ if (config.mode !== 'probe' && !official(url)) event.preventDefault(); });
  wc.on('will-redirect',(event,url)=>{ if (config.mode !== 'probe' && !official(url)) event.preventDefault(); });
  wc.on('certificate-error',(event,_url,_error,_certificate,callback)=>{event.preventDefault();callback(false);});
  wc.on('render-process-gone',()=>send({type:'error',code:'browser_process'}));
  wc.on('focus',()=>{if(config.mode==='login')send({type:'browser_focus'});});
  wc.on('did-navigate-in-page',(_e,url,isMain)=>{if(isMain)send({type:'address',url});});
  wc.on('did-fail-load',(_e,code,description,url,isMain)=>{if(isMain&&code!==-3)send({type:'navigation',ok:false,error:description,url});});
  wc.on('did-finish-load',async()=>{
    send({type:'navigation',ok:true,url:wc.getURL()});
    if(config.mode==='resolve') {
      try {const html=await wc.executeJavaScript('document.documentElement.outerHTML');if(html.length<=8388608)send({type:'html',html});}
      catch {} // 初始 HTML 失败时仍可从目标接口取得媒体。
    }
  });
  if(config.mode==='resolve') {
    const pending=new Map();let count=0;
    wc.debugger.attach('1.3');
    await wc.debugger.sendCommand('Network.enable',{maxTotalBufferSize:16777216,maxResourceBufferSize:8388608,maxPostDataSize:65536});
    wc.debugger.on('message',async(_event,method,p)=>{
      const matched = pending.has(p.requestId);
      try {
        if(count>=8)return;
        if(method==='Network.requestWillBeSent') {
          pending.delete(p.requestId);
          if(relevant(p.request.url,p.request.method,p.request.postData,config.tweet_id))pending.set(p.requestId,{status:0});
        } else if(method==='Network.responseReceived' && pending.has(p.requestId)) {
          pending.get(p.requestId).status=p.response.status;
          if(p.response.status!==200){send({type:'payload',status:p.response.status,payload:{}});pending.delete(p.requestId);count++;}
        } else if(method==='Network.loadingFinished' && pending.has(p.requestId)) {
          const item=pending.get(p.requestId);pending.delete(p.requestId);
          if(p.encodedDataLength>8388608){send({type:'capture_error',target:true,detail:'ResponseTooLarge'});return;}
          const body=await wc.debugger.sendCommand('Network.getResponseBody',{requestId:p.requestId});
          const raw=body.base64Encoded?Buffer.from(body.body,'base64').toString('utf8'):body.body;
          if(Buffer.byteLength(raw)>8388608){send({type:'capture_error',target:true,detail:'ResponseTooLarge'});return;}
          const payload=JSON.parse(raw);count++;send({type:'payload',status:item.status,payload});
        } else if(method==='Network.loadingFailed') {
          if(pending.has(p.requestId))send({type:'capture_error',target:true,detail:'ResponseLoadFailed'});
          pending.delete(p.requestId);
        }
      } catch (e) {send({type:'capture_error',target:matched,detail:e.name});}
    });
  }
  ready=true;
  const handle=win.getNativeWindowHandle();
  send({type:'ready',version:process.versions.chrome,window:handle.length===8?Number(handle.readBigUInt64LE()):handle.readUInt32LE()});
  if(config.mode==='login')win.show();
  if(config.mode==='probe') {
    const html='<html><body><video autoplay muted controls src="'+pathToFileURL(path.resolve(config.file)).href+'"></video></body></html>';
    const fs=require('node:fs');const file=path.join(config.profile,'probe.html');fs.writeFileSync(file,html);await win.loadFile(file);
  } else await win.loadURL(config.url || 'https://x.com/i/flow/login');
}
async function command(input) {
  const wc=win.webContents;
  switch(input.op) {
    case 'resize':win.setContentSize(Math.max(1,input.width),Math.max(1,input.height));break;
    case 'focus':if(config.mode==='login'&&!wc.isFocused()){win.focus();wc.focus();}break;
    case 'reload':wc.reload();break;
    case 'navigate':if(official(input.url))await wc.loadURL(input.url);break;
    case 'cookies': {
      const found=await session.defaultSession.cookies.get({url:'https://x.com'});const cookies={};
      for(const c of found)if(c.domain.replace(/^\./,'')==='x.com'&&['auth_token','ct0','twid'].includes(c.name))cookies[c.name]=c.value;
      send({type:'cookies',cookies});break;
    }
    case 'page_status': {
      const result=await wc.executeJavaScript(config.page_status_script || "JSON.stringify({url:location.href,has_article:!!document.querySelector('main article'),text:(document.querySelector('main')?.innerText||'').slice(0,4096)})");
      send({type:'page_status',result});break;
    }
    case 'probe': {
      const result=await wc.executeJavaScript('JSON.stringify({h264:document.createElement("video").canPlayType(\'video/mp4; codecs="avc1.42E01E"\'),aac:document.createElement("video").canPlayType(\'audio/mp4; codecs="mp4a.40.2"\'),videos:Array.from(document.querySelectorAll("video")).map(v=>({duration:v.duration,time:v.currentTime,paused:v.paused,ready:v.readyState,error:v.error?.code||0}))})');
      send({type:'probe',result});break;
    }
  }
}
