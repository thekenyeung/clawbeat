declare global {
  interface Window {
    gtag: (...args: any[]) => void;
  }
}

import React, { useState, useEffect } from 'react';
import { createRoot } from 'react-dom/client';
import { createClient } from '@supabase/supabase-js';

const SUPABASE_URL  = 'https://twouuiapzrkezwbtylij.supabase.co';
const SUPABASE_ANON = 'sb_publishable_j-AmOSIuQPEeKIyYAOA2Gg_8ekguDsG';
const supabase = createClient(SUPABASE_URL, SUPABASE_ANON);
import {
  Newspaper,
  Video,
  Github,
  ExternalLink,
  Star,
  Calendar,
  ChevronLeft,
  Menu,
  X,
  BookOpen,
  Microscope,
  MapPin,
  Globe
} from 'lucide-react';
import whitelist from './src/whitelist.json';

// --- TYPES ---
type Page = 'news' | 'videos' | 'projects' | 'research' | 'events';
type SortCriteria = 'stars' | 'date';

interface NewsItem {
  title: string;
  summary: string;
  url: string;
  source: string;
  date: string;
  inserted_at?: string;
  source_type?: 'priority' | 'standard' | 'delist';
  moreCoverage?: Array<{ source: string; url: string }>;
  tags?: string[];
}

interface VideoItem {
  title: string;
  url: string;
  thumbnail?: string;
  channel: string;
  publishedAt: string;
  isPriority: boolean;
  description?: string;
}

interface ProjectItem {
  name: string;
  description: string;
  url: string;
  stars: number;
  owner: string;
  created_at: string;
}

interface ResearchItem {
  title: string;
  authors: string[];
  date: string;
  url: string;
  summary: string;
}

interface SpotlightOverride {
  dispatch_date: string;  // MM-DD-YYYY
  slot: number;           // 1–4
  url: string;
  title?: string;
  source?: string;
  summary?: string;
  tags?: string[];
}

interface EventItem {
  url: string;
  title: string;
  organizer: string;
  event_type: 'virtual' | 'in-person' | 'unknown';
  location_city: string;
  location_state: string;
  location_country: string;
  start_date: string;  // MM/DD/YYYY
  end_date: string;    // MM/DD/YYYY
  description: string;
}

// --- HELPERS ---
const formatDate = (dateString: string) => {
  try {
    const date = new Date(dateString);
    if (isNaN(date.getTime())) return dateString;
    const mm = String(date.getMonth() + 1).padStart(2, '0');
    const dd = String(date.getDate()).padStart(2, '0');
    const yyyy = date.getFullYear();
    return `${mm}-${dd}-${yyyy}`;
  } catch {
    return dateString;
  }
};

const formatSourceName = (name: string) => {
  if (!name) return "";
  const rawName = name.trim();
  const key = rawName.toLowerCase().replace(/[\s\.]/g, '');

  const manualFixes: Record<string, string> = {
    "npr": "NPR", "cnbc": "CNBC", "wbur": "WBUR", "techcrunch": "TechCrunch",
    "venturebeat": "VentureBeat", "businessinsider": "Business Insider",
    "thenewstack": "The New Stack", "nytimes": "The New York Times",
    "newyorktimes": "The New York Times", "thehill": "The Hill", "wsj": "WSJ",
    "wallstreetjournal": "Wall Street Journal", "mittechnologyreview": "MIT Tech Review",
    "streetinsider": "Street Insider", "Security.Com": "Security.com",
    "Observer.Com": "Observer", "Pymnts.Com": "Pymnts", "Cnn": "CNN",
    "Cnet": "CNET", "Ibm": "IBM", "The-Decoder.com": "Decoder", "Tom'S Hardware": "Tom's Hardware"
  };

  if (manualFixes[key]) return manualFixes[key];
  if (rawName === rawName.toUpperCase() && rawName.length <= 4) return rawName;

  let cleanName = rawName.replace(/([a-z])([A-Z])/g, '$1 $2');
  if (cleanName === cleanName.toLowerCase()) {
    return cleanName.charAt(0).toUpperCase() + cleanName.slice(1);
  }
  return cleanName;
};

const checkIfVerified = (item: NewsItem) => {
  return (whitelist as any[]).some(w => {
    const whitelistName = String(w["Source Name"] || "").toLowerCase().trim();
    const articleSource = String(item.source || "").toLowerCase().trim();
    if (whitelistName === articleSource) return true;
    const whitelistUrl = String(w["Website URL"] || "").toLowerCase().replace('https://', '').replace('http://', '').replace('www.', '');
    if (whitelistUrl && item.url?.toLowerCase().includes(whitelistUrl)) return true;
    return false;
  });
};

// Parse MM-DD-YYYY → timestamp (used for sorting by publication date)
const parseMDY = (d: string) => {
  const parts = (d || '').split('-').map(Number);
  const [m, day, y] = [parts[0] ?? 0, parts[1] ?? 0, parts[2] ?? 0];
  return isNaN(y) || y === 0 ? 0 : new Date(y, m - 1, day).getTime();
};

// "Today" as a timestamp in Pacific time (midnight Pacific), for dispatch cutoff
const getTodayPacific = (): number => {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: 'America/Los_Angeles',
    year: 'numeric', month: '2-digit', day: '2-digit',
  }).formatToParts(new Date());
  const y = parts.find(p => p.type === 'year')?.value ?? '0';
  const m = parts.find(p => p.type === 'month')?.value ?? '0';
  const d = parts.find(p => p.type === 'day')?.value ?? '0';
  return parseMDY(`${m}-${d}-${y}`);
};

