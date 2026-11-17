// island-bridge.js - Spicetify extension for the user's desktop island.
// Sends the playing song's own Spotify synced lyrics + the exact play position to the island
// (http://127.0.0.1:47812/spotify, only on this PC), plus heart / shuffle / repeat / the next 3 songs in the queue.
// The island can ask for heart / shuffle / repeat back through http://127.0.0.1:47813/cmd. Nothing leaves this PC.
// Also: search Spotify, play a song now or add it to the queue (from the island's phone / tablet deck), and Spotify
// Connect: which of his devices is playing, move playback to another one, control it while it plays there.
// And his library for the deck: Liked Songs + playlists, their songs, play one in its playlist.
(function islandBridge() {
  const P = window.Spicetify && Spicetify.Player;
  if (!P || !Spicetify.CosmosAsync || !P.data) { setTimeout(islandBridge, 600); return; }
  const URL = "http://127.0.0.1:47812/spotify";
  const VER = 8;                                     // the island tells the deck to run "spicetify apply" when this is older
  const post = body => fetch(URL, {method: "POST", headers: {"Content-Type": "text/plain"}, body: JSON.stringify(body)}).catch(() => null);
  let cur = null, title = "", last = {pos: 0, at: 0, playing: false};

  function info() {
    const it = P.data && (P.data.item || P.data.track);
    if (!it) return null;
    const md = it.metadata || {};
    let img = md.image_url || md.image_large_url || md.image_xlarge_url ||
      (it.album && it.album.images && it.album.images[0] && it.album.images[0].url) || "";
    if (img.startsWith("spotify:image:")) img = "https://i.scdn.co/image/" + img.split(":")[2];
    return {
      uri: it.uri,
      title: it.name || md.title || "",
      artist: (it.artists || []).map(a => a.name).join(", ") || md.artist_name || "",
      album: (it.album && it.album.name) || md.album_title || "",
      dur: (P.getDuration() || 0) / 1000,
      art: /^https:\/\//.test(img) ? img : "",
    };
  }

  // Spotify Connect: the devices on his account and which one is playing
  function connect() {
    try {
      const C = Spicetify.Platform && Spicetify.Platform.ConnectAPI, ds = (C && C.state && C.state.devices) || [];
      const act = ds.find(d => d && d.isActive);
      return {remote: !!(act && !act.isLocal), dev: act ? String(act.name || "") : "", devs: ds};
    } catch (e) { return {remote: false, dev: "", devs: []}; }
  }
  let lastDevs = "";
  async function devicesPost(force) {
    let items = connect().devs.map(d => ({id: d.id, name: String(d.name || "?"), type: String(d.type || ""), active: !!d.isActive, local: !!d.isLocal}));
    if (!items.length && force) {
      try {
        const r = await Spicetify.CosmosAsync.get("https://api.spotify.com/v1/me/player/devices");
        items = ((r && r.devices) || []).map(d => ({id: d.id, name: d.name, type: d.type || "", active: !!d.is_active, local: false}));
      } catch (e) { /* no list: the deck says so */ }
    }
    const k = JSON.stringify(items);
    if (!force && k === lastDevs) return;
    lastDevs = k; post({type: "devices", items});
  }
  async function transfer(c) {
    try {
      const C = Spicetify.Platform && Spicetify.Platform.ConnectAPI;
      if (C && c.local && C.pullToLocal) await C.pullToLocal();
      else if (C && C.transferPlayback) await C.transferPlayback(c.id, {});
      else await Spicetify.CosmosAsync.put("https://api.spotify.com/v1/me/player", {device_ids: [c.id], play: true});
    } catch (e) {
      try { await Spicetify.CosmosAsync.put("https://api.spotify.com/v1/me/player", {device_ids: [c.id], play: true}); } catch (e2) { /* stays where it was */ }
    }
    setTimeout(() => { devicesPost(true); state(true); pos(true); }, 1200);
  }

  async function lyricsFor(id) {
    // Spotify's own request path first (what its lyrics view uses: it gets the syllable timing where Spotify has it),
    // then a plain fetch with the app's headers, then the old Cosmos call
    const host = "https://spclient.wg.spotify.com/color-lyrics/v2";
    try {
      const RB = Spicetify.Platform && Spicetify.Platform.RequestBuilder;
      if (RB && RB.build) {
        const res = await RB.build().withHost(host).withPath(`/track/${encodeURIComponent(id)}`)
          .withQueryParameters({format: "json", vocalRemoval: false}).withEndpointIdentifier("/track/{trackId}").send();
        if (res && res.body && res.body.lyrics) return [res.body, "rb"];
      }
    } catch (e) { /* older Spotify, or no lyrics */ }
    try {
      const P = Spicetify.Platform, tok = P.AuthorizationAPI.getState().token.accessToken;
      const r = await fetch(`${host}/track/${id}?format=json&vocalRemoval=false&market=from_token`, {headers: {
        Authorization: `Bearer ${tok}`, "App-Platform": (P.PlatformData && P.PlatformData.app_platform) || "Win32_x86_64",
        "Spotify-App-Version": P.version}});
      if (r.ok) { const j = await r.json(); if (j && j.lyrics) return [j, "fetch"]; }
    } catch (e) { /* fall through */ }
    return [await Spicetify.CosmosAsync.get(`${host}/track/${id}?format=json&vocalRemoval=false&market=from_token`), "cosmos"];
  }

  async function song() {
    const s = info();
    if (!s || s.uri === cur) return;
    cur = s.uri; title = s.title;
    let lines = null, sync = "none", colors = null, via = "", nsyl = 0;
    try {
      if (s.uri && s.uri.startsWith("spotify:track:")) {
        const got = await lyricsFor(s.uri.split(":")[2]);
        const r = got && got[0], L = r && r.lyrics;
        via = got ? got[1] : "";
        if (L && L.syncType && L.syncType !== "UNSYNCED" && Array.isArray(L.lines)) {
          sync = L.syncType;
          lines = L.lines.map(l => {
            const row = [Number(l.startTimeMs) / 1000, (l.words || "").trim() === "♪" ? "" : (l.words || "")];
            const sy = Array.isArray(l.syllables) ? l.syllables.filter(x => x && typeof x === "object" && x.startTimeMs != null) : [];
            if (sy.length) {             // word-by-word / syllable timing (Spotify's karaoke lyrics)
              row.push(sy.map(x => [Number(x.startTimeMs) / 1000, Number(x.numChars || x.charCount || 0),
                                    x.endTimeMs != null && Number(x.endTimeMs) > 0 ? Number(x.endTimeMs) / 1000 : null]));
              nsyl += sy.length;
            }
            return row;
          });
        }
        if (r && r.colors) colors = r.colors;
      }
    } catch (e) { /* no lyrics for this track (or offline): the island falls back to LRCLIB */ }
    if (cur !== s.uri) return;                  // skipped while we were fetching
    post(Object.assign({type: "song", lines, sync, colors, via, nsyl}, s));
    pos(true);
    // Spicy Lyrics (if installed) times each word / syllable: use it when Spotify only has line timing
    if (sync !== "SYLLABLE_SYNCED" && s.uri && s.uri.startsWith("spotify:track:")) spicyWatch(s, colors);
  }

  function spicyWatch(s, colors) {
    const id = s.uri.split(":")[2];
    [700, 2500, 6000, 12000, 20000].forEach((ms, k, all) => setTimeout(async () => {
      if (cur !== s.uri || spicyDone === s.uri) return;
      const got = await spicy(id);
      if (got && cur === s.uri && spicyDone !== s.uri) {
        spicyDone = s.uri;
        const nsyl = got[0].reduce((a, l) => a + (l[2] ? l[2].length : 0), 0);
        post(Object.assign({type: "song", lines: got[0], sync: got[1], colors, via: "spicy", nsyl}, s));
      } else if (k === all.length - 1 && cur === s.uri) {      // v8: say why there was nothing (the island logs it)
        post({type: "spicyinfo", uri: s.uri, title: s.title, stores: await spicyStores(true), v: VER});
      }
    }, ms));
  }
  let spicyDone = null;

  // v8: every Spicy Lyrics lyrics cache, newest name first (it was "SpicyLyrics_LyricsStore_g1"; an update can rename it)
  async function spicyStores(withCount) {
    try {
      if (!window.caches) return [];
      const names = (await caches.keys()).filter(n => /spicy/i.test(n) && /lyric/i.test(n)).sort().reverse();
      if (!withCount) return names;
      const out = [];
      for (const n of names) { try { out.push(n + ":" + (await (await caches.open(n)).keys()).length); } catch (e) { out.push(n + ":?"); } }
      return out;
    } catch (e) { return []; }
  }

  async function spicy(id) {            // Spicy Lyrics keeps each track's lyrics in its cache (seconds, per syllable)
    try {
      for (const name of await spicyStores(false)) {
        const c = await caches.open(name);
        let r = await c.match("/" + id);
        if (!r) { const hit = (await c.keys()).find(q => q.url.endsWith("/" + id) || q.url.includes(id)); if (hit) r = await c.match(hit); }
        if (!r) continue;
        let w = await r.json();
        if (w && w.ExpiresAt && w.ExpiresAt < Date.now()) continue;
        let L = w && w.Content !== undefined && !Array.isArray(w.Content) ? w.Content : w;
        if (typeof L === "string") { try { L = JSON.parse(L); } catch (e) { continue; } }
        const got = fromSpicy(L);
        if (got) return got;
      }
    } catch (e) {}
    return null;
  }

  function fromSpicy(L) {
    if (!L || !Array.isArray(L.Content)) return null;
    let lines = [];
    if (L.Type === "Syllable") {
      for (const c of L.Content) {
        const lead = c && c.Lead, sy = lead && Array.isArray(lead.Syllables) ? lead.Syllables.filter(x => x && x.StartTime != null) : [];
        if (!sy.length) continue;
        let text = "";
        const syl = sy.map((x, i) => {
          const t = String(x.Text || ""); text += t; if (i < sy.length - 1 && !x.IsPartOfWord) text += " ";
          return [+x.StartTime, t.replace(/\s/g, "").length, x.EndTime != null ? +x.EndTime : null];
        });
        lines.push([+(lead.StartTime != null ? lead.StartTime : sy[0].StartTime), text.trim(), syl,
                    +(lead.EndTime != null ? lead.EndTime : (sy[sy.length - 1].EndTime || 0)) || null]);
      }
    } else if (L.Type === "Line") {
      lines = L.Content.filter(c => c && c.Text != null && c.StartTime != null)
        .map(c => [+c.StartTime, String(c.Text), null, c.EndTime != null ? +c.EndTime : null]);
    }
    if (!lines.length) return null;
    lines.sort((a, b) => a[0] - b[0]);
    const out = [];                      // a long gap after a line = instrumental: show the dots, not the old line
    lines.forEach((l, i) => { out.push(l); const nx = lines[i + 1]; if (nx && l[3] && nx[0] - l[3] > 4) out.push([l[3] + .4, ""]); });
    return [out, L.Type === "Syllable" ? "SPICY_SYLLABLE" : "SPICY_LINE"];
  }

  function pos(force) {
    if (!cur) return;
    const now = Date.now(), p = (P.getProgress() || 0) / 1000, playing = !!P.isPlaying();
    const expect = last.pos + (last.playing ? (now - last.at) / 1000 : 0);
    const jumped = Math.abs(p - expect) > 1.2 || playing !== last.playing;
    if (!force && !jumped && now - last.at < 900) return;
    last = {pos: p, at: now, playing};
    const cn = connect();
    post({type: "pos", uri: cur, title, pos: p, playing, remote: cn.remote, dev: cn.dev, v: VER}).then(r => {
      if (r && r.status === 205) { cur = null; lastState = ""; song().then(() => state(true)); }   // the island restarted: send it all again
    });
  }

  // heart / shuffle / repeat / up next: sent when they change (checked every 2 s, and right after a song change)
  let lastState = "";
  function state(force) {
    if (!cur) return;
    let st;
    try {
      const q = (Spicetify.Queue && Spicetify.Queue.nextTracks) || [];
      const next = q.map(t => t && t.contextTrack).filter(t => t && t.uri && !t.uri.startsWith("spotify:delimiter") && t.metadata && t.metadata.title)
        .slice(0, 3).map(t => ({t: t.metadata.title, a: t.metadata.artist_name || ""}));
      const cn = connect();
      st = {type: "state", uri: cur, title, liked: !!P.getHeart(), shuffle: !!P.getShuffle(), repeat: P.getRepeat() | 0, next,
            remote: cn.remote, dev: cn.dev, vol: cn.remote && P.getVolume ? Math.round(P.getVolume() * 100) / 100 : null,
            ctx: String((P.data && ((P.data.context && P.data.context.uri) || P.data.context_uri)) || ""), v: VER};
    } catch (e) { return; }
    const k = JSON.stringify(st);
    if (!force && k === lastState) return;
    lastState = k; post(st);
  }
  setInterval(() => { state(false); devicesPost(false); }, 2000);

  // search (for the deck): Spotify's own search first, the Web API as a fallback
  const pickArt = srcs => {
    const a = (srcs || []).filter(x => x && x.url).sort((x, y) => (x.width || 300) - (y.width || 300));
    return (a.find(x => (x.width || 300) >= 160) || a[a.length - 1] || {}).url || "";
  };
  function fromGql(r) {
    const d = r && r.data && (r.data.searchV2 || r.data.search);
    const items = (d && ((d.tracksV2 && d.tracksV2.items) || (d.tracks && d.tracks.items))) || [];
    return items.map(x => (x && ((x.item && x.item.data) || (x.itemV2 && x.itemV2.data) || x.data || x)) || null)
      .filter(t => t && typeof t.uri === "string" && t.uri.startsWith("spotify:track:"))
      .map(t => ({uri: t.uri, t: t.name || "", a: ((t.artists && t.artists.items) || []).map(a => a.profile && a.profile.name).filter(Boolean).join(", "),
                  img: pickArt(t.albumOfTrack && t.albumOfTrack.coverArt && t.albumOfTrack.coverArt.sources),
                  dur: ((t.duration && t.duration.totalMilliseconds) || 0) / 1000}));
  }
  async function search(q) {
    const G = Spicetify.GraphQL, errs = [];
    const vars = {searchTerm: q, offset: 0, limit: 12, numberOfTopResults: 5, includeAudiobooks: false, includeArtistHasConcertsField: false,
                  includePreReleases: false, includeLocalConcertsField: false, includeAuthors: false};
    for (const name of ["searchTracks", "searchDesktop"]) {
      try {
        if (!G || !G.Request || !G.Definitions || !G.Definitions[name]) continue;
        const items = fromGql(await G.Request(G.Definitions[name], vars));
        if (items.length) return items;
      } catch (e) { errs.push(name + ": " + (e && e.message || e)); }
    }
    try {
      const r = await Spicetify.CosmosAsync.get("https://api.spotify.com/v1/search?type=track&limit=12&q=" + encodeURIComponent(q));
      const items = ((r && r.tracks && r.tracks.items) || []).map(t => ({uri: t.uri, t: t.name, a: (t.artists || []).map(a => a.name).join(", "),
        img: pickArt(t.album && t.album.images), dur: (t.duration_ms || 0) / 1000}));
      if (items.length) return items;
    } catch (e) { errs.push("web api: " + (e && e.message || e)); }
    if (errs.length) throw new Error(errs.join(" | ").slice(0, 300));
    return [];
  }
  async function runSearch(c) {
    try { post({type: "search", id: c.id, q: c.q, items: (await search(String(c.q || "").slice(0, 120))).slice(0, 12)}); }
    catch (e) { post({type: "search", id: c.id, q: c.q, items: [], error: String(e && e.message || e).slice(0, 300)}); }
  }

  // his library (for the deck): playlists + Liked Songs, their songs, play one in its playlist
  const imgUrl = u => {
    u = String(u || "");
    if (u.startsWith("spotify:image:")) return "https://i.scdn.co/image/" + u.slice(14);
    if (u.startsWith("spotify:mosaic:")) return "https://mosaic.scdn.co/300/" + u.slice(15).split(":").join("");
    return /^https:\/\//.test(u) ? u : "";
  };
  const IMG_PX = {xsmall: 32, small: 64, standard: 300, medium: 300, default: 300, large: 640, xlarge: 1000};
  const pickImg = (arr, want = 280) => {         // the smallest picture that is still sharp at that size
    const px = i => i.width || IMG_PX[String(i.label || "").toLowerCase()] || 300;
    const a = (arr || []).filter(x => x && x.url).sort((x, y) => px(x) - px(y));
    const x = a.find(i => px(i) >= want) || a[a.length - 1];
    return x ? imgUrl(x.url) : "";
  };
  const trk = t => ({uri: t.uri, t: String(t.name || ""), a: (t.artists || []).map(a => a && a.name).filter(Boolean).join(", ") || (t.album && t.album.artist && t.album.artist.name) || "",
                     img: pickImg((t.album && t.album.images) || t.images, 60), dur: ((t.duration && t.duration.milliseconds) || t.duration_ms || 0) / 1000});
  async function lib() {
    const out = [], seen = new Set(), errs = [], Pf = Spicetify.Platform || {};
    const add = p => { if (!p || !p.uri || seen.has(p.uri)) return; seen.add(p.uri);
      const by = (p.owner && (p.owner.name || p.owner.displayName)) || (p.artists && p.artists[0] && p.artists[0].name) || "";
      out.push({uri: p.uri, name: String(p.name || ""), img: pickImg(p.images), type: String(p.type || "playlist"), by: String(by), pin: !!p.pinned}); };
    try {
      const L = Pf.LibraryAPI;
      if (L && L.getContents) {
        const sortOrder = L.getRecentsSortOrderId ? L.getRecentsSortOrderId() : undefined;
        const walk = async (folderUri, depth) => {
          const r = await L.getContents({folderUri, sortOrder, limit: 500, offset: 0});
          for (const x of (r && r.items) || []) {
            if (x.type === "folder") { if (depth < 4) await walk(x.uri, depth + 1); }
            else if (x.type === "playlist" || x.type === "album") add(x);
          }
        };
        await walk(undefined, 0);
      }
    } catch (e) { errs.push("library: " + (e && e.message || e)); }
    if (!out.length) try {
      const walk = items => { for (const x of items || []) { if (x.type === "folder") walk(x.items); else if (x.type === "playlist") add(x); } };
      if (Pf.RootlistAPI && Pf.RootlistAPI.getContents) walk((await Pf.RootlistAPI.getContents()).items);
    } catch (e) { errs.push("rootlist: " + (e && e.message || e)); }
    if (!out.length) try {
      const r = await Spicetify.CosmosAsync.get("https://api.spotify.com/v1/me/playlists?limit=50");
      for (const x of (r && r.items) || []) add({uri: x.uri, name: x.name, images: x.images, type: "playlist"});
    } catch (e) { errs.push("web api: " + (e && e.message || e)); }
    let liked = 0;
    try { const r = await Pf.LibraryAPI.getTracks({limit: 1, offset: 0}); liked = (r && (r.totalLength || r.total)) || 0; } catch (e) {}
    post({type: "lib", items: out.slice(0, 300), liked, error: out.length ? "" : errs.join(" | ").slice(0, 300)});
  }
  async function tracks(c) {
    const uri = String(c.uri || ""), off = Math.max(0, c.offset | 0), lim = 100, Pf = Spicetify.Platform || {};
    let name = "", img = "", total = 0, rows = [], err = "";
    try {
      if (uri === "spotify:collection:tracks") {
        name = "Liked Songs";
        const r = await Pf.LibraryAPI.getTracks({limit: lim, offset: off});
        total = (r && (r.totalLength || r.total)) || 0;
        rows = ((r && r.items) || []).filter(t => t && t.uri).map(trk);
      } else if (uri.startsWith("spotify:playlist:")) {
        const A = Pf.PlaylistAPI;
        const [meta, r] = await Promise.all([off ? null : A.getMetadata(uri).catch(() => null), A.getContents(uri, {limit: lim, offset: off})]);
        if (meta) { name = String(meta.name || ""); img = pickImg(meta.images); }
        total = (r && (r.totalLength || r.total)) || 0;
        rows = ((r && r.items) || []).filter(t => t && t.uri && (t.type === "track" || t.type === "episode" || !t.type)).map(trk);
      } else if (uri.startsWith("spotify:album:")) {
        const a = await Spicetify.CosmosAsync.get("https://api.spotify.com/v1/albums/" + uri.split(":")[2]);
        name = a.name || ""; img = pickImg(a.images); total = (a.tracks && a.tracks.total) || 0;
        rows = ((a.tracks && a.tracks.items) || []).map(t => Object.assign(trk(t), {img}));
      }
    } catch (e) {
      err = String(e && e.message || e);
      if (uri.startsWith("spotify:playlist:")) try {            // the Web API as a fallback
        const r = await Spicetify.CosmosAsync.get(`https://api.spotify.com/v1/playlists/${uri.split(":")[2]}/tracks?offset=${off}&limit=${lim}`);
        total = r.total || 0; rows = (r.items || []).map(x => x && x.track).filter(t => t && t.uri).map(trk); err = "";
      } catch (e2) { err += " | web api: " + (e2 && e2.message || e2); }
    }
    post({type: "tracks", uri, offset: off, total, name, img, items: rows, error: rows.length ? "" : err.slice(0, 300)});
  }
  async function playCtx(c) {
    try {
      if (c.shuffle != null && !!P.getShuffle() !== !!c.shuffle) { if (P.setShuffle) P.setShuffle(!!c.shuffle); else P.toggleShuffle(); }
    } catch (e) {}
    const ctxs = [c.ctx], user = (Spicetify.Platform && Spicetify.Platform.username) || "";
    if (c.ctx === "spotify:collection:tracks" && user) ctxs.push(`spotify:user:${user}:collection`);
    for (const ctx of ctxs) {
      try {
        if (c.uri && /^spotify:(track|episode):/.test(c.uri)) await P.playUri(ctx, {}, {skipTo: {uri: c.uri}});
        else await P.playUri(ctx);
        return;
      } catch (e) {}
    }
    if (c.uri) try { await P.playUri(c.uri); } catch (e) {}
  }

  // commands from the island (long-poll: one request waits until there is something to do)
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  async function commands() {
    for (;;) {
      try {
        const r = await fetch("http://127.0.0.1:47813/cmd", {cache: "no-store"});
        if (r.status === 200) {
          const c = await r.json();
          if (c && Date.now() / 1000 - c.t < 5) {
            if (c.c === "like") P.toggleHeart();
            else if (c.c === "shuffle") P.toggleShuffle();
            else if (c.c === "repeat") P.toggleRepeat();
            else if (c.c === "search") runSearch(c);
            else if (c.c === "play" && /^spotify:track:/.test(c.uri || "")) P.playUri(c.uri);
            else if (c.c === "queue" && /^spotify:track:/.test(c.uri || "")) Spicetify.addToQueue([{uri: c.uri}]);
            else if (c.c === "devices") devicesPost(true);
            else if (c.c === "lib") lib();
            else if (c.c === "tracks" && /^spotify:/.test(c.uri || "")) tracks(c);
            else if (c.c === "playctx" && /^spotify:/.test(c.ctx || "")) playCtx(c);
            else if (c.c === "transfer" && c.id) transfer(c);
            else if (c.c === "toggle") P.togglePlay();
            else if (c.c === "next") P.next();
            else if (c.c === "prev") P.back();
            else if (c.c === "seek" && isFinite(c.pos)) P.seek(Math.max(0, c.pos * 1000));
            else if (c.c === "vol" && isFinite(c.v) && P.setVolume) P.setVolume(Math.max(0, Math.min(1, c.v)));
            setTimeout(() => state(true), 350);
          }
        } else if (r.status !== 204) await sleep(5000);
      } catch (e) { await sleep(5000); }       // island not running right now
    }
  }
  commands();

  P.addEventListener("songchange", () => { setTimeout(song, 250); setTimeout(() => state(true), 1200); });
  P.addEventListener("onplaypause", () => pos(true));
  P.addEventListener("onprogress", () => pos(false));
  setInterval(() => pos(false), 1000);
  setInterval(() => { if (!cur) post({type: "hi", v: VER}); }, 3000);   // nothing loaded yet: still tell the island we're here          // keeps the island in sync even if progress events go quiet
  song();
})();
