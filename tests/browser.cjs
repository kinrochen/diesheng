const { chromium } = require(process.env.RELAY_TEST_PLAYWRIGHT);
const { randomBytes } = require('node:crypto');
const assert = require('node:assert/strict');
const path = require('node:path');

async function checkTabSwitches(context,url) {
  const probe=await context.newPage();
  let releaseWarm, releaseSearch;
  const warmGate=new Promise(resolve=>{releaseWarm=resolve;});
  const searchGate=new Promise(resolve=>{releaseSearch=resolve;});
  const searchText='等待中的搜索';
  const tab=id=>probe.locator('.tabs a[data-view="'+id+'"]');
  const searchRequest=r=>r.url().endsWith('/detail') && r.postDataJSON()?.query===searchText;
  try {
    await probe.route('**/api/owner/conclusions',async route=>{
      await warmGate; await route.continue();
    });
    await probe.route('**/api/owner/detail',async route=>{
      if (searchRequest(route.request())) await searchGate;
      await route.continue();
    });
    await probe.goto(url);
    await probe.getByRole('log').locator('[data-message]').first().waitFor();
    const header=await probe.locator('.room-header').elementHandle(), strip=await probe.locator('.tab-strip').elementHandle();
    await tab('conclusions').focus(); await probe.keyboard.press('Enter');
    assert.equal(await tab('conclusions').getAttribute('aria-current'),'page');
    assert.ok(await tab('conclusions').evaluate(a=>a===document.activeElement));
    assert.equal(await probe.locator('.panel.conclusions').getAttribute('aria-busy'),'true');
    const search=probe.getByRole('searchbox');
    await search.fill('刚输入的关键词');
    releaseWarm();
    await probe.locator('.panel.conclusions .basis').waitFor();
    assert.equal(await search.inputValue(),'刚输入的关键词');
    assert.ok(await search.evaluate(el=>el===document.activeElement));
    await tab('members').click();
    await probe.locator('.panel.members').getByText('@订单前端').waitFor();
    await tab('conclusions').click();
    await probe.locator('.panel.conclusions .basis').waitFor();
    await probe.getByRole('searchbox').fill(searchText);
    await Promise.all([probe.waitForRequest(searchRequest),probe.getByRole('button',{name:'搜索',exact:true}).click()]);

    for (const width of [1320,375]) {
      await probe.setViewportSize({width,height:500});
      await probe.waitForFunction(()=>Boolean(document.querySelector('#mobile-actions .channel-actions'))===matchMedia('(max-width: 700px)').matches);
      const checks=await probe.evaluate(async()=>{
        const links=[...document.querySelectorAll('.tabs a')], byId=Object.fromEntries(links.map(a=>[a.dataset.view,a]));
        const rects=()=>links.map(a=>{const r=a.getBoundingClientRect(); return [r.x,r.y,r.width,r.height];});
        byId.history.click();
        const log=document.querySelector('.chat-log'); log.scrollTop=24;
        const oldTop=log.scrollTop, scrollable=log.scrollHeight-log.clientHeight-oldTop>72;
        const before=JSON.stringify(rects()), results=[];
        const content={history:'.chat-row',members:'.entry',invites:'.link-control',conclusions:'.basis'};
        for (const view of ['members','invites','conclusions','members','history']) {
          byId[view].focus(); byId[view].click();
          await new Promise(requestAnimationFrame);
          const panel=document.querySelector('.panel.'+view);
          if (byId[view].getAttribute('aria-current')!=='page') results.push(view+' 未选中');
          if (document.activeElement!==byId[view]) results.push(view+' 焦点丢失');
          if (!panel?.querySelector(content[view]) || panel.hasAttribute('aria-busy')) results.push(view+' 内容缺失');
          if (JSON.stringify(rects())!==before) results.push(view+' 标签移位 '+before+' → '+JSON.stringify(rects()));
        }
        return {results,scrollable,restored:Math.abs(document.querySelector('.chat-log').scrollTop-oldTop)<=1};
      });
      assert.deepEqual(checks,{results:[],scrollable:true,restored:true},'切换标签或阅读位置抖动，宽度 '+width);
    }
    const done=probe.waitForResponse(r=>searchRequest(r.request()));
    releaseSearch(); await done;
    await probe.evaluate(()=>new Promise(requestAnimationFrame));
    assert.ok(await probe.locator('.panel.history .chat-row').first().isVisible());
    await probe.goBack(); await probe.locator('.panel.members .entry').first().waitFor();
    await probe.goForward(); await probe.locator('.panel.history .chat-row').first().waitFor();
    assert.ok(await header.evaluate(el=>el===document.querySelector('.room-header')));
    assert.ok(await strip.evaluate(el=>el===document.querySelector('.tab-strip')));
    assert.equal(await tab('history').getAttribute('aria-current'),'page');
  } finally { releaseWarm(); releaseSearch(); await probe.close(); }
}

