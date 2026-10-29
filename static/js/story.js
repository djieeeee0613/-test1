// 緣投回家記：緣投從緣投星掉進蟲洞來到南投，玩家做任務累積「回家能量」（XP），升到最高等級時蟲洞再次打開送他回家。
// 三段動畫：Story.intro()（第一次進遊戲）、Story.mission()（每完成一個任務，緣投到南投一個自然景點玩）、Story.farewell()（升到最高等級）。
// 動畫依序播放（Story.enqueue），升級彈窗會排在任務動畫之後。全部用 SVG / CSS 畫，不需要額外圖檔。
(function () {
  const INTRO_KEY = 'yt_intro_v1', SPOT_KEY = 'yt_spot_idx';
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (e) {} },
  };

  // ── 南投自然景點 ──
  const SPOTS = [
    { id: 'sunmoon', name: '日月潭', town: '魚池鄉', fact: '台灣最大的天然湖泊之一，湖中央的拉魯島是邵族的聖地。' },
    { id: 'hehuan', name: '合歡山', town: '仁愛鄉', fact: '武嶺海拔 3,275 公尺，是台灣公路的最高點，冬天有機會看到雪。' },
    { id: 'cingjing', name: '清境', town: '仁愛鄉', fact: '海拔約 1,700 公尺的高山草原，綿羊在雲霧裡慢慢散步。' },
    { id: 'xitou', name: '溪頭', town: '鹿谷鄉', fact: '大學池上的竹拱橋，倒映在柳杉林裡，是溪頭最經典的畫面。' },
    { id: 'aowanda', name: '奧萬大', town: '仁愛鄉', fact: '台灣知名的賞楓勝地，秋冬時整片山谷都被楓葉染紅。' },
    { id: 'shanlinxi', name: '杉林溪', town: '竹山鎮', fact: '海拔約 1,600 公尺的森林，有瀑布、溪流和四季花海。' },
    { id: 'jiujiu', name: '九九峰', town: '草屯鎮・國姓鄉', fact: '921 地震時山壁崩落，露出一座座裸岩山峰，像大地的鋸齒。' },
    { id: 'jiji', name: '集集綠色隧道', town: '集集鎮', fact: '兩旁的老樟樹把道路圍成綠色隧道，騎單車最舒服。' },
  ];
  const TASK_LINE = {
    checkin: n => `幫職人蓋完章，緣投開心地跑到了${n}！`,
    quiz: n => `答完題目，緣投跑去${n}玩了！`,
    post: n => `看完你的分享，緣投也跑去${n}看看！`,
  };

  // ── 景點插畫（viewBox 400×300，底部對齊） ──
  const P = pts => pts.map(p => p.join(',')).join(' ');
  function ridge(y0, amp, n, seed) {
    const pts = [[0, 300]];
    for (let i = 0; i <= n; i++) pts.push([i * 400 / n, y0 - amp * Math.abs(Math.sin(i * 1.7 + seed)) - (i % 2 ? amp * .25 : 0)]);
    pts.push([400, 300]);
    return P(pts);
  }
  function stars(n) {
    let s = '';
    for (let i = 0; i < n; i++) s += `<circle cx="${(i * 97) % 400}" cy="${(i * 53) % 150}" r="${i % 3 ? .8 : 1.4}" fill="#fff" opacity="${.4 + (i % 5) / 8}"/>`;
    return s;
  }
  function sky(top, bottom, night) {
    return `<defs><linearGradient id="sk" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="${top}"/><stop offset="1" stop-color="${bottom}"/></linearGradient></defs>
      <rect width="400" height="300" fill="url(#sk)"/>${night ? stars(60) + '<circle cx="320" cy="60" r="18" fill="#fdf3c4"/><circle cx="328" cy="54" r="16" fill="' + top + '"/>' : '<circle cx="310" cy="70" r="26" fill="#fff6c9" opacity=".9"/>'}`;
  }
  const tree = (x, y, h, c) => `<polygon points="${x},${y - h} ${x - h * .32},${y} ${x + h * .32},${y}" fill="${c}"/><rect x="${x - 1.5}" y="${y}" width="3" height="${h * .15}" fill="#4a3424"/>`;
  const round = (x, y, r, c) => `<rect x="${x - 2}" y="${y}" width="4" height="${r * 1.2}" fill="#5a3a24"/><circle cx="${x}" cy="${y}" r="${r}" fill="${c}"/>`;
  const sheep = (x, y) => `<g transform="translate(${x},${y})"><ellipse rx="11" ry="7" fill="#fff"/><circle cx="10" cy="-3" r="4" fill="#3a3a3a"/><rect x="-7" y="5" width="2" height="5" fill="#3a3a3a"/><rect x="5" y="5" width="2" height="5" fill="#3a3a3a"/></g>`;
  const cloud = (x, y, s) => `<g transform="translate(${x},${y}) scale(${s})" fill="#fff" opacity=".95"><circle r="16"/><circle cx="18" cy="4" r="13"/><circle cx="-18" cy="5" r="12"/><circle cx="34" cy="8" r="9"/><circle cx="-32" cy="9" r="8"/></g>`;

  function landscape(id, night) {
    let s = '';
    switch (id) {
      case 'hehuan':
        s = sky('#0f1d4a', '#5b6fae', true) +
          `<polygon points="${ridge(150, 60, 6, 1)}" fill="#3d4f6e"/><polygon points="${ridge(175, 40, 7, 3)}" fill="#55684f"/>` +
          [[-10, 215, 1.6], [90, 225, 1.4], [200, 212, 1.7], [320, 222, 1.5], [420, 214, 1.6]].map(c => cloud(...c)).join('') +
          `<rect y="232" width="400" height="68" fill="#eef3fb"/>`;
        break;
      case 'cingjing':
        s = sky('#8fd3f4', '#e6f7ff') + cloud(80, 60, .8) +
          `<polygon points="${ridge(150, 30, 5, 2)}" fill="#7ea98a"/>` +
          `<path d="M0 210 Q100 170 200 200 T400 190 V300 H0Z" fill="#8fcf6a"/><path d="M0 240 Q120 215 240 238 T400 230 V300 H0Z" fill="#6fbf4f"/>` +
          sheep(70, 228) + sheep(130, 245) + sheep(300, 236) + sheep(345, 252) +
          `<g stroke="#8a6a44" stroke-width="2">${[20, 60, 100, 140, 180].map(x => `<line x1="${x}" y1="262" x2="${x}" y2="282"/>`).join('')}<line x1="15" y1="268" x2="185" y2="268"/></g>`;
        break;
      case 'xitou':
        s = sky('#a8d8c8', '#e8f5ee') +
          Array.from({ length: 14 }, (_, i) => tree(i * 30 + 5, 200, 90 + (i % 3) * 18, i % 2 ? '#2f6b45' : '#3d7d52')).join('') +
          `<rect y="200" width="400" height="100" fill="#4a8a6a"/><ellipse cx="200" cy="245" rx="190" ry="34" fill="#7cc1b4"/>` +
          `<path d="M110 245 Q200 175 290 245" fill="none" stroke="#c9a46a" stroke-width="7"/><path d="M110 245 Q200 175 290 245" fill="none" stroke="#e6c88a" stroke-width="2"/>` +
          `<path d="M110 245 Q200 315 290 245" fill="none" stroke="#c9a46a" stroke-width="4" opacity=".35"/>`;
        break;
      case 'aowanda':
        s = sky('#f6c58f', '#fde9cc') +
          `<polygon points="${ridge(160, 50, 5, 4)}" fill="#a86b45"/><rect y="215" width="400" height="85" fill="#7a5a3a"/>` +
          [[30, 200, 22, '#d9472b'], [80, 210, 18, '#e8742b'], [140, 198, 24, '#c7372b'], [205, 208, 19, '#f09a2b'], [260, 200, 23, '#d9472b'], [320, 210, 20, '#e8742b'], [375, 202, 22, '#c7372b']].map(t => round(...t)).join('') +
          Array.from({ length: 10 }, (_, i) => `<circle cx="${(i * 41) % 400}" cy="${60 + (i * 37) % 160}" r="3" fill="${i % 2 ? '#d9472b' : '#f09a2b'}"><animate attributeName="cy" values="${40 + (i * 37) % 160};${80 + (i * 37) % 200}" dur="${3 + i % 3}s" repeatCount="indefinite"/></circle>`).join('');
        break;
      case 'shanlinxi':
        s = sky('#9fd0c0', '#e6f4ee') +
          `<polygon points="120,300 120,80 175,60 175,300" fill="#4d6b58"/><polygon points="225,300 225,70 290,90 290,300" fill="#4d6b58"/>` +
          `<rect x="175" y="70" width="50" height="230" fill="#dff4ff"/>` +
          `<g stroke="#ffffff" stroke-width="3" stroke-dasharray="14 10">${[185, 200, 215].map(x => `<line x1="${x}" y1="70" x2="${x}" y2="250"><animate attributeName="stroke-dashoffset" values="0;-48" dur="1s" repeatCount="indefinite"/></line>`).join('')}</g>` +
          `<ellipse cx="200" cy="262" rx="130" ry="24" fill="#8fd0e6"/>` +
          Array.from({ length: 12 }, (_, i) => `<circle cx="${10 + i * 34}" cy="${285 - (i % 2) * 6}" r="5" fill="${['#f28cb1', '#f5d36b', '#b48cf2'][i % 3]}"/>`).join('') +
          tree(40, 250, 110, '#2f6b45') + tree(360, 250, 120, '#2f6b45');
        break;
      case 'jiujiu':
        s = sky('#bfe0f5', '#f2f8fb') + cloud(90, 70, .7) +
          `<polygon points="${P([[0, 300], ...Array.from({ length: 21 }, (_, i) => [i * 20, i % 2 ? 150 + (i % 4) * 12 : 110 + (i % 5) * 8]), [400, 300]])}" fill="#b49a73"/>` +
          `<polygon points="${P([[0, 300], ...Array.from({ length: 21 }, (_, i) => [i * 20, i % 2 ? 175 + (i % 3) * 8 : 150 + (i % 4) * 6]), [400, 300]])}" fill="#7f9a62"/>` +
          `<rect y="230" width="400" height="70" fill="#6a8a52"/>`;
        break;
      case 'jiji':
        s = sky('#cdeccf', '#f2fbf0') +
          `<polygon points="170,300 230,300 210,150 190,150" fill="#9a8a78"/><line x1="200" y1="300" x2="200" y2="150" stroke="#fff" stroke-width="2" stroke-dasharray="10 10"/>` +
          Array.from({ length: 6 }, (_, i) => { const y = 160 + i * 26, sc = .5 + i * .2; return round(170 - 30 * sc - i * 10, y, 22 * sc, '#3f8a4a') + round(230 + 30 * sc + i * 10, y, 22 * sc, '#3f8a4a'); }).join('') +
          `<path d="M0 0 H400 V90 Q200 150 0 90Z" fill="#4f9a5a" opacity=".85"/>`;
        break;
      default: // 日月潭
        s = sky(night ? '#0f1d4a' : '#f7b98b', night ? '#3b4f8a' : '#ffe2c4', night) +
          `<polygon points="${ridge(170, 45, 6, 2)}" fill="${night ? '#24365a' : '#6f9a7c'}"/><polygon points="${ridge(190, 28, 8, 5)}" fill="${night ? '#1d2c4a' : '#4f7d5f'}"/>` +
          `<rect y="200" width="400" height="100" fill="${night ? '#2a3f73' : '#6fb3c8'}"/>` +
          `<g stroke="#fff" opacity=".5" stroke-width="2">${[[60, 225, 40], [250, 240, 60], [140, 262, 50], [300, 275, 40]].map(([x, y, w]) => `<line x1="${x}" y1="${y}" x2="${x + w}" y2="${y}"><animate attributeName="opacity" values=".2;.8;.2" dur="2.6s" repeatCount="indefinite"/></line>`).join('')}</g>` +
          `<ellipse cx="300" cy="208" rx="22" ry="6" fill="${night ? '#1d2c4a' : '#4f7d5f'}"/>${round(300, 196, 7, night ? '#1d2c4a' : '#3d6d4f')}` +
          `<path d="M0 255 Q200 200 400 255 V300 H0Z" fill="${night ? '#1a2a3f' : '#7fae5f'}"/><path d="M0 275 Q200 240 400 275 V300 H0Z" fill="${night ? '#14212f' : '#6a9a4c'}"/>`;
    }
    return `<svg viewBox="0 0 400 300" preserveAspectRatio="xMidYMax slice" xmlns="http://www.w3.org/2000/svg">${s}</svg>`;
  }

  // ── 舞台 ──
  let root, timers = [], done = null;
  function stage(html) {
    if (!root) { root = document.createElement('div'); root.id = 'story'; document.body.appendChild(root); }
    root.innerHTML = html;
    root.classList.add('show');
    document.body.style.overflow = 'hidden';
    return sel => root.querySelector(sel);
  }
  const later = (ms, fn) => timers.push(setTimeout(fn, ms));
  function finish() {
    timers.forEach(clearTimeout); timers = [];
    if (root) { root.className = ''; root.innerHTML = ''; root.onclick = null; }
    document.body.style.overflow = '';
    const d = done; done = null; if (d) d();
  }
  const starLayer = () => `<div class="st-layer st-stars">${Array.from({ length: 90 }, (_, i) => `<i style="left:${(i * 37) % 100}%;top:${(i * 61) % 100}%;animation-delay:${(i % 7) * .3}s"></i>`).join('')}</div>`;
  const tunnel = () => `<div class="st-layer st-tunnel">${Array.from({ length: 36 }, (_, i) => `<i style="--r:${i * 10}deg;animation-delay:${(i % 6) * .11}s"></i>`).join('')}</div>`;
  function play(build) { return new Promise(res => { done = res; build(); }); }

  // ── 開場：緣投掉進蟲洞來到南投 ──
  function intro() {
    store.set(INTRO_KEY, '1');
    return play(() => {
      const $ = stage(`${starLayer()}<div class="st-layer st-home"><div class="st-planet"></div></div>${tunnel()}
        <div class="st-layer st-nantou">${landscape('sunmoon')}<div class="st-shade"></div></div>
        <div class="st-hole"></div><img class="st-mascot" src="${Story.forms[0]}" alt="緣投">
        <div class="st-bubble"></div><div class="st-cap"></div><div class="st-flash"></div>
        <div class="st-actions"><button class="st-btn">幫緣投回家 →</button></div>
        <button class="st-skip">略過</button>`);
      const cap = (t) => { const c = $('.st-cap'); c.classList.remove('on'); later(250, () => { c.innerHTML = t; c.classList.add('on'); }); };
      const m = $('.st-mascot'), hole = $('.st-hole');
      $('.st-skip').onclick = finish; $('.st-btn').onclick = finish;
      $('.st-stars').classList.add('on'); $('.st-home').classList.add('on');
      m.style.top = '42%'; m.classList.add('on', 'mv-bob');
      cap('在遙遠的緣投星上，<br>住著一隻愛睡覺的小精靈——緣投。');
      later(3400, () => { hole.classList.add('open'); cap('有一天，天空突然裂開一個蟲洞……'); });
      later(5400, () => { m.className = 'st-mascot on mv-suck'; });
      later(7200, () => { hole.classList.remove('open'); $('.st-tunnel').classList.add('on'); $('.st-stars').classList.remove('on'); $('.st-home').classList.remove('on'); cap('咻——！'); });
      later(9000, () => { $('.st-flash').classList.add('on'); });
      later(9300, () => {
        $('.st-flash').classList.remove('on'); $('.st-tunnel').classList.remove('on'); $('.st-nantou').classList.add('on');
        m.style.top = '50%'; m.className = 'st-mascot on mv-drop';
        $('.st-cap').classList.add('top');
        cap('砰！緣投掉進了一個有山、有湖的地方——<br><b>南投</b>。');
      });
      later(12000, () => { const b = $('.st-bubble'); b.textContent = '這裡是哪裡？我要怎麼回家……'; b.classList.add('on'); });
      later(14600, () => {
        $('.st-bubble').classList.remove('on');
        cap('完成任務、累積回家能量，<br>等緣投升到最高等級，蟲洞就會再次打開！<small>打卡、答題、分享旅程，都能幫緣投充能。</small>');
        $('.st-actions').classList.add('on');
      });
    });
  }

  // ── 任務：緣投到南投的自然景點玩 ──
  function mission(type, o = {}) {
    let i = +(store.get(SPOT_KEY) || 0);
    const spot = SPOTS[i % SPOTS.length];
    store.set(SPOT_KEY, String(i + 1));
    return play(() => {
      const $ = stage(`<div class="st-layer st-scene">${landscape(spot.id)}<div class="st-shade"></div></div>
        <div class="st-place"><span class="tag">緣投的南投冒險</span><h2>${spot.name}</h2><div class="town">${spot.town}</div>
          <div class="st-energy">回家能量${o.xp ? ` +${o.xp}` : ''}<b><i></i></b></div></div>
        <img class="st-mascot" src="${o.form || Story.forms[0]}" alt="緣投"><div class="st-cap"></div><div class="st-hint">點一下繼續</div>`);
      root.onclick = finish; root.classList.add('st-mission');
      $('.st-scene').classList.add('on');
      const m = $('.st-mascot'); m.classList.add('on', 'mv-run');
      later(300, () => $('.st-place').classList.add('on'));
      later(500, () => { $('.st-energy i').style.width = Math.max(4, Math.min(100, o.pct || 0)) + '%'; });
      later(900, () => { const c = $('.st-cap'); c.innerHTML = `${(TASK_LINE[type] || TASK_LINE.checkin)(spot.name)}<small>${spot.fact}</small>`; c.classList.add('on'); });
      later(6000, finish);
    });
  }

  // ── 回家：最高等級，蟲洞打開，緣投說 bye bye ──
  function farewell(o = {}) {
    return play(() => {
      const $ = stage(`${starLayer()}<div class="st-layer st-home"><div class="st-planet"></div></div>
        <div class="st-layer st-nantou">${landscape('sunmoon', true)}<div class="st-shade"></div></div>
        <div class="st-hole"></div><img class="st-mascot" src="${o.form || Story.forms[3]}" alt="緣投">
        <div class="st-bubble"></div><div class="st-cap"></div><div class="st-flash"></div>
        <div class="st-actions"><button class="st-btn">${o.button || '領取永續達人證書'}</button></div>
        <button class="st-skip">略過</button>`);
      const cap = (t) => { const c = $('.st-cap'); c.classList.remove('on'); later(250, () => { c.innerHTML = t; c.classList.add('on'); }); };
      const m = $('.st-mascot'), hole = $('.st-hole'), b = $('.st-bubble');
      $('.st-skip').onclick = finish; $('.st-btn').onclick = finish;
      $('.st-nantou').classList.add('on'); m.classList.add('on', 'mv-stay'); $('.st-cap').classList.add('top');
      cap('回家的能量，終於集滿了！');
      later(2600, () => { hole.classList.add('open'); cap('日月潭的夜空裡，蟲洞再次打開了。'); });
      later(4600, () => { m.className = 'st-mascot on mv-wave'; b.textContent = '謝謝你陪我走遍南投！Bye bye～'; b.classList.add('on'); cap(''); });
      later(8200, () => { b.classList.remove('on'); m.className = 'st-mascot on mv-rise'; });
      later(10200, () => { hole.classList.add('close'); $('.st-flash').classList.add('on'); });
      later(10600, () => {
        $('.st-flash').classList.remove('on'); $('.st-nantou').classList.remove('on');
        $('.st-stars').classList.add('on'); $('.st-home').classList.add('on');
        m.style.top = '50%'; m.className = 'st-mascot on mv-land'; $('.st-cap').classList.remove('top');
        cap('緣投平安回到了緣投星。<small>他把南投的山、湖、職人，還有你，都收進了心裡。</small>');
      });
      later(12400, () => $('.st-planet').classList.add('glow'));
      later(13600, () => $('.st-actions').classList.add('on'));
    });
  }

  // ── 播放佇列：動畫一段一段來 ──
  let chain = Promise.resolve();
  function enqueue(fn) { chain = chain.then(() => fn()).catch(() => {}); return chain; }

  window.Story = {
    forms: ['/static/images/mascot/mascot-sleep.png', '/static/images/mascot/mascot-farmer.png', '/static/images/mascot/mascot-foodie.png', '/static/images/mascot/mascot-cheer.png'],
    spots: SPOTS,
    seenIntro: () => !!store.get(INTRO_KEY),
    intro: () => enqueue(intro),
    mission: (type, o) => enqueue(() => mission(type, o)),
    farewell: o => enqueue(() => farewell(o)),
    enqueue,
    skip: finish,
  };
})();
