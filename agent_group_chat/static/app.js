'use strict';

const $ = (id) => document.getElementById(id);
const captions = {recorded:'已发送', pending:'项目待处理', received:'会话已接收',
  replied:'已回复', waiting:'等待同伴', reviewing:'正在核对答复', failed:'回复失败', uncertain:'需确认', manual:'等待人工答复', cancelled:'参与项目已退出',
  ready:'可使用', used:'已使用', revoked:'已撤销', expired:'已过期'};
const recipientCaptions = {...captions,pending:'待签收',received:'已签收'};
const kinds = {question:'提问', followup:'追问', answer:'答复', chat:'发言', broadcast:'仅同步', discussion:'全体讨论', group_question:'多人提问'};
const roomFromPath = () => location.pathname.match(/^\/room\/([0-9a-f]{32})\/?$/)?.[1];
let room = roomFromPath();
const views = {history:'对话', announcement:'公告', topics:'话题', private:'私聊', members:'频道成员', invites:'邀请'};
const liveChannelViews = ['history','announcement','topics'];
const topicKinds = {question:'定向提问',group_question:'多人提问',discussion:'全体讨论',collaboration:'Agent 协作',chat:'成员发言'};
const topicStates = {pending:'待答复',active:'进行中',answered:'已答复',attention:'需关注',cancelled:'已停止',recorded:'已记录'};
const clients = {
  codex: {name:'Codex', icon:'/codex.webp'},
  claude: {name:'Claude Code', icon:'/claude-code.webp'},
  deepseek: {name:'DeepSeek Harness', icon:'/deepseek-harness.webp'}
};
let searchOpen = false, navSnapshot = '';
const mobile = matchMedia('(max-width: 700px)');
const state = {groups:[], group:null, view:'history', topic:null, data:null, publicUrl:null,
  page:null, query:'', scope:'all', category:'', topicStatus:'', before:null, privateRoute:null,
  serial:0, error:'', logged:false, self:null, username:null};
