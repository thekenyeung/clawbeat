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
  const fetches = [
    // r/openclaw — just pull top posts, all are relevant
    fetch('https://www.reddit.com/r/openclaw/top.json?limit=15', { headers: UA })
      .then(r => r.ok ? r.json() : { data: { children: [] } })
      .catch(() => ({ data: { children: [] } })),

    // r/clawdbot — same, pull top posts directly
    fetch('https://www.reddit.com/r/clawdbot/top.json?limit=15', { headers: UA })
      .then(r => r.ok ? r.json() : { data: { children: [] } })
      .catch(() => ({ data: { children: [] } })),

    // r/LocalLLaMA — filter by Claw keywords, all time
    fetch(
      `https://www.reddit.com/r/LocalLLaMA/search.json?q=${encodeURIComponent(CLAW_QUERY)}&sort=top&t=all&limit=10&restrict_sr=1`,
      { headers: UA }
    ).then(r => r.ok ? r.json() : { data: { children: [] } })
     .catch(() => ({ data: { children: [] } })),
  ];

  const results = await Promise.all(fetches);

  // Deduplicate by post ID, keep highest score copy
  const seen = new Map();
  for (const result of results) {
    for (const child of result?.data?.children || []) {
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
    .sort((a, b) => b.score - a.score)
    .slice(0, 8);

  return new Response(JSON.stringify({ posts }), {
    headers: {
      'Content-Type':                'application/json',
      'Cache-Control':               'public, s-maxage=300, stale-while-revalidate=60',
      'Access-Control-Allow-Origin': '*',
    },
  });
}
