'use strict';

async function copyField(field) {
  if (navigator.clipboard) {
    try { await navigator.clipboard.writeText(field.value); return true; }
    catch { /* Try copying the selection when clipboard access is unavailable. */ }
  }
  if (!field.isConnected) return false;
  const masked = field.type === 'password';
  if (masked) field.type = 'text';
  try {
    field.focus();
    field.select();
    return document.execCommand('copy');
  }
  catch { return false; }
  finally { if (masked) field.type = 'password'; }
}
