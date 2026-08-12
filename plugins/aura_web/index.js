const {
  langsearchSearch,
  jinaReader,
  backendsPost,
  backendsGet,
  langsearchKey,
  loadConfig,
  toolText,
} = require('./lib/client');

function textResult(payload) {
  return toolText(payload);
}

module.exports = {
  id: 'aura_web',
  name: 'aura_web',
  register(api) {
    // LangSearch web_search provider lives in the sibling `langsearch` plugin
    // (keeps plugins.entries.langsearch.config.webSearch.apiKey as the key path).

    // Jina as web_fetch fallback provider
    try {
      api.registerWebFetchProvider({
        id: 'jina',
        label: 'Jina Reader',
        hint: 'URL → LLM-friendly markdown via r.jina.ai',
        envVars: ['JINA_API_KEY'],
        placeholder: '<>',
        signupUrl: 'https://jina.ai/reader',
        docsUrl: 'https://github.com/jina-ai/reader',
        credentialPath: 'plugins.entries.aura_web.config.jina.apiKey',
        getCredentialValue: () => undefined,
        createTool: () => ({
          description: 'Fetch a URL as markdown via Jina Reader.',
          parameters: {
            type: 'object',
            properties: { url: { type: 'string' } },
            required: ['url'],
          },
          execute: async (_id, params) => {
            const result = await jinaReader(params.url || params);
            return { content: [{ type: 'text', text: textResult(result) }] };
          },
        }),
      });
    } catch (err) {
      try {
        api.logger?.warn?.(`aura_web jina provider: ${err.message}`);
      } catch (_) {}
    }

    api.registerTool({
      name: 'langsearch_search',
      description:
        'Web search via LangSearch (free NL search with summaries). Use when Brave web_search fails or for summary-rich results. Requires LangSearch API key in openclaw.json.',
      parameters: {
        type: 'object',
        properties: {
          query: { type: 'string', description: 'Search query' },
          count: { type: 'number', description: '1-10 results (default 8)' },
        },
        required: ['query'],
      },
      execute: async (params) => textResult(await langsearchSearch(params.query, params.count)),
    });

    api.registerTool({
      name: 'jina_reader',
      description:
        'Fetch a single URL as clean markdown via Jina Reader (r.jina.ai). Prefer for article/docs reading before crawl4ai.',
      parameters: {
        type: 'object',
        properties: {
          url: { type: 'string', description: 'Full http(s) URL' },
        },
        required: ['url'],
      },
      execute: async (params) => textResult(await jinaReader(params.url)),
    });

    api.registerTool({
      name: 'crawl4ai',
      description:
        'LLM-friendly crawl/scrape via Crawl4AI (JS-capable when installed). Use for multi-page or JS-heavy pages after jina_reader/web_fetch fail.',
      parameters: {
        type: 'object',
        properties: {
          url: { type: 'string' },
          depth: { type: 'number' },
          max_pages: { type: 'number' },
        },
        required: ['url'],
      },
      execute: async (params) =>
        textResult(
          await backendsPost('/v1/crawl4ai', {
            url: params.url,
            depth: params.depth || 0,
            max_pages: params.max_pages || 1,
          })
        ),
    });

    api.registerTool({
      name: 'scrapling',
      description:
        'Adaptive scrape (Scrapling) for fragile DOMs / sites that change selectors. Optional css selector.',
      parameters: {
        type: 'object',
        properties: {
          url: { type: 'string' },
          css: { type: 'string' },
        },
        required: ['url'],
      },
      execute: async (params) =>
        textResult(await backendsPost('/v1/scrapling', { url: params.url, css: params.css || null })),
    });

    api.registerTool({
      name: 'crawlee_crawl',
      description:
        'Bulk site crawl with queues/retries (Crawlee-style). Returns job_id; poll with job_id for status. Use for multi-page research pipelines.',
      parameters: {
        type: 'object',
        properties: {
          url: { type: 'string' },
          max_pages: { type: 'number' },
          max_depth: { type: 'number' },
          job_id: { type: 'string', description: 'If set, poll existing job status' },
        },
      },
      execute: async (params) => {
        if (params.job_id) {
          return textResult(await backendsGet(`/v1/crawlee/${encodeURIComponent(params.job_id)}`));
        }
        if (!params.url) return textResult({ ok: false, error: 'url or job_id required' });
        return textResult(
          await backendsPost('/v1/crawlee', {
            url: params.url,
            max_pages: params.max_pages || 10,
            max_depth: params.max_depth || 2,
          })
        );
      },
    });

    api.registerTool({
      name: 'scrapegraph_extract',
      description:
        'Prompt→schema extraction from a page (ScrapeGraphAI). Use when user asks to pull specific fields from a site.',
      parameters: {
        type: 'object',
        properties: {
          url: { type: 'string' },
          prompt: { type: 'string' },
          schema_json: { type: 'string', description: 'Optional JSON object schema as string' },
        },
        required: ['url', 'prompt'],
      },
      execute: async (params) => {
        let schema = null;
        if (params.schema_json) {
          try {
            schema = JSON.parse(params.schema_json);
          } catch (_) {
            schema = null;
          }
        }
        return textResult(
          await backendsPost('/v1/scrapegraph', {
            url: params.url,
            prompt: params.prompt,
            schema,
          })
        );
      },
    });

    api.registerTool({
      name: 'browser_use',
      description:
        'Interactive browser agent (browser-use). Prefer OpenClaw `browser` first; use this for complex multi-step agent browsing. Falls back with hint if unavailable.',
      parameters: {
        type: 'object',
        properties: {
          task: { type: 'string' },
          start_url: { type: 'string' },
        },
        required: ['task'],
      },
      execute: async (params) =>
        textResult(
          await backendsPost(
            '/v1/browser-use',
            { task: params.task, start_url: params.start_url || null },
            180
          )
        ),
    });

    api.registerTool({
      name: 'obscura_browse',
      description:
        'Stealth/headless browse via Obscura CDP when configured (OBSCURA_CDP_URL). Otherwise degraded HTTP fetch. Use after browser anti-bot failures.',
      parameters: {
        type: 'object',
        properties: {
          url: { type: 'string' },
          action: { type: 'string' },
        },
        required: ['url'],
      },
      execute: async (params) =>
        textResult(
          await backendsPost('/v1/obscura', { url: params.url, action: params.action || 'fetch' })
        ),
    });

    api.registerTool({
      name: 'web_capability_status',
      description:
        'Report Aura web-stack health and routing cheat-sheet (which tool to use when). Call when asked what web tools are available.',
      parameters: {
        type: 'object',
        properties: {},
      },
      execute: async () => {
        const health = await backendsGet('/health');
        const routing = await backendsGet('/v1/routing');
        const cfg = loadConfig();
        const ls = !!langsearchKey(cfg);
        return textResult({
          ok: true,
          langsearch_configured: ls,
          brave_configured: !!cfg?.plugins?.entries?.brave?.config?.webSearch?.apiKey,
          openclaw_native: ['web_search', 'web_fetch', 'browser'],
          aura_tools: [
            'langsearch_search',
            'jina_reader',
            'crawl4ai',
            'scrapling',
            'crawlee_crawl',
            'scrapegraph_extract',
            'browser_use',
            'obscura_browse',
            'web_capability_status',
          ],
          backends: health,
          routing: routing?.routing || null,
          note: 'Perplexity/Firecrawl/Tavily are NOT configured unless explicitly added later.',
        });
      },
    });
  },
};
