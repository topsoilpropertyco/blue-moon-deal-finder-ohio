/* The property drawer: BM.renderProperty(root, id).
 *
 * Reads GET /api/m/{slug}/properties/{id} (PropertyDetail) and shows the owner contact work first (contact
 * level ladder, the DRILL timeline, the next step with its links and buttons, contacts with reach-out
 * outcomes, level actions, Crexi credit), then why the score is what it is, the photo check, rent, building
 * and owner facts, links, the deal outcome and every original column. Every write goes to the endpoints in
 * ARCHITECTURE.md and re-draws from the response; a 'bm:property-changed' event tells the open screen.
 */
(function () {
  'use strict';
  if (!window.BM) return;
  const {esc, fmt, ui} = BM;

  const LEVELS = {1: 'No owner data', 2: 'Possible owner', 3: 'Confirmed owner'};
  const CH_LABEL = {mail: 'Mail', phone: 'Call', text: 'Text', email: 'Email', social: 'Social', vm: 'Voicemail'};
  const CH_CLASS = {verified: 'v', untested: 'u', wrong: 'w', dnc: 'w dnc'};
  const CONTACT_STATUS = [['untested', 'Not tested yet'], ['verified', 'Works'], ['wrong', 'Wrong'], ['dnc', 'Do not contact']];
  const ADD_CHANNELS = [['phone', 'Phone (call)'], ['text', 'Text number'], ['email', 'Email'], ['social', 'Social profile'], ['vm', 'Voicemail drop'], ['mail', 'Mailing address']];
  const ROLES = [['', 'Role (optional)'], ['possible_owner', 'Owner'], ['member', 'LLC member'], ['officer', 'Officer'], ['trustee', 'Trustee'], ['deed_signer', 'Deed signer'], ['heir', 'Heir'], ['manager', 'Property manager'], ['agent', 'Agent'], ['other', 'Other']];
  const ATTEMPT = {found: 'Found', nothing: 'Nothing found', skipped: 'Skipped'};
  const ATTEMPT_CLASS = {found: 'hit', nothing: 'miss', skipped: 'skip'};
  const COST = {free: 'Free', credit: 'Uses a Crexi credit', paid: 'Paid', plan: 'Your Claude plan (no per-call bill)'};
  const ACTOR = {auto: 'automatic', ai: 'AI research'};
  const AI_SOURCE = 'ai_research';
  const CONFIDENCE = {high: 'sure', medium: 'fairly sure', low: 'unsure'};
  const FINDING_KIND = {entity: 'Business filing', person: 'Person', website: 'Website', phone: 'Phone', email: 'Email', social: 'Social page'};
  const RUN_OUTCOME = {found: 'Found something', nothing: 'Nothing new', error: 'Could not finish'};
  const ABSENTEE = {local: 'Local (same city)', instate: 'Elsewhere in the state', outofstate: 'Out of state', unknown: 'Unknown'};
  const DEBT = {free_and_clear: 'Likely free and clear', maturing_soon: 'Loan maturing soon', active: 'Active loan', unknown: 'Unknown'};
  const LABEL_TEXT = {EXTERIOR_CORRIDOR: 'Exterior corridor', DIRECT_ENTRY: 'Direct entry', INTERIOR_CORRIDOR: 'Interior corridor', MIXED: 'Mixed', UNKNOWN: "Can't see the building"};
  const FLAG_TEXT = {
    year_unknown: 'Year built unknown', year_invalid: 'Year built looked wrong, so it is ignored', units_unknown: 'Unit count unknown',
    units_from_addresses: 'Units estimated from the number of addresses', shared_apn: 'Shares a parcel number with another row',
    no_owner_name: 'No owner name in the export', no_coordinates: 'No map location yet', no_address: 'No street address',
  };
  const RENT_FIT = {in: ['g', 'Area rents match the scale'], near: ['y', 'Area rents are close to the scale'], out: ['r', 'Area rents are off the scale'], na: ['n', 'No HUD rent for this area']};
  const RENT_SOURCE = {hud_arcgis: 'HUD Fair Market Rents (HUD map service)', hud_file: 'HUD Fair Market Rents (downloaded file)', hud_api: 'HUD Fair Market Rents (HUD API)'};
  const GEOCODE = {source: 'From the Crexi export', exact: 'Exact (Census)', approximate: 'Approximate (Census)', google: 'Google', failed: 'Could not place it'};
  const SET_ASIDE = {drive_time: 'Too far to drive', year: 'Built too early', size: 'Wrong size', state: 'State not in the buy box', skip_county: 'County on the skip list', property_type: 'Not this kind of property', manual: 'Set aside by hand'};

  let cur = null;   // {id, slug, root, detail, open, seq, busy, mode:{}, kept:Set}
  let seqNo = 0;

  /* ---------- entry point ---------- */

  BM.renderProperty = async function renderProperty(root, id) {
    bindOnce(root);
    cur = {id: Number(id), slug: BM.state.market, root, detail: null, open: null, seq: ++seqNo, busy: false,
      mode: {}, kept: new Set()};
    setTitle(root, 'Loading…');
    root.innerHTML = ui.loading('Loading the property…');
    await load(cur, {});
  };

  async function load(c, opts) {
    let d;
    try {
      d = await BM.api(BM.m('/properties/' + c.id));
    } catch (err) {
      if (cur !== c) return;
      if (opts.quiet) { BM.toast(err.message, {error: true}); return; }
      c.root.innerHTML = '<div class="errbox" role="alert"><p>' + esc(err.message) + '</p>' +
        '<button type="button" class="btn small" data-act="reload">Try again</button></div>';
      return;
    }
    if (cur !== c) return;
    c.detail = d;
    draw(opts);
  }

  function scroller(root) {
    return root.closest('.drawer') || root;
  }

  function setTitle(root, text) {
    const t = scroller(root).querySelector('.dtitle');
    if (t) t.textContent = text;
  }

  function draw(opts) {
    const c = cur;
    const d = c.detail;
    const root = c.root;
    const box = scroller(root);
    const top = box.scrollTop;
    root.querySelectorAll('details[data-keep]').forEach((el) => {
      if (el.open) c.kept.add(el.getAttribute('data-keep')); else c.kept.delete(el.getAttribute('data-keep'));
    });
    setTitle(root, placeText(d).addr);
    // 'How did it end?' goes right under the score once the owner is confirmed (or an outcome is saved);
    // otherwise the DRILL work stays on top. Decided once per opening, so a save never moves it under the finger.
    if (c.outcomeEarly === undefined) {
      c.outcomeEarly = Number((d.drill || {}).level || d.level || 1) === 3 || !!(d.outcome && d.outcome.value);
    }
    const early = c.outcomeEarly;
    root.innerHTML = headerHtml(d) + scoreHtml(d) + buyboxHtml(d) + (early ? outcomeHtml(d) : '') + contactHtml(d) + whyHtml(d) + photoHtml(d) +
      rentHtml(d) + buildingHtml(d) + ownerHtml(d) + linksHtml(d) + (early ? '' : outcomeHtml(d)) + rawHtml(d);
    root.querySelectorAll('details[data-keep]').forEach((el) => { if (c.kept.has(el.getAttribute('data-keep'))) el.open = true; });
    box.scrollTop = opts && opts.keepScroll ? top : 0;
  }

  function redrawContact(focusForm) {
    const el = cur.root.querySelector('[data-part="contact"]');
    if (!el) { draw({keepScroll: true}); return; }
    el.outerHTML = contactHtml(cur.detail);
    if (focusForm) {
      const form = cur.root.querySelector('[data-part="contact"] form[data-form]');
      if (form) {
        const first = form.querySelector('input:not([type="radio"]):not([type="hidden"]), textarea, select');
        form.scrollIntoView({block: 'nearest', behavior: 'smooth'});
        if (first) first.focus({preventScroll: true});
      }
    }
  }

  /* ---------- small helpers ---------- */

  function has(v) {
    return v !== null && v !== undefined && v !== '';
  }

  function safeUrl(u) {
    return typeof u === 'string' && /^https?:\/\//i.test(u.trim()) ? u.trim() : null;
  }

  function extLink(url, label, title) {
    const u = safeUrl(url);
    if (!u) return '';
    return '<a href="' + esc(u) + '" target="_blank" rel="noopener noreferrer"' + (title ? ' title="' + esc(title) + '"' : '') + '>' +
      esc(label) + ' <span aria-hidden="true">↗</span></a>';
  }

  function kv(rows) {
    const items = rows.filter((r) => r && has(r[1]));
    if (!items.length) return '';
    return '<dl class="kv">' + items.map(([k, v, raw]) => '<dt>' + esc(k) + '</dt><dd>' + (raw ? v : esc(v)) + '</dd>').join('') + '</dl>';
  }

  function section(title, body, extra) {
    return '<section class="sec"' + (extra || '') + '><h2>' + esc(title) + '</h2>' + body + '</section>';
  }

  function labelText(label) {
    if (!label) return '';
    return LABEL_TEXT[label] || String(label).toLowerCase().replace(/_/g, ' ').replace(/^\w/, (ch) => ch.toUpperCase());
  }

  function placeText(d) {
    const addr = fmt.title(d.address || d.name || 'No address') + (d.unit ? ' ' + fmt.title(String(d.unit)) : '');
    const city = [fmt.title(d.city || ''), d.state].filter(Boolean).join(', ');
    return {addr, city};
  }

  function drillOf(d) {
    return d.drill || {};
  }

  function stageByKey(dr, key) {
    return (dr.stages || []).find((s) => s.key === key) || null;
  }

  function options(list, selected) {
    return list.map(([v, l]) => '<option value="' + esc(v) + '"' + (v === selected ? ' selected' : '') + '>' + esc(l) + '</option>').join('');
  }

  function radios(name, list, selected) {
    return '<div class="p-radio" role="radiogroup">' + list.map(([v, l]) =>
      '<label><input type="radio" name="' + name + '" value="' + esc(v) + '"' + (v === selected ? ' checked' : '') + '><span>' + esc(l) + '</span></label>').join('') + '</div>';
  }

  function field(label, inner, cls) {
    return '<label class="field' + (cls ? ' ' + cls : '') + '">' + esc(label) + inner + '</label>';
  }

  function input(name, attrs) {
    return '<input name="' + name + '" ' + (attrs || '') + '>';
  }

  /* ---------- header + score ---------- */

  function headerHtml(d) {
    const {addr, city} = placeText(d);
    const drive = d.drive || {};
    const mins = has(d.drive_minutes) ? fmt.minutes(d.drive_minutes, d.drive_method) + ' from ' + (drive.origin || 'the starting point') : '';
    const pills = [];
    if (d.by_hand === 'aside') {
      pills.push('<span class="pill bad">Set aside by you</span>');
    } else if (d.in_buybox === false) {
      const why = (d.exclusion_reasons || []).map((r) => SET_ASIDE[r] || r).join(', ');
      pills.push('<span class="pill bad">Set aside' + (why ? ': ' + esc(why) : '') + '</span>');
    } else {
      if (d.by_hand === 'added') pills.push('<span class="pill good">In the buy box: added by you</span>');
      if (has(d.rank)) pills.push('<span class="pill hot">Rank ' + fmt.num(d.rank) + '</span>');
    }
    return '<div class="dhead"><div><div class="eyebrow">' + esc([city, mins].filter(Boolean).join(' · ')) + '</div>' +
      '<h1 style="font-size:28px;margin-top:2px">' + esc(addr) + '</h1>' +
      (pills.length ? '<div class="p-sub">' + pills.join('') + '</div>' : '') + '</div></div>';
  }

  function scoreHtml(d) {
    let text = 'Blue Moon score: how worth chasing this owner is.';
    if (has(d.coverage_total) && Number(d.coverage_total) > 0) {
      text += ' Based on ' + fmt.num(d.coverage_have || 0) + ' of ' + fmt.num(d.coverage_total) + ' signals.';
    }
    if (!has(d.score)) text = 'Not scored yet. The Score step (free, on the Run screen) fills this in.';
    return '<div class="score">' + ui.moon(d.score, 64) + '<div><div class="big num">' + esc(fmt.pct(d.score)) + '</div>' +
      '<p>' + esc(text) + '</p></div></div>';
  }

  /* ---------- buy box: what the rules say, and decisions made by hand ---------- */

  /** Who decided, readably: 'on the Mac', 'by seth@…', 'by the team'. */
  function byText(who) {
    const w = String(who || '').trim();
    if (!w || w === 'unknown') return '';
    return {'this Mac': 'on the Mac', open: 'by you', team: 'by the team', cli: 'from the command line'}[w] || 'by ' + w;
  }

  function decisionText(reason, decision, label) {
    const rule = '“' + (label || reason) + '”';
    if (decision === 'include') return reason === '*' ? 'Added to the buy box' : rule + ' no longer counts';
    if (decision === 'exclude') return 'Set aside by hand';
    if (decision === 'keep') return 'Kept set aside for ' + rule;
    if (decision === 'cleared') return 'Back to the rules';
    return String(decision || '');
  }

  function whenText(by, at) {
    return [byText(by), at ? fmt.date(at) : ''].filter(Boolean).join(', ');
  }

  function buyboxHtml(d) {
    const bb = d.buybox;
    if (!bb || typeof bb !== 'object') return '';
    const rules = Array.isArray(bb.rules) ? bb.rules : [];
    const decisions = Array.isArray(bb.decisions) ? bb.decisions : [];
    const history = Array.isArray(bb.history) ? bb.history : [];
    const inBox = bb.in_buybox !== false;
    const failing = rules.filter((r) => !r.waived);
    let verdict;
    let more = '';
    if (bb.manual === 'exclude') {
      verdict = 'Set aside by you.';
      more = rules.length ? 'The rules also set it aside.' : 'By the rules it would be in the buy box.';
    } else if (bb.manual === 'include') {
      verdict = 'In the buy box: added by you.';
      more = rules.length ? 'By the rules it would be set aside.' : 'The rules agree.';
    } else if (bb.manual === 'waived') {
      verdict = inBox ? 'In the buy box: you said the rules it fails don’t count.' : 'Set aside by the rules.';
      if (!inBox) more = 'You said some rules don’t count here, but ' + (failing.length === 1 ? 'one still does.' : 'others still do.');
    } else {
      verdict = inBox ? 'In the buy box: it fits every rule.' : 'Set aside by the rules.';
    }
    const list = rules.length ? '<ul class="bb-rules">' + rules.map((r) =>
      '<li class="' + (r.waived ? 'off' : '') + '"><span class="mk" aria-hidden="true">' + (r.waived ? '–' : '✕') + '</span><div><b>' +
      esc(r.label || r.reason) + '</b>' + (r.detail ? ' <span class="d">' + esc(r.detail) + '</span>' : '') +
      (r.waived ? '<span class="pill">doesn’t count: your choice</span>' : '') +
      (r.kept ? '<span class="pill">kept set aside by you</span>' : '') + '</div></li>').join('') + '</ul>'
      : '<ul class="bb-rules"><li class="ok"><span class="mk" aria-hidden="true">✓</span><div>Passes every buy-box rule' +
        (bb.size_detail ? ' <span class="d">(' + esc(bb.size_detail) + (bb.size_estimated ? ', estimated' : '') + ')</span>' : '') + '.</div></li></ul>';
    const groups = Array.isArray(bb.groups) ? bb.groups : [];
    const groupLabel = {};
    groups.forEach((g) => {     // 'Set aside by hand: Set aside by hand' -> 'Set aside by hand'
      const parts = String(g.label || g.key).split(': ');
      g.label = parts.length === 2 && parts[0] === parts[1] ? parts[0] : g.label;
      groupLabel[g.key] = g.label;
    });
    const decided = decisions.length ? '<div class="bb-decided">' + decisions.map((x) => {
      const bits = [decisionText(x.reason, x.decision, x.reason_label), whenText(x.decided_by, x.decided_at)];
      if (x.bucket && groupLabel[x.bucket]) bits.push('decided for the group ' + groupLabel[x.bucket]);
      return '<span>' + esc(bits.filter(Boolean).join(' · ')) + '</span>' +
        (x.note ? '<p class="note">Note: ' + esc(x.note) + '</p>' : '') +
        (x.detail_at_decision && x.detail_at_decision !== x.detail_now ? '<p class="note">Then: ' + esc(x.detail_at_decision) + '</p>' : '');
    }).join('') + '</div>' : '';
    const buttons = [];
    if (!inBox) buttons.push('<button type="button" class="btn small primary" data-act="buybox" data-decision="include">Add to buy box</button>');
    else buttons.push('<button type="button" class="btn small" data-act="buybox" data-decision="exclude">Set aside</button>');
    if (decisions.length) {
      buttons.push('<button type="button" class="btn small" data-act="buybox" data-decision="rules">Undo: follow the rules</button>');
    }
    const groupLinks = groups.length ? '<div class="bb-groups"><span>See others like it:</span>' +
      groups.map((g) => '<button type="button" class="linkbtn" data-act="bb-review" data-bucket="' + esc(g.key) + '" title="Review every property in this group">' +
        esc(g.label) + '</button>').join('') + '</div>' : '';
    const hist = history.length ? '<details class="p-more" data-keep="bbhist"><summary>History (' + (history.length >= 20 ? 'the last 20' : fmt.num(history.length)) + ')</summary>' +
      '<ul class="bb-hist">' + history.map((h) => {
        let what = decisionText(h.reason, h.decision, h.reason_label);
        if (h.decision === 'cleared' && h.previous) what += ' (was: ' + decisionText(h.reason, h.previous, h.reason_label).toLowerCase() + ')';
        const note = h.note && /^Undo of /.test(h.note) ? 'an Undo' : h.note;   // undo notes name a batch id
        return '<li><span>' + esc(what) + '</span><span class="when">' + esc(whenText(h.actor, h.created_at)) +
          (note ? ' · ' + esc(note) : '') + '</span></li>';
      }).join('') + '</ul></details>' : '';
    const help = inBox ? 'Set aside means no new photos and no Crexi credits for it, whatever the rules say.'
      : 'Add to buy box means it gets photos, photo checks and Crexi credits when those steps run.';
    return section('Buy box',
      '<p class="bb-verdict ' + (inBox ? 'in' : 'out') + '">' + esc(verdict) + (more ? ' <span class="muted">' + esc(more) + '</span>' : '') + '</p>' +
      list + decided + groupLinks +
      '<div class="p-actions">' + buttons.join('') + '</div><p class="note" style="margin:0">' + esc(help) + '</p>' + hist,
      ' data-part="buybox"');
  }

  /* ---------- owner contact (the DRILL) ---------- */

  function contactHtml(d) {
    const dr = drillOf(d);
    const level = Number(dr.level || d.level || 1);
    const status = dr.status || d.drill_status || 'active';
    const names = Object.assign({}, LEVELS, dr.levels || {});
    const ladder = '<div class="ladder' + (level === 3 ? ' done' : '') + (status === 'dnc' ? ' off' : '') + '">' +
      [1, 2, 3].map((n) => '<div class="' + (n <= level ? 'on' : '') + (n === level ? ' cur' : '') + '"><b>Level ' + n + '</b>' +
        esc(names[n] || names[String(n)] || LEVELS[n]) + '</div>').join('') + '</div>';
    return '<section class="sec" data-part="contact" id="p-contact"><h2>Owner contact</h2>' + ladder + bannerHtml(dr, status) +
      whoHtml(d, dr, level) +
      '<p class="note" style="margin:0">Green = works · yellow = found, not tested · red = wrong · dashed = none found.</p>' +
      '<h3 style="margin-top:6px">Drill so far</h3>' + timelineHtml(dr) +
      nextHtml(d, dr, status) + contactsHtml(d, dr, status) + researchHtml(dr) + actionsHtml(d, dr, level, status) +
      (dr.notes ? '<h3>Notes</h3><pre class="p-notes">' + esc(dr.notes) + '</pre>' : '') + '</section>';
  }

  function bannerHtml(dr, status) {
    if (status === 'dnc') return '<div class="p-banner bad">Do not contact. This owner is off every list until you allow contact again.</div>';
    if (status === 'exhausted') return '<div class="p-banner">Every source has been tried. Reopen it when something new turns up.</div>';
    if (dr.handed_off_at) return '<div class="p-banner good">Handed off to the CRM on ' + esc(fmt.date(dr.handed_off_at)) + '.</div>';
    return '';
  }

  function whoHtml(d, dr, level) {
    const owner = d.owner || {};
    let maker = 'Unknown';
    if (dr.confirmed_person) maker = dr.confirmed_person + ' (confirmed' + (dr.confirmed_at ? ' ' + fmt.date(dr.confirmed_at) : '') + ')';
    else if (dr.best_guess_person || d.best_guess_person) maker = 'Best guess: ' + (dr.best_guess_person || d.best_guess_person);
    const count = Number(owner.portfolio_count || 0);
    const type = [owner.owner_type_label || (d.owner_type ? labelText(d.owner_type) : ''),
      count > 1 ? 'owns ' + fmt.num(count) + ' properties' : ''].filter(Boolean).join(' · ');
    return kv([
      ['Owner on record', d.owner_name || (level === 1 ? 'Not on record' : '')],
      ['Decision-maker', maker],
      ['Type', type],
      ['Channels', ui.channelBadges(dr.channels || d.channels).replace('class="chs"', 'class="chs" style="margin-left:0"'), true],
      ['Mailing address', owner.mailing_text],
    ]);
  }

  function effectOf(dr) {
    const map = {};
    (dr.outcome_options || []).forEach((o) => { map[o.key] = o.effect; });
    return map;
  }

  function timelineHtml(dr) {
    const effects = effectOf(dr);
    const events = [];
    (dr.stages || []).forEach((st) => (st.attempts || []).forEach((a) => events.push({
      at: a.created_at || '', id: Number(a.id) || 0, order: 0, cls: ATTEMPT_CLASS[a.outcome] || '',
      src: st.name || st.key, res: (ATTEMPT[a.outcome] || a.outcome || '') + (a.detail ? ': ' + a.detail : ''),
      who: ACTOR[a.actor] || '',
    })));
    (dr.outreach || []).forEach((o) => {
      const eff = effects[o.outcome];
      events.push({at: o.created_at || '', id: Number(o.id) || 0, order: 1,
        cls: eff === 'confirm' ? 'hit' : (eff === 'mark_wrong' || eff === 'dnc') ? 'miss' : '',
        src: CH_LABEL[o.channel] || o.channel, res: (o.outcome_label || o.outcome || '') + (o.notes ? ': ' + o.notes : ''), who: ''});
    });
    (dr.credits || []).filter((cr) => !cr.result).forEach((cr) => events.push({at: cr.created_at || '', id: Number(cr.id) || 0, order: 2,
      cls: '', src: 'Crexi credit', res: 'Used. Record what Crexi showed.', who: ''}));
    events.sort((a, b) => a.at.localeCompare(b.at) || a.order - b.order || a.id - b.id);
    const next = dr.next_step || {};
    const tail = {stage: ['next', 'Next'], validate: ['next', 'Next'], handoff: ['hit', 'Done'], dnc: ['miss', 'Stopped'], exhausted: ['skip', 'Paused']}[next.kind];
    let html = events.map((e) => '<li class="' + e.cls + '"><div><span class="src">' + esc(e.src) + '</span> <span class="res">· ' + esc(e.res) +
      '</span> <span class="when">' + esc([e.at ? fmt.date(e.at) : '', e.who].filter(Boolean).join(' · ')) + '</span></div></li>').join('');
    if (next.text) html += '<li class="' + (tail ? tail[0] : 'next') + '"><div><span class="src">' + esc(tail ? tail[1] : 'Next') +
      '</span> <span class="res">· ' + esc(next.text) + '</span></div></li>';
    if (!html) html = '<li class="skip"><div><span class="res">Nothing tried yet.</span></div></li>';
    return '<ul class="timeline">' + html + '</ul>';
  }

  function linkListHtml(links) {
    const list = (links || []).filter((l) => safeUrl(l.url));
    if (!list.length) return '';
    return '<div class="p-linklist">' + list.map((l) => '<div class="item"><div class="links">' + extLink(l.url, l.label || 'Open', l.finds) + '</div>' +
      (l.how ? '<p>' + esc(l.how) + '</p>' : '') + '</div>').join('') + '</div>';
  }

  function nextHtml(d, dr, status) {
    const next = dr.next_step || {};
    const head = '<div class="eyebrow">Next step</div>';
    if (next.kind === 'stage') {
      const st = stageByKey(dr, next.stage_key) || {};
      const facts = [COST[st.cost] || st.cost,
        has(st.hit_rate) ? 'finds something about ' + Math.round(Number(st.hit_rate) * 100) + '% of the time (a starting guess)' : '']
        .filter(Boolean).join(' · ');
      const open = cur.open === 'attempt' && (cur.mode.stage || next.stage_key) === next.stage_key;
      return '<div class="p-next">' + head + '<div class="what">' + esc(st.name || 'Next stage') + '</div>' +
        (st.method ? '<p class="p-how">' + esc(st.method) + '</p>' : '') + (facts ? '<p class="p-how">' + esc(facts) + '</p>' : '') +
        linkListHtml(st.links) +
        (open ? attemptForm(dr, next.stage_key) :
          '<div class="p-actions"><button type="button" class="btn primary small" data-act="attempt" data-outcome="found" data-stage="' + esc(next.stage_key) + '">Found something</button>' +
          '<button type="button" class="btn small" data-act="attempt" data-outcome="nothing" data-stage="' + esc(next.stage_key) + '">Nothing found</button>' +
          '<button type="button" class="btn small" data-act="attempt" data-outcome="skipped" data-stage="' + esc(next.stage_key) + '">Not applicable</button></div>') +
        '</div>';
    }
    if (next.kind === 'validate') {
      return '<div class="p-next">' + head + '<div class="what">Reach out and confirm</div><p class="p-how">' + esc(next.text) + '</p>' +
        (next.note ? '<p class="p-how">' + esc(next.note) + '</p>' : '') +
        '<p class="p-how">Log what happened on the contact below. “Talked to owner” confirms them (level 3).</p></div>';
    }
    if (next.kind === 'handoff') {
      return '<div class="p-next done">' + head + '<div class="what">' + esc(next.text) + '</div>' +
        (dr.handed_off_at ? '' : '<p class="p-how">The CRM file (crm_handoff.csv) is on the Exports screen.</p>' +
          '<div class="p-actions"><button type="button" class="btn small primary" data-act="level" data-action="handoff">Mark handed off to the CRM</button></div>') + '</div>';
    }
    if (next.kind === 'exhausted') {
      return '<div class="p-next">' + head + '<div class="what">' + esc(next.text) + '</div>' +
        '<div class="p-actions"><button type="button" class="btn small" data-act="level" data-action="reopen">Reopen</button></div></div>';
    }
    if (next.kind === 'dnc' || status === 'dnc') {
      return '<div class="p-next">' + head + '<div class="what">' + esc(next.text || 'Do not contact.') + '</div>' +
        '<div class="p-actions"><button type="button" class="btn small" data-act="level" data-action="undnc">Allow contact again</button></div></div>';
    }
    return '';
  }

  function attemptForm(dr, stageKey) {
    const stages = (dr.stages || []).filter((s) => s.status !== 'disabled' && s.enabled !== false);
    const outcome = cur.mode.outcome || 'found';
    const people = (dr.people || []).map((p) => p.name).filter(Boolean);
    return '<form class="p-form" data-form="attempt" data-mode="' + esc(outcome) + '">' +
      '<h3>Record what you found</h3>' +
      field('Stage', '<select name="stage_key">' + options(stages.map((s) => [s.key, s.name + (s.status === 'next' ? ' (next)' : '')]), stageKey) + '</select>') +
      radios('outcome', [['found', 'Found something'], ['nothing', 'Nothing found'], ['skipped', 'Not applicable']], outcome) +
      '<div class="fields found-only">' +
      field('Person’s name', input('person', 'autocomplete="off" list="p-people" placeholder="e.g. Jane Smith"')) +
      field('Their role', '<select name="person_role">' + options(ROLES, '') + '</select>') +
      field('Phone', input('phone', 'type="tel" autocomplete="off" inputmode="tel"')) +
      field('Email', input('email', 'type="email" autocomplete="off"')) +
      field('Social profile or link', input('social', 'autocomplete="off" placeholder="URL or @handle"'), 'full') +
      '</div>' +
      field('Notes (optional)', '<textarea name="detail" rows="2" placeholder="What the page showed"></textarea>') +
      '<datalist id="p-people">' + people.map((n) => '<option value="' + esc(n) + '">').join('') + '</datalist>' +
      '<div class="row"><button type="submit" class="btn primary">Save result</button><button type="button" class="btn quiet" data-act="close-form">Cancel</button></div></form>';
  }

  function contactsHtml(d, dr, status) {
    const list = dr.contacts || [];
    if (!list.length) return '<h3 style="margin-top:6px">Contacts</h3><p class="note" style="margin:0">No phone, email or address yet.</p>';
    const order = (c) => (c.needs_validation ? 0 : c.status === 'untested' ? 1 : c.status === 'verified' ? 2 : 3);
    const sorted = list.slice().sort((a, b) => order(a) - order(b) || (Number(a.id) || 0) - (Number(b.id) || 0));
    return '<h3 style="margin-top:6px">Contacts (' + fmt.num(list.length) + ')</h3><div class="p-contacts">' +
      sorted.map((c) => contactRow(dr, c, status)).join('') + '</div>';
  }

  function contactValue(c) {
    const v = String(c.value || '');
    if (c.channel === 'phone' || c.channel === 'text' || c.channel === 'vm') {
      const digits = v.replace(/[^\d+]/g, '');
      if (digits.length >= 7) return '<a href="' + (c.channel === 'text' ? 'sms:' : 'tel:') + esc(digits) + '">' + esc(v) + '</a>';
    }
    if (c.channel === 'email' && /^[^@\s]+@[^@\s]+$/.test(v)) return '<a href="mailto:' + esc(v) + '">' + esc(v) + '</a>';
    if (c.channel === 'social' && safeUrl(v)) return '<a href="' + esc(v) + '" target="_blank" rel="noopener noreferrer">' + esc(v) + '</a>';
    return esc(v);
  }

  function contactRow(dr, c, status) {
    const st = CONTACT_STATUS.find((s) => s[0] === c.status);
    const meta = [st ? st[1] : c.status, c.tries ? 'tried ' + c.tries + '×' : '', c.last_outcome ? 'last: ' + outcomeLabel(dr, c.last_outcome) : '']
      .filter(Boolean).join(' · ');
    const who = [c.person_name, c.source ? 'from ' + sourceName(dr, c.source) : ''].filter(Boolean).join(' · ');
    const ai = c.source === AI_SOURCE;
    const canReach = c.status === 'untested' && status !== 'dnc';
    const open = c.needs_validation && canReach ? true : cur.open === 'reach-' + c.id;
    let ctl = '<div class="ctl">';
    if (canReach && !c.needs_validation) {
      ctl += '<button type="button" class="btn small" data-act="toggle" data-form="reach-' + esc(c.id) + '" aria-expanded="' + open + '">' +
        (open ? 'Close' : 'Log a reach-out') + '</button>';
    }
    if (ai && c.status !== 'wrong' && c.status !== 'dnc') {
      ctl += '<button type="button" class="btn small" data-act="cwrong" data-cid="' + esc(c.id) + '" title="Not this owner\'s, or not real">Wrong</button>';
    }
    ctl += '<select class="select" data-act="cstatus" data-cid="' + esc(c.id) + '" aria-label="Contact status">' + options(CONTACT_STATUS, c.status) + '</select></div>';
    return '<div class="p-contact" data-cid="' + esc(c.id) + '"><div class="chead"><span class="ch ' + (CH_CLASS[c.status] || 'none') + '">' +
      esc(CH_LABEL[c.channel] || c.channel) + '</span><span class="val">' + contactValue(c) + '</span>' +
      (ai ? '<span class="pill" title="Found by AI web research. Not confirmed until you reach out.">AI</span>' : '') +
      (c.needs_validation && canReach ? '<span class="pill hot">Next</span>' : '') + '</div>' +
      (who ? '<div class="meta">' + esc(who) + '</div>' : '') + '<div class="meta">' + esc(meta) + '</div>' +
      (ai ? aiSourceHtml(findingFor(dr, c.id)) : (c.notes ? '<div class="meta">' + esc(c.notes) + '</div>' : '')) +
      ctl + (open ? reachForm(dr, c) : '') + '</div>';
  }

  /* ---------- AI research findings ---------- */

  function researchOf(dr) {
    return dr.research || {runs: [], findings: []};
  }

  function findingFor(dr, cid) {
    return (researchOf(dr).findings || []).find((f) => Number(f.contact_point_id) === Number(cid)) || null;
  }

  function aiSourceHtml(f) {
    if (!f) return '';
    const sure = [f.label, CONFIDENCE[f.confidence] || f.confidence, f.match_reason].filter(Boolean).join(' · ');
    return '<div class="meta">' + (safeUrl(f.source_url) ? extLink(f.source_url, 'Where the AI saw it') + ' ' : '') + esc(sure) + '</div>';
  }

  function findingRow(f) {
    const what = [f.role && f.kind === 'person' ? f.label : '', f.kind !== 'person' && f.label && f.label !== FINDING_KIND[f.kind] ? f.label : '',
      CONFIDENCE[f.confidence] || f.confidence, f.status === 'lead' ? 'unsure lead, not added' : '', f.status === 'wrong' ? 'marked wrong' : '']
      .filter(Boolean).join(' · ');
    const btn = f.status === 'wrong'
      ? '<button type="button" class="btn small quiet" data-act="fwrong" data-fid="' + esc(f.id) + '" data-status="kept">Undo</button>'
      : '<button type="button" class="btn small" data-act="fwrong" data-fid="' + esc(f.id) + '" data-status="wrong">Wrong</button>';
    return '<li><b>' + esc(FINDING_KIND[f.kind] || f.kind) + ':</b> ' + esc(f.value) + (what ? ' <span class="muted">· ' + esc(what) + '</span>' : '') +
      (f.match_reason ? '<div class="meta">' + esc(f.match_reason) + '</div>' : '') +
      '<div class="meta">' + (safeUrl(f.source_url) ? extLink(f.source_url, 'Source') + ' ' : '') + btn + '</div></li>';
  }

  /* What the AI research found for this owner: the filing, people and website (contacts are in the list above),
     each with the page it was seen on, and a Wrong button. Findings from blocked sites are never shown. */
  function researchHtml(dr) {
    const R = researchOf(dr);
    const runs = R.runs || [];
    if (!runs.length) return '';
    const last = runs[0];
    const shown = runs.find((r) => r.outcome !== 'error');
    const mine = shown ? (R.findings || []).filter((f) => Number(f.run_id) === Number(shown.id)) : [];
    const list = mine.filter((f) => !f.contact_point_id && (f.status === 'kept' || f.status === 'lead' || f.status === 'wrong'));
    const dropped = mine.filter((f) => f.status === 'blocked' || f.status === 'rejected').length;
    const head = [RUN_OUTCOME[last.outcome] || last.outcome, last.created_at ? fmt.date(last.created_at) : '',
      last.from_cache ? 'from a saved answer' : ''].filter(Boolean).join(' · ');
    return '<h3 style="margin-top:6px">AI research</h3><p class="note" style="margin:0">' + esc(head) +
      (last.outcome === 'error' && last.error ? '. ' + esc(last.error) : '') + '</p>' +
      (shown && shown.summary ? '<p class="note" style="margin:0">' + esc(shown.summary) + '</p>' : '') +
      (list.length ? '<ul class="p-why">' + list.map(findingRow).join('') + '</ul>' : '') +
      (dropped ? '<p class="note" style="margin:0">' + esc(fmt.plural(dropped, 'finding') + ' dropped (a people-search site, no source link, or not a real contact).') + '</p>' : '') +
      '<p class="note" style="margin:0">Nothing here is confirmed until you reach the owner.</p>';
  }

  function reachForm(dr, c) {
    const opts = dr.outcome_options || [];
    return '<div class="p-form" data-reach="' + esc(c.id) + '"><h3>How did the reach-out go?</h3>' +
      field('Notes (optional)', input('notes', 'autocomplete="off" placeholder="e.g. Spoke with Jane, call back Friday"')) +
      '<div class="p-outcomes">' + opts.map((o) => '<button type="button" class="btn small' +
        (o.effect === 'confirm' ? ' good' : (o.effect === 'mark_wrong' || o.effect === 'dnc') ? ' bad' : '') +
        '" data-act="outreach" data-cid="' + esc(c.id) + '" data-outcome="' + esc(o.key) + '">' + esc(o.label) + '</button>').join('') +
      '</div></div>';
  }

  function outcomeLabel(dr, key) {
    const o = (dr.outcome_options || []).find((x) => x.key === key);
    return o ? o.label : String(key).replace(/_/g, ' ');
  }

  function sourceName(dr, key) {
    const st = stageByKey(dr, key);
    if (st) return st.name;
    return {manual: 'you', you: 'you', conversation: 'a conversation'}[key] || String(key).replace(/_/g, ' ');
  }

  function actionsHtml(d, dr, level, status) {
    const credits = (BM.state.summary && BM.state.summary.credits) || {};
    const pendingCredit = (dr.credits || []).some((cr) => !cr.result);
    const btn = (form, label, primary) => '<button type="button" class="btn small' + (primary ? ' primary' : '') +
      '" data-act="toggle" data-form="' + form + '" aria-expanded="' + (cur.open === form) + '">' + esc(label) + '</button>';
    const open = status === 'active' || status === 'exhausted';
    const parts = [];
    if (status !== 'dnc') parts.push(btn('lookup', 'Record a lookup'));
    if (status !== 'dnc') parts.push(btn('contact', 'Add contact'));
    if (open && level === 1) parts.push(btn('owner_found', 'Owner name found'));
    if (open) parts.push(btn('confirm', 'Confirm owner'));
    if (open && level < 3) parts.push(btn('credit', pendingCredit ? 'Record what Crexi showed' : 'Use a Crexi credit', pendingCredit));
    parts.push(btn('note', 'Add a note'));
    if (status === 'active') parts.push('<button type="button" class="btn small" data-act="level" data-action="exhausted">Mark exhausted</button>');
    if (status === 'exhausted' || status === 'confirmed') parts.push('<button type="button" class="btn small" data-act="level" data-action="reopen">Reopen</button>');
    if (status === 'dnc') parts.push('<button type="button" class="btn small" data-act="level" data-action="undnc">Allow contact again</button>');
    else parts.push('<button type="button" class="btn small danger" data-act="level" data-action="dnc">Do not contact</button>');
    return '<h3 style="margin-top:6px">Actions</h3><div class="p-actions">' + parts.join('') + '</div>' + openFormHtml(d, dr, credits, pendingCredit);
  }

  function openFormHtml(d, dr, credits, pendingCredit) {
    const people = (dr.people || []).map((p) => p.name).filter(Boolean);
    const cancel = '<button type="button" class="btn quiet" data-act="close-form">Cancel</button>';
    const datalist = '<datalist id="p-people-all">' + people.map((n) => '<option value="' + esc(n) + '">').join('') + '</datalist>';
    switch (cur.open) {
      case 'lookup':
        return attemptForm(dr, cur.mode.stage || ((dr.next_step || {}).stage_key) || ((dr.stages || [])[0] || {}).key);
      case 'contact':
        return '<form class="p-form" data-form="contact"><h3>Add a contact</h3><div class="fields">' +
          field('Kind', '<select name="channel">' + options(ADD_CHANNELS, 'phone') + '</select>') +
          field('Phone, email, link or address', input('value', 'required autocomplete="off"')) +
          field('Whose is it? (optional)', input('person_name', 'list="p-people-all" autocomplete="off"')) +
          field('Notes (optional)', input('notes', 'autocomplete="off"')) + '</div>' + datalist +
          '<div class="row"><button type="submit" class="btn primary">Add contact</button>' + cancel + '</div></form>';
      case 'owner_found':
        return '<form class="p-form" data-form="owner_found"><h3>Owner name found</h3>' +
          '<p class="note" style="margin:0">Moves this owner to level 2 (possible owner). Only a conversation confirms them.</p>' +
          field('Owner’s name', input('person', 'required autocomplete="off"')) +
          '<div class="row"><button type="submit" class="btn primary">Save</button>' + cancel + '</div></form>';
      case 'confirm':
        return '<form class="p-form" data-form="confirm"><h3>Confirm the owner</h3>' +
          '<p class="note" style="margin:0">Only after you talked to them and they said they own it. This moves them to level 3 and hands them to the CRM.</p>' +
          field('Who confirmed?', input('person', 'list="p-people-all" autocomplete="off" value="' + esc(dr.best_guess_person || '') + '"')) +
          field('Notes (optional)', '<textarea name="note" rows="2"></textarea>') + datalist +
          '<div class="row"><button type="submit" class="btn primary">Confirm owner</button>' + cancel + '</div></form>';
      case 'credit':
        return creditForm(d, dr, credits, pendingCredit, cancel);
      case 'note':
        return '<form class="p-form" data-form="note"><h3>Add a note</h3>' +
          field('Note', '<textarea name="note" rows="3" required></textarea>') +
          '<div class="row"><button type="submit" class="btn primary">Save note</button>' + cancel + '</div></form>';
      default:
        return '';
    }
  }

  function creditForm(d, dr, credits, pendingCredit, cancel) {
    const result = cur.mode.result || (pendingCredit ? 'found' : 'later');
    const left = has(credits.left) ? fmt.num(credits.left) + ' of ' + fmt.num(credits.per_month) + ' credits left this month.' : '';
    const choices = pendingCredit ? [['found', 'Found contact info'], ['nothing', 'Nothing new']]
      : [['found', 'Found contact info'], ['nothing', 'Nothing new'], ['later', 'Record it later']];
    return '<form class="p-form" data-form="credit" data-mode="' + esc(result) + '"><h3>' +
      (pendingCredit ? 'Record what Crexi showed' : 'Use a Crexi credit') + '</h3>' +
      '<p class="note" style="margin:0">' + (pendingCredit ? 'A credit is already used on this property. ' :
        'Open the Crexi record and use one owner-contact credit there, then record what it showed here. ') + esc(left) + '</p>' +
      (safeUrl(d.crexi_url) ? '<div class="links">' + extLink(d.crexi_url, 'Open in Crexi') + '</div>' : '') +
      radios('result', choices, result) +
      '<div class="fields found-only">' +
      field('Owner or contact name', input('person', 'autocomplete="off"')) +
      field('Phone', input('phone', 'type="tel" inputmode="tel" autocomplete="off"')) +
      field('Email', input('email', 'type="email" autocomplete="off"')) +
      field('Notes (optional)', input('detail', 'autocomplete="off"')) + '</div>' +
      '<div class="row"><button type="submit" class="btn primary">' + (pendingCredit ? 'Save' : 'Record the credit') + '</button>' + cancel + '</div></form>';
  }

  /* ---------- why this score ---------- */

  function whyHtml(d) {
    const sig = Array.isArray(d.signals) ? d.signals : [];
    if (!sig.length) return section('Why this score', '<p class="note" style="margin:0">No score yet. The Score step explains it here.</p>');
    const base = d.score_info && has(d.score_info.base_rate) ? d.score_info.base_rate : null;
    const max = Math.log(2.2);
    const rows = sig.map((f) => {
      const m = Number(f.multiplier);
      if (!has(f.multiplier) || !Number.isFinite(m) || m <= 0) {
        return '<div class="f missing" title="' + esc(f.why || '') + '"><div class="lab"><b>' + esc(f.label || f.key) + '</b><span>' +
          esc(f.value || 'No data') + '</span></div><div class="track"><span class="miss"></span></div><div class="m">skip</div></div>';
      }
      const l = Math.log(m);
      const w = Math.min(50, (Math.abs(l) / max) * 50);
      const bar = w < 0.5 ? '' : '<span class="' + (l >= 0 ? 'up' : 'dn') + '" style="width:' + w.toFixed(1) + '%"></span>';
      return '<div class="f" title="' + esc(f.why || '') + '"><div class="lab"><b>' + esc(f.label || f.key) + '</b><span>' +
        esc(f.value || '') + '</span></div><div class="track">' + bar + '</div><div class="m">×' + m.toFixed(2) + '</div></div>';
    }).join('');
    const reasons = sig.filter((f) => f.why).map((f) => '<li><b>' + esc(f.label || f.key) + ':</b> ' + esc(f.why) + '</li>').join('');
    return section('Why this score',
      '<p class="note" style="margin:0">' + (base !== null ? 'Starts at ' + esc(fmt.pct(base)) + '. ' : '') +
      'Blue bars raise the odds, red bars lower them. Dashed lines are signals with no data, left out.</p>' +
      '<div class="factors">' + rows + '</div>' +
      (reasons ? '<details class="p-more" data-keep="reasons"><summary>Read the reasons</summary><ul class="p-why">' + reasons + '</ul></details>' : ''));
  }

  /* ---------- photo check ---------- */

  function photoList(v) {
    if (Array.isArray(v.photos) && v.photos.length) return v.photos.filter((p) => p && p.url);
    return (v.images || []).map((u) => ({url: u, kind: /sat\.\w+$/.test(String(u)) ? 'satellite' : 'street_view'}));
  }

  function imageHtml(p, i) {
    const url = String(p.url || '');
    if (!url.startsWith('/images/')) return '';
    const sat = p.kind === 'satellite';
    return '<a href="' + esc(url) + '" target="_blank" rel="noopener" class="' + (sat ? 'sat' : '') + '">' +
      '<img src="' + esc(url) + '" alt="' + (sat ? 'Satellite view' : 'Street View photo ' + (i + 1)) + '" loading="lazy" width="640" height="640"' +
      (sat ? ' style="aspect-ratio:2/1;object-fit:cover"' : '') + '></a>';
  }

  function photoHtml(d) {
    const v = d.visual_check || {};
    if (!v.enabled) return '';
    const ac = d.asset_class || {};
    const target = ac.target_word && ac.target_word !== 'Photo target' ? ac.target_word : null;
    const title = target ? target + ' check' : 'Photo check';
    const photos = photoList(v);
    const street = photos.filter((p) => p.kind !== 'satellite');
    const sat = photos.filter((p) => p.kind === 'satellite');
    let body = v.question ? '<p class="note" style="margin:0">' + esc(v.question) + '</p>' : '';
    if (photos.length) {
      body += '<div class="photos">' + street.map(imageHtml).join('') + sat.map(imageHtml).join('') + '</div>';
    } else {
      body += '<p class="note" style="margin:0">' + esc(noPhotoText(v)) + '</p>';
    }
    body += verdictHtml(v, target);
    body += reviewHtml(v, d);
    const ml = v.map_links || {};
    const links = [extLink(ml.street_view, 'Street View'), extLink(ml.satellite, 'Satellite'), extLink(ml.google_maps, 'Google Maps')].filter(Boolean);
    if (links.length) body += '<div class="links">' + links.join('') + '</div>';
    return section(title, body);
  }

  function noPhotoText(v) {
    if (v.status === 'no_imagery') return 'Google has no Street View photo here. Look on Google Maps and answer below.';
    if (v.status === 'error') return 'Getting the photos failed' + (v.error ? ' (' + v.error + ')' : '') + '. The next Photos run tries again.';
    return 'No photos yet. The Photos step on the Run screen takes them (it costs a little).';
  }

  function verdictHtml(v, target) {
    const label = v.reviewed_label || v.ai_label;
    const color = {green: 'g', yellow: 'y', red: 'r'}[v.color] || 'n';
    let text;
    if (v.reviewed_label) text = 'Answered by you' + (v.reviewed_at ? ' on ' + fmt.date(v.reviewed_at) : '') + (v.reviewed_note ? ': ' + v.reviewed_note : '');
    else if (v.ai_label && has(v.final_p)) text = Math.round(Number(v.final_p)) + '% likely ' + (target ? 'a ' + target.toLowerCase() : 'a target') + ' (AI ' + (has(v.ai_p) ? Math.round(Number(v.ai_p)) + '%' : '?') + ', rules ' + (has(v.prior) ? signed(v.prior) : '0') + ')';
    else if (v.ai_label) text = "The AI isn't sure. It needs your look.";
    else if (v.checked_at) text = 'Checked, but there is no clear answer. It needs your look.';
    else text = '';
    const parts = [];
    if (label || text) parts.push('<div class="verdict"><span class="dot ' + color + '"></span>' + (label ? '<b title="' + esc((v.label_help || {})[label] || '') + '">' + esc(labelText(label)) + '</b>' : '') +
      (text ? '<span class="muted">' + esc(text) + '</span>' : '') + '</div>');
    const facts = [];
    if (v.image_quality) facts.push('Photo quality: ' + v.image_quality);
    if (v.property_visible === false) facts.push('The building is not visible in the photos');
    if (v.reasons) parts.push('<p class="note" style="margin:0">' + esc(v.reasons) + '</p>');
    if (facts.length) parts.push('<p class="note" style="margin:0">' + esc(facts.join(' · ')) + '</p>');
    const cues = Array.isArray(v.cues) ? v.cues.filter(Boolean) : [];
    if (cues.length) parts.push('<div class="p-cues">' + cues.map((c) => '<span class="pill">' + esc(typeof c === 'string' ? c : JSON.stringify(c)) + '</span>').join('') + '</div>');
    const rules = (v.prior_rules || []).filter((r) => r && r.applies);
    if (rules.length) parts.push('<p class="note" style="margin:0">Rules of thumb: ' + esc(rules.map((r) => r.text).join('; ')) + '</p>');
    return parts.join('');
  }

  function signed(n) {
    const v = Math.round(Number(n));
    return (v > 0 ? '+' : '') + v;
  }

  function reviewHtml(v, d) {
    const labels = v.labels || [];
    if (!labels.length) return '';
    const buttons = '<div class="choices">' + labels.map((l, i) => '<button type="button" class="choice" data-act="review" data-label="' + esc(l) + '"' +
      ' aria-pressed="' + (v.reviewed_label === l) + '" title="' + esc((v.label_help || {})[l] || '') + '">' + esc(labelText(l)) +
      ' <kbd>' + (i + 1) + '</kbd></button>').join('') + '</div>';
    if (v.needs_review) {
      return '<div class="p-next"><div class="eyebrow">Your answer</div><p class="p-how">The AI is unsure. Your answer replaces its guess, updates the score, and becomes an example it learns from.</p>' + buttons + '</div>';
    }
    return '<details class="p-more" data-keep="review"><summary>' + (v.reviewed_label ? 'Change your answer' : 'Answer it yourself') + '</summary>' + buttons + '</details>';
  }

  /* ---------- rent ---------- */

  function rentHtml(d) {
    const r = d.rent || {};
    if (!r.enabled) return '';
    const rents = r.rents || {};
    const scale = r.scale || {};
    const keys = Array.from(new Set(Object.keys(scale).concat(Object.keys(rents)))).sort((a, b) => Number(a) - Number(b));
    if (!r.fit && !Object.keys(rents).length) {
      return section('Rent', '<p class="note" style="margin:0">No HUD rent yet. The Rent step (free, on the Run screen) fills this in.</p>' +
        (keys.length ? rentTable(keys, rents, scale, r.focus_bedrooms) : ''));
    }
    const fit = RENT_FIT[r.fit];
    let body = '<div class="verdict">' + (fit ? '<span class="dot ' + fit[0] + '"></span><b>' + esc(fit[1]) + '</b>' : '') + '</div>';
    if (r.detail) body += '<p class="note" style="margin:0">' + esc(r.detail) + '</p>';
    body += rentTable(keys, rents, scale, r.focus_bedrooms);
    const src = [r.area_name, r.fiscal_year, RENT_SOURCE[r.source] || r.source].filter(Boolean).join(' · ');
    if (src) body += '<p class="note" style="margin:0">' + esc(src) + '</p>';
    return section('Rent', body);
  }

  function rentTable(keys, rents, scale, focus) {
    const rows = keys.map((k) => {
      const hud = has(rents[k]) ? Number(rents[k]) : null;
      const want = has(scale[k]) ? Number(scale[k]) : null;
      let diff = '—';
      if (hud !== null && want !== null) {
        const delta = hud - want;
        diff = delta === 0 ? 'Same' : (delta > 0 ? '+' : '−') + fmt.money(Math.abs(delta));
      }
      const name = k === '0' ? 'Studio' : k + ' BR';
      const strong = String(focus) === String(k);
      return '<tr' + (strong ? ' style="font-weight:600"' : '') + '><td>' + esc(name) + (strong ? ' <span class="hint">focus</span>' : '') +
        '</td><td class="c num">' + esc(hud === null ? '—' : fmt.money(hud)) + '</td><td class="c num">' + esc(want === null ? '—' : fmt.money(want)) +
        '</td><td class="c num">' + esc(diff) + '</td></tr>';
    }).join('');
    return '<div class="tablewrap"><table class="p-rent"><thead><tr><th>Bedrooms</th><th class="c">HUD area rent</th><th class="c">Target</th><th class="c">Difference</th></tr></thead><tbody>' +
      rows + '</tbody></table></div>';
  }

  /* ---------- building + owner facts ---------- */

  function yesNo(v, yes, no) {
    if (v === 1 || v === true || v === '1') return yes;
    if (v === 0 || v === false || v === '0') return no;
    return null;
  }

  function buildingHtml(d) {
    const p = d.property || {};
    const bb = d.buybox || {};
    const n = (v, suffix) => (has(v) ? fmt.num(v) + (suffix || '') : null);
    const values = [p.improvement_value, p.land_value].some(has)
      ? [has(p.improvement_value) ? 'buildings ' + fmt.money(p.improvement_value) : '', has(p.land_value) ? 'land ' + fmt.money(p.land_value) : ''].filter(Boolean).join(' · ')
      : null;
    const rows = [
      ['Units', n(p.units)],
      ['Building size', n(p.building_sqft, ' sq ft')],
      ['Lot', n(p.lot_acres, ' acres')],
      ['Buildings', n(p.building_count)],
      ['Stories', n(p.stories)],
      ['Year built', has(p.year_built) ? String(p.year_built) : null],
      ['Property type', p.property_type],
      ['Size for the buy box', bb.size_detail ? bb.size_detail + (bb.size_basis_label ? ' (' + bb.size_basis_label.toLowerCase() + ')' : '') : null],
      ['Parcel number', p.apn],
      ['County', p.county ? fmt.title(p.county) + (p.state ? ', ' + p.state : '') : null],
      // value_type is Crexi's 'Total Value Type' (e.g. ASSESSED, MARKET); 'TOTAL VALUE' adds nothing, so skip it
      ['Total value', has(p.total_value) ? fmt.money(p.total_value) + (p.value_type && !/^total( value)?$/i.test(String(p.value_type).trim())
        ? ' (' + fmt.title(String(p.value_type)).toLowerCase() + ')' : '') : null],
      ['Value split', values],
      ['Annual tax', has(p.annual_tax) ? fmt.money(p.annual_tax) + (p.tax_year ? ' (' + p.tax_year + ')' : '') : null],
      ['USPS', yesNo(p.usps_vacant, 'Marked vacant', 'Not marked vacant')],
      ['Pre-foreclosure', yesNo(p.preforeclosure, 'Yes, on record', 'None on record')],
      ['Opportunity zone', yesNo(p.opportunity_zone, 'Yes', null)],
      ['Map location', GEOCODE[p.geocode_quality] || p.geocode_quality],
    ];
    const flags = (d.flags || []).map((f) => FLAG_TEXT[f] || String(f).replace(/_/g, ' '));
    const body = (kv(rows) || '<p class="note" style="margin:0">No building facts in the export.</p>') +
      (flags.length ? '<div class="p-cues">' + flags.map((f) => '<span class="pill warn">' + esc(f) + '</span>').join('') + '</div>' : '');
    return section('Building', body);
  }

  function ownerHtml(d) {
    const o = d.owner || {};
    const years = has(o.years_owned) ? Math.round(Number(o.years_owned)) + ' years' + (o.sale_date ? ' (since ' + fmt.date(o.sale_date) + ')' : '') : null;
    const loan = [DEBT[o.debt_status] || null, o.lender ? 'lender ' + o.lender : '', has(o.loan_amount) ? fmt.money(o.loan_amount) : '',
      o.loan_maturity ? 'due ' + fmt.date(o.loan_maturity) : ''].filter(Boolean).join(' · ');
    const count = Number(o.portfolio_count || 0);
    const mail = o.mailing_text ? o.mailing_text + (o.mailing && o.mailing.care_of ? ' (c/o ' + o.mailing.care_of + ')' : '') : null;
    const rows = [
      ['Owner of record', o.owner_name || d.owner_name || 'Not on record'],
      ['Owner type', o.owner_type_label || (d.owner_type ? labelText(d.owner_type) : null)],
      ['Properties owned', count ? (count === 1 ? 'Just this one' : fmt.num(count) + ' (same name or mailing address)') : null],
      ['Years owned', years],
      ['Last sale price', has(o.sale_price) ? fmt.money(o.sale_price) : null],
      ['Mortgage', loan || null],
      ['Owner lives', ABSENTEE[o.absentee] || null],
      ['Mailing address', mail],
    ];
    let body = kv(rows);
    const sib = Array.isArray(o.siblings) ? o.siblings : [];
    if (sib.length) {
      const items = sib.map((s) => {
        const text = fmt.title(s.address || 'Property ' + s.id) + (s.city ? ', ' + fmt.title(s.city) : '');
        return s.market === BM.state.market
          ? '<li><button type="button" data-open-property="' + esc(s.id) + '">' + esc(text) + '</button></li>'
          : '<li class="muted">' + esc(text) + ' <span class="hint">(' + esc(s.market || 'other market') + ')</span></li>';
      }).join('');
      const more = Number(o.sibling_total || 0) - sib.length;
      body += '<details class="p-more" data-keep="siblings"><summary>Same owner: ' + fmt.plural(o.sibling_total || sib.length, 'other property', 'other properties') +
        '</summary><ul class="p-siblings">' + items + (more > 0 ? '<li class="muted">and ' + fmt.num(more) + ' more</li>' : '') + '</ul></details>';
    }
    return section('Owner', body || '<p class="note" style="margin:0">Owner facts appear after the Owners step runs.</p>');
  }

  /* ---------- links, outcome, raw columns ---------- */

  function linksHtml(d) {
    const seen = new Set();
    const list = [];
    (d.links || []).forEach((l) => {
      const u = safeUrl(l.url);
      if (!u || seen.has(u)) return;
      seen.add(u);
      list.push(extLink(u, l.label || 'Link', l.how || l.finds));
    });
    const sat = d.visual_check && d.visual_check.map_links && d.visual_check.map_links.satellite;
    if (safeUrl(sat) && !seen.has(sat)) list.push(extLink(sat, 'Satellite'));
    if (safeUrl(d.crexi_url) && !seen.has(d.crexi_url) && !(d.links || []).some((l) => l.source_key === 'crexi')) list.unshift(extLink(d.crexi_url, 'Crexi'));
    if (!list.length) return '';
    return section('Links', '<div class="links">' + list.join('') + '</div>');
  }

  function outcomeHtml(d) {
    const o = d.outcome || {};
    const opts = Array.isArray(o.options) && o.options.length ? o.options
      : [{key: 'blue_moon', label: 'Blue Moon deal'}, {key: 'bought', label: 'Bought'}, {key: 'passed', label: 'Passed'}, {key: 'lost', label: 'Lost'}];
    const buttons = opts.map((x) => '<button type="button" data-act="outcome" data-outcome="' + esc(x.key) + '" aria-pressed="' + (o.value === x.key) + '">' +
      esc(x.label) + '</button>').join('') + (o.value ? '<button type="button" data-act="outcome" data-outcome="">Clear</button>' : '');
    return section('How did it end?',
      '<p class="note" style="margin:0">Log what happened with this deal. Outcomes teach the Blue Moon score which signals really matter.</p>' +
      '<div class="steps">' + buttons + '</div>' +
      (o.value ? '<p class="note" style="margin:0">Saved ' + esc(fmt.date(o.created_at)) + (o.note ? ': ' + esc(o.note) : '') + '</p>' : ''));
  }

  function rawHtml(d) {
    const raw = d.raw && typeof d.raw === 'object' ? d.raw : {};
    const keys = Object.keys(raw);
    if (!keys.length) return '';
    const filled = keys.filter((k) => has(raw[k]) && String(raw[k]).trim() !== '');
    const blank = keys.filter((k) => !filled.includes(k));
    const rows = filled.map((k) => '<tr><td>' + esc(k) + '</td><td>' + esc(raw[k]) + '</td></tr>').join('');
    return '<details class="p-more sec" data-keep="raw"><summary>All original columns (' + fmt.num(keys.length) + ')</summary>' +
      '<div class="tablewrap"><table class="p-raw"><tbody>' + rows + '</tbody></table></div>' +
      (blank.length ? '<p class="note">Blank in the export: ' + esc(blank.join(', ')) + '.</p>' : '') + '</details>';
  }

  /* ---------- writes ---------- */

  async function write(path, body, method) {
    const c = cur;
    if (!c || c.busy) return null;
    c.busy = true;
    c.root.classList.add('p-busy');
    try {
      const res = await BM.api(path, {method: method || 'POST', body});
      if (cur === c && res && res.message) BM.toast(res.message);
      return res;
    } catch (err) {
      if (cur === c) BM.toast(err.message, {error: true});
      return null;
    } finally {
      c.busy = false;
      c.root.classList.remove('p-busy');
    }
  }

  function changed(c) {
    const at = c || cur;
    if (!at || !at.detail) return;
    document.dispatchEvent(new CustomEvent('bm:property-changed', {detail: {id: at.id, row: at.detail}}));
    BM.refreshSummary({fresh: true}).catch(() => {});
  }

  /* Merge a DRILL write's answer into the property and re-draw the owner contact section. A write that
     finishes after another property was opened still updates that property's row in the list. */
  function applyDrill(c, res) {
    if (!res || !c.detail) return;
    if (!res.drill) {
      if (cur === c) load(c, {keepScroll: true, quiet: true}).then(() => changed(c));
      return;
    }
    const d = c.detail;
    const dr = res.drill;
    d.drill = dr;
    d.level = dr.level;
    d.drill_status = dr.status;
    d.channels = dr.channels;
    d.best_guess_person = dr.best_guess_person;
    d.next_step_text = dr.next_step ? dr.next_step.text : null;
    d.next_step_kind = dr.next_step ? dr.next_step.kind : null;
    c.open = null;
    c.mode = {};
    if (cur === c) redrawContact(false);
    changed(c);
  }

  async function drillWrite(path, body, method) {
    const c = cur;
    const res = await write(path, body, method);
    if (res) applyDrill(c, res);
    return res;
  }

  async function rescoringWrite(path, body) {
    const c = cur;
    const res = await write(path, body);
    if (!res || cur !== c) return;
    await load(c, {keepScroll: true, quiet: true});
    changed(c);
    if (res.rescore === 'scheduled') {
      setTimeout(() => {   // the score is recomputed a few seconds after the last change
        if (cur === c && !c.busy) load(c, {keepScroll: true, quiet: true}).then(() => changed(c));
      }, 4500);
    }
  }

  function contactsFrom(form) {
    const out = [];
    [['phone', 'phone'], ['email', 'email'], ['social', 'social']].forEach(([name, channel]) => {
      const el = form.elements[name];
      const v = el ? el.value.trim() : '';
      if (v) out.push({channel, value: v});
    });
    return out;
  }

  function val(form, name) {
    const el = form.elements[name];
    if (!el) return null;
    if (el instanceof RadioNodeList) return el.value || null;
    const v = String(el.value || '').trim();
    return v || null;
  }

  async function submitForm(form) {
    const kind = form.getAttribute('data-form');
    const pid = cur.id;
    const base = BM.m('/properties/' + pid);
    if (kind === 'attempt') {
      const outcome = val(form, 'outcome') || 'found';
      const found = outcome === 'found';
      const body = {stage_key: val(form, 'stage_key'), outcome, detail: val(form, 'detail')};
      if (found) {
        body.person = val(form, 'person');
        body.person_role = val(form, 'person_role');
        body.contacts = contactsFrom(form);
      }
      return drillWrite(base + '/attempt', body);
    }
    if (kind === 'contact') {
      return drillWrite(base + '/contacts', {channel: val(form, 'channel'), value: val(form, 'value') || '',
        person_name: val(form, 'person_name'), notes: val(form, 'notes'), source: 'manual'});
    }
    if (kind === 'owner_found') return drillWrite(base + '/level', {action: 'owner_found', person: val(form, 'person')});
    if (kind === 'confirm') return drillWrite(base + '/level', {action: 'confirm', person: val(form, 'person'), note: val(form, 'note')});
    if (kind === 'note') return drillWrite(base + '/level', {action: 'note', note: val(form, 'note')});
    if (kind === 'credit') {
      const result = val(form, 'result');
      const body = {result: result === 'later' ? null : result};
      if (result === 'found') {
        body.person = val(form, 'person');
        body.detail = val(form, 'detail');
        body.contacts = contactsFrom(form);
      }
      return drillWrite(base + '/credit', body);
    }
    return null;
  }

  const CONFIRM_DNC = 'Mark this owner do not contact? Every phone and email on this property also becomes ' +
    'do-not-contact, here and on any other property that has them.';

  async function levelAction(action) {
    const d = cur.detail;
    const status = (d.drill || {}).status;
    if (action === 'dnc' && !window.confirm(CONFIRM_DNC)) return;
    if (action === 'reopen' && status === 'confirmed' &&
      !window.confirm('Reopen this owner? The confirmation is removed and the drill continues.')) return;
    await drillWrite(BM.m('/properties/' + cur.id + '/level'), {action});
  }

  /* ---------- events (bound once on the drawer) ---------- */

  function bindOnce(root) {
    if (root.__bmPropertyBound) return;
    root.__bmPropertyBound = true;
    root.addEventListener('click', onClick);
    root.addEventListener('submit', onSubmit);
    root.addEventListener('change', onChange);
    root.addEventListener('keydown', onKey);
  }

  function onClick(ev) {
    const b = ev.target instanceof Element ? ev.target.closest('[data-act]') : null;
    if (!b || !cur || b.tagName === 'SELECT') return;
    const act = b.getAttribute('data-act');
    if (act === 'reload') { load(cur, {}); return; }
    if (!cur.detail || cur.busy) return;
    if (act === 'toggle') {
      const form = b.getAttribute('data-form');
      cur.open = cur.open === form ? null : form;
      cur.mode = {};
      redrawContact(!!cur.open);
    } else if (act === 'close-form') {
      cur.open = null;
      cur.mode = {};
      redrawContact(false);
    } else if (act === 'attempt') {
      cur.open = 'attempt';
      cur.mode = {outcome: b.getAttribute('data-outcome') || 'found', stage: b.getAttribute('data-stage')};
      redrawContact(true);
    } else if (act === 'outreach') {
      outreach(b);
    } else if (act === 'cwrong') {
      drillWrite('/api/contacts/' + Number(b.getAttribute('data-cid')), {status: 'wrong'}, 'PATCH');
    } else if (act === 'fwrong') {
      drillWrite('/api/research/findings/' + Number(b.getAttribute('data-fid')), {status: b.getAttribute('data-status')}, 'PATCH');
    } else if (act === 'level') {
      levelAction(b.getAttribute('data-action'));
    } else if (act === 'review') {
      rescoringWrite(BM.m('/properties/' + cur.id + '/review'), {label: b.getAttribute('data-label')});
    } else if (act === 'buybox') {
      const decision = b.getAttribute('data-decision');
      if (['include', 'exclude', 'rules'].includes(decision)) rescoringWrite(BM.m('/properties/' + cur.id + '/buybox'), {decision});
    } else if (act === 'bb-review') {
      const key = b.getAttribute('data-bucket');
      if (/^[a-z_]+:[a-z0-9-]+$/.test(key || '')) BM.go('deals', ['review', key]);
    } else if (act === 'outcome') {
      const key = b.getAttribute('data-outcome');
      const current = (cur.detail.outcome || {}).value;
      if (key && key === current) return;
      rescoringWrite(BM.m('/properties/' + cur.id + '/outcome'), {outcome: key || null});
    }
  }

  async function outreach(b) {
    const cid = Number(b.getAttribute('data-cid'));
    const outcome = b.getAttribute('data-outcome');
    const dr = cur.detail.drill || {};
    const contact = (dr.contacts || []).find((c) => Number(c.id) === cid);
    if (!contact) return;
    const opt = (dr.outcome_options || []).find((o) => o.key === outcome) || {};
    if (opt.effect === 'dnc' && !window.confirm('They asked not to be contacted? This owner and every contact on this property become do-not-contact.')) return;
    const box = b.closest('[data-reach]');
    const notesEl = box ? box.querySelector('input[name="notes"]') : null;
    const notes = notesEl && notesEl.value.trim() ? notesEl.value.trim() : null;
    await drillWrite(BM.m('/properties/' + cur.id + '/outreach'),
      {channel: contact.channel, outcome, contact_point_id: cid, notes});
  }

  function onSubmit(ev) {
    const form = ev.target.closest('form[data-form]');
    if (!form || !cur || !cur.detail) return;
    ev.preventDefault();
    if (cur.busy) return;
    if (!form.reportValidity()) return;
    submitForm(form);
  }

  async function onChange(ev) {
    const t = ev.target;
    if (!cur || !cur.detail) return;
    if (t.matches('input[type="radio"]') && (t.name === 'outcome' || t.name === 'result')) {
      const form = t.closest('form');
      if (form) form.setAttribute('data-mode', t.value);
      cur.mode[t.name] = t.value;
      return;
    }
    if (t.matches('select[name="stage_key"]')) {
      cur.mode.stage = t.value;
      return;
    }
    if (t.matches('select[data-act="cstatus"]')) {
      const cid = Number(t.getAttribute('data-cid'));
      const contact = ((cur.detail.drill || {}).contacts || []).find((c) => Number(c.id) === cid);
      if (t.value === 'dnc' && !window.confirm('Mark this contact do not contact? It also becomes do-not-contact on every other property that has it.')) {
        t.value = contact ? contact.status : 'untested';
        return;
      }
      const res = await drillWrite('/api/contacts/' + cid, {status: t.value}, 'PATCH');
      if (!res && contact) t.value = contact.status;
    }
  }

  function onKey(ev) {
    if (ev.key !== 'Escape' || !cur || !cur.open) return;
    if (!(ev.target instanceof Element) || !ev.target.closest('.p-form')) return;
    ev.stopPropagation();   // Esc inside a form closes the form, not the drawer
    cur.open = null;
    cur.mode = {};
    redrawContact(false);
  }
})();
