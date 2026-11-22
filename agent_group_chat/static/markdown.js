'use strict';

// All message surfaces share this renderer; message HTML never becomes app UI.
const markdownBody = (() => {
  const escape = value => String(value).replace(/[&<>"']/g, char =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const parser = globalThis.marked && new marked.Marked({
    gfm:true, breaks:true,
    renderer:{
      br:() => '<br>\n',
      html:({text}) => escape(text),
      // Keep images as links so reading a message does not contact remote hosts.
      image:({href,title,text}) => '<a href="'+escape(href)+'"'+
        (title?' title="'+escape(title)+'"':'')+'>'+escape(text || '图片')+'</a>'
    }
  });
  const options = {
    ALLOWED_TAGS:['p','br','strong','em','del','h1','h2','h3','h4','h5','h6',
      'ul','ol','li','blockquote','pre','code','hr','a','table','thead','tbody','tr','th','td','input'],
    ALLOWED_ATTR:['href','title','align','start','type','checked','disabled'],
    ALLOW_DATA_ATTR:false, ALLOW_ARIA_ATTR:false, RETURN_DOM_FRAGMENT:true
  };
  return body => {
    const content=document.createElement('div'), source=String(body ?? '');
    content.className='chat-bubble markdown';
    try {
      if (!parser || !globalThis.DOMPurify?.isSupported) throw new Error('Markdown unavailable');
      const fragment=DOMPurify.sanitize(parser.parse(source),options);
      for (const link of fragment.querySelectorAll('a')) {
        let target;
        try { target=new URL(link.getAttribute('href'),location.href); } catch {}
        if (!link.hasAttribute('href') || !target || !['http:','https:','mailto:'].includes(target.protocol)) {
          link.replaceWith(...link.childNodes); continue;
        }
        link.href=target.href;
        link.rel='noopener noreferrer';
        if (target.protocol!=='mailto:') link.target='_blank';
      }
      for (const input of fragment.querySelectorAll('input')) {
        input.type='checkbox'; input.disabled=true;
      }
      for (const pre of fragment.querySelectorAll('pre')) {
        pre.tabIndex=0; pre.setAttribute('role','region'); pre.setAttribute('aria-label','代码块');
      }
      for (const table of fragment.querySelectorAll('table')) {
        const wrapper=document.createElement('div');
        wrapper.className='markdown-table'; wrapper.tabIndex=0;
        wrapper.setAttribute('role','region'); wrapper.setAttribute('aria-label','表格');
        table.replaceWith(wrapper); wrapper.append(table);
      }
      content.append(fragment);
    } catch {
      // A failed or unavailable renderer must not hide the original message.
      content.classList.add('markdown-fallback'); content.textContent=source;
    }
    return content;
  };
})();
