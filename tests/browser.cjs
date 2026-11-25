const { chromium } = require(process.env.RELAY_TEST_PLAYWRIGHT);
const { randomBytes } = require('node:crypto');
const assert = require('node:assert/strict');
const path = require('node:path');

const appTheme=page=>page.locator('html').evaluate(el=>getComputedStyle(el).colorScheme);

async function selectTheme(page,theme) {
  if (await appTheme(page)===theme) return;
  const toggle=page.locator('#theme-toggle');
  const openDrawer=!(await toggle.isVisible());
  if (openDrawer) await page.locator('#nav-toggle').click();
  await toggle.click();
  if (openDrawer) await page.keyboard.press('Escape');
  assert.equal(await appTheme(page),theme);
}

async function checkThemeChoice(page,shot) {
  const toggle=page.locator('.server-address #theme-toggle');
  await toggle.waitFor();
  assert.equal(await appTheme(page),'dark');
  assert.equal(await toggle.getAttribute('aria-label'),'切换到浅色模式');
  assert.ok(await toggle.evaluate(button=>button.getBoundingClientRect().left>=
    document.getElementById('service').getBoundingClientRect().right));
  await toggle.focus();
  await toggle.press('Enter');
  assert.equal(await appTheme(page),'light');
  assert.equal(await toggle.getAttribute('aria-label'),'切换到深色模式');

  await page.emulateMedia({colorScheme:'dark'});
  assert.equal(await appTheme(page),'light');
  await page.reload();
  await toggle.waitFor();
  assert.equal(await appTheme(page),'light');
  await shot('网页端-浅色主题');

  const invitation=await page.context().newPage();
  try {
    await invitation.goto(new URL('/join',page.url()).href);
    assert.equal(await appTheme(invitation),'light');
    await page.setViewportSize({width:375,height:740});
    await page.locator('#nav-toggle').click();
    assert.ok(await toggle.evaluate(button=>button.offsetWidth>=44 && button.offsetHeight>=44));
    await selectTheme(page,'dark');
    await invitation.waitForFunction(()=>getComputedStyle(document.documentElement).colorScheme==='dark');
    await shot('网页端-手机主题切换');
  } finally { await invitation.close(); }
  await page.keyboard.press('Escape');
  await page.emulateMedia({colorScheme:'light'});
  assert.equal(await appTheme(page),'dark');
  await page.setViewportSize({width:1320,height:820});
}

async function checkUserGuide(page) {
  const trigger=page.getByRole('button',{name:'使用手册',exact:true});
  const guide=page.getByRole('dialog',{name:'使用手册'});
  const viewport=page.viewportSize();
  async function openAndClose() {
    await guide.getByRole('heading',{name:'创建和加入频道'}).waitFor();
    await page.keyboard.press('Escape');
    await guide.waitFor({state:'hidden'});
    await page.waitForFunction(()=>document.activeElement?.dataset.action==='open-manual');
  }
  await trigger.focus();
  await trigger.press('Enter');
  await openAndClose();
  await page.setViewportSize({width:375,height:740});
  await page.locator('#nav-toggle').click();
  await trigger.click();
  await openAndClose();
  await page.keyboard.press('Escape');
  await page.setViewportSize(viewport);
}

async function checkRecoveryDialog(page,origin) {
  await page.goto(origin+'/#channels');
  const open=page.getByRole('button',{name:'使用旧恢复码'});
  const dialog=page.getByRole('dialog',{name:'使用恢复码'});
  const field=dialog.getByLabel('恢复码',{exact:true});
  const close=dialog.getByRole('button',{name:'关闭窗口',exact:true});
  const cancel=dialog.getByRole('button',{name:'取消',exact:true});

  await open.click();
  await field.fill('临时输入');
  await field.fill('');
  await close.click();
  await dialog.waitFor({state:'hidden'});
  await open.click();
  await field.fill('尚未提交的恢复码');
  await cancel.click();
  await dialog.waitFor({state:'hidden'});
  await open.click();
  assert.equal(await field.inputValue(),'');
  await field.fill('使用键盘关闭');
  await field.press('Escape');
  await dialog.waitFor({state:'hidden'});

  const group='a'.repeat(32), code=group+'.'+'b'.repeat(43);
  const apiPath='/api/member/'+group+'/recover';
  let release;
  const pending=new Promise(resolve=>{release=resolve;});
  await page.route('**'+apiPath,async route=>{
    await pending;
    await route.fulfill({status:410,json:{error:'恢复凭证已过期'}});
  },{times:1});
  try {
    await open.click();
    await field.fill(code);
    await Promise.all([
      page.waitForRequest(request=>request.url().endsWith(apiPath)),
      dialog.getByRole('button',{name:'恢复并进入频道'}).click()
    ]);
    await dialog.getByRole('status').filter({hasText:'正在提交…'}).waitFor();
    assert.equal(await close.isDisabled(),true);
    assert.equal(await cancel.isDisabled(),true);
    release();
    await dialog.getByRole('alert').getByText('恢复凭证已过期',{exact:true}).waitFor();
    assert.equal(await field.inputValue(),code);
    await cancel.click();
    await dialog.waitFor({state:'hidden'});
  } finally { release(); }

  await page.route('**/api/account/login',route=>route.fulfill({status:404,json:{error:'未知接口'}}),{times:1});
  const login=page.getByRole('form',{name:'同事账号登录'});
  await login.getByLabel('账号',{exact:true}).fill('browser-test');
  await login.getByLabel('密码',{exact:true}).fill('test-password');
  await login.getByRole('button',{name:'登录',exact:true}).click();
  await login.getByText('当前服务器未提供同事账号登录，请核对叠声的服务地址和版本。',{exact:true}).waitFor();
}

async function checkPendingInviteDismissal(page) {
  const inviteSelector='button[data-action="invite"]';
  const prompt=page.getByRole('dialog',{name:'邀请 Agent'});
  const other=page.getByRole('dialog',{name:'新建群聊'});
  let release;
  const pending=new Promise(resolve=>{release=resolve;});
  await page.route('**/api/owner/channel_link',async route=>{
    await pending;
    await route.fulfill({json:{url:'/join#c=placeholder',expires:Math.floor(Date.now()/1000)+60}});
  },{times:1});
  try {
    await page.locator(inviteSelector).click();
    await prompt.getByText('正在准备频道链接…',{exact:true}).waitFor();
    await prompt.getByRole('button',{name:'关闭窗口'}).click();
    await prompt.waitFor({state:'hidden'});
    await page.locator('#nav-heading').getByRole('button',{name:'新建群聊'}).click();
    await other.getByLabel('群名称',{exact:true}).fill('正在编辑的名称');
    release();
    await page.locator(inviteSelector+':enabled').waitFor({state:'attached'});
    assert.equal(await prompt.isVisible(),false);
    assert.equal(await other.getByLabel('群名称',{exact:true}).inputValue(),'正在编辑的名称');
    await other.getByRole('button',{name:'取消',exact:true}).click();
    await other.waitFor({state:'hidden'});
  } finally { release(); }
}

async function checkAccountSettings(page,login) {
  await page.locator('#nav-toggle').click();
  const trigger=page.locator('#account').getByRole('button',{name:'账号设置'});
  assert.match(await trigger.innerText(),/小雨/);
  await trigger.tap();
  const dialog=page.getByRole('dialog',{name:'账号设置'});
  const username=dialog.getByLabel('登录账号名',{exact:true});
  const current=dialog.getByLabel('当前密码',{exact:true});
  const next=dialog.getByLabel('新密码（选填）',{exact:true});
  const confirm=dialog.getByLabel('确认新密码',{exact:true});
  const save=dialog.getByRole('button',{name:'保存修改',exact:true});
  assert.equal(await username.inputValue(),login.username);
  await dialog.getByText(/至少 8 个字符/).waitFor();
  assert.ok(await dialog.evaluate(el=>el.scrollWidth<=el.clientWidth));
  if (process.env.RELAY_TEST_SHOTS)
    await page.screenshot({path:path.join(process.env.RELAY_TEST_SHOTS,'网页端-账号设置.png')});

  const updated={username:'xiaoyu.renamed',password:'Next!842'};
  await username.fill(updated.username);
  await current.fill('incorrect');
  await next.fill(updated.password);
  await save.tap();
  await dialog.getByText('两次输入的新密码不一致',{exact:true}).waitFor();
  await confirm.fill(updated.password);
  await save.tap();
  await dialog.getByText('当前密码不正确',{exact:true}).waitFor();
  await current.fill(login.password);
  await save.tap();
  await dialog.waitFor({state:'hidden'});
  await page.locator('#notice').getByText('账号设置已保存',{exact:true}).waitFor();
  await page.waitForFunction(()=>document.activeElement===document.getElementById('account-identity'));
  await trigger.tap();
  assert.equal(await username.inputValue(),updated.username);
  assert.equal(await current.inputValue(),'');
  await dialog.getByRole('button',{name:'取消',exact:true}).tap();
  await dialog.waitFor({state:'hidden'});
  await page.keyboard.press('Escape');
  return updated;
}

