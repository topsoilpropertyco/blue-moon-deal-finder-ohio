/* Settings (Setup): a read-only view of the settings that matter, and how to change them.
 * Markets, the current asset class's buy box, which keys are set (yes/no only, never the values), the
 * photo check setup, Google's free calls used this month, where the data lives, and copy-paste commands
 * for editing config.yaml and restarting the app.
 */
(() => {
  'use strict';

  const BM = window.BM;
  const H = BM && BM.iv;
  if (!BM || !H) {
    console.error('settings.js needs core.js and funnel.js to load first.');
    return;
  }
  const {esc, toNum, num} = H;

  const APP_FOLDER = '~/blue-moon-deal-finder-ohio';
  const SERVICE = 'com.bluemoon.dealfinder';
  const KEYS = [
    ['google', 'GOOGLE_MAPS_API_KEY', 'Google Maps', 'Drive times, map locations, Street View and satellite photos.'],
    ['anthropic', 'ANTHROPIC_API_KEY', 'Anthropic API (optional)', 'Only if the photo check is switched to the pay-per-use API.'],
    ['hud', 'HUD_API_TOKEN', 'HUD USER token (optional)', 'Not needed: rents come from HUD\'s free map service.'],
    ['census', 'CENSUS_API_KEY', 'Census key (optional)', 'Extra rent context from the Census.'],
  ];
  const SKU_LABELS = {
    route_matrix_element: 'Google Routes (drive times)',
    geocoding: 'Google Geocoding (map locations)',
    street_view_static: 'Street View photos',
    static_maps: 'Satellite photos',
    street_view_metadata: 'Street View lookups',
    county_gis: 'County records (free county websites)',
  };
  const BACKENDS = {
    claude_cli: 'Your Claude plan, through Claude Code on the Mac Mini (no per-call bill)',
    anthropic_api: 'The pay-per-use Anthropic API (needs ANTHROPIC_API_KEY)',
    mock: 'Test mode (no real photo check)',
  };
  const PATHS = [
    ['db', 'Database (the master list)'], ['crexi_dir', 'Imported Crexi files'], ['hud_dir', 'HUD rent spreadsheets'],
    ['images', 'Street View and satellite photos'], ['labels', 'Your photo answers and the AI\'s examples'],
    ['output', 'Exported files'],
  ];

  /* ---------- small builders ---------- */

  const yesNo = (on) => '<span class="se-yn ' + (on ? 'on' : 'off') + '"><span class="dot ' + (on ? 'g' : 'n') + '" aria-hidden="true"></span>' +
    (on ? 'Yes' : 'No') + '</span>';

  function panel(title, inner, cls) {
    return '<div class="panel' + (cls ? ' ' + cls : '') + '"><h2>' + esc(title) + '</h2>' + inner + '</div>';
  }

  /** A panel that starts folded: its title is the button. For read-only detail nobody needs every day. */
  function fold(title, hint, inner, cls) {
    return '<details class="panel se-fold' + (cls ? ' ' + cls : '') + '"><summary><h2>' + esc(title) + '</h2>' +
      (hint ? '<span class="note">' + esc(hint) + '</span>' : '') + '</summary><div class="se-fold-body">' + inner + '</div></details>';
  }

  function kv(pairs) {
    return '<dl class="kv">' + pairs.filter(Boolean).map(([k, v]) => '<dt>' + esc(k) + '</dt><dd>' + v + '</dd>').join('') + '</dl>';
  }

  function range(a, unit) {
    return Array.isArray(a) && a.length === 2 ? esc(num(a[0]) + '–' + num(a[1]) + (unit ? ' ' + unit : '')) : '—';
  }

  /* ---------- sections ---------- */

  function marketsPanel(cfg, acs) {
    const counts = new Map((BM.state.markets || []).map((m) => [m.slug, m]));
    const rows = (cfg.markets || []).map((m) => {
      const ac = acs[m.asset_class] || {};
      const o = m.origin || {};
      const here = m.slug === BM.state.market;
      return '<tr' + (here ? ' class="sel"' : '') + '><td><b>' + esc(ac.label || m.asset_class) + '</b>' +
        (here ? ' <span class="pill hot">Showing</span>' : '') + '<div class="note">' + esc(m.slug) + '</div></td>' +
        '<td>' + esc(m.geography || '') + '</td><td>' + esc(o.label || '—') + '</td>' +
        '<td class="c num">' + (toNum(m.max_drive_minutes) ? num(m.max_drive_minutes) + ' min' : '—') + '</td>' +
        '<td class="c num">' + num((counts.get(m.slug) || {}).properties || 0) + '</td></tr>';
    }).join('');
    return panel('Markets', '<p class="note vw-tight">One market = one asset class + one geography. Add more in config.yaml under markets.</p>' +
      '<div class="vw-scroll"><table class="wt se-markets"><thead><tr><th>Asset class</th><th>Geography</th><th>Drive time from</th>' +
      '<th class="c">Max drive</th><th class="c">Properties</th></tr></thead><tbody>' + rows + '</tbody></table></div>', 'vw-full');
  }

  function rentScale(scale) {
    const keys = Object.keys(scale || {});
    if (!keys.length) return '—';
    return keys.sort().map((k) => esc((k === '0' ? 'Studio' : k + ' BR') + ' $' + num(scale[k]))).join(' · ');
  }

  function buyBoxPanel(ac) {
    if (!ac) return '';
    const size = ac.size || {};
    const metric = {units: 'units', building_sqft: 'sq ft', lot_acres: 'acres'}[size.metric] || size.metric || '';
    const rent = ac.rent || {};
    const vc = ac.visual_check || {};
    const fallback = size.fallback && size.sqft_per_unit ? 'Unknown units: building sq ft ÷ ' + num(size.sqft_per_unit) + ' (an estimate that never rules a property out)' : '';
    return panel('Buy box: ' + (ac.label || ac.key || ''), kv([
      ['Size measured by', esc(metric)],
      ['Sweet spot (green)', range(size.ideal, metric)],
      ['OK (yellow)', range(size.ok, metric)],
      fallback ? ['Missing size', esc(fallback)] : null,
      ['Built', ac.year_built_min ? esc(ac.year_built_min + ' or later') : 'Any year'],
      ['Rent scale', rent.enabled ? rentScale(rent.scale) : 'Not used'],
      ['Photo check', vc.enabled ? esc(vc.question || 'On') : 'Not used'],
      vc.enabled && vc.labels ? ['Photo labels', (vc.labels || []).map((l) => '<code class="se-code">' + esc(l) + '</code>').join(' ')] : null,
    ]) + '<p class="note vw-tight">From config/asset_classes.yaml.</p>');
  }

  function keysPanel(keys) {
    const rows = KEYS.map(([k, env, name, what]) => '<li class="se-key"><div><b>' + esc(name) + '</b> <code class="se-code">' + esc(env) + '</code>' +
      '<div class="note">' + esc(what) + '</div></div>' + yesNo(!!(keys || {})[k]) + '</li>').join('');
    return panel('Keys', '<p class="note vw-tight">Keys live only in the .env file on the Mac Mini. This screen shows whether each one is set, never its value.</p>' +
      '<ul class="se-keys">' + rows + '</ul>' +
      '<details class="im-more"><summary>How to add a key</summary>' +
      '<p class="note">1. Copy the key. For Google: console.cloud.google.com, APIs &amp; Services, Credentials. Turn on these APIs for the project: Routes API, Street View Static API, Maps Static API and Geocoding API.</p>' +
      '<p class="note">2. On the Mac Mini, run this and paste the key when it asks (nothing shows as you paste; that is normal):</p>' +
      H.command('cd ' + APP_FOLDER + ' && uv run run.py key google') +
      '<p class="note">For the others: <code class="se-code">key anthropic</code> or <code class="se-code">key census</code> instead of <code class="se-code">key google</code>.</p>' +
      '<p class="note">3. Restart the dashboard:</p>' + H.command('launchctl kickstart -k gui/$(id -u)/' + SERVICE) +
      '<p class="note">Never paste a key into a chat, an email or GitHub.</p></details>');
  }

  function photoPanel(cfg, health) {
    const v = cfg.vision || {};
    const backend = (health && health.vision_backend) || v.backend || '';
    const cli = (v.claude_cli || {});
    const found = health ? !!health.claude_cli : null;
    return fold('Photo check', 'What runs the photo check', kv([
      ['Runs on', esc(BACKENDS[backend] || backend || 'Unknown')],
      backend === 'claude_cli' ? ['Claude Code found', found === null ? 'Unknown' : yesNo(found)] : null,
      backend === 'claude_cli' && cli.model ? ['Model', esc(cli.model)] : null,
      backend === 'anthropic_api' && v.anthropic_api ? ['Model', esc(v.anthropic_api.model || '')] : null,
      ['Photo vs rules', toNum(v.weight_vision) !== null ? esc(Math.round(v.weight_vision * 100) + '% photos, ' + Math.round((1 - v.weight_vision) * 100) + '% rules') : '—'],
      ['Most checks per run', toNum(v.max_calls_per_run) !== null ? esc(num(v.max_calls_per_run)) : '—'],
    ]) + (backend === 'claude_cli' && found === false ?
      '<p class="note vw-tight">Install Claude Code on the Mac Mini and sign in once:</p>' + H.command('claude') +
      '<p class="note vw-tight">then type /login. Restart the app afterwards.</p>' : ''));
  }

  function usagePanel(cfg, usage) {
    const prices = ((cfg.google || {}).prices) || {};
    const used = {};
    (usage || []).forEach((u) => { used[u.sku] = (used[u.sku] || 0) + (toNum(u.units) || 0); });
    const rows = Object.keys(prices).map((sku) => {
      const p = prices[sku] || {};
      const free = toNum(p.free_per_month) || 0;
      const n = used[sku] || 0;
      const per = toNum(p.per_1000) || 0;
      return '<tr><td>' + esc(SKU_LABELS[sku] || sku) + '</td><td class="c num">' + num(n) + '</td>' +
        '<td class="c num">' + (free ? num(free) : '—') + '</td><td class="c num">' + (free ? num(Math.max(0, free - n)) : '—') + '</td>' +
        '<td class="c num">' + (per ? '$' + per.toFixed(2) : 'Free') + '</td></tr>';
    }).join('');
    const note = usage === null ? 'Usage this month could not be loaded.' :
      'Calls this month across all markets. After the free calls, Google bills the price per 1,000.';
    return fold('Google free usage this month', 'Calls used and free calls left', '<p class="note vw-tight">' + esc(note) + '</p>' +
      '<div class="vw-scroll"><table class="wt"><thead><tr><th>Service</th><th class="c">Used</th><th class="c">Free a month</th>' +
      '<th class="c">Free left</th><th class="c">Per 1,000 after</th></tr></thead><tbody>' + rows + '</tbody></table></div>' +
      '<p class="note vw-tight">Prices are in config.yaml (google.prices). Check them against Google\'s pricing page now and then.</p>');
  }

  function pathsPanel(cfg) {
    const paths = cfg.paths || {};
    const rows = PATHS.filter(([k]) => paths[k]).map(([k, what]) => [what, '<code class="se-code">' + esc(paths[k]) + '</code>']);
    return fold('Where the data lives', 'Folders on the Mac Mini', kv(rows) +
      '<p class="note vw-tight">Inside the Blue Moon folder on the Mac Mini. All private: none of it goes to GitHub.</p>');
  }

  function safetyPanel(cfg) {
    const safety = cfg.safety || {};
    const crexi = cfg.crexi || {};
    const scoring = cfg.scoring || {};
    return fold('Safety and limits', 'Ask-first limit, credits, score odds', kv([
      ['Ask before paid runs over', esc(num(safety.confirm_over_properties || 50) + ' properties')],
      ['Crexi credits a month', esc(num(crexi.credits_per_month || 0) + ' (no roll-over)')],
      ['Blue Moon starting odds', esc(BM.fmt.pct(scoring.base_rate))],
      ['Photo green / red', esc((scoring.visual_green_min || 80) + '%+ / ' + (scoring.visual_red_max || 20) + '% or less')],
    ]));
  }

  function howPanel() {
    return fold('How to change a setting', 'Three steps, with copy-paste commands',
      '<ol class="se-steps"><li><p>Open the settings file. Most settings are in <b>config.yaml</b>; buy boxes and score weights are in ' +
      '<b>config/asset_classes.yaml</b>; DRILL stages in <b>config/drill.yaml</b>; data sources in <b>config/sources.yaml</b>.</p>' +
      H.command('cd ' + APP_FOLDER + ' && open -e config.yaml') +
      '<p class="note">Change the folder name if yours is different. Keep the spacing at the start of each line as it is.</p></li>' +
      '<li><p>Save, then restart the app. If it runs in the background (you ran scripts/install_service.sh):</p>' +
      H.command('launchctl kickstart -k gui/$(id -u)/' + SERVICE) +
      '<p class="note">If it runs in a Terminal window instead, press Control+C there, then start it again:</p>' +
      H.command('cd ' + APP_FOLDER + ' && uv run run.py serve') + '</li>' +
      '<li><p>Reload this page. If the app does not come back, the error is in output/server.log.</p></li></ol>', 'vw-full');
  }

  function seenText(iso) {
    const d = iso ? new Date(iso) : null;
    if (!d || Number.isNaN(d.getTime())) return 'Signed in';
    return 'Last here ' + d.toLocaleDateString(undefined, {month: 'short', day: 'numeric'}) + ', ' +
      d.toLocaleTimeString(undefined, {hour: 'numeric', minute: '2-digit'});
  }

  function peopleList(sh) {
    const people = Array.isArray(sh.people_list) ? sh.people_list : [];
    if (!people.length) {
      return '<p class="note vw-tight">Nobody is on the list yet, so anyone with the link and the team password gets in. ' +
        'Add people so each person signs in with their own email.</p>';
    }
    const rows = people.map((p) => {
      let dot = 'n';
      let label = "Hasn't signed in yet";
      if (p.status === 'signed_in') { dot = 'g'; label = seenText(p.last_seen); }
      if (p.status === 'everyone') { dot = 'g'; label = 'Everyone there, each with their own email'; }
      const sub = p.status === 'everyone' ? 'Everyone with an ' + esc(p.email.slice(1)) + ' address'
        : (p.via ? 'Through ' + esc(p.via) : '');
      return '<li class="se-key"><div><b>' + esc(p.email) + '</b>' + (sub ? '<div class="note">' + sub + '</div>' : '') +
        '</div><span class="se-yn"><span class="dot ' + dot + '" aria-hidden="true"></span>' + esc(label) + '</span></li>';
    }).join('');
    const listed = people.filter((p) => !p.via).length;
    return '<p class="vw-tight"><b>People who can sign in</b> · ' + num(listed) + '</p><ul class="se-keys">' + rows + '</ul>';
  }

  function sharePanel(sh) {
    sh = sh || {};
    const team = sh.mode === 'team';
    const how = team
      ? 'Each person opens the link and types their own email plus the team password. Only emails on this list get in. Adding or removing someone works right away (no restart); a removed person is signed out on their next click.'
      : 'Everyone with the link types the team password.';
    const pw = sh.password_set === false
      ? '<p><b>Make the team password first.</b> On the Mac Mini, run this, then send the password to your team with the link:</p>' +
        H.command('cd ' + APP_FOLDER + ' && uv run run.py share password') +
        '<p class="note vw-tight">Then restart the dashboard and the link:</p>' +
        H.command('launchctl kickstart -k gui/$(id -u)/' + SERVICE + ' && launchctl kickstart -k gui/$(id -u)/com.bluemoon.share')
      : '';
    const link = sh.on && sh.url
      ? '<p><b>Your team link</b></p>' + H.command(sh.url) +
        '<p class="note vw-tight">The link changes whenever sharing restarts (for example after the Mac restarts); this card always shows the current one. On the Mac itself no sign-in is needed.</p>'
      : '<p><b>Sharing is off.</b> ' + esc(sh.problem || 'Start it on the Mac Mini:') + '</p>' +
        (sh.cloudflared === false ? H.command('cd ' + APP_FOLDER + ' && scripts/install_cloudflared.sh') : '') +
        (sh.password_set === false ? '' : H.command('cd ' + APP_FOLDER + ' && scripts/install_service.sh --share'));
    return panel('Team link (free)', pw + link +
      '<p class="note vw-tight">' + esc(how) + '</p>' + peopleList(sh) +
      '<details class="im-more"><summary>Add or remove people, or see the team password</summary>' +
      '<p class="note">Add people (several at once is fine; *@yourcompany.com lets in everyone there):</p>' +
      H.command('cd ' + APP_FOLDER + ' && uv run run.py share add name@example.com') +
      '<p class="note">Remove someone (they are signed out on their next click):</p>' +
      H.command('cd ' + APP_FOLDER + ' && uv run run.py share remove name@example.com') +
      '<p class="note">See the team password (it is only ever shown on the Mac):</p>' +
      H.command('cd ' + APP_FOLDER + ' && uv run run.py share password') + '</details>', 'vw-full');
  }

  /* ---------- page ---------- */

  const HEAD = H.head('Setup · Settings', 'Settings', [
    'Read-only here. Change them in the files on the Mac Mini (steps at the bottom).',
    'Keys are shown as yes or no, never their values.',
  ]);

  function page(st, health, usage) {
    const cfg = st.config || {};
    const acs = st.asset_classes || {};
    const s = BM.state.summary;
    const acKey = (s && s.asset_class && s.asset_class.key) || ((H.currentMarket() || {}).asset_class);
    return HEAD + '<div class="grid2">' + sharePanel(st.share) + marketsPanel(cfg, acs) + buyBoxPanel(acs[acKey]) + keysPanel(st.keys) +
      photoPanel(cfg, health) + usagePanel(cfg, usage) + pathsPanel(cfg) + safetyPanel(cfg) + howPanel() + '</div>';
  }

  async function render(root) {
    root.innerHTML = HEAD + H.loading('Loading settings…');
    const health = BM.state.health ? Promise.resolve(BM.state.health) : BM.api('/api/health').catch(() => null);
    const usage = BM.api(H.mpath('/analytics')).then((a) => (a && a.api_usage) || []).catch(() => null);
    let st;
    try {
      st = await BM.api('/api/settings');
    } catch (err) {
      if (root.isConnected) root.innerHTML = HEAD + H.errorBox(err);
      return;
    }
    const [h, u] = await Promise.all([health, usage, H.summary()]);
    if (root.isConnected) root.innerHTML = page(st || {}, h, u);
  }

  BM.registerView('settings', {label: 'Settings', group: 'setup', order: 4, render});
})();
