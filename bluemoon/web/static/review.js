/* Photo review: answer the photo check the AI was unsure about, one property at a time, with number keys.
 * Answers replace the AI's guess, update the Blue Moon score and become examples the AI reads next time.
 *
 *   #review             the review queue
 *   #review/scorecard   how often the AI agreed with you, and the acceptance check
 *
 * Uses BM.work helpers from drill.js.
 */
(() => {
  'use strict';

  const BM = window.BM;
  const W = BM && BM.work;
  if (!BM || !W) {
    console.error('review.js needs core.js and drill.js to load first.');
    return;
  }

  const esc = (v) => BM.esc(v == null ? '' : String(v));
  const num = (n) => BM.fmt.num(Number(n) || 0);
  const PAGE = 20;
  const QUALITY = { good: 'Good photos', partial: 'Partly useful photos', poor: 'Poor photos', none: 'No usable photos' };
  const COLOR_DOT = { green: 'g', yellow: 'y', red: 'r', na: 'n' };
  const COLOR_TEXT = { green: 'Green', yellow: 'Yellow', red: 'Red', na: 'No verdict' };

  const S = {
    root: null,
    market: null,
    seq: 0,
    mode: 'queue',   // queue | scorecard
    q: null,         // {items, total, counts, idx, done, busy, loadingMore: in-flight page request}
  };

  const newQueue = () => ({ items: [], total: 0, counts: {}, idx: 0, done: 0, busy: false, loadingMore: null });

  const asset = () => (BM.state.summary && BM.state.summary.asset_class) || {};

  // ---------------------------------------------------------------------------------------------

  async function render(root, params = []) {
    S.root = root;
    if (S.market !== BM.state.market) {
      S.market = BM.state.market;
      S.q = null;
    }
    const ac = asset();
    if (ac.visual_enabled === false) {
      S.mode = 'off';
      paint(noPhotoCheck(ac));
      return;
    }
    if (params[0] === 'scorecard') return renderScorecard();
    return renderQueue();
  }

  function paint(html) {
    if (!S.root) return null;
    S.root.innerHTML = `<div class="dr dr-review">${html}</div>`;
    const el = S.root.firstElementChild;
    el.addEventListener('click', onClick);
    return el;
  }

  /** 'Is it a walk-up? How do residents reach their front doors?' -> title + the rest as a line. */
  function splitQuestion(q) {
    const text = String(q || '').trim();
    const m = /^(.+?\?)\s+(.+)$/.exec(text);
    return m ? [m[1], m[2]] : [text || 'Photo review', ''];
  }

  function head(tab) {
    const ac = asset();
    const [title, rest] = splitQuestion(ac.visual_question);
    const tabs = `
      <div class="seg dr-tabs" role="group" aria-label="Photo review">
        <button data-go="review" aria-pressed="${tab === 'queue'}">Review</button>
        <button data-go="review" data-params="scorecard" aria-pressed="${tab === 'scorecard'}">Scorecard</button>
      </div>`;
    const lines = tab === 'scorecard'
      ? '<li>How often the AI agreed with you on the properties you reviewed.</li><li>Goal: greens are 90% truly a target, reds 90% truly not.</li>'
      : `${rest ? `<li>${esc(rest)}</li>` : ''}<li>The AI is unsure about these. Your answer replaces its guess and updates the Blue Moon score.</li>
         <li>Each answer becomes an example the AI reads next time. Use the number keys to move fast.</li>`;
    return `
      <div class="dr-row-between"><div class="eyebrow">Photo review</div>${tabs}</div>
      <h1>${esc(tab === 'scorecard' ? 'How well the AI sees' : title)}</h1>
      <ul class="lines">${lines}</ul>`;
  }

  function noPhotoCheck(ac) {
    return `<div class="eyebrow">Photo review</div><h1>No photo check here</h1>${BM.ui.empty(
      `${ac.label || 'This asset class'} has no photo check`,
      'Properties in this market are ranked without photos, so there is nothing to review. Switch markets to review photos for another asset class.')}`;
  }

  // ---------------------------------------------------------------------------------------------
  // The queue
  // ---------------------------------------------------------------------------------------------

  async function renderQueue() {
    const seq = ++S.seq;
    S.mode = 'queue';
    if (!S.q) S.q = newQueue();
    const q = S.q;
    if (!q.items.length) paint(head('queue') + W.loading('Loading photos…'));
    try {
      const data = await fetchQueue(Math.max(PAGE, q.items.length), 0);
      if (seq !== S.seq) return;
      applyQueue(q, data);
      paintQueue();
    } catch (err) {
      if (seq !== S.seq) return;
      paint(head('queue') + W.errorBox(err));
    }
  }

  const fetchQueue = (limit, offset) => BM.api(W.mpath(`/review/queue?limit=${Math.min(200, limit)}&offset=${offset}`));

  function applyQueue(q, data) {
    const current = q.items[q.idx] && q.items[q.idx].id;
    q.items = (data && data.items) || [];
    q.total = Math.max(Number(data && data.total) || 0, q.items.length);
    q.counts = (data && data.counts) || {};
    const at = q.items.findIndex((it) => it.id === current);
    q.idx = at >= 0 ? at : Math.min(q.idx, q.items.length);
  }

  /** Load the next page. Answered cards leave the server's queue, so the next page starts after what is
   *  loaded here. One request at a time: callers share the one in flight. */
  function loadMore(q) {
    if (q.items.length >= q.total) return Promise.resolve();
    if (!q.loadingMore) {
      q.loadingMore = fetchQueue(PAGE, q.items.length)
        .then((data) => {
          const seen = new Set(q.items.map((it) => it.id));
          ((data && data.items) || []).forEach((it) => { if (!seen.has(it.id)) q.items.push(it); });
          q.total = Math.max(Number(data && data.total) || 0, q.items.length);
        })
        .catch(() => { /* tried again on the next answer */ })
        .finally(() => { q.loadingMore = null; });
    }
    return q.loadingMore;
  }

  /** Make sure the card at position `want` is loaded when the server has more (waits for a prefetch). */
  async function fillTo(q, want) {
    let misses = 0;
    while (want >= q.items.length && q.items.length < q.total && misses < 2) {
      const had = q.items.length;
      await loadMore(q);
      misses = q.items.length > had ? 0 : misses + 1;
    }
  }

  function paintQueue() {
    const q = S.q;
    const it = q.items[q.idx];
    const el = paint(head('queue') + (it ? reviewCard(q, it) : queueEmpty(q)));
    preload(q.items[q.idx + 1]);
    if (q.items.length - q.idx < 5) loadMore(q);
    return el;
  }

  /** Warm the browser cache with the next card's photos so the next answer feels instant. */
  function preload(it) {
    (it && it.images ? it.images : []).forEach((im) => { if (im.url) new Image().src = im.url; });
  }

  function queueEmpty(q) {
    if (q.total > 0) {
      return BM.ui.empty('End of the list', `${num(q.total)} you skipped ${q.total === 1 ? 'is' : 'are'} still waiting.`,
        '<button class="btn primary" data-act="restart">Back to the first one</button>');
    }
    if (BM.state.summary && BM.state.summary.properties === 0) {
      return W.emptyMarket('Then fetch photos and run the photo check from the Run screen.');
    }
    const reviewed = Number(q.counts.reviewed) || 0;
    if (reviewed || q.done) {
      return BM.ui.empty('All caught up',
        'Every unsure property has an answer. Your answers are now examples the AI reads before its next guesses.',
        '<button class="btn" data-go="review" data-params="scorecard">See the scorecard</button>');
    }
    return BM.ui.empty('No photos to review yet',
      'Photos come from Google Street View. Add your Google key to .env, then run Photos and then Photo check from the Run screen. Anything the AI is unsure about lands here.',
      '<button class="btn primary" data-go="run">Open the Run screen</button>');
  }

  function sortedImages(images) {
    const list = (images || []).filter((im) => im && im.url);
    const sv = list.filter((im) => im.kind !== 'satellite').sort((a, b) => String(a.name).localeCompare(String(b.name)));
    return [...sv, ...list.filter((im) => im.kind === 'satellite')];
  }

  function imageTile(im, i, cls, alt) {
    return `<a class="dr-img ${cls}" href="${esc(im.url)}" target="_blank" rel="noopener" title="Open full size">
      <img src="${esc(im.url)}" alt="${esc(alt)}" loading="${i < 2 ? 'eager' : 'lazy'}" decoding="async"></a>`;
  }

  function imagesHtml(it) {
    const imgs = sortedImages(it.images);
    if (!imgs.length) {
      return `<div class="dr-imgs"><div class="dr-noimg"><b>No photos saved for this property.</b>
        <span class="note">Use the Street View and satellite links to look at it.</span></div></div>`;
    }
    const hasStreet = imgs.some((im) => im.kind !== 'satellite');
    let svCount = 0;
    const tiles = imgs.map((im, i) => {
      if (im.kind === 'satellite') return imageTile(im, i, hasStreet ? 'sat' : 'main', 'Satellite view from above');
      svCount += 1;
      return imageTile(im, i, svCount === 1 ? 'main' : '', `Street View photo ${svCount}`);
    }).join('');
    return `<div class="dr-imgs">${tiles}</div>`;
  }

  function aiHtml(it) {
    const word = (asset().target_word || 'target').toLowerCase();
    const conf = it.ai_p == null
      ? (it.ai_label ? 'No usable read' : 'The AI has not looked yet')
      : `${Math.round(Number(it.ai_p))}% likely ${esc(word)}`;
    const quality = [QUALITY[it.image_quality] || '', it.property_visible === false ? 'building not visible' : '']
      .filter(Boolean).join(' · ');
    return `
      <dl class="kv">
        <dt>AI guess</dt><dd>${it.ai_label ? `<b>${esc(W.labelText(it.ai_label))}</b> <span class="dr-mono note">${esc(it.ai_label)}</span>` : '<span class="muted">None yet</span>'}</dd>
        <dt>Confidence</dt><dd><span class="dot ${COLOR_DOT[it.color] || 'n'}"></span>${conf}</dd>
        ${quality ? `<dt>Photos</dt><dd>${esc(quality)}${it.pano_date ? ` · taken ${esc(it.pano_date)}` : ''}</dd>` : ''}
        <dt>Blue Moon now</dt><dd>${W.moonScore(it.score)}${it.rank ? ` <span class="note">rank #${num(it.rank)}</span>` : ''}</dd>
      </dl>
      ${it.reasons ? `<p class="dr-reasons">${esc(it.reasons)}</p>` : ''}
      ${(it.cues || []).length ? `<div class="dr-tags">${it.cues.map((c) => `<span class="dr-tag">${esc(c)}</span>`).join('')}</div>` : ''}`;
  }

  function choicesHtml(it) {
    return `<div class="dr-choices" role="group" aria-label="Your answer">${(it.choices || []).map((c) => `
      <button class="dr-choice${c.target ? ' target' : ''}" data-act="answer" data-label="${esc(c.label)}" title="${esc(c.label)}">
        <span class="dr-choice-t"><b>${esc(W.labelText(c.label))}</b>${c.help ? `<span class="note">${esc(c.help)}</span>` : ''}</span>
        <kbd>${esc(c.key)}</kbd>
      </button>`).join('')}</div>`;
  }

  function linksHtml(it) {
    const l = it.links || {};
    const links = [
      [l.street_view, 'Street View'], [l.google_maps, 'Google Maps'], [l.satellite, 'Satellite'], [it.crexi_url, 'Crexi'],
    ].map(([u, t]) => W.extLink(u, `${esc(t)} ↗`, 'chip dr-chiplink')).join('');
    return `<div class="dr-plinks">${links}<button class="chip" data-act="open-property">All details</button></div>`;
  }

  function reviewCard(q, it) {
    const left = Number(q.total) || 0;
    const all = left + q.done;
    const w = all ? Math.round((q.done / all) * 100) : 0;
    const meta = [W.place(it), it.units ? `${num(it.units)} units` : '', it.stories ? `${it.stories} stories` : '',
      it.year_built ? `built ${it.year_built}` : ''].filter(Boolean).join(' · ');
    const keys = (it.choices || []).length;
    return `
      <div class="dr-rev">
        ${imagesHtml(it)}
        <section class="panel dr-revpanel" aria-label="Your answer">
          <div class="dr-prog">
            <div class="dr-progress" role="progressbar" aria-label="Reviewed this session" aria-valuemin="0" aria-valuemax="${all}" aria-valuenow="${q.done}"><i style="width:${w}%"></i></div>
            <div class="note num">${num(left)} left to review${q.done ? ` · ${num(q.done)} done this session` : ''}</div>
          </div>
          <div>
            <div class="eyebrow">${esc(meta)}</div>
            <h2 class="dr-addr">${esc(W.address(it))}</h2>
            <p class="note dr-tight">${esc(it.why || '')}</p>
          </div>
          ${aiHtml(it)}
          <h3 class="dr-q">${esc(it.question || asset().visual_question || 'What do you see?')}</h3>
          ${choicesHtml(it)}
          <label class="dr-field dr-note"><span>Note (optional): why, in a few words</span>
            <input data-f="note" autocomplete="off" placeholder="e.g. stairs and breezeway on the side"></label>
          ${linksHtml(it)}
          <div class="dr-nav">
            <button class="btn" data-act="prev" ${q.idx <= 0 ? 'disabled' : ''}>← Back <kbd>K</kbd></button>
            <button class="btn" data-act="skip">Skip for now <kbd>S</kbd></button>
          </div>
          <p class="note dr-keys">Keys: <kbd>1</kbd>–<kbd>${keys}</kbd> answer · <kbd>S</kbd> skip · <kbd>K</kbd> back</p>
        </section>
      </div>`;
  }

  async function answer(label) {
    const q = S.q;
    const it = q && q.items[q.idx];
    if (!it || q.busy || !label) return;
    const note = ((S.root.querySelector('[data-f="note"]') || {}).value || '').trim();
    q.busy = true;
    const btns = [...S.root.querySelectorAll('.dr-choice, .dr-nav button')];
    btns.forEach((b) => { b.disabled = true; });
    try {
      const res = await BM.api(W.mpath(`/properties/${it.id}/review`), { method: 'POST', body: { label, note: note || null } });
      const agreed = res && res.agreed_with_ai;
      const extra = agreed === true ? ' The AI agreed.' : agreed === false ? ' The AI guessed differently; your answer wins.' : '';
      BM.toast(`Saved: ${W.labelText((res && res.label) || label)}.${extra}`);
      q.done += 1;
      q.items.splice(q.idx, 1);
      q.total = Math.max(0, q.total - 1);
      await fillTo(q, q.idx);
      if (S.q !== q || S.mode !== 'queue') return;
      W.scrollToTop(paintQueue() && S.root.querySelector('.dr-rev'));
      W.refreshSoon();
    } catch (err) {
      W.toastError(err);
      btns.forEach((b) => { if (b.isConnected) b.disabled = false; });
    } finally {
      q.busy = false;
    }
  }

  async function step(delta) {
    const q = S.q;
    if (!q || q.busy) return;
    const target = q.idx + delta;
    if (target < 0) return;
    await fillTo(q, target);
    if (S.q !== q) return;
    q.idx = Math.min(target, q.items.length);
    paintQueue();
  }

  // ---------------------------------------------------------------------------------------------
  // The scorecard
  // ---------------------------------------------------------------------------------------------

  async function renderScorecard() {
    const seq = ++S.seq;
    S.mode = 'scorecard';
    paint(head('scorecard') + W.loading('Loading the scorecard…'));
    try {
      const card = await BM.api(W.mpath('/review/scorecard'));
      if (seq !== S.seq) return;
      paint(head('scorecard') + scorecardHtml(card || {}));
    } catch (err) {
      if (seq !== S.seq) return;
      paint(head('scorecard') + W.errorBox(err));
    }
  }

  const pctText = (p) => (p == null ? '—' : `${Math.round(Number(p))}%`);

  function passPill(passed) {
    if (passed === true) return '<span class="dr-pass good">Passed</span>';
    if (passed === false) return '<span class="dr-pass bad">Not yet</span>';
    return '<span class="dr-pass na">Not enough reviews yet</span>';
  }

  function acceptanceHtml(acc) {
    const line = (color, a) => `
      <div class="dr-acc-row">
        <span class="dot ${COLOR_DOT[color]}"></span>
        <div class="dr-acc-text"><b>${COLOR_TEXT[color]}s</b> <span class="note">${esc(((a && a.text) || 'No reviews yet.').replace(/^(green|red):\s*/i, ''))}</span></div>
        ${passPill(a ? a.passed : null)}
      </div>`;
    return `
      <section class="panel" aria-label="Acceptance check">
        <div class="dr-row-between"><h2>Acceptance check</h2>${passPill(acc.passed)}</div>
        <p class="note dr-tight">Green should be at least 90% truly a target. Red should be at least 90% truly not.</p>
        ${line('green', acc.green)}${line('red', acc.red)}
      </section>`;
  }

  function confusionHtml(card) {
    const conf = card.confusion || {};
    const labels = conf.labels || card.labels || [];
    const matrix = conf.matrix || labels.map((t) => labels.map((a) => ((conf.rows || {})[t] || {})[a] || 0));
    if (!labels.length) return '';
    const headCells = labels.map((l) => `<th class="c" title="${esc(l)}">${esc(W.labelText(l))}</th>`).join('');
    const body = labels.map((t, i) => {
      const cells = labels.map((a, j) => {
        const n = Number((matrix[i] || [])[j]) || 0;
        const cls = n ? (i === j ? 'hit' : 'miss') : `zero${i === j ? ' diag' : ''}`;
        return `<td class="num c dr-cm ${cls}">${num(n)}</td>`;
      }).join('');
      return `<tr><th scope="row" title="${esc(t)}">${esc(W.labelText(t))}</th>${cells}</tr>`;
    }).join('');
    return `
      <section class="panel" aria-label="Confusion matrix">
        <h2>Your answer vs the AI's guess</h2>
        <p class="note dr-tight">Rows are your answers, columns are what the AI said. The diagonal is where you agreed.</p>
        <div class="dr-scroll">
          <table class="wt dr-cm-table">
            <thead><tr><th>You said ↓ · AI said →</th>${headCells}</tr></thead>
            <tbody>${body}</tbody>
          </table>
        </div>
      </section>`;
  }

  function byColorHtml(card) {
    const rows = (card.by_color || []).filter((b) => b.n).map((b) => `
      <tr>
        <td><span class="dot ${COLOR_DOT[b.color] || 'n'}"></span>${esc(COLOR_TEXT[b.color] || b.color)}</td>
        <td class="num c">${num(b.n)}</td>
        <td class="num c">${num(b.target)}</td>
        <td class="num c">${num(b.not_target)}</td>
        <td class="num c">${num(b.unknown)}</td>
        <td class="num c"><b>${b.accuracy == null ? '—' : pctText(b.accuracy)}</b></td>
      </tr>`).join('');
    if (!rows) return '';
    return `
      <section class="panel" aria-label="Accuracy per color">
        <h2>Accuracy per color</h2>
        <p class="note dr-tight">The color the AI gave, and what the property really was. Yellow is "unsure", so it has no accuracy.</p>
        <div class="dr-scroll">
          <table class="wt dr-color-table">
            <thead><tr><th>AI color</th><th class="c">Reviewed</th><th class="c">Truly target</th><th class="c">Truly not</th><th class="c">Couldn't tell</th><th class="c">Right</th></tr></thead>
            <tbody>${rows}</tbody>
          </table>
        </div>
      </section>`;
  }

  function scorecardHtml(card) {
    if (!card.reviewed) {
      return BM.ui.empty('No reviews yet',
        'Answer a few photo cards and this fills in: how often the AI agreed with you, and whether its greens and reds pass the 90% check.',
        '<button class="btn primary" data-go="review">Start reviewing</button>');
    }
    const tiles = [
      [num(card.reviewed), 'Properties you reviewed'],
      [num(card.with_ai), 'Compared with the AI'],
      [pctText(card.label_accuracy), 'Same label as you'],
      [pctText(card.target_accuracy), 'Right on target vs not'],
    ];
    return `
      <div class="strip dr-strip">${tiles.map(([v, l]) => BM.ui.stat(v, l)).join('')}</div>
      ${card.message ? `<p class="note dr-banner">${esc(card.message)}</p>` : ''}
      ${acceptanceHtml(card.acceptance || {})}
      <div class="dr-stack">${confusionHtml(card)}${byColorHtml(card)}</div>
      <p class="note">Run <span class="dr-mono">uv run run.py calibrate</span> to re-check everything you reviewed with the current examples.</p>`;
  }

  // ---------------------------------------------------------------------------------------------
  // Clicks and keys
  // ---------------------------------------------------------------------------------------------

  function onClick(e) {
    const b = e.target.closest('[data-act]');
    if (!b || !S.root || !S.root.contains(b) || b.disabled) return;
    const act = b.dataset.act;
    const handlers = {
      retry: () => render(S.root, W.route().params),
      answer: () => answer(b.dataset.label),
      skip: () => step(1),
      prev: () => step(-1),
      restart: () => { S.q.idx = 0; paintQueue(); },
      'open-property': () => { const it = S.q && S.q.items[S.q.idx]; if (it) BM.openProperty(it.id); },
    };
    if (!handlers[act]) return;
    e.preventDefault();
    handlers[act]();
  }

  document.addEventListener('keydown', (e) => {
    if (S.mode !== 'queue' || !S.q || !S.root || !S.root.isConnected || W.route().key !== 'review') return;
    if (W.keysBlocked(e)) return;
    const it = S.q.items[S.q.idx];
    if (!it) return;
    const k = e.key.toLowerCase();
    const choice = (it.choices || []).find((c) => String(c.key) === e.key);
    let act = null;
    if (choice) act = () => answer(choice.label);
    else if (k === 's' || k === 'j') act = () => step(1);
    else if (k === 'k') act = () => step(-1);
    if (!act) return;
    e.preventDefault();
    act();
  });

  // ---------------------------------------------------------------------------------------------

  BM.registerView('review', {
    label: 'Photo review',
    group: 'work',
    order: 4,
    badge: (s) => {
      if (!s || (s.asset_class && s.asset_class.visual_enabled === false)) return null;
      return Number(s.needs_review) ? W.compact(s.needs_review) : null;
    },
    render,
  });
})();