async function checkAnnouncementsAndAdmins(owner,human,front) {
  await owner.keyboard.press('Escape');
  await owner.getByRole('dialog').waitFor({state:'hidden'});
  await owner.setViewportSize({width:1320,height:820});
  const tab=(page,label)=>page.getByRole('navigation',{name:'频道详情'}).getByRole('link',
    {name:label==='频道成员'?/^频道成员/:label,exact:true});
  const ownerDialog=owner.getByRole('dialog');
  const ownerText=ownerDialog.getByLabel('公告正文',{exact:true});
  const publish=ownerDialog.getByRole('button',{name:'发布公告',exact:true});
  const openPublish=owner.getByRole('button',{name:'发布公告',exact:true});
  const ownerMember=owner.locator('.panel.members article').filter({hasText:'@小雨'});
  await tab(human,'公告').tap();
  await human.getByText('暂无群公告',{exact:true}).waitFor();
  assert.equal(await human.getByRole('button',{name:'发布公告',exact:true}).count(),0);
  await tab(owner,'公告').click();
  await openPublish.click();
  assert.equal(await ownerText.evaluate(el=>document.activeElement===el),true);
  await publish.click();
  await ownerDialog.getByText('请填写公告正文。',{exact:true}).waitFor();
  await ownerText.press('Escape');
  await ownerDialog.waitFor({state:'hidden'});
  await owner.waitForFunction(()=>document.activeElement?.dataset.action==='edit-announcement');
  await openPublish.click();
  const first='## 协作规则\n\n- 发布前核对 **验收记录**';
  await ownerText.fill(first);
  let release;
  const pending=new Promise(resolve=>{release=resolve;});
  await owner.route('**/api/owner/announcement-publish',async route=>{
    await pending;
    await route.fulfill({status:503,json:{error:'公告暂时无法发布，请重试。'}});
  },{times:1});
  try {
    await publish.click();
    await ownerDialog.getByRole('status').filter({hasText:'正在提交…'}).waitFor();
    assert.equal(await publish.isDisabled(),true);
    release();
    await ownerDialog.getByRole('alert').getByText('公告暂时无法发布，请重试。',{exact:true}).waitFor();
    assert.equal(await ownerText.inputValue(),first);
  } finally { release(); }
  await publish.click();
  await ownerDialog.waitFor({state:'hidden'});
  await owner.getByRole('heading',{name:'协作规则',exact:true}).waitFor();
  const receipts=owner.locator('.announcement-receipts');
  await receipts.getByText(/^已写入 0\/\d+ 个 Agent 项目$/).waitFor();
  await receipts.locator('summary').press('Enter');
  const frontReceipt=receipts.locator('li').filter({hasText:front.name});
  await frontReceipt.getByText('待写入',{exact:true}).waitFor();
  const pendingAnnouncement=await front.p('context',{announcements:'AGENTS.md'});
  await front.p('announcement-ack',{version:pendingAnnouncement.notice.version,file:'AGENTS.md'});
  await frontReceipt.getByText('已写入',{exact:true}).waitFor();
  await receipts.getByText(/^已写入 1\/\d+ 个 Agent 项目$/).waitFor();
  await human.bringToFront();
  await human.getByRole('heading',{name:'协作规则',exact:true}).waitFor();
  assert.equal(await human.locator('.announcement-body strong').innerText(),'验收记录');
  assert.equal(await human.getByRole('button',{name:'编辑公告',exact:true}).count(),0);

  await owner.bringToFront();
  await tab(owner,'频道成员').click();
  const oldBackend=await owner.context().newPage();
  try {
    await oldBackend.route('**/api/owner/detail',async route=>{
      const response=await route.fetch();
      const details=await response.json();
      for (const person of details.members) delete person.admin;
      await route.fulfill({response,json:details});
    });
    await oldBackend.goto(owner.url());
    const colleague=oldBackend.locator('.panel.members article').filter({hasText:'@小雨'});
    await colleague.waitFor();
    assert.equal(await colleague.getByRole('button',{name:'设为管理员',exact:true}).count(),0);
  } finally { await oldBackend.close(); }
  await ownerMember.getByRole('button',{name:'设为管理员',exact:true}).click();
  await ownerDialog.getByRole('button',{name:'设为管理员',exact:true}).click();
  await ownerMember.getByText('管理员',{exact:true}).waitFor();
  await tab(owner,'公告').click();
  await owner.getByRole('button',{name:'编辑公告',exact:true}).click();
  const draft='发布时附上验证结果。';
  await ownerText.fill(draft);

  await human.bringToFront();
  await human.getByRole('button',{name:'编辑公告',exact:true}).tap();
  const humanDialog=human.getByRole('dialog',{name:'编辑群公告'});
  await human.emulateMedia({reducedMotion:'reduce'});
  assert.ok(await humanDialog.evaluate(el=>el.scrollWidth<=el.clientWidth && el.getBoundingClientRect().width<innerWidth));
  if (process.env.RELAY_TEST_SHOTS) {
    await human.screenshot({path:path.join(process.env.RELAY_TEST_SHOTS,'网页端-管理员编辑公告.png')});
  }
  const current='先同步接口变更。';
  await humanDialog.getByLabel('公告正文',{exact:true}).fill(current);
  await humanDialog.getByRole('button',{name:'发布公告',exact:true}).tap();
  await humanDialog.waitFor({state:'hidden'});
  await human.getByText(current,{exact:true}).waitFor();
  const channelMenu=human.getByLabel('频道操作',{exact:true});
  await channelMenu.tap();
  await human.getByRole('button',{name:'暂停讨论',exact:true}).waitFor();
  assert.equal(await human.getByRole('button',{name:'解散频道',exact:true}).count(),0);
  await channelMenu.tap();
  await tab(human,'邀请').tap();
  await human.getByRole('heading',{name:'频道入群链接',exact:true}).waitFor();
  await tab(human,'频道成员').tap();
  assert.equal(await human.getByRole('button',{name:/^(设为|取消)管理员$/}).count(),0);
  await tab(human,'公告').tap();

  await owner.bringToFront();
  await publish.click();
  await ownerDialog.getByRole('alert').getByText('公告已更新。请核对当前公告，再发布你的改动。',{exact:true}).waitFor();
  await ownerDialog.getByText(current,{exact:true}).waitFor();
  assert.equal(await ownerText.inputValue(),draft);
  await publish.click();
  await ownerDialog.waitFor({state:'hidden'});
  await owner.getByText(draft,{exact:true}).waitFor();
  await receipts.getByText(/^已写入 0\/\d+ 个 Agent 项目$/).waitFor();
  await owner.getByRole('button',{name:'编辑公告',exact:true}).click();
  await ownerText.fill('');
  await publish.click();
  await owner.getByText('暂无群公告',{exact:true}).waitFor();
  await tab(owner,'频道成员').click();
  await ownerMember.getByRole('button',{name:'取消管理员',exact:true}).click();
  await ownerDialog.getByRole('button',{name:'取消管理员',exact:true}).click();
  await ownerMember.getByRole('button',{name:'设为管理员',exact:true}).waitFor();
  await human.bringToFront();
  await human.getByText('暂无群公告',{exact:true}).waitFor();
  await human.getByRole('button',{name:'发布公告',exact:true}).waitFor({state:'hidden'});
  await tab(human,'邀请').waitFor({state:'hidden'});
  await human.emulateMedia({reducedMotion:'no-preference'});
  await owner.setViewportSize({width:375,height:740});
}

