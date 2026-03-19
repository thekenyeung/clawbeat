/**
 * Vercel Edge Function — Reddit proxy
 *
 * Fetches Reddit Atom/RSS feeds — no OAuth required, bypasses IP blocks.
 * Sources: r/openclaw (hot), r/clawdbot (hot), r/LocalLLaMA (keyword search).
 * Parses XML with regex; sorts by date (most recent first); returns top 5.
 *
 * If REDDIT_CLIENT_ID + REDDIT_CLIENT_SECRET env vars are set, switches to
 * the authenticated JSON API (oauth.reddit.com) for vote counts.
 *
 * Response: { posts: Post[], _debug: DebugEntry[] }
 */

export const config = { runtime: 'edge' };

const CLAW_QUERY = 'openclaw OR nanoclaw OR nemoclaw OR nanobot OR zeroclaw OR picoclaw';
const UA_RSS     = 'Mozilla/5.0 (compatible; ClawBeat/1.0; +https://clawbeat.co)';
const UA_OAUTH   = 'web:clawbeat:1.0 (by /u/thekenyeung)';

// ── RSS parsing ──────────────────────────────────────────────────────────────

function extractAll(xml, tag) {
  const re = new RegExp(`<${tag}[^>]*>([\\s\\S]*?)<\\/${tag}>`, 'gi');
  const matches = [];
  let m;
  while ((m = re.exec(xml)) !== null) matches.push(m[1].trim());
  return matches;
}

function extractAttr(xml, tag, attr) {
  const re = new RegExp(`<${tag}[^>]*\\s${attr}="([^"]*)"`, 'i');
  const m = re.exec(xml);
  return m ? m[1] : null;
}

