/**
 * Vercel Edge Function — Reddit proxy
 *
 * Does a global Reddit search for Claw family keywords across all subreddits,
 * then also pulls hot posts from r/LocalLLaMA and r/MachineLearning to round
 * out the feed. Merges and deduplicates by score.
 *
 * Query params:
 *   t  — Reddit time filter: week | month | year  (default: week)
 *
 * Response: { posts: Post[] }
 * Cached 5 minutes server-side via Cache-Control s-maxage.
 */

export const config = { runtime: 'edge' };

const KEYWORD_QUERY = 'openclaw OR nanoclaw OR nemoclaw OR "nano claw" OR "open claw" OR "agentic AI framework"';
const BROAD_SUBS    = ['LocalLLaMA', 'MachineLearning'];
const BROAD_QUERY   = 'agentic AI agents framework';

export default async function handler(req) {
  const { searchParams } = new URL(req.url);
  const t = ['week', 'month', 'year'].includes(searchParams.get('t'))
    ? searchParams.get('t')
    : 'week';

  const UA = { 'User-Agent': 'ClawBeat/1.0 (clawbeat.co)' };

  // 1. Global keyword search — finds Claw mentions anywhere on Reddit
  const keywordFetch = fetch(
    `https://www.reddit.com/search.json?q=${encodeURIComponent(KEYWORD_QUERY)}&sort=top&t=${t}&limit=25`,
    { headers: UA }
  ).then(r => r.ok ? r.json() : { data: { children: [] } })
   .catch(() => ({ data: { children: [] } }));

  // 2. Broad agentic-AI search in r/LocalLLaMA + r/MachineLearning (no restrict)
  const subFetches = BROAD_SUBS.map(sub =>
    fetch(
      `https://www.reddit.com/r/${sub}/search.json?q=${encodeURIComponent(BROAD_QUERY)}&sort=top&t=${t}&limit=10`,
      { headers: UA }
    ).then(r => r.ok ? r.json() : { data: { children: [] } })
     .catch(() => ({ data: { children: [] } }))
  );

  const [keywordResult, ...subResults] = await Promise.all([keywordFetch, ...subFetches]);

  const allChildren = [
    ...(keywordResult?.data?.children || []),
    ...subResults.flatMap(r => r?.data?.children || []),
  ];

  // Deduplicate by post ID, keep highest score copy
  const seen = new Map();
  for (const child of allChildren) {
    const p = child?.data;
    if (!p?.id) continue;
    if (!seen.has(p.id) || p.score > seen.get(p.id).score) seen.set(p.id, p);
  }

  const posts = [...seen.values()]
    .filter(p => !p.over_18 && !p.removed_by_category && p.score > 0)
    .map(p => ({
      id:               p.id,
      title:            p.title,
      url:              p.url,
      permalink:        `https://reddit.com${p.permalink}`,
      subreddit:        p.subreddit,
      score:            p.score,
      num_comments:     p.num_comments,
      author:           p.author,
      created_utc:      p.created_utc,
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