// Format "YYYY-MM-DD HH:MM UTC" → Pacific time display string
const formatLastSyncPacific = (raw: string): string => {
  if (!raw) return '';
  const match = raw.match(/^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}) UTC$/);
  if (!match) return raw;
  const date = new Date(`${match[1]}T${match[2]}:00Z`);
  if (isNaN(date.getTime())) return raw;
  return date.toLocaleString('en-US', {
    timeZone: 'America/Los_Angeles',
    month: '2-digit', day: '2-digit', year: 'numeric',
    hour: '2-digit', minute: '2-digit',
    timeZoneName: 'short',
  });
};

const App: React.FC = () => {
  const urlTab = new URLSearchParams(window.location.search).get('tab') as Page | null;
  const [activePage, setActivePage] = useState<Page>(
    urlTab || (sessionStorage.getItem('activePage') as Page) || 'news'
  );
  
  const [currentPage, setCurrentPage] = useState(Number(sessionStorage.getItem('newsPage')) || 1);
  const [currentVideoPage, setCurrentVideoPage] = useState(Number(sessionStorage.getItem('videoPage')) || 1);
  const [currentProjectPage, setCurrentProjectPage] = useState(Number(sessionStorage.getItem('projectPage')) || 1);
  const [currentResearchPage, setCurrentResearchPage] = useState(Number(sessionStorage.getItem('researchPage')) || 1);
  const [currentEventsPage, setCurrentEventsPage] = useState(Number(sessionStorage.getItem('eventsPage')) || 1);

  const [sortBy, setSortBy] = useState<SortCriteria>((sessionStorage.getItem('projectSort') as SortCriteria) || 'date');
  const [isMobileMenuOpen, setIsMobileMenuOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lastUpdated, setLastUpdated] = useState<string>("");

  const [news, setNews] = useState<NewsItem[]>([]);
  const [videos, setVideos] = useState<VideoItem[]>([]);
  const [projects, setProjects] = useState<ProjectItem[]>([]);
  const [research, setResearch] = useState<ResearchItem[]>([]);
  const [events, setEvents] = useState<EventItem[]>([]);
  const [spotlightOverrides, setSpotlightOverrides] = useState<SpotlightOverride[]>([]);

  const [showScrollTop, setShowScrollTop] = useState(false);

  const newsPerPage = 20;
  const videosPerPage = 9; 
  const projectsPerPage = 20;
  const researchPerPage = 10;

  // Suppress pushState on first render and after popstate/logo resets
  const skipHistoryPush = React.useRef(true);

  // On mount: set initial history state without pushing a new entry
  useEffect(() => {
    history.replaceState({ newsPage: currentPage }, '', currentPage > 1 ? `/?page=${currentPage}` : '/');
  }, []); // eslint-disable-line

  // Push history entry on page changes; skip is consumed and cleared inside the effect
  useEffect(() => {
    if (skipHistoryPush.current) { skipHistoryPush.current = false; return; }
    const url = currentPage > 1 ? `/?page=${currentPage}` : '/';
    history.pushState({ newsPage: currentPage }, '', url);
  }, [currentPage]);

  // Restore page from browser back/forward
  useEffect(() => {
    const handlePop = (e: PopStateEvent) => {
      skipHistoryPush.current = true;   // effect will skip push and clear the flag
      setCurrentPage(e.state?.newsPage ?? 1);
    };
    window.addEventListener('popstate', handlePop);
    return () => window.removeEventListener('popstate', handlePop);
  }, []);

  useEffect(() => {
    const handleScroll = () => setShowScrollTop(window.scrollY > 800);
    window.addEventListener('scroll', handleScroll);
    return () => window.removeEventListener('scroll', handleScroll);
  }, []);

  const scrollToTop = () => window.scrollTo({ top: 0, behavior: 'smooth' });

  const trackEvent = (action: string, params: object) => {
    if (typeof window.gtag === 'function') window.gtag('event', action, params);
  };

  const handleLinkClick = (title: string, source: string, type: string = 'news_article') => {
    trackEvent('select_content', { content_type: type, item_id: title, content_source: source });
  };

  const handleNavClick = (page: Page) => {
    setActivePage(page);
    setIsMobileMenuOpen(false);
  };

  // Consolidation of Logo Click Reset Logic
  const handleLogoClick = (e?: React.MouseEvent) => {
    if (e) e.preventDefault();
    skipHistoryPush.current = true; // effect will skip push and clear the flag
    setCurrentPage(1);
    setCurrentVideoPage(1);
    setCurrentProjectPage(1);
    setCurrentResearchPage(1);
    setCurrentEventsPage(1);
    setActivePage('news');
    setIsMobileMenuOpen(false);
    history.pushState({ newsPage: 1 }, '', '/');
    window.scrollTo({ top: 0, behavior: 'smooth' });
  };

  useEffect(() => {
    sessionStorage.setItem('activePage', activePage);
    sessionStorage.setItem('newsPage', currentPage.toString());
    sessionStorage.setItem('videoPage', currentVideoPage.toString());
    sessionStorage.setItem('projectPage', currentProjectPage.toString());
    sessionStorage.setItem('researchPage', currentResearchPage.toString());
    sessionStorage.setItem('eventsPage', currentEventsPage.toString());
    sessionStorage.setItem('projectSort', sortBy);
  }, [activePage, currentPage, currentVideoPage, currentProjectPage, currentResearchPage, currentEventsPage, sortBy]);

  const fetchContent = async () => {
    setLoading(true);
    try {
      const [newsRes, videosRes, projectsRes, researchRes, eventsRes, metaRes, spotlightRes] = await Promise.all([
        supabase.from('news_items').select('*').order('inserted_at', { ascending: false }).limit(1000),
        supabase.from('videos').select('*').limit(300),
        supabase.from('github_projects').select('*').limit(100),
        supabase.from('research_papers').select('*').limit(100),
        supabase.from('events').select('*').limit(500),
        supabase.from('feed_metadata').select('*').eq('id', 1).maybeSingle(),
        supabase.from('spotlight_overrides').select('*'),
      ]);

      if (newsRes.error) throw newsRes.error;
      if (videosRes.error) throw videosRes.error;
      if (projectsRes.error) throw projectsRes.error;
      if (researchRes.error) throw researchRes.error;

      setLastUpdated(formatLastSyncPacific(metaRes.data?.last_updated || ''));

      // Map DB snake_case → frontend camelCase
      setNews((newsRes.data || []).map((item: any) => ({
        ...item,
        moreCoverage: item.more_coverage || [],
        tags: item.tags || [],
      })));

      setVideos((videosRes.data || [])
        .map((v: any) => ({ ...v, publishedAt: v.published_at || '' }))
        .sort((a: any, b: any) => parseMDY(b.publishedAt) - parseMDY(a.publishedAt)));

      setProjects(projectsRes.data || []);
      setResearch(researchRes.data || []);
      setEvents(eventsRes.data || []);
      setSpotlightOverrides(spotlightRes.data || []);
    } catch (err: any) {
      setError("Intelligence feed is currently updating...");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchContent();
  }, []);

  const sortedProjects = [...projects].sort((a, b) => 
    sortBy === 'stars' 
      ? (b.stars || 0) - (a.stars || 0) 
      : new Date(b.created_at).getTime() - new Date(a.created_at).getTime()
  );

  const sortedResearch = [...research].sort((a, b) => 
    new Date(b.date).getTime() - new Date(a.date).getTime()
  );

  // Sort all articles by publication date (MM-DD-YYYY) descending before paginating
  // Filter out any items dated after today in Pacific time (prevents early UTC-midnight scraper runs
  // from surfacing tomorrow's dispatch before Pacific midnight)
  const sortedNews = React.useMemo(
    () => {
      const todayPT = getTodayPacific();
      return [...news]
        .filter(item => !item.date || parseMDY(item.date) <= todayPT)
        .sort((a, b) => parseMDY(b.date) - parseMDY(a.date));
    },
    [news]
  );
  const currentNewsItems = sortedNews.slice((currentPage - 1) * newsPerPage, currentPage * newsPerPage);
  const totalNewsPages = Math.ceil(sortedNews.length / newsPerPage);

  // Determine which dispatch days show their spotlight on the current page:
  // a day's spotlight only appears on the page where that day's first article lands.
  const spotlightDays = React.useMemo(() => {
    const days = new Set<string>();
    const pageStart = (currentPage - 1) * newsPerPage;
    const pageEnd = currentPage * newsPerPage;
    const seen = new Set<string>();
    for (let i = 0; i < sortedNews.length; i++) {
      const day = sortedNews[i]!.date || 'unknown';
      if (!seen.has(day)) {
        seen.add(day);
        if (i >= pageStart && i < pageEnd) days.add(day);
      }
    }
    return days;
  }, [sortedNews, currentPage]);

  const currentVideoItems = videos.slice((currentVideoPage - 1) * videosPerPage, currentVideoPage * videosPerPage);
  const totalVideoPages = Math.ceil(videos.length / videosPerPage);

  const currentProjectItems = sortedProjects.slice((currentProjectPage - 1) * projectsPerPage, currentProjectPage * projectsPerPage);
  const totalProjectPages = Math.ceil(sortedProjects.length / projectsPerPage);

  const currentResearchItems = sortedResearch.slice((currentResearchPage - 1) * researchPerPage, currentResearchPage * researchPerPage);
  const totalResearchPages = Math.ceil(sortedResearch.length / researchPerPage);

  // Events: filter past (end_date < today) client-side, sort nearest upcoming first
  const eventsPerPage = 20;
  const parseMMDDYYYY = (d: string) => {
    const p = d.split('/').map(Number);
    return p.length === 3 ? new Date(p[2] ?? 0, (p[0] ?? 1) - 1, p[1] ?? 1) : new Date(0);
  };
  const todayMidnight = new Date(); todayMidnight.setHours(0, 0, 0, 0);
  const upcomingEvents = [...events]
    .filter(e => parseMMDDYYYY(e.end_date || e.start_date) >= todayMidnight)
    .sort((a, b) => parseMMDDYYYY(a.start_date).getTime() - parseMMDDYYYY(b.start_date).getTime());
  const currentEventItems = upcomingEvents.slice((currentEventsPage - 1) * eventsPerPage, currentEventsPage * eventsPerPage);
  const totalEventsPages = Math.ceil(upcomingEvents.length / eventsPerPage);

  return (
    <div className="min-h-screen bg-[#0a0a0c] text-slate-200 font-sans selection:bg-orange-500/30 selection:text-orange-200">
      <header className="header">
        <div className="header-inner">
          <button className="brand" onClick={handleLogoClick}>
            <div className="brand-img">
              <img src="/images/clawbeat-icon-claw-logo-512x512.jpg" alt="ClawBeat" />
            </div>
            <span className="brand-text">ClawBeat<span>.co</span></span>
          </button>
          <nav className="header-nav">
            <button className={`nav-item${activePage === 'news' ? ' active' : ''}`} onClick={() => handleNavClick('news')}>
              <Newspaper size={16} />Intel
            </button>
            <a href="/research.html" className="nav-item"><BookOpen size={16} />Research</a>
            <a href="/media.html" className="nav-item"><Video size={16} />Media</a>
            <a href="/forge.html" className="nav-item"><Github size={16} />Forge</a>
            <a href="/events-calendar.html" className="nav-item"><Calendar size={16} />Events</a>
          </nav>
          <button className="hamburger-btn" onClick={() => setIsMobileMenuOpen(!isMobileMenuOpen)}>
            {isMobileMenuOpen ? <X size={24} /> : <Menu size={24} />}
          </button>
        </div>
        <div className={`mobile-menu${isMobileMenuOpen ? ' open' : ''}`}>
          <button className={`mobile-nav-item${activePage === 'news' ? ' active' : ''}`} onClick={() => handleNavClick('news')}>
            <Newspaper size={16} />Intel Feed
          </button>
          <a href="/research.html" className="mobile-nav-item"><BookOpen size={16} />Research</a>
          <a href="/media.html" className="mobile-nav-item"><Video size={16} />Media Lab</a>
          <a href="/forge.html" className="mobile-nav-item"><Github size={16} />The Forge</a>
          <a href="/events-calendar.html" className="mobile-nav-item"><Calendar size={16} />Events</a>
        </div>
      </header>

      <main className="max-w-6xl mx-auto px-6 pt-8 pb-0">
        <div className="flex justify-between items-end pb-6">
          <div>
            <h2 className="text-4xl font-black text-white uppercase italic tracking-tighter leading-[1.1]">
              {activePage === 'news' && <>Ecosystem <span className="text-orange-500">Dispatch</span></>}
              {activePage === 'research' && <>Technical <span className="text-orange-500">Papers</span></>}
              {activePage === 'videos' && <>Visual <span className="text-orange-500">Stream</span></>}
              {activePage === 'projects' && <>The <span className="text-orange-500">Forge</span></>}
              {activePage === 'events' && <>Community <span className="text-orange-500">Events</span></>}
            </h2>
            <div className="flex flex-col gap-1 mt-3">
              <p style={{fontFamily:"'JetBrains Mono',monospace"}} className="text-[0.65rem] text-[#525866] uppercase tracking-[0.1em]">
                {activePage === 'research' ? 'ArXiv Intelligence & Semantic Scholar' : activePage === 'events' ? 'Upcoming OpenClaw Gatherings' : 'Autonomous Intelligence Curation'}
              </p>
              {lastUpdated && (
                <span style={{fontFamily:"'JetBrains Mono',monospace"}} className="text-[0.7rem] text-[#525866] whitespace-nowrap">
                  Last Sync: {lastUpdated}
                </span>
              )}
            </div>
          </div>
          {activePage === 'projects' && (
            <div className="flex gap-2 bg-white/5 p-1 rounded-lg">
              <SortButton active={sortBy === 'stars'} onClick={() => setSortBy('stars')} label="Top Rated" />
              <SortButton active={sortBy === 'date'} onClick={() => setSortBy('date')} label="Latest" />
            </div>
          )}
        </div>

        {loading ? (
          <div className="space-y-6 animate-pulse">
            {[...Array(6)].map((_, i) => <div key={i} className="h-24 bg-white/5 rounded-lg" />)}
          </div>
        ) : error ? (
          <div className="bg-red-500/10 border border-red-500/20 p-8 rounded-xl text-center">
            <p className="text-red-400 font-mono text-sm">{error}</p>
            <button onClick={() => fetchContent()} className="mt-4 text-xs text-slate-500 underline uppercase tracking-widest">Retry Sync</button>
          </div>
        ) : (
          <div className="min-h-[50vh] border-t border-white/[0.09] pt-6">
            {activePage === 'news' && (
              <>
                <NewsList items={currentNewsItems} allNews={sortedNews} onTrackClick={handleLinkClick} spotlightOverrides={spotlightOverrides} spotlightDays={spotlightDays} />
                <Pagination current={currentPage} total={totalNewsPages} onChange={setCurrentPage} />
              </>
            )}
            {activePage === 'research' && (
              <>
                <ResearchList items={currentResearchItems} onTrackClick={handleLinkClick} />
                <Pagination current={currentResearchPage} total={totalResearchPages} onChange={setCurrentResearchPage} />
              </>
            )}
            {activePage === 'videos' && (
              <>
                <VideoGrid items={currentVideoItems} onTrackClick={handleLinkClick} />
                <Pagination current={currentVideoPage} total={totalVideoPages} onChange={setCurrentVideoPage} />
              </>
            )}
            {activePage === 'projects' && (
              <>
                <ProjectGrid items={currentProjectItems} onTrackClick={handleLinkClick} />
                <Pagination current={currentProjectPage} total={totalProjectPages} onChange={setCurrentProjectPage} />
              </>
            )}
            {activePage === 'events' && (
              <>
                <EventsList items={currentEventItems} total={upcomingEvents.length} onTrackClick={handleLinkClick} />
                <Pagination current={currentEventsPage} total={totalEventsPages} onChange={setCurrentEventsPage} />
              </>
            )}
          </div>
        )}
      </main>

      <button
        onClick={scrollToTop}
        className={`fixed bottom-8 right-8 p-4 rounded-xl bg-orange-600 text-white shadow-[0_0_25px_rgba(234,88,12,0.4)] transition-all duration-300 z-[100] hover:scale-110 active:scale-95 border border-orange-400/50 ${
          showScrollTop ? 'opacity-100 translate-y-0' : 'opacity-0 translate-y-12 pointer-events-none'
        }`}
      >
        <ChevronLeft className="w-6 h-6 rotate-90" />
      </button>

      <footer className="footer">
        <div className="footer-inner">
          <div className="footer-brand">ClawBeat<span>.co</span></div>
        </div>
      </footer>
    </div>
  );
};