function parseAtomFeed(xml, subreddit) {
  // Split on <entry> boundaries
  const entries = xml.split(/<entry[\s>]/i).slice(1);
  return entries.map((entry, i) => {
    const title = extractAll(entry, 'title')[0] || '';
    // Atom uses <link href="..."/> (self-closing)
    const link  = extractAttr(entry, 'link', 'href') || '';
    const name  = extractAll(entry, 'name')[0] || '';
    const updated = extractAll(entry, 'updated')[0] || '';
    // id looks like: t3_abc123
    const idRaw = extractAll(entry, 'id')[0] || '';
    const id    = idRaw.split('_').pop() || String(i);
    return {
      id,
      title:        title.replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&#39;/g, "'").replace(/&quot;/g, '"'),
      permalink:    link.startsWith('http') ? link : `https://reddit.com${link}`,
      subreddit,
      score:        null,   // not available in RSS
      num_comments: null,
      author:       name.replace(/^\/u\//, ''),
      created_utc:  updated ? new Date(updated).getTime() / 1000 : 0,
    };
  }).filter(p => p.title && p.permalink);
}

async function fetchRss(url, subreddit) {
  try {
    const r = await fetch(url, {
      headers: { 'User-Agent': UA_RSS, 'Accept': 'application/rss+xml, application/atom+xml, text/xml' },
    });
    const text = await r.text();
    return { status: r.status, posts: r.ok ? parseAtomFeed(text, subreddit) : [], raw: r.ok ? null : text.slice(0, 200) };
  } catch (e) {
    return { status: 0, posts: [], error: String(e) };
  }
}

// ── OAuth JSON API (if credentials available) ────────────────────────────────

async function getRedditToken(clientId, clientSecret) {
  const creds = btoa(`${clientId}:${clientSecret}`);
  const r = await fetch('https://www.reddit.com/api/v1/access_token', {
    method: 'POST',
    headers: { 'Authorization': `Basic ${creds}`, 'User-Agent': UA_OAUTH, 'Content-Type': 'application/x-www-form-urlencoded' },
    body: 'grant_type=client_credentials',
  });
  if (!r.ok) throw new Error(`Token ${r.status}`);
  return (await r.json()).access_token;
}

async function fetchJson(url, token) {
  try {
    const r = await fetch(url, { headers: { 'Authorization': `Bearer ${token}`, 'User-Agent': UA_OAUTH } });
    const body = r.ok ? await r.json() : null;
    const children = body?.data?.children || [];
    return {
      status: r.status,
      posts: children.map(c => {
        const p = c.data;
        return {
          id:           p.id,
          title:        p.title,
          permalink:    `https://reddit.com${p.permalink}`,
          subreddit:    p.subreddit,
          score:        p.score,
          num_comments: p.num_comments,
          author:       p.author,
          created_utc:  p.created_utc,
        };
      }).filter(p => !p.over_18),
      count: children.length,
    };
  } catch (e) {
    return { status: 0, posts: [], count: 0, error: String(e) };
  }
}

// ── Handler ──────────────────────────────────────────────────────────────────

export default async function handler(_req) {
  const clientId     = process.env.REDDIT_CLIENT_ID;
  const clientSecret = process.env.REDDIT_CLIENT_SECRET;
  const useOAuth     = !!(clientId && clientSecret);

  let allPosts = [];
  let _debug   = [];

  if (useOAuth) {
    // ── Authenticated JSON path ──
    let token;
    try { token = await getRedditToken(clientId, clientSecret); }
    catch (e) {
      return new Response(JSON.stringify({ posts: [], _debug: [{ error: `OAuth failed: ${e.message}` }] }), {
        status: 502, headers: { 'Content-Type': 'application/json' },
      });
    }
    const sources = [
      { label: 'r/openclaw',   url: 'https://oauth.reddit.com/r/openclaw/hot?limit=25' },
      { label: 'r/clawdbot',   url: 'https://oauth.reddit.com/r/clawdbot/hot?limit=25' },
      { label: 'r/LocalLLaMA', url: `https://oauth.reddit.com/r/LocalLLaMA/search?q=${encodeURIComponent(CLAW_QUERY)}&sort=top&t=all&limit=15&restrict_sr=1` },
    ];
    const results = await Promise.all(sources.map(s => fetchJson(s.url, token)));
    results.forEach((r, i) => {
      allPosts.push(...r.posts);
      _debug.push({ label: sources[i].label, status: r.status, count: r.count ?? r.posts.length, mode: 'oauth', error: r.error || null });
    });
  } else {
    // ── RSS fallback path ──
    const sources = [
      { label: 'r/openclaw',   url: 'https://www.reddit.com/r/openclaw/hot.rss?limit=25',    sub: 'openclaw' },
      { label: 'r/clawdbot',   url: 'https://www.reddit.com/r/clawdbot/hot.rss?limit=25',    sub: 'clawdbot' },
      { label: 'r/LocalLLaMA', url: `https://www.reddit.com/r/LocalLLaMA/search.rss?q=${encodeURIComponent(CLAW_QUERY)}&sort=top&t=all&restrict_sr=1`, sub: 'LocalLLaMA' },
    ];
    const results = await Promise.all(sources.map(s => fetchRss(s.url, s.sub)));
    results.forEach((r, i) => {
      allPosts.push(...r.posts);
      _debug.push({ label: sources[i].label, status: r.status, count: r.posts.length, mode: 'rss', error: r.error || r.raw || null });
    });
  }

  // Deduplicate by id
  const seen = new Map();
  for (const p of allPosts) {
    if (!seen.has(p.id) || (p.score ?? 0) > (seen.get(p.id).score ?? 0)) seen.set(p.id, p);
  }

  const posts = [...seen.values()]
    // Sort: by score if available (OAuth), else by recency (RSS)
    .sort((a, b) => {
      if (a.score !== null && b.score !== null) return (b.score + (b.num_comments||0) * 5) - (a.score + (a.num_comments||0) * 5);
      return (b.created_utc || 0) - (a.created_utc || 0);
    })
    .slice(0, 5);

  return new Response(JSON.stringify({ posts, _debug }), {
    headers: {
      'Content-Type':                'application/json',
      'Cache-Control':               'public, s-maxage=300, stale-while-revalidate=60',
      'Access-Control-Allow-Origin': '*',
    },
  });
}
