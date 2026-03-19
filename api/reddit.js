/**
 * Vercel Edge Function — Reddit proxy
 *
 * Uses Reddit app-only OAuth (client_credentials) to avoid 403s from
 * Vercel's datacenter IPs. Requires REDDIT_CLIENT_ID + REDDIT_CLIENT_SECRET
 * env vars (set in Vercel dashboard).
 *
 * Sources:
 *   r/openclaw + r/clawdbot  — hot posts, all on-topic
 *   r/LocalLLaMA             — keyword-filtered, top all-time
 *
 * Ranked by score + num_comments×5 (upvotes + engagement).
 * Response: { posts: Post[], _debug: DebugEntry[] }
 */

export const config = { runtime: 'edge' };

const CLAW_QUERY   = 'openclaw OR nanoclaw OR nemoclaw OR nanobot OR zeroclaw OR picoclaw';
// Reddit requires a descriptive UA: platform:appid:version (by /u/user)
const USER_AGENT   = 'web:clawbeat:1.0 (by /u/thekenyeung)';

async function getRedditToken(clientId, clientSecret) {
  const creds = btoa(`${clientId}:${clientSecret}`);
  const r = await fetch('https://www.reddit.com/api/v1/access_token', {
    method:  'POST',
    headers: {
      'Authorization': `Basic ${creds}`,
      'User-Agent':    USER_AGENT,
      'Content-Type':  'application/x-www-form-urlencoded',
    },
    body: 'grant_type=client_credentials',
  });
  if (!r.ok) throw new Error(`Token fetch failed: ${r.status}`);
  const json = await r.json();
  return json.access_token;
}

export default async function handler(req) {
  const clientId     = process.env.REDDIT_CLIENT_ID;
  const clientSecret = process.env.REDDIT_CLIENT_SECRET;

  if (!clientId || !clientSecret) {
    return new Response(JSON.stringify({
      posts:  [],
      _debug: [{ error: 'REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET not set' }],
    }), { status: 500, headers: { 'Content-Type': 'application/json' } });
  }

  let token;
  try {
    token = await getRedditToken(clientId, clientSecret);
  } catch (e) {
    return new Response(JSON.stringify({
      posts:  [],
      _debug: [{ error: `OAuth failed: ${e.message}` }],
    }), { status: 502, headers: { 'Content-Type': 'application/json' } });
  }

  const authHeaders = {
    'Authorization': `Bearer ${token}`,
    'User-Agent':    USER_AGENT,
  };

  const sources = [
    { label: 'r/openclaw',   url: 'https://oauth.reddit.com/r/openclaw/hot?limit=25' },
    { label: 'r/clawdbot',   url: 'https://oauth.reddit.com/r/clawdbot/hot?limit=25' },
    { label: 'r/LocalLLaMA', url: `https://oauth.reddit.com/r/LocalLLaMA/search?q=${encodeURIComponent(CLAW_QUERY)}&sort=top&t=all&limit=15&restrict_sr=1` },
  ];

  const rawResponses = await Promise.all(
    sources.map(s =>
      fetch(s.url, { headers: authHeaders })
        .then(async r => ({ label: s.label, status: r.status, ok: r.ok, body: r.ok ? await r.json() : null }))
        .catch(e => ({ label: s.label, status: 0, ok: false, body: null, error: String(e) }))
    )
  );

  // Deduplicate by post ID, keep highest score copy
  const seen = new Map();
  for (const { body } of rawResponses) {
    for (const child of body?.data?.children || []) {
      const p = child?.data;
      if (!p?.id) continue;
      if (!seen.has(p.id) || p.score > seen.get(p.id).score) seen.set(p.id, p);
    }
  }

  const posts = [...seen.values()]
    .filter(p => !p.over_18 && !p.removed_by_category)
    .map(p => ({
      id:           p.id,
      title:        p.title,
      permalink:    `https://reddit.com${p.permalink}`,
      subreddit:    p.subreddit,
      score:        p.score,
      num_comments: p.num_comments,
      author:       p.author,
      created_utc:  p.created_utc,
    }))
    .sort((a, b) => (b.score + b.num_comments * 5) - (a.score + a.num_comments * 5))
    .slice(0, 5);

  const _debug = rawResponses.map(r => ({
    label:  r.label,
    status: r.status,
    count:  r.body?.data?.children?.length ?? 0,
    error:  r.error || null,
  }));

  return new Response(JSON.stringify({ posts, _debug }), {
    headers: {
      'Content-Type':                'application/json',
      'Cache-Control':               'public, s-maxage=300, stale-while-revalidate=60',
      'Access-Control-Allow-Origin': '*',
    },
  });
}