(async () => {
  const browser=await chromium.launch({headless:true,args:['--no-proxy-server'],executablePath:process.env.RELAY_TEST_CHROMIUM || undefined});
  try {
    const context=await browser.newContext({viewport:{width:1320,height:820},locale:'zh-CN',colorScheme:'light'});
    const page=await context.newPage(), errors=[];
    page.on('pageerror',error=>errors.push(error.message));
    const origin=process.env.RELAY_TEST_URL;
    const team=process.env.RELAY_TEST_SHARE_URL || origin;
    const click=label=>page.getByRole('button',{name:label,exact:true}).click();
    const visible=text=>page.getByText(text,{exact:true}).first().waitFor();
    async function checkLogos(container) {
      const logos=container.locator('.avatar img');
      await logos.first().waitFor();
      const sources=await logos.evaluateAll(async images=>{
        await Promise.all(images.map(img=>img.decode()));
        return images.filter(img=>img.naturalWidth>0).map(img=>new URL(img.src).pathname);
      });
      assert.deepEqual(new Set(sources),new Set(['/codex.webp','/claude-code.webp','/deepseek-harness.webp']));
    }
    const shot=async(name,subject=page)=>{
      if (process.env.RELAY_TEST_SHOTS) {
        if (subject===page) await page.locator('#notice').waitFor({state:'hidden',timeout:9000});
        await subject.screenshot({path:path.join(process.env.RELAY_TEST_SHOTS,name+'.png')});
      }
    };
    await context.grantPermissions(['clipboard-read','clipboard-write'],{origin});
    const guest=await browser.newContext({viewport:{width:960,height:730},locale:'zh-CN',colorScheme:'light'});
    const invited=await guest.newPage();
    invited.on('pageerror',error=>errors.push(error.message));
    async function rpc(scope,op,data,key) {
      const transport=scope==='owner' ? context : guest;
      const base=scope==='owner'?origin:team;
      const response=await transport.request.post(base+'/api/'+scope+'/'+op,{data,headers:key?{Authorization:'Bearer '+key}:{}});
      assert.equal(response.status(),200,await response.text()); return response.json();
    }
    await page.goto(origin);
    await page.getByRole('heading',{name:'登录工作区'}).waitFor();
    assert.equal(await page.title(),'群主登录 · 叠声');
    await checkLogos(page.getByRole('group',{name:'支持的 Agent'}));
    await shot('网页端-登录');
    await page.emulateMedia({colorScheme:'dark'}); await shot('网页端-登录-深色');
    await page.setViewportSize({width:375,height:740});
    await checkLogos(page.locator('.client-strip'));
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    await page.setViewportSize({width:1320,height:820}); await page.emulateMedia({colorScheme:'light'});
    await page.getByText('在哪里找到密钥文件？').click(); await visible('cat ./build/local-workspace/owner.key');
    await page.getByText('在哪里找到密钥文件？').click();
    await click('进入工作区'); await visible('请填写群主密钥');
    await page.getByLabel('群主密钥',{exact:true}).fill('invalid');
    await click('进入工作区'); await visible('群主密钥无效，请从服务主机重新复制');
    await page.getByLabel('群主密钥',{exact:true}).fill(process.env.RELAY_TEST_OWNER);
    await click('进入工作区'); await visible('创建第一个频道'); await shot('网页端-新工作区');
    await page.locator('#workspace').getByRole('button',{name:'新建群聊'}).click();
    await click('创建群聊'); await visible('请填写群名称');
    await page.getByLabel('群名称',{exact:true}).fill('订单联调');
    await page.getByLabel('讨论范围（可选）').fill('订单详情页、接口契约与发布前的回归验证');
    await click('创建群聊');
    await page.getByRole('heading',{name:'订单联调',exact:true}).waitFor();
    assert.equal(await page.locator('#service').innerText(),new URL(team).host);
    const group=await page.evaluate(()=>location.hash.split('/')[1]);
    await click('邀请 Agent');
    await click('复制链接');
    const link=await page.evaluate(()=>navigator.clipboard.readText());
    assert.ok(link.startsWith(team+'/join#c='));
    await invited.goto(link);
    await invited.getByRole('heading',{name:'# 订单联调'}).waitFor();
    assert.equal(await invited.title(),'频道邀请 · 叠声');
    assert.ok((await invited.getByLabel('给 Agent 的入群指令').inputValue()).includes(link));
    await invited.getByRole('button',{name:'复制入群指令'}).click();
    await invited.getByText('已复制，可以粘贴到 Agent 会话。').waitFor();
    if (team!==origin) {
      await invited.goto(origin+'/join'+new URL(link).hash);
      await invited.getByRole('heading',{name:'# 订单联调'}).waitFor();
      assert.ok((await invited.getByLabel('给 Agent 的入群指令').inputValue()).includes(link));
      await invited.goto(link); await invited.getByRole('heading',{name:'# 订单联调'}).waitFor();
    }
    await shot('网页端-入群',invited);
    const guide=await guest.request.get(link,{headers:{Accept:'text/markdown'}});
    assert.equal(guide.status(),200); assert.match(await guide.text(),/\/api\/project\/wait/);
    const code=new URLSearchParams(new URL(link).hash.slice(1)).get('c');
    await page.keyboard.press('Escape');
    await page.getByRole('dialog').waitFor({state:'hidden'});
    await rpc('owner','create',{name:'发布协调',purpose:'跟进上线时间与回滚窗口'});
    await rpc('owner','create',{name:'设计评审',purpose:'交互方案与组件验收'});
    const members=[];
    for (const [agent,name,role] of [['codex','订单前端','订单详情与页面状态'],['claude','订单后端','接口契约与数据模型'],['deepseek','质量工程','回归测试与发布验收']]) {
      const key=randomBytes(32).toString('hex');
      const m=await rpc('channel','join',{code,agent,name,role},key);
      const p=(op,fields={})=>rpc('project',op,{session:agent,...fields},key);
      const status=await p('status');
      await p('check',{challenge:status.challenge});
      members.push({p,...m});
    }
    const [front,back,qa]=members;
    await front.p('wait',{timeout:0}); await back.p('wait',{timeout:0});
    await page.getByLabel('群消息',{exact:true}).fill('大家核对一下旧订单的展示逻辑，今天一起收口。');
    await click('发送');
    await page.getByRole('log').getByText('大家核对一下旧订单的展示逻辑，今天一起收口。',{exact:true}).waitFor();
    await visible('0/3 已回复');
    const q=await front.p('post',{kind:'question',body:'旧订单没有确认收货时间时，接口会省略这个字段，还是返回 null？',target:back.member,request:'order-q'});
    const received=await back.p('receive',{message:q.id});
    const answer=await back.p('post',{kind:'answer',parent:q.id,ticket:received.ticket,body:'release 分支会返回 null。前端可以据此隐藏确认时间，字段名保持不变。',
      evidence:'release · order_detail_test',request:'order-a'});
    await front.p('accept',{answer:answer.id});
    await qa.p('post',{kind:'broadcast',body:'旧订单、已确认和已取消三组样例已加入回归清单。',request:'qa-update'});
    await front.p('post',{kind:'broadcast',body:'收到，详情页已按这个约定处理，可以开始联调。',request:'ui-update'});
    await page.getByRole('log').getByText('收到，详情页已按这个约定处理，可以开始联调。').waitFor();
    await checkTabSwitches(context,origin+'/#/'+group+'/history');
    await page.getByRole('complementary',{name:'频道成员'}).getByText('DeepSeek Harness').waitFor();
    await checkLogos(page.getByRole('complementary',{name:'频道成员'}));
    await page.getByRole('complementary',{name:'频道成员'}).getByText(/正在监听/).first().waitFor();
    await checkLogos(page.getByRole('log'));
    await page.getByRole('log').evaluate(log=>{log.scrollTop=0;});
    await shot('网页端-频道');
    await page.emulateMedia({colorScheme:'dark'});
    await shot('网页端-深色');
    await page.emulateMedia({colorScheme:'light'});
    await page.getByLabel('查找频道').fill('设计');
    assert.equal(await page.locator('#group-list a').count(),1);
    await page.getByLabel('查找频道').fill('');
    await page.getByLabel('群消息',{exact:true}).fill('待收口的草稿');
    await page.getByRole('link',{name:'设计评审',exact:true}).click();
    await page.getByRole('heading',{name:'设计评审',exact:true}).waitFor();
    await page.getByRole('link',{name:'订单联调',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('#chat-body').value==='待收口的草稿');
    await page.getByRole('link',{name:'已采纳结论'}).click();
    await visible('release 分支会返回 null。前端可以据此隐藏确认时间，字段名保持不变。');
    await page.getByRole('searchbox').fill('order_detail'); await click('搜索');
    await visible('依据　release · order_detail_test');
    await page.getByRole('searchbox').fill('不存在的字段'); await click('搜索'); await visible('没有匹配的记录');
    await click('清除搜索'); await visible('依据　release · order_detail_test');
    await page.getByLabel('频道操作').click(); await click('暂停讨论'); await visible('讨论已暂停');
    await page.getByLabel('频道操作').click(); await click('恢复讨论');
    await page.getByRole('link',{name:'邀请',exact:true}).click();
    await click('新建单次邀请'); await page.getByLabel('项目公开名称').fill('发布值班');
    await click('生成邀请');
    const once=await page.getByLabel('入群链接',{exact:true}).inputValue();
    assert.ok(once.startsWith(team+'/join#i='));
    await invited.goto(once); await invited.getByText('群主邀请你以「发布值班」的身份加入。').waitFor();
    await page.keyboard.press('Escape'); await click('撤销邀请');
    await page.getByRole('dialog').getByRole('button',{name:'撤销邀请'}).click();
    await visible('已撤销');
    await invited.reload(); await invited.getByRole('heading',{name:'邀请已失效'}).waitFor();
    await click('停用链接'); await page.getByRole('dialog').getByRole('button',{name:'停用链接'}).click();
    await invited.goto(link); await invited.getByRole('heading',{name:'邀请已失效'}).waitFor();
    await page.getByRole('navigation',{name:'频道详情'}).getByRole('link',{name:/频道成员/}).click();
    await page.locator('article').filter({hasText:'@订单前端'}).getByRole('button',{name:'成员设置'}).click();
    await page.getByLabel('工作职责',{exact:true}).fill('负责客户端回归'); await click('保存设置'); await visible('负责客户端回归');
    await page.setViewportSize({width:375,height:740});
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth));
    await page.getByRole('link',{name:'对话',exact:true}).click();
    await page.getByRole('log').waitFor();
    await page.getByLabel('群消息',{exact:true}).fill('');
    await page.getByRole('log').evaluate(log=>{log.scrollTop=0;}); await shot('网页端-手机');
    await page.getByRole('navigation',{name:'频道详情'}).getByRole('link',{name:/频道成员/}).click();
    await page.locator('article').filter({hasText:'@订单后端'}).getByRole('button',{name:'移除'}).click();
    await page.getByRole('dialog').getByRole('button',{name:'移除项目'}).click();
    await page.locator('article').filter({hasText:'@订单后端'}).waitFor({state:'detached'});
    await click('邀请同事');
    const humanLink=await page.getByLabel('入群链接',{exact:true}).inputValue();
    assert.ok(humanLink.startsWith(team+'/room/'+group+'#invite='));
    const colleague=await browser.newContext({viewport:{width:375,height:740},locale:'zh-CN'});
    const human=await colleague.newPage();
    human.on('pageerror',error=>errors.push(error.message));
    await human.goto(humanLink);
    await human.getByLabel('你的昵称').waitFor();
    await shot('网页端-同事邀请',human);
    assert.equal(await human.getByLabel('群主密钥',{exact:true}).isVisible(),false);
    await human.getByLabel('你的昵称').fill('小雨');
    await human.getByRole('button',{name:'加入并查看对话'}).click();
    await human.getByRole('log').getByText('大家核对一下旧订单的展示逻辑，今天一起收口。',{exact:true}).waitFor();
    assert.equal(new URL(human.url()).hash.includes('invite='),false);
    assert.equal(await human.getByRole('button',{name:'邀请同事'}).count(),0);
    assert.equal(await human.getByRole('button',{name:'新建群聊'}).count(),0);
    assert.equal(await human.getByRole('link',{name:'邀请',exact:true}).count(),0);
    assert.equal(await human.locator('#group-list a').count(),1);
    await human.getByLabel('发给',{exact:true}).selectOption(front.member);
    await human.getByLabel('群消息',{exact:true}).fill('hi');
    await human.getByRole('button',{name:'发送',exact:true}).click();
    const bubble=human.getByRole('log').locator('article').filter({has:human.getByText('hi',{exact:true})});
    await bubble.getByText('我',{exact:true}).waitFor();
    const mid=Number(await bubble.getAttribute('data-message'));
    const hr=await front.p('receive',{message:mid});
    assert.equal(hr.body,'hi');
    const before=await front.p('history',{topic:mid});
    assert.deepEqual(before.messages.map(m=>m.kind),['question']);
    await front.p('post',{kind:'answer',parent:mid,ticket:hr.ticket,body:'明早十点开始上线。',request:'reply-colleague'});
    await human.getByRole('log').getByText('明早十点开始上线。',{exact:true}).waitFor();
    const {self}=await rpcFromMember('groups');
    const backQuestion=await front.p('post',{kind:'question',target:self.id,body:'请人工确认这个窗口。',request:'ask-colleague'});
    await human.locator('[data-message="'+backQuestion.id+'"]').getByRole('button',{name:'答复问题'}).click();
    await human.getByLabel('群消息',{exact:true}).fill('窗口已确认。');
    await human.getByRole('button',{name:'发送',exact:true}).click();
    await human.getByRole('log').getByText('窗口已确认。',{exact:true}).waitFor();
    await human.setViewportSize({width:1280,height:800}); await shot('网页端-同事频道',human);
    await human.setViewportSize({width:375,height:740});
    await human.reload();
    await human.getByRole('log').getByText('明早十点开始上线。',{exact:true}).waitFor();
    await human.goto(team+'/');
    await human.getByRole('heading',{name:'我的频道'}).waitFor();
    assert.equal(await human.getByLabel('群主密钥',{exact:true}).isVisible(),false);
    await human.getByRole('navigation',{name:'我加入的频道'}).getByRole('link',{name:'# 订单联调'}).click();
    await human.getByRole('log').getByText('明早十点开始上线。',{exact:true}).waitFor();
    assert.ok(await human.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth));
    await page.keyboard.press('Escape');
    await page.locator('article').filter({hasText:'@小雨'}).getByRole('button',{name:'移除'}).click();
    await page.getByRole('dialog').getByRole('button',{name:'移除成员'}).click();
    await human.getByRole('heading',{name:'加入频道'}).waitFor();
    assert.equal(await human.getByRole('log').count(),0);
    assert.equal(await human.getByLabel('群主密钥',{exact:true}).isVisible(),false);
    await colleague.close();
    async function rpcFromMember(op) {
      const response=await colleague.request.post(team+'/api/member/'+group+'/'+op,{data:{}});
      assert.equal(response.status(),200); return response.json();
    }
    await page.locator('#nav-toggle').click();
    await click('退出登录');
    await page.getByRole('heading',{name:'登录工作区'}).waitFor();
    assert.deepEqual(errors,[]);
    console.log('PASS · 桌面与手机慢网标签切换、阅读位置及焦点、群主与同事双向问答、三类 Agent、权限与移除');
  } finally { await browser.close(); }
})().catch(error=>{console.error(error); process.exitCode=1;});
