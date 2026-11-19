'use strict';

const $ = (id) => document.getElementById(id);
const captions = {recorded:'已发送', pending:'项目待处理', received:'会话已接收',
  replied:'已回复', failed:'回复失败', uncertain:'需确认', manual:'等待人工答复', cancelled:'参与项目已退出',
  ready:'可使用', used:'已使用', revoked:'已撤销', expired:'已过期'};
const kinds = {question:'提问', followup:'追问', answer:'答复', chat:'发言', broadcast:'广播',discussion:'全体讨论'};
const room = location.pathname.match(/^\/room\/([0-9a-f]{32})\/?$/)?.[1];
const views = {history:'对话', members:'频道成员', ...(room?{}:{invites:'邀请'}), conclusions:'已采纳结论'};
const clients = {
  codex: {name:'Codex', icon:'/codex.webp'},
  claude: {name:'Claude Code', icon:'/claude-code.webp'},
  deepseek: {name:'DeepSeek Harness', icon:'/deepseek-harness.webp'}
};
let searchOpen = false, navSnapshot = '';
const mobile = matchMedia('(max-width: 700px)');
const state = {groups:[], group:null, view:'history', topic:null, data:null, publicUrl:null,
  page:null, query:'', serial:0, error:'', logged:false, self:null};
let submitDialog = null, busy = false, dirty = false, closeReturn = null, noticeTimer, focusSearch = false;
const dialog = $('dialog');
const drafts = new Map(), sending = new Set();
let composerGroup = null, recipientsKey = '';
let liveView = null, lastRefresh = 0;
let channelShell = null, refreshing = false;
// Only the two unfiltered message lists are shared between tabs in the current channel.
const pages = new Map();
function channelPage(view,topic,query) {
  const key=view==='conclusions'?'conclusions':'history';
  if (!topic && !query && pages.has(key)) return pages.get(key);
  const page={history:null,older:[],more:null,position:null,updated:0};
  if (!topic && !query) pages.set(key,page);
  return page;
}
function setHistory(page,history) {
  // Keep the page the reader scrolled into when new messages push it off the first page.
  if (page.older.length && page.history) {
    const known=new Set([...history.messages,...page.older].map(m=>m.id));
    page.older.push(...page.history.messages.filter(m=>!known.has(m.id)));
  }
  page.history=history;
  page.updated=performance.now();
}
function warmPage(view,group,revision) {
  const page=channelPage(view);
  if (page.history || page.pending) return;
  page.pending=api(view,{group}).then(history=>{
    if (pages.get(view)!==page || page.history) return;
    setHistory(page,history); page.revision=revision;
    if (state.page===page) render();
  }).catch(()=>{ /* Opening the tab retries a failed prefetch. */ }).finally(()=>{page.pending=null;});
}
const requestId = () => Array.from(crypto.getRandomValues(new Uint32Array(4)),n=>n.toString(16).padStart(8,'0')).join('');
function draftFor(group) {
  if (!drafts.has(group)) drafts.set(group,{body:'',target:'',parent:null,request:requestId(),error:''});
  return drafts.get(group);
}
function syncComposer() {
  const g=state.group?.id, d=state.data;
  const visible=state.logged && g && d && state.view==='history' && !state.query && !state.topic;
  $('composer').hidden=!visible;
  if (!visible) { composerGroup=null; return; }
  const draft=draftFor(g), targets=d.members.filter(m=>m.id!==state.self?.id).map(m=>[m.id,m.name]);
  const signature=JSON.stringify(targets);
  if (g!==composerGroup || recipientsKey!==signature) {
    const choices=[node('option',{value:''},'全体成员'),...targets.map(([id,name])=>node('option',{value:id},name))];
    if (draft.target && !targets.some(([id])=>id===draft.target)) choices.push(node('option',{value:draft.target},'成员已离开'));
    $('chat-to').replaceChildren(...choices); recipientsKey=signature;
  }
  if (g!==composerGroup) { $('chat-body').value=draft.body; composerGroup=g; }
  $('chat-to').value=draft.target;
  $('reply-context').hidden=!draft.parent;
  if (draft.parent) $('reply-parent').textContent='答复问题 #'+draft.parent;
  const disabled=d.paused || !targets.length || sending.has(g);
  $('chat-body').placeholder=d.paused ? '频道已暂停，恢复讨论后可发言' : !targets.length ? '邀请同事或 Agent 后即可发消息' : '发消息到 # '+state.group.name+'，或 @ 一位成员…';
  $('chat-body').disabled=disabled; $('chat-to').disabled=disabled || Boolean(draft.parent);
  $('chat-send').disabled=disabled || !draft.body.trim() || (draft.target && !targets.some(([id])=>id===draft.target));
  $('chat-error').textContent=draft.error;
}
function editDraft() {
  if (!composerGroup) return;
  Object.assign(draftFor(composerGroup),{body:$('chat-body').value,target:$('chat-to').value,request:requestId(),error:''});
  syncComposer();
}
$('chat-body').addEventListener('input',editDraft);
$('chat-to').addEventListener('change',editDraft);
$('chat-body').addEventListener('keydown',e=>{
  if (!e.isComposing && e.key==='Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); $('composer').requestSubmit(); }
});
$('composer').addEventListener('submit',async e=>{
  e.preventDefault();
  if (!composerGroup || $('chat-send').disabled) return;
  const g=composerGroup, draft=draftFor(g);
  sending.add(g); syncComposer();
  try {
    await api('speak',{group:g,target:draft.target,parent:draft.parent,body:draft.body.trim(),request:draft.request});
    draft.body=''; draft.parent=null; draft.error=''; draft.request=requestId();
    if (composerGroup===g) {
      $('chat-body').value=''; await refresh();
      const log=$('workspace').querySelector('.chat-log');
      if (composerGroup===g && log) log.scrollTop=log.scrollHeight;
    }
  } catch (err) { draft.error=err.message; }
  finally { sending.delete(g); syncComposer(); if (composerGroup===g) $('chat-body').focus(); }
});
function node(tag, attrs = {}, ...children) {
  const item = document.createElement(tag);
  for (const [k,v] of Object.entries(attrs)) {
    if (v !== null && v !== undefined) item.setAttribute(k,v);
  }
  item.append(...children.filter(x => x !== null && x !== undefined));
  return item;
}
function action(label, name, cls = '', data = {}) {
  return node('button', {type:'button',class:cls,'data-action':name,...data}, label);
}
$('owner-key').after(action('显示','show-key','text-button',{id:'show-key','aria-pressed':'false'}));
if (!room) $('nav-heading').append(action('＋','create','square',{'aria-label':'新建群聊',title:'新建群聊'}));
$('account').append(action(room?'退出频道':'退出登录',room?'leave-room':'logout','text-button'));
document.body.classList.toggle('member-room',Boolean(room));
$('send-shortcut').textContent = /Mac|iPhone|iPad/.test(navigator.platform) ? '⌘ ↵' : 'Ctrl ↵';
function mobileNav(open) {
  const shown = mobile.matches && open;
  $('shell').classList.toggle('nav-open', shown);
  $('nav-scrim').hidden = !shown;
  $('sidebar').inert = mobile.matches && !shown;
  $('main').inert = shown;
  $('nav-toggle').setAttribute('aria-expanded', String(shown));
  if (shown) $('channel-filter').focus();
}
$('nav-toggle').addEventListener('click',()=>mobileNav(true));
$('nav-scrim').addEventListener('click',()=>{ mobileNav(false); $('nav-toggle').focus(); });
$('nav-toggle').disabled=false; $('nav-scrim').disabled=false;
document.addEventListener('keydown',e=>{
  if (e.key==='Escape' && $('shell').classList.contains('nav-open') && !dialog.open) {
    mobileNav(false); $('nav-toggle').focus();
  }
});
mobile.addEventListener('change',()=>{ mobileNav(false); if(state.logged) render(); });
mobileNav(false);
$('channel-filter').addEventListener('input',nav);
$('group-list').addEventListener('click',e=>{ if (e.target.closest('a')) mobileNav(false); });
$('dialog-title').after(action('×','close','square',{'aria-label':'关闭窗口'}));
function chip(label, tone='') { return node('span',{class:'chip '+tone}, label); }
function statusChip(value) {
  const tone = ['replied','received','used'].includes(value) ? 'good' :
    ['pending','uncertain','manual'].includes(value) ? 'warn' : value==='ready' ? 'info' : '';
  return chip(captions[value] || value, tone);
}
function timestamp(value,withDate=false) {
  const when = new Date(value*1000);
  return node('time',{datetime:when.toISOString(),title:when.toLocaleString('zh-CN')},
    when.toLocaleString('zh-CN',{...(withDate?{month:'2-digit',day:'2-digit'}:{}),hour:'2-digit',minute:'2-digit',hour12:false}));
}
function notice(text) {
  clearTimeout(noticeTimer); $('notice').textContent=text;
  noticeTimer=setTimeout(() => { $('notice').textContent=''; },6500);
}
function rememberedRooms() {
  try {
    const items=JSON.parse(localStorage.getItem('diesheng.rooms') || '[]');
    return Array.isArray(items)?items.filter(r=>r && /^[0-9a-f]{32}$/.test(r.id) && typeof r.name==='string'):[];
  } catch { return []; }
}
function rememberRoom(id,name) {
  const rooms=rememberedRooms();
  if (name && rooms[0]?.id===id && rooms[0].name===name) return;
  const updated=rooms.filter(item=>item.id!==id);
  if (name) updated.unshift({id,name});
  try { localStorage.setItem('diesheng.rooms',JSON.stringify(updated)); } catch {}
}
function showLogin(asOwner=false) {
  stopLiveView();
  clearTimeout(noticeTimer); $('notice').textContent='';
  $('boot').hidden=true;
  mobileNav(false); $('channel-filter').value=''; navSnapshot=''; searchOpen=false; state.group=null;
  state.logged=false; state.serial++; state.groups=[]; state.data=null; state.page=null; state.error='';
  pages.clear(); channelShell=null; refreshing=false; focusSearch=false;
  if (dialog.open) dialog.close();
  $('workspace').replaceChildren(); $('group-list').replaceChildren(); $('roster').replaceChildren(); $('mobile-actions').replaceChildren();
  drafts.clear(); composerGroup=null; $('composer').hidden=true;
  $('shell').hidden=true; $('member-entry').hidden=true; $('known-rooms').hidden=true; $('login').hidden=true; state.self=null;
  if (room) { rememberRoom(room,null); void showMemberEntry(); return; }
  const saved=asOwner?[]:rememberedRooms();
  if (saved.length) {
    $('known-links').replaceChildren(...saved.map(r=>node('a',{href:'/room/'+r.id},'# '+r.name)));
    $('member-caption').textContent='叠声'; $('member-channel').textContent='我的频道';
    $('member-help').textContent='选择一个频道继续对话。'; $('member-form').hidden=true; $('member-retry').hidden=true;
    $('known-rooms').hidden=false; $('member-entry').hidden=false; document.title='我的频道 · 叠声'; return;
  }
  $('login').hidden=false;
  document.title='群主登录 · 叠声';
}
async function api(name, data={}, signal=AbortSignal.timeout(12000)) {
  const scope=room?'member/'+room:'owner';
  const resp=await fetch('/api/'+scope+'/'+name,{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(data),signal});
  const result=await resp.json();
  if (!resp.ok) {
    if (resp.status===401 && !['login','preview','join'].includes(name)) showLogin();
    const error=new Error(result.error || '服务暂不可用，请重试'); error.status=resp.status; throw error;
  }
  return result;
}
const memberCode=()=>new URLSearchParams(location.hash.slice(1)).get('invite');
async function showMemberEntry() {
  $('member-entry').hidden=false; $('member-form').hidden=true; $('member-retry').hidden=true;
  $('member-channel').textContent='加入频道'; $('member-error').textContent=''; document.title='加入频道 · 叠声';
  const code=memberCode();
  if (!code) { $('member-help').textContent='请在这个浏览器打开群主发给你的同事邀请。'; return; }
  $('member-help').textContent='正在读取邀请…';
  try {
    const invite=await api('preview',{code});
    if (code!==memberCode() || state.logged) return;
    $('member-channel').textContent='# '+invite.name;
    $('member-help').textContent='用昵称加入后，你可以查看本频道近 30 天保留的对话、发言和向 Agent 提问。';
    $('member-form').hidden=false;
  } catch (error) {
    $('member-help').textContent=error.message; $('member-retry').hidden=error.status===410;
  }
}
$('member-retry').addEventListener('click',()=>refresh());
$('member-form').addEventListener('submit',async e=>{
  e.preventDefault();
  const submit=e.currentTarget.querySelector('button'); submit.disabled=true;
  try {
    await api('join',{code:memberCode(),name:$('member-nickname').value.trim()});
    history.replaceState(null,'',location.pathname);
    await refresh();
  } catch (error) { $('member-error').textContent=error.message; }
  finally { submit.disabled=false; }
});
function empty(title, hint, button) {
  return node('div',{class:'empty'},node('h2',{},title),node('p',{},hint),button);
}
function url(g, view='history', topic=null, query='') {
  return '#/'+g+'/'+view+(topic ? '/'+topic : '')+(query?'?'+new URLSearchParams({q:query}):'');
}
function openRoute(hash) {
  if (location.hash===hash) return;
  history.pushState(null,'',hash);
  activateRoute();
}
function activateRoute() { stopLiveView(); void refresh('navigate'); }
function searchRoute(query) {
  const next=url(state.group.id,state.view,state.topic,query);
  if (location.hash===next) return;
  focusSearch=true;
  openRoute(next);
}
function nav() {
  const query=$('channel-filter').value.trim().toLocaleLowerCase();
  const signature=JSON.stringify([state.groups,state.group?.id,query,state.publicUrl]);
  if (signature===navSnapshot) return;
  navSnapshot=signature;
  const current=document.activeElement?.closest('#group-list a')?.getAttribute('href');
  const rooms=state.groups.filter(g=>g.name.toLocaleLowerCase().includes(query));
  $('group-list').replaceChildren(...rooms.map(g=>{
    const author=!room && g.last_agent==='host'?'我':g.last_author;
    const preview=g.last_body ? (author ? author+'：' : '')+g.last_body : (g.purpose || g.members+' 位成员');
    return node('a',{href:url(g.id),'aria-label':g.name,'aria-current':g.id===state.group?.id?'page':null},
      node('span',{class:'channel-hash','aria-hidden':'true'},'#'),node('span',{class:'channel-entry'},
        node('strong',{},g.name),node('small',{},preview)));
  }));
  if (query && !rooms.length) $('group-list').append(node('p',{class:'side-empty'},'没有匹配的频道'));
  if (current) [...$('group-list').children].find(el=>el.getAttribute('href')===current)?.focus();
  $('channel-count').textContent=String(state.groups.length);
  $('service').textContent=new URL(state.publicUrl || location.origin).host;
  $('service').title=state.publicUrl || location.origin;
}
function clientName(agent) {
  return agent==='host' ? '群主' : agent==='human' ? '频道同事' : clients[agent]?.name || agent;
}
function avatar(name,agent,small=false) {
  const icon=clients[agent]?.icon;
  const style=icon?' branded':agent==='host'?' host':'';
  const content=icon ? node('img',{src:icon,alt:'',width:38,height:38,draggable:'false'}) :
    agent==='host' ? '主' : Array.from(name || '?')[0].toUpperCase();
  return node('span',{class:'avatar'+style+(small?' small':''),'aria-hidden':'true'},content);
}
function supportedClients() {
  return Object.entries(clients).map(([agent,{name}])=>
    node('div',{},avatar(name,agent,true),node('strong',{},name)));
}
$('supported-clients').replaceChildren(...supportedClients());
function listeningLabel(member) {
  if (member.agent==='human' || member.agent==='host') return member.online?'在线':'离线';
  if (member.paused || state.data?.paused) return '已暂停';
  if (!member.online) return '离线';
  return member.automatic?'正在监听':'在线 · 待监听';
}
function inspector(members) {
  const roster=$('roster'), online=members.filter(m=>m.online).length;
  roster.replaceChildren(node('div',{class:'roster-title'},node('h2',{},'频道成员'),node('span',{class:'count'},String(members.length))),
    node('p',{class:'roster-summary'},node('i',{class:online?'dot online':'dot'}),online+' 位在线'));
  for (const m of members) {
    roster.append(node(room?'div':'button',room?{class:'person'}:{type:'button',class:'person','data-action':'profile','data-id':m.id,'aria-label':'设置成员 '+m.name},
      node('span',{class:'presence-avatar'},avatar(m.name,m.agent,true),node('i',{class:'dot '+(m.online?'online':''),'aria-label':m.online?'在线':'离线'})),
      node('span',{class:'person-details'},node('strong',{},m.name),node('small',{},node('span',{},clientName(m.agent)),' · '+listeningLabel(m)),
        m.role && m.agent!=='host'?node('span',{class:'person-role'},m.role):null)));
  }
  if (!members.length) roster.append(node('p',{class:'muted'},'Agent 加入后会显示在这里。'));
  roster.append(node('div',{class:'inbox-summary'},node('h3',{},'项目收件箱'),
    node('div',{},node('span',{},'待处理'),node('b',{},String(members.reduce((n,m)=>n+m.pending,0)))),
    node('div',{},node('span',{},'处理中'),node('b',{},String(members.reduce((n,m)=>n+m.processing,0))))));
}
function msgEntry(m) {
  if (state.view==='history') {
    const own=room?m.sender===state.self?.id:m.author_agent==='host', people=m.recipients || [];
    let text=captions[m.receipt] || m.receipt;
    if (people.length) text=people.filter(p=>p.status==='replied').length+'/'+people.length+' 已回复';
    else if (m.accepted) text='已采纳';
    else if (m.kind==='answer' && m.receipt==='pending') text='待签收';
    else if (m.kind!=='answer' && m.receipt==='received') text='已签收 · 处理中';
    const meta=node('div',{class:'meta'},m.quick_reply?node('span',{class:'client-label',title:'这条历史消息由旧版预设规则生成'},'预设回复'):null,
      node('span',{class:'receipt '+(['received','replied'].includes(m.receipt)||m.accepted?'positive':'')},text));
    if (!state.topic && m.kind!=='broadcast') meta.append(node('a',{href:url(state.group.id,'history',m.topic)},'话题 #'+m.topic));
    if (!room && (m.receipt==='failed' || m.receipt==='uncertain')) meta.append(action('重新投递',m.receipt==='failed'?'retry-message':'requeue','text-button',{'data-id':m.id}));
    if (!own && !state.topic && !state.query && state.data.members.some(member=>member.id===m.sender)) {
      const direct=m.target===(room?state.self?.id:state.data.self) && ['question','followup'].includes(m.kind)
        && ['pending','manual','received','uncertain'].includes(m.receipt);
      meta.append(action(direct?'答复问题':'回复','reply-to','text-button',{'data-id':m.sender,'data-parent':direct?m.id:null}));
    }
    return node('article',{class:'chat-row'+(own?' own':''),'data-message':m.id},avatar(m.author,m.author_agent),
      node('div',{class:'chat-content'},node('div',{class:'chat-byline'},node('strong',{},own?'我':m.author),node('span',{class:'client-label'},clientName(m.author_agent)),
        m.recipient?node('span',{class:'to'},'@'+m.recipient):(people.length?node('span',{},'全体成员'):null),timestamp(m.created)),
        node('p',{class:'chat-bubble'},m.body),m.evidence?node('p',{class:'basis'},'依据　'+m.evidence):null,meta,
        m.error?node('p',{class:'error'},m.error):null));
  }
  const title=node('div',{class:'row-title'},node('span',{class:'msg-id'},'#'+m.id),node('strong',{},m.author),
    m.recipient ? node('span',{class:'arrow','aria-label':'发往'},'→') : null,
    m.recipient ? node('strong',{},m.recipient) : null,node('span',{class:'sub'},kinds[m.kind]),
    node('span',{class:'end'},m.accepted?chip('已采纳','good'):statusChip(m.receipt)));
  const meta=node('div',{class:'meta'},timestamp(m.created));
  if (m.parent) meta.append(node('span',{},'引用 #'+m.parent));
  if (m.kind!=='broadcast' && !state.topic) meta.append(node('a',{href:url(state.group.id,'history',m.topic)},'话题 #'+m.topic));
  if (!room && m.receipt==='uncertain') meta.append(action('核对并重新投递','requeue','text-button',{'data-id':m.id}));
  return node('article',{class:'entry'},title,state.view==='conclusions' && m.question?
    node('p',{class:'question'},'原问题　'+m.question):null,node('p',{class:'body'},m.body),
    m.evidence ? node('p',{class:'basis'},'依据　'+m.evidence) : null,meta);
}
function memberEntry(m) {
  const browser=m.agent==='human' || m.agent==='host';
  return node('article',{class:'entry'},node('div',{class:'row-title'},avatar(m.name,m.agent,true),node('strong',{},'@'+m.name),
    chip(m.online?'在线':'离线',m.online?'good':''),browser?null:chip(m.verified?'已验证收发':'项目已加入',m.verified?'info':''),
    room?null:node('span',{class:'end'},action('成员设置','profile','text-button',{'data-id':m.id}),action('移除','remove','text-button text-danger',{'data-id':m.id}))),
    m.role?node('p',{class:'body'},m.role):null,node('div',{class:'meta'},
      node('span',{},clientName(m.agent)),...(browser?[]:[node('span',{},listeningLabel(m)),node('span',{},m.sessions+' 个近期会话'),
      node('span',{},m.processing+' 条处理中'),node('span',{},m.pending+' 条待处理'),m.paused?chip('自动应答已暂停','warn'):null])));
}
function invitationEntry(i) {
  return node('article',{class:'entry'},node('div',{class:'row-title'},node('strong',{},i.kind==='human' && i.status!=='used'?'同事邀请':'@'+i.name),
    statusChip(i.status),i.status==='ready'?node('span',{class:'end'},action('撤销邀请','revoke','text-button text-danger',{'data-id':i.id})):null),
    i.role?node('p',{class:'body'},i.role):null,node('div',{class:'meta'},'生成',timestamp(i.created,true),'到期',timestamp(i.expires,true)));
}
function panel() {
  const {data:d,view,group:g}=state;
  const part=node('section',{class:'panel '+view,'aria-label':views[view]});
  const reading=view==='history' || view==='conclusions';
  const waiting=!d || (reading && !state.page.history);
  if (waiting && !state.error) part.setAttribute('aria-busy','true');
  const head=node('div',{class:'section-heading'},node('h2',{},state.topic?'话题 #'+state.topic:views[view]));
  if (state.topic) head.append(node('a',{href:url(g.id)},'返回群记录'));
  if (state.topic) part.append(head);
  if (reading) {
    const search=node('input',{type:'search',id:'query','aria-label':'搜索正文或依据',maxlength:'160',placeholder:'搜索字段、分支或测试名'});
    search.value=state.query;
    const clear=action('清除','clear','text-button',{'aria-label':'清除搜索'});
    clear.hidden=!search.value;
    search.addEventListener('input',()=>{clear.hidden=!search.value;});
    const form=node('form',{class:'search-form',novalidate:'',role:'search'},search,clear,node('button',{type:'submit'},'搜索'));
    form.addEventListener('submit',e=>{
      e.preventDefault(); searchRoute(search.value.trim());
    });
    if (searchOpen || state.query || view==='conclusions') part.append(form);
  }
  if (waiting) {
    part.append(state.error?empty('暂时无法读取'+views[view],state.error,action('重新加载','retry')):
      node('p',{class:'loading',role:'status'},'正在读取'+views[view]+'…'));
    return part;
  }
  if (reading) {
    const {history,older:previous,more}=state.page;
    const map=new Map([...previous,...history.messages].map(m=>[m.id,m]));
    const messages=[...map.values()].sort((a,b)=>view==='history'?a.id-b.id:b.id-a.id);
    const list=view==='history'?node('div',{class:'chat-log',role:'log','aria-label':'群消息记录',tabindex:'0'}):part;
    const older=(more ?? history.more)?node('div',{class:'load'},action('查看更早消息','older')):null;
    if (older && view==='history') list.append(older);
    let lastDay='';
    for (const message of messages) {
      const date=new Date(message.created*1000);
      const day=date.toLocaleDateString('zh-CN',{month:'long',day:'numeric',weekday:'long'});
      if (view==='history' && day!==lastDay) {
        list.append(node('div',{class:'day-divider'},node('span',{},day)));
        lastDay=day;
      }
      list.append(msgEntry(message));
    }
    if (list!==part) part.append(list);
    if (!messages.length) {
      if (state.query) part.append(empty('没有匹配的记录','可尝试字段名、分支名或测试名。'));
      else if (view==='conclusions') part.append(empty('还没有已采纳结论','提问成员采纳答复后，可以在这里找回原问题、结论和依据。'));
      else list.append(empty(state.topic?'暂无可见的话题记录':'开始群聊',
        state.topic?'此话题的记录可能已超过保留期。':(d.members.length?'给全体发一句话，或选一位成员开始聊。':'添加一位 Agent，邀请它参与对话。')));
    }
    if (older && view!=='history') part.append(older);
  } else if (view==='members') {
    part.append(...d.members.map(memberEntry));
    if (!d.members.length) part.append(empty('等待项目加入','生成邀请后，让同事在目标项目的 Agent 会话中确认接入。'));
  } else {
    const live=Boolean(d.link_expires);
    part.append(node('section',{class:'link-control'},node('div',{},node('h2',{},'频道入群链接'),
      node('p',{class:'muted'},live?'同一链接可邀请多个 Agent，有效期至 '+new Date(d.link_expires*1000).toLocaleString('zh-CN')+'。':'创建一个链接，即可邀请多个项目会话加入这个频道。')),
      node('div',{class:'actions'},action(live?'查看链接':'生成链接','invite'),live?action('停用链接','disable-link','text-button text-danger'):null)),
      node('div',{class:'section-heading'},node('h2',{},'同事邀请'),action('新建同事邀请','human-invite','text-button')));
    const humans=d.invites.filter(i=>i.kind==='human'), agents=d.invites.filter(i=>i.kind!=='human');
    part.append(...humans.map(invitationEntry));
    if (!humans.length) part.append(node('p',{class:'muted'},'每位同事使用自己的链接，在浏览器查看和参与这个频道。'));
    part.append(node('div',{class:'section-heading'},node('h2',{},'单项目邀请'),action('新建单次邀请','personal','text-button')));
    part.append(...agents.map(invitationEntry));
    if (!agents.length) part.append(empty('暂无单项目邀请','需要预先指定成员名称和职责时，可生成一份 30 分钟内有效的单次邀请。'));
  }
  return part;
}
function createChannelShell(g) {
  const title=document.createTextNode(g.name), purpose=node('p',{}), paused=chip('讨论已暂停','warn');
  const stack=node('span',{class:'avatar-stack','aria-hidden':'true'}), count=node('span',{}), badge=node('span',{class:'tab-count'});
  const people=node('a',{class:'cover-people',href:url(g.id,'members')},stack,count,node('span',{'aria-hidden':'true'},'›'));
  const header=node('header',{class:'room-header'},node('div',{class:'cover-art','aria-hidden':'true'}),
    node('div',{class:'room-title'},node('h1',{},node('span',{'aria-hidden':'true'},'# '),title),purpose,people,paused));
  const pause=action('暂停讨论','pause');
  const actions=room?null:node('div',{class:'actions channel-actions'},
    node('details',{class:'channel-menu'},node('summary',{'aria-label':'频道操作',title:'频道操作'},'•••'),
      node('div',{},pause)),
    action('邀请同事','human-invite','invite-human'),action('邀请 Agent','invite','primary invite-action',{'aria-label':'邀请 Agent'}));
  const search=node('button',{type:'button',class:'search-trigger','data-action':'toggle-search',
    'aria-label':'搜索频道消息',title:'搜索频道消息'},node('i',{class:'search-icon','aria-hidden':'true'}));
  const tools=node('div',{class:'tab-tools'},search);
  const links=Object.entries(views).map(([id,label])=>node('a',{href:url(g.id,id),'data-view':id},label,id==='members'?badge:null));
  const tabs=node('div',{class:'tab-strip'},node('nav',{class:'tabs','aria-label':'频道详情'},...links),tools);
  $('mobile-actions').replaceChildren();
  $('workspace').replaceChildren(header,tabs);
  return {group:g.id,title,purpose,people,stack,count,badge,paused,pause,links,actions,tools,search,panels:new Map(),scrollTops:{}};
}
function updateChannelShell(s,g,d) {
  const count=d?.members.length ?? g.members, members=d?.members.slice(0,3) || [];
  const avatars=JSON.stringify(members.map(m=>[m.name,m.agent]));
  if (s.avatars!==avatars) { s.stack.replaceChildren(...members.map(m=>avatar(m.name,m.agent,true))); s.avatars=avatars; }
  if (s.badge.textContent!==String(count)) {
    s.badge.textContent=String(count); s.count.textContent=count+' 位成员';
    s.people.setAttribute('aria-label','查看频道的 '+count+' 位成员');
  }
  if (s.title.data!==g.name) s.title.data=g.name;
  if (s.purpose.textContent!==g.purpose) s.purpose.textContent=g.purpose;
  s.purpose.hidden=!g.purpose; s.paused.hidden=!(d?.paused ?? g.paused);
  const pauseLabel=s.paused.hidden?'暂停讨论':'恢复讨论';
  if (s.pause.textContent!==pauseLabel) s.pause.textContent=pauseLabel;
  for (const link of s.links) {
    if (state.view===link.dataset.view) link.setAttribute('aria-current','page');
    else link.removeAttribute('aria-current');
  }
  s.search.classList.toggle('inactive',state.view!=='history');
  const destination=mobile.matches?$('mobile-actions'):s.tools;
  if (s.actions && s.actions.parentElement!==destination) destination.append(s.actions);
}
function rememberPosition(s) {
  const log=s.panel?.querySelector('.chat-log');
  if (log && s.reading) {
    const top=log.getBoundingClientRect().top, scroll=log.scrollTop;
    const pinned=log.scrollHeight-scroll-log.clientHeight<72;
    const anchor=!pinned && [...log.querySelectorAll('[data-message]')].find(el=>el.getBoundingClientRect().bottom>=top);
    s.reading.position={pinned,scroll,id:anchor?.dataset?.message,offset:anchor?anchor.getBoundingClientRect().top-top:0};
  } else if (s.list && s.panel) s.scrollTops[s.list]=s.panel.scrollTop;
}
function restorePosition(s) {
  const log=s.panel.querySelector('.chat-log');
  if (log) {
    const pos=s.reading.position;
    if (!pos || pos.pinned) log.scrollTop=log.scrollHeight;
    else {
      const anchor=pos.id && log.querySelector('[data-message="'+pos.id+'"]');
      log.scrollTop=anchor?log.scrollTop+anchor.getBoundingClientRect().top-log.getBoundingClientRect().top-pos.offset:pos.scroll;
    }
  } else if (s.list) s.panel.scrollTop=s.scrollTops[s.list] || 0;
}
function render() {
  const g=state.group, d=state.data, view=state.view, page=state.page;
  if (!g) {
    channelShell=null; syncComposer(); $('mobile-actions').replaceChildren();
    $('main').classList.remove('has-roster'); $('roster').hidden=true;
    document.title='频道 · 叠声';
    if (!$('workspace').querySelector('.welcome')) $('workspace').replaceChildren(node('section',{class:'welcome'},
      node('div',{class:'welcome-cover'},node('div',{class:'cover-art','aria-hidden':'true'}),
        node('div',{class:'welcome-content'},node('h1',{},'创建第一个频道'),node('p',{},'为项目建一个讨论空间，邀请你正在使用的 Agent 一起加入。'),
          action('新建群聊','create','primary'))),
      node('div',{class:'welcome-clients'},node('h2',{},'可以邀请的 Agent'),...supportedClients())));
    return;
  }
  if (channelShell?.group!==g.id) channelShell=createChannelShell(g);
  const s=channelShell;
  const content=view==='members'?[d?.members,d?.paused]:view==='invites'?[d?.invites,d?.link_expires]:
    [page.history,page.older,page.more,d?.members.length,searchOpen];
  const signature=JSON.stringify([view,state.topic,state.query,state.error,content]);
  if (signature!==s.signature) rememberPosition(s);
  updateChannelShell(s,g,d);
  document.title=g.name+' · '+views[view]+' · 叠声';
  const showRoster=Boolean(d && view==='history');
  $('main').classList.toggle('has-roster',showRoster); $('roster').hidden=!showRoster;
  if (showRoster) {
    const rosterKey=JSON.stringify([d.members,d.paused]);
    if (s.rosterKey!==rosterKey) { inspector(d.members); s.rosterKey=rosterKey; }
  }
  syncComposer();
  if (signature!==s.signature) {
    const base=!state.topic && !state.query, cached=base && s.panels.get(view);
    const next=cached && cached.signature===signature?cached.panel:panel();
    const route=url(g.id,view,state.topic,state.query);
    const form=s.route===route && s.panel.querySelector('.search-form'), nextForm=next.querySelector('.search-form');
    const editing=form && form.contains(document.activeElement)?document.activeElement:null;
    if (form && nextForm) nextForm.replaceWith(form);
    if (base) s.panels.set(view,{panel:next,signature});
    if (s.panel) s.panel.replaceWith(next); else $('workspace').append(next);
    s.panel=next; s.signature=signature; s.route=route; s.reading=view==='history'?page:null;
    s.list=view!=='history' && !state.query && !next.hasAttribute('aria-busy') && !state.error?view:null;
    restorePosition(s);
    if (editing?.isConnected) editing.focus({preventScroll:true});
    else if (focusSearch) $('query')?.focus({preventScroll:true});
    focusSearch=false;
  }
}
function canRefresh() {
  return state.logged && !document.hidden && !dialog.open && !busy &&
    !document.querySelector('.channel-menu[open]') && !document.activeElement?.closest('.search-form');
}
function stopLiveView() {
  liveView?.controller.abort(); liveView=null;
}
async function followMessages() {
  const group=state.group?.id;
  if (liveView || !canRefresh() || state.view!=='history' || !Number.isSafeInteger(state.page?.revision)) return;
  const watcher={controller:new AbortController()};
  liveView=watcher;
  try {
    while (liveView===watcher && canRefresh() && state.group?.id===group && state.view==='history') {
      const since=state.page.revision;
      const timer=setTimeout(()=>watcher.controller.abort(),22000);
      let next;
      try { next=await api('changes',{group,since,timeout:12},watcher.controller.signal); }
      finally { clearTimeout(timer); }
      if (liveView!==watcher || !canRefresh()) break;
      if (next.revision!==state.page?.revision || performance.now()-lastRefresh>=10000) await refresh('background');
      if (next.revision!==since && state.page?.revision===since) break;
    }
  } catch { /* The normal refresh retries when the channel or network is available. */ }
  finally { if (liveView===watcher) liveView=null; }
}
function selectRoute(g,tab,topic,params) {
  const view=Object.hasOwn(views,tab)?tab:'history';
  const filter=view==='history' && /^[1-9][0-9]*$/.test(topic || '')?Number(topic):null;
  const query=(view==='history' || view==='conclusions')?(new URLSearchParams(params).get('q') || '').slice(0,160):'';
  const groupChanged=g?.id!==state.group?.id;
  const changed=groupChanged || view!==state.view || filter!==state.topic || query!==state.query;
  if (groupChanged) { pages.clear(); state.data=null; }
  if (groupChanged || state.view!==view) { searchOpen=false; focusSearch=false; }
  if (changed) {
    channelShell?.actions?.querySelector('details')?.removeAttribute('open');
    state.page=g?channelPage(view,filter,query):null;
    state.error='';
  }
  state.group=g; state.view=view; state.topic=filter; state.query=query;
  nav();
  if (changed || !g) render();
}
async function refresh(mode) {
  if (mode==='background' && refreshing) return;
  const navigating=mode==='navigate';
  const id=++state.serial;
  refreshing=true;
  try {
    const [route,params]=location.hash.split('?');
    const [,gid,tab,topic]=route.split('/');
    let g=navigating && state.groups.find(item=>item.id===gid);
    if (!g) {
      const list=await api('groups');
      if (id!==state.serial) return;
      g=list.groups.find(item=>item.id===gid) || (!gid && list.groups.find(item=>item.id===state.group?.id)) || list.groups[0] || null;
      if (g && g.id!==gid) history.replaceState(null,'',url(g.id));
      state.groups=list.groups; state.publicUrl=list.public_url; state.logged=true; state.self=list.self || null;
      if (state.self && g) {
        rememberRoom(room,g.name);
        if ($('account').querySelector('strong').textContent!==state.self.name) {
          $('account').querySelector('strong').textContent=state.self.name;
          $('account').querySelector('.avatar').replaceWith(avatar(state.self.name,'human',true));
        }
      }
      $('boot').hidden=true; $('member-entry').hidden=true; $('login').hidden=true; $('shell').hidden=false;
    }
    selectRoute(g,tab,topic,params);
    if (!g) return;
    const {view,topic:filter,query,page}=state;
    if (navigating && state.data) {
      if (page.pending) await page.pending;
      if (id!==state.serial) return;
      const reading=view==='history' || view==='conclusions';
      const fresh=!reading || (page.revision===state.data.revision && performance.now()-page.updated<2500);
      if (fresh && performance.now()-lastRefresh<2500) { void followMessages(); return; }
    }
    const {history:result,...details}=await api('detail',{group:g.id,topic:filter,query,conclusions:view==='conclusions'});
    if (id!==state.serial) return;
    state.data=details; state.error='';
    setHistory(page,result); page.revision=details.revision;
    lastRefresh=performance.now();
    render();
    warmPage('history',g.id,details.revision);
    warmPage('conclusions',g.id,details.revision);
    void followMessages();
  } catch (e) {
    if (id!==state.serial) return;
    if (!state.logged) {
      if (room && e.status!==401) {
        $('boot').hidden=true; $('member-entry').hidden=false; $('member-form').hidden=true;
        $('member-help').textContent='暂时无法连接频道，请检查网络后重试。'; $('member-retry').hidden=false;
      } else { showLogin(); $('login-error').textContent=e.status===401?'':'连接服务失败，请检查地址后重试'; }
      return;
    }
    const reading=state.view==='history' || state.view==='conclusions';
    if (state.group && (!state.data || (reading && !state.page.history))) {
      state.error=e.message; render();
    } else notice('暂时无法刷新，当前显示上次读取的记录');
  } finally { if (id===state.serial) refreshing=false; }
}
function field(id,label,limit,required=false) {
  return node('div',{class:'field'},node('label',{for:id},label),node('input',{id,name:id,maxlength:limit,
    required:required?'':null,'aria-describedby':'dialog-error',autocomplete:'off'}));
}
function openDialog(title,children,handler,label,destructive=false) {
  if (!dialog.open) closeReturn=document.activeElement;
  submitDialog=handler; dirty=false;
  $('dialog-title').textContent=title; $('dialog-body').replaceChildren(...children); $('dialog-error').textContent='';
  const cancel=action(handler?'取消':'关闭','close');
  $('dialog-actions').replaceChildren(cancel);
  if (handler) $('dialog-actions').append(node('button',{type:'submit',class:destructive?'danger':'primary'},label));
  if (!dialog.open) dialog.showModal();
  (destructive?cancel:$('dialog-body').querySelector('input,textarea,button') || cancel).focus();
}
function tryClose() {
  if (busy && submitDialog) return;
  if (dirty && !dialog.querySelector('[data-action="discard"]')) {
    $('dialog-error').replaceChildren('输入尚未提交。 ',action('放弃输入','discard','text-button text-danger'));
    return;
  }
  if (!dirty) dialog.close();
}
dialog.addEventListener('cancel',e=>{ e.preventDefault(); tryClose(); });
dialog.addEventListener('close',()=>{ $('dialog-body').replaceChildren(); submitDialog=null; dirty=false;
  if (closeReturn?.isConnected) closeReturn.focus(); else if (state.logged) $('workspace').focus(); });
$('dialog-form').addEventListener('input',()=>{ dirty=true; });
$('dialog-form').addEventListener('submit',async e=>{
  e.preventDefault(); if (busy || !submitDialog) return;
  const form=e.currentTarget;
  for (const input of form.querySelectorAll('input[required]')) {
    if (!input.value.trim()) { input.setAttribute('aria-invalid','true'); input.focus();
      $('dialog-error').textContent='请填写'+input.labels[0].textContent; return; }
    input.removeAttribute('aria-invalid');
  }
  busy=true; form.setAttribute('aria-busy','true');
  const button=form.querySelector('[type="submit"]'); button.disabled=true;
  try { await submitDialog(); } catch (e) { $('dialog-error').textContent=e.message; }
  finally { busy=false; form.removeAttribute('aria-busy'); button.disabled=false; }
});
$('login-form').addEventListener('submit',async e=>{
  e.preventDefault(); const key=$('owner-key'),btn=e.currentTarget.querySelector('[type="submit"]');
  if (btn.disabled) return;
  if (!key.value.trim()) { $('login-error').textContent='请填写群主密钥'; key.setAttribute('aria-invalid','true'); key.focus(); return; }
  btn.disabled=true;
  try { await api('login',{key:key.value.trim()}); key.value=''; key.removeAttribute('aria-invalid'); $('login-error').textContent='';
    await refresh(); }
  catch (e) { $('login-error').textContent=e.message; key.setAttribute('aria-invalid','true'); key.focus(); }
  finally { btn.disabled=false; }
});
function confirmAction(title,text,op,payload,label) {
  openDialog(title,[node('p',{},text)],async()=>{
    await api(op,payload); dirty=false; dialog.close();
    notice({remove:'成员已移除',revoke:'邀请已撤销',requeue:'消息已重新排队',disable_link:'频道链接已停用'}[op]); await refresh();
  },label,true);
}
function inviteResult(result,personal,human=false) {
  const link=new URL(result.url,location.origin).href;
  const area=node('input',{id:'invite-output',class:'invite-url',readonly:'','aria-label':'入群链接',value:link});
  const copy=action('复制链接','copy','primary');
  openDialog(human?'邀请同事':personal?'单项目入群链接':'邀请 Agent',[
    node('div',{class:'invite-channel'},node('span',{class:'channel-tile','aria-hidden':'true'},'#'),node('div',{},
      node('strong',{},state.group.name),node('small',{},human?'浏览器成员邀请':personal?'单项目邀请':'频道共享链接'))),
    node('p',{},human?'将这份链接私发给一位同事。对方填写昵称后，可以查看本频道近 30 天保留的对话、发言和向 Agent 提问。':'把链接发给项目会话，告诉它「加入叠声，记住频道，监听并回复发给我的问题」。'),
    node('div',{class:'copy-row'},area,copy),
    node('small',{class:'hint'},(personal || human?'可用一次':'可供多个 Agent 使用')+' · '+new Date(result.expires*1000).toLocaleString('zh-CN',{month:'long',day:'numeric',hour:'2-digit',minute:'2-digit'})+' 到期'),
    !state.publicUrl?node('p',{class:'hint'},'当前链接仅供本机使用。要邀请同事，请连接局域网后以共享模式启动服务。'):
      link.startsWith('http://')?node('p',{class:'hint'},'同事连接同一局域网后，即可使用这个链接。'):null
  ].filter(Boolean),null);
  copy.focus();
  void refresh('background');
}
async function handle(button) {
  const g=state.group, id=button.dataset.id;
  switch (button.dataset.action) {
    case 'show-key': {
      const key=$('owner-key'), hide=key.type==='text';
      key.type=hide?'password':'text'; button.textContent=hide?'显示':'隐藏'; button.setAttribute('aria-pressed',String(!hide)); break;
    }
    case 'clear': {
      const input=$('query'); input.value=''; button.hidden=true; input.focus();
      searchRoute(''); break;
    }
    case 'close': tryClose(); break;
    case 'discard': dirty=false; dialog.close(); break;
    case 'retry': await refresh(); break;
    case 'owner-login': showLogin(true); $('owner-key').focus(); break;
    case 'logout': await api('logout'); showLogin(); break;
    case 'leave-room':
      openDialog('退出这个频道？',[node('p',{},'退出后，这个浏览器将失去频道访问权限。重新加入时需要群主发一份新邀请。')],async()=>{
        await api('leave'); showLogin();
      },'确认退出',true); break;
    case 'create':
      openDialog('新建群聊',[field('room-name','群名称',64,true),field('room-purpose','讨论范围（可选）',240)],async()=>{
        const result=await api('create',{name:$('room-name').value,purpose:$('room-purpose').value});
        dirty=false; dialog.close(); location.hash=url(result.group); notice('群聊已创建');
      },'创建群聊'); break;
    case 'human-invite':
    case 'invite': {
      const group=g.id;
      const human=button.dataset.action==='human-invite', title=human?'邀请同事':'邀请 Agent';
      openDialog(title,[node('p',{role:'status'},'正在准备频道链接…')],null);
      try {
        const result=await api(human?'human_invite':'channel_link',{group});
        if (!dialog.open || $('dialog-title').textContent!==title || state.group?.id!==group) break;
        inviteResult(result,false,human);
      } catch (e) { if(dialog.open) $('dialog-error').replaceChildren(e.message,' ',action('重试',button.dataset.action)); }
      break;
    }
    case 'personal':
      openDialog('单项目邀请',[field('project-name','项目公开名称',64,true),field('project-role','项目职责（可选）',160),
        node('p',{class:'hint'},'此链接限用一次，30 分钟内有效。')],async()=>{
        const result=await api('invite',{group:g.id,name:$('project-name').value,role:$('project-role').value});
        inviteResult(result,true);
      },'生成邀请'); break;
    case 'disable-link':
      confirmAction('停用频道链接','持有此链接的 Agent 将无法再加入。现有成员可以继续对话。','disable_link',{group:g.id},'停用链接'); break;
    case 'copy':
      if (await copyField($('invite-output'))) button.textContent='已复制';
      else $('dialog-error').textContent='已选中链接，请使用系统复制快捷键。';
      break;
    case 'toggle-search':
      searchOpen=!searchOpen;
      if (!searchOpen && state.query) searchRoute(''); else { render(); if (searchOpen) $('query')?.focus(); }
      break;
    case 'pause':
      button.closest('details').open=false;
      await api('pause',{group:g.id,paused:!(state.data?.paused ?? g.paused)}); await refresh(); break;
    case 'profile': {
      const m=state.data.members.find(item=>item.id===id);
      openDialog('成员设置',[field('member-name','群内名字',64,true),field('member-role','工作职责',160)],async()=>{
        await api('profile',{group:g.id,member:id,name:$('member-name').value,role:$('member-role').value});
        dirty=false; dialog.close(); await refresh();
      },'保存设置');
      $('member-name').value=m.name; $('member-role').value=m.role; break;
    }
    case 'reply-to':
      if (composerGroup===g.id) {
        draftFor(g.id).parent=button.dataset.parent?Number(button.dataset.parent):null;
        $('chat-to').value=id; editDraft(); $('chat-body').focus();
      } break;
    case 'cancel-reply': draftFor(g.id).parent=null; editDraft(); break;
    case 'retry-message':
      await api('requeue',{group:g.id,message:Number(id)}); await refresh(); break;
    case 'remove': {
      const m=state.data.members.find(x=>x.id===id);
      confirmAction('移除 @'+m.name,m.agent==='human'?'这位同事将立即失去本频道的访问权限。':'此成员凭据的读写权限将立即失效。仍有效的频道链接可用于加入新身份。',
        'remove',{group:g.id,member:id},m.agent==='human'?'移除成员':'移除项目'); break;
    }
    case 'revoke': {
      const i=state.data.invites.find(x=>x.id===id);
      confirmAction(i.kind==='human'?'撤销同事邀请':'撤销 @'+i.name+' 的邀请','这份邀请将立即失效。','revoke',{group:g.id,invite:id},'撤销邀请'); break;
    }
    case 'requeue':
      confirmAction('核对消息 #'+id,'请先与项目开发者核对回执，确认原会话已停止处理。继续后，这条消息会重新进入待处理队列。',
        'requeue',{group:g.id,message:Number(id)},'重新投递'); break;
    case 'older': {
      const {group,topic,view,query,page}=state;
      const ids=[...page.older,...page.history.messages].map(m=>m.id);
      const previous=await api(view==='conclusions'?'conclusions':'history',{group:group.id,topic,query,before:Math.min(...ids)});
      if (state.page!==page || state.view!==view) return;
      page.older.push(...previous.messages); page.more=previous.more; render(); break;
    }
  }
}
document.addEventListener('click',async e=>{
  const link=e.target.closest('a[href^="#/"]');
  if (link && e.button===0 && !e.metaKey && !e.ctrlKey && !e.altKey && !e.shiftKey && !e.defaultPrevented) {
    e.preventDefault(); openRoute(link.hash); return;
  }
  const btn=e.target.closest('button[data-action]');
  if (!btn || btn.disabled) return;
  btn.disabled=true;
  try { await handle(btn); } catch (err) { if(dialog.open) $('dialog-error').textContent=err.message; else notice(err.message); }
  finally { btn.disabled=false; }
});
window.addEventListener('hashchange',activateRoute);
document.addEventListener('visibilitychange',()=>{if (document.hidden) stopLiveView(); else if (state.logged) refresh('background');});
window.addEventListener('pagehide',stopLiveView);
window.addEventListener('online',()=>{ if (state.logged) refresh(); });
window.addEventListener('offline',()=>notice('连接已断开，当前显示上次读取的记录'));
setInterval(()=>{if (canRefresh() && (!liveView || performance.now()-lastRefresh>=5000)) refresh('background');},2500);
refresh();
