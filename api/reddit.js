/**
 * Vercel Edge Function — Reddit proxy
 *
 * Fetches posts from r/OpenClaw, r/LocalLLaMA, r/MachineLearning that mention
 * Claw family keywords. Avoids browser CORS restrictions by running server-side.
 *
 * Query params:
 *   t  — Reddit time filter: week | month | year  (default: week)
 *   q  — Search query override (default: Claw family terms)
 *
 * Response: { posts: Post[] }
 * Cached 5 minutes server-side via Cache-Control s-maxage.
 */

export const config = { runtime: 'edge' };

const SUBREDDITS   = ['OpenClaw', 'LocalLLaMA', 'MachineLearning'];
const DEFAULT_QUERY = 'openclaw OR nanoclaw OR nemoclaw OR nanobot OR zeroclaw OR picoclaw';

export default async function handler(req) {
  const { searchParams } = new URL(req.url);
  const t = ['week', 'month', 'year'].includes(searchParams.get('t'))
    ? searchParams.get('t')
    : 'week';
  const q = searchParams.get('q') || DEFAULT_QUERY;

  const fetches = SUBREDDITS.map(sub =>
    fetch(
      `https://www.reddit.com/r/${sub}/search.json` +
      `?q=${encodeURIComponent(q)}&sort=top&t=${t}&limit=15&restrict_sr=1`,
      { headers: { 'User-Agent': 'ClawBeat/1.0 (clawbeat.co)' } }
    )
    .then(r => r.ok ? r.json() : { data: { children: [] } })
    .catch(() => ({ data: { children: [] } }))
  );

  const results = await Promise.allSettled(fetches);

  const posts = results
    .filter(r => r.status === 'fulfilled')
    .flatMap(r => r.value?.data?.children || [])
    .map(c => c.data)
    .filter(p => p && !p.over_18 && !p.removed_by_category && p.score > 0)
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
      selftext_preview: (p.selftext || '').slice(0, 200),
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