async function checkMarkdownRendering(page,front,back) {
  const source=[
    '## Markdown 验收', '',
    '**粗体内容**、*斜体内容*、~~删除内容~~和 `inline <tag>`。',
    '普通换行的第二行。', '',
    '- 第一项', '  - 嵌套项', '- [x] 已完成', '- [ ] 待处理', '',
    '3. 第三步', '', '> 引用内容', '',
    '| 项目 | 结果 |', '| :--- | ---: |', '| 长表格 | '+ 'table-'.repeat(35)+' |', '',
    '```js', 'const markup = "<img src=x onerror=alert(1)>";', '// '+'code-'.repeat(50), '```', '',
    '[安全链接](https://example.com/markdown?q=1&lang=zh "说明")', '',
    '[危险链接](javascript:alert%281%29)', '[数据链接](data:text/html,bad)', '',
    '![图片链接](https://example.com/markdown-image.png)', '',
    '<script>window.markdownXss = true</script>',
    '<button data-action="logout" onclick="window.markdownXss=true">伪造按钮</button>',
    '<img src="https://example.com/tracker.png" onerror="window.markdownXss=true">'
  ].join('\n');
  const question=await front.p('post',{kind:'question',target:back.member,body:'请核对 Markdown 消息。',request:'markdown-question'});
  const receipt=await back.p('receive',{message:question.id});
  const answer=await back.p('post',{kind:'answer',parent:question.id,ticket:receipt.ticket,body:source,request:'markdown-answer'});
  const row=page.locator('.chat-row[data-message="'+answer.id+'"]');
  const body=row.locator('.markdown');
  await body.getByRole('heading',{name:'Markdown 验收'}).waitFor();
  assert.equal(await body.locator('strong').innerText(),'粗体内容');
  assert.equal(await body.locator('em').innerText(),'斜体内容');
  assert.equal(await body.locator('del').innerText(),'删除内容');
  assert.equal(await body.locator('ul ul li').innerText(),'嵌套项');
  assert.equal(await body.locator('ol').getAttribute('start'),'3');
  assert.equal(await body.locator('blockquote').innerText(),'引用内容');
  assert.equal(await body.locator('input:checked:disabled').count(),1);
  assert.equal(await body.locator('input:not(:checked):disabled').count(),1);
  assert.ok((await body.locator('p').first().innerText()).includes('\n普通换行的第二行。'));
  assert.ok((await body.locator('p').first().textContent()).includes('\n普通换行的第二行。'));
  assert.ok((await body.locator('pre code').innerText()).includes('<img src=x onerror=alert(1)>'));
  assert.equal(await body.locator('script,img,button,svg,iframe,[data-action],[onclick]').count(),0);
  assert.equal(await page.evaluate(()=>window.markdownXss),undefined);
  assert.equal(await body.getByRole('link',{name:'危险链接',exact:true}).count(),0);
  assert.equal(await body.getByRole('link',{name:'数据链接',exact:true}).count(),0);
  assert.equal(await body.getByRole('link',{name:'图片链接',exact:true}).getAttribute('href'),'https://example.com/markdown-image.png');
  const link=body.getByRole('link',{name:'安全链接',exact:true});
  assert.equal(await link.getAttribute('target'),'_blank');
  assert.equal(await link.getAttribute('rel'),'noopener noreferrer');
  await link.focus(); assert.equal(await link.evaluate(el=>el===document.activeElement),true);
  const original=(await front.p('history',{query:'Markdown 验收'})).messages.find(m=>m.id===answer.id);
  assert.equal(original.body,source);
  for (const [width,height,theme] of [[1320,820,'light'],[375,740,'dark']]) {
    await page.setViewportSize({width,height}); await selectTheme(page,theme);
    await body.getByRole('heading',{name:'Markdown 验收'}).scrollIntoViewIfNeeded();
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    assert.equal(await body.locator('pre').evaluate(el=>el.scrollWidth>el.clientWidth),true);
    assert.equal(await body.locator('.markdown-table').evaluate(el=>el.scrollWidth>el.clientWidth),true);
    await body.locator('pre').focus(); await page.keyboard.press('ArrowRight');
    assert.equal(await body.locator('pre').evaluate(el=>el===document.activeElement),true);
    if (process.env.RELAY_TEST_SHOTS) {
      await body.getByRole('heading',{name:'Markdown 验收'}).scrollIntoViewIfNeeded();
      await page.screenshot({path:path.join(process.env.RELAY_TEST_SHOTS,'Markdown-'+theme+'.png')});
      await body.locator('.markdown-table').scrollIntoViewIfNeeded();
      await page.screenshot({path:path.join(process.env.RELAY_TEST_SHOTS,'Markdown-'+theme+'-代码表格.png')});
    }
  }
  await page.setViewportSize({width:1320,height:820}); await selectTheme(page,'light');
  await row.getByRole('link',{name:'话题 #'+question.id,exact:true}).click();
  await page.getByRole('log').getByRole('heading',{name:'Markdown 验收'}).waitFor();
  await page.getByRole('link',{name:'返回群记录'}).click();
  await front.p('receive',{message:answer.id});
  await body.getByRole('heading',{name:'Markdown 验收'}).waitFor();
  // A missing optional browser dependency preserves message text and the composer.
  const fallback=await page.context().newPage();
  try {
    await fallback.route('**/vendor-marked.js',route=>route.abort());
    await fallback.goto(page.url());
    const plain=fallback.locator('.chat-row[data-message="'+answer.id+'"] .markdown-fallback');
    await plain.waitFor(); assert.equal(await plain.textContent(),source);
    await fallback.getByRole('textbox',{name:'群消息',exact:true}).waitFor();
  } finally { await fallback.close(); }
}

async function checkDeliveryModes(page,front,back,qa) {
  const input=page.getByRole('textbox',{name:'群消息',exact:true});
  const list=page.getByRole('listbox',{name:'选择频道成员'});
  const quiet=page.getByRole('checkbox',{name:'仅同步',exact:true});
  const log=page.getByRole('log');
  const note='@订单前端 @订单后端 十点发布，供大家查阅。';
  await page.getByRole('complementary',{name:'频道成员'}).getByText('订单后端',{exact:true}).waitFor();
  await input.fill('@前'); await list.getByRole('option',{name:/订单前端/}).click();
  await page.keyboard.insertText('@后'); await list.getByRole('option',{name:/订单后端/}).click();
  await page.keyboard.insertText('十点发布，供大家查阅。');
  assert.equal(await input.inputValue(),note);
  await quiet.check();
  const [sent]=await Promise.all([page.waitForRequest(r=>r.url().endsWith('/broadcast')),input.press('Enter')]);
  assert.deepEqual(sent.postDataJSON().mentions,[front.member,back.member]);
  const notice=log.locator('article').filter({has:page.getByText(note,{exact:true})});
  await notice.getByText('仅同步',{exact:true}).waitFor();
  assert.equal(await quiet.isChecked(),false);
  for (const member of [front,back,qa]) {
    assert.equal((await member.p('wait',{timeout:0})).delivery,null);
    assert.deepEqual((await member.p('inbox')).messages,[]);
    const personal=await member.p('history',{scope:'mine',query:note});
    assert.equal(personal.messages.length,member===qa?0:1);
  }

  await input.fill('@前');
  await list.getByRole('option',{name:/订单前端/}).click();
  await page.keyboard.insertText('@后');
  await list.getByRole('option',{name:/订单后端/}).waitFor();
  await input.press('Enter');
  await page.keyboard.insertText('请分别确认发布窗口。');
  const question=await input.inputValue();
  await input.press('Enter');
  const row=log.locator('article').filter({has:page.getByText(question,{exact:true})});
  await row.getByText('0/2 已回复',{exact:true}).waitFor();
  await row.getByText('@订单后端 · 待签收',{exact:true}).waitFor();
  assert.equal(await row.count(),1);
  assert.equal(await row.getByText('全体成员',{exact:true}).count(),0);
  const topic=Number(await row.getAttribute('data-message'));
  const first=(await front.p('wait',{timeout:0})).delivery;
  assert.equal(first.topic,topic);
  await row.getByText('@订单前端 · 已签收',{exact:true}).waitFor();
  await front.p('post',{kind:'answer',parent:first.id,ticket:first.ticket,body:'前端已确认窗口。',request:'multi-front'});
  await row.getByText('1/2 已回复',{exact:true}).waitFor();
  assert.equal((await qa.p('wait',{timeout:0})).delivery,null);
  const second=(await back.p('wait',{timeout:0})).delivery;
  assert.equal(second.topic,topic);
  await back.p('post',{kind:'answer',parent:second.id,ticket:second.ticket,body:'后端已确认窗口。',request:'multi-back'});
  await row.getByText('2/2 已回复',{exact:true}).waitFor();
  await row.getByRole('link',{name:'话题 #'+topic,exact:true}).click();
  await log.getByText('后端已确认窗口。',{exact:true}).waitFor();
  assert.equal(await log.locator('article').count(),3);
  await page.getByRole('link',{name:'返回群记录'}).click();
  await input.waitFor();
}

async function checkPersonalFeed(page,group,host,front,qa) {
  const ranges=page.getByRole('navigation',{name:'消息范围'});
  const mine=ranges.getByRole('link',{name:'只看与我有关',exact:true});
  const all=ranges.getByRole('link',{name:'全部消息',exact:true});
  const log=page.getByRole('log');
  const unrelated='收到，详情页已按这个约定处理，可以开始联调。';
  const root=await front.p('post',{kind:'group_question',targets:[host,qa.member],body:'请群主和测试确认发布。',request:'personal-question'});
  const [question]=(await front.p('history',{topic:root.id})).messages;
  const qaBranch=question.recipients.find(recipient=>recipient.member===qa.member);
  const receipt=await qa.p('receive',{message:qaBranch.question});
  const sibling='这个分支的回归已完成。';
  await qa.p('post',{kind:'answer',parent:receipt.id,ticket:receipt.ticket,body:sibling,request:'personal-sibling'});
  await log.getByText(sibling,{exact:true}).waitFor();
  await mine.focus(); await mine.press('Enter');
  const row=log.locator('[data-message="'+root.id+'"]');
  await row.getByText('1/2 已回复',{exact:true}).waitFor();
  assert.equal(await mine.getAttribute('aria-current'),'true');
  assert.ok(await mine.evaluate(a=>a===document.activeElement));
  assert.equal(await log.getByText(sibling,{exact:true}).count(),0);
  assert.equal(await log.getByText(unrelated,{exact:true}).count(),0);
  await row.getByRole('link',{name:'话题 #'+root.id}).click();
  await log.getByText(sibling,{exact:true}).waitFor();
  await page.getByRole('link',{name:'返回群记录'}).click();
  await row.waitFor();
  assert.equal(await mine.getAttribute('aria-current'),'true');
  assert.equal(await log.getByText(sibling,{exact:true}).count(),0);

  await page.getByRole('button',{name:'搜索频道消息'}).click();
  await page.getByRole('searchbox').fill('详情页已按这个约定');
  await page.getByRole('button',{name:'搜索',exact:true}).click();
  await page.getByRole('heading',{name:'没有匹配的记录'}).waitFor();
  await all.click();
  await log.getByText(unrelated,{exact:true}).waitFor();
  await page.getByRole('button',{name:'清除搜索'}).click();
  await mine.click(); await row.waitFor();
  await page.goto(new URL('/#/'+group+'/history',page.url()).href);
  await row.waitFor();
  assert.equal(await mine.getAttribute('aria-current'),'true');
  await all.click();
  await log.getByText(unrelated,{exact:true}).waitFor();
}

