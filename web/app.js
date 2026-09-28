/* Local UI. Every action reports errors and every application has a durable history. */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const safeUrl = s => { try { const u = new URL(s, location.origin); return ['http:', 'https:'].includes(u.protocol) ? u.href : '#'; } catch { return '#'; } };
const api = async (path, opts = {}) => {
  const r = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...opts });
  const data = await r.json().catch(() => ({}));
  if (!r.ok || data.error) throw new Error(typeof data.detail === 'string' ? data.detail : data.error || `Request failed (${r.status})`);
  return data;
};
const toast = m => { const t = $('#toast'); t.textContent = m; t.classList.remove('hidden'); clearTimeout(t._t); t._t = setTimeout(() => t.classList.add('hidden'), 6500); };
const perform = fn => async (...args) => { try { return await fn(...args); } catch (e) { toast(e.message); } };
const fmtTs = s => s ? new Date(s).toLocaleString([], { month:'short',day:'numeric',hour:'2-digit',minute:'2-digit' }) : 'Not yet';
const rel = s => { if (!s) return 'Not scheduled'; const d = (new Date(s) - Date.now()) / 60000; return d > 0 ? `in ${Math.ceil(d)}m` : `${Math.round(-d)}m ago`; };
const money = n => n ? '$' + Math.round(n / 1000) + 'k' : '';
const label = s => s.replace(/_/g, ' ');
const prettyStatus = s => ({needs_human:'Needs your input',dry_run:'Practice run',applied:'Submitted',new:'New',failed:'Failed',applying:'In progress',queued:'Queued',skipped:'Skipped'}[s] || label(s));
let STATE, SETTINGS, PROFILE, activeView = 'overview', refreshBusy = false;
const modal = html => { $('#modal-body').innerHTML = html; $('#modal').classList.remove('hidden'); $('#modal-x').focus(); };
function closeModal() { $('#modal').classList.add('hidden'); }
$$('nav button').forEach(b => b.onclick = () => showView(b.dataset.view));
function showView(v) {
  activeView = v;
  $$('nav button').forEach(b => b.classList.toggle('active', b.dataset.view === v));
  $$('.view').forEach(s => s.classList.toggle('active', s.id === 'view-' + v));
  perform(({jobs:loadJobs,applications:loadApps,profile:loadProfile,settings:loadSettings})[v] || refreshState)();
}
function jobCard(j) {
  const cls = j.score >= 70 ? 'hi' : j.score >= 50 ? 'mid' : 'lo';
  const salary = j.salary_min && j.salary_max ? `${money(j.salary_min)}–${money(j.salary_max)}` : money(j.salary_min || j.salary_max);
  const supported = ['greenhouse','ashby','lever'].includes(j.ats);
  const locked = ['applied','queued','applying'].includes(j.status);
  return `<article class="job" data-id="${esc(j.id)}"><div class="score ${cls}" style="--p:${Math.max(0,Math.min(100,Math.round(j.score)))}" title="Match score">${Math.round(j.score)}<small>MATCH</small></div><div class="b">
    <div class="t"><button class="title-link" data-action="detail" data-id="${esc(j.id)}">${esc(j.title)}</button><span class="pill">${esc(j.ats)}</span>${j.status !== 'new' ? `<span class="pill ${esc(j.status)}">${prettyStatus(j.status)}</span>` : ''}</div>
    <div class="m"><span>${esc(j.company)}</span><span>${esc(j.location || 'Location not listed')}</span><span>${salary || 'Salary not listed'}</span></div>
    <div class="bar"><i style="width:${Math.max(0,Math.min(100,j.score))}%"></i></div></div>
    <div class="a"><a class="btn ghost sm" href="${esc(safeUrl(j.apply_url || j.url))}" target="_blank" rel="noopener noreferrer">Posting ↗</a>
    ${supported && !locked ? `<button class="btn ghost sm" data-action="dry" data-id="${esc(j.id)}">Practice</button><button class="btn sm" data-action="apply" data-id="${esc(j.id)}">Apply</button>` : !supported ? '<span class="pill">Open to apply</span>' : ''}
    ${!locked ? `<button class="btn ghost sm" data-action="${j.status === 'skipped' ? 'restore' : 'skip'}" data-id="${esc(j.id)}">${j.status === 'skipped' ? 'Restore' : 'Skip'}</button>` : ''}</div></article>`;
}
async function refreshState() {
  if (refreshBusy) return;
  refreshBusy = true;
  try {
    STATE = await api('/api/state');
    const s = STATE, sc = s.scheduler;
    $('#connection').classList.add('hidden');
    $('#r-sched').textContent = sc.running_apply ? 'Applying' : sc.running_scan ? 'Scanning' : sc.enabled === false ? 'Paused' : 'Running';
    $('#r-llm').textContent = s.llm.backend === 'none' ? 'No credits needed' : s.llm.backend;
    $('#r-next').textContent = rel(sc.next_scan_at);
    $('#sw-auto').checked = !!s.settings.auto_apply; $('#sw-dry').checked = !!s.settings.dry_run;
    $('#btn-scan').disabled = !!sc.running_scan; $('#btn-scan').textContent = sc.running_scan ? 'Scanning…' : 'Scan now';
    $('#btn-auto').disabled = !!sc.running_apply || !s.settings.auto_apply;
    const j = s.jobs, a = s.applications;
    $('#stats').innerHTML = [['accent',j.total || 0,'Jobs discovered'],['',j.new || 0,'To explore'],['ok',a.applied || 0,'Submitted'],['warn',a.needs_human || 0,'Need your input']].map(([c,n,l]) => `<div class="stat ${c}"><div class="n">${esc(n)}</div><div class="l">${l}</div></div>`).join('');
    const confirmed = s.settings.answers.confirmed_fields || [];
    const missing = ['work_authorized_us','requires_sponsorship','available_start'].filter(k => !confirmed.includes(k) || !s.settings.answers[k]);
    $('#readiness').innerHTML = `<div><span class="eyebrow">YOUR APPLICATION WORKSPACE</span><h2>${s.profile ? `Ready when you are, ${esc(s.profile.name.split(' ')[0])}.` : 'Your next opportunity starts here.'}</h2><p>${s.profile ? `${s.profile.skills} skills in your profile. ${s.settings.auto_apply ? 'Automatic applications are enabled.' : 'Pick a match or turn on automatic applications.'}` : 'Upload your resume to build a profile and find relevant jobs.'}</p>${missing.length ? '<p class="small muted">Add your work eligibility and start date once so required forms can proceed.</p>' : ''}</div><button class="btn ghost" id="setup-profile">${s.profile ? 'Edit profile & answers' : 'Upload resume'}</button>`;
    $('#setup-profile').onclick = () => showView('profile');
    const [events, top] = await Promise.all([api('/api/events?limit=40'),api('/api/jobs?status=new&limit=6')]);
    $('#feed').innerHTML = events.map(e => `<li><span class="lv ${esc(e.level)}"></span><span class="ts">${fmtTs(e.ts)}</span><span>${esc(e.msg)}</span></li>`).join('') || '<li class="muted">Your scan and application activity will appear here.</li>';
    $('#top-jobs').innerHTML = top.map(jobCard).join('') || '<div class="empty">Run a scan to find jobs.</div>';
    $('#scan-history').innerHTML = (s.runs || []).slice(0,3).map(r => `<details class="scan"><summary>${fmtTs(r.started_at)} <span class="pill">${esc(r.status)}</span> <span class="muted">${r.found || 0} found · ${r.new_jobs || 0} new</span></summary><pre>${esc((r.log || []).join('\n'))}</pre></details>`).join('') || '<p class="muted small">No scans yet.</p>';
  } catch (e) {
    $('#r-sched').textContent = 'Offline'; $('#connection').classList.remove('hidden');
    $('#connection').textContent = 'Cannot reach JobHub. Keep the launcher window open, then reload this page.';
  } finally { refreshBusy = false; }
}
$('#btn-scan').onclick = perform(async () => { await api('/api/scan',{method:'POST'}); toast('Scanning your enabled sources'); refreshState(); });
$('#btn-auto').onclick = perform(async () => { await api('/api/auto',{method:'POST'}); toast('Application batch started'); showView('applications'); });
for (const [id,key] of [['#sw-auto','auto_apply'],['#sw-dry','dry_run']]) $(id).onchange = perform(async e => {
  const enabled = e.target.checked;
  try { await api('/api/settings',{method:'PUT',body:JSON.stringify({[key]:enabled})}); toast(key === 'auto_apply' ? enabled ? 'Automatic applications enabled' : 'Automatic applications paused' : enabled ? 'Practice mode enabled: forms are filled without submitting' : 'Live application mode enabled'); }
  catch (err) { e.target.checked = !enabled; throw err; }
  refreshState();
});
async function loadJobs() {
  const params = new URLSearchParams({status:$('#job-status').value,limit:200});
  if ($('#job-q').value) params.set('q',$('#job-q').value);
  if ($('#job-ats').value) params.set('ats',$('#job-ats').value);
  if ($('#job-min-score').value) params.set('min_score',$('#job-min-score').value);
  const jobs = await api('/api/jobs?' + params);
  $('#jobs').innerHTML = jobs.map(jobCard).join('') || '<div class="empty">No matching jobs. Try a different filter or run a scan.</div>';
  $('#job-count').textContent = `Showing ${jobs.length}${jobs.length === 200 ? ' top' : ''} matches`;
}
['#job-q','#job-status','#job-ats','#job-min-score'].forEach(s => $(s).oninput = debounce(perform(loadJobs),250));
$('#btn-import').onclick = () => {
  modal(`<h2>Add a job from its employer link</h2><p class="muted">Paste a direct Greenhouse, Lever, or Ashby posting. Its description will be used to tailor your resume.</p><form id="import-form" class="form"><label class="full">Job URL<input type="url" required class="input" id="import-url" placeholder="https://jobs.lever.co/company/posting-id"></label><button class="btn" type="submit">Add job</button></form>`);
  $('#import-url').focus();
  $('#import-form').onsubmit = perform(async e => {
    e.preventDefault(); const button = $('button', e.target); button.disabled = true; button.textContent = 'Reading job…';
    try {
      const result = await api('/api/jobs/import', {method:'POST', body:JSON.stringify({url:$('#import-url').value.trim()})});
      toast(result.created ? 'Job added' : 'Existing job updated'); await loadJobs(); await openJob(result.job.id); refreshState();
    } finally { button.disabled = false; button.textContent = 'Add job'; }
  });
};
async function openJob(id) {
  const j = await api('/api/jobs/' + encodeURIComponent(id)), bd = j.score_json || {};
  modal(`<h2>${esc(j.title)}</h2><p class="muted">${esc(j.company)} · ${esc(j.location)}</p><p class="small muted">Match scores compare your preferences and resume. They are not hiring probabilities.</p>
  <div class="breakdown">${[['title_fit','Title'],['skills_fit','Skills'],['seniority','Experience'],['location','Location'],['salary','Salary'],['freshness','Freshness'],['ease','Form support']].map(([k,l])=>`<div>${l}<b>${esc(bd[k] ?? '?')}</b></div>`).join('')}</div>
  ${bd.review_reasons?.length ? `<p class="notice">${esc(bd.review_reasons.join(' · '))}</p>` : ''}
  <p class="small muted">${bd.matched_skills ? `Matched skills: ${esc(bd.matched_skills)}` : 'No matched skills found in the available description.'}</p>
  <div class="actions"><a class="btn ghost" target="_blank" rel="noopener noreferrer" href="${esc(safeUrl(j.url))}">Open posting ↗</a><button class="btn ghost" data-action="tailor" data-id="${esc(j.id)}">Preview tailored resume</button></div>
  <div class="desc">${esc(j.description || 'Open the posting to read its description.')}</div>`);
}
document.addEventListener('click',perform(async e => {
  const b = e.target.closest('[data-action]'); if (!b) return;
  const {action,id} = b.dataset;
  if (action === 'detail') return openJob(id);
  if (action === 'shot') return modal(`<img src="/screenshots/${encodeURIComponent(id)}" alt="Application form screenshot">`);
  if (action === 'answers') return showAnswerForm(id);
  b.disabled = true;
  try {
    if (action === 'apply' || action === 'dry') {
      const dry = action === 'dry' ? '?dry_run=true' : '';
      await api(`/api/jobs/${encodeURIComponent(id)}/apply${dry}`,{method:'POST'});
      toast(action === 'dry' ? 'Practice run queued' : 'Application queued'); closeModal(); showView('applications');
    } else if (['skip','restore'].includes(action)) {
      await api(`/api/jobs/${encodeURIComponent(id)}/status?status=${action === 'skip' ? 'skipped' : 'new'}`,{method:'POST'});
      await loadJobs(); refreshState();
    } else if (action === 'tailor') {
      b.textContent = 'Preparing resume…';
      const r = await api(`/api/jobs/${encodeURIComponent(id)}/tailor`,{method:'POST'});
      const a = document.createElement('a'); a.href = safeUrl(r.url); a.target = '_blank'; a.rel = 'noopener'; a.textContent = 'Open tailored resume PDF ↗'; a.className = 'btn'; b.replaceWith(a); a.click();
    }
  } finally { b.disabled = false; }
}));
$('#modal-x').onclick = closeModal;
$('#modal').onclick = e => { if (e.target.id === 'modal') closeModal(); };
document.addEventListener('keydown',e => { if (e.key === 'Escape') closeModal(); });
let APPLICATIONS = [];
async function loadApps() {
  if (!$('#modal').classList.contains('hidden')) return;
  APPLICATIONS = await api('/api/applications');
  $('#apps').innerHTML = APPLICATIONS.map(a => `<article class="app"><div class="h"><div><b>${esc(a.title)}</b> <span class="muted">· ${esc(a.company)}</span> <span class="pill ${esc(a.status)}">${prettyStatus(a.status)}</span></div><div class="small muted">${fmtTs(a.started_at)}</div></div>
  ${a.error ? `<p class="notice">${esc(a.error)}</p>` : ''}
  ${a.unanswered.length ? `<p class="small muted">Missing: ${esc([...new Set(a.unanswered)].join(' · '))}</p>` : ''}
  <div class="actions app-actions">${a.resume_path ? `<a class="btn ghost sm" target="_blank" rel="noopener" href="/api/resume/file?path=${encodeURIComponent(a.resume_path)}">Resume used ↗</a>` : ''}<a class="btn ghost sm" target="_blank" rel="noopener noreferrer" href="${esc(safeUrl(a.url))}">Posting ↗</a>
  ${a.unanswered.length && ['needs_human','dry_run'].includes(a.status) ? `<button class="btn sm" data-action="answers" data-id="${a.id}">Add missing answers</button>` : ''}
  ${!a.submission_attempted && ['needs_human','failed','dry_run'].includes(a.status) ? `<button class="btn ghost sm" data-action="dry" data-id="${esc(a.job_id)}">Practice again</button><button class="btn ghost sm" data-action="apply" data-id="${esc(a.job_id)}">Retry application</button>` : ''}</div>
  <div class="shots">${a.screenshots.map(s => `<button class="shot" data-action="shot" data-id="${esc(s)}"><img src="/screenshots/${encodeURIComponent(s)}" loading="lazy" alt="Captured application form"></button>`).join('')}</div>
  <details><summary>Activity log (${a.log.length})</summary><pre>${esc(a.log.join('\n'))}</pre></details></article>`).join('') || '<div class="empty">Your application history will appear here. Start with a practice run from Jobs.</div>';
}
async function showAnswerForm(id) {
  const app = APPLICATIONS.find(a => String(a.id) === id); if (!app) return;
  const settings = await api('/api/settings');
  const questions = [...new Set(app.unanswered.map(q => q.replace(/ \(required\)$/,'')))];
  modal(`<h2>Complete your application answers</h2><p class="muted">${esc(app.company)} · ${esc(app.title)}</p><p class="small muted">Use the exact option wording for a choice question. Saved answers are reused when the same question appears. Leave unknown answers blank.</p><div class="form">${questions.map((q,i)=>`<label class="full">${esc(q)}<textarea class="input" data-question-index="${i}">${esc(settings.answers.field_answers?.[q] || '')}</textarea></label>`).join('')}</div><button class="btn" id="save-field-answers" style="margin-top:16px">Save answers</button>`);
  $('#save-field-answers').onclick = perform(async () => {
    const field_answers = {...settings.answers.field_answers};
    $$('[data-question-index]').forEach(input => { const key = questions[Number(input.dataset.questionIndex)]; field_answers[key] = input.value.trim(); });
    await api('/api/settings',{method:'PUT',body:JSON.stringify({answers:{field_answers}})});
    closeModal(); toast('Answers saved. Retry this application when ready.');
  });
}
const PFIELDS = ['first_name','last_name','email','phone','city','state','country','linkedin','github','website','headline'];
const AFIELDS = ['work_authorized_us','requires_sponsorship','available_start','willing_to_relocate','salary_expectation','how_heard','preferred_name','pronouns'];
const ALABELS = {work_authorized_us:'Authorized to work in the U.S.',requires_sponsorship:'Require visa sponsorship now or later',available_start:'Earliest full-time start',willing_to_relocate:'Willing to relocate'};
async function loadProfile() {
  [PROFILE,SETTINGS] = await Promise.all([api('/api/profile'),api('/api/settings')]);
  const p = PROFILE || {};
  $('#profile-form').innerHTML = PFIELDS.map(k=>`<label>${label(k)}<input class="input" data-k="${k}" value="${esc(p[k])}" autocomplete="off"></label>`).join('') + `<label class="full">Summary<textarea class="input" data-k="summary">${esc(p.summary)}</textarea></label><label class="full">Skills (comma separated)<textarea class="input" data-k="skills">${esc((p.skills || []).join(', '))}</textarea></label>`;
  const confirmed = SETTINGS.answers.confirmed_fields || [];
  $('#answers-form').innerHTML = AFIELDS.map(k => {
    const value = confirmed.includes(k) ? SETTINGS.answers[k] || '' : '';
    return `<label>${ALABELS[k] || label(k)}${['work_authorized_us','requires_sponsorship','willing_to_relocate'].includes(k) ? `<select class="input" data-a="${k}">${['','Yes','No'].map(v=>`<option value="${v}" ${value === v ? 'selected' : ''}>${v || 'Not answered'}</option>`).join('')}</select>` : `<input class="input" data-a="${k}" value="${esc(value)}" placeholder="Not answered">`}</label>`;
  }).join('');
  $('#profile-content').innerHTML = p.resume_path ? `<a class="text-link" href="/api/resume/file?path=${encodeURIComponent(p.resume_path)}" target="_blank" rel="noopener">Original resume PDF ↗</a>` : '<p>Upload a PDF to enable applications.</p>';
  $('#profile-content').innerHTML += ['education','experience','projects'].map(section=>`<details class="profile-section"><summary>${label(section)} (${(p[section] || []).length})</summary><div class="form">${(p[section] || []).map((item,index)=>Object.entries(item).map(([key,value])=>`<label class="${['bullets','tech','school','name','title'].includes(key) ? 'full' : ''}">${label(key)}${Array.isArray(value) ? `<textarea class="input" data-section="${section}" data-index="${index}" data-field="${key}" data-array="true">${esc(value.join('\n'))}</textarea>` : `<input class="input" data-section="${section}" data-index="${index}" data-field="${key}" value="${esc(value)}">`}</label>`).join('')).join('<div class="divider full"></div>')}</div></details>`).join('');
}
$('#btn-save-profile').onclick = perform(async () => {
  const patch = {}; $$('#profile-form [data-k]').forEach(i=>patch[i.dataset.k] = i.dataset.k === 'skills' ? i.value.split(',').map(s=>s.trim()).filter(Boolean) : i.value);
  for (const section of ['education','experience','projects']) patch[section] = structuredClone(PROFILE?.[section] || []);
  $$('#profile-content [data-field]').forEach(i=> {patch[i.dataset.section][Number(i.dataset.index)][i.dataset.field] = i.dataset.array ? i.value.split('\n').map(s=>s.trim()).filter(Boolean) : i.value;});
  await api('/api/profile',{method:'PUT',body:JSON.stringify(patch)});
  const answers = {}; const confirmed_fields = [];
  $$('#answers-form [data-a]').forEach(i=>{ answers[i.dataset.a] = i.value.trim(); if (i.value.trim()) confirmed_fields.push(i.dataset.a); });
  await api('/api/settings',{method:'PUT',body:JSON.stringify({answers:{...answers,confirmed_fields}})});
  toast('Profile and your confirmed answers saved'); refreshState();
});
$('#resume-file').onchange = perform(async e=> {
  const f = e.target.files[0]; if (!f) return;
  toast('Reading your resume…'); const fd = new FormData(); fd.append('file',f);
  const r = await fetch('/api/profile/resume',{method:'POST',body:fd}); const data = await r.json();
  if (!r.ok) throw new Error(data.detail || 'Resume upload failed');
  e.target.value = ''; toast('Resume ready'); await loadProfile(); refreshState();
});
const SFIELDS = [['target_titles','list'],['exclude_title_words','list'],['preferred_locations','list'],['min_salary','num'],['years_experience','num'],['scan_interval_min','num'],['max_apply_per_run','num'],['min_score_to_apply','num'],['remote_ok','bool'],['include_internships','bool']];
async function loadSettings() {
  SETTINGS = await api('/api/settings');
  $('#settings-form').innerHTML = SFIELDS.map(([k,t])=>t === 'bool' ? `<label class="check"><input type="checkbox" data-s="${k}" data-t="bool" ${SETTINGS[k] ? 'checked' : ''}>${label(k)}</label>` : t === 'list' ? `<label class="full">${label(k)} (comma separated)<textarea class="input" data-s="${k}" data-t="list">${esc((SETTINGS[k] || []).join(', '))}</textarea></label>` : `<label>${label(k)}<input class="input" type="number" min="0" data-s="${k}" data-t="num" value="${esc(SETTINGS[k])}"></label>`).join('') +
  `<label>Resume strategy<select class="input" data-s="resume_strategy" data-t="text"><option value="tailored" ${SETTINGS.resume_strategy === 'tailored' ? 'selected' : ''}>Tailored to each job</option><option value="original" ${SETTINGS.resume_strategy === 'original' ? 'selected' : ''}>Original PDF</option></select></label><label>Application browser<select class="input" data-s="browser_channel" data-t="text"><option value="">Included browser</option><option value="chrome" ${SETTINGS.browser_channel === 'chrome' ? 'selected' : ''}>Google Chrome</option></select></label>`;
  $('#sources-form').innerHTML = Object.entries(SETTINGS.sources).map(([k,v])=>`<label class="check"><input type="checkbox" data-src="${k}" ${v ? 'checked' : ''}>${esc(k === 'jobspy' ? 'LinkedIn, Indeed & other boards' : k)}</label>`).join('') +
  `<label class="full">Board search terms (comma separated)<textarea class="input" data-js="search_terms">${esc(SETTINGS.jobspy.search_terms.join(', '))}</textarea></label><label>Search location<input class="input" data-js="location" value="${esc(SETTINGS.jobspy.location)}"></label><label>Sites (comma separated)<input class="input" data-js="sites" value="${esc(SETTINGS.jobspy.sites.join(', '))}"></label><label>Results per search<input type="number" class="input" data-js="results_wanted" value="${SETTINGS.jobspy.results_wanted}"></label><label>Maximum age in hours<input type="number" class="input" data-js="hours_old" value="${SETTINGS.jobspy.hours_old}"></label>`;
  $('#companies-form').innerHTML = ['greenhouse','lever','ashby'].map(k=>`<label>${k}<textarea class="input" data-co="${k}">${esc(SETTINGS.companies[k].join('\n'))}</textarea></label>`).join('');
}
$('#btn-save-settings').onclick = perform(async ()=> {
  const patch = {}; $$('#settings-form [data-s]').forEach(i=>{const t = i.dataset.t;patch[i.dataset.s] = t === 'bool' ? i.checked : t === 'num' ? Number(i.value) : t === 'list' ? i.value.split(',').map(s=>s.trim()).filter(Boolean) : i.value;});
  const js = {}; $$('#sources-form [data-js]').forEach(i=>{const k = i.dataset.js;js[k] = ['search_terms','sites'].includes(k) ? i.value.split(',').map(s=>s.trim()).filter(Boolean) : ['results_wanted','hours_old'].includes(k) ? Number(i.value) : i.value;}); patch.jobspy = js;
  patch.sources = {}; $$('[data-src]').forEach(i=>patch.sources[i.dataset.src] = i.checked);
  patch.companies = {}; $$('[data-co]').forEach(i=>patch.companies[i.dataset.co] = i.value.split(/\n|,/).map(s=>s.trim()).filter(Boolean));
  await api('/api/settings',{method:'PUT',body:JSON.stringify(patch)}); toast('Preferences saved'); refreshState();
});
function debounce(f,ms) { let t; return (...args)=>{clearTimeout(t);t = setTimeout(()=>f(...args),ms);}; }
refreshState();
setInterval(()=>{refreshState();if (activeView === 'applications') perform(loadApps)();},5000);