// ... Remaining Components (Pagination, NavButton, SortButton, NewsList, ResearchList, VideoGrid, ProjectGrid)
// Ensure they stay exactly as they were in your functional version.

const Pagination = ({ current, total, onChange }: { current: number; total: number; onChange: (p: number) => void }) => {
  if (total <= 1) return null;
  const handlePageChange = (newPage: number) => {
    onChange(newPage);
    window.scrollTo({ top: 0, behavior: 'smooth' });
  };

  // Build pill list: always show first/last, up to 3 around current, ellipsis elsewhere
  const pills: (number | '…')[] = [];
  if (total <= 7) {
    for (let i = 1; i <= total; i++) pills.push(i);
  } else if (current <= 4) {
    for (let i = 1; i <= 5; i++) pills.push(i);
    pills.push('…'); pills.push(total);
  } else if (current >= total - 3) {
    pills.push(1); pills.push('…');
    for (let i = total - 4; i <= total; i++) pills.push(i);
  } else {
    pills.push(1); pills.push('…');
    for (let i = current - 1; i <= current + 1; i++) pills.push(i);
    pills.push('…'); pills.push(total);
  }

  return (
    <div className="flex justify-center items-center gap-4 mt-16 pt-12 border-t border-white/5 flex-wrap">
      <button disabled={current === 1} onClick={() => handlePageChange(current - 1)} className="page-btn">
        ← Prev
      </button>
      <div className="page-pills">
        {pills.map((p, i) =>
          p === '…'
            ? <span key={`e${i}`} className="page-pill ellipsis">…</span>
            : <button key={p} onClick={() => handlePageChange(p as number)} className={`page-pill${p === current ? ' active' : ''}`}>{p}</button>
        )}
      </div>
      <div className="page-info">
        <span className="page-info-label">Page</span>
        <div className="page-info-nums">
          <span className="page-current">{current}</span>
          <span className="page-sep">/</span>
          <span className="page-total">{total}</span>
        </div>
      </div>
      <button disabled={current === total} onClick={() => handlePageChange(current + 1)} className="page-btn">
        Next →
      </button>
    </div>
  );
};

