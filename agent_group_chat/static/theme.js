'use strict';

(() => {
  const storageKey='diesheng.theme';
  const root=document.documentElement;
  const colorScheme=document.querySelector('meta[name="color-scheme"]');
  let button;

  function applyTheme(value) {
    const theme=value==='light'?'light':'dark';
    root.dataset.theme=theme;
    colorScheme.content=theme;
    if (button) {
      const label=theme==='dark'?'切换到浅色模式':'切换到深色模式';
      button.setAttribute('aria-label',label);
      button.title=label;
    }
  }

  let saved;
  try { saved=localStorage.getItem(storageKey); } catch {}
  applyTheme(saved);

  document.addEventListener('DOMContentLoaded',()=>{
    button=document.getElementById('theme-toggle');
    if (!button) return;
    applyTheme(root.dataset.theme);
    button.addEventListener('click',()=>{
      applyTheme(root.dataset.theme==='dark'?'light':'dark');
      try { localStorage.setItem(storageKey,root.dataset.theme); } catch {}
    });
    button.disabled=false;
  },{once:true});

  window.addEventListener('storage',event=>{
    if (event.key===storageKey || event.key===null) applyTheme(event.newValue);
  });
})();
