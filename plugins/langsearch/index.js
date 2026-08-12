const path = require('path');
const client = require(path.join(__dirname, '..', 'aura_web', 'lib', 'client.js'));

module.exports = {
  id: 'langsearch',
  name: 'langsearch',
  register(api) {
    try {
      api.registerWebSearchProvider({
        id: 'langsearch',
        label: 'LangSearch',
        hint: 'Free natural-language web search with summaries',
        envVars: ['LANGSEARCH_API_KEY'],
        placeholder: '<>',
        signupUrl: 'https://langsearch.com/api-keys',
        docsUrl: 'https://docs.langsearch.com/api/web-search-api',
        autoDetectOrder: 20,
        credentialPath: 'plugins.entries.langsearch.config.webSearch.apiKey',
        getCredentialValue: () => client.langsearchKey(client.loadConfig()) || undefined,
        getConfiguredCredentialValue: () => client.langsearchKey(client.loadConfig()) || undefined,
        createTool: () => ({
          description: 'Search the web with LangSearch.',
          parameters: {
            type: 'object',
            properties: {
              query: { type: 'string' },
              count: { type: 'number' },
            },
            required: ['query'],
          },
          execute: async (_id, params) => {
            const query = typeof params === 'string' ? params : params?.query;
            const result = await client.langsearchSearch(query, params?.count || 8);
            return { content: [{ type: 'text', text: client.toolText(result) }] };
          },
        }),
      });
    } catch (err) {
      try { api.logger?.warn?.(`langsearch provider: ${err.message}`); } catch (_) {}
    }
  },
};