const SortButton = ({ active, onClick, label }: any) => (
  <button onClick={onClick} className={`px-3 py-1 text-[10px] font-black uppercase rounded transition-colors ${active ? 'bg-orange-600 text-white' : 'text-slate-500 hover:bg-white/5'}`}>
    {label}
  </button>
);

// Score an article for spotlight selection (higher = more prominent)
const scoreArticle = (item: NewsItem): number => {
  let score = (item.moreCoverage?.length || 0) * 3;
  if (item.source_type === 'priority') score += 2;
  if (checkIfVerified(item)) score += 1;
  return score;
};

const NewsList = ({ items, allNews, onTrackClick, spotlightOverrides, spotlightDays }: {
  items: NewsItem[];
  allNews: NewsItem[];
  onTrackClick: (t: string, s: string) => void;
  spotlightOverrides: SpotlightOverride[];
  spotlightDays: Set<string>;
}) => {
  // Group current-page items by day
  const grouped: Record<string, NewsItem[]> = {};
  for (const item of items) {
    const day = item.date || 'unknown';
    if (!grouped[day]) grouped[day] = [];
    grouped[day].push(item);
  }

  // Full sorted article list per day across ALL news — used for continuous numbering across pages
  const fullDayArticles = React.useMemo(() => {
    const byDay: Record<string, NewsItem[]> = {};
    for (const item of allNews) {
      const day = item.date || 'unknown';
      if (!byDay[day]) byDay[day] = [];
      byDay[day]!.push(item);
    }
    for (const day of Object.keys(byDay)) {
      byDay[day]!.sort((a, b) => {
        const aV = checkIfVerified(a), bV = checkIfVerified(b);
        if (aV !== bV) return aV ? -1 : 1;
        const w: Record<string, number> = { priority: 1, standard: 2, delist: 3 };
        const wDiff = (w[a.source_type || 'standard'] ?? 2) - (w[b.source_type || 'standard'] ?? 2);
        if (wDiff !== 0) return wDiff;
        return new Date(b.inserted_at || 0).getTime() - new Date(a.inserted_at || 0).getTime();
      });
    }
    return byDay;
  }, [allNews]);

  // Build override lookup: { "MM-DD-YYYY": { 1: override, 2: override, ... } }
  const overrideLookup: Record<string, Record<number, SpotlightOverride>> = {};
  for (const ov of spotlightOverrides) {
    if (!overrideLookup[ov.dispatch_date]) overrideLookup[ov.dispatch_date] = {};
    overrideLookup[ov.dispatch_date]![ov.slot] = ov;
  }

  const formatDispatchDay = (mdy: string) => {
    if (mdy === 'unknown') return 'Unknown';
    const parts = mdy.split('-').map(Number);
    const [m, d, y] = [parts[0] ?? 1, parts[1] ?? 1, parts[2] ?? 0];
    const months = ['JAN','FEB','MAR','APR','MAY','JUN','JUL','AUG','SEP','OCT','NOV','DEC'];
    return `${months[(m - 1)]} ${String(d).padStart(2, '0')} ${y}`;
  };

  const sortedDays = Object.keys(grouped).sort((a, b) => parseMDY(b) - parseMDY(a));

  return (
    <div className="flex flex-col">
      {sortedDays.map((day) => {
        const dayItems = grouped[day] ?? [];
        const dayOverrides = overrideLookup[day] || {};

        // Build spotlight slots 1–4: override takes priority, else algorithmic
        const overriddenUrls = new Set(
          Object.values(dayOverrides).map(ov => ov.url)
        );
        // Score-sorted queue for algorithm, excluding any URLs already manually placed
        const algoQueue = [...dayItems]
          .sort((a, b) => scoreArticle(b) - scoreArticle(a))
          .filter(item => !overriddenUrls.has(item.url));
        let queueIdx = 0;

        const spotlightSlots: (NewsItem | null)[] = [1, 2, 3, 4].map(slot => {
          if (dayOverrides[slot]) {
            // Manual override — build a NewsItem-shaped object from override data
            return {
              url:          dayOverrides[slot].url,
              title:        dayOverrides[slot].title   || '',
              source:       dayOverrides[slot].source  || '',
              summary:      dayOverrides[slot].summary || '',
              tags:         dayOverrides[slot].tags    || [],
              date:         day,
              moreCoverage: [],
            } as NewsItem;
          }
          // Algorithmic pick
          return algoQueue[queueIdx++] ?? null;
        });

        const leadSlot      = spotlightSlots[0];
        const alsoTodaySlots = spotlightSlots.slice(1).filter(Boolean) as NewsItem[];

        // Below-spotlight: current-page items for this day, in full-day sort order
        // Filtering from the full sorted day list preserves continuous numbering across pages
        const currentPageUrls = new Set(dayItems.map(i => i.url));
        const allArticles = (fullDayArticles[day] || []).filter(a => currentPageUrls.has(a.url));

        return (
          <React.Fragment key={day}>
            {/* ── Date divider ── */}
            <div className="date-divider">
              <span className="date-label">
                Dispatch: <span className="date-text">{formatDispatchDay(day)}</span>
              </span>
              <div className="date-line" />
              <span className="date-count">{dayItems.length} {dayItems.length === 1 ? 'story' : 'stories'}</span>
            </div>

            {/* ── Spotlight card (only on the page where this day first appears) ── */}
            {leadSlot && spotlightDays.has(day) && (() => {
              const isVerified = checkIfVerified(leadSlot);
              const isPriority = leadSlot.source_type === 'priority';
              const moreCov = (leadSlot.moreCoverage || []).filter(l => !l.source.toLowerCase().includes('facebook'));
              return (
                <div className="lead-card">
                  <div className="lead-body">
                    <div className="lead-flag">Lead Signal</div>
                    <h2 className="lead-headline">
                      <a href={leadSlot.url} target="_blank" rel="noopener noreferrer" onClick={() => onTrackClick(leadSlot.title, leadSlot.source)}>
                        {leadSlot.title}
                      </a>
                    </h2>
                    {leadSlot.summary && <p className="lead-summary">{leadSlot.summary}</p>}
                    <div className="story-meta">
                      <span className="meta-source">{formatSourceName(leadSlot.source)}</span>
                      <span className="meta-sep">·</span>
                      <span className="meta-date">{leadSlot.date}</span>
                      {isVerified && <span className="badge-verified">✓ verified</span>}
                      {isPriority && !isVerified && <span className="badge-priority">priority</span>}
                    </div>
                    {leadSlot.tags && leadSlot.tags.length > 0 && (
                      <div className="tags-strip">
                        {leadSlot.tags.map((tag, i) => <span key={i} className="tag">{tag}</span>)}
                      </div>
                    )}
                    {moreCov.length > 0 && (
                      <div className="coverage-strip" style={{marginTop: '0.75rem'}}>
                        <span className="coverage-label">// more coverage</span>
                        {moreCov.map((link, i) => (
                          <a key={i} href={link.url} target="_blank" rel="noopener noreferrer" className="coverage-link" onClick={() => onTrackClick(leadSlot.title, link.source)}>
                            {formatSourceName(link.source)}
                          </a>
                        ))}
                      </div>
                    )}
                  </div>
                  {alsoTodaySlots.length > 0 && (
                    <aside className="lead-sidebar">
                      <div className="sidebar-hdr">// also_today</div>
                      {alsoTodaySlots.map((item, idx) => (
                        <div key={idx} className="sidebar-item">
                          <div className="sidebar-num">{String(idx + 2).padStart(2, '0')} ›</div>
                          <div className="sidebar-title">
                            <a href={item.url} target="_blank" rel="noopener noreferrer" onClick={() => onTrackClick(item.title, item.source)}>
                              {item.title}
                            </a>
                          </div>
                          <div className="sidebar-source">
                            {formatSourceName(item.source)}{checkIfVerified(item) ? ' · verified' : ''}
                          </div>
                        </div>
                      ))}
                    </aside>
                  )}
                </div>
              );
            })()}

            {/* ── Full article list (all articles, spotlight is a separate environment) ── */}
            {allArticles.map((item) => {
              const isVerified = checkIfVerified(item);
              const isPriority = item.source_type === 'priority';
              const moreCov = (item.moreCoverage || []).filter(l => !l.source.toLowerCase().includes('facebook'));
              return (
                <div key={item.url} className={`story-item${isVerified || isPriority ? ' is-priority' : ''}`}>
                  <div className="story-body">
                    <div className="story-meta">
                      <span className="meta-source">{formatSourceName(item.source)}</span>
                      <span className="meta-sep">·</span>
                      <span className="meta-date">{item.date}</span>
                      {isVerified && <span className="badge-verified">✓ verified</span>}
                      {isPriority && !isVerified && <span className="badge-priority">priority</span>}
                    </div>
                    <div className="story-headline">
                      <a href={item.url} target="_blank" rel="noopener noreferrer" onClick={() => onTrackClick(item.title, item.source)}>
                        {item.title}
                      </a>
                    </div>
                    {item.summary && <p className="story-summary">{item.summary}</p>}
                    {item.tags && item.tags.length > 0 && (
                      <div className="tags-strip">
                        {item.tags.map((tag, i) => <span key={i} className="tag">{tag}</span>)}
                      </div>
                    )}
                    {moreCov.length > 0 && (
                      <div className="coverage-strip">
                        <span className="coverage-label">// more coverage</span>
                        {moreCov.map((link, i) => (
                          <a key={i} href={link.url} target="_blank" rel="noopener noreferrer" className="coverage-link" onClick={() => onTrackClick(item.title, link.source)}>
                            {formatSourceName(link.source)}
                          </a>
                        ))}
                      </div>
                    )}
                  </div>
                </div>
              );
            })}
          </React.Fragment>
        );
      })}
    </div>
  );
};