const canManageChannel = () => !room || Boolean(state.data?.can_manage ?? state.group?.can_manage);
const canSetAdmin = member => !room && member?.agent==='human' && member.admin!=null;
let submitDialog = null, busy = false, closeReturn = null, noticeTimer, focusSearch = false;
const dialog = $('dialog');
const drafts = new Map(), sending = new Set();
const messageInput = $('chat-body'), mentionList = $('mention-list'), syncOnlyInput = $('sync-only');
let composerGroup = null, mentionMenu = null, composing = false, nativeLineBreak = false;
let liveView = null, lastRefresh = 0;
let channelShell = null, refreshing = false;
let recoveryAttempt = null;
let memberInvite = null, useExistingAccount = false;
let memberRoomsChecked = -Infinity, memberRoomsRequest = 0;
// Each timeline scope keeps its own pages and reading position within the current channel.
const pages = new Map();
const agentScope = history => (history?.my_agents || []).join(',');
function pageKey(view,scope) {
  if (view==='announcement') return view;
  if (view==='topics') return 'topics/'+scope;
  return scope==='mine'?'mine':'history';
}
function channelPage(view,topic,query,scope='all') {
  const key=pageKey(view,scope);
  const cacheable=view!=='private' && !topic && !query;
  const cached=cacheable && pages.get(key);
  if (cached) {
    if (key!=='mine' || !cached.history || !state.data?.members) return cached;
    const agents=state.data.members.filter(m=>m.human_binding==='mine').map(m=>m.id).sort().join(',');
    if (agentScope(cached.history)===agents) return cached;
  }
  const page={history:null,older:[],more:null,position:null,updated:0};
  if (cacheable) pages.set(key,page);
  return page;
}
function setHistory(page,history) {
  // Keep the page the reader scrolled into when new messages push it off the first page.
  if (agentScope(page.history)!==agentScope(history)) {
    page.older=[]; page.more=null;
  } else if (page.older.length && page.history) {
    const known=new Set([...history.messages,...page.older].map(m=>m.id));
    page.older.push(...page.history.messages.filter(m=>!known.has(m.id)));
  }
  page.history=history;
  page.updated=performance.now();
}
function warmHistory(group,revision,scope) {
  const page=channelPage('history',null,'',scope);
  if (page.history || page.pending) return;
  page.pending=api('history',{group,scope}).then(history=>{
    if (pages.get(pageKey('history',scope))!==page || page.history) return;
    setHistory(page,history); page.revision=revision;
    if (state.page===page) render();
  }).catch(()=>{ /* Opening the tab retries a failed prefetch. */ }).finally(()=>{page.pending=null;});
}
const requestId = () => Array.from(crypto.getRandomValues(new Uint32Array(4)),n=>n.toString(16).padStart(8,'0')).join('');
function draftFor(group) {
  if (!drafts.has(group)) drafts.set(group,{body:'',targets:[],syncOnly:false,parent:null,request:requestId(),error:''});
  return drafts.get(group);
}
function composerMembers() {
  return (state.data?.members || []).filter(m=>m.id!==state.self?.id);
}
function memberMentions(body,members,selected) {
  if (!/[@＠]/u.test(body)) return [];
  // Keep an explicitly chosen identity if its name changes or is reused while the draft is open.
  const names=[...selected,...members];
  names.sort((a,b)=>b.name.length-a.name.length);
  const mentions=[];
  for (const at of body.matchAll(/[@＠]/gu)) {
    const lastEnd=mentions.at(-1)?.end ?? -1;
    if (at.index<lastEnd || (at.index!==lastEnd && /[\w.+%-]/u.test(body.charAt(at.index-1)))) continue;
    const tail=body.slice(at.index+1);
    const member=names.find(m=>tail.startsWith(m.name) && /^(?:$|[\s@＠，,。.!！?？、:：;；()（）])/u.test(tail.slice(m.name.length)));
    if (member) mentions.push({member,start:at.index,end:at.index+1+member.name.length});
  }
  return mentions;
}
function closeMentions() {
  mentionMenu=null;
  mentionList.hidden=true;
  messageInput.removeAttribute('aria-activedescendant');
}
function highlightMention(index,scroll=false) {
  mentionMenu.active=index;
  const option=mentionList.children[index];
  mentionList.querySelector('[aria-selected=true]')?.setAttribute('aria-selected','false');
  option.setAttribute('aria-selected','true');
  messageInput.setAttribute('aria-activedescendant',option.id);
  if (scroll) option.scrollIntoView({block:'nearest'});
}
function updateMentions() {
  if (!composerGroup || messageInput.disabled || composing || document.activeElement!==messageInput ||
      messageInput.selectionStart!==messageInput.selectionEnd || draftFor(composerGroup).parent) return closeMentions();
  const end=messageInput.selectionStart;
  const prefix=messageInput.value.slice(0,end), members=composerMembers();
  const match=prefix.match(/[@＠]([^\s@＠]*)$/u);
  if (!match) return closeMentions();
  const query=match[1].toLocaleLowerCase(), start=end-match[0].length;
  if (/[\w.+%-]/u.test(prefix.charAt(start-1)) &&
      memberMentions(prefix.slice(0,start),members,draftFor(composerGroup).targets).at(-1)?.end!==start) return closeMentions();
  const options=members.filter(m=>m.name.toLocaleLowerCase().includes(query));
  if (!options.length) return closeMentions();
  const key=JSON.stringify([start,end,options.map(m=>[m.id,m.name,m.agent])]);
  if (mentionMenu?.key===key) return;
  const activeId=mentionMenu?.options[mentionMenu.active]?.id;
  mentionMenu={key,start,end,options,active:0};
  mentionList.replaceChildren(...options.map(m=>node('div',{id:'mention-'+m.id,role:'option','data-id':m.id,'aria-selected':'false'},
    avatar(m.name,m.agent,true),node('span',{},m.name),node('small',{},clientName(m.agent)))));
  mentionList.hidden=false;
  highlightMention(Math.max(0,options.findIndex(m=>m.id===activeId)));
}
function insertMention(member,start,end) {
  const draft=draftFor(composerGroup);
  const existing=memberMentions(draft.body,composerMembers(),draft.targets).find(m=>m.start===start);
  if (existing) end=Math.max(end,existing.end);
  const space=/^[ \t]/.test(draft.body.slice(end));
  const text='@'+member.name+(space?'':' ');
  closeMentions();
  if (draft.body.length+text.length-(end-start)>messageInput.maxLength) {
    draft.error='消息太长，无法插入成员名称。'; syncComposer(); return;
  }
  messageInput.setRangeText(text,start,end,'end');
  if (space) messageInput.setSelectionRange(messageInput.selectionEnd+1,messageInput.selectionEnd+1);
  draft.targets=[member,...draft.targets.filter(m=>m.id!==member.id)];
  messageInput.focus(); editDraft();
}
function syncComposer() {
  const g=state.group?.id, d=state.data;
  const visible=state.logged && g && d && state.view==='history' && !state.query && !state.topic;
  $('composer').hidden=!visible;
  if (!visible) { composerGroup=null; closeMentions(); return; }
  const draft=draftFor(g), members=composerMembers();
  if (g!==composerGroup) { closeMentions(); messageInput.value=draft.body; composerGroup=g; }
  if (!draft.parent) {
    const mentions=memberMentions(draft.body,members,draft.targets);
    draft.targets=[...new Map(mentions.map(({member})=>[member.id,member])).values()];
  }
  const missing=draft.targets.find(target=>!members.some(m=>m.id===target.id));
  const issue=missing && !draft.syncOnly ? '@'+missing.name+' 已离开频道，请重新选择成员。' : '';
  $('reply-context').hidden=!draft.parent;
  if (draft.parent) $('reply-parent').textContent='答复问题 #'+draft.parent;
  syncOnlyInput.parentElement.hidden=Boolean(draft.parent);
  syncOnlyInput.checked=draft.syncOnly;
  messageInput.disabled=d.paused || sending.has(g);
  syncOnlyInput.disabled=messageInput.disabled || Boolean(draft.parent);
  if (d.paused) messageInput.placeholder='频道已暂停，恢复讨论后可发言';
  else if (draft.syncOnly) messageInput.placeholder='同步到 # '+state.group.name+'，无需成员答复…';
  else if (!members.length) messageInput.placeholder='勾选「仅同步」记录消息，或先邀请成员参与讨论…';
  else messageInput.placeholder='发消息到 # '+state.group.name+'，或输入 @ 选择成员…';
  $('chat-send').disabled=messageInput.disabled || (!members.length && !draft.syncOnly) || !draft.body.trim() || Boolean(issue);
  $('chat-error').textContent=issue || draft.error;
  if (mentionMenu) updateMentions();
}
function editDraft() {
  if (!composerGroup) return;
  Object.assign(draftFor(composerGroup),{body:messageInput.value,request:requestId(),error:''});
  syncComposer();
}
messageInput.addEventListener('input',()=>{ editDraft(); updateMentions(); });
syncOnlyInput.addEventListener('change',()=>{
  if (!composerGroup) return;
  draftFor(composerGroup).syncOnly=syncOnlyInput.checked;
  editDraft(); messageInput.focus();
});
messageInput.addEventListener('click',updateMentions);
messageInput.addEventListener('blur',()=>{ composing=false; nativeLineBreak=false; closeMentions(); });
messageInput.addEventListener('compositionstart',()=>{ composing=true; closeMentions(); });
messageInput.addEventListener('compositionend',()=>{ composing=false; updateMentions(); });
mentionList.addEventListener('mousedown',e=>e.preventDefault());
mentionList.addEventListener('click',e=>{
  const member=mentionMenu?.options.find(m=>m.id===e.target.closest('[role=option]')?.dataset.id);
  if (member) insertMention(member,mentionMenu.start,mentionMenu.end);
});
messageInput.addEventListener('keyup',e=>{
  nativeLineBreak=false;
  if (['ArrowLeft','ArrowRight','Home','End'].includes(e.key)) updateMentions();
});
function confirmComposer() {
  if (mentionMenu) insertMention(mentionMenu.options[mentionMenu.active],mentionMenu.start,mentionMenu.end);
  else $('composer').requestSubmit();
}
messageInput.addEventListener('keydown',e=>{
  const ime=composing || e.isComposing || e.keyCode===229;
  nativeLineBreak=e.key==='Enter' && (ime || e.shiftKey || e.altKey);
  if (ime || e.shiftKey || e.altKey) return;
  if (mentionMenu) updateMentions();
  if (mentionMenu && (e.key==='ArrowDown' || e.key==='ArrowUp')) {
    e.preventDefault();
    const count=mentionMenu.options.length, step=e.key==='ArrowDown'?1:-1;
    highlightMention((mentionMenu.active+step+count)%count,true); return;
  }
  if (mentionMenu && e.key==='Escape') { e.preventDefault(); closeMentions(); return; }
  if (e.key!=='Enter' && !(mentionMenu && e.key==='Tab')) return;
  e.preventDefault();
  if (!e.repeat) confirmComposer();
});
// Some virtual keyboards only issue the editing event for their return key.
messageInput.addEventListener('beforeinput',e=>{
  if (e.inputType!=='insertLineBreak' && e.inputType!=='insertParagraph') return;
  if (nativeLineBreak || composing || e.isComposing || !e.cancelable) return;
  e.preventDefault(); confirmComposer();
});
$('composer').addEventListener('submit',async e=>{
  e.preventDefault();
  if (!composerGroup || $('chat-send').disabled) return;
  const g=composerGroup, draft=draftFor(g);
  sending.add(g); syncComposer();
  try {
    const payload={group:g,body:draft.body.trim(),request:draft.request};
    if (draft.parent) await api('speak',{...payload,parent:draft.parent});
    else if (draft.syncOnly) await api('broadcast',{...payload,mentions:draft.targets.map(m=>m.id)});
    else if (draft.targets.length>1) await api('group_question',{...payload,targets:draft.targets.map(m=>m.id)});
    else await api('speak',{...payload,target:draft.targets[0]?.id || ''});
    Object.assign(draft,{body:'',targets:[],syncOnly:false,parent:null,error:'',request:requestId()});
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
function secretToggle(id) {
  return action('显示','toggle-secret','text-button',{'data-id':id,'aria-controls':id,'aria-pressed':'false'});
}
function toggleSecret(input, button) {
  const reveal=input.type==='password';
  input.type=reveal?'text':'password';
  button.textContent=reveal?'隐藏':'显示';
  button.setAttribute('aria-pressed',String(reveal));
}
privateChat.install();
$('reply-context').append(action('取消答复','cancel-reply','text-button'));
for (const id of ['owner-key','account-password','member-password']) $(id).after(secretToggle(id));
if (!room) $('nav-heading').append(action('＋','create','square',{'aria-label':'新建群聊',title:'新建群聊'}));
$('theme-toggle').before(action('使用手册','open-manual','manual-open',{'aria-haspopup':'dialog'}));
if (room) {
  const settings=action('','account-settings','account-identity',{id:'account-identity','aria-haspopup':'dialog'});
  settings.append(...$('account').children,node('span',{class:'account-caret','aria-hidden':'true'},'›'));
  $('account').append(settings,action('退出登录','member-logout','text-button'));
} else $('account').append(action('退出登录','logout','text-button'));
document.body.classList.toggle('member-room',Boolean(room));
function mobileNav(open) {
  const shown = mobile.matches && open;
  $('shell').classList.toggle('nav-open', shown);
  $('nav-scrim').hidden = !shown;
  $('sidebar').inert = mobile.matches && !shown;
  $('main').inert = shown;
  $('nav-toggle').setAttribute('aria-expanded', String(shown));
  if (shown) { $('channel-filter').focus(); void refreshMemberRooms(true); }
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
  const existing=rooms.find(item=>item.id===id);
  if (name ? existing?.name===name : !existing) return;
  const updated=rooms.filter(item=>item.id!==id);
  if (name) updated.unshift({id,name});
  try { localStorage.setItem('diesheng.rooms',JSON.stringify(updated)); } catch {}
}
async function refreshMemberRooms(force=false) {
  if (!room || !state.logged || !state.self || (!force && performance.now()-memberRoomsChecked<30000)) return;
  memberRoomsChecked=performance.now();
  const request=++memberRoomsRequest, activeRoom=room, member=state.self.id;
  if (state.username) {
    try {
      const account=await accountApi('groups');
      if (request!==memberRoomsRequest || room!==activeRoom || state.self?.id!==member) return;
      state.groups=account.groups;
      nav();
    } catch { /* The current channel also checks its session. */ }
    return;
  }
  const previous=new Map(state.groups.map(group=>[group.id,group]));
  const channels=await Promise.all(rememberedRooms().filter(item=>item.id!==activeRoom).map(async item=>{
    try {
      return (await memberRoomSession(item.id)).group;
    } catch (error) {
      if (error.code==='channel_dissolved') {
        rememberRoom(item.id,null);
        return null;
      }
      return error.status===401 || error.status===403 ? null : previous.get(item.id);
    }
  }));
  if (request!==memberRoomsRequest || room!==activeRoom || state.self?.id!==member) return;
  const current=state.groups.find(group=>group.id===activeRoom);
  state.groups=[current,...channels].filter(Boolean).sort((a,b)=>a.name.localeCompare(b.name,'zh-CN'));
  nav();
}
function showKnownRooms(saved=rememberedRooms()) {
  const rows=saved.map(item=>{
    const row=node('div',{class:'known-room'});
    const title=name=>node('span',{class:'known-room-name'},'# '+name);
    async function check() {
      row.replaceChildren(title(item.name),node('span',{class:'sub',role:'status'},'正在核对…'));
      try {
        const {group}=await memberRoomSession(item.id);
        if (!row.isConnected) return;
        row.replaceChildren(node('a',{href:'/room/'+group.id,'aria-label':'# '+group.name},title(group.name),
          node('span',{class:'sub','aria-hidden':'true'},'已登录')));
      } catch (error) {
        if (!row.isConnected) return;
        if (error.code==='channel_dissolved') {
          rememberRoom(item.id,null);
          row.replaceChildren(title(item.name),node('span',{class:'sub'},'已解散'));
          return;
        }
        const signedOut=[401,403,404].includes(error.status);
        const control=signedOut?action('恢复登录','recover-code','text-button',{'data-id':item.id,
          'aria-label':'恢复 '+item.name+' 的登录'}):node('button',{type:'button',class:'text-button',
            'aria-label':'重试检查 '+item.name+' 的登录'},'重试检查');
        if (!signedOut) control.addEventListener('click',()=>void check());
        row.replaceChildren(title(item.name),control);
      }
    }
    return {row,check};
  });
  $('known-links').replaceChildren(...rows.map(({row})=>row));
  for (const {check} of rows) void check();
}
function clearWorkspace() {
  stopLiveView();
  clearTimeout(noticeTimer); $('notice').textContent='';
  $('boot').hidden=true;
  mobileNav(false); $('channel-filter').value=''; navSnapshot=''; searchOpen=false; state.group=null;
  state.logged=false; state.serial++; state.groups=[]; state.data=null; state.page=null; state.error='';
  pages.clear(); channelShell=null; refreshing=false; focusSearch=false;
  if (dialog.open) dialog.close();
  $('workspace').replaceChildren(); $('group-list').replaceChildren(); $('roster').replaceChildren(); $('mobile-actions').replaceChildren();
  drafts.clear(); composerGroup=null; closeMentions(); $('composer').hidden=true; privateChat.clear();
  $('shell').hidden=true; $('member-entry').hidden=true; $('known-rooms').hidden=true; $('login').hidden=true;
  state.self=null; state.username=null;
  memberRoomsChecked=-Infinity; memberRoomsRequest++;
}
function entryActions(...buttons) {
  $('member-entry-actions').replaceChildren(...buttons);
  $('member-entry-actions').hidden=!buttons.length;
}
function memberScreen() {
  $('member-entry').hidden=false;
  for (const id of ['member-form','account-login-form','member-recover-link','member-retry','known-rooms']) $(id).hidden=true;
  for (const id of ['member-error','account-error','member-recover-error']) $(id).textContent='';
  for (const id of ['member-password','account-password']) {
    const input=$(id);
    input.value='';
    if (input.type==='text') toggleSecret(input,input.nextElementSibling);
  }
  entryActions();
}
function showAccountHome(account=null) {
  memberScreen();
  $('member-caption').textContent=account?'@'+account.username:'叠声';
  $('member-channel').textContent=account?'我的频道':'同事账号登录';
  document.title=$('member-channel').textContent+' · 叠声';
  $('member-help').textContent=account?(account.groups.length?'':'打开同事邀请链接，加入你的第一个频道。'):
    '首次使用请打开群主发给你的邀请链接，创建账号并加入频道。';
  if (account) {
    $('known-rooms').hidden=!account.groups.length;
    $('known-links').replaceChildren(...account.groups.map(group=>node('div',{class:'known-room'},
      node('a',{href:'/room/'+group.id},'# '+group.name))));
    entryActions(action('账号设置','account-settings','text-button',{'data-username':account.username,'aria-haspopup':'dialog'}),
      action('退出登录','member-logout','text-button'),node('a',{href:'/#owner'},'群主登录'));
  } else {
    $('account-login-form').hidden=false;
    const saved=rememberedRooms();
    $('known-rooms').hidden=!saved.length;
    if (saved.length) showKnownRooms(saved);
    entryActions(action('使用旧恢复码','recover-code','text-button'),node('a',{href:'/#owner'},'群主登录'));
  }
}
function showLogin(memberNotice='') {
  clearWorkspace();
  if (room) { void showMemberEntry(memberNotice); return; }
  if (location.hash!=='#owner' && !location.hash.startsWith('#/')) { showAccountHome(); return; }
  $('login').hidden=false;
  document.title='群主登录 · 叠声';
}
async function requestApi(scope,name,data,signal) {
  const options={method:'POST',headers:{'Content-Type':'application/json',Accept:'application/json'},
    body:JSON.stringify(data),signal};
  let retry=['groups','detail','history','topics','private-list','private-read','private-topics','recover_preview','recover','legacy_status','logout'].includes(name);
  for (;;) {
    let resp, result;
    try {
      resp=await fetch('/api/'+scope+'/'+name,options);
      result=await resp.json();
    } catch {
      // A disconnected service or proxy can return headers without a complete JSON body.
    }
    if (resp?.ok && result && typeof result==='object' && !Array.isArray(result)) {
      if (data.scope==='mine' && (result.history || result).scope!=='mine') throw new Error('当前服务尚未启用个人消息筛选。');
      return result;
    }
    const status=resp?.status;
    if (retry && !signal.aborted && (!resp || resp.ok || [502,503,504].includes(status))) {
      retry=false;
      await new Promise(resolve=>setTimeout(resolve,250));
      continue;
    }
    const message=resp?.ok?'频道数据接收不完整，请重新加载。':'暂时无法连接频道服务，请稍后重试。';
    const detail=typeof result?.error==='string' && result.error || message;
    const error=new Error(scope==='account' && status===404?
      '当前服务器未提供同事账号登录，请核对叠声的服务地址和版本。':detail);
    error.status=status;
    error.code=result?.code;
    throw error;
  }
}
const accountApi=(name,data={})=>requestApi('account',name,data,AbortSignal.timeout(12000));
async function api(name, data={}, signal=AbortSignal.timeout(12000), memberRoom=room, resetOnUnauthorized=true) {
  try { return await requestApi(memberRoom?'member/'+memberRoom:'owner',name,data,signal); }
  catch (error) {
    if (error.status===401 && resetOnUnauthorized && memberRoom===room && !['login','preview','join','recover_preview','recover'].includes(name)) showLogin();
    throw error;
  }
}
async function memberRoomSession(id) {
  const result=await api('groups',{},undefined,id,false);
  const group=result.groups?.find(item=>item.id===id);
  if (!group || !result.self?.id) throw new Error('频道登录状态读取不完整，请重试');
  return {group,self:result.self,username:result.username};
}
const memberCode=()=>new URLSearchParams(location.hash.slice(1)).get('invite');
const memberRecoveryLink=()=>room && new URLSearchParams(location.hash.slice(1)).get('recover');
const credentials=prefix=>({username:$(prefix+'-username').value.trim(),password:$(prefix+'-password').value});
function renderInviteForm() {
  const {username,member,legacy}=memberInvite;
  const home=node('a',{href:'/#channels'},'我的频道');
  if (legacy && !member) {
    $('member-form').hidden=true;
    $('member-help').textContent='此浏览器已有本频道的旧身份。关联账号后可继续使用原来的记录。';
    entryActions(action('设置账号','setup-account','text-button'),home);
    return;
  }
  $('member-form').hidden=false;
  $('member-credentials').hidden=Boolean(username);
  $('member-nickname-field').hidden=Boolean(member);
  $('member-password').autocomplete=useExistingAccount?'current-password':'new-password';
  for (const id of ['member-user-help','member-password-help']) $(id).hidden=useExistingAccount;
  $('member-submit').textContent=member?'进入频道':username?'加入频道':useExistingAccount?'登录并加入':'创建账号并加入';
  $('member-help').textContent=member?'你的账号已加入这个频道。':username?'用 @'+username+' 加入这个频道。':
    '创建同事账号，之后可用账号登录已加入的频道。';
  if (username) entryActions(home);
  else entryActions(action(useExistingAccount?'创建新账号':'已有账号？登录加入','invite-account-mode','text-button'),home);
}
async function showMemberEntry(memberNotice) {
  memberScreen();
  memberInvite=null; useExistingAccount=false;
  const activeRoom=room;
  const home=node('a',{href:'/#channels'},'我的频道');
  entryActions(action('使用旧恢复码','recover-code','text-button'),home);
  const recovery=memberRecoveryLink();
  if (recovery) {
    if (recoveryAttempt?.code!==recovery) recoveryAttempt={code:recovery,request:requestId()};
    $('member-caption').textContent='同事恢复链接';
    $('member-channel').textContent='恢复原身份'; document.title='恢复原身份 · 叠声';
    $('member-help').textContent='正在核对恢复链接…';
    try {
      const result=await api('recover_preview',{code:recovery});
      if (recovery!==memberRecoveryLink()) return;
      $('member-channel').textContent='恢复 '+result.name+' 的身份';
      $('member-help').textContent='恢复 #'+result.channel+' 的旧身份后，可以把原记录关联到同事账号。';
      $('member-recover-link').hidden=false;
    } catch (error) {
      if (recovery!==memberRecoveryLink()) return;
      $('member-help').textContent=error.message; $('member-retry').hidden=error.status===410;
    }
    return;
  }
  recoveryAttempt=null;
  const code=memberCode();
  if (!code) {
    const saved=rememberedRooms().find(item=>item.id===room);
    $('member-caption').textContent=saved?'# '+saved.name:'叠声';
    $('member-channel').textContent='同事账号登录'; document.title='同事账号登录 · 叠声';
    $('member-help').textContent=memberNotice || '登录后可打开已加入的频道。首次加入请打开同事邀请。';
    $('account-login-form').hidden=false;
    try {
      const legacy=await api('legacy_status',{},undefined,activeRoom,false);
      if (room!==activeRoom || state.logged || memberCode() || memberRecoveryLink()) return;
      $('member-help').textContent='可把 #'+legacy.channel+' 的旧身份 @'+legacy.name+' 关联到你的账号。';
      $('account-login-form').hidden=Boolean(legacy.username);
      entryActions(action('关联旧身份','setup-account','text-button'),home);
    } catch { /* The legacy action is available only when the browser holds that identity. */ }
    return;
  }
  $('member-caption').textContent='同事邀请';
  $('member-channel').textContent='加入频道'; document.title='加入频道 · 叠声';
  $('member-help').textContent='正在读取邀请…';
  try {
    const invite=await api('preview',{code});
    if (room!==activeRoom || code!==memberCode() || state.logged) return;
    $('member-channel').textContent='# '+invite.name;
    memberInvite=invite;
    renderInviteForm();
  } catch (error) {
    if (room!==activeRoom || code!==memberCode() || state.logged) return;
    $('member-help').textContent=error.message; $('member-retry').hidden=error.status===410;
    if (error.code==='channel_dissolved') {
      $('member-channel').textContent='频道已解散'; entryActions(home);
    } else $('account-login-form').hidden=false;
  }
}
$('member-retry').addEventListener('click',()=>refresh());
$('member-retry').disabled=false;
$('account-login-form').addEventListener('submit',async e=>{
  e.preventDefault();
  const submit=e.currentTarget.querySelector('[type="submit"]');
  if (submit.disabled) return;
  submit.disabled=true; $('account-error').textContent='';
  try {
    await accountApi('login',credentials('account'));
    await accountApi('groups');
    $('account-password').value='';
    history.replaceState(null,'',room?location.pathname:'/#channels');
    await refresh();
  } catch (error) {
    $('account-error').textContent=error.status===401 && error.code!=='invalid_credentials'?
      '浏览器未保存登录会话，请允许本站保存 Cookie 后重试。':error.message;
  } finally { submit.disabled=false; }
});
$('member-form').addEventListener('submit',async e=>{
  e.preventDefault();
  const nickname=$('member-nickname'), submit=$('member-submit');
  if (submit.disabled || !memberInvite || !room) return;
  if (!memberInvite.member && !nickname.value.trim()) {
    nickname.setAttribute('aria-invalid','true'); nickname.focus();
    $('member-error').textContent='请填写你的昵称。'; return;
  }
  nickname.removeAttribute('aria-invalid');
  const activeRoom=room, code=memberCode(), invite=memberInvite;
  submit.disabled=true; $('member-error').textContent='';
  try {
    if (!invite.username && useExistingAccount) {
      const {username}=await accountApi('login',credentials('member'));
      if (room!==activeRoom || code!==memberCode()) return;
      invite.username=username;
      renderInviteForm();
    }
    const operation=invite.username?'join':'register';
    const payload={code,name:nickname.value.trim(),...(operation==='register'?credentials('member'):{username:invite.username})};
    const result=await api(operation,payload,undefined,activeRoom,false);
    const confirmed=await memberRoomSession(activeRoom);
    if (!confirmed.username || confirmed.self.id!==result.member) throw new Error('无法确认频道登录，请用账号重新登录。');
    if (room!==activeRoom || code!==memberCode()) return;
    $('member-password').value='';
    history.replaceState(null,'',location.pathname);
    await refresh();
  } catch (error) {
    $('member-error').textContent=error.message;
    if (error.code==='username_taken') $('member-username').focus();
  }
  finally { submit.disabled=false; }
});
async function restoreMember(group, attempt) {
  const result=await api('recover',attempt,undefined,group);
  let session;
  try {
    session=await api('legacy_status',{},undefined,group,false);
  } catch (error) {
    if (error.status!==401) throw error;
    throw new Error('此浏览器未保存频道会话。请允许叠声保存 Cookie 后，在当前页面重试。');
  }
  if (session.member!==result.member) throw new Error('无法确认已恢复的身份，请重试');
  rememberRoom(group,session.channel);
}
async function finishMemberRecovery(group) {
  if (room!==group) { location.replace('/room/'+group); return; }
  history.replaceState(null,'',location.pathname);
  recoveryAttempt=null;
  await refresh();
  if (state.logged) notice('已恢复原身份，可在侧栏设置账号');
}
$('member-recover-link').addEventListener('submit',async e=>{
  e.preventDefault();
  const submit=e.currentTarget.querySelector('button'), attempt=recoveryAttempt;
  if (submit.disabled || !attempt || attempt.code!==memberRecoveryLink()) return;
  submit.disabled=true; $('member-recover-error').textContent='';
  try {
    const group=room;
    await restoreMember(group,attempt);
    if (recoveryAttempt===attempt) await finishMemberRecovery(group);
  } catch (error) {
    if (recoveryAttempt===attempt) $('member-recover-error').textContent=error.message;
  } finally { submit.disabled=false; }
});
function empty(title, hint, button) {
  return node('div',{class:'empty'},node('h2',{},title),node('p',{},hint),button);
}
function feedPreferenceKey(group) {
  const viewer=room?state.self?.id:'host';
  return group && viewer?'diesheng.feed.'+group+'.'+viewer:null;
}
function preferredScope(group) {
  const key=feedPreferenceKey(group);
  try { return key && localStorage.getItem(key)==='mine'?'mine':'all'; } catch { return 'all'; }
}
function url(g, view='history', topic=null, query='', scope='') {
  const params=new URLSearchParams();
  if (query) params.set('q',query);
  if (scope && view==='history') params.set('scope',scope);
  const search=params.toString();
  return '#/'+g+'/'+view+(topic ? '/'+topic : '')+(search?'?'+search:'');
}
function topicUrl(group,topic=null,category=state.category,status=state.topicStatus,before=state.before) {
  const params=new URLSearchParams();
  if (category) params.set('category',category);
  if (status) params.set('status',status);
  if (before) params.set('before',before);
  return url(group,'topics',topic)+(params.size?'?'+params:'');
}
function openRoute(hash,entry=null) {
  if (location.hash===hash) return;
  history.pushState(entry,'',hash);
  activateRoute();
}
function activateRoute() {
  stopLiveView();
  const next=roomFromPath();
  if (room && next && next!==room) {
    room=next; memberRoomsChecked=-Infinity; memberRoomsRequest++;
    state.logged=false; state.self=null; state.username=null; state.group=null; state.data=null; state.page=null; state.privateRoute=null;
    pages.clear(); channelShell=null; composerGroup=null; closeMentions();
    $('composer').hidden=true; $('roster').hidden=true; $('mobile-actions').replaceChildren();
    $('workspace').replaceChildren(node('p',{class:'loading',role:'status'},'正在读取频道…'));
    privateChat.sync();
  }
  void refresh('navigate');
}
function searchRoute(query) {
  const next=url(state.group.id,state.view,state.topic,query,state.scope);
  if (location.hash===next) return;
  focusSearch=true;
  openRoute(next,state.topic?history.state:null);
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
    const differentRoom=room && g.id!==room;
    return node('a',{href:differentRoom?'/room/'+g.id:url(g.id),'data-member-room':differentRoom?g.id:null,
      'aria-label':g.name,'aria-current':g.id===state.group?.id?'page':null},
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
    const editable=canManageChannel() && m.agent!=='host';
    const card=node(editable?'button':'div',editable?{type:'button',class:'person','data-action':'profile','data-id':m.id,'aria-label':'设置成员 '+m.name}:{class:'person'},
      node('span',{class:'presence-avatar'},avatar(m.name,m.agent,true),node('i',{class:'dot '+(m.online?'online':''),'aria-label':m.online?'在线':'离线'})),
      node('span',{class:'person-details'},node('strong',{},m.name),node('small',{},node('span',{},clientName(m.agent)),' · '+listeningLabel(m)),
        m.role && m.agent!=='host'?node('span',{class:'person-role'},m.role):null));
    roster.append(node('div',{class:'roster-member'},card,privateChat.memberLink(state.group.id,m,true)));
  }
  if (!members.length) roster.append(node('p',{class:'muted'},'Agent 加入后会显示在这里。'));
  roster.append(node('div',{class:'inbox-summary'},node('h3',{},'项目收件箱'),
    node('div',{},node('span',{},'待处理'),node('b',{},String(members.reduce((n,m)=>n+m.pending,0)))),
    node('div',{},node('span',{},'处理中'),node('b',{},String(members.reduce((n,m)=>n+m.processing,0))))));
}
function msgEntry(m) {
  const self=room?state.self?.id:state.data.self;
  const own=m.sender===self, people=m.recipients || [];
  const myAgent=state.page.history?.my_agents?.includes(m.sender);
  const groupQuestion=m.kind==='group_question';
  let audience=m.recipient?'@'+m.recipient:'';
  if (groupQuestion || m.kind==='broadcast') audience=kinds[m.kind];
  else if (people.length) audience='全体成员';
  let text=captions[m.receipt] || m.receipt;
  if (people.length) text=people.filter(p=>p.status==='replied').length+'/'+people.length+' 已回复';
  else if (m.kind==='answer' && m.receipt==='pending') text='待签收';
  else if (m.kind!=='answer' && m.receipt==='received') text='已签收 · 处理中';
  const meta=node('div',{class:'meta'},m.quick_reply?node('span',{class:'client-label',title:'这条历史消息由旧版预设规则生成'},'预设回复'):null,
    node('span',{class:'receipt '+(['received','replied'].includes(m.receipt)?'positive':'')},text));
  if (!own && !state.topic && state.scope==='mine' && m.kind==='broadcast' && (m.mentions_viewer || !myAgent))
    meta.prepend(node('span',{},m.mentions_viewer===0?'提到我的 Agent':'提到我'));
  if (!state.topic && m.kind!=='broadcast') meta.append(node('a',{href:url(state.group.id,'history',m.topic,'',state.scope),'data-open-topic':'true'},'话题 #'+m.topic));
  if (canManageChannel() && (m.receipt==='failed' || m.receipt==='uncertain')) meta.append(action('重新投递',m.receipt==='failed'?'retry-message':'requeue','text-button',{'data-id':m.id}));
  if (!own && !state.topic && !state.query && state.data.members.some(member=>member.id===m.sender)) {
    const canAnswer=status=>['pending','manual','received','uncertain'].includes(status);
    const branch=people.find(p=>p.member===self && canAnswer(p.status));
    const direct=m.target===self && ['question','followup'].includes(m.kind) && canAnswer(m.receipt);
    const parent=branch?.question || (direct?m.id:null);
    meta.append(action(parent?'答复问题':'回复','reply-to','text-button',{'data-id':m.sender,'data-parent':parent}));
  }
  const receipts=groupQuestion?node('ul',{class:'recipient-receipts','aria-label':'成员回执'},...people.map(p=>
    node('li',{},'@'+p.name+' · '+(recipientCaptions[p.status] || p.status)))):null;
  return node('article',{class:'chat-row'+(own?' own':''),'data-message':m.id},avatar(m.author,m.author_agent),
    node('div',{class:'chat-content'},node('div',{class:'chat-byline'},node('strong',{},own?'我':m.author),node('span',{class:'client-label'},clientName(m.author_agent)),
      myAgent?chip('我的 Agent','info'):null,
      audience?node('span',{class:'to'},audience):null,timestamp(m.created)),
      markdownBody(m.body),m.evidence?node('p',{class:'basis'},'依据　'+m.evidence):null,receipts,meta,
      m.error?node('p',{class:'error'},m.error):null));
}
function memberEntry(m) {
  const browser=m.agent==='human' || m.agent==='host';
  const editable=canManageChannel() && m.agent!=='host';
  const binding=m.human_binding;
  return node('article',{class:'entry'},node('div',{class:'row-title'},avatar(m.name,m.agent,true),node('strong',{},'@'+m.name),
    chip(m.online?'在线':'离线',m.online?'good':''),browser?null:chip(m.verified?'已验证收发':'项目已加入',m.verified?'info':''),
    m.admin?chip('管理员','info'):null,
    binding==='mine'?chip('我的 Agent','info'):binding==='bound'?chip('已绑定'):null,
    node('span',{class:'end'},privateChat.memberLink(state.group.id,m),
      binding==='unbound'?action('绑定给我','bind-agent','text-button',{'data-id':m.id}):
        binding==='mine'?action('解除绑定','unbind-agent','text-button',{'data-id':m.id}):null,
      editable?action('成员设置','profile','text-button',{'data-id':m.id}):null,
      editable && m.agent==='human' && !m.has_account?action('恢复身份','recovery-link','text-button',{'data-id':m.id}):null,
      canSetAdmin(m)?action(m.admin?'取消管理员':'设为管理员','admin-role','text-button',
        {'aria-haspopup':'dialog','data-id':m.id}):null,
      editable?action('移除','remove','text-button text-danger',{'data-id':m.id}):null)),
    m.role?node('p',{class:'body'},m.role):null,node('div',{class:'meta'},
      node('span',{},clientName(m.agent)),...(browser?[]:[node('span',{},listeningLabel(m)),node('span',{},m.sessions+' 个近期会话'),
      node('span',{},m.processing+' 条处理中'),node('span',{},m.pending+' 条待处理'),m.paused?chip('自动应答已暂停','warn'):null])));
}
function invitationEntry(i) {
  return node('article',{class:'entry'},node('div',{class:'row-title'},node('strong',{},i.kind==='human' && i.status!=='used'?'同事邀请':'@'+i.name),
    statusChip(i.status),i.status==='ready'?node('span',{class:'end'},action('撤销邀请','revoke','text-button text-danger',{'data-id':i.id})):null),
    i.role?node('p',{class:'body'},i.role):null,node('div',{class:'meta'},'生成',timestamp(i.created,true),'到期',timestamp(i.expires,true)));
}
function topicFilters(group) {
  const box=node('div',{class:'topic-filters'});
  function choice(label,options,selected) {
    const select=node('select',{'aria-label':label},node('option',{value:''},'全部'+label),
      ...Object.entries(options).map(([value,text])=>node('option',{value},text)));
    select.value=selected;
    box.append(node('label',{},label,select));
    return select;
  }
  const category=choice('分类',topicKinds,state.category), status=choice('状态',topicStates,state.topicStatus);
  box.addEventListener('change',()=>openRoute(topicUrl(group,null,category.value,status.value,null)));
  return box;
}
function topicRows(part,group,directory) {
  for (const t of directory.topics) {
    const title=node('a',{href:topicUrl(group,t.id)},t.title);
    const status=chip(topicStates[t.status],t.status==='attention'?'warn':'');
    part.append(node('article',{class:'entry topic-row','data-topic':t.id},
      node('div',{class:'row-title'},node('span',{class:'msg-id'},'#'+t.id),node('h3',{},title),status),
      node('div',{class:'meta'},node('span',{},topicKinds[t.category]),node('span',{},t.participants.join('、')),
        node('span',{},t.answers+' 条答复'),timestamp(t.last_at,true)),
      t.answers || t.latest!==t.title?node('p',{class:'topic-latest'},t.latest_author+' · '+t.latest):null));
  }
  const filtered=Boolean(state.category || state.topicStatus || state.before);
  if (!directory.topics.length) part.append(empty(filtered?'没有符合条件的话题':'还没有话题',
    filtered?'可以调整分类或状态。':'频道里发起提问后，可在这里跟进讨论。'));
  if (directory.more || filtered) {
    const links=node('div',{class:'topic-pages'});
    if (filtered) links.append(node('a',{href:topicUrl(group,null,'','',null)},'查看全部话题'));
    if (directory.more) links.append(node('a',{href:topicUrl(group,null,state.category,state.topicStatus,directory.topics.at(-1).last_id)},'下一页话题'));
    part.append(links);
  }
}
function announcementReceipts(members, receipts) {
  const agents=members.filter(member=>member.agent!=='host' && member.agent!=='human');
  if (!agents.length || !Array.isArray(receipts)) return null;
  const applied=new Set(receipts), count=agents.filter(agent=>applied.has(agent.id)).length;
  const results=node('ul',{},...agents.map(agent=>node('li',{},node('span',{},agent.name),
    applied.has(agent.id)?chip('已写入','good'):chip('待写入','warn'))));
  const help=count<agents.length?node('p',{class:'hint'},'Agent 使用新版监听器连接或恢复时会补收。 ',
    node('a',{href:'/agent.md#同步群公告',target:'_blank',rel:'noopener noreferrer'},'查看同步说明')):null;
  return node('details',{class:'announcement-receipts'},
    node('summary',{},'已写入 '+count+'/'+agents.length+' 个 Agent 项目'),results,help);
}
function panel() {
  const {data:d,view,group:g}=state;
  const listing=view==='topics' && !state.topic, thread=view==='topics' && Boolean(state.topic);
  const part=node('section',{class:'panel '+view+(thread?' history':''),'aria-label':views[view]});
  const reading=view==='history' || thread;
  const waiting=!d || (listing && !state.page.directory) || (reading && !state.page.history);
  if (waiting && !state.error) part.setAttribute('aria-busy','true');
  const head=node('div',{class:'section-heading'},node('h2',{},state.topic?'话题 #'+state.topic:views[view]));
  if (state.topic) {
    const from=window.history.state?.feedHash, list=url(g.id,'history');
    const fromList=typeof from==='string' && (from===list || from.startsWith(list+'?'));
    const back=fromList?from:url(g.id,'history',null,'',state.scope);
    head.append(thread?node('a',{href:topicUrl(g.id)},'返回话题列表'):node('a',{href:back},'返回群记录'));
  }
  if (state.topic) part.append(head);
  if (listing) part.append(topicFilters(g.id));
  if (view==='history' && !state.topic) {
    const scopes=node('nav',{class:'feed-scope','aria-label':'消息范围'});
    for (const [scope,label] of [['all','全部消息'],['mine','只看与我有关']]) {
      scopes.append(node('a',{href:url(g.id,'history',null,state.query,scope),'data-feed-scope':scope,
        'aria-current':state.scope===scope?'true':null},label));
    }
    part.append(scopes);
  }
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
    if (searchOpen || state.query) part.append(form);
  }
  if (waiting) {
    part.append(state.error?empty('暂时无法读取'+views[view],state.error,action('重新加载','retry')):
      node('p',{class:'loading',role:'status'},'正在读取'+views[view]+'…'));
    return part;
  }
  if (listing) { topicRows(part,g.id,state.page.directory); return part; }
  if (reading) {
    const {history,older:previous,more}=state.page;
    const map=new Map([...previous,...history.messages].map(m=>[m.id,m]));
    const messages=[...map.values()].sort((a,b)=>a.id-b.id);
    const list=node('div',{class:'chat-log',role:'log','aria-label':'群消息记录',tabindex:'0'});
    if (more ?? history.more) list.append(node('div',{class:'load'},action('查看更早消息','older')));
    let lastDay='';
    for (const message of messages) {
      const date=new Date(message.created*1000);
      const day=date.toLocaleDateString('zh-CN',{month:'long',day:'numeric',weekday:'long'});
      if (day!==lastDay) {
        list.append(node('div',{class:'day-divider'},node('span',{},day)));
        lastDay=day;
      }
      list.append(msgEntry(message));
    }
    part.append(list);
    if (!messages.length) {
      if (state.query) part.append(empty('没有匹配的记录','可尝试字段名、分支名或测试名。'));
      else if (!state.topic && state.scope==='mine') list.append(empty('还没有与你有关的消息','你和已绑定 Agent 发出、收到或被点名的消息会显示在这里。可以在频道成员中绑定 Agent。',
        node('a',{href:url(g.id,'history',null,'','all'),'data-feed-scope':'all'},'查看全部消息')));
      else list.append(empty(state.topic?'暂无可见的话题记录':'开始群聊',
        state.topic?'此话题的记录可能已超过保留期。':(d.members.length?'给全体发一句话，或输入 @ 选择成员。':'添加一位 Agent，邀请它参与对话。')));
    }
  } else if (view==='announcement') {
    const announcement=d.announcement;
    part.append(node('div',{class:'section-heading announcement-heading'},
      node('div',{},node('h2',{},'群公告'),announcement?.body?
        node('p',{class:'sub'},'更新于 ',timestamp(announcement.updated,true)):null),
      canManageChannel()?action(announcement?.body?'编辑公告':'发布公告','edit-announcement','text-button',{'aria-haspopup':'dialog'}):null));
    if (announcement) {
      const receipts=announcementReceipts(d.members,d.announcement_receipts);
      if (receipts) part.append(receipts);
    }
    part.append(announcement?.body?node('article',{class:'announcement-body'},markdownBody(announcement.body)):
      node('p',{class:'muted announcement-body'},'暂无群公告'));
  } else if (view==='members') {
    part.append(...d.members.map(memberEntry));
    if (!d.members.length) part.append(empty('等待项目加入','生成邀请后，让同事在目标项目的 Agent 会话中确认接入。'));
  } else if (!canManageChannel()) {
    part.append(empty('无权管理邀请','本频道的群主和管理员可以管理邀请。',node('a',{href:url(g.id)},'返回对话')));
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
  const dissolve=room?null:action('解散频道','dissolve','text-danger');
  const inviteHuman=action('邀请同事','human-invite','invite-human');
  const inviteAgent=action('邀请 Agent','invite','primary invite-action',{'aria-label':'邀请 Agent'});
  const actions=node('div',{class:'actions channel-actions'},
    node('details',{class:'channel-menu'},node('summary',{'aria-label':'频道操作',title:'频道操作'},'•••'),
      node('div',{},pause,dissolve,room?action('退出频道','leave-room','text-danger'):null)),
    inviteHuman,inviteAgent);
  const search=node('button',{type:'button',class:'search-trigger','data-action':'toggle-search',
    'aria-label':'搜索频道消息',title:'搜索频道消息'},node('i',{class:'search-icon','aria-hidden':'true'}));
  const tools=node('div',{class:'tab-tools'},search);
  const links=Object.entries(views).map(([id,label])=>node('a',{href:url(g.id,id),'data-view':id},label,id==='members'?badge:null));
  const tabs=node('div',{class:'tab-strip'},node('nav',{class:'tabs','aria-label':'频道详情'},...links),tools);
  $('mobile-actions').replaceChildren();
  $('workspace').replaceChildren(header,tabs);
  return {group:g.id,title,purpose,people,stack,count,badge,paused,pause,links,actions,tools,search,
    management:[pause,inviteHuman,inviteAgent],panels:new Map(),scrollTops:{}};
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
  const canManage=canManageChannel();
  for (const control of s.management) control.hidden=!canManage;
  for (const link of s.links) {
    if (link.dataset.view==='invites') link.hidden=!canManage;
    if (state.view===link.dataset.view) link.setAttribute('aria-current','page');
    else link.removeAttribute('aria-current');
  }
  s.search.classList.toggle('inactive',state.view!=='history');
  const destination=mobile.matches?$('mobile-actions'):s.tools;
  if (s.actions) {
    s.actions.hidden=!room && state.view==='private';
    if (s.actions.parentElement!==destination) destination.append(s.actions);
  }
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
    channelShell=null; syncComposer(); privateChat.sync(); $('mobile-actions').replaceChildren();
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
  const privateView=view==='private';
  const content=view==='announcement'?[Boolean(d),d?.announcement,d?.announcement_receipts,d?.members.map(m=>[m.id,m.name,m.agent])]:
    view==='members'?[d?.members,d?.paused]:view==='invites'?[d?.invites,d?.link_expires]:
    [page.history,page.older,page.more,page.directory,page.private,d?.members.length,searchOpen];
  const signature=JSON.stringify([view,canManageChannel(),state.topic,state.query,state.scope,state.category,state.topicStatus,
    state.before,state.privateRoute?.hash,state.error,content]);
  if (signature!==s.signature) rememberPosition(s);
  updateChannelShell(s,g,d);
  document.title=g.name+' · '+views[view]+' · 叠声';
  const showRoster=Boolean(d && view==='history');
  $('main').classList.toggle('has-roster',showRoster); $('roster').hidden=!showRoster;
  if (showRoster) {
    const rosterKey=JSON.stringify([d.members,d.paused,canManageChannel()]);
    if (s.rosterKey!==rosterKey) { inspector(d.members); s.rosterKey=rosterKey; }
  }
  syncComposer(); privateChat.sync();
  if (signature!==s.signature) {
    const panelKey=view==='history'?pageKey(view,state.scope):view;
    const base=!privateView && !state.topic && !state.query, cached=base && s.panels.get(panelKey);
    const next=privateView?privateChat.panel():cached && cached.signature===signature?cached.panel:panel();
    const route=privateView?state.privateRoute.hash:view==='topics'?topicUrl(g.id,state.topic):url(g.id,view,state.topic,state.query,state.scope);
    const form=s.route===route && s.panel.querySelector('.search-form,.topic-filters'), nextForm=next.querySelector('.search-form,.topic-filters');
    const editing=form && form.contains(document.activeElement)?document.activeElement:null;
    const focusedScope=document.activeElement?.closest('.feed-scope a')?.dataset.feedScope;
    const previousReceipts=s.route===route && s.panel.querySelector('.announcement-receipts');
    const nextReceipts=next.querySelector('.announcement-receipts');
    const focusedReceipts=previousReceipts && previousReceipts.contains(document.activeElement);
    if (previousReceipts && nextReceipts) nextReceipts.open=previousReceipts.open;
    if (form && nextForm) nextForm.replaceWith(form);
    if (base) s.panels.set(panelKey,{panel:next,signature});
    if (s.panel) s.panel.replaceWith(next); else $('workspace').append(next);
    s.panel=next; s.signature=signature; s.route=route;
    s.reading=view==='history' || state.topic || state.privateRoute?.thread?page:null;
    s.list=!s.reading && !state.query && !next.hasAttribute('aria-busy') && !state.error?route:null;
    restorePosition(s);
    if (editing?.isConnected) editing.focus({preventScroll:true});
    else if (focusSearch) $('query')?.focus({preventScroll:true});
    else if (focusedReceipts) nextReceipts?.querySelector('summary').focus({preventScroll:true});
    else if (focusedScope) next.querySelector('.feed-scope [data-feed-scope="'+focusedScope+'"]')?.focus({preventScroll:true});
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
  if (liveView || !canRefresh() || !liveChannelViews.includes(state.view) || !Number.isSafeInteger(state.page?.revision)) return;
  const watcher={controller:new AbortController()};
  liveView=watcher;
  try {
    while (liveView===watcher && canRefresh() && state.group?.id===group && liveChannelViews.includes(state.view)) {
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
  const filter=tab===view && ['history','topics'].includes(view) && /^[1-9][0-9]*$/.test(topic || '')?Number(topic):null;
  const search=new URLSearchParams(params);
  const privateRoute=view==='private' && g?privateChat.route(g.id,topic,search):null;
  const category=view==='topics' && Object.hasOwn(topicKinds,search.get('category'))?search.get('category'):'';
  const status=view==='topics' && Object.hasOwn(topicStates,search.get('status'))?search.get('status'):'';
  const cursor=Number(search.get('before'));
  const before=view==='topics' && Number.isSafeInteger(cursor) && cursor>0?cursor:null;
  const query=view==='history'?(search.get('q') || '').slice(0,160):'';
  let scope='all';
  if (view==='history' && g) {
    const requested=tab==='conclusions'?'all':search.get('scope');
    scope=['all','mine'].includes(requested)?requested:preferredScope(g.id);
    if (!filter && (tab!==view || requested!==scope)) history.replaceState(history.state,'',url(g.id,view,null,query,scope));
  }
  const groupChanged=g?.id!==state.group?.id;
  const changed=groupChanged || view!==state.view || filter!==state.topic || query!==state.query || scope!==state.scope ||
    category!==state.category || status!==state.topicStatus || before!==state.before || privateRoute?.hash!==state.privateRoute?.hash;
  if (groupChanged) { pages.clear(); state.data=null; }
  if (groupChanged || state.view!==view) { searchOpen=false; focusSearch=false; }
  if (changed) {
    channelShell?.actions?.querySelector('details')?.removeAttribute('open');
    state.page=g?channelPage(view,filter,query,view==='topics'?category+'/'+status+'/'+(before || ''):scope):null;
    state.error='';
  }
  state.group=g; state.view=view; state.topic=filter; state.query=query; state.scope=scope;
  state.category=category; state.topicStatus=status; state.before=before;
  state.privateRoute=privateRoute;
  nav();
  if (changed || !g) render();
}
async function refresh(mode) {
  if (memberRecoveryLink() || room && memberCode()) { if (mode!=='background') showLogin(); return; }
  if (mode==='background' && refreshing) return;
  const navigating=mode==='navigate';
  const id=++state.serial;
  refreshing=true;
  try {
    if (!room && (!location.hash || location.hash==='#channels')) {
      const accountOnly=location.hash==='#channels';
      try {
        const account=await accountApi('groups');
        if (id!==state.serial) return;
        clearWorkspace(); showAccountHome(account); return;
      } catch (error) {
        if (id!==state.serial) return;
        if (accountOnly) {
          clearWorkspace(); showAccountHome();
          if (error.status!==401) $('account-error').textContent=error.message;
          return;
        }
      }
    }
    const [route,params]=location.hash.split('?');
    const [,gid,tab,topic]=route.split('/');
    const withinRoom=!room || gid===room;
    let g=(!room || state.self) && (navigating || tab==='private') && state.groups.find(item=>item.id===(room || gid));
    if (!g) {
      const list=await api('groups');
      if (id!==state.serial) return;
      g=room?list.groups.find(item=>item.id===room):
        list.groups.find(item=>item.id===gid) || (!gid && list.groups.find(item=>item.id===state.group?.id)) || list.groups[0] || null;
      if (room && g && !list.username) {
        const groups=new Map(state.groups.map(item=>[item.id,item]));
        groups.set(room,g); state.groups=[...groups.values()];
      } else state.groups=list.groups;
      if (state.username!==list.username) memberRoomsChecked=-Infinity;
      state.publicUrl=list.public_url; state.logged=true; state.self=list.self || null; state.username=list.username || null;
      if (state.self && g) {
        rememberRoom(room,state.username?null:g.name);
        const settings=$('account-identity');
        settings.title=state.username?'账号设置':'设置账号';
        settings.setAttribute('aria-label',settings.title);
        if ($('account').querySelector('strong').textContent!==state.self.name) {
          $('account').querySelector('strong').textContent=state.self.name;
          $('account').querySelector('.avatar').replaceWith(avatar(state.self.name,'human',true));
        }
      }
      $('boot').hidden=true; $('member-entry').hidden=true; $('login').hidden=true; $('shell').hidden=false;
    }
    if (g && g.id!==gid) history.replaceState(null,'',url(g.id));
    selectRoute(g,withinRoom?tab:null,withinRoom?topic:null,withinRoom?params:null);
    if (room) void refreshMemberRooms();
    if (!g) return;
    const {view,topic:filter,query,page}=state;
    if (view==='private') {
      const route=state.privateRoute;
      const data=await privateChat.load(g.id,route);
      if (id!==state.serial) return;
      if (route.thread) {
        const {messages,more,...thread}=data;
        page.private=thread;
        setHistory(page,{messages,more});
      } else page.private=data;
      state.error='';
      render();
      return;
    }
    if (navigating && state.data) {
      if (page.pending) await page.pending;
      if (id!==state.serial) return;
      const fresh=!liveChannelViews.includes(view) || (page.revision===state.data.revision && performance.now()-page.updated<2500);
      if (fresh && performance.now()-lastRefresh<2500) { void followMessages(); return; }
    }
    const scope=view==='history' && !filter?state.scope:'all';
    const listing=view==='topics' && !filter;
    const criteria=listing?{directory:true,category:state.category,status:state.topicStatus,before:state.before}:{};
    const {history:result,directory,...details}=await api('detail',{group:g.id,topic:filter,query,scope,...criteria});
    if (id!==state.serial) return;
    if (listing && !Array.isArray(directory?.topics)) throw new Error('当前服务尚未启用话题列表。');
    state.data=details; state.error='';
    if (listing) { page.directory=directory; page.updated=performance.now(); }
    else setHistory(page,result);
    page.revision=details.revision;
    lastRefresh=performance.now();
    render();
    warmHistory(g.id,details.revision,view==='history'?state.scope:preferredScope(g.id));
    void followMessages();
  } catch (e) {
    if (id!==state.serial) return;
    if (e.code==='channel_dissolved') {
      if (!room) { await refresh(); return; }
      rememberRoom(room,null);
      history.replaceState(null,'',location.pathname);
      clearWorkspace(); memberScreen();
      $('member-caption').textContent='频道状态';
      $('member-channel').textContent='频道已解散'; document.title='频道已解散 · 叠声';
      $('member-help').textContent='返回「我的频道」可以继续使用其他频道。';
      entryActions(node('a',{href:'/#channels'},'我的频道'));
      return;
    }
    if (room && e.code==='not_a_member') {
      showLogin(e.message);
      return;
    }
    if (!state.logged) {
      if (room && e.status!==401) {
        $('boot').hidden=true; $('member-entry').hidden=false; $('member-form').hidden=true;
        $('member-help').textContent='暂时无法连接频道，请检查网络后重试。'; $('member-retry').hidden=false;
      } else { showLogin(); $('login-error').textContent=e.status===401?'':'连接服务失败，请检查地址后重试'; }
      return;
    }
    if (state.view==='private') {
      if (e.status===403 || e.status===404) {
        state.page.private=null; state.page.history=null; state.page.older=[]; state.page.more=null;
      }
      if (!state.page.private) { state.error=e.message; render(); }
      else notice('暂时无法刷新私聊，当前显示上次读取的记录');
      return;
    }
    const reading=['history','topics'].includes(state.view);
    if (state.group && (!state.data || (reading && !state.page.history && !state.page.directory))) {
      state.error=e.message; render();
    } else notice('暂时无法刷新，当前显示上次读取的记录');
  } finally { if (id===state.serial) refreshing=false; }
}
function openManual(trigger) {
  const sections=[
    ['创建和加入频道','群主可在侧栏点击「＋」创建频道。同事通过邀请链接加入，之后可从首页登录。'],
    ['邀请 Agent','在频道中点击「邀请 Agent」，复制链接并发送给项目会话，让 Agent 加入频道并监听。'],
    ['发起讨论','在输入框中输入消息，使用 @ 选择成员；勾选「仅同步」可分享消息而无需成员答复。Enter 发送，Shift + Enter 换行。'],
    ['跟进与协作','在「话题」查看讨论进展，在「公告」查看频道规则；从成员列表或「私聊」发起私聊。'],
    ['个人设置','点击侧栏底部的账号可管理账号设置，使用旁边的主题按钮切换深浅模式。']
  ];
  openDialog('使用手册',[node('div',{class:'manual-content'},...sections.map(([heading,body])=>
    node('section',{},node('h3',{},heading),node('p',{},body))))],null,undefined,false,trigger);
}
function field(id,label,limit,required=false) {
  return node('div',{class:'field'},node('label',{for:id},label),node('input',{id,name:id,maxlength:limit,
    required:required?'':null,'aria-describedby':'dialog-error',autocomplete:'off'}));
}
function secretField(input,label,help=null) {
  return node('div',{class:'field'},node('label',{for:input.id},label),
    node('div',{class:'secret'},input,secretToggle(input.id)),help);
}
function openDialog(title,children,handler,label,destructive=false,trigger=document.activeElement) {
  if (!dialog.open) closeReturn=trigger;
  submitDialog=handler;
  $('dialog-title').textContent=title; $('dialog-body').replaceChildren(...children); $('dialog-error').textContent='';
  const cancel=action(handler?'取消':'关闭','close');
  $('dialog-actions').replaceChildren(cancel);
  if (handler) $('dialog-actions').append(node('button',{type:'submit',class:destructive?'danger':'primary'},label));
  if (!dialog.open) dialog.showModal();
  (destructive?cancel:$('dialog-body').querySelector('input,textarea,button') || cancel).focus();
}
function tryClose() {
  if (!busy || !submitDialog) dialog.close();
}
dialog.addEventListener('cancel',e=>{ e.preventDefault(); tryClose(); });
dialog.addEventListener('close',()=>{
  if (dialog.open) return;
  const trigger=closeReturn; closeReturn=null; submitDialog=null;
  $('dialog-body').replaceChildren(); $('dialog-error').textContent=''; $('dialog-status').textContent='';
  if (trigger?.isConnected && !trigger.closest('[hidden],[inert]')) trigger.focus();
  else if (state.logged) $('workspace').focus();
});
$('dialog-form').addEventListener('submit',async e=>{
  e.preventDefault(); if (busy || !submitDialog) return;
  const form=e.currentTarget;
  for (const input of form.querySelectorAll('input[required],textarea[required]')) {
    if (!input.value.trim()) { input.setAttribute('aria-invalid','true'); input.focus();
      $('dialog-error').textContent='请填写'+input.labels[0].textContent; return; }
    input.removeAttribute('aria-invalid');
  }
  const submit=submitDialog, focused=document.activeElement;
  const controls=[...form.elements].filter(control=>!control.disabled);
  busy=true; $('dialog-error').textContent=''; $('dialog-status').textContent='正在提交…';
  for (const control of controls) control.disabled=true;
  try { await submit(); }
  catch (error) { if (dialog.open && submitDialog===submit) $('dialog-error').textContent=error.message; }
  finally {
    busy=false; $('dialog-status').textContent='';
    for (const control of controls) control.disabled=false;
    if (dialog.open && submitDialog===submit) (form.querySelector('[aria-invalid="true"]') || focused)?.focus();
  }
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
    await api(op,payload); dialog.close();
    const feedback={dissolve:'频道已解散',remove:'成员已移除',revoke:'邀请已撤销',requeue:'消息已重新排队',
      disable_link:'频道链接已停用','agent-unbind':'已解除绑定','admin-role':payload.admin?'已设为管理员':'已取消管理员'};
    notice(feedback[op]); await refresh();
  },label,true);
}
function editAnnouncement(group, trigger) {
  let published=state.data.announcement;
  const input=node('textarea',{id:'announcement-text',class:'resize-none',rows:7,maxlength:6000,
    'aria-describedby':'announcement-help dialog-error'});
  input.value=published?.body || '';
  input.addEventListener('input',()=>input.removeAttribute('aria-invalid'));
  const latest=node('details',{class:'announcement-conflict',hidden:''});
  const help=node('small',{id:'announcement-help'},'Agent 会把每版规则追加到项目根目录的 AGENTS.md。清空正文可撤下公告。');
  openDialog(published?.body?'编辑群公告':'发布群公告',
    [node('div',{class:'field'},node('label',{for:input.id},'公告正文'),input,help),latest],async()=>{
    if (!input.value.trim() && !published?.body) {
      input.setAttribute('aria-invalid','true');
      throw new Error('请填写公告正文。');
    }
    let result;
    try { result=await api('announcement-publish',{group,version:published?.version || 0,body:input.value}); }
    catch (error) {
      if (error.code!=='announcement_changed') throw error;
      await refresh();
      const current=state.group?.id===group && state.data?.announcement;
      if (!current || current.version<=(published?.version || 0)) throw error;
      published=current;
      latest.replaceChildren(node('summary',{},'当前公告 · 第 '+current.version+' 版'),
        current.body?markdownBody(current.body):node('p',{class:'muted'},'公告已撤下。'));
      latest.hidden=false; latest.open=true;
      throw new Error('公告已更新。请核对当前公告，再发布你的改动。');
    }
    if (state.group?.id===group && state.data) {
      if (state.data.announcement?.version!==result.announcement?.version && Array.isArray(state.data.announcement_receipts)) {
        state.data.announcement_receipts=[];
      }
      state.data.announcement=result.announcement;
      render();
      closeReturn=$('workspace').querySelector('[data-action="edit-announcement"]') || closeReturn;
    }
    dialog.close();
    notice(result.announcement?.body?'公告已发布':'公告已撤下');
    await refresh();
  },'发布公告',false,trigger);
}
function inviteResult(result,personal,human=false) {
  const link=new URL(result.url,location.origin).href;
  const area=node('input',{id:'invite-output',class:'invite-url',readonly:'','aria-label':'入群链接',value:link});
  const copy=action('复制链接','copy','primary');
  openDialog(human?'邀请同事':personal?'单项目入群链接':'邀请 Agent',[
    node('div',{class:'invite-channel'},node('span',{class:'channel-tile','aria-hidden':'true'},'#'),node('div',{},
      node('strong',{},state.group.name),node('small',{},human?'浏览器成员邀请':personal?'单项目邀请':'频道共享链接'))),
    node('p',{},human?'将这份链接私发给一位同事。对方可创建账号或使用现有账号加入本频道。':'把链接发给项目会话，告诉它「加入叠声，记住频道，监听并回复发给我的问题」。'),
    node('div',{class:'copy-row'},area,copy),
    node('small',{class:'hint'},(personal || human?'可用一次':'可供多个 Agent 使用')+' · '+new Date(result.expires*1000).toLocaleString('zh-CN',{month:'long',day:'numeric',hour:'2-digit',minute:'2-digit'})+' 到期'),
    !state.publicUrl?node('p',{class:'hint'},'当前链接仅供本机使用。要邀请同事，请连接局域网后以共享模式启动服务。'):
      link.startsWith('http://')?node('p',{class:'hint'},'同事连接同一局域网后，即可使用这个链接。'):null
  ].filter(Boolean),null);
  copy.focus();
  void refresh('background');
}
function secretResult(title,label,value,help) {
  const input=node('input',{id:'secret-output',type:'password',value,readonly:'',autocomplete:'off','aria-label':label});
  const copy=action('复制'+label,'copy-secret','primary');
  openDialog(title,[node('p',{},help),node('div',{class:'copy-row recovery-copy'},
    node('div',{class:'secret'},input,secretToggle(input.id)),copy)],null);
  copy.focus();
}
function openAccountSettings(trigger,username) {
  const nameField=field('settings-username','登录账号名',32,true);
  const name=nameField.querySelector('input');
  name.value=username; name.autocomplete='username'; name.spellcheck=false;
  name.setAttribute('autocapitalize','none');
  const current=node('input',{id:'settings-current-password',name:'current-password',type:'password',maxlength:128,
    autocomplete:'current-password',required:'','aria-describedby':'dialog-error'});
  const password=node('input',{id:'settings-new-password',name:'new-password',type:'password',maxlength:128,
    autocomplete:'new-password','aria-describedby':'settings-password-help dialog-error'});
  const confirm=node('input',{id:'settings-confirm-password',name:'confirm-password',type:'password',maxlength:128,
    autocomplete:'new-password','aria-describedby':'dialog-error'});
  const help=node('small',{id:'settings-password-help'},'至少 8 个字符。修改密码后，其他设备需要重新登录。');
  const startRoom=room;
  openDialog('账号设置',[nameField,secretField(current,'当前密码'),secretField(password,'新密码（选填）',help),
    secretField(confirm,'确认新密码')],async()=>{
    password.removeAttribute('aria-invalid'); confirm.removeAttribute('aria-invalid');
    if (password.value!==confirm.value) {
      confirm.setAttribute('aria-invalid','true');
      throw new Error('两次输入的新密码不一致');
    }
    const payload={current_username:username,username:name.value.trim(),current_password:current.value,new_password:password.value};
    let result;
    try { result=await accountApi('update',payload); }
    catch (error) {
      const fields={invalid_username:name,username_taken:name,invalid_current_password:current,invalid_password:password};
      fields[error.code]?.setAttribute('aria-invalid','true');
      throw error;
    }
    if (!name.isConnected || !dialog.open) return;
    dialog.close();
    if (!trigger.isConnected || room!==startRoom) return;
    if (room) {
      state.username=result.username;
      void refresh();
    } else {
      trigger.dataset.username=result.username;
      $('member-caption').textContent='@'+result.username;
    }
    notice('账号设置已保存');
  },'保存修改',false,trigger);
}
async function openAccountSetup(trigger,existing=false) {
  const group=room;
  if (!group) return;
  const inDialog=dialog.contains(trigger);
  const isCurrent=()=>!busy && room===group && trigger.isConnected && !trigger.closest('[hidden]') && dialog.open===inDialog;
  const legacy=await api('legacy_status',{},undefined,group,false).catch(error=>{
    if (isCurrent()) throw error;
    return null;
  });
  if (!legacy || !isCurrent()) return;
  const help=node('p',{},'将 @'+legacy.name+' 的频道身份关联到账号，保留原对话、私聊和 Agent 绑定。');
  const finish=async(operation,data={})=>{
    const result=await api(operation,data,undefined,group,false);
    const confirmed=await memberRoomSession(group);
    if (!confirmed.username || confirmed.self.id!==result.member) throw new Error('账号已设置，请用账号重新登录。');
    rememberRoom(group,null); dialog.close();
    if (memberCode()) history.replaceState(null,'',location.pathname);
    await refresh(); notice('已关联账号 @'+result.username);
  };
  if (legacy.username) {
    openDialog('关联旧身份',[help,node('p',{},'当前登录账号是 @'+legacy.username+'。')],
      ()=>finish('claim',{username:legacy.username}),'关联到当前账号',false,trigger);
    return;
  }
  const user=field('setup-username','账号',32,true);
  user.querySelector('input').autocomplete='username';
  user.querySelector('input').setAttribute('autocapitalize','none');
  const password=node('input',{id:'setup-password',type:'password',maxlength:'128',required:'',
    autocomplete:existing?'current-password':'new-password','aria-describedby':'dialog-error'});
  openDialog(existing?'关联已有账号':'设置同事账号',[help,user,
    secretField(password,'密码'),
    existing?null:node('p',{class:'hint'},'账号使用 3–32 个字母、数字或 . _ -，密码至少 8 个字符。'),
    action(existing?'创建新账号':'关联已有账号',existing?'setup-account':'link-existing-account','text-button')].filter(Boolean),async()=>{
    const login=credentials('setup');
    if (existing) { const {username}=await accountApi('login',login); await finish('claim',{username}); }
    else await finish('upgrade',login);
  },existing?'登录并关联':'创建账号并保留身份',false,trigger);
}
function recoverWithCode(target) {
  const input=node('input',{id:'recovery-input',type:'password',maxlength:100,required:'',autocomplete:'off',
    spellcheck:'false',autocapitalize:'none','aria-describedby':'dialog-error'});
  const targetName=rememberedRooms().find(item=>item.id===target)?.name;
  let attempt=null;
  openDialog('使用恢复码',[secretField(input,'恢复码'),
    node('p',{class:'hint'},targetName?'请粘贴 #'+targetName+' 的完整恢复码。':'每份恢复码对应一个频道。粘贴后直接进入该频道。')],async()=>{
    const match=/^([0-9a-f]{32})\.([A-Za-z0-9_-]{43})$/.exec(input.value.trim());
    if (!match) { input.setAttribute('aria-invalid','true'); input.focus(); throw new Error('恢复码格式不完整，请检查复制的内容'); }
    const [,group,code]=match;
    if (target && group!==target) {
      input.setAttribute('aria-invalid','true'); input.focus();
      throw new Error(targetName?'请使用 #'+targetName+' 的恢复码，或打开群主发来的恢复链接。':
        '请使用这个频道的恢复码，或打开群主发来的恢复链接。');
    }
    if (attempt?.code!==match[0]) attempt={code:match[0],request:requestId()};
    await restoreMember(group,{code,kind:'code',request:attempt.request});
    dialog.close();
    await finishMemberRecovery(group);
  },'恢复并进入频道');
}
function signOutMember() {
  const current=room;
  openDialog('退出登录？',[node('p',{},'退出此浏览器的登录后，可以再次使用账号进入已加入的频道。')],async()=>{
    stopLiveView();
    const groups=new Set(rememberedRooms().map(group=>group.id));
    if (current && !state.username) groups.add(current);
    const results=await Promise.allSettled([...groups].map(group=>api('logout',{},undefined,group)));
    if (results.some(result=>result.status==='rejected')) {
      void refreshMemberRooms(true);
      throw new Error('部分频道暂未退出，请检查网络后重试');
    }
    await accountApi('logout');
    if (room) location.replace('/#channels');
    else { history.replaceState(null,'','/#channels'); await refresh(); }
  },'退出登录');
}
async function handle(button) {
  const g=state.group, id=button.dataset.id;
  switch (button.dataset.action) {
    case 'toggle-secret': toggleSecret($(id),button); break;
    case 'open-manual': openManual(button); break;
    case 'clear': {
      const input=$('query'); input.value=''; button.hidden=true; input.focus();
      searchRoute(''); break;
    }
    case 'close': tryClose(); break;
    case 'retry': await refresh(); break;
    case 'edit-announcement':
      if (canManageChannel() && state.data) editAnnouncement(g.id,button);
      break;
    case 'private-share':
    case 'private-older':
    case 'private-retry':
    case 'private-remind':
      await privateChat.act(button); break;
    case 'logout': await api('logout'); history.replaceState(null,'','/#owner'); showLogin(); break;
    case 'member-logout': signOutMember(); break;
    case 'account-settings': {
      const username=room?state.username:button.dataset.username;
      if (username) openAccountSettings(button,username);
      else if (room) await openAccountSetup(button);
      break;
    }
    case 'recover-code': recoverWithCode(id || room); break;
    case 'setup-account': await openAccountSetup(button); break;
    case 'link-existing-account': await openAccountSetup(button,true); break;
    case 'invite-account-mode':
      if (memberInvite && !$('member-submit').disabled) {
        useExistingAccount=!useExistingAccount;
        $('member-error').textContent=''; $('member-password').value='';
        renderInviteForm(); $('member-username').focus();
      }
      break;
    case 'bind-agent': {
      const agent=state.data.members.find(m=>m.id===id);
      if (!agent) break;
      const title='绑定 @'+agent.name;
      openDialog(title,[node('p',{},'绑定后，「只看与我有关」会纳入这个 Agent 的消息，你也可只读查看它在本频道保留期内的私聊。'),
        node('p',{class:'hint'},'生成指令后，复制到这个 Agent 原来的项目会话，由它使用自己的频道身份确认。')],async()=>{
        const result=await api('agent-bind-code',{group:g.id,agent:id});
        const link=new URL(result.url,location.origin).href;
        const instruction='请将你在叠声频道「'+g.name+'」的 Agent 身份绑定给「'+result.human+'」。我授权这位用户查看你在本频道保留期内的私聊。请按此链接的绑定说明操作。 '+link;
        secretResult(title,'绑定指令',instruction,'将指令粘贴给 @'+agent.name+'。链接可用一次，30 分钟内有效。');
      },'生成绑定指令');
      break;
    }
    case 'unbind-agent': {
      const agent=state.data.members.find(m=>m.id===id);
      if (!agent) break;
      confirmAction('解除 @'+agent.name+' 的绑定？','解除后，个人筛选和私聊列表会移除通过此绑定显示的记录。',
        'agent-unbind',{group:g.id,agent:id},'解除绑定');
      break;
    }
    case 'recovery-link': {
      const member=state.data.members.find(m=>m.id===id);
      if (!member) break;
      openDialog('为 @'+member.name+' 生成恢复链接',[
        node('p',{},'持有链接的人可以用这位同事的身份进入频道，查看本人及已绑定 Agent 的私聊。请先确认对方身份，再将链接私发给本人。'),
        node('p',{class:'hint'},'链接 30 分钟内可用一次。生成新链接后，上一份链接失效。')
      ],async()=>{
        const result=await api('recovery_link',{group:g.id,member:id});
        const expiry=timestamp(result.expires,true).textContent;
        secretResult('恢复链接','恢复链接',new URL(result.url,location.origin).href,
          '仅发给 @'+result.name+' 本人。链接可用一次，'+expiry+' 到期。');
      },'生成恢复链接');
      break;
    }
    case 'leave-room':
      button.closest('details').open=false;
      openDialog('退出这个频道？',[node('p',{},'退出后将结束你在 #'+g.name+' 的成员资格。重新加入需要群主发一份新邀请。')],async()=>{
        await api('leave',{group:g.id});
        rememberRoom(g.id,null);
        const next=state.groups.find(group=>group.id!==g.id);
        location.replace(next?'/room/'+next.id:'/#channels');
      },'退出频道',true); break;
    case 'create':
      openDialog('新建群聊',[field('room-name','群名称',64,true),field('room-purpose','讨论范围（可选）',240)],async()=>{
        const result=await api('create',{name:$('room-name').value,purpose:$('room-purpose').value});
        dialog.close(); location.hash=url(result.group); notice('群聊已创建');
      },'创建群聊'); break;
    case 'human-invite':
    case 'invite': {
      const group=g.id;
      const human=button.dataset.action==='human-invite', title=human?'邀请同事':'邀请 Agent';
      const pending=node('p',{role:'status'},'正在准备频道链接…');
      openDialog(title,[pending],null);
      try {
        const result=await api(human?'human_invite':'channel_link',{group});
        if (!dialog.open || !pending.isConnected || state.group?.id!==group) break;
        inviteResult(result,false,human);
      } catch (e) {
        if (dialog.open && pending.isConnected) $('dialog-error').replaceChildren(e.message,' ',action('重试',button.dataset.action));
      }
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
    case 'copy': {
      const input=$('invite-output'), copied=await copyField(input);
      if (!dialog.open || !input.isConnected) break;
      if (copied) button.textContent='已复制';
      else $('dialog-error').textContent='已选中链接，请使用系统复制快捷键。';
      break;
    }
    case 'copy-secret': {
      const input=$('secret-output'), copied=await copyField(input);
      if (!dialog.open || !input.isConnected) break;
      if (copied) button.textContent='已复制';
      else {
        if (input.type==='password') toggleSecret(input,dialog.querySelector('[data-id="secret-output"]'));
        input.focus(); input.select();
        $('dialog-error').textContent='已选中凭证，请使用系统复制快捷键。';
      }
      break;
    }
    case 'toggle-search':
      searchOpen=!searchOpen;
      if (!searchOpen && state.query) searchRoute(''); else { render(); if (searchOpen) $('query')?.focus(); }
      break;
    case 'pause':
      button.closest('details').open=false;
      await api('pause',{group:g.id,paused:!(state.data?.paused ?? g.paused)}); await refresh(); break;
    case 'dissolve':
      if (room) break;
      button.closest('details').open=false;
      confirmAction('解散 #'+g.name+'？','解散会永久关闭频道，所有成员和邀请将失效。Agent 会在连接时收到通知并清理此频道的本机绑定。',
        'dissolve',{group:g.id},'解散频道'); break;
    case 'profile': {
      const m=state.data.members.find(item=>item.id===id);
      openDialog('成员设置',[field('member-name','群内名字',64,true),field('member-role','工作职责',160)],async()=>{
        await api('profile',{group:g.id,member:id,name:$('member-name').value,role:$('member-role').value});
        dialog.close(); await refresh();
      },'保存设置');
      $('member-name').value=m.name; $('member-role').value=m.role; break;
    }
    case 'admin-role': {
      const member=state.data.members.find(item=>item.id===id);
      if (!canSetAdmin(member)) break;
      const admin=!member.admin, label=admin?'设为管理员':'取消管理员';
      const text=admin?'这位同事可以发布公告、管理成员和邀请、重新投递消息或暂停讨论。设置管理员和解散频道由群主负责。':
        '这位同事将继续留在频道，管理权限会立即收回。';
      confirmAction(label+' · @'+member.name,text,'admin-role',{group:g.id,member:id,admin},label);
      break;
    }
    case 'reply-to': {
      const member=composerMembers().find(m=>m.id===id);
      if (composerGroup!==g.id || !member) break;
      const draft=draftFor(g.id);
      draft.syncOnly=false;
      draft.parent=button.dataset.parent?Number(button.dataset.parent):null;
      if (draft.parent) { draft.targets=[member]; editDraft(); messageInput.focus(); }
      else {
        const previous=memberMentions(draft.body,composerMembers(),draft.targets)[0];
        insertMention(member,previous?.start ?? 0,previous?.end ?? 0);
      }
      break;
    }
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
      const scope=view==='history' && !topic?state.scope:'all';
      const previous=await api('history',{group:group.id,topic,query,scope,before:Math.min(...ids)});
      if (state.page!==page || state.view!==view) return;
      if (agentScope(previous)!==agentScope(page.history)) { await refresh(); break; }
      page.older.push(...previous.messages); page.more=previous.more; render(); break;
    }
  }
}
document.addEventListener('click',async e=>{
  const link=e.target.closest('a[href^="#/"],a[data-member-room]');
  if (link && e.button===0 && !e.metaKey && !e.ctrlKey && !e.altKey && !e.shiftKey && !e.defaultPrevented) {
    e.preventDefault();
    const target=link.dataset.memberRoom;
    if (target) {
      history.pushState(null,'','/room/'+target+url(target));
      activateRoute(); return;
    }
    if (link.dataset.feedScope) {
      const key=feedPreferenceKey(state.group?.id);
      if (key) {
        try { localStorage.setItem(key,link.dataset.feedScope); } catch {}
      }
    }
    openRoute(link.hash,link.dataset.openTopic?{feedHash:location.hash}:null); return;
  }
  const btn=e.target.closest('button[data-action]');
  if (!btn || btn.disabled) return;
  btn.disabled=true;
  try { await handle(btn); } catch (err) { if(dialog.open) $('dialog-error').textContent=err.message; else notice(err.message); }
  finally { btn.disabled=false; }
});
window.addEventListener('hashchange',activateRoute);
window.addEventListener('popstate',()=>{ if (room!==roomFromPath()) activateRoute(); });
window.addEventListener('storage',e=>{
  if (e.key!=='diesheng.rooms' && e.key!==null) return;
  if (!room && !$('account-login-form').hidden && !$('known-rooms').hidden) showKnownRooms();
  else void refreshMemberRooms(true);
});
window.addEventListener('pageshow',e=>{ if (e.persisted) void refresh(); });
document.addEventListener('visibilitychange',()=>{
  if (document.hidden) stopLiveView();
  else if (state.logged) { void refresh('background'); void refreshMemberRooms(true); }
  else if (!room && !$('account-login-form').hidden && !$('known-rooms').hidden && !dialog.open) showKnownRooms();
});
window.addEventListener('pagehide',stopLiveView);
window.addEventListener('online',()=>{ if (state.logged) refresh(); });
window.addEventListener('offline',()=>notice('连接已断开，当前显示上次读取的记录'));
setInterval(()=>{if (canRefresh() && (!liveView || performance.now()-lastRefresh>=5000)) refresh('background');},2500);
refresh();
