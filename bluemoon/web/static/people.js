/* The drawer's "Who is behind this property" block: BM.people.html(detail) draws it,
 * BM.people.mount(root, detail, onDrill) wires it (property.js calls both).
 *
 * The seller (owner of record, usually a company) and the people tied to it, each with a role and where that
 * came from. A company with nobody confirmed says "No person confirmed yet". The offer recipient is chosen
 * here and kept apart from the seller. Conflicting owner names stay listed. Every change goes to the people
 * routes in ARCHITECTURE.md; the answer redraws the block, and `onDrill` gets the answer when it carries a new
 * owner-contact state (a role change can move the contact level).
 */
(function () {
  'use strict';
  if (!window.BM) return;
  const {esc} = BM;

  let st = null;   // {id, d, root, busy, onDrill}

  function has(v) {
    return v !== null && v !== undefined && v !== '';
  }

  function whoText(by, at) {
    return [has(by) ? 'by ' + by : '', has(at) ? BM.fmt.date(at) : ''].filter(Boolean).join(' · ');
  }

  /* ---------- pieces ---------- */

  function sellerHtml(p) {
    const s = p.seller;
    const name = s.name ? '<b>' + esc(s.name) + '</b>' + (s.is_entity ? ' <span class="pill">Company</span>' : '') : '<span class="muted">Not on record</span>';
    const conf = p.confirmed.length
      ? p.confirmed.map((n) => '<b>' + esc(n) + '</b>').join(', ') + ' <span class="pill good">Confirmed</span>'
      : '<span class="pill warn">No person confirmed yet</span>';
    return '<dl class="kv pe-who"><dt>Seller (owner of record)</dt><dd>' + name + '</dd>' +
      '<dt>Person confirmed</dt><dd>' + conf + '</dd></dl>';
  }

  function conflictsHtml(p) {
    const names = p.conflicts.owner_names || [];
    if (!names.length && !p.conflicts.county_differs) return '';
    const lines = names.map((x) => '<li>' + esc(x.header + ': ' + x.value) + ' <span class="hint">(' + esc([x.source, x.day].filter(Boolean).join(', ')) + ')</span></li>');
    if (p.conflicts.county_differs) lines.push('<li>The county tax record shows a different owner than the Crexi export.</li>');
    return '<div class="p-banner pe-conflict" role="note"><b>Sources disagree about the owner.</b> The first value stays as the seller; nothing was overwritten.' +
      '<ul>' + lines.join('') + '</ul></div>';
  }

  function contactHtml(c, person) {
    const use = person && c.can_receive
      ? ' <button type="button" class="btn small" data-pe-act="recipient" data-person="' + esc(person.id) + '" data-contact="' + esc(c.id) + '"' +
        ' aria-label="Address offers to ' + esc(person.name) + ' at ' + esc(c.value) + '">Use this one</button>'
      : '';
    const flag = c.status === 'wrong' || c.status === 'dnc' ? ' <span class="pill bad">' + (c.status === 'dnc' ? 'Do not contact' : 'Wrong') + '</span>' : '';
    return '<li><span class="muted">' + esc(c.channel) + '</span> <span class="pe-val">' + esc(c.value) + '</span>' + flag + use + '</li>';
  }

  function personHtml(p, x) {
    const roles = p.role_choices.map(([k, l]) => '<option value="' + esc(k) + '"' + (k === x.role ? ' selected' : '') + '>' + esc(l) + '</option>').join('');
    const role = x.can_change_role
      ? '<label class="field pe-role">Role<select data-pe-act="role" data-person="' + esc(x.id) + '" aria-label="Role for ' + esc(x.name) + '">' + roles + '</select></label>'
      : '<span class="hint">Role set by the owner-contact steps</span>';
    const source = [x.source_label ? 'From ' + x.source_label : '', whoText(x.edited_by, x.edited_at)].filter(Boolean).join(' · ');
    const pick = x.is_recipient
      ? '<span class="pill good">Offer recipient</span>'
      : '<button type="button" class="btn small" data-pe-act="recipient" data-person="' + esc(x.id) + '" aria-label="Address offers to ' + esc(x.name) + '">Address offers to them</button>';
    return '<li class="pe-person"><div class="pe-top"><b>' + esc(x.name) + '</b> <span class="pill">' + esc(x.role_label) + '</span></div>' +
      (source ? '<div class="note">' + esc(source) + '</div>' : '') +
      (x.notes ? '<div class="note">' + esc(x.notes) + '</div>' : '') +
      (x.contacts.length ? '<ul class="pe-contacts">' + x.contacts.map((c) => contactHtml(c, x)).join('') + '</ul>' : '<div class="note">No contact recorded for them.</div>') +
      '<div class="pe-acts">' + role + pick + '</div></li>';
  }

  function recipientHtml(p) {
    const r = p.recipient;
    if (!r) return '<p class="note pe-recipient">No offer recipient chosen yet. Pick one below; it is kept separate from the seller.</p>';
    return '<div class="pe-recipient"><b>Offers go to ' + esc(r.name) + '</b> <span class="pill">' + esc(r.role_label) + '</span>' +
      '<div class="note">' + esc(r.contact_text ? 'Using ' + r.contact_text + '. ' : 'No contact chosen yet. ') + 'The seller stays ' +
      esc(p.seller.name || 'as recorded') + '.' + (r.note ? ' ' + esc(r.note) : '') + (whoText(r.chosen_by, r.chosen_at) ? ' Chosen ' + esc(whoText(r.chosen_by, r.chosen_at)) + '.' : '') + '</div>' +
      '<button type="button" class="btn small" data-pe-act="clear-recipient">Clear recipient</button></div>';
  }

  function addHtml(p) {
    const roles = p.role_choices.map(([k, l]) => '<option value="' + esc(k) + '"' + (k === 'other' ? ' selected' : '') + '>' + esc(l) + '</option>').join('');
    return '<details class="p-more" data-keep="add-person"><summary>Add a person</summary>' +
      '<form class="pe-add" data-pe-form="person">' +
      '<label class="field">Name<input name="name" required maxlength="300" autocomplete="off"></label>' +
      '<label class="field">Role<select name="role">' + roles + '</select></label>' +
      '<label class="field">Where you learned it<input name="notes" maxlength="500" autocomplete="off"></label>' +
      '<button type="submit" class="btn small">Add person</button></form></details>';
  }

  function html(d) {
    const p = d && d.people;
    if (!p) return '';
    if (!st || st.id !== d.id) st = {id: d.id, root: null, busy: false, onDrill: null};
    st.d = d;
    const where = BM.fmt.title(p.property.address || '') + (p.property.city ? ', ' + BM.fmt.title(p.property.city) : '');
    const none = !p.people.length ? '<p class="note">No people recorded yet. Add one below, or run the owner-contact steps.</p>' : '';
    const loose = p.loose_contacts.length
      ? '<h3>Contacts not tied to a person</h3><ul class="pe-contacts">' + p.loose_contacts.map((c) => contactHtml(c, null)).join('') + '</ul>' : '';
    return '<section class="sec pe" data-part="people" aria-labelledby="pe-h"><h2 id="pe-h">Who is behind this property</h2>' +
      '<div class="eyebrow">Property ' + esc(p.property.id) + ' · ' + esc(where) + '</div>' +
      sellerHtml(p) + conflictsHtml(p) + recipientHtml(p) + none +
      (p.people.length ? '<ul class="pe-list">' + p.people.map((x) => personHtml(p, x)).join('') + '</ul>' : '') + loose + addHtml(p) + '</section>';
  }

  function redraw() {
    const el = st.root.querySelector('[data-part="people"]');
    if (el) el.outerHTML = html(st.d);
  }

  /* ---------- writes ---------- */

  async function write(path, body, method) {
    const mine = st;
    if (!mine || mine.busy) return null;
    mine.busy = true;
    try {
      const res = await BM.api(path, {method, body});
      if (st !== mine) return res;
      mine.d.people = res.people;
      redraw();
      if (res.message) BM.toast(res.message);
      if (res.drill && mine.onDrill) mine.onDrill(res);
      return res;
    } catch (err) {
      if (st === mine) { BM.toast(err.message, {error: true}); redraw(); }
      return null;
    } finally {
      mine.busy = false;
    }
  }

  function base() {
    return BM.m('/properties/' + st.id);
  }

  /* ---------- events (bound once on the drawer) ---------- */

  function onClick(ev) {
    const b = ev.target instanceof Element ? ev.target.closest('[data-pe-act]') : null;
    if (!b || !st || !st.d || b.tagName === 'SELECT') return;
    const act = b.getAttribute('data-pe-act');
    if (act === 'recipient') {
      const contact = b.getAttribute('data-contact');
      write(base() + '/recipient', {person_id: Number(b.getAttribute('data-person')), contact_id: contact ? Number(contact) : null}, 'PUT');
    } else if (act === 'clear-recipient') {
      write(base() + '/recipient', undefined, 'DELETE');
    }
  }

  function onChange(ev) {
    const t = ev.target;
    if (!(t instanceof Element) || !st || !st.d || !t.matches('select[data-pe-act="role"]')) return;
    write(base() + '/people/' + Number(t.getAttribute('data-person')) + '/role', {role: t.value}, 'PUT');
  }

  function onSubmit(ev) {
    const form = ev.target instanceof Element ? ev.target.closest('form[data-pe-form]') : null;
    if (!form || !st || !st.d) return;
    ev.preventDefault();
    if (st.busy || (form.reportValidity && !form.reportValidity())) return;
    const v = (name) => String(form.elements[name].value || '').trim();
    write(base() + '/people', {name: v('name'), role: v('role') || null, notes: v('notes') || null, source: 'you'}, 'POST');
  }

  function mount(root, d, onDrill) {
    if (!st || st.id !== d.id) return;
    st.root = root;
    st.onDrill = onDrill;
    if (root.__bmPeopleBound) return;
    root.__bmPeopleBound = true;
    root.addEventListener('click', onClick);
    root.addEventListener('change', onChange);
    root.addEventListener('submit', onSubmit);
  }

  BM.people = {html, mount};
})();