const ResearchList = ({ items, onTrackClick }: { items: ResearchItem[], onTrackClick: (t: string, s: string) => void }) => (
  <div className="flex flex-col gap-8">
    {items.map((paper, idx) => (
      <div key={idx} className="group border-l-2 border-white/5 hover:border-orange-500/50 pl-6 py-4 transition-all bg-white/[0.01] hover:bg-white/[0.03] rounded-r-xl relative">
        <a href={paper.url} target="_blank" rel="noopener noreferrer" onClick={() => onTrackClick(paper.title, 'ArXiv')} className="flex items-start justify-between gap-4">
          <h3 className="text-xl font-bold text-white group-hover:text-orange-500 transition-colors leading-tight pr-12">
            {paper.title}
          </h3>
          <ExternalLink className="w-5 h-5 mt-1 text-slate-600 group-hover:text-orange-500 transition-colors absolute right-6 top-6" />
        </a>
        <div className="flex flex-wrap items-center gap-3 mt-3">
          <div className="flex items-center gap-2 text-[10px] font-black text-slate-500 uppercase tracking-widest">
            <Microscope className="w-3 h-3 text-orange-500/50" />
            <span>{formatDate(paper.date)}</span>
          </div>
          <span className="text-white/10">|</span>
          <div className="text-[10px] font-bold text-slate-400 uppercase truncate max-w-xl italic tracking-tight">
            Authored by: {paper.authors.join(', ')}
          </div>
        </div>
        <p className="mt-4 text-sm text-slate-400 leading-relaxed italic border-t border-white/5 pt-4">
          <span className="text-orange-500/50 not-italic font-black text-[10px] uppercase tracking-widest mr-3">Intel Brief:</span>
          {paper.summary}
        </p>
      </div>
    ))}
  </div>
);

