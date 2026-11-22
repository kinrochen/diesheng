'use strict';

const privateChat = (() => {
  const drafts = new Map();
  const memberId = value => /^[a-f0-9]{32}$/.test(value || '') ? value : null;
  const availability = {
    online:'私聊在线', offline:'离线 · 消息会排队', paused:'应答已暂停 · 消息会排队',
    left:'对方已离开频道'
  };
  const setupLabels = {
    not_requested:'接收尚未开启 · 消息已排队',
    queued:'提醒已排队 · 等待对方 Agent 接收',
    notified:'提醒已送达 · 等待对方开启接收',
    deferred:'对方暂缓开启 · 消息继续排队'
  };
  const receipts = {pending:'等待答复',processing:'正在答复',replied:'已答复',failed:'答复失败',cancelled:'投递已结束'};
  let current = null, composing = false, newline = false;

  function availabilityLabel(status, enabled) {
    if (status==='offline' && enabled===false) return '接收尚未开启 · 首次发送会提醒对方';
    return availability[status];
  }

  function href(group, {thread=null,peer=null,create=false,before=null}={}) {
    const params = new URLSearchParams();
    if (peer) params.set('peer',peer);
    if (create) params.set('new','1');
    if (before) params.set('before',before);
    return url(group,'private',thread)+(params.size?'?'+params:'');
  }
  function route(group, fragment, params) {
    const thread = memberId(fragment), peer = memberId(params.get('peer'));
    const create = !thread && params.get('new')==='1';
    const raw = Number(params.get('before'));
    const before = !create && Number.isSafeInteger(raw) && raw>0 ? raw : null;
    return {thread,peer,create,before,hash:href(group,{thread,peer,create,before})};
  }
  function memberLink(group, member, compact=false) {
    if (member.agent==='host' || member.agent==='human') return null;
    return node('a',{href:href(group,{peer:member.id}),class:compact?'roster-private-link':'text-button',
      'aria-label':'私下询问 @'+member.name},compact?'私聊':'私下询问');
  }
  function directoryHeader(group, route, title, next) {
    const back = route.peer || route.create ? node('a',{href:href(group)},'‹ 我的私聊') : null;
    return node('div',{class:'section-heading private-directory-heading'},
      node('div',{},back,node('h2',{},title)),next);
  }
  const lock = () => node('i',{class:'private-lock','aria-hidden':'true'});
  function list(part, data, r, group) {
    const peer = data.agents.find(a=>a.id===r.peer);
    if (r.create) {
      part.append(directoryHeader(group,r,'新建私聊'));
      for (const member of data.agents) {
        const row = node('a',{class:'entry private-entry',href:href(group,{peer:member.id})},
          avatar(member.name,member.agent,true),node('div',{},
            node('h3',{},member.name),node('p',{class:'sub'},member.role || clientName(member.agent))),
          chip(availabilityLabel(member.state,member.listener_enabled),member.state==='online'?'good':''));
        part.append(row);
      }
      if (!data.agents.length) part.append(empty('还没有可选的 Agent','Agent 加入这个频道后会显示在这里。'));
      return;
    }
    const title = peer ? '与 @'+peer.name+' 的私聊' : '我的私聊';
    const start = peer || !r.peer ? node('a',{class:'private-start',
      href:href(group,{peer:r.peer,create:true})},peer?'新建询问':'新建私聊') : null;
    part.append(directoryHeader(group,r,title,start));
    if (peer) part.append(node('p',{class:'private-availability sub'},availabilityLabel(peer.state,peer.listener_enabled)));
    for (const item of data.threads) {
      const participants=item.via_agent?'@'+item.via_agent.name+' 与 @'+item.name:'@'+item.name;
      const details = node('div',{class:'private-entry-detail'},
        node('h3',{},item.title),node('p',{class:'sub'},participants,' · ',item.preview));
      part.append(node('a',{class:'entry private-entry',href:href(group,{thread:item.id,peer:r.peer,before:r.before})},
        avatar(item.name,item.agent,true),details,
        node('div',{class:'private-entry-aside'},timestamp(item.created,true),
          item.unread?chip(item.unread+' 条未读','info'):null)));
    }
    if (!data.threads.length) part.append(empty('还没有私聊',r.peer && !peer?'此成员已离开频道。':
      '选一位频道里的项目 Agent，发起一段私聊。'));
    if (data.more || r.before) {
      const pages = node('nav',{class:'topic-pages','aria-label':'私聊翻页'});
      if (r.before) pages.append(node('a',{href:href(group,{peer:r.peer})},'最新私聊'));
      if (data.more) pages.append(node('a',{href:href(group,{peer:r.peer,before:data.threads.at(-1).last_id})},'更早私聊'));
      part.append(pages);
    }
  }
  function bubble(m, {state:readiness,listener_enabled,paused,via_agent}) {
    let receipt=receipts[m.status];
    if (m.status==='pending' && (readiness!=='online' || paused))
      receipt=readiness==='offline' && listener_enabled===false?'已排队 · 等待对方开启接收':'已排队';
    return node('article',{class:'chat-row'+(m.own?' own':''),'data-message':m.id},
      avatar(m.author,m.agent),node('div',{class:'chat-content'},
        node('div',{class:'chat-byline'},node('strong',{},m.own?'我':m.author),timestamp(m.created)),
        markdownBody(m.body),
        node('div',{class:'meta'},
          m.own && m.status?node('span',{class:'receipt'},receipt):null,
          m.own && m.status==='failed'?action('重试答复','private-retry','text-button',{'data-id':m.id}):null,
          !via_agent && !m.own && m.reply_to && readiness!=='left' && !paused?
            action('分享结论到频道','private-share','text-button',{'data-id':m.id}):null)));
  }
  function conversation(part, peer, title, label, r, group, {remind=false,topic=null,viaAgent=null}={}) {
    const audience=viaAgent?'@'+viaAgent.name+' 与 @'+peer.name+' · 只读':'你、@'+peer.name+' 及其绑定用户可见';
    part.append(node('div',{class:'private-thread-header'},
      node('a',{href:href(group,{peer:r.peer,before:r.before})},'‹ 我的私聊'),
      node('div',{class:'private-identity'},avatar(peer.name,peer.agent,true),
        node('div',{},node('h2',{},title),node('p',{class:'private-audience'},lock(),audience))),
      !viaAgent?node('div',{class:'private-readiness'},
        node('p',{class:'private-availability sub',role:'status'},label),
        remind?action('提醒对方开启私聊','private-remind','text-button'):null):null,
      topic?node('p',{class:'private-topic-reference'},node('a',{href:topicUrl(group,topic.id),title:topic.title},
        '查看关联话题 #'+topic.id)):null));
  }
  function panel() {
    const {group,page,privateRoute:r,error} = state;
    const data = page.private;
    const part = node('section',{class:'panel private-panel','aria-label':'私聊'});
    if (!data) {
      if (error) part.append(empty('无法打开私聊',error,node('div',{class:'actions'},
        action('重新加载','retry'),node('a',{href:href(group.id)},'返回我的私聊'))));
      else {
        part.setAttribute('aria-busy','true');
        part.append(node('p',{class:'loading',role:'status'},'正在读取私聊…'));
      }
      return part;
    }
    if (r.thread) {
      part.classList.add('private-thread');
      const label=data.paused?'频道已暂停':setupLabels[data.setup_status] || availabilityLabel(data.state,data.listener_enabled);
      conversation(part,data.thread,data.thread.title,label,r,group.id,
        {remind:!data.paused && data.setup_status==='not_requested',topic:data.topic,viaAgent:data.via_agent});
      const log = node('div',{class:'chat-log',role:'log','aria-label':'私聊记录',tabindex:'0'});
      if (page.more ?? page.history.more) log.append(node('div',{class:'load'},action('查看更早消息','private-older')));
      const messages = new Map([...page.older,...page.history.messages].map(m=>[m.id,m]));
      log.append(...[...messages.values()].sort((a,b)=>a.id-b.id).map(m=>bubble(m,data)));
      part.append(log);
    } else if (r.create && r.peer) {
      const peer = data.agents.find(m=>m.id===r.peer);
      part.classList.add('private-thread');
      if (peer) conversation(part,peer,'新建询问',data.paused?'频道已暂停':availabilityLabel(peer.state,peer.listener_enabled),r,group.id);
      else part.append(empty('无法开始私聊','请选择仍在本频道的 Agent。',node('a',{href:href(group.id,{create:true})},'选择 Agent')));
    } else list(part,data,r,group.id);
    return part;
  }

  async function load(group, route) {
    if (route.thread) return api('private-read',{group,thread:route.thread,seen:!document.hidden});
    return api('private-list',{group,peer:route.peer || '',before:route.before});
  }
  function clear() {
    drafts.clear();
    current=null;
    $('private-body').value='';
    $('private-title').value='';
    $('private-composer').hidden=true;
  }
  function sync() {
    const r = state.privateRoute, data = state.page?.private;
    const newThread = r?.create && r?.peer;
    const target = r?.thread ? data?.thread : data?.agents.find(m=>m.id===r?.peer);
    const status = r?.thread ? data?.state : target?.state;
    const form = $('private-composer');
    const visible = state.logged && state.view==='private' && target && !data.via_agent && status!=='left' && (r.thread || newThread);
    form.hidden=!visible;
    if (!visible) {
      if (current) { $('private-body').value=''; $('private-title').value=''; }
      current=null; return;
    }
    const group = state.group.id;
    const key = [group,state.self?.id || 'owner',r.thread || 'new/'+r.peer].join('/');
    if (!drafts.has(key)) drafts.set(key,{title:'',topic:null,body:'',request:requestId(),error:'',sending:false});
    const draft = drafts.get(key), input = $('private-body');
    if (current?.key!==key) {
      input.value=draft.body;
      $('private-title').value=draft.title;
      current={key,group,route:r,draft};
    }
    $('private-title-field').hidden=!newThread;
    const topic = newThread ? draft.topic : null;
    $('private-title').placeholder=topic?'留空时使用话题标题':'';
    $('private-topic-selection').hidden=!topic;
    const topicName=topic?'已关联 #'+topic.id+' · '+topic.title:'';
    if ($('private-topic-name').textContent!==topicName) $('private-topic-name').textContent=topicName;
    const choose=$('private-choose-topic');
    choose.textContent=topic?'更换频道话题':'选择频道话题';
    choose.disabled=$('private-clear-topic').disabled=draft.sending;
    input.placeholder='私下回复 @'+target.name+'…';
    const issue = data.paused ? '频道已暂停，草稿已保留。' : '';
    $('private-error').textContent=draft.error || issue;
    input.setAttribute('aria-invalid',String(Boolean(draft.error)));
    input.readOnly=draft.sending;
    $('private-title').readOnly=draft.sending;
    $('private-send').disabled=draft.sending || !draft.body.trim() || Boolean(issue);
    form.setAttribute('aria-busy',String(draft.sending));
  }
  function setTopic(topic) {
    Object.assign(current.draft,{topic,request:requestId(),error:''});
    sync();
  }
  function chooseTopic() {
    const {group,route,draft}=current;
    const searchField=field('private-topic-query','搜索话题',100);
    const input=searchField.querySelector('input');
    input.type='search'; input.placeholder='标题关键词或 #编号';
    const search=node('button',{type:'button'},'搜索');
    searchField.append(node('div',{class:'private-topic-search'},input,search));
    const feedback=node('p',{class:'sub private-topic-feedback',role:'status'});
    const results=node('div',{class:'private-topic-results',role:'group','aria-label':'可关联的频道话题'});
    const more=node('button',{type:'button',hidden:''},'加载更多话题');
    let query='', cursor=null, generation=0;

    async function load(before=null) {
      const attempt=++generation;
      if (before===null) { results.replaceChildren(); more.hidden=true; }
      feedback.textContent='正在读取话题…';
      more.disabled=true;
      try {
        const response=await api('private-topics',{group,target:route.peer,query,before,limit:12});
        if (!results.isConnected || attempt!==generation) return;
        if (!Array.isArray(response.topics)) throw new Error('暂时无法读取可关联的话题。');
        for (const topic of response.topics) {
          const row=node('button',{type:'button',class:'private-topic-choice'},node('span',{class:'msg-id'},'#'+topic.id),
            node('span',{class:'private-topic-choice-title'},topic.title),chip(topicStates[topic.status]));
          row.addEventListener('click',()=>{
            if (current?.draft===draft && !draft.sending) setTopic({id:topic.id,title:topic.title});
            dialog.close();
          });
          results.append(row);
        }
        cursor=response.more ? response.topics.at(-1).last_id : null;
        more.hidden=cursor===null;
        feedback.textContent=response.topics.length || before!==null ? '' :
          query ? '没有符合搜索的话题。' : '暂无双方都能查看的话题。';
      } catch (err) {
        if (results.isConnected && attempt===generation) {
          const retry=node('button',{type:'button',class:'text-button'},'重试');
          retry.addEventListener('click',()=>void load(before));
          feedback.replaceChildren(err.message,' ',retry);
          more.hidden=true;
        }
      } finally {
        if (attempt===generation) more.disabled=false;
      }
    }
    input.addEventListener('keydown',e=>{
      if (e.isComposing || e.keyCode===229) return;
      if (e.key==='Escape') { e.preventDefault(); tryClose(); }
      if (e.key==='Enter') { e.preventDefault(); search.click(); }
    });
    search.addEventListener('click',()=>{ query=input.value.trim(); void load(); });
    more.addEventListener('click',()=>void load(cursor));
    $('private-choose-topic').focus({preventScroll:true});
    openDialog('选择频道话题',[searchField,feedback,results,more],null);
    void load();
  }
  function install() {
    const form = $('private-composer'), input = $('private-body');
    $('private-choose-topic').addEventListener('click',chooseTopic);
    $('private-clear-topic').addEventListener('click',()=>{ setTopic(null); $('private-choose-topic').focus(); });
    form.addEventListener('input',()=>{
      if (!current) return;
      Object.assign(current.draft,{body:input.value,title:$('private-title').value,request:requestId(),error:''});
      sync();
    });
    input.addEventListener('compositionstart',()=>{composing=true;});
    input.addEventListener('compositionend',()=>{composing=false;});
    input.addEventListener('blur',()=>{composing=false;newline=false;});
    input.addEventListener('keyup',()=>{newline=false;});
    input.addEventListener('keydown',e=>{
      if (e.key!=='Enter') return;
      newline=e.shiftKey || e.altKey || composing || e.isComposing || e.keyCode===229;
      if (newline) return;
      e.preventDefault();
      if (!e.repeat) form.requestSubmit();
    });
    input.addEventListener('beforeinput',e=>{
      if (!['insertLineBreak','insertParagraph'].includes(e.inputType) || newline || composing || e.isComposing || !e.cancelable) return;
      e.preventDefault(); form.requestSubmit();
    });
    form.addEventListener('submit',async e=>{
      e.preventDefault();
      if (!current || $('private-send').disabled) return;
      const {key,group,route,draft}=current;
      const payload={group,body:draft.body,request:draft.request};
      draft.sending=true; draft.error=''; sync();
      try {
        const result=route.thread ? await api('private-send',{...payload,thread:route.thread}) :
          await api('private-start',{...payload,target:route.peer,title:draft.title,topic:draft.topic?.id});
        if (drafts.get(key)===draft) Object.assign(draft,{body:'',title:'',topic:null,request:requestId()});
        if (current?.key!==key) return;
        input.value=''; $('private-title').value='';
        if (!route.thread) {
          history.replaceState(null,'',href(group,{thread:result.thread,peer:route.peer}));
          await refresh('navigate');
        } else await refresh();
        if (state.view==='private' && current) {
          const log=$('workspace').querySelector('.chat-log');
          if (log) log.scrollTop=log.scrollHeight;
          input.focus();
        }
      } catch (err) { draft.error=err.message; }
      finally { draft.sending=false; sync(); }
    });
  }
  async function act(button) {
    const group=state.group.id, thread=state.privateRoute.thread, page=state.page;
    if (button.dataset.action==='private-older') {
      const before=Math.min(...[...page.older,...page.history.messages].map(m=>m.id));
      const previous=await api('private-read',{group,thread,before});
      if (state.page===page) { page.older.push(...previous.messages); page.more=previous.more; render(); }
    } else if (button.dataset.action==='private-retry') {
      await api('private-retry',{group,thread,message:Number(button.dataset.id)});
      await refresh();
    } else if (button.dataset.action==='private-remind') {
      await api('private-remind',{group,thread});
      await refresh();
    } else {
      const name=state.group.name, message=Number(button.dataset.id);
      let request=requestId();
      const text=node('textarea',{id:'share-summary',class:'resize-none',maxlength:'6000',rows:'4',
        required:'',autocomplete:'off','aria-describedby':'dialog-error'});
      text.addEventListener('input',()=>{request=requestId();});
      openDialog('分享结论到频道',[node('p',{class:'hint'},'频道 #'+name+' 的成员会看到你在这里填写的内容。'),
        node('div',{class:'field'},node('label',{for:'share-summary'},'公开内容'),text)],async()=>{
        await api('private-share',{group,thread,message,request,body:text.value});
        dialog.close(); notice('结论已发布到 #'+name);
      },'发布到 #'+name,true);
    }
  }
  return {route, memberLink, panel, load, clear, sync, install, act};
})();
