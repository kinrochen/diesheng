'use strict';

async function copyField(field) {
  if (navigator.clipboard) {
    try { await navigator.clipboard.writeText(field.value); return true; }
    catch { /* Try copying the selection when clipboard access is unavailable. */ }
  }
  field.focus();
  field.select();
  try { return document.execCommand('copy'); }
  catch { return false; }
}