const VideoGrid = ({ items, onTrackClick }: { items: VideoItem[], onTrackClick: (t: string, s: string, type: string) => void }) => (
  <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-10">
    {items.map((video, idx) => (
      <div key={idx} className="group relative flex flex-col">
        <div className="relative aspect-video rounded-xl overflow-hidden mb-4 ring-1 ring-white/10 group-hover:ring-orange-500/50 transition-all shadow-lg">
          {video.thumbnail ? (
            <img src={video.thumbnail} className="w-full h-full object-cover group-hover:scale-105 transition-transform duration-500" alt={video.title} />
          ) : (
            <div className="w-full h-full bg-white/5 flex items-center justify-center"><span className="text-white/20 text-xs uppercase tracking-widest font-black">No Preview</span></div>
          )}
        </div>
        <h4 className="font-bold text-white text-lg group-hover:text-orange-500 line-clamp-2 leading-tight">{video.title}</h4>
        <p className="text-[10px] text-orange-500 mt-2 uppercase font-black tracking-widest">{video.channel} • {formatDate(video.publishedAt)}</p>
        {video.description && <p className="text-slate-400 text-xs mt-3 line-clamp-2 leading-relaxed italic">{video.description}</p>}
        <a href={video.url} target="_blank" rel="noopener noreferrer" onClick={() => onTrackClick(video.title, video.channel, 'video')} className="absolute inset-0 z-10" />
      </div>
    ))}
  </div>
);

