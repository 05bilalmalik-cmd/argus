'use strict';
(() => {
  const $ = id => document.getElementById(id);
  async function api(path, options={}) {
    const response = await fetch(path, {cache:'no-store', ...options,
      headers:{'Content-Type':'application/json','X-Argus-Tracker':'1'}});
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Invalid mail setup request');
    return data;
  }
  async function status() {
    try {
      const data = await api('/api/email/configuration');
      if (data.smtp_host) $('smtp-host').value = data.smtp_host;
      if (data.smtp_user) $('smtp-user').value = data.smtp_user;
      if (data.from_address) $('mail-from').value = data.from_address;
      if (data.to_address) $('mail-to').value = data.to_address;
      $('email-result').textContent = data.configured ? 'Mail settings saved. Send the connection test to verify actual provider handoff.' : 'Email is not configured. No alert has been sent.';
      $('test-mail').disabled = !data.configured;
    } catch (error) { $('email-result').textContent = error.message; }
  }
  $('email-form').addEventListener('submit', async event => {
    event.preventDefault(); $('save-mail').disabled = true;
    $('email-result').textContent = 'Checking verified TLS and account login…';
    const payload = {smtp_host:$('smtp-host').value, smtp_port:587,
      smtp_user:$('smtp-user').value, smtp_password:$('smtp-password').value,
      from_address:$('mail-from').value, to_address:$('mail-to').value};
    try {
      await api('/api/email/configuration', {method:'PUT',body:JSON.stringify(payload)});
      $('smtp-password').value = '';
      await status();
    } catch (error) { $('email-result').textContent = error.message; }
    finally { payload.smtp_password = ''; $('save-mail').disabled = false; }
  });
  $('test-mail').addEventListener('click', async () => {
    $('test-mail').disabled = true;
    try {
      const result = await api('/api/email/test', {method:'POST'});
      $('test-receipt').textContent = `Status: ${result.status}\nMessage-ID: ${result.message_id || 'Not assigned'}\n${result.note || ''}`;
    } catch (error) { $('email-result').textContent = error.message; }
    finally { $('test-mail').disabled = false; }
  });
  status();
})();