async function checkPersonalPaging(page,sendOwn,front) {
  const body=n=>'个人分页验收 '+n;
  const unrelated='不属于群主的分页记录';
  await sendOwn(body(0),'mine-page-0');
  await front.p('post',{kind:'broadcast',body:unrelated,request:'other-page'});
  for (let i=1;i<=41;i++) await sendOwn(body(i),'mine-page-'+i);
  await page.getByRole('link',{name:'对话',exact:true}).click();
  const ranges=page.getByRole('navigation',{name:'消息范围'});
  const log=page.getByRole('log');
  await ranges.getByRole('link',{name:'只看与我有关',exact:true}).click();
  await log.getByText(body(41),{exact:true}).waitFor();
  assert.equal(await log.getByText(body(0),{exact:true}).count(),0);
  await log.getByRole('button',{name:'查看更早消息'}).click();
  await log.getByText(body(0),{exact:true}).waitFor();
  assert.equal(await log.getByText(unrelated,{exact:true}).count(),0);
  await ranges.getByRole('link',{name:'全部消息',exact:true}).click();
  await log.getByRole('button',{name:'查看更早消息'}).click();
  await log.getByText(unrelated,{exact:true}).waitFor();
}

async function checkTopicDirectory(page,group,rpc,front,qa) {
  const title='v13 可以交付了吗？';
  const root=await rpc('owner','speak',{group,target:front.member,body:title,request:'topic-directory'});
  const take=(peer,message)=>peer.p('receive',{message,collaborate:true});
  const act=(peer,e,decision,fields={})=>peer.p('turn',{decision,message:e.id,ticket:e.ticket,...fields});
  const request=await act(front,await take(front,root.id),'ask',{target:qa.member,body:'请核对 v13'});
  const tab=page.getByRole('navigation',{name:'频道详情'}).getByRole('link',{name:'话题',exact:true});
  await tab.click();
  const category=page.getByRole('combobox',{name:'分类',exact:true});
  const status=page.getByRole('combobox',{name:'状态',exact:true});
  assert.equal(await status.locator('option[value="accepted"]').count(),0);
  await category.selectOption('collaboration');
  await status.selectOption('active');
  const entry=page.locator('article[data-topic="'+root.id+'"]');
  await entry.getByRole('link',{name:title}).waitFor();
  assert.equal(await page.locator('.topic-row').count(),1);
  const reply=await act(qa,await take(qa,request.id),'reply',{body:'v13 验收已完成'});
  await act(front,await take(front,reply.id),'reply',{body:'可以交付'});
  await page.getByRole('heading',{name:'没有符合条件的话题'}).waitFor();
  await status.selectOption('answered');
  await entry.getByRole('link',{name:title}).click();
  await page.getByRole('log').getByText('v13 验收已完成',{exact:true}).waitFor();
  assert.equal(await tab.getAttribute('aria-current'),'page');
  await page.getByRole('link',{name:'返回话题列表'}).click();
  await entry.getByText('已答复',{exact:true}).waitFor();
  assert.equal(await category.inputValue(),'collaboration');
  assert.equal(await status.inputValue(),'answered');
  if (process.env.RELAY_TEST_SHOTS)
    await page.screenshot({path:path.join(process.env.RELAY_TEST_SHOTS,'网页端-话题目录.png')});
  await page.getByRole('link',{name:'对话',exact:true}).click();
}

async function checkProjectInbox(context,origin,rpc) {
  const {group}=await rpc('owner','create',{name:'收件箱验证'});
  const page=await context.newPage();
  try {
    await page.setViewportSize({width:1320,height:820});
    const link=await rpc('owner','channel_link',{group});
    const code=link.url.split('#c=')[1];
    const frontKey=randomBytes(32).toString('hex');
    const front=await rpc('channel','join',{code,agent:'codex',name:'项目甲'},frontKey);
    const back=await rpc('channel','join',{code,agent:'claude',name:'项目乙'},randomBytes(32).toString('hex'));
    const body='请分别核对发布窗口。';
    const root=await rpc('owner','group_question',{group,targets:[front.member,back.member],body,request:'inbox-fanout'});
    for (let i=1;i<=21;i++) {
      await rpc('owner','speak',{group,target:back.member,body:'独立核对任务 '+i,request:'inbox-'+i});
    }
    await page.goto(origin+'/#/'+group+'/history');
    await page.getByRole('heading',{name:'收件箱验证',exact:true}).waitFor();
    const roster=page.getByRole('complementary',{name:'频道成员'});
    const emptyTrigger=roster.getByRole('button',{name:'查看处理中任务，0 项'});
    const processing=page.getByRole('dialog',{name:'项目收件箱 · 处理中'});
    await emptyTrigger.focus();
    await emptyTrigger.press('Enter');
    await processing.getByText('暂无处理中任务。').waitFor();
    await page.keyboard.press('Escape');
    await processing.waitFor({state:'hidden'});
    await page.waitForFunction(()=>document.activeElement?.getAttribute('aria-label')==='查看处理中任务，0 项');

    const {delivery}=await rpc('project','wait',{session:'inbox-fixture',timeout:0},frontKey);
    assert.equal(delivery.topic,root.id);
    const pending=page.getByRole('dialog',{name:'项目收件箱 · 待处理'});
    let release;
    const delayed=new Promise(resolve=>{release=resolve;});
    await page.route('**/api/owner/inbox',async route=>{
      await delayed;
      await route.fulfill({status:500,json:{error:'任务明细暂不可用'}});
    },{times:1});
    try {
      await roster.getByRole('button',{name:'查看待处理任务，22 项'}).click();
      await pending.getByText('正在读取任务…').waitFor();
      release();
      await pending.getByText('任务明细暂不可用').waitFor();
    } finally { release(); }
    await pending.getByRole('button',{name:'重试',exact:true}).click();
    await pending.getByRole('link',{name:/独立核对任务 21/}).waitFor();
    assert.equal(await pending.getByRole('listitem').count(),20);
    await pending.getByRole('button',{name:'查看更早任务'}).click();
    const outstanding=pending.getByRole('link',{name:/请分别核对发布窗口/});
    assert.match(await outstanding.innerText(),/群主 → 项目乙/);
    assert.equal(await pending.getByRole('listitem').count(),22);
    if (process.env.RELAY_TEST_SHOTS)
      await page.screenshot({path:path.join(process.env.RELAY_TEST_SHOTS,'网页端-项目收件箱.png')});
    await outstanding.click();
    const focused=page.locator('.message-located[data-message="'+root.id+'"]');
    await focused.getByText(body,{exact:true}).waitFor();
    await page.waitForFunction(id=>document.activeElement?.dataset.message===String(id),root.id);
    await focused.getByText('@项目甲 · 已签收',{exact:true}).waitFor();
    assert.equal(await pending.isVisible(),false);

    await page.setViewportSize({width:375,height:740});
    await page.locator('[data-view="members"]').click();
    await page.getByRole('button',{name:'查看处理中任务，1 项'}).click();
    const claimed=processing.getByRole('link',{name:/请分别核对发布窗口/});
    await claimed.waitFor();
    assert.match(await claimed.innerText(),/群主 → 项目甲/);
    assert.ok(await processing.evaluate(el=>el.getBoundingClientRect().width<=innerWidth));
    if (process.env.RELAY_TEST_SHOTS)
      await page.screenshot({path:path.join(process.env.RELAY_TEST_SHOTS,'网页端-手机收件箱.png')});
  } finally {
    await page.close();
    await rpc('owner','dissolve',{group});
  }
}

