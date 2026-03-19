/**
 * Vercel Edge Function — Reddit proxy
 *
 * Pulls top posts from r/openclaw, r/clawdbot, and r/LocalLLaMA.
 * r/openclaw and r/clawdbot fetch all top posts (no keyword filter needed).
 * r/LocalLLaMA is filtered by Claw keywords.
 * Merges, deduplicates by post ID, sorts by score.
 *
 * Response: { posts: Post[] }
 * Cached 5 minutes server-side via Cache-Control s-maxage.
 */

export const config = { runtime: 'edge' };

const CLAW_QUERY = 'openclaw OR nanoclaw OR nemoclaw OR nanobot OR zeroclaw OR picoclaw';
const UA         = { 'User-Agent': 'ClawBeat/1.0 (clawbeat.co)' };

export default async function handler(req) {
  const sources = [
    { label: 'r/openclaw',   url: 'https://www.reddit.com/r/openclaw/hot.json?limit=25' },
    { label: 'r/clawdbot',   url: 'https://www.reddit.com/r/clawdbot/hot.json?limit=25' },
    { label: 'r/LocalLLaMA', url: `https://www.reddit.com/r/LocalLLaMA/search.json?q=${encodeURIComponent(CLAW_QUERY)}&sort=top&t=all&limit=15&restrict_sr=1` },
  ];

  const rawResponses = await Promise.all(
    sources.map(s =>
      fetch(s.url, { headers: UA })
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
      url:          p.url,
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
      'Cache-Control':               'no-store',   // disable cache while debugging
      'Access-Control-Allow-Origin': '*',
    },
  });
}