const ProjectGrid = ({ items, onTrackClick }: { items: ProjectItem[], onTrackClick: (t: string, s: string, type: string) => void }) => (
  <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
    {items.map((p, idx) => (
      <div key={idx} className="group relative p-6 rounded-xl bg-white/[0.03] border border-white/5 hover:border-orange-500/40 transition-all flex flex-col justify-between shadow-sm">
        <div>
          <div className="flex justify-between items-start mb-4">
            <h4 className="text-lg font-bold text-white group-hover:text-orange-500 transition-colors tracking-tight">{p.name}</h4>
            <div className="flex items-center gap-1 bg-orange-500/10 px-2 py-1 rounded">
              <Star className="w-3 h-3 text-orange-500 fill-orange-500" />
              <span className="text-orange-500 font-black text-xs">{p.stars?.toLocaleString() ?? 0}</span>
            </div>
          </div>
          <p className="text-slate-400 text-sm leading-relaxed line-clamp-3 italic mb-6">{p.description}</p>
        </div>
        <div className="flex items-center justify-between mt-auto pt-4 border-t border-white/5">
          <div className="flex flex-col">
            <span className="text-[10px] text-slate-500 uppercase font-black tracking-widest">Author</span>
            <span className="text-xs text-white font-bold">{p.owner}</span>
          </div>
          <div className="flex flex-col text-right">
            <span className="text-[10px] text-slate-500 uppercase font-black tracking-widest">Created</span>
            <span className="text-xs text-white font-bold flex items-center gap-1"><Calendar className="w-3 h-3 text-orange-500/50" /> {formatDate(p.created_at)}</span>
          </div>
        </div>
        <a href={p.url} target="_blank" rel="noopener noreferrer" onClick={() => onTrackClick(p.name, p.owner, 'github_repo')} className="absolute inset-0 z-10" />
      </div>
    ))}
  </div>
);