async function checkPrivateChat(human,ownerPage,rpc,front) {
  const tabs=human.getByRole('navigation',{name:'频道详情'});
  const panel=human.getByRole('region',{name:'私聊',exact:true});
  const body=human.getByRole('textbox',{name:'私聊消息'});
  const publicInput=human.getByRole('textbox',{name:'群消息',exact:true});
  const transcript=human.getByRole('log',{name:'私聊记录'});
  const secret='仅对端可读的样本 QA-X71';
  const answer='QA-X71 的内部答复';
  const summary='已确认测试样本的规则版本';
  await publicInput.fill('公开草稿');
  await tabs.getByRole('link',{name:'私聊',exact:true}).tap();
  await panel.getByRole('link',{name:'新建私聊'}).tap();
  await panel.getByRole('link',{name:/订单前端/}).tap();
  await panel.getByRole('link',{name:'新建询问'}).tap();
  const title=human.getByLabel('私聊标题（选填）');
  await title.fill('样本版本核对');
  await body.fill(secret);
  const choose=human.getByRole('button',{name:'选择频道话题'});
  const picker=human.getByRole('dialog',{name:'选择频道话题'});
  const query=picker.getByRole('searchbox',{name:'搜索话题'});
  await choose.tap();
  await query.fill('没有的标题');
  await query.press('Enter');
  await picker.getByText('没有符合搜索的话题。').waitFor();
  await query.press('Escape');
  await picker.waitFor({state:'hidden'});
  assert.equal(await title.inputValue(),'样本版本核对');
  assert.equal(await body.inputValue(),secret);
  await choose.tap();
  await query.fill('v13 可以交付');
  await picker.getByRole('button',{name:'搜索',exact:true}).tap();
  const option=picker.getByRole('button',{name:/v13 可以交付了吗/});
  await option.waitFor();
  if (process.env.RELAY_TEST_SHOTS) {
    await human.screenshot({path:path.join(process.env.RELAY_TEST_SHOTS,'网页端-手机私聊话题选择.png')});
  }
  await option.tap();
  await picker.waitFor({state:'hidden'});
  await human.getByText(/已关联 #\d+ · v13 可以交付了吗/).waitFor();
  await tabs.getByRole('link',{name:'对话',exact:true}).tap();
  await human.getByRole('log',{name:'群消息记录'}).waitFor();
  await human.goBack();
  await human.getByText(/已关联 #\d+ · v13 可以交付了吗/).waitFor();
  assert.equal(await title.inputValue(),'样本版本核对');
  assert.equal(await body.inputValue(),secret);
  assert.equal(await human.getByRole('button',{name:'发送私聊'}).isEnabled(),true);
  await panel.getByText('接收尚未开启 · 首次发送会提醒对方',{exact:true}).waitFor();
  await body.press('Enter');
  await transcript.getByText(secret,{exact:true}).waitFor();
  await transcript.getByText('已排队 · 等待对方开启接收',{exact:true}).waitFor();
  await panel.getByRole('status').filter({hasText:'提醒已排队 · 等待对方 Agent 接收'}).waitFor();
  const setup=await rpc('project','next',{session:'private-setup-test',private_setup:true},front.key);
  assert.equal(setup.delivery,null);
  assert.equal(setup.notice.kind,'private_setup');
  await rpc('project','private-setup-response',{session:'private-setup-test',id:setup.notice.id,decision:'notified'},front.key);
  await panel.getByRole('status').filter({hasText:'提醒已送达 · 等待对方开启接收'}).waitFor();
  const threadUrl=new URL(human.url());
  await panel.getByRole('link',{name:/我的私聊/}).tap();
  const token=randomBytes(32).toString('hex');
  const run=(action,data={})=>rpc('private',action,{session:'test-private',...data},token);
  await rpc('private','enable',{credential:token},front.key);
  const job=(await run('next')).delivery;
  assert.equal(job.body,secret);
  assert.equal(job.history.length,0);
  assert.equal(job.topic.title,'v13 可以交付了吗？');
  await run('reply',{thread:job.thread,message:job.id,ticket:job.ticket,body:'**'+answer+'**'});
  await panel.getByText('1 条未读',{exact:true}).waitFor();
  await panel.getByRole('link',{name:/样本版本核对/}).tap();
  await transcript.getByText(answer,{exact:true}).waitFor();
  assert.equal(await transcript.locator('.markdown strong').innerText(),answer);
  await panel.getByRole('status').filter({hasText:'私聊在线'}).waitFor();
  await panel.getByRole('link',{name:'查看关联话题 #'+job.topic.id}).tap();
  await human.getByRole('log',{name:'群消息记录'}).getByText('v13 可以交付了吗？',{exact:true}).waitFor();
  assert.equal(await human.getByText(secret,{exact:true}).count(),0);
  await human.goBack();
  await transcript.getByText(answer,{exact:true}).waitFor();
  assert.equal(await human.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  if (process.env.RELAY_TEST_SHOTS) {
    await human.screenshot({path:path.join(process.env.RELAY_TEST_SHOTS,'网页端-手机私聊.png')});
  }
  const other=await ownerPage.context().newPage();
  try {
    await other.goto(new URL(threadUrl.hash,ownerPage.url()).href);
    await other.getByRole('heading',{name:'无法打开私聊'}).waitFor();
    assert.equal(await other.getByText(secret,{exact:true}).count(),0);
  } finally { await other.close(); }
  await transcript.getByRole('button',{name:'分享结论到频道'}).tap();
  const confirm=human.getByRole('dialog',{name:'分享结论到频道'});
  const edit=confirm.getByRole('textbox',{name:'公开内容'});
  await edit.fill('暂不发布的内容');
  await confirm.getByRole('button',{name:'取消',exact:true}).tap();
  await confirm.waitFor({state:'hidden'});
  await transcript.getByRole('button',{name:'分享结论到频道'}).tap();
  assert.equal(await edit.inputValue(),'');
  await confirm.getByRole('button',{name:'发布到 #订单联调'}).tap();
  await confirm.getByText('请填写公开内容').waitFor();
  await edit.fill(summary);
  await confirm.getByRole('button',{name:'发布到 #订单联调'}).tap();
  await confirm.waitFor({state:'hidden'});
  await tabs.getByRole('link',{name:'对话',exact:true}).tap();
  await human.getByRole('log',{name:'群消息记录'}).getByText(summary,{exact:true}).waitFor();
  assert.equal(await human.getByText(secret,{exact:true}).count(),0);
  assert.equal(await human.getByText(answer,{exact:true}).count(),0);
  assert.equal(await publicInput.inputValue(),'公开草稿');
  await publicInput.fill('');
}

async function checkAgentBinding(human,ownerPage,rpc,agent,peer) {
  const tabs=human.getByRole('navigation',{name:'频道详情'});
  const tab=name=>tabs.getByRole('link',{name,exact:true});
  const log=human.getByRole('log',{name:'群消息记录'});
  const members=human.locator('.panel.members .entry').filter({has:human.getByText('@'+agent.name,{exact:true})});
  const update='绑定 Agent 发出的公开更新';
  const mention='请 @'+agent.name+' 核对绑定验收';
  const title='Agent 自主发起的私聊', secret='仅在绑定后可见的样本 R92';
  const participants='@'+agent.name+' 与 @'+peer.name;
  const runtime=randomBytes(32).toString('hex');
  await rpc('private','enable',{credential:runtime},agent.key);
  await rpc('private','start',{session:'auto',target:peer.member,title,body:secret,request:'agent-dm'},runtime);
  const sent=await agent.p('post',{kind:'broadcast',body:update,request:'binding-public'});
  const mentioned=await peer.p('post',{kind:'broadcast',body:mention,mentions:[agent.member],request:'binding-mention'});
  await human.getByRole('link',{name:'只看与我有关',exact:true}).tap();
  await log.getByText('已确认测试样本的规则版本',{exact:true}).waitFor();
  assert.equal(await log.getByText(update,{exact:true}).count(),0);

  await tab('频道成员').tap();
  await members.getByRole('button',{name:'绑定给我',exact:true}).tap();
  const binding=human.getByRole('dialog',{name:'绑定 @'+agent.name});
  await binding.getByRole('button',{name:'生成绑定指令'}).tap();
  const instruction=await binding.getByLabel('绑定指令',{exact:true}).inputValue();
  assert.ok(instruction.includes('小雨'));
  assert.ok(instruction.includes('保留期内的私聊'));
  const code=new URLSearchParams(new URL(instruction.split(' ').at(-1)).hash.slice(1)).get('bind');
  assert.match(code,/^[a-zA-Z0-9_-]{43}$/);
  assert.equal((await agent.p('binding-preview',{code})).human.name,'小雨');
  await agent.p('bind-human',{code});
  await binding.getByRole('button',{name:'关闭',exact:true}).last().tap();
  await members.getByText('我的 Agent',{exact:true}).waitFor();
  assert.ok(await human.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  await tab('对话').tap();
  await log.locator('[data-message="'+sent.id+'"]').getByText('我的 Agent',{exact:true}).waitFor();
  await log.locator('[data-message="'+mentioned.id+'"]').getByText('提到我的 Agent',{exact:true}).waitFor();
  await tab('私聊').tap();
  const mine=human.getByRole('link',{name:new RegExp(title)});
  await mine.getByText(participants).waitFor();
  await mine.tap();
  const transcript=human.getByRole('log',{name:'私聊记录'});
  await transcript.getByText(secret,{exact:true}).waitFor();
  await human.getByText(participants+' · 只读',{exact:true}).waitFor();
  assert.equal(await human.getByRole('textbox',{name:'私聊消息'}).count(),0);
  const opened=new URL(human.url());
  const unrelated=await ownerPage.context().newPage();
  try {
    await unrelated.goto(new URL(opened.hash,ownerPage.url()).href);
    await unrelated.getByRole('heading',{name:'无法打开私聊'}).waitFor();
    assert.equal(await unrelated.getByText(secret,{exact:true}).count(),0);
  } finally { await unrelated.close(); }
  await tab('频道成员').tap();
  await members.getByRole('button',{name:'解除绑定',exact:true}).tap();
  await human.getByRole('dialog',{name:'解除 @'+agent.name+' 的绑定？'}).getByRole('button',{name:'解除绑定',exact:true}).tap();
  await members.getByRole('button',{name:'绑定给我',exact:true}).waitFor();
  await human.goto(opened.href);
  await human.getByRole('heading',{name:'无法打开私聊'}).waitFor();
  assert.equal(await human.getByText(secret,{exact:true}).count(),0);
  await tab('私聊').tap();
  await human.getByRole('link',{name:/样本版本核对/}).waitFor();
  assert.equal(await mine.count(),0);
  await tab('对话').tap();
  await log.getByText('已确认测试样本的规则版本',{exact:true}).waitFor();
  assert.equal(await log.getByText(update,{exact:true}).count(),0);
  await human.getByRole('link',{name:'全部消息',exact:true}).tap();
}

async function checkFeedRecovery(context,url) {
  const page=await context.newPage();
  const question='请群主和测试确认发布。';
  const row=page.getByRole('log').getByText(question,{exact:true});
  const mine=page.getByRole('link',{name:'只看与我有关',exact:true});
  let firstRead=true, searchOffline=true, sends=0;
  try {
    await page.route('**/api/owner/detail',async route=>{
      const {scope,query}=route.request().postDataJSON();
      if (scope==='mine' && !query && firstRead) {
        firstRead=false;
        return route.fulfill({status:502,body:''});
      }
      if (scope==='mine' && query===question && searchOffline)
        return route.fulfill({status:200,contentType:'application/json',body:'{"history":'});
      await route.continue();
    });
    await page.goto(url+'?scope=all'); await row.waitFor();
    await page.evaluate(()=>{
      window.feedErrors=[];
      new MutationObserver(()=>{
        const error=document.querySelector('.empty h2')?.textContent;
        if (error==='暂时无法读取对话') window.feedErrors.push(document.querySelector('.empty p').textContent);
      }).observe(document.querySelector('#workspace'),{childList:true,subtree:true});
    });
    await Promise.all([page.waitForResponse(r=>r.url().endsWith('/detail') && r.request().postDataJSON().scope==='mine'),mine.click()]);
    await row.waitFor();
    assert.equal(firstRead,false);
    assert.deepEqual(await page.evaluate(()=>window.feedErrors),[],'临时空响应应在个人筛选内自动恢复');
    const unrelated=page.getByRole('log').getByText('这个分支的回归已完成。',{exact:true});
    assert.equal(await unrelated.count(),0);

    await page.getByRole('button',{name:'搜索频道消息'}).click();
    await page.getByRole('searchbox').fill(question);
    await page.getByRole('button',{name:'搜索',exact:true}).click();
    await page.getByText('频道数据接收不完整，请重新加载。',{exact:true}).waitFor();
    assert.equal(await mine.getAttribute('aria-current'),'true');
    searchOffline=false;
    await page.getByRole('button',{name:'重新加载',exact:true}).click(); await row.waitFor();
    assert.equal(await page.getByRole('searchbox').inputValue(),question);

    await page.getByRole('button',{name:'清除搜索'}).click(); await row.waitFor();
    await page.route('**/api/owner/broadcast',route=>{ sends++; return route.fulfill({status:200,body:''}); });
    const input=page.getByRole('textbox',{name:'群消息',exact:true});
    await input.fill('一条仅同步的草稿');
    await page.getByRole('checkbox',{name:'仅同步',exact:true}).check();
    await input.press('Enter');
    await page.locator('#chat-error').filter({hasText:'频道数据接收不完整，请重新加载。'}).waitFor();
    assert.equal(sends,1,'写操作不能自动重发');
    assert.equal(await input.inputValue(),'一条仅同步的草稿');
  } finally { await page.close(); }
}

async function checkTabSwitches(context,url) {
  const probe=await context.newPage();
  let releaseWarm, releaseSearch;
  const warmGate=new Promise(resolve=>{releaseWarm=resolve;});
  const searchGate=new Promise(resolve=>{releaseSearch=resolve;});
  const searchText='等待中的搜索';
  const tab=id=>probe.locator('.tabs a[data-view="'+id+'"]');
  const searchRequest=r=>r.url().endsWith('/detail') && r.postDataJSON()?.query===searchText;
  try {
    await probe.route('**/api/owner/history',async route=>{
      await warmGate; await route.continue();
    });
    await probe.route('**/api/owner/detail',async route=>{
      if (searchRequest(route.request())) await searchGate;
      await route.continue();
    });
    await probe.goto(url.replace(/\/history$/,'/members'));
    await probe.locator('.panel.members').getByText('@订单前端').waitFor();
    const header=await probe.locator('.room-header').elementHandle(), strip=await probe.locator('.tab-strip').elementHandle();
    await tab('history').focus(); await probe.keyboard.press('Enter');
    assert.equal(await tab('history').getAttribute('aria-current'),'page');
    assert.ok(await tab('history').evaluate(a=>a===document.activeElement));
    assert.equal(await probe.locator('.panel.history').getAttribute('aria-busy'),'true');
    await probe.getByRole('button',{name:'搜索频道消息'}).click();
    const search=probe.getByRole('searchbox');
    await search.fill('刚输入的关键词');
    releaseWarm();
    await probe.locator('.panel.history .chat-row').first().waitFor();
    assert.equal(await search.inputValue(),'刚输入的关键词');
    assert.ok(await search.evaluate(el=>el===document.activeElement));
    await tab('topics').click();
    await probe.locator('.panel.topics .topic-row').first().waitFor();
    await tab('members').click();
    await probe.locator('.panel.members').getByText('@订单前端').waitFor();
    await tab('history').click();
    await probe.getByRole('button',{name:'搜索频道消息'}).click();
    await search.fill(searchText);
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
        const content={history:'.chat-row',members:'.entry',invites:'.link-control',topics:'.topic-row'};
        for (const view of ['members','invites','topics','members','history']) {
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
    await checkRecoveryDialog(page,origin);
    await page.getByRole('link',{name:'群主登录',exact:true}).click();
    await page.getByRole('heading',{name:'登录工作区'}).waitFor();
    assert.equal(await page.title(),'群主登录 · 叠声');
    await checkLogos(page.getByRole('group',{name:'支持的 Agent'}));
    assert.equal(await appTheme(page),'dark');
    await shot('网页端-登录-深色');
    await page.setViewportSize({width:375,height:740});
    await checkLogos(page.locator('.client-strip'));
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    await page.setViewportSize({width:1320,height:820});
    await page.getByText('在哪里找到密钥文件？').click(); await visible('cat ./build/local-workspace/owner.key');
    await page.getByText('在哪里找到密钥文件？').click();
    await click('进入工作区'); await visible('请填写群主密钥');
    await page.getByLabel('群主密钥',{exact:true}).fill('invalid');
    await click('进入工作区'); await visible('群主密钥无效，请从服务主机重新复制');
    await page.getByLabel('群主密钥',{exact:true}).fill(process.env.RELAY_TEST_OWNER);
    await click('进入工作区'); await visible('创建第一个频道'); await shot('网页端-新工作区');
    const newGroup=page.locator('#workspace').getByRole('button',{name:'新建群聊'});
    await newGroup.click();
    const createDialog=page.getByRole('dialog',{name:'新建群聊'});
    const groupName=createDialog.getByLabel('群名称',{exact:true});
    await groupName.fill('临时名称');
    await createDialog.getByRole('button',{name:'取消',exact:true}).click();
    await createDialog.waitFor({state:'hidden'});
    await newGroup.click();
    assert.equal(await groupName.inputValue(),'');
    await click('创建群聊'); await visible('请填写群名称');
    await page.getByLabel('群名称',{exact:true}).fill('订单联调');
    await page.getByLabel('讨论范围（可选）').fill('订单详情页、接口契约与发布前的回归验证');
    await click('创建群聊');
    await page.getByRole('heading',{name:'订单联调',exact:true}).waitFor();
    assert.equal(await page.locator('#service').innerText(),new URL(team).host);
    const group=await page.evaluate(()=>location.hash.split('/')[1]);
    await checkThemeChoice(page,shot);
    await checkUserGuide(page);
    await checkPendingInviteDismissal(page);
    await click('邀请 Agent');
    await click('复制链接');
    const link=await page.evaluate(()=>navigator.clipboard.readText());
    assert.ok(link.startsWith(team+'/join#c='));
    await invited.goto(link);
    await invited.getByRole('heading',{name:'# 订单联调'}).waitFor();
    assert.equal(await appTheme(invited),'dark');
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
    const {group:coordination}=await rpc('owner','create',{name:'发布协调',purpose:'跟进上线时间与回滚窗口'});
    await rpc('owner','create',{name:'设计评审',purpose:'交互方案与组件验收'});
    const members=[];
    for (const [agent,name,role] of [['codex','订单前端','订单详情与页面状态'],['claude','订单后端','接口契约与数据模型'],['deepseek','质量工程','回归测试与发布验收']]) {
      const key=randomBytes(32).toString('hex');
      const m=await rpc('channel','join',{code,agent,name,role},key);
      const p=(op,fields={})=>rpc('project',op,{session:agent,...fields},key);
      const status=await p('status');
      await p('check',{challenge:status.challenge});
      members.push({p,...m,key});
    }
    const [front,back,qa]=members;
    await front.p('wait',{timeout:0}); await back.p('wait',{timeout:0});
    const input=page.getByRole('textbox',{name:'群消息',exact:true});
    const list=page.getByRole('listbox',{name:'选择频道成员'});
    await checkDeliveryModes(page,front,back,qa);
    await checkMarkdownRendering(page,front,back);
    await input.fill('@');
    await list.getByRole('option',{name:/质量工程/}).waitFor();
    await input.press('ArrowDown');
    await input.press('Enter');
    assert.equal(await input.inputValue(),'@订单后端 ');
    assert.equal(await list.isVisible(),false);
    assert.equal(await page.locator('#composer select').count(),0);
    await input.fill('请检查接口');
    await input.evaluate(el=>el.setSelectionRange(1,1));
    await page.keyboard.insertText('＠质量');
    await list.getByRole('option',{name:/质量工程/}).waitFor();
    // A virtual keyboard can submit a line break without a corresponding keydown.
    await input.evaluate(el=>el.dispatchEvent(new InputEvent('beforeinput',{inputType:'insertLineBreak',bubbles:true,cancelable:true})));
    assert.equal(await input.inputValue(),'请@质量工程 检查接口');
    await input.fill('大家核对一下旧订单的展示逻辑，今天一起收口。');
    const imeKeys=await input.evaluate(el=>{
      const enter=init=>el.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true,cancelable:true,...init}));
      el.dispatchEvent(new CompositionEvent('compositionstart',{bubbles:true}));
      const composing=enter({});
      el.dispatchEvent(new CompositionEvent('compositionend',{bubbles:true}));
      return [composing,enter({isComposing:true}),enter({keyCode:229})];
    });
    assert.deepEqual(imeKeys,[true,true,true],'选词回车应交给输入法');
    await input.press('Enter');
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
    const host=(await rpc('owner','detail',{group})).self;
    await checkPersonalFeed(page,group,host,front,qa);
    await checkTopicDirectory(page,group,rpc,front,qa);
    await checkProjectInbox(context,origin,rpc);
    await checkFeedRecovery(context,origin+'/#/'+group+'/history');
    await checkTabSwitches(context,origin+'/#/'+group+'/history');
    await page.getByRole('complementary',{name:'频道成员'}).getByText('DeepSeek Harness').waitFor();
    await checkLogos(page.getByRole('complementary',{name:'频道成员'}));
    await page.getByRole('complementary',{name:'频道成员'}).getByText(/正在监听/).first().waitFor();
    await checkLogos(page.getByRole('log'));
    await page.getByRole('log').evaluate(log=>{log.scrollTop=0;});
    await shot('网页端-频道');
    await selectTheme(page,'dark');
    await shot('网页端-深色');
    await selectTheme(page,'light');
    await page.getByLabel('查找频道').fill('设计');
    assert.equal(await page.locator('#group-list a').count(),1);
    await page.getByLabel('查找频道').fill('');
    await page.getByLabel('群消息',{exact:true}).fill('待收口的草稿');
    const quiet=page.getByRole('checkbox',{name:'仅同步'});
    await quiet.check();
    await page.getByRole('link',{name:'设计评审',exact:true}).click();
    await page.getByRole('heading',{name:'设计评审',exact:true}).waitFor();
    await quiet.waitFor();
    assert.equal(await quiet.isChecked(),false);
    await page.getByRole('link',{name:'订单联调',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('#chat-body').value==='待收口的草稿');
    assert.equal(await quiet.isChecked(),true);
    await quiet.uncheck();
    await page.goto(origin+'/#/'+group+'/conclusions?q=order_detail');
    await visible('release 分支会返回 null。前端可以据此隐藏确认时间，字段名保持不变。');
    assert.ok(new URL(page.url()).hash.startsWith('#/'+group+'/history?'));
    assert.equal(await page.getByRole('searchbox').inputValue(),'order_detail');
    assert.equal(await page.getByRole('link',{name:'已采纳结论',exact:true}).count(),0);
    assert.equal(await page.locator('[data-message="'+answer.id+'"]').getByText('已采纳',{exact:true}).count(),0);
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
    await visible('频道链接已停用');
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
    const colleague=await browser.newContext({viewport:{width:375,height:740},locale:'zh-CN',hasTouch:true});
    const human=await colleague.newPage();
    const colleagueLogin={username:'xiaoyu.yan',password:'Test!842'};
    const fillCredentials=async(form, fields=colleagueLogin)=>{
      await form.getByLabel('账号',{exact:true}).fill(fields.username);
      await form.getByLabel('密码',{exact:true}).fill(fields.password);
    };
    human.on('pageerror',error=>errors.push(error.message));
    await human.goto(humanLink);
    await human.getByLabel('你的昵称').waitFor();
    await human.getByText('至少 8 个字符。',{exact:true}).waitFor();
    await shot('网页端-同事邀请',human);
    assert.equal(await human.getByLabel('群主密钥',{exact:true}).isVisible(),false);
    await human.getByLabel('你的昵称').fill('小雨');
    await fillCredentials(human.getByRole('form',{name:'同事邀请'}));
    await human.getByRole('button',{name:'创建账号并加入'}).click();
    await human.getByRole('log',{name:'群消息记录'}).waitFor();
    await checkAnnouncementsAndAdmins(page,human,front);
    await human.goto(team+'/room/'+group+'#/'+group+'/conclusions');
    await human.getByRole('log').getByText('大家核对一下旧订单的展示逻辑，今天一起收口。',{exact:true}).waitFor();
    assert.ok(new URL(human.url()).hash.startsWith('#/'+group+'/history?'));
    assert.equal(await human.getByRole('link',{name:'已采纳结论',exact:true}).count(),0);
    await human.getByRole('navigation',{name:'频道详情'}).getByRole('link',{name:'话题',exact:true}).tap();
    await human.getByRole('combobox',{name:'分类',exact:true}).selectOption('collaboration');
    await human.getByRole('link',{name:'v13 可以交付了吗？'}).tap();
    await human.getByRole('log').getByText('可以交付',{exact:true}).waitFor();
    await human.getByRole('link',{name:'返回话题列表'}).tap();
    await human.getByRole('link',{name:'v13 可以交付了吗？'}).waitFor();
    if (process.env.RELAY_TEST_SHOTS)
      await human.screenshot({path:path.join(process.env.RELAY_TEST_SHOTS,'网页端-手机话题目录.png')});
    assert.ok(await human.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    await human.getByRole('link',{name:'对话',exact:true}).tap();
    const humanMine=human.getByRole('link',{name:'只看与我有关',exact:true});
    await humanMine.tap();
    await human.getByRole('heading',{name:'还没有与你有关的消息'}).waitFor();
    await human.getByRole('link',{name:'查看全部消息'}).tap();
    await human.getByRole('log').getByText('大家核对一下旧订单的展示逻辑，今天一起收口。',{exact:true}).waitFor();
    await checkPrivateChat(human,page,rpc,front);
    await checkAgentBinding(human,page,rpc,qa,front);
    assert.equal(new URL(human.url()).hash.includes('invite='),false);
    assert.equal(await human.getByRole('button',{name:'邀请同事'}).count(),0);
    assert.equal(await human.getByRole('button',{name:'新建群聊'}).count(),0);
    assert.equal(await human.getByRole('link',{name:'邀请',exact:true}).count(),0);
    assert.equal(await human.locator('#group-list a').count(),1);
    const humanInput=human.getByRole('textbox',{name:'群消息',exact:true});
    const humanList=human.getByRole('listbox',{name:'选择频道成员'});
    await humanInput.fill('dev@example.com');
    assert.equal(await humanList.isVisible(),false);
    await humanInput.fill('请@');
    await humanList.waitFor();
    assert.equal(await humanList.getByRole('option',{name:/小雨|订单后端/}).count(),0);
    await humanInput.press('Escape');
    assert.equal(await humanList.isVisible(),false);
    await human.keyboard.insertText('前');
    await humanList.getByRole('option',{name:/订单前端/}).tap();
    assert.equal(await humanInput.inputValue(),'请@订单前端 ');
    await human.keyboard.insertText('确认一下');
    await humanInput.press('Shift+Enter');
    await human.keyboard.insertText('明天几点发布？');
    const message='请@订单前端 确认一下\n明天几点发布？';
    assert.equal(await humanInput.inputValue(),message);
    await humanInput.press('Enter');
    const bubble=human.getByRole('log').locator('article').filter({hasText:'明天几点发布？'});
    await bubble.getByText('我',{exact:true}).waitFor();
    assert.equal((await bubble.locator('.chat-bubble').innerText()).trim(),message);
    const mid=Number(await bubble.getAttribute('data-message'));
    const hr=await front.p('receive',{message:mid});
    assert.equal(hr.body,message);
    const before=await front.p('history',{topic:mid});
    assert.deepEqual(before.messages.map(m=>m.kind),['question']);
    await front.p('post',{kind:'answer',parent:mid,ticket:hr.ticket,body:'明早十点开始上线。',request:'reply-colleague'});
    await human.getByRole('log').getByText('明早十点开始上线。',{exact:true}).waitFor();
    const {self}=await rpcFromMember('groups');
    const backQuestion=await front.p('post',{kind:'question',target:self.id,body:'请人工确认这个窗口。',request:'ask-colleague'});
    await human.locator('[data-message="'+backQuestion.id+'"]').getByRole('button',{name:'答复问题'}).click();
    await humanInput.fill('@');
    assert.equal(await humanList.isVisible(),false);
    await humanInput.fill('窗口已确认。');
    await human.getByRole('button',{name:'发送',exact:true}).click();
    await human.getByRole('log').getByText('窗口已确认。',{exact:true}).waitFor();
    const mixed=await front.p('post',{kind:'group_question',targets:[self.id,qa.member],body:'请小雨和质量工程分别验收。',request:'mixed-question'});
    const mixedRow=human.locator('[data-message="'+mixed.id+'"]');
    await mixedRow.getByRole('button',{name:'答复问题'}).click();
    assert.equal(await human.getByRole('checkbox',{name:'仅同步'}).count(),0);
    await humanInput.fill('同事已完成验收。');
    await humanInput.press('Enter');
    await mixedRow.getByText('@小雨 · 已回复',{exact:true}).waitFor();
    await mixedRow.getByText('@质量工程 · 待签收',{exact:true}).waitFor();
    const mixedHistory=await front.p('history',{topic:mixed.id});
    assert.deepEqual(mixedHistory.messages.map(m=>m.kind),['group_question','answer']);
    assert.equal(mixedHistory.messages[1].author,'小雨');
    const update='@质量工程 验收结果已归档。';
    await humanInput.fill(update);
    await human.getByRole('checkbox',{name:'仅同步',exact:true}).check();
    await humanInput.press('Enter');
    await human.getByRole('log').locator('article').filter({has:human.getByText(update,{exact:true})}).getByText('仅同步',{exact:true}).waitFor();
    assert.deepEqual((await qa.p('inbox',{query:update})).messages,[]);
    const guestQuestion='@群主@订单前端 请分别确认上线计划。';
    await humanInput.fill(guestQuestion);
    await humanInput.press('Enter');
    const guestRow=human.getByRole('log').locator('article').filter({has:human.getByText(guestQuestion,{exact:true})});
    await guestRow.getByText('0/2 已回复',{exact:true}).waitFor();
    const guestTopic=Number(await guestRow.getAttribute('data-message'));
    const [guestRoot]=(await front.p('history',{topic:guestTopic})).messages;
    assert.deepEqual(guestRoot.recipients.map(r=>r.name),['群主','订单前端']);
    assert.deepEqual((await qa.p('inbox',{query:guestQuestion})).messages,[]);
    await humanMine.tap(); await guestRow.waitFor();
    assert.equal(await human.getByRole('log').getByText('大家核对一下旧订单的展示逻辑，今天一起收口。',{exact:true}).count(),0);
    assert.equal(await mixedRow.count(),1);
    const cc='@小雨 本次记录供查阅。';
    await front.p('post',{kind:'broadcast',mentions:[self.id],body:cc,request:'personal-cc'});
    const ccRow=human.getByRole('log').locator('article').filter({has:human.getByText(cc,{exact:true})});
    await ccRow.getByText('提到我',{exact:true}).waitFor();
    await human.setViewportSize({width:1280,height:800}); await shot('网页端-同事频道',human);
    await human.setViewportSize({width:375,height:740});
    await human.reload();
    await human.getByRole('log').getByText('明早十点开始上线。',{exact:true}).waitFor();
    await human.goto(team+'/');
    await human.getByRole('heading',{name:'我的频道'}).waitFor();
    assert.equal(await human.getByLabel('群主密钥',{exact:true}).isVisible(),false);
    await human.getByRole('navigation',{name:'我加入的频道'}).getByRole('link',{name:'# 订单联调'}).click();
    await human.getByRole('log').getByText('明早十点开始上线。',{exact:true}).waitFor();
    assert.equal(await humanMine.getAttribute('aria-current'),'true');
    assert.ok(await human.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth));
    await page.keyboard.press('Escape');
    const secondInvite=await rpc('owner','human_invite',{group:coordination});
    const secondBrowser=await browser.newContext({viewport:{width:375,height:740},locale:'zh-CN',hasTouch:true});
    const secondTab=await secondBrowser.newPage();
    secondTab.on('pageerror',error=>errors.push(error.message));
    await secondTab.goto(new URL(secondInvite.url,team).href);
    await secondTab.getByRole('button',{name:'已有账号？登录加入'}).tap();
    await fillCredentials(secondTab.getByRole('form',{name:'同事邀请'}));
    await secondTab.getByLabel('你的昵称').fill('小雨');
    await secondTab.getByRole('button',{name:'登录并加入'}).tap();
    await secondTab.getByRole('heading',{name:'发布协调',exact:true}).waitFor();
    await human.bringToFront();
    await humanInput.fill('先保留这段频道草稿');
    const openSidebar=()=>human.locator('#nav-toggle').click();
    const channels=human.getByRole('navigation',{name:'频道列表'});
    await openSidebar();
    await channels.getByRole('link',{name:'发布协调'}).tap();
    await human.getByRole('heading',{name:'发布协调',exact:true}).waitFor();
    assert.equal(new URL(human.url()).pathname,'/room/'+coordination);
    assert.equal(await humanInput.inputValue(),'');
    await human.goBack();
    await human.getByRole('heading',{name:'订单联调',exact:true}).waitFor();
    await humanInput.waitFor();
    assert.equal(await humanInput.inputValue(),'先保留这段频道草稿');
    await humanInput.fill('');
    await human.getByLabel('频道操作',{exact:true}).tap();
    await human.getByRole('button',{name:'退出频道',exact:true}).tap();
    await human.getByRole('dialog',{name:'退出这个频道？'}).getByRole('button',{name:'取消'}).tap();
    await openSidebar();
    await human.getByRole('button',{name:'退出登录',exact:true}).tap();
    await human.getByRole('dialog',{name:'退出登录？'}).getByRole('button',{name:'退出登录',exact:true}).tap();
    await human.getByRole('heading',{name:'同事账号登录'}).waitFor();
    for (const gid of [group,coordination])
      assert.equal((await colleague.request.post(team+'/api/member/'+gid+'/groups',{data:{}})).status(),401);
    assert.equal((await secondBrowser.request.post(team+'/api/member/'+coordination+'/groups',{data:{}})).status(),200);
    assert.ok((await rpc('owner','detail',{group})).members.some(m=>m.id===self.id));
    assert.ok((await rpc('owner','detail',{group:coordination})).members.some(m=>m.name==='小雨'));
    await secondBrowser.close();

    await colleague.clearCookies();
    await human.evaluate(()=>localStorage.clear());
    await human.goto(team+'/#channels');
    const accountForm=human.getByRole('form',{name:'同事账号登录'});
    await fillCredentials(accountForm,{...colleagueLogin,password:'wrong-pass'});
    await accountForm.getByRole('button',{name:'登录',exact:true}).tap();
    await accountForm.getByText('账号或密码不正确',{exact:true}).waitFor();
    await fillCredentials(accountForm);
    await accountForm.getByRole('button',{name:'登录',exact:true}).tap();
    const savedRooms=human.getByRole('navigation',{name:'我加入的频道'});
    await savedRooms.getByRole('link',{name:'# 发布协调',exact:true}).waitFor();
    assert.equal(await savedRooms.getByRole('link').count(),2);
    await human.getByRole('button',{name:'账号设置',exact:true}).tap();
    const homeSettings=human.getByRole('dialog',{name:'账号设置'});
    assert.equal(await homeSettings.getByLabel('登录账号名',{exact:true}).inputValue(),colleagueLogin.username);
    await homeSettings.getByRole('button',{name:'取消',exact:true}).tap();
    await homeSettings.waitFor({state:'hidden'});
    if (process.env.RELAY_TEST_SHOTS) await human.screenshot({path:path.join(process.env.RELAY_TEST_SHOTS,'网页端-同事账号频道.png')});
    await savedRooms.getByRole('link',{name:'# 订单联调',exact:true}).tap();
    await human.getByRole('log').getByText(cc,{exact:true}).waitFor();
    assert.deepEqual((await rpcFromMember('groups')).self,self);
    Object.assign(colleagueLogin,await checkAccountSettings(human,colleagueLogin));
    await human.goto(team+'/room/'+coordination);
    await human.getByRole('heading',{name:'发布协调',exact:true}).waitFor();

    assert.equal(await page.locator('article').filter({hasText:'@小雨'}).getByRole('button',{name:'恢复身份'}).count(),0);
    const newBrowser=await browser.newContext({viewport:{width:375,height:740},locale:'zh-CN',hasTouch:true});
    const recovered=await newBrowser.newPage();
    recovered.on('pageerror',error=>errors.push(error.message));
    await recovered.goto(team+'/room/'+group);
    await recovered.getByRole('heading',{name:'同事账号登录'}).waitFor();
    assert.equal(await recovered.getByRole('log').count(),0);
    const freshForm=recovered.getByRole('form',{name:'同事账号登录'});
    await fillCredentials(freshForm);
    await freshForm.getByRole('button',{name:'登录',exact:true}).tap();
    await recovered.getByRole('log').getByText(cc,{exact:true}).waitFor();
    await recovered.getByRole('navigation',{name:'频道详情'}).getByRole('link',{name:'私聊',exact:true}).tap();
    await recovered.getByRole('link',{name:/样本版本核对/}).tap();
    await recovered.getByRole('log',{name:'私聊记录'}).getByText('QA-X71 的内部答复',{exact:true}).waitFor();
    await page.locator('article').filter({hasText:'@小雨'}).getByRole('button',{name:'移除'}).click();
    await page.getByRole('dialog').getByRole('button',{name:'移除成员'}).click();
    await recovered.getByText('当前账号尚未加入这个频道',{exact:true}).waitFor();
    assert.equal(await recovered.getByRole('log').count(),0);
    assert.equal(await recovered.getByLabel('群主密钥',{exact:true}).isVisible(),false);
    await human.goto(team+'/#channels');
    await savedRooms.getByRole('link',{name:'# 发布协调',exact:true}).waitFor();
    assert.equal(await savedRooms.getByRole('link',{name:'# 订单联调',exact:true}).count(),0);
    await newBrowser.close();
    await checkPersonalPaging(page,(body,request)=>rpc('owner','broadcast',{group,body,request}),front);
    const ownerChannels=page.getByRole('navigation',{name:'频道列表'});
    await page.locator('#nav-toggle').click();
    await ownerChannels.getByRole('link',{name:'发布协调'}).click();
    await page.getByRole('heading',{name:'发布协调',exact:true}).waitFor();
    await page.getByLabel('频道操作',{exact:true}).click();
    await click('解散频道');
    const dissolve=page.getByRole('dialog',{name:'解散 #发布协调？'});
    await dissolve.getByRole('button',{name:'解散频道',exact:true}).click();
    await page.getByText('频道已解散',{exact:true}).waitFor();
    await page.getByRole('heading',{name:'订单联调',exact:true}).waitFor();
    await human.goto(team+'/room/'+coordination);
    await human.getByRole('heading',{name:'频道已解散',exact:true}).waitFor();
    await colleague.close();
    async function rpcFromMember(op) {
      const response=await colleague.request.post(team+'/api/member/'+group+'/'+op,{data:{}});
      assert.equal(response.status(),200); return response.json();
    }
    await page.locator('#nav-toggle').click();
    assert.equal(await ownerChannels.getByRole('link',{name:'发布协调'}).count(),0);
    await click('退出登录');
    await page.getByRole('heading',{name:'登录工作区'}).waitFor();
    assert.deepEqual(errors,[]);
    console.log('PASS · 主题切换、使用手册、弹窗关闭、同事注册、账号设置、跨浏览器账号登录、多频道、频道生命周期、群公告、管理员、Markdown、Agent 绑定、私聊、话题、个人筛选、项目收件箱');
  } finally { await browser.close(); }
})().catch(error=>{console.error(error); process.exitCode=1;});
