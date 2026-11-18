// Native Chromium/CDP pipe: isolated profile, no public debug port, no private profile.
import {spawn} from 'node:child_process';
import {mkdir, mkdtemp, readFile, writeFile, rm} from 'node:fs/promises';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {fileURLToPath} from 'node:url';

export async function collect({url, output, executable, actions = [], wait_selector,
  dom_fields = {}, expected_title}) {
  const parsed = new URL(url);
  if (!['http:', 'https:'].includes(parsed.protocol) || parsed.username || parsed.password)
    throw new Error('source_not_authorized');
  await mkdir(output, {recursive: true});
  const profile = await mkdtemp(path.join(output, 'profile-'));
  const child = spawn(executable, ['--headless=new', '--disable-gpu', '--no-first-run',
    '--no-default-browser-check', '--remote-debugging-pipe', `--user-data-dir=${profile}`, 'about:blank'],
    {stdio: ['ignore', 'ignore', 'pipe', 'pipe', 'pipe'], windowsHide: true});
  let chromiumDiagnostic = '';
  child.stderr.on('data', chunk => { chromiumDiagnostic = (chromiumDiagnostic + chunk.toString()).slice(-8000); });
  const pending = new Map();
  let sequence = 0, buffer = '';
  child.stdio[4].on('data', chunk => {
    buffer += chunk.toString();
    let end;
    while ((end = buffer.indexOf('\0')) >= 0) {
      const raw = buffer.slice(0, end); buffer = buffer.slice(end + 1);
      let message;
      try { message = JSON.parse(raw); } catch { continue; }
      if (message.method === 'Fetch.requestPaused') {
        let allowed = false;
        try { allowed = new URL(message.params.request.url).origin === parsed.origin; } catch { /* deny */ }
        const method = allowed ? 'Fetch.continueRequest' : 'Fetch.failRequest';
        const params = {requestId: message.params.requestId, ...(!allowed ? {errorReason: 'BlockedByClient'} : {})};
        call(method, params, message.sessionId).catch(() => {});
      }
      const entry = pending.get(message.id);
      if (entry) {
        pending.delete(message.id); clearTimeout(entry.timer);
        message.error ? entry.reject(new Error('cdp_error')) : entry.resolve(message.result);
      }
    }
  });
  const rejectAll = () => {
    for (const entry of pending.values()) { clearTimeout(entry.timer); entry.reject(new Error('browser_closed')); }
    pending.clear();
  };
  const watchdog = setTimeout(() => { rejectAll(); child.kill(); }, 60000);
  child.on('error', rejectAll); child.on('exit', rejectAll);
  child.stdio[3].on('error', rejectAll);
  function call(method, params = {}, sessionId) {
    return new Promise((resolve, reject) => {
      const id = ++sequence;
      const timer = setTimeout(() => { pending.delete(id); reject(new Error('cdp_timeout')); }, 15000);
      pending.set(id, {resolve, reject, timer});
      child.stdio[3].write(JSON.stringify({id, method, params, ...(sessionId ? {sessionId} : {})}) + '\0');
    });
  }
  try {
    const {targetId} = await call('Target.createTarget', {url: 'about:blank'});
    const {sessionId} = await call('Target.attachToTarget', {targetId, flatten: true});
    const evaluate = async expression => {
      const value = await call('Runtime.evaluate', {expression, returnByValue: true, awaitPromise: true}, sessionId);
      if (value.exceptionDetails) throw new Error('dom_evaluation_failed');
      return value.result.value;
    };
    const waitFor = async expression => {
      for (let i = 0; i < 80; i++) {
        try { if (await evaluate(expression)) return; } catch { /* navigation replaced context */ }
        await new Promise(resolve => setTimeout(resolve, 100));
      }
      throw new Error('page_condition_timeout');
    };
    await call('Page.enable', {}, sessionId);
    await call('Network.enable', {}, sessionId);
    await call('Fetch.enable', {patterns: [{urlPattern: '*', requestStage: 'Request'}]}, sessionId);
    // Block common authentication subdomains; navigation/interactions also check origin.
    await call('Network.setBlockedURLs', {urls: ['*://accounts.*/*', '*://login.*/*']}, sessionId);
    const navigation = await call('Page.navigate', {url}, sessionId);
    if (navigation.errorText) throw new Error('navigation_failed');
    await waitFor("location.href !== 'about:blank' && document.readyState === 'complete'");
    if (wait_selector) await waitFor(`document.querySelector(${JSON.stringify(wait_selector)}) !== null`);
    if (new URL(await evaluate('location.href')).origin !== parsed.origin) throw new Error('redirect_not_authorized');
    const initial = await evaluate('({title:document.title,content:document.body.innerText,url:location.href})');
    if (/captcha|access denied|verify you are human|paywall/i.test(initial.title + '\n' + initial.content))
      throw new Error('access_control_encountered');
    if (actions.length > 10) throw new Error('too_many_browser_actions');
    let actionCount = 0;
    for (const action of actions) {
      if (!['fill', 'click', 'read'].includes(action.type)) throw new Error('unsupported_action');
      const selector = JSON.stringify(action.selector);
      const exists = await evaluate(`document.querySelector(${selector}) !== null`);
      if (!exists) throw new Error('element_missing');
      if (await evaluate(`document.querySelector(${selector}).type === 'password' || !!document.querySelector(${selector}).form?.querySelector('input[type=password]')`))
        throw new Error('login_interaction_forbidden');
      if (action.type === 'fill')
        await evaluate(`(() => { const e=document.querySelector(${selector}); e.value=${JSON.stringify(action.value)}; e.dispatchEvent(new Event('input',{bubbles:true})); })()`);
      if (action.type === 'click') {
        const before = await evaluate('location.href');
        const href = await evaluate(`document.querySelector(${selector}).href || document.querySelector(${selector}).form?.action || location.href`);
        if (new URL(href).origin !== parsed.origin) throw new Error('interaction_origin_not_authorized');
        await evaluate(`document.querySelector(${selector}).click()`);
        if (href !== before) await waitFor(`location.href !== ${JSON.stringify(before)}`);
        await waitFor("document.readyState === 'complete'");
        if (action.wait_selector) await waitFor(`document.querySelector(${JSON.stringify(action.wait_selector)}) !== null`);
      }
      actionCount += 1;
    }
    const page = await evaluate('({title:document.title,content:document.body.innerText,url:location.href})');
    if (new URL(page.url).origin !== parsed.origin) throw new Error('redirect_not_authorized');
    if (/captcha|access denied|verify you are human|paywall/i.test(page.title + '\n' + page.content))
      throw new Error('access_control_encountered');
    if (expected_title && page.title !== expected_title) throw new Error('unexpected_page_title');
    const fields = {};
    for (const [name, selector] of Object.entries(dom_fields)) {
      fields[name] = await evaluate(`document.querySelector(${JSON.stringify(selector)})?.textContent?.trim() ?? null`);
      if (fields[name] === null) throw new Error('dom_field_missing');
    }
    const {data} = await call('Page.captureScreenshot', {format: 'png'}, sessionId);
    await writeFile(path.join(output, 'page.png'), Buffer.from(data, 'base64'));
    const result = {source_url: url, source_name: parsed.hostname, collected_at: new Date().toISOString(),
      title: page.title, content: page.content, extracted_fields: fields,
      content_hash: createHash('sha256').update(page.content).digest('hex'), status: 'ok', error_code: null,
      metadata: {final_url: page.url, screenshot: 'page.png', transport: 'cdp-pipe',
        initial_title: initial.title, actions_completed: actionCount, resource_policy: 'same-origin-only'}};
    await writeFile(path.join(output, 'record.json'), JSON.stringify(result, null, 2));
    return result;
  } catch (error) {
    await writeFile(path.join(output, 'browser-error.json'), JSON.stringify({
      error_code: error.message, chromium_diagnostic: chromiumDiagnostic,
      browser_exit_code: child.exitCode}, null, 2));
    throw error;
  } finally {
    clearTimeout(watchdog);
    try { await call('Browser.close'); } catch { /* closed pipe */ }
    if (child.exitCode === null) child.kill();
    await Promise.race([new Promise(resolve => child.once('exit', resolve)), new Promise(resolve => setTimeout(resolve, 3000))]);
    rejectAll();
    await rm(profile, {recursive: true, force: true, maxRetries: 5, retryDelay: 200});
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const config = JSON.parse(await readFile(process.argv[2], 'utf8'));
  if (config.automation_allowed !== true || !config.permission_basis) throw new Error('source_not_authorized');
  await collect(config);
  console.log(JSON.stringify({status: 'ok'}));
}