const EventsList = ({ items, total, onTrackClick }: { items: EventItem[], total: number, onTrackClick: (t: string, s: string) => void }) => {
  if (total === 0) {
    return (
      <div className="flex flex-col items-center justify-center py-32 gap-6 text-center">
        <Calendar className="w-12 h-12 text-white/10" />
        <div>
          <p className="text-white font-black text-xl uppercase italic tracking-tight">No Upcoming Events</p>
          <p className="text-slate-500 text-sm mt-2 max-w-sm">Events are discovered automatically each day. Check back soon.</p>
        </div>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-4">
      {items.map((event, idx) => {
        const isVirtual = event.event_type === 'virtual';
        const locationParts = [event.location_city, event.location_state, event.location_country].filter(Boolean);
        const locationStr = locationParts.join(', ');
        const dateStr = event.start_date === event.end_date || !event.end_date
          ? event.start_date
          : `${event.start_date} – ${event.end_date}`;

        return (
          <div key={idx} className="group relative p-6 rounded-xl bg-white/[0.02] border border-white/5 hover:border-orange-500/30 transition-all flex flex-col gap-3">
            <div className="flex items-center gap-3 flex-wrap">
              {isVirtual ? (
                <span className="flex items-center gap-1.5 text-[9px] font-black text-orange-500 bg-orange-500/10 px-2 py-0.5 rounded uppercase tracking-wider">
                  <Globe className="w-2.5 h-2.5" /> Virtual
                </span>
              ) : (
                <span className="flex items-center gap-1.5 text-[9px] font-black text-slate-400 bg-white/5 border border-white/10 px-2 py-0.5 rounded uppercase tracking-wider">
                  <MapPin className="w-2.5 h-2.5" /> In-Person
                </span>
              )}
              {locationStr && !isVirtual && (
                <span className="text-[10px] font-black text-slate-500 uppercase tracking-wider">{locationStr}</span>
              )}
              {dateStr && (
                <span className="text-[10px] font-black text-slate-600 uppercase tracking-wider ml-auto">{dateStr}</span>
              )}
            </div>

            <a href={event.url} target="_blank" rel="noopener noreferrer" onClick={() => onTrackClick(event.title, event.organizer)} className="text-xl font-bold text-white group-hover:text-orange-500 leading-tight transition-colors flex items-start gap-2">
              <span className="flex-1">{event.title}</span>
              <ExternalLink className="w-4 h-4 mt-1.5 opacity-0 group-hover:opacity-40 transition-opacity flex-shrink-0" />
            </a>

            {event.organizer && (
              <span className="text-[10px] font-black text-slate-500 uppercase tracking-widest">{event.organizer}</span>
            )}
            {event.description && (
              <p className="text-slate-400 text-sm leading-relaxed line-clamp-3">{event.description}</p>
            )}
          </div>
        );
      })}
    </div>
  );
};

const container = document.getElementById('root');
if (container) createRoot(container).render(<App />);