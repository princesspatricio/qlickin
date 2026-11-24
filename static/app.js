document.addEventListener('DOMContentLoaded', function () {
  var root = document.documentElement, btn = document.getElementById('theme');
  if (btn) btn.onclick = function () {
    var t = root.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
    root.setAttribute('data-theme', t);
    try { localStorage.setItem('theme', t); } catch (e) {}
  };
  document.querySelectorAll('[data-count]').forEach(function (el) {     // count-up numbers
    var end = +el.dataset.count, n = 0, step = Math.max(1, Math.ceil(end / 25));
    if (!end) return;
    var t = setInterval(function () { n = Math.min(end, n + step); el.textContent = n; if (n >= end) clearInterval(t); }, 35);
  });

  // ---------- icons: added automatically to buttons, headings, cards and messages ----------
  //RULES-START
  var BTN = [
    [/add to pool/i, 'plus'], [/remove from pool/i, 'minus'], [/close registration/i, 'lock'], [/open registration/i, 'unlock'],
    [/regenerate|new items/i, 'refresh'], [/generate/i, 'sparkles'], [/delete|^remove/i, 'trash'], [/decline|cancel|^clear$/i, 'x'],
    [/approve|set active/i, 'check'], [/resend/i, 'mail'], [/register/i, 'user-plus'],
    [/take a picture|camera/i, 'camera'], [/choose photos|choose file|attach/i, 'paperclip'], [/upload/i, 'upload'],
    [/download|pdf|excel/i, 'download'], [/print/i, 'printer'], [/give full marks|^full$/i, 'award'],
    [/publish|submit|send/i, 'send'], [/take quiz|start quiz|start camera|^start/i, 'play'], [/^stop/i, 'stop'],
    [/end class/i, 'flag'], [/scan/i, 'scan'], [/see students|^check$|scores/i, 'clipboard'],
    [/save|update my work|^apply/i, 'check'], [/search/i, 'search'], [/reset/i, 'refresh'], [/^edit|change/i, 'edit'],
    [/^view|preview/i, 'eye'], [/copy/i, 'copy'], [/^back|all announcements|all activities/i, 'arrow-left'],
    [/^open|next|continue/i, 'arrow-right'], [/login/i, 'login'], [/^filter/i, 'filter']
  ];
  var HEAD = [
    [/registration link/i, 'link'], [/quick (actions|links)/i, 'sparkles'], [/registrations?|register a student|students? with|absences/i, 'users'],
    [/sessions?/i, 'calendar'], [/quizzes|quiz|result|review/i, 'quiz'], [/question/i, 'help'], [/manual attendance/i, 'edit'],
    [/scan/i, 'scan'], [/learning|module|material/i, 'book'], [/activit|to-do|my work|check students|post an activity/i, 'clipboard'],
    [/announce/i, 'megaphone'], [/history|log$/i, 'history'], [/setting/i, 'settings'], [/notification/i, 'bell'],
    [/profile|my details/i, 'user'], [/qr code/i, 'qr'], [/password/i, 'lock'], [/sign in|welcome|forgot/i, 'login'],
    [/\brate\b|chart|per week|per class/i, 'percent'], [/records|attendance|summary/i, 'table']
  ];
  var STAT = [
    [/pending/i, 'clock'], [/absent|absence/i, 'x'], [/present|checked in|took it|submitted/i, 'check'], [/\blate\b/i, 'alert'],
    [/approved|students/i, 'graduation'], [/\brate\b|average/i, 'percent'], [/highest|lowest/i, 'award'], [/excused/i, 'info']
  ];
  var BAD = /could not|wrong|invalid|cannot|can no longer|too big|too many|error|failed|expired|nothing was saved|not in this|already|outdated|please|must |not allowed|refused/i;
  //RULES-END
  function ic(n) { return '<svg class="ic" aria-hidden="true"><use href="#i-' + n + '"/></svg>'; }
  function pick(rules, text) { for (var i = 0; i < rules.length; i++) if (rules[i][0].test(text)) return rules[i][1]; return null; }
  function ownText(el) {
    var t = Array.prototype.filter.call(el.childNodes, function (n) { return n.nodeType === 3; })
      .map(function (n) { return n.textContent; }).join(' ').trim();
    return t || (el.textContent || '').trim();
  }
  document.querySelectorAll('.btn').forEach(function (el) {
    if (el.querySelector('svg.ic') || el.classList.contains('switch')) return;
    var t = ownText(el).replace(/^\+\s*/, '');
    var n = t && pick(BTN, t);
    if (n) el.insertAdjacentHTML('afterbegin', ic(n));
  });
  document.querySelectorAll('main.wrap h2').forEach(function (el) {
    if (el.querySelector('svg.ic') || el.closest('.pr') || el.classList.contains('pr')) return;
    var n = pick(HEAD, ownText(el));
    if (n) el.insertAdjacentHTML('afterbegin', ic(n));
  });
  document.querySelectorAll('.stat').forEach(function (el) {
    if (el.querySelector('.stat-ic')) return;
    var n = pick(STAT, (el.textContent || '').replace(/[0-9%.\/\-]+/g, ' '));
    if (n) el.insertAdjacentHTML('afterbegin', '<span class="stat-ic">' + ic(n) + '</span>');
  });
  document.querySelectorAll('main.wrap > .msg').forEach(function (el) {
    var bad = BAD.test(el.textContent);
    el.classList.toggle('bad', bad);
    el.insertAdjacentHTML('afterbegin', ic(bad ? 'alert' : 'check'));
  });

  // ---------- books / list view switch (Learning Materials) ----------
  var tg = document.querySelector('.viewtoggle');
  if (tg) {
    var panes = document.querySelectorAll('[data-pane]');
    var show = function (v) {
      panes.forEach(function (p) { p.hidden = p.dataset.pane !== v; });
      tg.querySelectorAll('[data-show]').forEach(function (b) { b.classList.toggle('on', b.dataset.show === v); });
      try { localStorage.setItem('matView', v); } catch (e) {}
    };
    tg.querySelectorAll('[data-show]').forEach(function (b) { b.onclick = function () { show(b.dataset.show); }; });
    var saved = 'shelf';
    try { saved = localStorage.getItem('matView') || 'shelf'; } catch (e) {}
    show(saved === 'list' ? 'list' : 'shelf');
  }
});
